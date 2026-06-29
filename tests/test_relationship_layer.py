"""Tests for the ADR-0004 relationship DATA layer.

Covers the offline, network-free populators (mentions, derived relations, OHSU
map) plus the citation-edge accessors. The live EuropePMC citation FETCH
(pipeline.citations.track_targets) is not exercised here — it needs network — but
its DB write/resolve path is, via upsert_citation_edges + resolve_citation_endpoints.

Runnable two ways:
    .venv/bin/python -m pytest tests/test_relationship_layer.py
    .venv/bin/python tests/test_relationship_layer.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline import mentions, ohsu_map, relationships
from store import db


def _paper(pid, title, abstract, *, pmid=None, authors=None, fsd="2026-06-20"):
    return {"paper_id": pid, "doi": pid, "title": title, "abstract": abstract,
            "ids": {"pmid": pmid}, "authors": authors or [], "first_seen_date": fsd,
            "annotations": {"genes": [], "diseases": []}}


def _fresh_db():
    dbp = Path(tempfile.mkdtemp()) / "corpus.sqlite"
    conn = db.connect(dbp)
    db.init_schema(conn)
    conn.close()
    return dbp


def _seed(dbp):
    conn = db.connect(dbp)
    recs = [
        _paper("10.1/a", "MYC and KRAS drive PDAC",
               "MYC stability and KRAS G12D in pancreatic cancer; ATR signaling.",
               pmid="111", authors=["Sears RC", "Doe J"]),
        _paper("10.1/b", "MYC in neurons",
               "MYC and KRAS expression across the nervous system and brain.",
               pmid="222", authors=["Smith A"]),
        _paper("10.1/c", "A review of cars",
               "The car and the max value were measured. Nothing biological here.",
               pmid="333", authors=["Brody J"]),
    ]
    db.upsert_papers(conn, recs)
    db.set_topic_tags(conn, "10.1/a", {"myc": 0.9})
    db.set_topic_tags(conn, "10.1/b", {"myc": 0.8})
    conn.commit()
    conn.close()


def test_schema_version_is_6():
    dbp = _fresh_db()
    conn = db.connect(dbp)
    v = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
    conn.close()
    assert v == "6"


def test_mention_index_and_false_positive_guard():
    dbp = _fresh_db()
    _seed(dbp)
    mentions.index_mentions(dbp)
    conn = db.connect(dbp)
    # MYC is a literal mention in both the PDAC and the neuroscience paper.
    assert set(db.papers_mentioning(conn, "MYC", "gene")) == {"10.1/a", "10.1/b"}
    # Short all-caps symbols are matched case-sensitively: lowercase 'car'/'max'
    # in plain English must NOT register as the CAR/MAX gene symbols.
    assert db.papers_mentioning(conn, "CAR", "gene") == []
    assert db.papers_mentioning(conn, "MAX", "gene") == []
    conn.close()


def test_mentions_resumable():
    dbp = _fresh_db()
    _seed(dbp)
    assert mentions.index_mentions(dbp)["papers_indexed"] == 3
    assert mentions.index_mentions(dbp)["papers_indexed"] == 0  # already processed


def test_cross_field_shared_gene_edge():
    dbp = _fresh_db()
    _seed(dbp)
    mentions.index_mentions(dbp)
    relationships.derive_relations(dbp)
    conn = db.connect(dbp)
    rels = db.relations_for_paper(conn, "10.1/a", rel_type="shared_genes")
    conn.close()
    # The PDAC paper and the neuroscience paper share MYC+KRAS -> a cross-field edge.
    assert any(r["other_paper_id"] == "10.1/b"
               and set(r["evidence"]["genes"]) == {"MYC", "KRAS"} for r in rels)


def test_citation_edge_resolution_and_idempotency():
    dbp = _fresh_db()
    _seed(dbp)
    conn = db.connect(dbp)
    edge = {"citing_src": "MED", "citing_ext_id": "999",
            "cited_src": "MED", "cited_ext_id": "111"}  # external paper cites our pmid 111
    db.upsert_citation_edges(conn, [edge])
    assert db.resolve_citation_endpoints(conn) == 1      # 111 -> 10.1/a
    db.upsert_citation_edges(conn, [edge])               # re-run must not duplicate
    n = conn.execute("SELECT COUNT(*) FROM citation_edges").fetchone()[0]
    row = conn.execute("SELECT cited_paper_id FROM citation_edges").fetchone()[0]
    conn.close()
    assert n == 1 and row == "10.1/a"


def test_ohsu_seed_author_mapping():
    dbp = _fresh_db()
    _seed(dbp)
    ohsu_map.map_interests(dbp)
    conn = db.connect(dbp)
    sears = db.papers_for_interest(conn, "0000-0003-1558-2413")  # Rosalie Sears ORCID
    conn.close()
    assert "10.1/a" in sears


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
