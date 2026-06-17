"""
pipeline/analytics.py — week/month/year coverage aggregations (Phase 4).

Simple aggregations over the timestamped, topic-tagged corpus: per-focus-area
counts for each window and the delta vs the immediately-preceding equal-length
window. Precomputed and cached to data/analytics.json so the Space renders them
instantly (the Space never recomputes), and a compact "coverage at a glance"
block is rendered into the digest.

Windows are keyed on first_seen_date (the canonical "new" date), so counts
reconcile against the runs table. With only one week of history the month/year
windows coincide with the week and prior-period deltas are the full count
(everything is new) — they fill in as weeks accumulate.

Roadmap (CLAUDE.md "Post-v1 roadmap"): per-area breakdown stays first-class so
the Space can render one tab per area off this same cache.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

WINDOWS = {"week": 7, "month": 30, "year": 365}


def _counts(conn: sqlite3.Connection, start: str, end: str) -> tuple[int, dict[str, int]]:
    total = conn.execute(
        "SELECT COUNT(*) FROM papers WHERE first_seen_date BETWEEN ? AND ?",
        (start, end)).fetchone()[0]
    rows = conn.execute(
        "SELECT t.focus_area, COUNT(DISTINCT t.paper_id) FROM topic_tags t "
        "JOIN papers p ON p.paper_id = t.paper_id "
        "WHERE p.first_seen_date BETWEEN ? AND ? GROUP BY t.focus_area",
        (start, end)).fetchall()
    return total, {r[0]: r[1] for r in rows}


def compute(conn: sqlite3.Connection, today: date | None = None) -> dict:
    """Per-window totals + per-area counts + deltas vs the prior equal-length window."""
    today = today or date.today()
    out: dict = {"generated_at": datetime.now().isoformat(timespec="seconds"),
                 "today": today.isoformat(), "windows": {}}
    for name, days in WINDOWS.items():
        cur_start = (today - timedelta(days=days - 1)).isoformat()
        cur_end = today.isoformat()
        prior_start = (today - timedelta(days=2 * days - 1)).isoformat()
        prior_end = (today - timedelta(days=days)).isoformat()
        cur_total, cur_area = _counts(conn, cur_start, cur_end)
        prior_total, prior_area = _counts(conn, prior_start, prior_end)
        delta_area = {a: cur_area.get(a, 0) - prior_area.get(a, 0)
                      for a in set(cur_area) | set(prior_area)}
        out["windows"][name] = {
            "from": cur_start, "to": cur_end,
            "total": cur_total, "by_area": cur_area,
            "delta_total": cur_total - prior_total, "delta_by_area": delta_area,
        }
    return out


def cache(data: dict, path: str | Path = "data/analytics.json") -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))
    return path


def _area_name(profile: dict, area_id: str) -> str:
    for a in profile.get("focus_areas", []):
        if a["id"] == area_id:
            return a["name"]
    return area_id


def footer_html(data: dict, profile: dict, window_key: str = "week") -> str:
    """Compact 'coverage at a glance' block for the digest (this window, with deltas)."""
    w = data["windows"][window_key]
    rows = sorted(w["by_area"].items(), key=lambda x: -x[1])
    items = []
    for aid, n in rows:
        d = w["delta_by_area"].get(aid, 0)
        arrow = f' <span style="color:#059669;">▲{d}</span>' if d > 0 else (
            f' <span style="color:#dc2626;">▼{abs(d)}</span>' if d < 0 else "")
        items.append(f'<li style="margin:2px 0;">{_area_name(profile, aid)}: <b>{n}</b>{arrow}</li>')
    return (
        '<div style="background:#f9fafb;border:1px solid #e5e7eb;border-radius:6px;'
        'padding:12px 16px;margin:18px 0;">'
        f'<div style="font-size:13px;font-weight:600;margin-bottom:6px;">Coverage at a glance '
        f'({window_key}, Δ vs prior {window_key})</div>'
        f'<ul style="margin:0;padding-left:18px;font-size:13px;color:#374151;list-style:disc;">'
        f'{"".join(items) or "<li>No tagged papers this window.</li>"}</ul></div>')
