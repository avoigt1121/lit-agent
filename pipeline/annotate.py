"""
pipeline/annotate.py — EPMC text-mined-annotation enrichment of the literal
mention index (ADR-0004, layer 1 enrichment).

WHY THIS EXISTS
The curated literal scan (``pipeline/mentions.py``) only finds the ~30-80 entities
someone listed in ``relationships.mentions`` config (``extra_genes`` +
``tracked_keywords`` + focus ``keywords``). The normalized record reserves
``annotations.genes/diseases`` but the harvest (``resultType=core`` search) never
populates them, so that field is empty on every row of the real corpus. This module
ADDS broad-recall coverage by pulling Europe PMC's actual text-mined annotations —
every gene/disease/chemical EPMC's NLP tagged — and writing them as
``method='epmc_annotation'`` rows in the SAME ``mentions`` table, ALONGSIDE (never
clobbering) the ``literal_scan`` rows.

HOW
Uses the SANCTIONED Europe PMC **Annotations API**
(``https://www.ebi.ac.uk/europepmc/annotations_api/annotationsByArticleIds``) — a
public TDM service, NOT the core search endpoint and NOT scraping. Reuses harvest's
polite retrying session (``_session`` / ``request_json`` / ``POLITE_PAUSE``) and
batches up to 8 articleIds per request (the API cap).

Each returned annotation occurrence -> one count toward a ``(entity_type, entity)``
mention. ``entity`` is the literal matched span (config ``entity_source: exact``,
the grounded default) or EPMC's preferred tag name (``preferred``). Only the
configured annotation types are kept.

OFFLINE only, resumable + new-papers-only via ``relationship_progress`` (layer
``annotations``), gated by config ``relationships.mentions.use_epmc_annotations``
(default OFF). Writes via ``db.set_mentions_for_method`` so the independent literal
scan is preserved. Never runs in the Space.

    python -m pipeline.annotate            # enrich new papers only (cap-limited)
    python -m pipeline.annotate --all      # re-enrich the whole corpus
"""
from __future__ import annotations

import argparse
import logging
import time
from collections import defaultdict
from pathlib import Path

from pipeline.harvest import POLITE_PAUSE, _session, load_config, request_json
from pipeline.score import load_interest_profile
from store import db

logger = logging.getLogger("annotate")

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "corpus.sqlite"
PROFILE_PATH = ROOT / "config" / "interest_profile.yaml"
ANNOTATIONS_API = "https://www.ebi.ac.uk/europepmc/annotations_api/annotationsByArticleIds"
LAYER = "annotations"

# EPMC's default type->our-type map (config overrides). Kept narrow on purpose —
# named entities, not topic concepts.
DEFAULT_TYPE_MAP = {
    "Gene_Proteins": "gene",
    "Diseases": "disease",
    "Chemicals": "chemical",
    "Organisms": "organism",
}


def _article_id(rec: dict) -> str | None:
    """EPMC articleId (``MED:<pmid>`` / ``PMC:<pmcid>``) for a record, else None.

    Only EPMC-addressable papers can be annotated. A pmid -> MED is preferred
    (every MED record is annotated); a pmcid -> PMC is the fallback.
    """
    ids = rec.get("ids") or {}
    pmid = ids.get("pmid")
    if pmid:
        return f"MED:{pmid}"
    pmcid = ids.get("pmcid")
    if pmcid:
        pmcid = str(pmcid)
        if not pmcid.upper().startswith("PMC"):
            pmcid = f"PMC{pmcid}"
        return f"PMC:{pmcid}"
    return None


def _fetch_annotations(session, article_ids: list[str], types: list[str]) -> dict[str, list[dict]]:
    """Fetch annotations for a batch of articleIds. Returns {articleId: [annotation,...]}.

    Batches up to ``len(article_ids)`` ids in one request (caller respects the
    EPMC cap of 8). ``types`` restricts the server-side annotation types.
    """
    params: list[tuple[str, str]] = [("format", "JSON")]
    params += [("articleIds", aid) for aid in article_ids]
    params += [("type", t) for t in types]
    data = request_json(session, ANNOTATIONS_API, params)
    out: dict[str, list[dict]] = {}
    # The API returns a JSON array of per-article objects (source/extId/annotations).
    for art in data if isinstance(data, list) else []:
        src, ext = art.get("source"), art.get("extId")
        if src and ext is not None:
            out[f"{src}:{ext}"] = art.get("annotations") or []
    return out


def map_annotations(annotations: list[dict], type_map: dict[str, str],
                    entity_source: str = "exact") -> list[dict]:
    """Map EPMC annotation occurrences -> deduped mention dicts with occurrence counts.

    Each annotation in EPMC's payload is a single text occurrence (it carries a
    matched ``exact`` span, a ``type``, and ``tags`` with a preferred name). We
    group by (our entity_type, entity surface) and ``count`` = #occurrences.
    ``entity`` = the literal ``exact`` span (grounded default) or the preferred
    ``tags[0].name`` when ``entity_source='preferred'``.
    """
    counts: dict[tuple[str, str], int] = defaultdict(int)
    for ann in annotations:
        etype = type_map.get(ann.get("type"))
        if not etype:
            continue
        entity = None
        if entity_source == "preferred":
            tags = ann.get("tags") or []
            if tags:
                entity = (tags[0].get("name") or "").strip()
        if not entity:  # exact, or preferred with no tag name
            entity = (ann.get("exact") or "").strip()
        if entity:
            counts[(etype, entity)] += 1
    return [{"entity_type": et, "entity": ent, "count": n}
            for (et, ent), n in counts.items()]


def enrich_annotations(db_path: Path = DEFAULT_DB, *, profile_path: Path = PROFILE_PATH,
                       config: dict | None = None, reindex_all: bool = False,
                       cap: int | None = None) -> dict:
    """Pull EPMC annotations for new (or all) EPMC-addressable papers. Returns a summary.

    Resumable via ``relationship_progress`` (layer ``annotations``); a paper with
    legitimately zero kept annotations is still marked done (so it is not re-queried
    every run). Writes annotation rows with ``set_mentions_for_method`` so the
    independently-run ``literal_scan`` rows are preserved.
    """
    profile = load_interest_profile(profile_path)
    rel = (profile.get("relationships") or {}).get("mentions") or {}
    ann_cfg = rel.get("annotations") or {}
    type_map = dict(ann_cfg.get("types") or DEFAULT_TYPE_MAP)
    types = list(type_map.keys())
    entity_source = str(ann_cfg.get("entity_source", "exact"))
    batch_size = max(1, min(int(ann_cfg.get("batch_size", 8)), 8))  # EPMC caps at 8
    if cap is None:
        cap = int(ann_cfg.get("per_run_cap", 2000))

    cfg = config or load_config()
    session = _session(cfg.get("contact_email", ""), cfg.get("tool_name", "lit-agent"))

    conn = db.connect(db_path)
    db.init_schema(conn)
    done = set() if reindex_all else db.relationship_progress_present(conn, LAYER)

    # Collect the candidate batch: new, EPMC-addressable papers (cap-limited).
    batch: list[tuple[str, str]] = []   # (paper_id, articleId)
    n_candidates = 0
    for rec in db.iter_papers(conn, include_excluded=False):
        pid = rec["paper_id"]
        if pid in done:
            continue
        aid = _article_id(rec)
        if not aid:
            # Not EPMC-addressable -> nothing to fetch, but mark done so we don't
            # re-scan it every run.
            db.mark_relationship_progress(conn, LAYER, [pid], commit=False)
            continue
        batch.append((pid, aid))
        n_candidates += 1
        if cap and n_candidates >= cap:
            break
    conn.commit()

    n_papers = n_annotated = n_mentions = 0
    for i in range(0, len(batch), batch_size):
        chunk = batch[i:i + batch_size]
        by_aid = {aid: pid for pid, aid in chunk}
        try:
            fetched = _fetch_annotations(session, list(by_aid.keys()), types)
        except Exception as exc:  # noqa: BLE001 — one bad batch must not lose the rest
            logger.warning("annotations batch failed (%d ids): %s", len(chunk), exc)
            continue
        pending: list[str] = []
        for aid, pid in by_aid.items():
            anns = fetched.get(aid) or []
            mentions = map_annotations(anns, type_map, entity_source)
            db.set_mentions_for_method(conn, pid, mentions, "epmc_annotation", commit=False)
            n_papers += 1
            n_mentions += len(mentions)
            if mentions:
                n_annotated += 1
            pending.append(pid)
        db.mark_relationship_progress(conn, LAYER, pending, commit=True)
        time.sleep(POLITE_PAUSE)
    conn.close()
    summary = {"papers_processed": n_papers, "papers_with_annotations": n_annotated,
               "annotation_mentions_written": n_mentions, "entity_source": entity_source}
    logger.info("annotations: %s", summary)
    return summary


def run(db_path: Path = DEFAULT_DB, *, profile_path: Path = PROFILE_PATH) -> dict:
    """Weekly entrypoint: only runs when ``use_epmc_annotations`` is enabled in config."""
    profile = load_interest_profile(profile_path)
    rel = (profile.get("relationships") or {}).get("mentions") or {}
    if not rel.get("use_epmc_annotations"):
        return {"skipped": "use_epmc_annotations disabled in config"}
    return enrich_annotations(db_path, profile_path=profile_path)


def main() -> None:
    ap = argparse.ArgumentParser(description="Enrich the mention index with EPMC annotations (ADR-0004).")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--all", action="store_true", help="Re-enrich the whole corpus (not just new papers).")
    ap.add_argument("--cap", type=int, default=None, help="Override the per-run new-paper cap.")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    s = enrich_annotations(args.db, reindex_all=args.all, cap=args.cap)
    print(f"Enriched {s['papers_processed']} papers "
          f"({s['papers_with_annotations']} with annotations, "
          f"{s['annotation_mentions_written']} mentions, entity_source={s['entity_source']}).")


if __name__ == "__main__":
    main()
