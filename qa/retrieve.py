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

from dataclasses import dataclass
from pathlib import Path

from pipeline.score import Embedder
from store import db
from store.vectors import VectorIndex

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "corpus.sqlite"
DEFAULT_INDEX = ROOT / "data" / "vectors.npz"


@dataclass
class Passage:
    paper_id: str
    doi: str | None
    title: str | None
    text: str | None              # the grounding text (abstract)
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
        self._papers = {p["paper_id"]: p for p in db.iter_papers(self.conn)}

    def retrieve(self, query: str, k: int = 6, since: str | None = None,
                 paper_id: str | None = None) -> list[Passage]:
        """Top-k passages by cosine similarity, optionally filtered by paper or
        first_seen_date >= `since` (YYYY-MM-DD)."""
        qv = self.embedder.embed_query(query)
        fetch = k * 5 if (since or paper_id) else k  # over-fetch when filtering
        out: list[Passage] = []
        for pid, score in self.index.search(qv, k=fetch):
            p = self._papers.get(pid)
            if p is None:
                continue
            if paper_id and pid != paper_id:
                continue
            if since and (p.get("first_seen_date") or "") < since:
                continue
            out.append(Passage(
                paper_id=pid, doi=p.get("doi"), title=p.get("title"),
                text=p.get("abstract"), journal_or_server=p.get("journal_or_server"),
                published_date=p.get("published_date"), is_oa=bool(p.get("is_oa")),
                oa_fulltext_url=p.get("oa_fulltext_url"), is_full_text=False, score=score))
            if len(out) >= k:
                break
        return out

    def __len__(self) -> int:
        return len(self.index)
