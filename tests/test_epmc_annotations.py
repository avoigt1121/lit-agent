"""Tests for the EPMC text-mined-annotation enrichment (ADR-0004, layer-1 enrich).

Network-FREE: a fake annotations-API payload is fed through the mapping + the
merge-aware writer. The single live smoke call against the real Annotations API
(used once to confirm endpoint/response shape for pmid 39636224) is NOT exercised
here.

Asserts:
  (a) annotation rows land with method='epmc_annotation',
  (b) existing literal_scan rows for the SAME paper are preserved (merge-aware),
  (c) resumability skips already-processed papers,
  (d) papers_mentioning() now returns annotation-only entities,
  (e) occurrence counting + entity_source (exact vs preferred) + type filtering.

Runnable two ways:
    .venv/bin/python -m pytest tests/test_epmc_annotations.py
    .venv/bin/python tests/test_epmc_annotations.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline import annotate
from store import db

TYPE_MAP = {"Gene_Proteins": "gene", "Diseases": "disease", "Chemicals": "chemical"}


def _ann(exact, typ, name=None):
    return {"exact": exact, "type": typ, "tags": [{"name": name}] if name else []}


# Shape mirrors the real EPMC annotationsByArticleIds response: a list of
# per-article objects, each with source/extId/annotations.
FAKE_PAYLOAD = [
    {"source": "MED", "extId": "111", "annotations": [
        _ann("MYC", "Gene_Proteins", name="MYC proto-oncogene"),
        _ann("c-Myc", "Gene_Proteins", name="MYC proto-oncogene"),  # diff span, same exact-group
        _ann("MYC", "Gene_Proteins", name="MYC proto-oncogene"),    # repeat occurrence
        _ann("pancreatic ductal adenocarcinoma", "Diseases", name="PDAC"),
        _ann("gemcitabine", "Chemicals", name="gemcitabine"),
        _ann("mouse", "Organisms", name="Mus musculus"),            # type not in TYPE_MAP -> dropped
    ]},
]


def _paper(pid, pmid):
    return {"paper_id": pid, "doi": pid, "title": "t", "abstract": "a",
            "ids": {"pmid": pmid}, "authors": [], "first_seen_date": "2026-06-20",
            "annotations": {"genes": [], "diseases": []}}


def _fresh_db():
    dbp = Path(tempfile.mkdtemp()) / "corpus.sqlite"
    conn = db.connect(dbp)
    db.init_schema(conn)
    db.upsert_papers(conn, [_paper("10.1/a", "111")])
    # Pre-existing literal_scan mention on the same paper — must survive enrichment.
    db.set_mentions(conn, "10.1/a",
                    [{"entity_type": "gene", "entity": "KRAS",
                      "method": "literal_scan", "count": 2}])
    conn.commit()
    conn.close()
    return dbp


class _FakeSession:
    """Stands in for harvest._session; records calls, returns the fake payload."""
    def __init__(self):
        self.calls = 0


def _patch_fetch(monkeypatchish, payload=FAKE_PAYLOAD):
    """Replace network calls with a fixture. Returns a restore() callable."""
    orig_session = annotate._session
    orig_fetch = annotate._fetch_annotations
    orig_cfg = annotate.load_config

    def fake_session(*a, **k):
        return _FakeSession()

    def fake_fetch(session, article_ids, types):
        session.calls += 1
        by = {f"{a['source']}:{a['extId']}": a["annotations"] for a in payload}
        return {aid: by.get(aid, []) for aid in article_ids}

    annotate._session = fake_session
    annotate._fetch_annotations = fake_fetch
    annotate.load_config = lambda *a, **k: {"contact_email": "", "tool_name": "t"}

    def restore():
        annotate._session = orig_session
        annotate._fetch_annotations = orig_fetch
        annotate.load_config = orig_cfg
    return restore


def test_map_annotations_exact_counts_and_type_filter():
    rows = annotate.map_annotations(FAKE_PAYLOAD[0]["annotations"], TYPE_MAP, "exact")
    by = {(r["entity_type"], r["entity"]): r["count"] for r in rows}
    assert by[("gene", "MYC")] == 2          # two "MYC" occurrences grouped
    assert by[("gene", "c-Myc")] == 1
    assert by[("disease", "pancreatic ductal adenocarcinoma")] == 1
    assert by[("chemical", "gemcitabine")] == 1
    assert all(et != "organism" for (et, _) in by)  # Organisms not in TYPE_MAP


def test_map_annotations_preferred_name():
    rows = annotate.map_annotations(FAKE_PAYLOAD[0]["annotations"], TYPE_MAP, "preferred")
    by = {(r["entity_type"], r["entity"]): r["count"] for r in rows}
    # "MYC" + "c-Myc" share the preferred tag name -> grouped into one (count 3).
    assert by[("gene", "MYC proto-oncogene")] == 3
    assert by[("disease", "PDAC")] == 1


def test_enrich_writes_annotation_rows_and_preserves_literal_scan():
    dbp = _fresh_db()
    restore = _patch_fetch(None)
    try:
        s = annotate.enrich_annotations(dbp)
    finally:
        restore()
    assert s["papers_processed"] == 1 and s["papers_with_annotations"] == 1
    conn = db.connect(dbp)
    rows = conn.execute(
        "SELECT entity, method, count FROM mentions WHERE paper_id='10.1/a' ORDER BY method, entity"
    ).fetchall()
    methods = {r[1] for r in rows}
    # (a) annotation rows present, (b) literal_scan preserved
    assert "epmc_annotation" in methods and "literal_scan" in methods
    kras = [r for r in rows if r[0] == "KRAS"][0]
    assert kras[1] == "literal_scan" and kras[2] == 2   # untouched
    # (d) an annotation-only entity is now queryable
    assert db.papers_mentioning(conn, "gemcitabine", "chemical") == ["10.1/a"]
    assert db.papers_mentioning(conn, "MYC", "gene") == ["10.1/a"]
    conn.close()


def test_enrich_is_resumable():
    dbp = _fresh_db()
    restore = _patch_fetch(None)
    try:
        assert annotate.enrich_annotations(dbp)["papers_processed"] == 1
        # Second run: the paper is already marked done -> nothing processed.
        assert annotate.enrich_annotations(dbp)["papers_processed"] == 0
    finally:
        restore()


def test_non_addressable_paper_marked_done_without_fetch():
    dbp = Path(tempfile.mkdtemp()) / "corpus.sqlite"
    conn = db.connect(dbp)
    db.init_schema(conn)
    # No pmid/pmcid -> not EPMC-addressable.
    rec = {"paper_id": "10.1/x", "doi": "10.1/x", "title": "t", "abstract": "a",
           "ids": {}, "authors": [], "first_seen_date": "2026-06-20"}
    db.upsert_papers(conn, [rec])
    conn.commit()
    conn.close()
    restore = _patch_fetch(None)
    try:
        s = annotate.enrich_annotations(dbp)
    finally:
        restore()
    assert s["papers_processed"] == 0  # nothing fetched
    conn = db.connect(dbp)
    assert "10.1/x" in db.relationship_progress_present(conn, "annotations")  # still marked done
    conn.close()


def test_run_respects_config_flag():
    # annotate.run() must no-op unless use_epmc_annotations is enabled — independent
    # of the shipped config default (which is ON now that the layer is deployed).
    dbp = _fresh_db()
    prof_off = Path(tempfile.mkdtemp()) / "profile.yaml"
    prof_off.write_text("relationships:\n  mentions:\n    use_epmc_annotations: false\n")
    assert annotate.run(dbp, profile_path=prof_off).get("skipped")

    prof_on = Path(tempfile.mkdtemp()) / "profile.yaml"
    prof_on.write_text("relationships:\n  mentions:\n    use_epmc_annotations: true\n")
    restore = _patch_fetch(None)
    try:
        out = annotate.run(dbp, profile_path=prof_on)  # enabled -> actually enriches
    finally:
        restore()
    assert not out.get("skipped") and out["papers_processed"] == 1


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
