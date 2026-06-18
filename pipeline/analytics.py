"""
pipeline/analytics.py — rolling coverage trends (Phase 4).

Reads the keyword count time-series (coverage_counts, weekly buckets — see
pipeline/backfill.py) and computes trailing-window totals + deltas vs the prior
equal window, for quarter / year. Week/month were dropped — too noisy and
indexing-lagged to mean anything; the yearly/quarterly trend is the real signal.

    quarter = last 13 weekly buckets vs the prior 13
    year    = last 52 weekly buckets vs the prior 52

Precomputed and cached to data/analytics.json so the Space renders instantly (it
never recomputes), and a compact "coverage trends" table is rendered into the
digest. These are a coverage-VOLUME lens (keyword hitCounts), distinct from the
digest's embedding+LLM curation.

Roadmap (CLAUDE.md "Post-v1 roadmap"): per-area series stay first-class so the
Space can render one tab per area off this same cache.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

from store import db

WINDOWS = {"quarter": 13, "year": 52}  # measured in weekly buckets
# Recent weeks undercount until Europe PMC finishes indexing (FIRST_PDATE lag).
# Drop the most recent LAG_WEEKS complete weeks so every window compares
# settled-vs-settled data instead of showing a spurious decline.
LAG_WEEKS = 2


def compute_trends(conn, today: date | None = None) -> dict:
    """Per-series trailing-window totals + deltas vs the prior equal window."""
    areas = [r[0] for r in conn.execute(
        "SELECT DISTINCT focus_area FROM coverage_counts WHERE granularity='week'")]
    out: dict = {"generated_at": datetime.now().isoformat(timespec="seconds"),
                 "windows": list(WINDOWS), "as_of": None, "series": {}}
    for area in areas:
        series = db.get_coverage(conn, area, "week")
        # complete 7-day weeks only, then drop the last LAG_WEEKS (indexing lag)
        series = [r for r in series
                  if (date.fromisoformat(r["period_end"]) - date.fromisoformat(r["period_start"])).days == 6]
        if LAG_WEEKS and len(series) > LAG_WEEKS:
            series = series[:-LAG_WEEKS]
        if not series:
            continue
        counts = [row["count"] or 0 for row in series]  # chronological
        out["as_of"] = series[-1]["period_end"]
        wins = {}
        for name, n in WINDOWS.items():
            cur = sum(counts[-n:]) if counts else 0
            prior = sum(counts[-2 * n:-n]) if len(counts) >= 2 * n else None
            wins[name] = {"current": cur, "prior": prior,
                          "delta": (cur - prior) if prior is not None else None}
        out["series"][area] = wins
    return out


def cache(data: dict, path: str | Path = "data/analytics.json") -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))
    return path


def _area_name(profile: dict, area_id: str) -> str:
    if area_id == "_total":
        return "All PDAC"
    for a in profile.get("focus_areas", []):
        if a["id"] == area_id:
            return a["name"]
    return area_id


def _delta_span(d) -> str:
    if d is None:
        return ' <span style="color:#9ca3af;">—</span>'
    if d > 0:
        return f' <span style="color:#059669;">▲{d}</span>'
    if d < 0:
        return f' <span style="color:#dc2626;">▼{abs(d)}</span>'
    return ' <span style="color:#9ca3af;">0</span>'


def footer_html(data: dict, profile: dict) -> str:
    """Compact 'coverage trends' table for the digest (quarter/year + deltas)."""
    series = data.get("series", {})
    order = ["_total"] + [a["id"] for a in profile.get("focus_areas", [])]
    rows = []
    for aid in order:
        w = series.get(aid)
        if not w:
            continue
        cells = "".join(
            f'<td style="padding:2px 10px;text-align:right;white-space:nowrap;">'
            f'{w[win]["current"]}{_delta_span(w[win]["delta"])}</td>'
            for win in ("quarter", "year"))
        weight = "600" if aid == "_total" else "400"
        rows.append(f'<tr><td style="padding:2px 10px;font-weight:{weight};">'
                    f'{_area_name(profile, aid)}</td>{cells}</tr>')
    if not rows:
        return ('<div style="font-size:12px;color:#6b7280;margin:18px 0;">No coverage trend data '
                'yet — run <code>python -m pipeline.backfill</code>.</div>')
    return (
        '<div style="margin:18px 0;">'
        '<div style="font-size:13px;font-weight:600;margin-bottom:4px;">Coverage trends '
        f'<span style="font-weight:400;color:#6b7280;">(papers per period · Δ vs prior · '
        f'as of {data.get("as_of", "")})</span></div>'
        '<table style="border-collapse:collapse;font-size:12px;color:#374151;">'
        '<tr style="color:#6b7280;"><th style="text-align:left;padding:2px 10px;">Area</th>'
        '<th style="padding:2px 10px;">Quarter</th><th style="padding:2px 10px;">Year</th></tr>'
        + "".join(rows) + '</table>'
        '<div style="font-size:11px;color:#9ca3af;margin-top:4px;">Keyword-match counts '
        '(Europe PMC) — a coverage-volume lens, distinct from the curated picks above. '
        'Quarter = trailing 13 weeks, Year = trailing 52 weeks; recent weeks undercount '
        'until indexing settles.</div></div>')


# ---------------------------------------------------------------------------
# Keyword movers (specific terms; the drill-down beneath the topic rollup)
# ---------------------------------------------------------------------------

def keyword_movers(conn, profile: dict, top_n: int = 3) -> dict:
    """Per area, the tracked keywords with the largest YoY-style growth.

    For each (area, keyword): trailing 4 COMPLETE quarters vs the prior 4 (a
    rolling-12-month comparison; the partial current quarter is excluded).
    Returns {area: [{keyword, cur, prior, delta, pct}]} sorted by % growth.
    """
    from collections import defaultdict
    series = defaultdict(list)
    for r in db.get_keyword_counts(conn, "quarter"):
        if r["complete"]:
            series[(r["focus_area"], r["keyword"])].append(r["count"] or 0)
    by_area: dict = defaultdict(list)
    for (area, kw), counts in series.items():
        cur = sum(counts[-4:])
        prior = sum(counts[-8:-4]) if len(counts) >= 8 else None
        delta = (cur - prior) if prior is not None else None
        pct = round(delta / prior * 100) if (prior and prior > 0) else None
        by_area[area].append({"keyword": kw, "cur": cur, "prior": prior, "delta": delta, "pct": pct})
    out = {}
    for area, lst in by_area.items():
        lst.sort(key=lambda m: (m["pct"] if m["pct"] is not None else -999, m["delta"] or 0), reverse=True)
        out[area] = lst[:top_n]
    return out


def keyword_movers_html(movers: dict, profile: dict) -> str:
    """Compact per-area 'keyword movers' list for the digest (specific terms only)."""
    name = {a["id"]: a["name"] for a in profile.get("focus_areas", [])}
    items = []
    for aid in [a["id"] for a in profile.get("focus_areas", [])]:
        ms = movers.get(aid)
        if not ms:
            continue
        parts = []
        for m in ms:
            arrow = ""
            if m["pct"] is not None and m["pct"] > 0:
                arrow = f' <span style="color:#059669;">▲{m["pct"]}%</span>'
            elif m["pct"] is not None and m["pct"] < 0:
                arrow = f' <span style="color:#dc2626;">▼{abs(m["pct"])}%</span>'
            parts.append(f'{m["keyword"]} ({m["cur"]}){arrow}')
        items.append(f'<li style="margin:3px 0;"><b style="font-weight:500;">{name.get(aid, aid)}:</b> '
                     f'{" · ".join(parts)}</li>')
    if not items:
        return ""
    return ('<div style="margin:16px 0;">'
            '<div style="font-size:13px;font-weight:600;margin-bottom:4px;">Keyword movers '
            '<span style="font-weight:400;color:#6b7280;">(specific terms · last 12 mo count · '
            '▲ vs prior 12 mo)</span></div>'
            f'<ul style="margin:0;padding-left:18px;font-size:12px;color:#374151;list-style:disc;">'
            f'{"".join(items)}</ul></div>')
