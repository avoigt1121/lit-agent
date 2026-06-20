"""
scripts/cleanup_corpus.py — retroactively quarantine non-groundable corpus rows.

The Phase A full-corpus census (pipeline/census.py) ingested whatever the saved
Europe PMC query returned. Because that query is unrestricted-field (it matches
title, abstract, full text AND references), it swept in:

  * abstract-less meta records — reply letters, errata/corrections, "Issue
    Information", "Talks", editorials. No groundable content; they pollute the
    digest's new-paper list and the Q&A corpus.

This script SOFT-FLAGS those rows (db.flag_excluded → excluded=1) rather than
deleting them: the rows + their vectors stay in the store, so the change is fully
reversible and vectors.npz need not be rebuilt. The retriever and the digest read
``iter_papers(include_excluded=False)`` and so never surface them.

Scope is deliberately CONSERVATIVE (zero-false-positive): abstract-less rows plus
a short list of unambiguous meta-title patterns. Genuinely off-topic papers that
*do* carry an abstract (e.g. a breast-cancer paper that merely cites PDAC work)
are left to the tightened query (config/sources.yaml) going forward and to the
Q&A retrieval floor (RETRIEVAL_MIN_SCORE) — an embedding cull does not separate
them cleanly from legitimate mechanism reviews, so we don't attempt it here.

Usage:
    python -m scripts.cleanup_corpus            # apply
    python -m scripts.cleanup_corpus --dry-run  # report only, no writes
    python -m scripts.cleanup_corpus --db /tmp/t.sqlite
"""
from __future__ import annotations

import argparse
from pathlib import Path

from store import db

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "corpus.sqlite"

# Unambiguous meta / non-article title patterns (case-insensitive LIKE).
META_TITLE_PATTERNS = [
    "Reply to %", "Reply%", "In Reply%", "Authors' Reply%", "Author's Reply%",
    "Response to %", "Response letter%", "Letter to %", "Re: %",
    "Comment on %", "Comment to %", "Correction%", "Corrigendum%", "Erratum%",
    "Retraction%", "Issue Information%", "Editorial%",
]


def select_abstract_less(conn) -> set[str]:
    rows = conn.execute(
        "SELECT paper_id FROM papers "
        "WHERE excluded=0 AND (abstract IS NULL OR TRIM(abstract)='')")
    return {r[0] for r in rows}


def select_meta_titles(conn) -> set[str]:
    clause = " OR ".join(["title LIKE ?"] * len(META_TITLE_PATTERNS))
    rows = conn.execute(
        f"SELECT paper_id FROM papers WHERE excluded=0 AND ({clause})",
        META_TITLE_PATTERNS)
    return {r[0] for r in rows}


def cleanup(db_path: Path = DEFAULT_DB, *, dry_run: bool = False) -> dict:
    conn = db.connect(db_path)
    db.init_schema(conn)  # runs the v5 excluded-columns migration if needed

    total_before = db.count_papers(conn)
    active_before = db.count_papers(conn, include_excluded=False)

    abstract_less = select_abstract_less(conn)
    meta_titles = select_meta_titles(conn)
    # An abstract-less row keeps the 'abstract_less' reason; a row that has an
    # abstract but a meta title is flagged 'meta_title'.
    meta_only = meta_titles - abstract_less

    if not dry_run:
        db.flag_excluded(conn, abstract_less, "abstract_less")
        db.flag_excluded(conn, meta_only, "meta_title")

    result = {
        "total": total_before,
        "active_before": active_before,
        "flagged_abstract_less": len(abstract_less),
        "flagged_meta_title": len(meta_only),
        "flagged_total": len(abstract_less | meta_titles),
        "active_after": db.count_papers(conn, include_excluded=False),
        "breakdown": db.excluded_breakdown(conn),
        "dry_run": dry_run,
    }
    conn.close()
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--dry-run", action="store_true",
                    help="Report what would be flagged without writing.")
    args = ap.parse_args()

    r = cleanup(args.db, dry_run=args.dry_run)
    tag = "DRY RUN — no writes" if r["dry_run"] else "APPLIED"
    print(f"[{tag}]  corpus={r['total']:,}")
    print(f"  abstract-less : {r['flagged_abstract_less']:,}")
    print(f"  meta-title    : {r['flagged_meta_title']:,}")
    print(f"  flagged total : {r['flagged_total']:,}")
    print(f"  active corpus : {r['active_before']:,} -> {r['active_after']:,}")
    if r["breakdown"]:
        print(f"  excluded_reason breakdown: {r['breakdown']}")


if __name__ == "__main__":
    main()
