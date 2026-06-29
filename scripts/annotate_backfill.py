"""
scripts/annotate_backfill.py — ONE-OFF: enrich the WHOLE corpus with EPMC
text-mined annotations (ADR-0004, layer-1 enrichment backfill).

WHY THIS EXISTS
---------------
``pipeline/annotate.py`` enriches only the papers NEW each weekly run (resumable via
``relationship_progress`` layer ``annotations``, cap-limited). The ~44.5k papers the
census ingested before the annotation layer existed have no ``epmc_annotation`` rows.
This one-off drains that backlog: it pulls the durable corpus, runs the SAME
``enrich_annotations`` populator with NO per-run cap, and pushes back — mirroring
``scripts/classify_backfill.py`` but for the annotation layer.

It is just a HUB-SYNC + LOOP wrapper around ``pipeline.annotate.enrich_annotations``;
all the mapping / merge-aware write / resumability lives there. No LLM key needed
(the Annotations API is keyless); only ``HF_TOKEN`` + ``CORPUS_HF_DATASET`` to pull/push.

RESUMABLE + TIMEOUT-SAFE
------------------------
``enrich_annotations`` marks each processed paper in ``relationship_progress`` and
preserves any existing ``literal_scan`` rows (merge-aware writer). We call it in
bounded chunks (``--push-every`` papers) and push the corpus to the hub after each,
so an HF Job that hits its timeout still banks progress; a re-run skips processed
papers. Re-running after completion is a no-op.

NEVER INSIDE THE WEEKLY CRON
----------------------------
Corpus-scale (~44.5k papers / ~5.5k sanctioned EPMC batch calls). Standalone module —
``pipeline.run_weekly`` does NOT import it — and its HF Job mode
(``scripts/hf_job.sh annotate``) is a ONE-OFF ``hf jobs run``, never ``scheduled run``.

Usage:
    python -m scripts.annotate_backfill --dry-run            # counts only, no fetch/writes
    python -m scripts.annotate_backfill --limit 50 --no-sync # smoke test: 50 papers, local only
    python -m scripts.annotate_backfill                      # full run: pull -> enrich -> push
"""
from __future__ import annotations

import argparse
import logging
import time
from pathlib import Path

from pipeline.annotate import enrich_annotations, LAYER
from pipeline.run_weekly import DEFAULT_DB, DEFAULT_INDEX, PROFILE_PATH, pull_from_hub, sync_to_hub
from store import db

logger = logging.getLogger("annotate_backfill")


def _checkpoint(db_path: Path) -> None:
    """Fold the WAL into corpus.sqlite so a push of the file is complete."""
    try:
        conn = db.connect(db_path)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()
    except Exception as exc:  # noqa: BLE001 — a failed checkpoint must not lose the run
        logger.warning("wal_checkpoint failed (%s); push may lag the WAL.", exc)


def _addressable_remaining(db_path: Path) -> int:
    """Count non-excluded, EPMC-addressable papers not yet annotation-processed."""
    from pipeline.annotate import _article_id
    conn = db.connect(db_path)
    db.init_schema(conn)
    done = db.relationship_progress_present(conn, LAYER)
    n = sum(1 for rec in db.iter_papers(conn, include_excluded=False)
            if rec["paper_id"] not in done and _article_id(rec))
    conn.close()
    return n


def run(args) -> int:
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    except Exception:
        pass

    # Fresh HF Job container has no corpus.sqlite (gitignored; lives in the Dataset
    # hub) — pull it first. --no-sync == purely local (no pull, no push).
    if not args.no_sync:
        if pull_from_hub(args.db, args.index):
            logger.info("Pulled corpus + index from the HF Dataset (hub is source of truth).")
        else:
            logger.warning("No hub pull (creds/files absent) — using the local corpus as-is.")

    remaining = _addressable_remaining(args.db)
    print(f"EPMC-addressable papers not yet annotation-enriched: {remaining:,}")
    if args.dry_run:
        print("\n[DRY RUN] no EPMC calls, no writes. Re-run without --dry-run to enrich.")
        return 0
    if remaining == 0:
        print("\nNothing to do — annotation backlog already enriched. ✔")
        return 0

    t0 = time.time()
    total_processed = total_mentions = total_with = 0
    while True:
        cap = args.limit if args.limit else args.push_every
        s = enrich_annotations(args.db, profile_path=PROFILE_PATH, cap=cap)
        n = s["papers_processed"]
        total_processed += n
        total_mentions += s["annotation_mentions_written"]
        total_with += s["papers_with_annotations"]
        rate = total_processed / max(time.time() - t0, 1e-9)
        print(f"  [{total_processed:>6,}] processed  {rate:4.1f} papers/s  "
              f"(+annotations {total_with:,}, mentions {total_mentions:,})")
        if not args.no_sync and n:
            _checkpoint(args.db)
            sync_to_hub(args.db, args.index)
            print(f"  ↑ pushed corpus to hub at {total_processed:,} processed.")
        # Stop when a chunk drains nothing (backlog empty), or on --limit (one chunk).
        if n == 0 or args.limit:
            break

    if not args.no_sync:
        _checkpoint(args.db)
        sync_to_hub(args.db, args.index)
    elapsed = int(time.time() - t0)
    print(f"\nDone in {elapsed // 60}m{elapsed % 60:02d}s.  processed={total_processed:,}  "
          f"papers_with_annotations={total_with:,}  mentions_written={total_mentions:,}")
    if not args.no_sync:
        print("Corpus pushed to the HF Dataset.")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="ONE-OFF: EPMC-annotation-enrich the whole corpus.")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--index", type=Path, default=DEFAULT_INDEX)
    ap.add_argument("--dry-run", action="store_true", help="Report counts; no EPMC calls or writes.")
    ap.add_argument("--limit", type=int, default=None,
                    help="Enrich at most N papers this run (smoke test / chunking).")
    ap.add_argument("--push-every", type=int, default=2000,
                    help="Enrich + push in chunks of N papers (HF-Job-timeout-safe). Ignored with --no-sync chunk size still applies.")
    ap.add_argument("--no-sync", action="store_true",
                    help="Local only: no hub pull at start, no push (resumes off local sqlite).")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    raise SystemExit(run(args))


if __name__ == "__main__":
    main()
