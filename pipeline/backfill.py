"""
pipeline/backfill.py — coverage count time-series backfill (Phase 4 analytics).

For each weekly bucket over the last N years, queries Europe PMC hitCount
(COUNTS ONLY — no records, no embeddings, no LLM) for the overall PDAC total and
each focus area's `count_query` (from interest_profile.yaml), and stores them in
the coverage_counts table. This is the keyword "how much is published" lens —
distinct from the digest's embedding+LLM curation.

Re-runnable and resumable: weeks already present are skipped unless --force.
The weekly pipeline calls update_current_week() to keep the latest bucket fresh.

    python -m pipeline.backfill --years 5 -v        # one-time history
    python -m pipeline.backfill --years 5 --force   # recompute everything
"""
from __future__ import annotations

import argparse
import logging
import time
from datetime import date, timedelta
from pathlib import Path

from pipeline.harvest import _session, load_config
from pipeline.score import load_interest_profile
from store import db

logger = logging.getLogger("backfill")

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "corpus.sqlite"
PROFILE_PATH = ROOT / "config" / "interest_profile.yaml"
PAUSE = 0.12  # seconds between hitCount queries (fair-use)


def weekly_buckets(years: int, today: date | None = None) -> list[tuple[date, date]]:
    """Consecutive Mon–Sun buckets from ~N years ago to today (last one clamped)."""
    today = today or date.today()
    start = today - timedelta(days=365 * years)
    start -= timedelta(days=start.weekday())  # back to Monday
    out, cur = [], start
    while cur <= today:
        out.append((cur, min(cur + timedelta(days=6), today)))
        cur += timedelta(days=7)
    return out


def _hitcount(session, base_url: str, query: str) -> int:
    r = session.get(base_url, params={"query": query, "format": "json", "pageSize": 1}, timeout=30)
    r.raise_for_status()
    return int(r.json().get("hitCount", 0))


def _area_queries(profile: dict) -> list[tuple[str, str]]:
    """[(focus_area_id, count_query)] for areas that define a count_query."""
    return [(a["id"], " ".join(a["count_query"].split()))
            for a in profile["focus_areas"] if a.get("count_query")]


def _week_rows(session, base_url, pdac_q, areas, ws: date, we: date) -> list[dict]:
    """One coverage row per series (_total + each area) for a single week."""
    dr = f"FIRST_PDATE:[{ws.isoformat()} TO {we.isoformat()}]"
    base = dict(granularity="week", period_start=ws.isoformat(), period_end=we.isoformat(),
                source="europepmc", method="keyword_hitcount")
    rows = [dict(base, focus_area="_total", count=_hitcount(session, base_url, f"({pdac_q}) AND {dr}"))]
    time.sleep(PAUSE)
    for aid, cq in areas:
        rows.append(dict(base, focus_area=aid,
                         count=_hitcount(session, base_url, f"({pdac_q}) AND {cq} AND {dr}")))
        time.sleep(PAUSE)
    return rows


def backfill(years: int = 5, db_path: Path = DEFAULT_DB, force: bool = False) -> int:
    cfg = load_config()
    ep = cfg["europepmc"]
    base_url, pdac_q = ep["base_url"], " ".join(ep["query"].split())
    areas = _area_queries(load_interest_profile(PROFILE_PATH))
    session = _session(cfg.get("contact_email", ""), cfg.get("tool_name", "lit-agent"))

    conn = db.connect(db_path)
    db.init_schema(conn)
    present = set() if force else db.coverage_periods_present(conn, "week", "europepmc")
    weeks = weekly_buckets(years)
    todo = [w for w in weeks if w[0].isoformat() not in present]
    logger.info("Backfill: %d weeks (%d to do, %d present) × %d series",
                len(weeks), len(todo), len(weeks) - len(todo), len(areas) + 1)

    nq = 0
    for i, (ws, we) in enumerate(todo, 1):
        rows = _week_rows(session, base_url, pdac_q, areas, ws, we)
        db.upsert_coverage(conn, rows)
        nq += len(rows)
        if i % 10 == 0 or i == len(todo):
            logger.info("  %d/%d weeks done (latest %s) — %d queries", i, len(todo), ws.isoformat(), nq)
    conn.close()
    logger.info("Backfill complete: %d queries over %d weeks.", nq, len(todo))
    return nq


def update_current_week(db_path: Path = DEFAULT_DB, n_weeks: int = 4) -> int:
    """Refresh the trailing n weeks' counts — called by the weekly pipeline.

    Recent weeks grow as Europe PMC assigns FIRST_PDATE / indexes new papers, so
    re-querying the last few weeks each run lets the series self-correct.
    """
    cfg = load_config()
    ep = cfg["europepmc"]
    base_url, pdac_q = ep["base_url"], " ".join(ep["query"].split())
    areas = _area_queries(load_interest_profile(PROFILE_PATH))
    session = _session(cfg.get("contact_email", ""), cfg.get("tool_name", "lit-agent"))
    conn = db.connect(db_path)
    db.init_schema(conn)
    nq = 0
    for ws, we in weekly_buckets(1)[-n_weeks:]:
        db.upsert_coverage(conn, _week_rows(session, base_url, pdac_q, areas, ws, we))
        nq += len(areas) + 1
    conn.close()
    return nq


# ---------------------------------------------------------------------------
# Keyword-level counts (the specific high-signal terms; calendar quarters)
# ---------------------------------------------------------------------------

def quarter_buckets(years: int, today: date | None = None) -> list[tuple[date, date, bool, str]]:
    """Calendar-quarter (start, end, is_complete, label) over the last N years."""
    today = today or date.today()
    out = []
    for y in range(today.year - years, today.year + 1):
        for q, (m1, m2) in enumerate([(1, 3), (4, 6), (7, 9), (10, 12)], start=1):
            qs = date(y, m1, 1)
            qe = date(y, 12, 31) if m2 == 12 else date(y, m2 + 1, 1) - timedelta(days=1)
            if qs > today:
                continue
            out.append((qs, min(qe, today), qe <= today, f"{y}Q{q}"))
    return out


def _kw_q(kw: str) -> str:
    return f'"{kw}"' if " " in kw else kw


def _tracked_pairs(profile: dict) -> list[tuple[str, str]]:
    return [(a, kw) for a, kws in (profile.get("tracked_keywords") or {}).items() for kw in kws]


def backfill_keywords(years: int = 5, db_path: Path = DEFAULT_DB, force: bool = False,
                      quarters: list | None = None) -> int:
    """Quarterly hitCount per tracked keyword → keyword_counts. Resumable."""
    cfg = load_config()
    ep = cfg["europepmc"]
    base_url, pdac_q = ep["base_url"], " ".join(ep["query"].split())
    pairs = _tracked_pairs(load_interest_profile(PROFILE_PATH))
    session = _session(cfg.get("contact_email", ""), cfg.get("tool_name", "lit-agent"))
    conn = db.connect(db_path)
    db.init_schema(conn)
    present = set() if force else db.keyword_periods_present(conn, "europepmc")
    quarters = quarters if quarters is not None else quarter_buckets(years)
    logger.info("Keyword backfill: %d keywords x %d quarters", len(pairs), len(quarters))
    nq = 0
    for qs, qe, complete, label in quarters:
        rows = []
        for area, kw in pairs:
            if not force and (area, kw, qs.isoformat()) in present:
                continue
            q = f'({pdac_q}) AND {_kw_q(kw)} AND FIRST_PDATE:[{qs.isoformat()} TO {qe.isoformat()}]'
            rows.append(dict(focus_area=area, keyword=kw, granularity="quarter",
                             period_start=qs.isoformat(), period_end=qe.isoformat(),
                             complete=1 if complete else 0,
                             count=_hitcount(session, base_url, q),
                             source="europepmc", method="keyword_hitcount"))
            nq += 1
            time.sleep(PAUSE)
        if rows:
            db.upsert_keyword_counts(conn, rows)
            logger.info("  %s: +%d (running %d)", label, len(rows), nq)
    conn.close()
    logger.info("Keyword backfill done: %d queries", nq)
    return nq


def update_recent_keyword_quarters(n: int = 2, db_path: Path = DEFAULT_DB) -> int:
    """Refresh the trailing n quarters' keyword counts (force) — called weekly."""
    return backfill_keywords(years=1, db_path=db_path, force=True, quarters=quarter_buckets(1)[-n:])


def main() -> None:
    ap = argparse.ArgumentParser(description="Backfill the coverage count time-series.")
    ap.add_argument("--years", type=int, default=5)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--force", action="store_true", help="Recompute periods already present.")
    ap.add_argument("--keywords", action="store_true",
                    help="Backfill per-keyword quarterly counts (tracked_keywords) instead of topic weekly counts.")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO if a.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    (backfill_keywords if a.keywords else backfill)(a.years, a.db, a.force)


if __name__ == "__main__":
    main()
