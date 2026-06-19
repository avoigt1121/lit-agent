"""
pipeline/score.py — embed abstracts (Phase 1) + classify/relevance (Phase 2).

Phase 1 (this file): embed each paper's title+abstract with a LOCAL model
(BGE-small via fastembed by default — ONNX, no API key, fast on CPU; configurable
via the EMBEDDING_MODEL env var). Corpus passages are embedded with no prefix;
queries get the BGE retrieval instruction (embed_query) — the asymmetric setup
BGE expects. The same model must embed the corpus (offline) and queries (in the
Space), so changing it means re-embedding the corpus.

Phase 2 (TODO): classify each paper into BCC focus areas (embedding similarity to
interest_profile.yaml descriptors + a cheap-LLM confirm) and compute
relevance_score — graded against eval/relevance_set.json (§9.3 precision).
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import numpy as np
import yaml

DEFAULT_MODEL = os.environ.get("EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5")
# BGE v1.5 expects this instruction on the QUERY side only (not on passages).
_BGE_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "


class Embedder:
    """Thin wrapper over fastembed; lazy-loads the ONNX model on first use."""

    def __init__(self, model_name: str = DEFAULT_MODEL):
        self.model_name = model_name
        self._model = None

    def _ensure(self):
        if self._model is None:
            from fastembed import TextEmbedding
            self._model = TextEmbedding(self.model_name)
        return self._model

    def embed_passages(self, texts: list[str], *, batch_size: int = 0,
                       cooldown: float = 0.0) -> np.ndarray:
        """Embed texts. Default: one batched fastembed call (fast, no pauses).

        For a long offline backfill on a *fanless* Mac, pass batch_size + cooldown:
        the texts are embedded in batch_size-sized bursts with a `cooldown`-second
        pause between bursts. This keeps the chip below its thermal-throttle knee and
        sustains ~4-5 docs/s instead of collapsing to ~1 (see pipeline/census.py).
        Leave OFF (0) for the weekly pipeline and the latency-sensitive Q&A query path.
        """
        model = self._ensure()
        if batch_size > 0 and len(texts) > batch_size:
            chunks: list[np.ndarray] = []
            for i in range(0, len(texts), batch_size):
                chunks.append(np.array(list(model.embed(texts[i:i + batch_size])),
                                       dtype=np.float32))
                if cooldown > 0 and i + batch_size < len(texts):
                    time.sleep(cooldown)
            return np.vstack(chunks)
        return np.array(list(model.embed(texts)), dtype=np.float32)

    def embed_query(self, text: str) -> np.ndarray:
        prefix = _BGE_QUERY_PREFIX if "bge" in self.model_name.lower() else ""
        return self.embed_passages([prefix + text])[0]


def _embed_text(rec: dict) -> str:
    """Text to embed for a paper: title + abstract (abstract carries the signal)."""
    title = rec.get("title") or ""
    abstract = rec.get("abstract") or ""
    return f"{title}\n\n{abstract}".strip() or title or "(no text)"


def embed_corpus(records: list[dict], embedder: Embedder | None = None, *,
                 batch_size: int = 0, cooldown: float = 0.0) -> dict[str, np.ndarray]:
    """Embed every record's text; set embedding_id (== paper_id) on each record.

    Returns {embedding_id: vector}. Records lacking an abstract are still embedded
    on the title so they remain retrievable (with weaker signal). ``batch_size`` /
    ``cooldown`` are passed through to ``embed_passages`` for the census's
    thermal-throttle-aware backfill (default off — one batched call).
    """
    embedder = embedder or Embedder()
    texts = [_embed_text(r) for r in records]
    vectors = embedder.embed_passages(texts, batch_size=batch_size, cooldown=cooldown)
    out: dict[str, np.ndarray] = {}
    for rec, vec in zip(records, vectors):
        eid = rec["paper_id"]
        rec["embedding_id"] = eid
        out[eid] = vec
    return out


# ---------------------------------------------------------------------------
# Focus-area classification + relevance (Phase 2)
# ---------------------------------------------------------------------------

def load_interest_profile(path: str | Path = "config/interest_profile.yaml") -> dict:
    return yaml.safe_load(Path(path).read_text())


def area_descriptor(area: dict) -> str:
    """The text embedded to represent a focus area (name + audience + keywords)."""
    note = " ".join((area.get("audience_note") or "").split())
    kws = ", ".join(area.get("keywords", []))
    return f"{area['name']}. {note} Keywords: {kws}".strip()


def _unit_rows(mat: np.ndarray) -> np.ndarray:
    mat = np.asarray(mat, dtype=np.float32)
    return mat / (np.linalg.norm(mat, axis=1, keepdims=True) + 1e-12)


CLASSIFY_MODEL = os.environ.get("CLASSIFY_MODEL", "claude-haiku-4-5-20251001")


def classify_and_score(records: list[dict], vectors: dict[str, np.ndarray],
                       profile: dict, embedder: Embedder | None = None,
                       client=None) -> dict[str, dict]:
    """Multi-label focus-area assignment + relevance score.

    Stage 1 (embedding): each area is embedded from its descriptor; the paper's
    top ``candidate_top_k`` areas by cosine become candidates.
    Stage 2: if ``client`` (Anthropic) is given, an LLM confirm prunes the
    candidates to the areas that truly apply (or none) per the CLAUDE.md classify
    prompt; otherwise the key-free fallback keeps the single best area only if it
    clears ``fallback_min_confidence``.

    Sets ``focus_areas`` + ``relevance_score`` on each record; returns
    {paper_id: {area_id: score}} for topic_tags. Absolute thresholds are weak here
    (all PDAC text clusters) — the LLM confirm is the real precision lever (§9.3).
    """
    embedder = embedder or Embedder()
    areas = profile["focus_areas"]
    by_id = {a["id"]: a for a in areas}
    cfg = profile.get("classification", {})
    top_k = int(cfg.get("candidate_top_k", 2))
    max_areas = int(cfg.get("max_areas_per_paper", 2))
    floor = float(cfg.get("fallback_min_confidence", 0.68))

    area_ids = [a["id"] for a in areas]
    area_mat = _unit_rows(embedder.embed_passages([area_descriptor(a) for a in areas]))

    tags: dict[str, dict] = {}
    for rec in records:
        v = np.asarray(vectors[rec["paper_id"]], dtype=np.float32)
        v = v / (np.linalg.norm(v) + 1e-12)
        sims = area_mat @ v
        order = np.argsort(-sims)
        candidates = [(area_ids[i], float(sims[i])) for i in order[:top_k]]
        if client is not None:
            confirmed = confirm_with_llm(rec, [by_id[a] for a, _ in candidates], client)
            kept = sorted(confirmed.items(), key=lambda x: -x[1])[:max_areas]
        else:
            kept = [(a, s) for a, s in candidates[:1] if s >= floor]
        rec["focus_areas"] = [a for a, _ in kept]
        rec["relevance_score"] = round(kept[0][1], 4) if kept else 0.0
        tags[rec["paper_id"]] = {a: round(s, 4) for a, s in kept}
    return tags


def confirm_with_llm(rec: dict, candidate_areas: list[dict], client) -> dict[str, float]:
    """CLAUDE.md classify prompt, restricted to the embedding candidates; {} if none.

    Returns {area_id: confidence}. On any parse/API error returns {} — we never
    invent a false-positive assignment.
    """
    if not candidate_areas:
        return {}
    descs = "\n".join(
        f"- {a['id']}: {a['name']} — {' '.join((a.get('audience_note') or '').split())}"
        for a in candidate_areas)
    prompt = (
        "Given this paper's title and abstract, decide which of the listed focus "
        "areas it is GENUINELY about. Use only the title/abstract; do not guess "
        "from the topic in general. Return a JSON object mapping area id -> "
        "confidence (0-1) for the areas that truly apply, or {} if none fit. "
        "JSON only, no prose.\n\n"
        f"Title: {rec.get('title')}\n\n"
        f"Abstract: {(rec.get('abstract') or '(no abstract)')[:2500]}\n\n"
        f"Focus areas:\n{descs}")
    try:
        resp = client.messages.create(
            model=CLASSIFY_MODEL, max_tokens=200,
            messages=[{"role": "user", "content": prompt}])
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        data = json.loads(raw)
        valid = {a["id"] for a in candidate_areas}
        return {k: float(v) for k, v in data.items() if k in valid}
    except Exception:
        return {}
