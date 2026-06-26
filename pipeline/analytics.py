"""
pipeline/analytics.py — rolling coverage trends (Phase 4).

Reads the keyword count time-series (coverage_counts, weekly buckets — see
pipeline/backfill.py) and computes, per series, trailing-window totals + deltas
and SHARE OF VOICE (each area as a fraction of all PDAC papers).

    quarter = last 13 weekly buckets vs the prior 13   (kept in JSON; not headlined)
    year    = last 52 weekly buckets vs the prior 52
    share   = area / _total, trailing year vs prior year (Δ in percentage points)

Precomputed and cached to data/analytics.json so the Space renders instantly (it
never recomputes). Two blocks render into the digest, both a coverage-VOLUME lens
(keyword hitCounts), distinct from the digest's embedding+LLM curation:

  Concept A — footer_html(): a share-of-voice leaderboard with a per-area trend
    sparkline. Share of voice is the headline because it cancels the field's
    overall growth AND Europe PMC indexing lag (both hit numerator and the
    _total denominator equally), so it shows real shifts in attention rather
    than the spurious ~20% "decline" a raw trailing-13-week quarter shows.
  Concept B — keyword_movers_html(): the fastest-rising specific terms as a
    ranked bar strip, each linking to the Europe PMC papers behind the count.

Roadmap (CLAUDE.md "Post-v1 roadmap"): per-area series stay first-class so the
Space can render one tab per area off this same cache.
"""
from __future__ import annotations

import json
import math
import urllib.parse
from datetime import date, datetime, timedelta
from pathlib import Path

from store import db

WINDOWS = {"quarter": 13, "year": 52}  # measured in weekly buckets
SPARK_WEEKS = 52  # trailing weekly volume kept per series for the digest sparkline
# Recent weeks undercount until Europe PMC finishes indexing (FIRST_PDATE lag).
# Drop the most recent LAG_WEEKS complete weeks so every window compares
# settled-vs-settled data. Even so, a raw trailing-13-week "quarter" sits almost
# entirely inside the lag zone and reads as a spurious ~20% drop — so the digest
# headlines SHARE OF VOICE (lag-robust; see module docstring) and keeps the raw
# quarter only in the JSON for the Space/debugging.
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
        wins["spark"] = counts[-SPARK_WEEKS:]  # trailing weekly volume for the sparkline
        out["series"][area] = wins
    _add_share_of_voice(out)
    return out


def _add_share_of_voice(out: dict) -> None:
    """Annotate each series with share of voice vs the _total denominator.

    share = area papers / all-PDAC papers, trailing year and prior year; delta
    is in percentage POINTS. This is the lag-robust headline metric: indexing
    lag and overall growth scale numerator and denominator together.
    """
    ty = out["series"].get("_total", {}).get("year", {})
    tc, tp = ty.get("current"), ty.get("prior")
    for w in out["series"].values():
        y = w.get("year", {})
        sc = round(y["current"] / tc * 100, 1) if tc else None
        sp = (round(y["prior"] / tp * 100, 1)
              if (tp and y.get("prior") is not None) else None)
        w["share"] = {"current": sc, "prior": sp,
                      "delta": (round(sc - sp, 1) if (sc is not None and sp is not None) else None)}


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


def _pp_span(d) -> str:
    """Percentage-point delta badge (share-of-voice change vs prior year)."""
    if d is None:
        return '<span style="color:#9ca3af;font-size:12px;">—</span>'
    if d > 0.05:
        return f'<span style="color:#059669;font-size:12px;">▲{d:.1f}pp</span>'
    if d < -0.05:
        return f'<span style="color:#dc2626;font-size:12px;">▼{abs(d):.1f}pp</span>'
    return '<span style="color:#9ca3af;font-size:12px;">0pp</span>'


def _yoy_pct(win: dict):
    c, p = win.get("current"), win.get("prior")
    return round((c - p) / p * 100) if p else None


def _chunk_sum(vals: list[int], bars: int = 13) -> list[int]:
    """Aggregate a weekly series into ~`bars` equal buckets (e.g. 52wk -> 13)."""
    if not vals:
        return []
    size = max(1, math.ceil(len(vals) / bars))
    return [sum(vals[i:i + size]) for i in range(0, len(vals), size)][-bars:]


def _sparkbars(counts: list[int], bars: int = 13, h: int = 26, bw: int = 5) -> str:
    """Email-safe mini bar chart (Gmail strips inline SVG; table cells survive).

    Each bar scaled to the series' own max, so it shows that area's trajectory.
    """
    vals = _chunk_sum(counts, bars)
    if not vals:
        return ""
    vmax = max(vals) or 1
    cells = "".join(
        f'<td valign="bottom" style="padding:0 1px;">'
        f'<div style="width:{bw}px;height:{max(2, round(v / vmax * (h - 2)))}px;'
        f'background:#9bc1e8;font-size:0;line-height:0;">&nbsp;</div></td>'
        for v in vals)
    return (f'<table cellpadding="0" cellspacing="0" role="presentation" '
            f'style="border-collapse:collapse;height:{h}px;"><tr>{cells}</tr></table>')


def footer_html(data: dict, profile: dict) -> str:
    """Concept A — share-of-voice leaderboard + per-area trend sparkline."""
    series = data.get("series", {})
    rows_data = [(a["name"], series[a["id"]]) for a in profile.get("focus_areas", [])
                 if series.get(a["id"])]
    if not rows_data:
        return ('<div style="font-size:12px;color:#6b7280;margin:18px 0;">No coverage trend data '
                'yet — run <code>python -m pipeline.backfill</code>.</div>')
    # Lead with who is gaining ground: sort by share-of-voice change (None last).
    rows_data.sort(key=lambda t: (t[1].get("share", {}).get("delta") is not None,
                                  t[1].get("share", {}).get("delta") or 0.0), reverse=True)

    body = []
    for name, w in rows_data:
        sh = w.get("share", {})
        cur = sh.get("current")
        share_txt = f'{cur:.1f}%' if cur is not None else '—'
        body.append(
            '<tr>'
            f'<td style="padding:6px 10px;font-size:12px;color:#374151;">{name}</td>'
            f'<td style="padding:6px 10px;">{_sparkbars(w.get("spark") or [])}</td>'
            f'<td style="padding:6px 10px;text-align:right;font-size:13px;font-weight:600;'
            f'color:#111;white-space:nowrap;">{share_txt}</td>'
            f'<td style="padding:6px 10px;text-align:right;white-space:nowrap;">'
            f'{_pp_span(sh.get("delta"))}</td></tr>')

    ty = series.get("_total", {}).get("year", {})
    ctx = ""
    if ty.get("current") is not None:
        yoy = _yoy_pct(ty)
        yoy_txt = (f' · <span style="color:#059669;">▲{yoy}% YoY</span>' if (yoy and yoy > 0)
                   else (f' · <span style="color:#dc2626;">▼{abs(yoy)}% YoY</span>' if yoy else ''))
        ctx = (f'<div style="font-size:12px;color:#6b7280;margin:2px 0 8px;">All PDAC: '
               f'{ty["current"]:,} papers in the trailing year{yoy_txt}</div>')

    return (
        '<div style="margin:18px 0;">'
        '<div style="font-size:13px;font-weight:600;margin-bottom:2px;">Coverage by focus area '
        f'<span style="font-weight:400;color:#6b7280;">(share of all PDAC papers · 12-mo trend · '
        f'Δ vs prior year · as of {data.get("as_of", "")})</span></div>'
        + ctx +
        '<table style="border-collapse:collapse;font-size:12px;color:#374151;">'
        '<tr style="color:#6b7280;">'
        '<th style="text-align:left;padding:2px 10px;">Area</th>'
        '<th style="text-align:left;padding:2px 10px;">Trend (12 mo)</th>'
        '<th style="padding:2px 10px;text-align:right;">Share</th>'
        '<th style="padding:2px 10px;text-align:right;">Δ vs last yr</th></tr>'
        + "".join(body) + '</table>'
        '<div style="font-size:11px;color:#9ca3af;margin-top:6px;">Share of voice = an area\'s '
        'papers as a fraction of all PDAC papers (Europe PMC keyword match), trailing 12 months '
        'vs the prior 12 — it cancels the field\'s overall growth and indexing lag, so it reflects '
        'real shifts in attention. Bars show each area\'s own weekly volume over the year '
        '(scaled to itself).</div></div>')


# ---------------------------------------------------------------------------
# Keyword movers (specific terms; the drill-down beneath the topic rollup)
# ---------------------------------------------------------------------------

# A keyword needs at least this many papers in the prior 12-mo baseline before a
# % growth is trustworthy — otherwise a 2→8 blip reads as "+300%" and dominates
# the strip. Below the floor the term is still tracked but excluded from the
# movers ranking (pct=None), so "what's heating up" reflects real, sustained rises.
MOVER_MIN_PRIOR = 8


def keyword_movers(conn, profile: dict, top_n: int = 3) -> dict:
    """Per area, the tracked keywords with the largest YoY-style growth.

    For each (area, keyword): trailing 4 COMPLETE quarters vs the prior 4 (a
    rolling-12-month comparison; the partial current quarter is excluded). A
    percentage is only assigned when the prior-year baseline clears
    MOVER_MIN_PRIOR, so small-count noise doesn't surface as a top mover.
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
        pct = (round(delta / prior * 100)
               if (prior and prior >= MOVER_MIN_PRIOR) else None)
        by_area[area].append({"keyword": kw, "cur": cur, "prior": prior, "delta": delta, "pct": pct})
    out = {}
    for area, lst in by_area.items():
        lst.sort(key=lambda m: (m["pct"] if m["pct"] is not None else -999, m["delta"] or 0), reverse=True)
        out[area] = lst[:top_n]
    return out


def keyword_movers_html(movers: dict, profile: dict, pdac_query: str | None = None,
                        today: date | None = None, top_n: int = 8,
                        see_all_url: str | None = None) -> str:
    """Concept B — 'what's heating up': the fastest-rising specific terms across
    all areas, as a ranked bar strip. Each term links to the Europe PMC papers
    behind the count (when the PDAC query is supplied). When more movers exist
    than `top_n` and `see_all_url` is given, a "See all" link is appended."""
    name = {a["id"]: a["name"] for a in profile.get("focus_areas", [])}
    flat = [(aid, m) for aid, ms in (movers or {}).items() for m in ms
            if m.get("pct") is not None]
    if not flat:
        return ""
    flat.sort(key=lambda t: t[1]["pct"], reverse=True)
    n_all = len(flat)
    flat = flat[:top_n]
    maxpct = max(m["pct"] for _, m in flat) or 1
    today = today or date.today()
    start, end = (today - timedelta(days=365)).isoformat(), today.isoformat()

    rows = []
    for aid, m in flat:
        kw = m["keyword"]
        label = (f'<a href="{_epmc_link(pdac_query, kw, start, end)}" '
                 f'style="color:#1d4ed8;text-decoration:none;">{kw}</a>' if pdac_query else kw)
        wpct = max(6, round(m["pct"] / maxpct * 100))
        rows.append(
            '<tr>'
            f'<td style="padding:4px 10px 4px 0;font-size:13px;white-space:nowrap;">{label} '
            f'<span style="font-size:11px;color:#9ca3af;">{name.get(aid, aid)}</span></td>'
            '<td style="padding:4px 0;width:55%;">'
            '<div style="background:#f3f4f6;border-radius:3px;">'
            f'<div style="width:{wpct}%;height:14px;background:#f0997b;border-radius:3px;'
            'font-size:0;line-height:0;">&nbsp;</div></div></td>'
            f'<td style="padding:4px 0 4px 10px;text-align:right;font-size:13px;font-weight:600;'
            f'color:#b45309;white-space:nowrap;">+{m["pct"]}%</td></tr>')
    see_all = ""
    if see_all_url and n_all > len(flat):
        see_all = (f'<div style="font-size:12px;margin-top:6px;">'
                   f'<a href="{see_all_url}" style="color:#1d4ed8;text-decoration:none;font-weight:600;">'
                   f'See all {n_all} rising terms on the site →</a></div>')
    return (
        '<div style="margin:16px 0;">'
        '<div style="font-size:13px;font-weight:600;margin-bottom:4px;">What\'s heating up '
        '<span style="font-weight:400;color:#6b7280;">(fastest-rising terms · last 12 mo vs '
        'prior 12 mo' + (' · click a term for the papers' if pdac_query else '') + ')</span></div>'
        '<table style="border-collapse:collapse;width:100%;max-width:520px;">'
        + "".join(rows) + '</table>' + see_all + '</div>')


def movers_full_html(movers: dict, profile: dict, pdac_query: str | None = None,
                     today: date | None = None) -> str:
    """The FULL keyword-trend table for the Space 'Trends' tab — every tracked
    term across all areas (not just the email's top slice), with its 12-mo count,
    prior-year baseline, and % change. Terms below the trust floor show '—' for %
    (still listed). Renders from the cached movers dict (no DB read)."""
    name = {a["id"]: a["name"] for a in profile.get("focus_areas", [])}
    flat = [(aid, m) for aid, ms in (movers or {}).items() for m in ms]
    if not flat:
        return "<p style='color:#6b7280;'>No keyword-trend data yet.</p>"
    # Sort: real movers (pct set) by % desc first, then the rest by current count.
    flat.sort(key=lambda t: (t[1].get("pct") is not None, t[1].get("pct") or 0,
                             t[1].get("cur") or 0), reverse=True)
    today = today or date.today()
    start, end = (today - timedelta(days=365)).isoformat(), today.isoformat()

    rows = []
    for aid, m in flat:
        kw = m["keyword"]
        label = (f'<a href="{_epmc_link(pdac_query, kw, start, end)}" '
                 f'style="color:#1d4ed8;text-decoration:none;">{kw}</a>' if pdac_query else kw)
        pct = m.get("pct")
        pct_html = (f'<span style="color:{"#059669" if pct >= 0 else "#dc2626"};font-weight:600;">'
                    f'{"+" if pct >= 0 else ""}{pct}%</span>' if pct is not None
                    else '<span style="color:#9ca3af;">—</span>')
        rows.append(
            '<tr>'
            f'<td style="padding:4px 12px 4px 0;font-size:13px;">{label}</td>'
            f'<td style="padding:4px 12px;font-size:12px;color:#6b7280;">{name.get(aid, aid)}</td>'
            f'<td style="padding:4px 12px;text-align:right;font-size:13px;">{m.get("cur", 0)}</td>'
            f'<td style="padding:4px 12px;text-align:right;font-size:13px;color:#6b7280;">'
            f'{m.get("prior") if m.get("prior") is not None else "—"}</td>'
            f'<td style="padding:4px 0;text-align:right;font-size:13px;">{pct_html}</td></tr>')
    return (
        '<div style="margin:8px 0;">'
        '<table style="border-collapse:collapse;width:100%;max-width:640px;">'
        '<tr style="color:#6b7280;font-size:12px;text-align:left;">'
        '<th style="padding:2px 12px 2px 0;">Term</th><th style="padding:2px 12px;">Area</th>'
        '<th style="padding:2px 12px;text-align:right;">Last 12 mo</th>'
        '<th style="padding:2px 12px;text-align:right;">Prior 12 mo</th>'
        '<th style="padding:2px 0;text-align:right;">Change</th></tr>'
        + "".join(rows) + '</table>'
        '<div style="font-size:11px;color:#9ca3af;margin-top:6px;">% change shown only when the '
        f'prior-year baseline ≥ {MOVER_MIN_PRIOR} papers (smaller baselines are too noisy to trust); '
        'those rows show "—". Counts are Europe PMC keyword matches.</div></div>')


def _epmc_link(pdac_query: str, term: str, start: str, end: str) -> str:
    """Europe PMC website search reproducing a tracked term's recent papers."""
    tq = f'"{term}"' if " " in term else term
    q = f'({" ".join(pdac_query.split())}) AND {tq} AND (FIRST_PDATE:[{start} TO {end}])'
    return "https://europepmc.org/search?query=" + urllib.parse.quote(q)
