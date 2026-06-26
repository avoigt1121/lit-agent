"""Regression test for the digest window filter.

Bug: make_digest() rendered the ENTIRE corpus instead of only papers NEW in the
run window, so last year's high-relevance census papers crowded out this week's
harvest. "New" is keyed on first_seen_date (store/db.py). This test builds a
corpus straddling the window boundary and asserts only in-window papers render.

Runnable two ways:
    .venv/bin/python -m pytest tests/test_digest_window.py
    .venv/bin/python tests/test_digest_window.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline import run_weekly
from store import db


def _paper(pid: str, title: str, first_seen: str, *, area: str = "kras_ras_mapk",
           score: float = 0.9) -> dict:
    """A minimal normalized record good enough for upsert + digest rendering."""
    return {
        "paper_id": pid,
        "doi": pid,
        "title": title,
        "abstract": f"Abstract for {title}.",
        "ids": {}, "authors": [], "journal_or_server": "Test J",
        "published_date": first_seen, "first_seen_date": first_seen,
        "is_oa": True, "oa_fulltext_url": None, "source": "europepmc",
        "is_preprint": False, "linked_published_doi": None,
        "mesh": [], "annotations": {},
        "focus_areas": [area], "relevance_score": score, "embedding_id": pid,
    }


def _seed_db(path: Path) -> None:
    conn = db.connect(path)
    db.init_schema(conn)
    db.upsert_papers(conn, [
        # In window (2026-06-22 .. 2026-06-26)
        _paper("p_new1", "Fresh KRAS finding this week", "2026-06-23"),
        _paper("p_new2", "Another new PDAC paper", "2026-06-25"),
        # Out of window — last year's census; high score so it WOULD dominate
        # top-N if the filter were missing.
        _paper("p_old1", "Stale census paper from last year", "2025-06-23", score=0.99),
        _paper("p_old2", "Older boundary paper just before window", "2026-06-21", score=0.99),
    ])
    conn.close()


def test_digest_includes_only_in_window_papers(tmp_path, monkeypatch):
    db_path = tmp_path / "corpus.sqlite"
    _seed_db(db_path)

    # Redirect side-effect writes (out/digest_*.html, data/analytics.json) to tmp.
    monkeypatch.setattr(run_weekly, "ROOT", tmp_path)
    (tmp_path / "out").mkdir()
    (tmp_path / "data").mkdir()

    window = {"from": "2026-06-22", "to": "2026-06-26"}
    _path, html = run_weekly.make_digest(window, db_path=db_path, client=None)

    assert "Fresh KRAS finding this week" in html
    assert "Another new PDAC paper" in html
    # The window filter must drop both out-of-window papers, even though their
    # relevance_score (0.99) is higher than the in-window papers'.
    assert "Stale census paper from last year" not in html
    assert "Older boundary paper just before window" not in html
    # Header reflects the windowed denominator (2 papers), not the full corpus.
    assert "of 2 papers matched a focus area" in html


if __name__ == "__main__":  # allow running without pytest installed
    import tempfile

    class _MP:
        def __init__(self): self._undo = []
        def setattr(self, obj, name, val):
            self._undo.append((obj, name, getattr(obj, name)))
            setattr(obj, name, val)
        def undo(self):
            for obj, name, val in reversed(self._undo):
                setattr(obj, name, val)

    with tempfile.TemporaryDirectory() as d:
        mp = _MP()
        try:
            test_digest_includes_only_in_window_papers(Path(d), mp)
        finally:
            mp.undo()
    print("OK — digest window filter test passed")
