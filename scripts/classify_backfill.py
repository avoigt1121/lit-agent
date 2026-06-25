"""
scripts/classify_backfill.py — ONE-OFF: LLM-classify the historical backlog.

WHY THIS EXISTS
---------------
ADR-0001 made the weekly path (pipeline.run_weekly.build_corpus) classify ONLY the
papers that are NEW each run — re-classifying the whole 46.5k corpus every week was
the multi-hour timeout. Correct for the weekly digest (new papers get focus_areas),
but it leaves a backlog: the Phase A census ingested + classified the whole corpus
with ``classify_and_score(client=None)`` — the EMBEDDING-ONLY fallback, which keeps a
paper's single best area only if it clears the absolute floor (0.68). Everything
below the floor was persisted with ``focus_areas=[]`` and ``relevance_score=0.0``.

So the backlog is not "never processed" — it is "processed without the LLM". This
script re-runs JUST those papers through ``classify_and_score`` WITH the cheap LLM
client (top-k embedding candidates + an LLM confirm — the real precision lever, see
pipeline/score.py), reusing each paper's EXISTING vector from vectors.npz via
``VectorIndex.get()`` (NO re-embedding — the index already holds all 46,570). Result:
the post-v1 per-focus-area Space tabs and any analytics-by-area see a complete corpus,
not just the weekly trickle.

RESUMABLE + IDEMPOTENT
----------------------
Because the LLM legitimately returns ZERO areas for some papers, a processed paper can
look identical to an un-processed one (``focus_areas=[]``, ``relevance_score=0.0``) —
neither column can mark progress. So this script keeps its OWN tiny progress table,
``classify_backfill_progress`` (created here, NOT in db.init_schema, so the shared
schema / SCHEMA_VERSION is untouched). It rides along in corpus.sqlite, so a killed
run — or an HF Job that hits its timeout — resumes by re-querying the still-unclassified
set MINUS the already-processed ids. Re-running is always safe.

NEVER INSIDE THE WEEKLY CRON
----------------------------
This is ~13k sequential LLM calls (~2-3h). It is a standalone module — pipeline.run_weekly
does NOT import or call it — and its HF Job mode (scripts/hf_job.sh backfill) is a ONE-OFF
``hf jobs run``, never ``hf jobs scheduled run``. Keep it that way: the weekly schedule
must only ever run pipeline.run_weekly.

Usage:
    python -m scripts.classify_backfill --dry-run            # report the plan, no LLM, no writes
    python -m scripts.classify_backfill --limit 5 --no-sync  # smoke test: 5 papers, local only
    python -m scripts.classify_backfill                      # full run: pull -> classify -> push
    python -m scripts.classify_backfill --no-sync            # full run, local corpus only (no hub)

Model + provider are inherited from the weekly config: CLASSIFY_MODEL (default
claude-haiku-4-5) via LLM_PROVIDER (anthropic default; hf routes to HF Inference
Providers — same switch as ADR-0002). With no usable LLM credential this refuses to
run (embedding-only would just reproduce the census's empty result); pass --dry-run
to inspect without a key.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

from pipeline.llm import cheap_client
from pipeline.run_weekly import (DEFAULT_DB, DEFAULT_INDEX, PROFILE_PATH,
                                 pull_from_hub, sync_to_hub)
from pipeline.score import Embedder, classify_and_score, load_interest_profile
from store import db
from store.vectors import VectorIndex

logger = logging.getLogger("classify_backfill")

# Non-excluded papers with no focus area assigned yet — the exact predicate from the
# task brief. JSON-encoded empty list / null / '' all mean "no areas" (db stores
# focus_areas as json.dumps(...)); excluded rows are out of scope (abstract-less).
_UNCLASSIFIED_PRED = ("excluded=0 AND "
                      "(focus_areas IS NULL OR focus_areas IN ('', '[]', 'null'))")

# Marker for a paper we had to skip because its vector is missing from the index
# (should never happen — vectors.npz holds all ids — but we record it so a resume
# does not retry it forever). Distinct from n_areas>=0 = genuinely classified.
_NO_VECTOR = -1


# --------------------------------------------------------------------------- #
# Progress table (script-local; rides in corpus.sqlite so HF Job restarts resume)
# --------------------------------------------------------------------------- #

def _ensure_progress(conn) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS classify_backfill_progress ("
        "  paper_id      TEXT PRIMARY KEY,"
        "  classified_at TEXT,"     # ISO timestamp the paper was processed
        "  n_areas       INTEGER"   # areas assigned (0 = LLM found none; -1 = no vector)
        ")")
    conn.commit()


def _done_ids(conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT paper_id FROM classify_backfill_progress")}


def _mark_done(conn, paper_id: str, n_areas: int) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO classify_backfill_progress"
        "(paper_id, classified_at, n_areas) VALUES (?,?,?)",
        (paper_id, datetime.now().isoformat(timespec="seconds"), n_areas))
    conn.commit()


# --------------------------------------------------------------------------- #
# Selection
# --------------------------------------------------------------------------- #

def count_unclassified(conn) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM papers WHERE {_UNCLASSIFIED_PRED}").fetchone()[0]


def select_unclassified_ids(conn) -> list[str]:
    # ORDER BY for a deterministic, resume-stable processing order.
    return [r[0] for r in conn.execute(
        f"SELECT paper_id FROM papers WHERE {_UNCLASSIFIED_PRED} ORDER BY paper_id")]


def _fetch_records(conn, ids: list[str]) -> list[dict]:
    """Full normalized records for a batch of ids (chunked under SQLite's var limit)."""
    out: list[dict] = []
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        q = f"SELECT * FROM papers WHERE paper_id IN ({','.join('?' * len(chunk))})"
        out.extend(db.record_to_dict(r) for r in conn.execute(q, chunk))
    return out


# --------------------------------------------------------------------------- #
# Durable push (WAL-safe)
# --------------------------------------------------------------------------- #

def _checkpoint(conn) -> None:
    """Fold the WAL into corpus.sqlite so a push of the file is complete.

    db.connect() opens in WAL mode, so committed rows live in corpus.sqlite-wal
    until a checkpoint. run_weekly closes the connection (which checkpoints) before
    syncing; this script pushes MID-RUN with the connection still open, so it must
    checkpoint explicitly or the pushed file would be missing recent classifications.
    """
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception as exc:  # noqa: BLE001 — a failed checkpoint must not lose the run
        logger.warning("wal_checkpoint failed (%s); push may lag the WAL.", exc)


def _fmt_eta(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}m" if h else (f"{m}m{s:02d}s" if m else f"{s}s")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def run(args) -> int:
    # Load a local .env (CORPUS_HF_DATASET etc.) exactly as run_weekly does.
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    except Exception:
        pass

    # On a fresh HF Job container the repo has no corpus.sqlite (it is gitignored and
    # lives in the Dataset hub) — pull it first. --no-sync == purely local (no pull, no push).
    if not args.no_sync:
        if pull_from_hub(args.db, args.index):
            logger.info("Pulled corpus + index from the HF Dataset (hub is source of truth).")
        else:
            logger.warning("No hub pull (creds/files absent) — using the local corpus as-is.")

    conn = db.connect(args.db)
    db.init_schema(conn)        # ensure excluded columns etc. (idempotent)
    _ensure_progress(conn)

    total_unclassified = count_unclassified(conn)
    done = _done_ids(conn)
    todo = [pid for pid in select_unclassified_ids(conn) if pid not in done]
    if args.limit:
        todo = todo[:args.limit]

    print(f"Backlog (non-excluded, empty focus_areas): {total_unclassified:,}")
    print(f"Already processed this backfill:           {len(done):,}")
    print(f"To classify {'(--limit) ' if args.limit else ''}this run:          {len(todo):,}")

    if args.dry_run:
        print("\n[DRY RUN] no LLM calls, no writes. "
              "Re-run without --dry-run to classify.")
        conn.close()
        return 0
    if not todo:
        print("\nNothing to do — backlog already classified. ✔")
        conn.close()
        return 0

    client = cheap_client()
    if client is None:
        conn.close()
        print("\nERROR: no usable LLM client. The embedding-only fallback would just "
              "reproduce the census's empty result, so this refuses to run.\n"
              "  Set ANTHROPIC_API_KEY (anthropic), or LLM_PROVIDER=hf + HF_TOKEN.\n"
              "  (Pass --dry-run to inspect the plan without a key.)", file=sys.stderr)
        return 1

    profile = load_interest_profile(PROFILE_PATH)
    embedder = Embedder()                      # ONE instance: loads the ONNX model once
    index = VectorIndex.load(args.index)       # vectors only read (get()), never written

    t0 = time.time()
    processed = 0
    since_push = 0
    assigned_areas: Counter = Counter()        # area_id -> papers assigned
    n_with_area = n_no_area = n_no_vector = 0

    for start in range(0, len(todo), args.batch_size):
        batch_ids = todo[start:start + args.batch_size]
        recs = _fetch_records(conn, batch_ids)

        # Reuse each paper's stored vector (no re-embedding). embedding_id == paper_id.
        vecs: dict = {}
        missing: list[dict] = []
        for r in recs:
            v = index.get(r.get("embedding_id") or r["paper_id"])
            if v is None:
                missing.append(r)
            else:
                vecs[r["paper_id"]] = v
        recs_ok = [r for r in recs if r["paper_id"] in vecs]

        if recs_ok:
            # classify_and_score sets focus_areas + relevance_score on each rec and
            # returns {paper_id: {area_id: score}} for topic_tags. The embedder here
            # only embeds the ~handful of area descriptors, not the papers.
            tags = classify_and_score(recs_ok, vecs, profile, embedder, client=client)
            db.upsert_papers(conn, recs_ok)               # persist focus_areas + relevance_score
            for r in recs_ok:
                pid = r["paper_id"]
                db.set_topic_tags(conn, pid, tags.get(pid, {}))
                areas = r.get("focus_areas") or []
                _mark_done(conn, pid, len(areas))
                if areas:
                    n_with_area += 1
                    assigned_areas.update(areas)
                else:
                    n_no_area += 1
        for r in missing:                                  # vector absent — record + skip
            _mark_done(conn, r["paper_id"], _NO_VECTOR)
            n_no_vector += 1
            logger.warning("No vector for %s — skipped.", r["paper_id"])

        processed += len(recs)
        since_push += len(recs)

        rate = processed / max(time.time() - t0, 1e-9)
        eta = _fmt_eta((len(todo) - processed) / rate) if rate else "?"
        print(f"  [{processed:>6,}/{len(todo):,}] {processed / len(todo) * 100:5.1f}%  "
              f"{rate:4.1f} papers/s  ETA {eta}  "
              f"(+area {n_with_area:,} / none {n_no_area:,})")

        # Mid-run durable push so an HF Job timeout still banks progress to the hub.
        if not args.no_sync and args.push_every and since_push >= args.push_every:
            _checkpoint(conn)
            sync_to_hub(args.db, args.index)
            print(f"  ↑ pushed corpus to hub at {processed:,} processed.")
            since_push = 0

    # Final durable push: checkpoint, then close so the file is fully flushed.
    _checkpoint(conn)
    remaining = count_unclassified(conn)
    conn.close()

    if not args.no_sync:
        sync_to_hub(args.db, args.index)

    elapsed = _fmt_eta(time.time() - t0)
    print(f"\nDone in {elapsed}.  processed={processed:,}  "
          f"assigned≥1 area={n_with_area:,}  no area={n_no_area:,}  "
          f"no vector={n_no_vector:,}")
    if assigned_areas:
        print("Focus-area assignments this run:")
        for aid, n in assigned_areas.most_common():
            print(f"  {aid:28s} {n:,}")
    # After a full run the residual is exactly the no-area papers — all marked processed,
    # so the next run's todo is empty even though they keep an empty focus_areas.
    print(f"\nStill empty focus_areas (LLM found no matching area): {remaining:,} "
          f"— all marked processed; a re-run will skip them.")
    if not args.no_sync:
        print("Corpus pushed to the HF Dataset.")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="ONE-OFF: LLM-classify the unclassified backlog.")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--index", type=Path, default=DEFAULT_INDEX)
    ap.add_argument("--dry-run", action="store_true",
                    help="Report the plan (counts) without LLM calls or writes.")
    ap.add_argument("--limit", type=int, default=None,
                    help="Classify at most N papers this run (smoke test / chunking).")
    ap.add_argument("--batch-size", type=int, default=50,
                    help="Papers per classify_and_score call (amortizes area embedding).")
    ap.add_argument("--push-every", type=int, default=2000,
                    help="Push to the hub every N processed papers (0 = only at the end). "
                         "Ignored with --no-sync.")
    ap.add_argument("--no-sync", action="store_true",
                    help="Local only: no hub pull at start, no push (resumes off local sqlite).")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    raise SystemExit(run(args))


if __name__ == "__main__":
    main()
