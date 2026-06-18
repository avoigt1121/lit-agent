"""
store/db.py — SQLite corpus store (Phase 1).

Schema (CLAUDE.md §"Corpus store"):
  papers      PK = paper_id (normalized DOI, else a stable synthetic id assigned
              by normalize.py); one row per deduped record. Nested fields (ids,
              authors, mesh, annotations, focus_areas) are stored as JSON text.
  topic_tags  (paper_id, focus_area, score) — multi-label, feeds analytics.
  runs        (run_date, window_from, window_to, n_harvested, n_new, n_emailed)
              — audit trail + analytics deltas.
  meta        (key, value) — schema_version etc.

"New" is keyed on first_seen_date: a paper is new on the run where its paper_id
first appears. upsert_papers() preserves the original first_seen_date on update,
so re-ingesting the same paper never re-marks it new.

Vectors live in store/vectors.py (the embedding index), keyed by embedding_id.

Persistence: the SQLite file is committed to the durable store (HF Dataset repo)
at the end of each run; the Space opens it read-only.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

SCHEMA_VERSION = 3

# Scalar columns stored as their own SQLite columns; everything else in the
# normalized record is JSON-encoded into the matching column.
_SCALAR_COLS = (
    "doi", "title", "abstract", "journal_or_server", "published_date",
    "first_seen_date", "is_oa", "oa_fulltext_url", "source", "is_preprint",
    "linked_published_doi", "relevance_score", "embedding_id",
)
_JSON_COLS = ("ids", "authors", "mesh", "annotations", "focus_areas")


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS papers (
            paper_id             TEXT PRIMARY KEY,
            doi                  TEXT,
            title                TEXT,
            abstract             TEXT,
            ids                  TEXT,   -- JSON {pmid, pmcid, preprint_doi}
            authors              TEXT,   -- JSON list
            journal_or_server    TEXT,
            published_date       TEXT,
            first_seen_date      TEXT,
            is_oa                INTEGER,
            oa_fulltext_url      TEXT,
            source               TEXT,
            is_preprint          INTEGER,
            linked_published_doi TEXT,
            mesh                 TEXT,   -- JSON list
            annotations          TEXT,   -- JSON {genes, diseases}
            focus_areas          TEXT,   -- JSON list
            relevance_score      REAL,
            embedding_id         TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_papers_first_seen ON papers(first_seen_date);
        CREATE INDEX IF NOT EXISTS idx_papers_doi        ON papers(doi);

        CREATE TABLE IF NOT EXISTS topic_tags (
            paper_id   TEXT NOT NULL,
            focus_area TEXT NOT NULL,
            score      REAL,
            PRIMARY KEY (paper_id, focus_area),
            FOREIGN KEY (paper_id) REFERENCES papers(paper_id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_tags_area ON topic_tags(focus_area);

        CREATE TABLE IF NOT EXISTS runs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date    TEXT,
            window_from TEXT,
            window_to   TEXT,
            n_harvested INTEGER,
            n_new       INTEGER,
            n_emailed   INTEGER
        );

        CREATE TABLE IF NOT EXISTS coverage_counts (
            granularity  TEXT NOT NULL,   -- 'week' (rolled up to month/year in analytics)
            period_start TEXT NOT NULL,   -- ISO date, inclusive
            period_end   TEXT NOT NULL,   -- ISO date, inclusive
            focus_area   TEXT NOT NULL,   -- focus-area id, or '_total'
            count        INTEGER,
            source       TEXT,            -- e.g. 'europepmc'
            method       TEXT,            -- e.g. 'keyword_hitcount'
            PRIMARY KEY (granularity, period_start, focus_area, source)
        );
        CREATE INDEX IF NOT EXISTS idx_cov_area ON coverage_counts(focus_area, period_start);

        CREATE TABLE IF NOT EXISTS keyword_counts (
            focus_area   TEXT NOT NULL,
            keyword      TEXT NOT NULL,
            granularity  TEXT NOT NULL,   -- 'quarter'
            period_start TEXT NOT NULL,
            period_end   TEXT NOT NULL,
            complete     INTEGER,         -- 1 = full quarter, 0 = partial (current)
            count        INTEGER,
            source       TEXT,
            method       TEXT,
            PRIMARY KEY (focus_area, keyword, granularity, period_start, source)
        );
        CREATE INDEX IF NOT EXISTS idx_kw ON keyword_counts(focus_area, keyword, period_start);

        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
        """
    )
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()


def _row_from_record(rec: dict) -> dict:
    """Flatten a normalized record into a column->value dict for SQLite."""
    row = {"paper_id": rec["paper_id"]}
    for col in _SCALAR_COLS:
        val = rec.get(col)
        if col in ("is_oa", "is_preprint"):
            val = 1 if val else 0
        row[col] = val
    for col in _JSON_COLS:
        row[col] = json.dumps(rec.get(col))
    return row


def record_to_dict(row: sqlite3.Row) -> dict:
    """Inverse of _row_from_record: a DB row back to a normalized record."""
    rec = dict(row)
    for col in _JSON_COLS:
        rec[col] = json.loads(rec[col]) if rec.get(col) else (
            {} if col in ("ids", "annotations") else [])
    rec["is_oa"] = bool(rec.get("is_oa"))
    rec["is_preprint"] = bool(rec.get("is_preprint"))
    return rec


def existing_ids(conn: sqlite3.Connection, paper_ids) -> set[str]:
    ids = list(paper_ids)
    if not ids:
        return set()
    out: set[str] = set()
    # chunk to stay under SQLite's variable limit
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        q = f"SELECT paper_id FROM papers WHERE paper_id IN ({','.join('?' * len(chunk))})"
        out.update(r[0] for r in conn.execute(q, chunk))
    return out


def upsert_papers(conn: sqlite3.Connection, records: list[dict]) -> list[str]:
    """Insert new papers, update mutable fields on existing ones.

    Returns the list of paper_ids that were NEW this call (their first_seen_date
    is the canonical "new" date). On update, first_seen_date is never changed —
    re-ingesting a paper does not re-mark it new. A preprint later linked to a
    published version is updated in place (e.g. linked_published_doi).
    """
    have = existing_ids(conn, [r["paper_id"] for r in records])
    new_ids: list[str] = []
    cols = ["paper_id", *_SCALAR_COLS, *_JSON_COLS]
    placeholders = ",".join("?" * len(cols))
    # On conflict, update everything EXCEPT first_seen_date.
    update_cols = [c for c in cols if c not in ("paper_id", "first_seen_date")]
    update_clause = ",".join(f"{c}=excluded.{c}" for c in update_cols)
    sql = (
        f"INSERT INTO papers ({','.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT(paper_id) DO UPDATE SET {update_clause}"
    )
    for rec in records:
        row = _row_from_record(rec)
        conn.execute(sql, [row[c] for c in cols])
        if rec["paper_id"] not in have:
            new_ids.append(rec["paper_id"])
    conn.commit()
    return new_ids


def set_topic_tags(conn: sqlite3.Connection, paper_id: str, tags: dict[str, float]) -> None:
    conn.execute("DELETE FROM topic_tags WHERE paper_id=?", (paper_id,))
    conn.executemany(
        "INSERT INTO topic_tags(paper_id, focus_area, score) VALUES(?,?,?)",
        [(paper_id, area, float(score)) for area, score in tags.items()],
    )
    conn.commit()


def record_run(conn: sqlite3.Connection, run_date: str, window_from: str,
               window_to: str, n_harvested: int, n_new: int, n_emailed: int = 0) -> None:
    conn.execute(
        "INSERT INTO runs(run_date, window_from, window_to, n_harvested, n_new, n_emailed) "
        "VALUES(?,?,?,?,?,?)",
        (run_date, window_from, window_to, n_harvested, n_new, n_emailed),
    )
    conn.commit()


def count_papers(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]


def iter_papers(conn: sqlite3.Connection):
    for row in conn.execute("SELECT * FROM papers"):
        yield record_to_dict(row)


def papers_first_seen_between(conn: sqlite3.Connection, start: str, end: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM papers WHERE first_seen_date >= ? AND first_seen_date <= ? "
        "ORDER BY relevance_score DESC",
        (start, end),
    )
    return [record_to_dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Coverage counts (the keyword-based trend time-series; see pipeline/backfill.py)
# ---------------------------------------------------------------------------

def upsert_coverage(conn: sqlite3.Connection, rows: list[dict]) -> None:
    """Idempotent upsert of coverage rows (re-running a period overwrites its count)."""
    conn.executemany(
        "INSERT INTO coverage_counts"
        "(granularity, period_start, period_end, focus_area, count, source, method) "
        "VALUES(:granularity, :period_start, :period_end, :focus_area, :count, :source, :method) "
        "ON CONFLICT(granularity, period_start, focus_area, source) "
        "DO UPDATE SET count=excluded.count, period_end=excluded.period_end, method=excluded.method",
        rows,
    )
    conn.commit()


def coverage_periods_present(conn: sqlite3.Connection, granularity: str, source: str) -> set[str]:
    """period_start values already stored (for resumable backfills)."""
    rows = conn.execute(
        "SELECT DISTINCT period_start FROM coverage_counts WHERE granularity=? AND source=?",
        (granularity, source),
    )
    return {r[0] for r in rows}


def get_coverage(conn: sqlite3.Connection, focus_area: str, granularity: str = "week") -> list[dict]:
    """Ordered weekly series for one area (or '_total'): [{period_start, period_end, count}]."""
    rows = conn.execute(
        "SELECT period_start, period_end, count FROM coverage_counts "
        "WHERE focus_area=? AND granularity=? ORDER BY period_start",
        (focus_area, granularity),
    )
    return [{"period_start": r[0], "period_end": r[1], "count": r[2]} for r in rows]


def upsert_keyword_counts(conn: sqlite3.Connection, rows: list[dict]) -> None:
    conn.executemany(
        "INSERT INTO keyword_counts"
        "(focus_area, keyword, granularity, period_start, period_end, complete, count, source, method) "
        "VALUES(:focus_area, :keyword, :granularity, :period_start, :period_end, :complete, :count, :source, :method) "
        "ON CONFLICT(focus_area, keyword, granularity, period_start, source) DO UPDATE SET "
        "count=excluded.count, period_end=excluded.period_end, complete=excluded.complete, method=excluded.method",
        rows,
    )
    conn.commit()


def keyword_periods_present(conn: sqlite3.Connection, source: str) -> set:
    """(focus_area, keyword, period_start) already stored (for resumable backfills)."""
    return {(r[0], r[1], r[2]) for r in conn.execute(
        "SELECT focus_area, keyword, period_start FROM keyword_counts WHERE source=?", (source,))}


def get_keyword_counts(conn: sqlite3.Connection, granularity: str = "quarter") -> list[dict]:
    rows = conn.execute(
        "SELECT focus_area, keyword, period_start, period_end, complete, count "
        "FROM keyword_counts WHERE granularity=? ORDER BY period_start", (granularity,))
    return [{"focus_area": r[0], "keyword": r[1], "period_start": r[2],
             "period_end": r[3], "complete": r[4], "count": r[5]} for r in rows]
