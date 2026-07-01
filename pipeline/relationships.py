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


def _paper_genes(conn, paper_id: str, method: str | None = "literal_scan") -> set[str]:
    """Curated gene mentions for a paper. Defaults to ``literal_scan`` (the config
    lexicon) — NOT the broad EPMC annotation set, whose generic tags (antibodies,
    cytokine, CD8) make shared-gene edges noisy and the candidate join quadratic.
    Pass ``method=None`` to span all sources."""
    q = "SELECT DISTINCT entity FROM mentions WHERE paper_id=? AND entity_type='gene'"
    args: list = [paper_id]
    if method:
        q += " AND method=?"
        args.append(method)
    return {r[0] for r in conn.execute(q, args)}


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


def _gene_candidates(conn, genes: set[str], self_id: str, cap: int,
                     method: str | None = "literal_scan", max_entity_papers: int = 5000) -> Counter:
    """Counter of {other_paper_id: # shared genes} via the mention index.

    Restricted to ``method`` (curated literal_scan by default). ``max_entity_papers``
    skips any gene that appears in more papers than the cap — a gene that ubiquitous
    is uninformative as a shared signal and dominates the join cost. Belt-and-
    suspenders on top of the literal_scan restriction (which already drops the
    generic EPMC tags)."""
    tally: Counter = Counter()
    for g in genes:
        pids = db.papers_mentioning(conn, g, entity_type="gene", method=method)
        if len(pids) > max_entity_papers:
            continue  # too common to be an informative shared-gene signal
        for pid in pids:
            if pid != self_id:
                tally[pid] += 1
    # Cap to the strongest neighbors to bound write volume / combinatorics.
    return Counter(dict(tally.most_common(cap * 4)))  # over-fetch; filtered by threshold next


def derive_for_paper(conn, paper_id: str, cfg: dict) -> list[dict]:
    """Compute all derived edges for one paper. Returns relation dicts."""
    min_shared = int(cfg.get("min_shared_genes", 2))
    max_neighbors = int(cfg.get("max_neighbors", 25))
    want_focus = bool(cfg.get("shared_focus", True))
    # Which mention source feeds shared-gene edges. literal_scan = the curated
    # lexicon (high-signal); the broad EPMC annotation set is too noisy/large.
    gene_method = cfg.get("gene_method", "literal_scan")
    max_entity_papers = int(cfg.get("max_entity_papers", 5000))

    genes = _paper_genes(conn, paper_id, method=gene_method)
    my_focus = _focus_areas(conn, paper_id)
    cands = (_gene_candidates(conn, genes, paper_id, max_neighbors,
                              method=gene_method, max_entity_papers=max_entity_papers)
             if genes else Counter())
    cite_neighbors = _citation_neighbors(conn, paper_id)

    rels: list[dict] = []

    # shared_genes (+ shared_focus on the same bounded neighbor set)
    strong = [pid for pid, n in cands.most_common(max_neighbors) if n >= min_shared]
    for pid in strong:
        shared = sorted(genes & _paper_genes(conn, pid, method=gene_method))
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


def _load_indices(conn, gene_method: str | None) -> dict:
    """Precompute the whole-corpus lookups ONCE, in memory, so per-paper edge
    derivation is pure set math — no per-candidate SQL. This is the difference
    between a ~2-hour and a ~1-minute ``--all`` run over 44k papers (the naive
    per-paper queries issue millions of tiny statements). Structures are tiny:
    the curated literal gene set is ~dozens of entities."""
    gq = "SELECT paper_id, entity FROM mentions WHERE entity_type='gene'"
    gargs: list = []
    if gene_method:
        gq += " AND method=?"
        gargs.append(gene_method)
    paper_genes: dict[str, set] = {}
    gene_papers: dict[str, set] = {}
    for pid, ent in conn.execute(gq, gargs):
        paper_genes.setdefault(pid, set()).add(ent)
        gene_papers.setdefault(ent, set()).add(pid)

    paper_focus: dict[str, set] = {}
    for pid, area in conn.execute("SELECT paper_id, focus_area FROM topic_tags"):
        paper_focus.setdefault(pid, set()).add(area)

    cite_adj: dict[str, set] = {}
    for a, b in conn.execute(
        "SELECT citing_paper_id, cited_paper_id FROM citation_edges "
        "WHERE citing_paper_id IS NOT NULL AND cited_paper_id IS NOT NULL"):
        if a != b:
            cite_adj.setdefault(a, set()).add(b)
            cite_adj.setdefault(b, set()).add(a)
    return {"paper_genes": paper_genes, "gene_papers": gene_papers,
            "paper_focus": paper_focus, "cite_adj": cite_adj}


def _edges_for_paper(pid: str, idx: dict, cfg: dict) -> list[dict]:
    """Derived edges for one paper from the in-memory indices (see _load_indices).
    Semantically identical to derive_for_paper, without the SQL round-trips."""
    min_shared = int(cfg.get("min_shared_genes", 2))
    max_neighbors = int(cfg.get("max_neighbors", 25))
    want_focus = bool(cfg.get("shared_focus", True))
    max_entity_papers = int(cfg.get("max_entity_papers", 5000))

    paper_genes = idx["paper_genes"]
    gene_papers = idx["gene_papers"]
    genes = paper_genes.get(pid, set())
    rels: list[dict] = []

    if genes:
        tally: Counter = Counter()
        for g in genes:
            pset = gene_papers.get(g, ())
            if len(pset) > max_entity_papers:
                continue  # too common to be an informative shared-gene signal
            for opid in pset:
                if opid != pid:
                    tally[opid] += 1
        my_focus = idx["paper_focus"].get(pid, set())
        for opid, _n in tally.most_common(max_neighbors):
            shared = sorted(genes & paper_genes.get(opid, set()))
            if len(shared) < min_shared:
                continue
            rels.append({"src_paper_id": pid, "dst_paper_id": opid,
                         "rel_type": "shared_genes", "weight": float(len(shared)),
                         "evidence": {"genes": shared}})
            if want_focus:
                shared_areas = sorted(my_focus & idx["paper_focus"].get(opid, set()))
                if shared_areas:
                    rels.append({"src_paper_id": pid, "dst_paper_id": opid,
                                 "rel_type": "shared_focus", "weight": float(len(shared_areas)),
                                 "evidence": {"focus_areas": shared_areas}})

    for opid in idx["cite_adj"].get(pid, ()):  # undirected related-via-citation
        rels.append({"src_paper_id": pid, "dst_paper_id": opid,
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
    idx = _load_indices(conn, cfg.get("gene_method", "literal_scan"))

    n_papers = 0
    by_type: Counter = Counter()
    pending: list[str] = []
    for rec in db.iter_papers(conn, include_excluded=False):
        pid = rec["paper_id"]
        if pid in done:
            continue
        rels = _edges_for_paper(pid, idx, cfg)
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
