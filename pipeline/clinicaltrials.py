"""
pipeline/clinicaltrials.py — ClinicalTrials.gov "translational motion" feed (Phase F).

For a Center for *Care* (Brenden-Colson, OHSU), new PDAC trial registrations and
early-phase / first-in-human activity are a more care-relevant signal than raw
publication volume. This module queries the ClinicalTrials.gov v2 REST API
(https://clinicaltrials.gov/api/v2) for PDAC-relevant trials whose record was
FIRST POSTED inside a date window and normalizes each into a compact trial record
(NCT id, title, phase, overall status, conditions, interventions, first-posted
date, sponsor, url).

Like pipeline/harvest.py this is an OFFLINE module: it only fetches, normalizes,
and caches a render-ready summary. It is NEVER imported or called from the Space
(app.py / ui.py) — ingestion is a scheduled job and the Space serves cached
analytics only. The trial query + per-source params are config, not code (the
``clinicaltrials:`` block in config/sources.yaml); this is its OWN query and does
not touch the Europe PMC literature query.

Two render-ready outputs mirror pipeline/analytics.py's keyword-movers pattern (a
count plus deep links): ``translational_motion()`` computes the summary and
``translational_motion_html()`` renders the email-safe "Translational motion"
section the weekly digest footer (and, later, the Space) can drop in.

"New" trials are those first-posted in the window. Each record also carries a
``first_seen_date`` (stamped at harvest, like the paper corpus) so that IF trials
are ever persisted to the store, "new" stays keyed on first_seen_date — consistent
with the rest of the corpus.

Run standalone (a preview / dry-run — prints normalized trials, never sends or
writes to the corpus DB):
    python -m pipeline.clinicaltrials                 # last `window_days`, preview to stdout
    python -m pipeline.clinicaltrials --days 30       # override the trailing window
    python -m pipeline.clinicaltrials --days 30 --out data/trials.json -v
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import time
import urllib.parse
from datetime import date, datetime
from pathlib import Path

from pipeline.harvest import _session, load_config, parse_window

logger = logging.getLogger("clinicaltrials")

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "sources.yaml"
DEFAULT_OUT = ROOT / "data" / "trials.json"
# Render-ready summary the digest footer / Space read (offline-cached, like
# data/analytics.json). Kept separate from analytics.json so it stays decoupled.
MOTION_CACHE = ROOT / "data" / "translational_motion.json"

REQUEST_TIMEOUT = 30  # seconds
POLITE_PAUSE = 0.2    # seconds between paginated requests (fair-use)
STUDY_URL = "https://clinicaltrials.gov/study/{nct}"

# Lean field projection: only the protocol-section modules we normalize from, so
# we don't pull the (large) full study record. These map 1:1 to blank_trial().
_FIELDS = ",".join((
    "protocolSection.identificationModule",
    "protocolSection.statusModule",
    "protocolSection.sponsorCollaboratorsModule",
    "protocolSection.conditionsModule",
    "protocolSection.designModule",
    "protocolSection.armsInterventionsModule",
))


# ---------------------------------------------------------------------------
# Normalized trial record
# ---------------------------------------------------------------------------

def blank_trial() -> dict:
    """A normalized trial record with every key present.

    ``first_posted_date`` is the trial's own "new" signal (when the registration
    went public). ``first_seen_date`` is when this pipeline first saw it — present
    for parity with the paper corpus so that, if trials are ever persisted, "new"
    stays keyed on first_seen_date.
    """
    return {
        "nct_id": None,
        "title": None,
        "phase": None,              # human-readable, e.g. "Phase 1/2"
        "phases": [],               # raw, e.g. ["PHASE1", "PHASE2"]
        "overall_status": None,     # raw, e.g. "RECRUITING"
        "conditions": [],
        "interventions": [],        # [{"type": "DRUG", "name": "..."}]
        "first_posted_date": None,  # YYYY-MM-DD (the date_field above)
        "sponsor": None,            # lead sponsor name
        "sponsor_class": None,      # INDUSTRY | NIH | OTHER | ...
        "url": None,                # https://clinicaltrials.gov/study/<nct>
        "is_first_in_human": False,
        "is_early_phase": False,    # Early Phase 1 or Phase 1
        "source": "clinicaltrials",
        "first_seen_date": date.today().isoformat(),
    }


def normalize_nct(nct) -> str | None:
    """Uppercase + strip a raw NCT id. None-safe."""
    if not nct:
        return None
    return str(nct).strip().upper() or None


_PHASE_SHORT = {"EARLY_PHASE1": "Early 1", "PHASE1": "1", "PHASE2": "2",
                "PHASE3": "3", "PHASE4": "4"}


def _phase_label(phases: list[str]) -> str:
    """["PHASE1","PHASE2"] -> "Phase 1/2"; ["EARLY_PHASE1"] -> "Early Phase 1"."""
    phases = [p for p in (phases or []) if p and p != "NA"]
    if not phases:
        return "N/A"
    if phases == ["EARLY_PHASE1"]:
        return "Early Phase 1"
    return "Phase " + "/".join(_PHASE_SHORT.get(p, p) for p in phases)


def _is_early_phase(phases: list[str]) -> bool:
    return any(p in ("EARLY_PHASE1", "PHASE1") for p in (phases or []))


def _is_first_in_human(ident: dict, conditions_module: dict) -> bool:
    """Detect first-in-human from the titles + curated keywords (where FIH is
    conventionally stated). Cheap string match — no LLM."""
    hay = " ".join([
        ident.get("briefTitle") or "",
        ident.get("officialTitle") or "",
        " ".join(conditions_module.get("keywords") or []),
    ]).lower()
    return ("first-in-human" in hay) or ("first in human" in hay)


def _study_to_record(study: dict) -> dict:
    ps = study.get("protocolSection", {}) or {}
    ident = ps.get("identificationModule", {}) or {}
    status = ps.get("statusModule", {}) or {}
    design = ps.get("designModule", {}) or {}
    conds = ps.get("conditionsModule", {}) or {}
    sponsors = ps.get("sponsorCollaboratorsModule", {}) or {}
    arms = ps.get("armsInterventionsModule", {}) or {}

    rec = blank_trial()
    rec["nct_id"] = normalize_nct(ident.get("nctId"))
    rec["title"] = (ident.get("briefTitle") or "").strip() or None
    rec["phases"] = list(design.get("phases") or [])
    rec["phase"] = _phase_label(rec["phases"])
    rec["overall_status"] = status.get("overallStatus")
    rec["first_posted_date"] = (status.get("studyFirstPostDateStruct") or {}).get("date")
    rec["conditions"] = list(conds.get("conditions") or [])
    rec["interventions"] = [
        {"type": iv.get("type"), "name": iv.get("name")}
        for iv in (arms.get("interventions") or []) if iv.get("name")
    ]
    lead = sponsors.get("leadSponsor") or {}
    rec["sponsor"] = lead.get("name")
    rec["sponsor_class"] = lead.get("class")
    rec["url"] = STUDY_URL.format(nct=rec["nct_id"]) if rec["nct_id"] else None
    rec["is_early_phase"] = _is_early_phase(rec["phases"])
    rec["is_first_in_human"] = _is_first_in_human(ident, conds)
    return rec


# ---------------------------------------------------------------------------
# Fetch (ClinicalTrials.gov v2 /studies)
# ---------------------------------------------------------------------------

def harvest_clinicaltrials(cfg: dict, date_from: str, date_to: str,
                           session) -> list[dict]:
    """Query the v2 /studies endpoint for the window and return normalized records.

    The window is applied to ``date_field`` (default StudyFirstPostDate) via an
    Essie advanced filter, so coverage is reproducible from the saved query alone.
    Paginates with pageToken/nextPageToken up to ``max_pages``.
    """
    base = cfg["base_url"]
    date_field = cfg.get("date_field", "StudyFirstPostDate")
    query_cond = " ".join(cfg["query_cond"].split())
    advanced = f"AREA[{date_field}]RANGE[{date_from},{date_to}]"
    statuses = cfg.get("overall_status") or []
    page_size = cfg.get("page_size", 1000)
    max_pages = cfg.get("max_pages", 10)
    logger.info("ClinicalTrials.gov cond=(%s) | %s", query_cond, advanced)

    records: list[dict] = []
    token: str | None = None
    for page in range(max_pages):
        params = {
            "query.cond": query_cond,
            "filter.advanced": advanced,
            "fields": _FIELDS,
            "pageSize": page_size,
            "format": "json",
        }
        if statuses:
            params["filter.overallStatus"] = "|".join(statuses)
        if page == 0:
            params["countTotal"] = "true"
        if token:
            params["pageToken"] = token
        resp = session.get(base, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        studies = data.get("studies", []) or []
        if not studies:
            break
        records.extend(_study_to_record(s) for s in studies)
        token = data.get("nextPageToken")
        logger.info("CT.gov page %d: +%d (running %d)", page + 1, len(studies), len(records))
        if not token:
            break
        time.sleep(POLITE_PAUSE)
    return records


def harvest_trials(config: dict, days: int | None = None) -> dict:
    """Run the ClinicalTrials.gov source and return the trials payload.

    Mirrors harvest.harvest_all()'s shape (window + counts + records) but for the
    single trials source. Isolated in try/except so an API hiccup yields an empty
    payload with a recorded error rather than aborting the caller.
    """
    cfg = config.get("clinicaltrials", {}) or {}
    window_days = days if days is not None else config.get("window_days", 7)
    date_from, date_to = parse_window(window_days)
    window = {"from": date_from, "to": date_to, "days": window_days}
    query_cond = " ".join((cfg.get("query_cond") or "").split())

    trials: list[dict] = []
    error: str | None = None
    if not cfg or not cfg.get("enabled", True):
        logger.info("Skipping clinicaltrials (disabled or unconfigured)")
    else:
        session = _session(config.get("contact_email", ""),
                           config.get("tool_name", "lit-agent"))
        try:
            trials = harvest_clinicaltrials(cfg, date_from, date_to, session)
        except Exception as exc:  # noqa: BLE001 — isolate the source failure
            error = f"{type(exc).__name__}: {exc}"
            logger.error("clinicaltrials failed: %s", exc)

    return {
        "harvested_at": datetime.now().isoformat(timespec="seconds"),
        "window": window,
        "query_cond": query_cond,
        "count": len(trials),
        "error": error,
        "trials": trials,
    }


# ---------------------------------------------------------------------------
# Translational-motion signal (compute + render) — mirrors analytics.keyword_movers
# ---------------------------------------------------------------------------

def _search_url(query_cond: str) -> str:
    """ClinicalTrials.gov search reproducing the PDAC condition query (the
    aggregate deep link; per-trial links go straight to each study)."""
    q = " ".join((query_cond or "").split())
    return "https://clinicaltrials.gov/search?cond=" + urllib.parse.quote(q)


def translational_motion(payload: dict, top_n: int = 6) -> dict:
    """Summarize a trials payload into the render-ready motion summary.

    A count (total, early-phase, first-in-human) plus the most notable trials
    (first-in-human, then early-phase, then most-recently posted) with deep links.
    """
    trials = payload.get("trials", []) or []
    window = payload.get("window", {})
    query_cond = payload.get("query_cond", "")

    ranked = sorted(
        trials,
        key=lambda t: (t.get("is_first_in_human", False),
                       t.get("is_early_phase", False),
                       t.get("first_posted_date") or ""),
        reverse=True,
    )
    top = [{
        "nct_id": t.get("nct_id"),
        "title": t.get("title"),
        "phase": t.get("phase"),
        "overall_status": t.get("overall_status"),
        "sponsor": t.get("sponsor"),
        "url": t.get("url"),
        "is_first_in_human": t.get("is_first_in_human", False),
        "is_early_phase": t.get("is_early_phase", False),
    } for t in ranked[:top_n]]

    # The FULL ranked list (compact) so the Space can show every new trial; the
    # email renders only `top`. Same field set as `top`.
    all_compact = [{
        "nct_id": t.get("nct_id"),
        "title": t.get("title"),
        "phase": t.get("phase"),
        "overall_status": t.get("overall_status"),
        "sponsor": t.get("sponsor"),
        "url": t.get("url"),
        "first_posted_date": t.get("first_posted_date"),
        "is_first_in_human": t.get("is_first_in_human", False),
        "is_early_phase": t.get("is_early_phase", False),
    } for t in ranked]

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "window": window,
        "n_total": len(trials),
        "n_early_phase": sum(1 for t in trials if t.get("is_early_phase")),
        "n_first_in_human": sum(1 for t in trials if t.get("is_first_in_human")),
        "search_url": _search_url(query_cond),
        "top": top,
        "all": all_compact,
    }


def _esc(s) -> str:
    return html.escape(str(s)) if s else ""


def _badge(text: str, fg: str, bg: str) -> str:
    return (f'<span style="display:inline-block;font-size:11px;color:{fg};background:{bg};'
            f'border-radius:4px;padding:1px 6px;margin-left:6px;">{_esc(text)}</span>')


def _status_label(status: str | None) -> str:
    return status.replace("_", " ").capitalize() if status else ""


def translational_motion_html(summary: dict | None, see_all_url: str | None = None) -> str:
    """Email-safe "Translational motion" section: a count + deep-linked trials.

    Shows only `top`; when more trials exist and `see_all_url` is given, appends a
    "See all on the site" link. Returns "" when there are no new trials."""
    if not summary or not summary.get("n_total"):
        return ""
    n = summary["n_total"]
    win = summary.get("window", {})
    window_txt = f'{win.get("from", "?")} → {win.get("to", "?")}'

    extras = []
    if summary.get("n_early_phase"):
        extras.append(f'{summary["n_early_phase"]} early-phase')
    if summary.get("n_first_in_human"):
        extras.append(f'{summary["n_first_in_human"]} first-in-human')
    extras_txt = (' — ' + ' · '.join(extras)) if extras else ''

    rows = []
    for t in summary.get("top", []):
        title = _esc((t.get("title") or "(untitled)")[:140])
        url = t.get("url")
        title_html = (f'<a href="{_esc(url)}" style="color:#1d4ed8;text-decoration:none;">{title}</a>'
                      if url else title)
        badges = ""
        if t.get("is_first_in_human"):
            badges += _badge("first-in-human", "#0f766e", "#ccfbf1")
        elif t.get("is_early_phase"):
            badges += _badge("early phase", "#9a3412", "#ffedd5")
        meta = " · ".join(x for x in [_esc(t.get("phase")), _esc(_status_label(t.get("overall_status"))),
                                      _esc(t.get("sponsor"))] if x)
        rows.append(
            '<tr><td style="padding:4px 0;border-bottom:1px solid #f3f4f6;">'
            f'<div style="font-size:13px;line-height:1.35;">{title_html}{badges}</div>'
            f'<div style="font-size:11px;color:#6b7280;margin-top:1px;">{meta}</div>'
            '</td></tr>')

    count_link = (f'<a href="{_esc(summary.get("search_url"))}" '
                  f'style="color:#0f766e;text-decoration:none;font-weight:600;">'
                  f'{n} new trial{"s" if n != 1 else ""}</a>')
    see_all = ""
    if see_all_url and n > len(summary.get("top", [])):
        see_all = (f'<div style="font-size:12px;margin-top:6px;">'
                   f'<a href="{_esc(see_all_url)}" style="color:#1d4ed8;text-decoration:none;'
                   f'font-weight:600;">See all {n} new trials on the site →</a></div>')
    return (
        '<div style="margin:16px 0;">'
        '<div style="font-size:13px;font-weight:600;margin-bottom:2px;">Translational motion '
        '<span style="font-weight:400;color:#6b7280;">(new PDAC trial registrations · '
        f'{_esc(window_txt)} · ClinicalTrials.gov)</span></div>'
        f'<div style="font-size:12px;color:#374151;margin-bottom:6px;">{count_link} '
        f'registered{extras_txt}.</div>'
        '<table style="border-collapse:collapse;width:100%;max-width:560px;">'
        + "".join(rows) + '</table>' + see_all + '</div>')


def translational_motion_full_html(summary: dict | None) -> str:
    """The FULL trials list for the Space 'Trends' tab — every new trial (from the
    cached `all` list), deep-linked, with phase/status/sponsor + posted date."""
    if not summary or not summary.get("n_total"):
        return "<p style='color:#6b7280;'>No new PDAC trial registrations in the latest window.</p>"
    trials = summary.get("all") or summary.get("top") or []
    win = summary.get("window", {})
    window_txt = f'{win.get("from", "?")} → {win.get("to", "?")}'
    n = summary["n_total"]

    rows = []
    for t in trials:
        title = _esc((t.get("title") or "(untitled)")[:200])
        url = t.get("url")
        title_html = (f'<a href="{_esc(url)}" style="color:#1d4ed8;text-decoration:none;">{title}</a>'
                      if url else title)
        badges = ""
        if t.get("is_first_in_human"):
            badges += _badge("first-in-human", "#0f766e", "#ccfbf1")
        elif t.get("is_early_phase"):
            badges += _badge("early phase", "#9a3412", "#ffedd5")
        meta = " · ".join(x for x in [_esc(t.get("phase")), _esc(_status_label(t.get("overall_status"))),
                                      _esc(t.get("sponsor")), _esc(t.get("first_posted_date"))] if x)
        rows.append(
            '<tr><td style="padding:6px 0;border-bottom:1px solid #f3f4f6;">'
            f'<div style="font-size:14px;line-height:1.35;">{title_html}{badges}</div>'
            f'<div style="font-size:12px;color:#6b7280;margin-top:2px;">{meta}</div></td></tr>')
    return (
        '<div style="margin:8px 0;">'
        f'<div style="font-size:13px;color:#374151;margin-bottom:6px;">{n} new PDAC trial'
        f'{"s" if n != 1 else ""} first-posted · {_esc(window_txt)} · '
        f'<a href="{_esc(summary.get("search_url"))}" style="color:#0f766e;">all on ClinicalTrials.gov →</a>'
        '</div>'
        '<table style="border-collapse:collapse;width:100%;max-width:720px;">'
        + "".join(rows) + '</table></div>')


# ---------------------------------------------------------------------------
# Cache (offline-rendered; the digest footer / Space read this read-only)
# ---------------------------------------------------------------------------

def cache_motion(payload: dict, path: str | Path = MOTION_CACHE, top_n: int = 6) -> Path:
    """Compute + write the render-ready motion summary to JSON. Returns the path."""
    summary = translational_motion(payload, top_n=top_n)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, indent=2))
    return path


def motion_html_from_cache(path: str | Path = MOTION_CACHE,
                           see_all_url: str | None = None) -> str:
    """Render the motion section from the cached summary; "" if absent/empty.

    The digest hook calls this so make_digest stays offline (cache-only, no
    network), exactly like it reads data/analytics.json. `see_all_url` (the Space)
    is forwarded so the email can link to the full list."""
    path = Path(path)
    if not path.exists():
        return ""
    try:
        return translational_motion_html(json.loads(path.read_text()), see_all_url=see_all_url)
    except Exception as exc:  # noqa: BLE001 — a bad cache must not break the digest
        logger.warning("translational-motion cache unreadable (%s)", exc)
        return ""


# ---------------------------------------------------------------------------
# CLI (preview / dry-run — mirrors pipeline/harvest.py)
# ---------------------------------------------------------------------------

def _print_preview(payload: dict) -> None:
    w = payload["window"]
    trials = payload["trials"]
    print(f"\nClinicalTrials.gov window: {w['from']} → {w['to']} ({w['days']} days)")
    print(f"New PDAC trial registrations: {payload['count']}"
          f"  (early-phase: {sum(1 for t in trials if t['is_early_phase'])},"
          f" first-in-human: {sum(1 for t in trials if t['is_first_in_human'])})")
    if payload.get("error"):
        print(f"Error: {payload['error']}")
    if not trials:
        return
    print()
    for t in trials:
        flag = "★" if t["is_first_in_human"] else ("·" if t["is_early_phase"] else " ")
        title = (t["title"] or "(untitled)")
        title = title[:70] + "…" if len(title) > 70 else title
        print(f"  {flag} {t['nct_id'] or '-':12s} {(t['phase'] or 'N/A'):14s} "
              f"{(t['overall_status'] or ''):22s} {t['first_posted_date'] or '':10s}  {title}")
    print("\n  ★ first-in-human   · early-phase (Phase 1 / Early Phase 1)")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Preview new PDAC ClinicalTrials.gov registrations (Phase F; offline, dry-run).")
    ap.add_argument("--days", type=int, default=None, help="Trailing window in days (overrides config).")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="Output JSON path for the full payload.")
    ap.add_argument("--config", type=Path, default=CONFIG_PATH, help="sources.yaml path.")
    ap.add_argument("--no-cache", action="store_true",
                    help="Skip writing the render-ready motion summary the digest reads.")
    ap.add_argument("-v", "--verbose", action="store_true", help="Verbose logging.")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = load_config(args.config)
    payload = harvest_trials(config, days=args.days)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)

    _print_preview(payload)
    print(f"\nWrote {payload['count']} trials to {args.out}")
    if not args.no_cache:
        cache_path = cache_motion(payload)
        print(f"Wrote translational-motion summary to {cache_path}")


if __name__ == "__main__":
    main()
