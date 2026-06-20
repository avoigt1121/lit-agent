"""
qa/retrieve.py — top-k retrieval over the ingested corpus (Phase 5).

Embeds the query with the SAME local model used for the corpus (BGE via
fastembed; embed_query adds the BGE retrieval instruction), searches the vector
index, and returns the top-k passages with their source paper's metadata for
citation. Read-only — the Space loads the corpus at startup and never ingests.

Currently every passage is an ABSTRACT (full-text ingestion is a later
expansion), flagged via is_full_text=False — answer.py's guard relies on that to
refuse fabricating methods that aren't in the abstract.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from pipeline.score import Embedder
from store import db
from store.vectors import VectorIndex

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "corpus.sqlite"
DEFAULT_INDEX = ROOT / "data" / "vectors.npz"

# Minimum cosine similarity for a passage to count as a real match. Genuine
# topical hits in this BGE-small corpus score ~0.83+; noise/meta queries ("what
# can you do") return their nearest neighbours at ~0.5-0.63 — letters, "Talks",
# "Issue Information", ChatGPT/Q&A papers. Without this floor those got fed to the
# grounding prompt and the model dutifully answered ABOUT the noise. Configurable
# via RETRIEVAL_MIN_SCORE.
DEFAULT_MIN_SCORE = float(os.environ.get("RETRIEVAL_MIN_SCORE", "0.70"))


@dataclass
class Passage:
    paper_id: str
    doi: str | None
    title: str | None
    text: str | None              # the grounding text (abstract)
    authors: list[str]            # for attribution in the answer's citation
    journal_or_server: str | None
    published_date: str | None
    is_oa: bool
    oa_fulltext_url: str | None
    is_full_text: bool            # False = abstract only (no OA full text ingested)
    score: float


class Retriever:
    """Loads the corpus + vector index once; answers retrieve() queries."""

    def __init__(self, db_path=DEFAULT_DB, index_path=DEFAULT_INDEX, embedder: Embedder | None = None):
        self.conn = db.connect(db_path)
        self.index = VectorIndex.load(index_path)
        self.embedder = embedder or Embedder()
        # Skip quarantined rows (off-topic / abstract-less): they keep their
        # vectors in the index but never enter _papers, so retrieve()'s
        # `self._papers.get(pid)` miss drops them from results.
        self._papers = {p["paper_id"]: p
                        for p in db.iter_papers(self.conn, include_excluded=False)}

    def retrieve(self, query: str, k: int = 6, since: str | None = None,
                 paper_id: str | None = None,
                 min_score: float | None = None) -> list[Passage]:
        """Top-k passages by cosine similarity, optionally filtered by paper or
        first_seen_date >= `since` (YYYY-MM-DD).

        Passages scoring below `min_score` (default ``DEFAULT_MIN_SCORE``) are
        dropped — so an off-topic or meta query ("what can you do") returns ``[]``
        rather than the corpus's least-bad noise. Pass a paper_id filter to bypass
        the floor for "drill into this specific paper" lookups.
        """
        floor = DEFAULT_MIN_SCORE if min_score is None else min_score
        if paper_id is not None:
            floor = -1.0  # explicit single-paper lookup: never gate on similarity
        qv = self.embedder.embed_query(query)
        # Over-fetch when filtering OR gating, so the floor doesn't starve k.
        fetch = k * 5 if (since or paper_id or floor > -1.0) else k
        out: list[Passage] = []
        for pid, score in self.index.search(qv, k=fetch):
            if score < floor:
                break  # search() returns descending score — nothing better remains
            p = self._papers.get(pid)
            if p is None:
                continue
            if paper_id and pid != paper_id:
                continue
            if since and (p.get("first_seen_date") or "") < since:
                continue
            out.append(Passage(
                paper_id=pid, doi=p.get("doi"), title=p.get("title"),
                text=p.get("abstract"), authors=p.get("authors") or [],
                journal_or_server=p.get("journal_or_server"),
                published_date=p.get("published_date"), is_oa=bool(p.get("is_oa")),
                oa_fulltext_url=p.get("oa_fulltext_url"), is_full_text=False, score=score))
            if len(out) >= k:
                break
        return out

    def __len__(self) -> int:
        return len(self.index)
