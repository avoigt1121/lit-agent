"""
pipeline/citations.py — paper<->paper citation graph (ADR-0004, layer 2).

Builds a directed citation graph from Europe PMC's SANCTIONED citation links
(the public REST citations/references endpoints — NOT scraping). Endpoints are
keyed by EPMC's (source, ext_id) pair so an edge is stored even when one end is
outside our corpus; ``store.db.resolve_citation_endpoints`` later fills the
internal ``*_paper_id`` as the other end gets ingested.

Primary v1 use — **citation tracking** (CLAUDE.md backlog): track every paper
that CITES a configured target. Seed target = PMID 39636224 (Loveless et al.
PDAC single-cell atlas — a Steele-lab REFERENCE of interest, NOT a BCC/Sears
paper; do not label it "ours"). Targets live in config
``relationships.citations.track_targets``.

Optionally (off by default) it also pulls REFERENCES for newly-ingested corpus
papers, growing intra-corpus edges; that is heavier, so it is gated and
resumable via ``relationship_progress`` (layer ``citations``).

OFFLINE only, rate-limited + retrying (reuses harvest's polite session). Never
runs in the Space.

    python -m pipeline.citations            # fetch citers of configured targets
    python -m pipeline.citations --refs     # also pull refs for new corpus papers
"""
from __future__ import annotations

import argparse
import logging
import time
from pathlib import Path

from pipeline.harvest import POLITE_PAUSE, _session, load_config, request_json
from pipeline.score import load_interest_profile
from store import db

logger = logging.getLogger("citations")

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "corpus.sqlite"
PROFILE_PATH = ROOT / "config" / "interest_profile.yaml"
EPMC_REST = "https://www.ebi.ac.uk/europepmc/webservices/rest"
LAYER = "citations"


def _fetch_links(session, src: str, ext_id: str, kind: str,
                 page_size: int, max_pages: int) -> list[dict]:
    """Page the EPMC ``citations`` or ``references`` endpoint for one paper.

    Returns the raw link records ({id, source, ...}); kind in {citations, references}.
    """
    url = f"{EPMC_REST}/{src}/{ext_id}/{kind}"
    list_key = "citationList" if kind == "citations" else "referenceList"
    item_key = "citation" if kind == "citations" else "reference"
    out: list[dict] = []
    for page in range(1, max_pages + 1):
        params = {"format": "json", "pageSize": page_size, "page": page}
        data = request_json(session, url, params)
        items = (data.get(list_key) or {}).get(item_key) or []
        if not items:
            break
        out.extend(items)
        if len(items) < page_size:
            break
        time.sleep(POLITE_PAUSE)
    return out


def _edge_from_link(link: dict, *, target_src: str, target_ext: str, kind: str) -> dict | None:
    """Map an EPMC link record to a directed citation edge dict.

    For ``citations`` the linked paper CITES the target; for ``references`` the
    target (a corpus paper) cites the linked paper.
    """
    other_src = link.get("source")
    other_ext = link.get("id")
    if not other_src or not other_ext:
        return None
    if kind == "citations":
        return {"citing_src": other_src, "citing_ext_id": other_ext,
                "cited_src": target_src, "cited_ext_id": target_ext}
    return {"citing_src": target_src, "citing_ext_id": target_ext,
            "cited_src": other_src, "cited_ext_id": other_ext}


def track_targets(db_path: Path = DEFAULT_DB, *, profile_path: Path = PROFILE_PATH,
                  config: dict | None = None) -> dict:
    """Fetch + persist all papers citing each configured target. Returns a summary."""
    profile = load_interest_profile(profile_path)
    cit = (profile.get("relationships") or {}).get("citations") or {}
    if not cit.get("enabled", True):
        return {"skipped": "citations disabled in config"}
    targets = cit.get("track_targets") or []
    page_size = int(cit.get("page_size", 1000))
    max_pages = int(cit.get("max_pages", 25))

    cfg = config or load_config()
    session = _session(cfg.get("contact_email", ""), cfg.get("tool_name", "lit-agent"))

    conn = db.connect(db_path)
    db.init_schema(conn)

    total = 0
    per_target: dict[str, int] = {}
    for tgt in targets:
        src, ext = tgt.get("src", "MED"), str(tgt.get("ext_id"))
        links = _fetch_links(session, src, ext, "citations", page_size, max_pages)
        edges = [e for e in (_edge_from_link(l, target_src=src, target_ext=ext, kind="citations")
                             for l in links) if e]
        db.upsert_citation_edges(conn, edges)
        per_target[f"{src}/{ext}"] = len(edges)
        total += len(edges)
        logger.info("target %s/%s: %d citing edges", src, ext, len(edges))

    resolved = db.resolve_citation_endpoints(conn)
    conn.close()
    return {"targets": per_target, "edges": total, "endpoints_resolved": resolved}


def fetch_corpus_refs(db_path: Path = DEFAULT_DB, *, profile_path: Path = PROFILE_PATH,
                      config: dict | None = None, reindex_all: bool = False,
                      limit: int | None = None) -> dict:
    """Pull REFERENCES for new corpus papers (resumable). Off by default in v1.

    Only papers with an EPMC-resolvable id (a pmid -> MED) are queried.
    """
    profile = load_interest_profile(profile_path)
    cfg = config or load_config()
    session = _session(cfg.get("contact_email", ""), cfg.get("tool_name", "lit-agent"))
    cit = (profile.get("relationships") or {}).get("citations") or {}
    page_size = int(cit.get("page_size", 1000))
    max_pages = int(cit.get("max_pages", 25))

    conn = db.connect(db_path)
    db.init_schema(conn)
    done = set() if reindex_all else db.relationship_progress_present(conn, LAYER)

    n_papers = n_edges = 0
    pending: list[str] = []
    for rec in db.iter_papers(conn, include_excluded=False):
        pid = rec["paper_id"]
        if pid in done:
            continue
        pmid = (rec.get("ids") or {}).get("pmid")
        if pmid:  # only EPMC-addressable papers can be queried
            links = _fetch_links(session, "MED", str(pmid), "references", page_size, max_pages)
            edges = [e for e in (_edge_from_link(l, target_src="MED", target_ext=str(pmid),
                                                 kind="references") for l in links) if e]
            for e in edges:
                e["citing_paper_id"] = pid  # the corpus paper is the citer
            db.upsert_citation_edges(conn, edges, commit=False)
            n_edges += len(edges)
            time.sleep(POLITE_PAUSE)
        pending.append(pid)
        n_papers += 1
        if len(pending) >= 50:
            db.mark_relationship_progress(conn, LAYER, pending, commit=True)
            pending.clear()
        if limit and n_papers >= limit:
            break
    if pending:
        db.mark_relationship_progress(conn, LAYER, pending, commit=True)
    db.resolve_citation_endpoints(conn)
    conn.close()
    return {"papers_queried": n_papers, "ref_edges": n_edges}


def run(db_path: Path = DEFAULT_DB, *, do_refs: bool = False) -> dict:
    """Weekly entrypoint: track configured targets (+ optional corpus refs)."""
    out = track_targets(db_path)
    profile = load_interest_profile(PROFILE_PATH)
    cit = (profile.get("relationships") or {}).get("citations") or {}
    if do_refs or cit.get("fetch_corpus_refs"):
        out["refs"] = fetch_corpus_refs(db_path)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Build the citation graph (ADR-0004).")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--refs", action="store_true", help="Also pull references for new corpus papers.")
    ap.add_argument("--refs-all", action="store_true", help="Pull references for the WHOLE corpus.")
    ap.add_argument("--limit", type=int, default=None, help="Cap papers queried for --refs.")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    out = track_targets(args.db)
    print(f"Citation targets: {out}")
    if args.refs or args.refs_all:
        r = fetch_corpus_refs(args.db, reindex_all=args.refs_all, limit=args.limit)
        print(f"Corpus references: {r}")


if __name__ == "__main__":
    main()
