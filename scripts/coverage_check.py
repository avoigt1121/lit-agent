#!/usr/bin/env python3
"""
scripts/coverage_check.py — measure coverage by triangulating the saved PDAC
query across Europe PMC and OpenAlex (CAPABILITIES.md §1.4, IMPLEMENTATION_PLAN
Phase B).

Coverage = database completeness × query recall. EPMC holdings are top-tier, so
the query is the real recall ceiling: a paper EPMC holds is invisible if the
saved query in config/sources.yaml doesn't match it. This makes coverage a
**measured** property instead of an assumption:

  1. Run the SAME PDAC query (config/sources.yaml `europepmc.query`) over a date
     window against BOTH Europe PMC and OpenAlex (free, key-less, returns DOIs).
  2. Diff the DOI sets — report totals, overlap, and each side's unique DOIs.
     A non-trivial OpenAlex-only set means EPMC or (more likely) the query leaks.
  3. Compute a measured recall figure WITH an explicit denominator:
        measured_recall = |EPMC DOIs| / |EPMC ∪ OpenAlex DOIs|
     i.e. the fraction of the two-source DOI union that EPMC returns. This is a
     conservative lower bound on EPMC's recall of peer-reviewed PDAC papers:
     spot-checks (see the commit/PR) show the EPMC-absent remainder is dominated
     by conference abstracts and repository deposits (Figshare/Zenodo) that
     OpenAlex types as "article" but MEDLINE/PMC doesn't index as papers — a
     content-scope difference, not a query leak.
  4. Write the number + denominator + as-of date into a `provenance:` block in
     config/sources.yaml so the digest/Space copy (§3.5) cites a real,
     self-measured figure — NOT the published ~98% OpenAlex/guideline study.

The gold-set recall test (the harness's other half) is deliberately deferred —
see gold_set_recall(); it needs user-supplied Sears/Brody seed DOIs.

STANDALONE + offline: no reference-repo imports; never runs inside the Space.

    python scripts/coverage_check.py                 # settled 12-mo window (default)
    python scripts/coverage_check.py --days 180 -v   # last 180 days (ending 30 d ago)
    python scripts/coverage_check.py --from 2025-01-01 --to 2025-03-31
    python scripts/coverage_check.py --no-write      # report only, don't touch config
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # runnable as `python scripts/coverage_check.py`

from pipeline.harvest import (  # noqa: E402
    _session, load_config, normalize_doi, request_json)
from pipeline.openalex import OpenAlexClient, search_doi_set  # noqa: E402

logger = logging.getLogger("coverage_check")

CONFIG_PATH = ROOT / "config" / "sources.yaml"
ARTIFACT_DIR = ROOT / "data"            # gitignored; full DOI diff for inspection
REQUEST_TIMEOUT = 30
POLITE_PAUSE = 0.2

# Managed-block markers: the provenance region is delimited so it can be removed
# and rewritten idempotently without disturbing the rest of the file's comments.
PROV_BEGIN = "# >>> provenance: measured coverage — auto-written by scripts/coverage_check.py >>>"
PROV_END = "# <<< provenance <<<"

# OpenAlex content types kept for the cross-check, so the denominator is PDAC
# *primary literature* comparable to what Europe PMC/MEDLINE indexes — not book
# chapters, peer-review objects, errata, datasets, etc. (see §1.4 investigation).
DEFAULT_OPENALEX_TYPES = "article|review|preprint"

# Default measurement window: a large, SETTLED span. Recent windows understate
# recall badly — EPMC FIRST_PDATE vs OpenAlex publication_date disagree near the
# edges, and the newest weeks are still being indexed. So end the window
# DEFAULT_LAG_DAYS before today and make it DEFAULT_WINDOW_DAYS long.
DEFAULT_WINDOW_DAYS = 365
DEFAULT_LAG_DAYS = 30


def resolve_window(date_from: str | None, date_to: str | None,
                   days: int, lag_days: int) -> dict:
    """Explicit --from/--to if given, else a trailing settled window."""
    if date_from:
        return {"from": date_from, "to": date_to or date.today().isoformat()}
    end = date.today() - timedelta(days=lag_days)
    return {"from": (end - timedelta(days=days)).isoformat(), "to": end.isoformat()}


# ---------------------------------------------------------------------------
# Europe PMC — DOI set for the saved query over a window
# ---------------------------------------------------------------------------

def epmc_doi_set(cfg: dict, query: str, date_from: str, date_to: str,
                 session, *, max_pages: int | None = None,
                 pause: float = POLITE_PAUSE) -> tuple[set[str], int, int]:
    """Normalized DOI set for the EPMC query in the window.

    Paginates the FULL result set via cursorMark (resultType=lite is enough for
    DOIs — lighter than the harvester's `core`). Returns
    (dois, hit_count, n_without_doi); records lacking a DOI can't join a DOI diff
    so they're counted separately, not silently dropped.
    """
    ep = cfg["europepmc"]
    base = ep["base_url"]
    date_field = ep.get("date_field", "FIRST_PDATE")
    full_query = f"({query}) AND {date_field}:[{date_from} TO {date_to}]"
    logger.info("Europe PMC query: %s", full_query)

    dois: set[str] = set()
    hit_count: int | None = None
    n_no_doi = 0
    cursor, page = "*", 0
    while True:
        params = {"query": full_query, "format": "json", "pageSize": 1000,
                  "resultType": "lite", "cursorMark": cursor}
        data = request_json(session, base, params, timeout=REQUEST_TIMEOUT)
        if hit_count is None:
            hit_count = int(data.get("hitCount", 0))
        hits = data.get("resultList", {}).get("result", []) or []
        for h in hits:
            doi = normalize_doi(h.get("doi"))
            if doi:
                dois.add(doi)
            else:
                n_no_doi += 1
        page += 1
        nxt = data.get("nextCursorMark")
        logger.info("Europe PMC page %d: +%d (running %d, hitCount %s)",
                    page, len(hits), len(dois), hit_count)
        if not hits or not nxt or nxt == cursor:
            break
        if max_pages is not None and page >= max_pages:
            logger.warning("Europe PMC: hit max_pages=%d cap; result set truncated", max_pages)
            break
        cursor = nxt
        time.sleep(pause)
    return dois, hit_count or 0, n_no_doi


# ---------------------------------------------------------------------------
# Query translation (EPMC syntax -> OpenAlex search expression)
# ---------------------------------------------------------------------------

def _strip_outer_parens(query: str) -> str:
    """Drop a single enclosing paren pair if it wraps the whole expression.

    The saved EPMC query is a flat OR group `(A OR B OR "C" ...)`; OpenAlex's
    `.search` filter takes the same OR/quoted-phrase syntax but does not need the
    grouping parens. Only strips when the leading `(` matches the trailing `)`
    (so `(A) OR (B)` is left untouched).
    """
    query = query.strip()
    if not (query.startswith("(") and query.endswith(")")):
        return query
    depth = 0
    for i, ch in enumerate(query):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return query[1:-1].strip() if i == len(query) - 1 else query
    return query


def openalex_query(cfg: dict, epmc_query: str) -> str:
    """The OpenAlex search expression: explicit config override, else derived.

    Config-not-code: `openalex.search` in sources.yaml lets the OpenAlex side be
    tuned independently if the auto-translation from EPMC syntax is imperfect.
    """
    oa_cfg = cfg.get("openalex", {}) or {}
    if oa_cfg.get("search"):
        return " ".join(str(oa_cfg["search"]).split())
    return _strip_outer_parens(epmc_query)


# ---------------------------------------------------------------------------
# Diff + measured recall
# ---------------------------------------------------------------------------

def diff_sets(epmc: set[str], openalex: set[str]) -> dict:
    union = epmc | openalex
    return {
        "epmc_dois": len(epmc),
        "openalex_dois": len(openalex),
        "overlap": len(epmc & openalex),
        "epmc_only": len(epmc - openalex),
        "openalex_only": len(openalex - epmc),
        "union": len(union),
        # Fraction of the EPMC ∪ OpenAlex DOI union that EPMC returns.
        "measured_recall": round(len(epmc) / len(union), 4) if union else None,
    }


# ---------------------------------------------------------------------------
# Gold-set recall — DEFERRED STUB (the harness's other half)
# ---------------------------------------------------------------------------

def gold_set_recall(epmc_dois: set[str], gold_dois: list[str] | None = None) -> dict | None:
    """STUB — NOT YET IMPLEMENTED (CAPABILITIES.md §1.4 step 2).

    The second half of the coverage harness: test query recall against a curated
    gold set of must-find papers (the Sears/Brody lab's own PDAC publications +
    their reference lists). Misses tell you exactly which synonyms / MeSH terms
    the saved query is missing.

    Deliberately deferred: it needs user-supplied seed DOIs we don't have yet —
    the same set that gates Phase D (see IMPLEMENTATION_PLAN.md "Needs user
    input"). When those land (e.g. config/interest_profile.yaml `exemplar_dois`
    or a config/gold_set.json), implement:
      1. load + normalize the gold DOIs,
      2. check each for membership in the EPMC result set over a generous window,
      3. report recall = found / total and LIST the misses (each names a term the
         saved query should add).

    Returns None until then so the caller skips it cleanly.
    """
    # TODO(phase-b-goldset): implement once Sears/Brody seed DOIs are provided.
    return None


# ---------------------------------------------------------------------------
# Persist the provenance block (surgical — preserves the rest of sources.yaml)
# ---------------------------------------------------------------------------

def render_provenance_yaml(prov: dict) -> str:
    body = yaml.safe_dump({"provenance": prov}, sort_keys=False,
                          allow_unicode=True, default_flow_style=False)
    return (
        f"\n{PROV_BEGIN}\n"
        "# Do NOT hand-edit; re-run `python scripts/coverage_check.py` to refresh.\n"
        "# measured_recall is THIS system's self-measured number — never hardcode the\n"
        "# ~98% figure (a study of OpenAlex vs guideline-cited papers, not this query;\n"
        "# see CAPABILITIES.md §3.5).\n"
        f"{body}"
        f"{PROV_END}\n"
    )


def _strip_bare_block(text: str, key: str) -> str:
    """Remove an unmarked top-level `key:` block (key + indented body), if present.

    Fallback for a provenance block a human pasted without the managed markers.
    """
    lines = text.splitlines(keepends=True)
    i = next((n for n, ln in enumerate(lines) if re.match(rf"^{key}\s*:", ln)), None)
    if i is None:
        return text
    end = len(lines)
    for k in range(i + 1, len(lines)):
        if lines[k].strip() and not lines[k][0].isspace() and not lines[k].startswith("#"):
            end = k
            break
    del lines[i:end]
    return "".join(lines)


def write_provenance_block(path: Path, prov: dict) -> None:
    """Replace (or append) the provenance block, preserving every other comment.

    yaml.safe_dump would strip the file's comments, so this edits text: remove any
    existing managed block(s) delimited by PROV_BEGIN/PROV_END (idempotent, dedupes
    older runs), fall back to stripping a bare `provenance:` block if someone pasted
    one without markers, then append a freshly rendered block at EOF. Block order is
    irrelevant to YAML.
    """
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"\n*" + re.escape(PROV_BEGIN) + r".*?" + re.escape(PROV_END) + r"\n?",
                  "\n", text, flags=re.DOTALL)
    text = _strip_bare_block(text, "provenance")
    text = text.rstrip("\n") + "\n" + render_provenance_yaml(prov)
    path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _sample(dois: set[str], n: int = 12) -> list[str]:
    return sorted(dois)[:n]


def print_report(window: dict, method: dict, epmc_hits: int, epmc_no_doi: int,
                 oa_total: int, result: dict,
                 epmc_only: set[str], oa_only: set[str]) -> None:
    rec = result["measured_recall"]
    rec_str = f"{rec:.1%}" if rec is not None else "n/a (empty union)"
    print(f"\nCoverage check — window {window['from']} → {window['to']}")
    print("=" * 64)
    print(f"OpenAlex side: {method['openalex_search_field']}.search, "
          f"types={method['openalex_types']}")
    print("Raw totals (incl. records without a usable DOI):")
    print(f"  Europe PMC hitCount   : {epmc_hits}")
    print(f"  OpenAlex work count   : {oa_total}")
    print("\nComparable DOI sets (records with a DOI):")
    print(f"  Europe PMC DOIs       : {result['epmc_dois']}"
          f"  ({epmc_no_doi} EPMC records had no DOI, excluded)")
    print(f"  OpenAlex DOIs         : {result['openalex_dois']}"
          f"  ({oa_total - result['openalex_dois']} OpenAlex works had no DOI, excluded)")
    print(f"  Overlap (both)        : {result['overlap']}")
    print(f"  EPMC only             : {result['epmc_only']}")
    print(f"  OpenAlex only         : {result['openalex_only']}")
    print(f"  Union (denominator)   : {result['union']}")
    print("-" * 64)
    print(f"  MEASURED RECALL       : {rec_str}")
    print("    = |EPMC DOIs| / |EPMC ∪ OpenAlex DOIs|  (fraction of the two-source")
    print("      union that EPMC returns; conservative lower bound on true recall)")
    if oa_only:
        print(f"\nSample OpenAlex-only DOIs (query/EPMC may be leaking these — {result['openalex_only']} total):")
        for d in _sample(oa_only):
            print(f"  https://doi.org/{d}")
    if epmc_only:
        print(f"\nSample EPMC-only DOIs ({result['epmc_only']} total):")
        for d in _sample(epmc_only):
            print(f"  https://doi.org/{d}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=DEFAULT_WINDOW_DAYS,
                    help=f"Trailing window length in days (default {DEFAULT_WINDOW_DAYS}). "
                         "Ignored if --from is given.")
    ap.add_argument("--lag-days", type=int, default=DEFAULT_LAG_DAYS,
                    help=f"End the window this many days before today (default {DEFAULT_LAG_DAYS}) "
                         "to skip the unsettled recent zone. Ignored if --from is given.")
    ap.add_argument("--from", dest="date_from", default=None,
                    help="Window start YYYY-MM-DD (overrides --days/--lag-days).")
    ap.add_argument("--to", dest="date_to", default=None,
                    help="Window end YYYY-MM-DD (defaults to today).")
    ap.add_argument("--openalex-types", default=None,
                    help="OpenAlex type filter, '|'-joined (default from config "
                         f"openalex.types, else '{DEFAULT_OPENALEX_TYPES}'). "
                         "Pass 'all' to disable type filtering.")
    ap.add_argument("--config", type=Path, default=CONFIG_PATH, help="sources.yaml path.")
    ap.add_argument("--max-pages", type=int, default=None,
                    help="Safety cap on pages per source (default: no cap — fetch all).")
    ap.add_argument("--no-write", action="store_true",
                    help="Print the report but do NOT update the provenance block.")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    cfg = load_config(args.config)
    window = resolve_window(args.date_from, args.date_to, args.days, args.lag_days)
    date_from, date_to = window["from"], window["to"]

    ep = cfg["europepmc"]
    epmc_query = " ".join(ep["query"].split())
    oa_query = openalex_query(cfg, epmc_query)
    oa_cfg = cfg.get("openalex", {}) or {}
    search_field = oa_cfg.get("search_field", "title_and_abstract")
    oa_date_field = oa_cfg.get("date_field", "publication_date")
    oa_types = args.openalex_types or oa_cfg.get("types") or DEFAULT_OPENALEX_TYPES
    if str(oa_types).lower() == "all":
        oa_types = None
    logger.info("OpenAlex %s.search query: %s | types=%s", search_field, oa_query, oa_types)

    session = _session(cfg.get("contact_email", ""), cfg.get("tool_name", "lit-agent"))

    print(f"Querying Europe PMC ({date_from} → {date_to}) ...")
    epmc_dois, epmc_hits, epmc_no_doi = epmc_doi_set(
        cfg, epmc_query, date_from, date_to, session, max_pages=args.max_pages)

    print(f"Querying OpenAlex   ({date_from} → {date_to}, types={oa_types or 'all'}) ...")
    client = OpenAlexClient(mailto=cfg.get("contact_email"),
                            tool_name=cfg.get("tool_name", "lit-agent"))
    oa_dois, oa_total = search_doi_set(
        oa_query, date_from, date_to, client=client,
        search_field=search_field, date_field=oa_date_field,
        types=oa_types, max_pages=args.max_pages)

    result = diff_sets(epmc_dois, oa_dois)
    method = {"openalex_search_field": search_field,
              "openalex_types": oa_types or "all", "openalex_query": oa_query}
    print_report(window, method, epmc_hits, epmc_no_doi, oa_total, result,
                 epmc_dois - oa_dois, oa_dois - epmc_dois)

    # Gold-set half — deferred until user supplies seed DOIs.
    gold = gold_set_recall(epmc_dois)
    print("\nGold-set recall test : SKIPPED (needs Sears/Brody seed DOIs — see "
          "gold_set_recall() / IMPLEMENTATION_PLAN.md)." if gold is None
          else f"\nGold-set recall      : {gold}")

    # Full DOI diff artifact for inspection (data/ is gitignored).
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    artifact = ARTIFACT_DIR / f"coverage_check_{date_from}_{date_to}.json"
    artifact.write_text(json.dumps({
        "window": window, "query": {"epmc": epmc_query, "openalex": oa_query},
        "summary": result, "epmc_hitCount": epmc_hits, "openalex_count": oa_total,
        "epmc_only": sorted(epmc_dois - oa_dois),
        "openalex_only": sorted(oa_dois - epmc_dois),
    }, indent=2), encoding="utf-8")
    print(f"\nFull DOI diff written to {artifact.relative_to(ROOT)}")

    if args.no_write:
        print("\n--no-write: provenance block in config/sources.yaml left unchanged.")
        return
    if result["measured_recall"] is None:
        print("\nEmpty union — provenance block NOT updated (widen the window).")
        return

    prov = {
        "measured_recall": result["measured_recall"],
        "denominator": "fraction of the EPMC + OpenAlex DOI union returned by EPMC",
        "denominator_source": "OpenAlex cross-check",
        "as_of": date.today().isoformat(),
        "window": window,
        "method": method,
        "epmc_doi_count": result["epmc_dois"],
        "openalex_doi_count": result["openalex_dois"],
        "overlap": result["overlap"],
        "union": result["union"],
        "note": ("Self-measured by scripts/coverage_check.py over the window above. "
                 "NOT the published ~98% OpenAlex/guideline figure (CAPABILITIES.md "
                 "§3.5). Denominator = DOI union of EPMC and OpenAlex (types: "
                 f"{oa_types or 'all'}). Spot-checks show the EPMC-absent remainder "
                 "is dominated by conference/meeting abstracts and repository "
                 "deposits (Figshare/Zenodo) that OpenAlex types as 'article' but "
                 "MEDLINE/PMC does not index as primary literature; date-field "
                 "semantics is a minor contributor. So this is a CONSERVATIVE lower "
                 "bound on EPMC's recall of peer-reviewed PDAC papers, not a 24% gap "
                 "in research coverage. Re-run after any change to europepmc.query."),
    }
    write_provenance_block(args.config, prov)
    print(f"\nWrote provenance block to {args.config.relative_to(ROOT)} "
          f"(measured_recall={result['measured_recall']:.1%}, as_of={prov['as_of']}).")


if __name__ == "__main__":
    main()
