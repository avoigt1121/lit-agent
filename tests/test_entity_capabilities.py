"""Tests for the read-side entity capabilities built on the ADR-0004 mentions index.

Network-free + key-free:
  Capability 1 — entity-mention lookup in chat (qa/corpus_qa.papers_mentioning_text
                 + its wiring as the planner's find_papers_mentioning tool).
  Capability 2 — entity leaderboards (pipeline/analytics.entity_leaderboards + _html).

Runnable two ways:
    .venv/bin/python -m pytest tests/test_entity_capabilities.py
    .venv/bin/python tests/test_entity_capabilities.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline import analytics
from qa import corpus_qa
from qa.planner import QueryPlanner, tool_specs
from store import db


def _paper(pid, title, fsd="2026-06-20"):
    return {"paper_id": pid, "doi": pid, "title": title, "abstract": "a",
            "ids": {}, "authors": ["Sears RC"], "first_seen_date": fsd,
            "journal_or_server": "J", "published_date": "2026-01-01", "is_oa": 1}


def _seed():
    dbp = Path(tempfile.mkdtemp()) / "corpus.sqlite"
    conn = db.connect(dbp)
    db.init_schema(conn)
    db.upsert_papers(conn, [_paper("10.1/a", "MYC paper"),
                            _paper("10.1/b", "KRAS + gemcitabine paper"),
                            _paper("10.1/c", "gemcitabine review")])
    # Curated literal_scan + broad-recall epmc_annotation rows (merge-aware).
    db.set_mentions_for_method(conn, "10.1/a", [{"entity_type": "gene", "entity": "MYC", "count": 3}], "literal_scan")
    db.set_mentions_for_method(conn, "10.1/b", [{"entity_type": "gene", "entity": "KRAS", "count": 2}], "literal_scan")
    # gemcitabine is an annotation-only entity (no curated lexicon term for it).
    db.set_mentions_for_method(conn, "10.1/b", [{"entity_type": "chemical", "entity": "gemcitabine", "count": 1},
                                               {"entity_type": "disease", "entity": "PDAC", "count": 2}], "epmc_annotation")
    db.set_mentions_for_method(conn, "10.1/c", [{"entity_type": "chemical", "entity": "gemcitabine", "count": 4},
                                               {"entity_type": "disease", "entity": "PDAC", "count": 1}], "epmc_annotation")
    conn.commit()
    conn.close()
    return dbp


class _FakeRetriever:
    def __init__(self, dbp):
        self.db_path = dbp
        self.conn = db.connect(dbp)
        self._papers = {}


# --- Capability 1: entity-mention lookup -------------------------------------

def test_papers_mentioning_text_annotation_only_entity():
    r = _FakeRetriever(_seed())
    out = corpus_qa.papers_mentioning_text(r, "gemcitabine")
    # Both annotation-only papers surface, with a count header and DOIs.
    assert "**2** papers" in out
    assert "gemcitabine" in out and "10.1/b" in out and "10.1/c" in out
    assert "10.1/a" not in out  # the MYC paper does not mention gemcitabine


def test_papers_mentioning_text_type_filter_and_empty():
    r = _FakeRetriever(_seed())
    # Type filter: 'PDAC' as a disease hits both annotated papers.
    assert "**2** papers" in corpus_qa.papers_mentioning_text(r, "PDAC", "disease")
    # Wrong type -> no match, graceful message.
    assert "No papers" in corpus_qa.papers_mentioning_text(r, "gemcitabine", "gene")
    # Unknown entity -> graceful message.
    assert "No papers" in corpus_qa.papers_mentioning_text(r, "BRCA2")


def test_entity_count_question_defers_to_planner():
    # "how many papers mention SMAD4?" matches the corpus-size regex but must DEFER
    # (return None) so the planner's find_papers_mentioning answers the per-entity
    # count — otherwise it wrongly returns total corpus size.
    r = _FakeRetriever(_seed())
    assert corpus_qa.answer_meta("How many papers mention SMAD4?", r, {}) is None
    assert corpus_qa.answer_meta("how many papers are about gemcitabine?", r, {}) is None
    # A plain size question (no entity constraint) still answers directly.
    out = corpus_qa.answer_meta("How many papers do you have?", r, {})
    assert out is not None and "active PDAC papers" in out


def test_planner_exposes_and_dispatches_find_papers_mentioning():
    specs = {s["name"]: s for s in tool_specs({})}
    assert "find_papers_mentioning" in specs
    assert specs["find_papers_mentioning"]["input_schema"]["properties"]["entity_type"]["enum"] \
        == list(corpus_qa._MENTION_TYPES)
    # Dispatch routes to the corpus_qa wrapper (no LLM client needed for dispatch).
    p = QueryPlanner(_FakeRetriever(_seed()), client=None, profile={})
    out = p._dispatch("find_papers_mentioning", {"entity": "MYC", "entity_type": "gene"})
    assert "**1** paper" in out and "10.1/a" in out


# --- Capability 2: entity leaderboards ---------------------------------------

def test_entity_leaderboards_counts_distinct_papers():
    r = _FakeRetriever(_seed())
    lb = analytics.entity_leaderboards(r.conn, top_n=10)
    assert dict(lb["gene"]).get("MYC") == 1
    assert dict(lb["chemical"]).get("gemcitabine") == 2   # 2 distinct papers
    assert dict(lb["disease"]).get("PDAC") == 2
    # gemcitabine leads the chemical board.
    assert lb["chemical"][0][0] == "gemcitabine"


def test_entity_leaderboards_html_renders_and_handles_empty():
    r = _FakeRetriever(_seed())
    html = analytics.entity_leaderboards_html(analytics.entity_leaderboards(r.conn))
    assert "gemcitabine" in html and "Genes / proteins" in html and "Drugs / chemicals" in html
    assert "No entity-mention data" in analytics.entity_leaderboards_html({})


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
