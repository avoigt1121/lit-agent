"""
pipeline/ohsu_map.py — OHSU/BCC-interest mapping (ADR-0004, layer 4 — STUB).

Relates corpus papers to OHSU/BCC Center interests in ``ohsu_interest_links``.
This is the contract the future "identify more specific OHSU research targets" /
"infer correlations between OHSU research and the papers" tools (PI to-do
backlog, CLAUDE.md) will read. The TABLE + its accessors are the deliverable;
population is intentionally MINIMAL and fully grounded in v1:

  - ``seed_author`` links — a paper an author of which surname-matches the BCC
    roster (config/seed_authors.yaml). Real, verifiable authorship, not inference.

Richer mappings (lab-topic correlation, embedding similarity to active OHSU
work) are deferred to a later LLM/embedding pass; they slot in as additional
``interest_kind`` rows without a schema change.

OFFLINE only, new-papers-only, resumable via ``relationship_progress`` (layer
``ohsu``). Never runs in the Space.

    python -m pipeline.ohsu_map
    python -m pipeline.ohsu_map --all
"""
from __future__ import annotations

import argparse
import logging
import re
from pathlib import Path

import yaml

from store import db

logger = logging.getLogger("ohsu_map")

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "corpus.sqlite"
SEED_AUTHORS_PATH = ROOT / "config" / "seed_authors.yaml"
LAYER = "ohsu"


def _surname(name: str) -> str:
    """Best-effort surname for a roster entry like 'Rosalie Sears'."""
    toks = re.sub(r"[^A-Za-z\- ]", " ", name or "").split()
    return toks[-1].lower() if toks else ""


def load_roster(path: Path = SEED_AUTHORS_PATH) -> list[dict]:
    """[{name, orcid, surname}] from config/seed_authors.yaml."""
    data = yaml.safe_load(Path(path).read_text()) or {}
    out = []
    for a in data.get("seed_authors") or []:
        name = a.get("name")
        if not name:
            continue
        out.append({"name": name, "orcid": a.get("orcid"), "surname": _surname(name)})
    return out


def _author_surnames(rec: dict) -> set[str]:
    """Surnames appearing in a record's author list (handles 'Last F' and 'First Last')."""
    out: set[str] = set()
    for a in rec.get("authors") or []:
        toks = re.sub(r"[^A-Za-z\- ]", " ", a or "").split()
        if not toks:
            continue
        # EPMC/PubMed: 'Sears RC' -> first token is the surname; preprints:
        # 'Rosalie Sears' -> last token. Add both candidates; match is exact on
        # a roster surname so a stray token rarely collides.
        out.add(toks[0].lower())
        out.add(toks[-1].lower())
    return out


def links_for_paper(rec: dict, roster: list[dict]) -> list[dict]:
    """Seed-author interest links for one paper (surname match)."""
    surnames = _author_surnames(rec)
    out: list[dict] = []
    for member in roster:
        sn = member["surname"]
        if sn and sn in surnames:
            out.append({
                "interest_id": member.get("orcid") or member["name"],
                "interest_kind": "seed_author",
                "score": 1.0,
                "evidence": {"matched_surname": sn, "roster_name": member["name"]},
            })
    return out


def map_interests(db_path: Path = DEFAULT_DB, *, seed_path: Path = SEED_AUTHORS_PATH,
                  reindex_all: bool = False) -> dict:
    """Populate ``ohsu_interest_links`` for new (or all) papers. Returns a summary."""
    roster = load_roster(seed_path)
    conn = db.connect(db_path)
    db.init_schema(conn)
    done = set() if reindex_all else db.relationship_progress_present(conn, LAYER)

    n_papers = n_links = n_linked_papers = 0
    pending: list[str] = []
    for rec in db.iter_papers(conn, include_excluded=False):
        pid = rec["paper_id"]
        if pid in done:
            continue
        links = links_for_paper(rec, roster)
        db.set_ohsu_links(conn, pid, links, commit=False)
        if links:
            n_linked_papers += 1
            n_links += len(links)
        n_papers += 1
        pending.append(pid)
        if len(pending) >= 500:
            db.mark_relationship_progress(conn, LAYER, pending, commit=True)
            pending.clear()
    if pending:
        db.mark_relationship_progress(conn, LAYER, pending, commit=True)
    conn.close()
    summary = {"papers_processed": n_papers, "papers_linked": n_linked_papers,
               "links_written": n_links, "roster_size": len(roster)}
    logger.info("ohsu_map: %s", summary)
    return summary


def main() -> None:
    ap = argparse.ArgumentParser(description="Map papers to OHSU/BCC interests (ADR-0004 stub).")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--all", action="store_true", help="Recompute for the whole corpus.")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    s = map_interests(args.db, reindex_all=args.all)
    print(f"Processed {s['papers_processed']} papers; linked {s['papers_linked']} "
          f"({s['links_written']} links, {s['roster_size']}-member roster).")


if __name__ == "__main__":
    main()
