"""
pipeline/census.py — full-corpus record backfill (CAPABILITIES.md §1, Phase A).

Populates the ``papers`` table (metadata + abstract + embedding) for *every* PDAC
paper over the last N years — the "census" tier (§1.1). This is the keystone that
unblocks novelty + the macro→micro bridge (Phase E) and the granularity acceptance
tests (Phase C): it gives the analytics a complete body of records that share IDs
with the curated weekly corpus, instead of months of weekly accumulation.

**No full text, no LLM** (§1.3). It reuses the existing weekly path end to end —
``harvest_europepmc`` → ``normalize_records`` → ``embed_corpus`` →
``classify_and_score(client=None)`` → ``db.upsert_papers`` — but as a distinct
orchestration entry, not a flag on ``harvest_all``, because the census wants:

  * **Europe PMC only** — EPMC is a superset of PubMed + preprints (§1.4), so the
    other two sources would only add dedup work.
  * **Explicit, historical date ranges** walked as calendar-month windows, each well
    under the EPMC ``max_pages`` cap (~880 PDAC papers/month ≈ 1 page; cap is 10).
  * **Per-window resumability** (mirrors ``backfill.coverage_periods_present``): a
    completed month is recorded in ``census_progress`` and skipped on re-run.

Two gotchas the naive reuse gets wrong (both handled here):

  1. **Historical ``first_seen_date``.** ``harvest.blank_record()`` stamps
     ``date.today()`` — correct for the weekly path, WRONG for a backfill (it would
     make all ~53k papers look "new today" and render novelty meaningless). We
     override it per record from ``published_date`` (fallback: the window start)
     *before* normalize, so a merge keeps the earliest; ``db.backdate_first_seen``
     then corrects any rows a prior weekly run already stamped with today().
  2. **Vector append-after-load.** ``VectorIndex.load()`` seeds ``_rows`` from the
     loaded matrix so add()-after-load extends rather than replaces the index; we
     also skip ids already present (``in index``) so reprocessing the current
     partial month on resume never duplicates vectors.

Operational note: the census writes into the SAME ``data/corpus.sqlite`` +
``data/vectors.npz`` as the weekly pipeline (the curated corpus is just the scored
subset of the same table; ``upsert_papers`` preserves ``first_seen_date`` so this is
safe). **Back up ``corpus.sqlite``/``vectors.npz`` before the first full run.**

Usage:
    python -m pipeline.census --years 5 -v                       # full backfill
    python -m pipeline.census --years 5 --force                  # recompute all windows
    python -m pipeline.census --from 2026-05-01 --to 2026-05-31 \\
        --db /tmp/t.sqlite --index /tmp/t.npz -v                 # small validation run
"""
from __future__ import annotations

import argparse
import logging
import time
from datetime import date, timedelta
from pathlib import Path

from pipeline.harvest import (POLITE_PAUSE, REQUEST_TIMEOUT, _session,
                              harvest_europepmc, load_config)
from pipeline.normalize import normalize_records
from pipeline.score import (Embedder, classify_and_score, embed_corpus,
                            load_interest_profile)
from store import db
from store.vectors import VectorIndex

logger = logging.getLogger("census")

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "corpus.sqlite"
DEFAULT_INDEX = ROOT / "data" / "vectors.npz"
PROFILE_PATH = ROOT / "config" / "interest_profile.yaml"

SOURCE = "europepmc"


# ---------------------------------------------------------------------------
# Date-range windows (calendar months — stable, cap-safe keys)
# ---------------------------------------------------------------------------

def _years_ago_month(today: date, years: int) -> date:
    """First of the month `years` years before `today` (day pinned to 1)."""
    return date(today.year - years, today.month, 1)


def month_windows(start: date, end: date) -> list[tuple[date, date, bool]]:
    """Calendar-month windows (window_start, window_end, complete) covering [start, end].

    ``window_start`` is always the 1st of the month so the resumability key is
    stable across runs regardless of which day "today" is. The final (current)
    month is marked ``complete=False`` — it is processed but NOT recorded as done,
    so a re-run reprocesses it and the weekly pipeline keeps it fresh thereafter.
    """
    out: list[tuple[date, date, bool]] = []
    cur = date(start.year, start.month, 1)
    while cur <= end:
        nxt = date(cur.year + 1, 1, 1) if cur.month == 12 else date(cur.year, cur.month + 1, 1)
        month_end = nxt - timedelta(days=1)
        out.append((cur, min(month_end, end), month_end <= end))
        cur = nxt
    return out


# ---------------------------------------------------------------------------
# Harvest a window completely (subdivide if the page cap truncated it)
# ---------------------------------------------------------------------------

def _window_hitcount(session, base_url: str, query: str) -> int:
    r = session.get(base_url, params={"query": query, "format": "json", "pageSize": 1},
                    timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return int(r.json().get("hitCount", 0))


def _harvest_window(ep: dict, base_url: str, pdac_q: str, date_field: str,
                    ds: date, de: date, session) -> list[dict]:
    """All EPMC records with FIRST_PDATE in [ds, de], subdividing on a cap hit.

    ``harvest_europepmc`` paginates the whole result set via cursorMark, so the only
    way it returns fewer than the hitCount is hitting ``max_pages``. We compare the
    two and, if short by more than live-index drift, split the window in half and
    recurse. Monthly windows never trip this (≈1 page); it is correctness insurance
    against a busier-than-expected window or a future cap change.
    """
    recs = harvest_europepmc(ep, ds.isoformat(), de.isoformat(), session)
    expected = _window_hitcount(
        session, base_url, f"({pdac_q}) AND {date_field}:[{ds.isoformat()} TO {de.isoformat()}]")
    tol = max(5, int(expected * 0.01))  # tolerate index drift between the two calls
    if expected - len(recs) > tol and ds < de:
        mid = ds + (de - ds) // 2
        logger.warning("window %s..%s hit the page cap (%d of %d) — subdividing at %s",
                       ds, de, len(recs), expected, mid)
        time.sleep(POLITE_PAUSE)
        left = _harvest_window(ep, base_url, pdac_q, date_field, ds, mid, session)
        time.sleep(POLITE_PAUSE)
        right = _harvest_window(ep, base_url, pdac_q, date_field, mid + timedelta(days=1), de, session)
        return left + right
    if expected - len(recs) > tol:
        logger.warning("window %s..%s truncated and indivisible: %d of %d records",
                       ds, de, len(recs), expected)
    else:
        logger.info("window %s..%s: %d/%d records", ds, de, len(recs), expected)
    return recs


# ---------------------------------------------------------------------------
# Census
# ---------------------------------------------------------------------------

def census(years: int = 5, *, db_path: Path = DEFAULT_DB, index_path: Path = DEFAULT_INDEX,
           force: bool = False, start_override: date | None = None,
           end_override: date | None = None, embedder: Embedder | None = None) -> dict:
    """Backfill ``papers`` + vectors for every PDAC paper over the window. Resumable.

    Per window: harvest → stamp historical first_seen → dedup → embed → classify
    (no LLM) → persist vectors → persist papers/tags → mark done. Vectors are saved
    BEFORE the done-marker so a crash between them can never leave a window recorded
    complete with its vectors missing (the reverse is self-healing: the window simply
    reprocesses and ``upsert_papers`` is idempotent).
    """
    cfg = load_config()
    ep = cfg["europepmc"]
    base_url = ep["base_url"]
    pdac_q = " ".join(ep["query"].split())
    date_field = ep.get("date_field", "FIRST_PDATE")
    session = _session(cfg.get("contact_email", ""), cfg.get("tool_name", "lit-agent"))
    profile = load_interest_profile(PROFILE_PATH)
    embedder = embedder or Embedder()

    today = end_override or date.today()
    start = start_override or _years_ago_month(today, years)
    windows = month_windows(start, today)

    conn = db.connect(db_path)
    db.init_schema(conn)
    present = set() if force else db.census_periods_present(conn, SOURCE)
    todo = [w for w in windows if force or w[0].isoformat() not in present]

    # One persistent index for the whole run: loaded once, extended + saved per
    # window. VectorIndex.load() seeds _rows so add() extends rather than replaces.
    index = VectorIndex.load(index_path) if Path(index_path).exists() else VectorIndex()

    start_total = db.count_papers(conn)
    logger.info("Census %s..%s: %d month windows (%d to do, %d complete) | corpus=%d vectors=%d",
                start.isoformat(), today.isoformat(), len(windows), len(todo),
                len(windows) - len(todo), start_total, len(index))

    n_new_total = 0
    for i, (ws, we, complete) in enumerate(todo, 1):
        raw = _harvest_window(ep, base_url, pdac_q, date_field, ws, we, session)
        if not raw:
            if complete:
                db.mark_census_period(conn, ws.isoformat(), we.isoformat(), SOURCE, 0, 0)
            logger.info("  [%d/%d] %s: 0 records", i, len(todo), ws.isoformat())
            continue

        # GOTCHA 1 — historical first_seen_date (NOT today()). Set BEFORE normalize
        # so a merge keeps the earliest sighting (normalize._merge_group takes min).
        for r in raw:
            r["first_seen_date"] = r.get("published_date") or ws.isoformat()

        deduped = normalize_records(raw)["records"]
        for r in deduped:
            r["embedding_id"] = r["paper_id"]  # 1:1 for abstract embeddings

        vectors = embed_corpus(deduped, embedder)
        # Key-free area tagging (embedding similarity only) — ~0 added cost (§1.3).
        tags = classify_and_score(deduped, vectors, profile, embedder, client=None)

        # 1) Vectors durable first. Skip ids already indexed so the resumable
        #    current-month reprocess (and the pre-existing curated rows) don't dup.
        for r in deduped:
            eid = r["embedding_id"]
            if eid not in index:
                index.add(eid, vectors[eid])
        index.save(index_path)

        # 2) Papers + tags + first_seen backdate + done-marker (DB-committed).
        new_ids = db.upsert_papers(conn, deduped)  # persists focus_areas/relevance too
        new_set = set(new_ids)
        n_new_total += len(new_ids)
        for r in deduped:
            db.set_topic_tags(conn, r["paper_id"], tags.get(r["paper_id"], {}))
        # Correct first_seen on any rows a prior weekly run stamped today() (§gotcha 1).
        existing = [r for r in deduped if r["paper_id"] not in new_set]
        if existing:
            db.backdate_first_seen(conn, existing)
        if complete:
            db.mark_census_period(conn, ws.isoformat(), we.isoformat(), SOURCE,
                                  n_harvested=len(raw), n_records=len(deduped))
        else:
            logger.info("  current month %s processed (left resumable; weekly path keeps it fresh)",
                        ws.isoformat())

        logger.info("  [%d/%d] %s: +%d new (%d unique / %d raw) | corpus=%d vectors=%d",
                    i, len(todo), ws.isoformat(), len(new_ids), len(deduped), len(raw),
                    db.count_papers(conn), len(index))
        time.sleep(POLITE_PAUSE)

    total = db.count_papers(conn)
    conn.close()
    logger.info("Census complete: corpus %d → %d (+%d) | vectors=%d",
                start_total, total, n_new_total, len(index))
    return {"windows": len(windows), "processed": len(todo),
            "corpus_before": start_total, "corpus_after": total, "n_new": n_new_total,
            "vectors": len(index), "db_path": str(db_path), "index_path": str(index_path)}


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Census backfill: every PDAC paper over N years into papers + "
                    "vectors (Europe PMC only, no full text, no LLM). Resumable.")
    ap.add_argument("--years", type=int, default=5, help="Backfill length in years from today.")
    ap.add_argument("--from", dest="date_from", default=None,
                    help="Explicit start YYYY-MM-DD (overrides --years; for small validation runs).")
    ap.add_argument("--to", dest="date_to", default=None,
                    help="Explicit end YYYY-MM-DD (defaults to today).")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--index", type=Path, default=DEFAULT_INDEX)
    ap.add_argument("--force", action="store_true", help="Reprocess windows already marked complete.")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    result = census(
        years=args.years, db_path=args.db, index_path=args.index, force=args.force,
        start_override=date.fromisoformat(args.date_from) if args.date_from else None,
        end_override=date.fromisoformat(args.date_to) if args.date_to else None)

    print(f"\nCensus: corpus {result['corpus_before']} → {result['corpus_after']} "
          f"(+{result['n_new']} new) over {result['processed']}/{result['windows']} windows.")
    print(f"Vectors: {result['vectors']}")
    print(f"DB:    {result['db_path']}")
    print(f"Index: {result['index_path']}")


if __name__ == "__main__":
    main()
