"""
pipeline/relationships.py — derived paper<->paper relationship edges (ADR-0004, layer 3).

Turns the literal substrate (layer 1 mentions, layer 2 citations) plus the
existing focus-area classification into explicit, queryable paper<->paper edges
in ``paper_relations`` — the data future "related papers" / "cross-field
correlation alert" tools consume. NO LLM, NO inference: every edge is a
deterministic, evidence-bearing fact (shared literal genes, shared focus area, a
real citation link).

Edge types (config ``relationships.derived_relations``):
  - ``shared_genes``  weight = # shared literal genes; evidence = the gene list.
                      Bounds the combinatorics: a paper is only compared to the
                      papers it co-mentions a gene with, capped at ``max_neighbors``.
  - ``shared_focus``  emitted (when ``shared_focus: true``) for a gene-neighbor
                      pair that ALSO shares a focus area; evidence = the areas.
                      Deliberately NOT computed over all co-classified pairs
                      (that is combinatorial across the ~44.5k corpus — §9.3).
  - ``citation``      a real directed citation between the two (from layer 2),
                      surfaced as an undirected "related-via-citation" edge.

The cross-FIELD signal (e.g. a PDAC paper sharing a gene with a nervous-system
paper) falls straight out of ``shared_genes`` + the two papers' differing
``focus_areas`` — no separate machinery needed.

OFFLINE only, new-papers-only, resumable via ``relationship_progress`` (layer
``relations``). Run mentions.py (and optionally citations.py) FIRST.

    python -m pipeline.relationships
    python -m pipeline.relationships --all
"""
from __future__ import annotations

import argparse
import json
import logging
from collections import Counter
from pathlib import Path

from pipeline.score import load_interest_profile
from store import db

logger = logging.getLogger("relationships")

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "corpus.sqlite"
PROFILE_PATH = ROOT / "config" / "interest_profile.yaml"
LAYER = "relations"


def _paper_genes(conn, paper_id: str) -> set[str]:
    rows = conn.execute(
        "SELECT DISTINCT entity FROM mentions WHERE paper_id=? AND entity_type='gene'",
        (paper_id,))
    return {r[0] for r in rows}


def _focus_areas(conn, paper_id: str) -> set[str]:
    rows = conn.execute("SELECT focus_area FROM topic_tags WHERE paper_id=?", (paper_id,))
    return {r[0] for r in rows}


def _citation_neighbors(conn, paper_id: str) -> set[str]:
    """Corpus paper_ids with a resolved citation edge to/from ``paper_id``."""
    rows = conn.execute(
        "SELECT cited_paper_id FROM citation_edges WHERE citing_paper_id=? AND cited_paper_id IS NOT NULL "
        "UNION SELECT citing_paper_id FROM citation_edges WHERE cited_paper_id=? AND citing_paper_id IS NOT NULL",
        (paper_id, paper_id))
    return {r[0] for r in rows if r[0] and r[0] != paper_id}


def _gene_candidates(conn, genes: set[str], self_id: str, cap: int) -> Counter:
    """Counter of {other_paper_id: # shared genes} via the mention index."""
    tally: Counter = Counter()
    for g in genes:
        for pid in db.papers_mentioning(conn, g, entity_type="gene"):
            if pid != self_id:
                tally[pid] += 1
    # Cap to the strongest neighbors to bound write volume / combinatorics.
    return Counter(dict(tally.most_common(cap * 4)))  # over-fetch; filtered by threshold next


def derive_for_paper(conn, paper_id: str, cfg: dict) -> list[dict]:
    """Compute all derived edges for one paper. Returns relation dicts."""
    min_shared = int(cfg.get("min_shared_genes", 2))
    max_neighbors = int(cfg.get("max_neighbors", 25))
    want_focus = bool(cfg.get("shared_focus", True))

    genes = _paper_genes(conn, paper_id)
    my_focus = _focus_areas(conn, paper_id)
    cands = _gene_candidates(conn, genes, paper_id, max_neighbors) if genes else Counter()
    cite_neighbors = _citation_neighbors(conn, paper_id)

    rels: list[dict] = []

    # shared_genes (+ shared_focus on the same bounded neighbor set)
    strong = [pid for pid, n in cands.most_common(max_neighbors) if n >= min_shared]
    for pid in strong:
        shared = sorted(genes & _paper_genes(conn, pid))
        if len(shared) < min_shared:
            continue
        rels.append({"src_paper_id": paper_id, "dst_paper_id": pid,
                     "rel_type": "shared_genes", "weight": float(len(shared)),
                     "evidence": {"genes": shared}})
        if want_focus:
            shared_areas = sorted(my_focus & _focus_areas(conn, pid))
            if shared_areas:
                rels.append({"src_paper_id": paper_id, "dst_paper_id": pid,
                             "rel_type": "shared_focus", "weight": float(len(shared_areas)),
                             "evidence": {"focus_areas": shared_areas}})

    # citation edges -> undirected related-via-citation
    for pid in cite_neighbors:
        rels.append({"src_paper_id": paper_id, "dst_paper_id": pid,
                     "rel_type": "citation", "weight": 1.0,
                     "evidence": {"via": "europepmc_citation"}})
    return rels


def derive_relations(db_path: Path = DEFAULT_DB, *, profile_path: Path = PROFILE_PATH,
                     reindex_all: bool = False) -> dict:
    """Populate ``paper_relations`` for new (or all) papers. Returns a summary."""
    profile = load_interest_profile(profile_path)
    cfg = (profile.get("relationships") or {}).get("derived_relations") or {}

    conn = db.connect(db_path)
    db.init_schema(conn)
    done = set() if reindex_all else db.relationship_progress_present(conn, LAYER)

    n_papers = 0
    by_type: Counter = Counter()
    pending: list[str] = []
    for rec in db.iter_papers(conn, include_excluded=False):
        pid = rec["paper_id"]
        if pid in done:
            continue
        rels = derive_for_paper(conn, pid, cfg)
        db.upsert_paper_relations(conn, rels, commit=False)
        for r in rels:
            by_type[r["rel_type"]] += 1
        n_papers += 1
        pending.append(pid)
        if len(pending) >= 200:
            db.mark_relationship_progress(conn, LAYER, pending, commit=True)
            pending.clear()
    if pending:
        db.mark_relationship_progress(conn, LAYER, pending, commit=True)
    conn.close()
    summary = {"papers_processed": n_papers, "edges_by_type": dict(by_type)}
    logger.info("relationships: %s", summary)
    return summary


def main() -> None:
    ap = argparse.ArgumentParser(description="Derive paper<->paper relationship edges (ADR-0004).")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--all", action="store_true", help="Recompute for the whole corpus.")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    s = derive_relations(args.db, reindex_all=args.all)
    print(f"Processed {s['papers_processed']} papers; edges: {json.dumps(s['edges_by_type'])}")


if __name__ == "__main__":
    main()
