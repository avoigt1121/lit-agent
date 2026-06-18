"""
pipeline/harvest.py — multi-source literature harvester (Phase 0 spike).

Pulls newly published / posted PDAC literature from the three sources named in
``config/sources.yaml`` and maps every result into the normalized paper record
(CLAUDE.md §"Normalized paper record"). This module ONLY fetches and normalizes;
dedup, preprint→published linkage, scoring, and persistence happen downstream
(normalize.py / score.py / store/). It never runs inside the Space — ingestion
is an offline, scheduled job.

Sources
-------
- Europe PMC REST  : PRIMARY. All PubMed + OA full-text flags + text-mined
                     annotations. Server-side query + cursorMark pagination.
- PubMed E-utils   : secondary; authoritative MeSH terms. esearch -> efetch,
                     throttled to the configured req/s (3 anon, 10 with a key
                     via the NCBI_API_KEY env var).
- bioRxiv/medRxiv  : preprints, for recency. The details endpoint has NO text
                     search, so results are filtered client-side against the
                     PDAC keyword list in config/sources.yaml.

Run standalone to produce the spike artifact:
    python -m pipeline.harvest                 # writes data/spike.json
    python -m pipeline.harvest --days 14       # override the trailing window
    python -m pipeline.harvest --out /tmp/x.json -v
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import time
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
import yaml

logger = logging.getLogger("harvest")

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "sources.yaml"
DEFAULT_OUT = ROOT / "data" / "spike.json"

REQUEST_TIMEOUT = 30  # seconds
POLITE_PAUSE = 0.2    # seconds between paginated requests (fair-use)


# ---------------------------------------------------------------------------
# Normalized paper record
# ---------------------------------------------------------------------------

def blank_record() -> dict:
    """A normalized paper record with every key present (CLAUDE.md schema).

    Harvest fills the bibliographic + provenance fields; ``focus_areas``,
    ``relevance_score`` and ``embedding_id`` are populated later by score.py.
    ``first_seen_date`` is stamped at harvest time and is the canonical
    definition of "new" used consistently downstream.
    """
    return {
        "doi": None,
        "ids": {"pmid": None, "pmcid": None, "preprint_doi": None},
        "title": None,
        "abstract": None,
        "authors": [],
        "journal_or_server": None,
        "published_date": None,
        "first_seen_date": date.today().isoformat(),
        "is_oa": False,
        "oa_fulltext_url": None,
        "source": None,  # europepmc | pubmed | biorxiv | medrxiv
        "is_preprint": False,
        "linked_published_doi": None,
        "mesh": [],
        "annotations": {"genes": [], "diseases": []},
        "focus_areas": [],
        "relevance_score": 0.0,
        "embedding_id": None,
    }


def normalize_doi(doi) -> str | None:
    """Lowercase, strip, and drop any doi.org/scheme prefix. None-safe.

    Full canonical-DOI dedup logic lives in normalize.py (Phase 1); this is the
    minimal normalization needed so the same DOI from two sources compares equal.
    """
    if not doi:
        return None
    doi = str(doi).strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/",
                   "https://dx.doi.org/", "http://dx.doi.org/", "doi:"):
        if doi.startswith(prefix):
            doi = doi[len(prefix):]
            break
    return doi or None


_MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1)}


def _coerce_date(value) -> str | None:
    """Best-effort coercion to YYYY-MM-DD; partial dates padded to the 1st."""
    if not value:
        return None
    value = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            dt = datetime.strptime(value, fmt)
        except ValueError:
            continue
        if fmt == "%Y":
            return f"{dt.year:04d}-01-01"
        if fmt == "%Y-%m":
            return f"{dt.year:04d}-{dt.month:02d}-01"
        return dt.strftime("%Y-%m-%d")
    return value  # last resort: store raw rather than lose it


def _assemble_date(year, month, day) -> str:
    y = int(year)
    m = 1
    if month:
        m = _MONTHS.get(str(month)[:3].title()) or (int(month) if str(month).isdigit() else 1)
    d = int(day) if (day and str(day).isdigit()) else 1
    return f"{y:04d}-{m:02d}-{d:02d}"


def _session(contact_email: str, tool_name: str) -> requests.Session:
    """A session with a polite, identifying User-Agent (NCBI/EBI etiquette)."""
    s = requests.Session()
    ua = tool_name or "lit-agent"
    if contact_email:
        ua = f"{ua} (mailto:{contact_email})"
    s.headers.update({"User-Agent": ua})
    return s


RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def _retry_after(resp: requests.Response) -> float | None:
    """Seconds from a Retry-After header, if it's the numeric form."""
    val = resp.headers.get("Retry-After")
    if not val:
        return None
    try:
        return float(val)
    except ValueError:
        return None  # HTTP-date form — fall back to exponential backoff


def request_json(session: requests.Session, url: str, params: dict, *,
                 timeout: int = REQUEST_TIMEOUT, retries: int = 4,
                 backoff: float = 1.0) -> dict:
    """GET ``url`` and return parsed JSON, retrying transient failures politely.

    Retries on connection/timeout errors and on HTTP 429/5xx with exponential
    backoff (honoring a numeric ``Retry-After`` when present) — both robustness
    and etiquette: back off rather than hammer a struggling API. Raises on the
    final attempt. Used by the coverage harness and the OpenAlex client; the
    legacy spike harvesters above keep their inline calls.
    """
    for attempt in range(retries + 1):
        try:
            resp = session.get(url, params=params, timeout=timeout)
        except (requests.ConnectionError, requests.Timeout) as exc:
            if attempt >= retries:
                raise
            wait = backoff * (2 ** attempt)
            logger.warning("%s -> %s; retry %d/%d in %.1fs",
                           url, type(exc).__name__, attempt + 1, retries, wait)
            time.sleep(wait)
            continue
        if resp.status_code in RETRYABLE_STATUS and attempt < retries:
            wait = _retry_after(resp) or backoff * (2 ** attempt)
            logger.warning("%s -> HTTP %d; retry %d/%d in %.1fs",
                           url, resp.status_code, attempt + 1, retries, wait)
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError("unreachable: retry loop always returns or raises")


# ---------------------------------------------------------------------------
# Europe PMC  (PRIMARY)
# ---------------------------------------------------------------------------

def harvest_europepmc(cfg: dict, date_from: str, date_to: str,
                      session: requests.Session) -> list[dict]:
    """Query Europe PMC for the window and return normalized records.

    Uses ``resultType=core`` (abstract, authors, MeSH, OA flags) and paginates
    via cursorMark. The trailing-window restriction is injected as a date-field
    range so coverage is reproducible from the saved query alone.
    """
    base = cfg["base_url"]
    date_field = cfg.get("date_field", "FIRST_PDATE")
    query = " ".join(cfg["query"].split())
    full_query = f"({query}) AND {date_field}:[{date_from} TO {date_to}]"
    page_size = cfg.get("page_size", 1000)
    result_type = cfg.get("result_type", "core")
    max_pages = cfg.get("max_pages", 10)
    logger.info("Europe PMC query: %s", full_query)

    records: list[dict] = []
    cursor = "*"
    for page in range(max_pages):
        params = {
            "query": full_query,
            "format": "json",
            "pageSize": page_size,
            "resultType": result_type,
            "cursorMark": cursor,
        }
        resp = session.get(base, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        hits = data.get("resultList", {}).get("result", []) or []
        if not hits:
            break
        records.extend(_epmc_to_record(h) for h in hits)
        next_cursor = data.get("nextCursorMark")
        logger.info("Europe PMC page %d: +%d (running %d)", page + 1, len(hits), len(records))
        if not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor
        time.sleep(POLITE_PAUSE)
    return records


def _epmc_list(node, key):
    """Europe PMC nests list-of-X as {"xList": {"x": [...]}}; unwrap safely."""
    if isinstance(node, dict):
        inner = node.get(key)
        if isinstance(inner, list):
            return inner
        if inner is not None:
            return [inner]
    return []


def _epmc_to_record(h: dict) -> dict:
    rec = blank_record()
    rec["source"] = "europepmc"
    rec["doi"] = normalize_doi(h.get("doi"))
    rec["ids"]["pmid"] = h.get("pmid")
    rec["ids"]["pmcid"] = h.get("pmcid")
    rec["title"] = (h.get("title") or "").strip() or None
    rec["abstract"] = (h.get("abstractText") or "").strip() or None

    authors = []
    for a in _epmc_list(h.get("authorList"), "author"):
        name = a.get("fullName") or a.get("collectiveName") if isinstance(a, dict) else None
        if name:
            authors.append(name)
    if not authors and h.get("authorString"):
        authors = [h["authorString"]]
    rec["authors"] = authors

    journal = (h.get("journalInfo") or {}).get("journal") or {}
    rec["journal_or_server"] = journal.get("title") or journal.get("medlineAbbreviation")
    rec["published_date"] = _coerce_date(h.get("firstPublicationDate"))
    rec["is_oa"] = (h.get("isOpenAccess") == "Y") or (h.get("inEPMC") == "Y")
    rec["oa_fulltext_url"] = _epmc_oa_url(h)

    src = (h.get("source") or "").upper()
    pub_types = [str(p).lower() for p in _epmc_list(h.get("pubTypeList"), "pubType")]
    rec["is_preprint"] = (src == "PPR") or any("preprint" in p for p in pub_types)

    rec["mesh"] = [
        mh.get("descriptorName") for mh in _epmc_list(h.get("meshHeadingList"), "meshHeading")
        if isinstance(mh, dict) and mh.get("descriptorName")
    ]
    return rec


def _epmc_oa_url(h: dict) -> str | None:
    for f in _epmc_list(h.get("fullTextUrlList"), "fullTextUrl"):
        if not isinstance(f, dict):
            continue
        if f.get("availability") in ("Open access", "Free") and f.get("url"):
            return f["url"]
    pmcid = h.get("pmcid")
    if pmcid:
        return f"https://europepmc.org/articles/{pmcid}"
    return None


# ---------------------------------------------------------------------------
# PubMed E-utilities  (secondary / MeSH)
# ---------------------------------------------------------------------------

def harvest_pubmed(cfg: dict, date_from: str, date_to: str,
                   session: requests.Session, api_key: str | None = None) -> list[dict]:
    """esearch for PMIDs in the window, then efetch full records in batches.

    Throttled to ``requests_per_second`` (NCBI allows 3 anon / 10 with a key).
    """
    term = " ".join(cfg["term"].split())
    rps = cfg.get("requests_per_second", 3)
    if api_key and rps < 10:
        rps = 10
    delay = 1.0 / max(rps, 1)

    params = {
        "db": "pubmed",
        "term": term,
        "retmax": cfg.get("retmax", 2000),
        "retmode": "json",
        "datetype": cfg.get("date_type", "pdat"),
        "mindate": date_from.replace("-", "/"),
        "maxdate": date_to.replace("-", "/"),
    }
    if api_key:
        params["api_key"] = api_key
    r = session.get(cfg["esearch_url"], params=params, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    idlist = r.json().get("esearchresult", {}).get("idlist", []) or []
    logger.info("PubMed esearch: %d PMIDs in window", len(idlist))
    time.sleep(delay)

    records: list[dict] = []
    batch = cfg.get("efetch_batch", 200)
    for i in range(0, len(idlist), batch):
        chunk = idlist[i:i + batch]
        fparams = {"db": "pubmed", "id": ",".join(chunk), "retmode": "xml"}
        if api_key:
            fparams["api_key"] = api_key
        fr = session.get(cfg["efetch_url"], params=fparams, timeout=REQUEST_TIMEOUT)
        fr.raise_for_status()
        records.extend(_parse_pubmed_xml(fr.text))
        logger.info("PubMed efetch %d-%d: running %d", i, i + len(chunk), len(records))
        time.sleep(delay)
    return records


def _text_join(el) -> str | None:
    if el is None:
        return None
    return "".join(el.itertext()).strip() or None


def _parse_pubmed_xml(xml_text: str) -> list[dict]:
    out: list[dict] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        logger.warning("PubMed XML parse error: %s", exc)
        return out
    for art in root.findall(".//PubmedArticle"):
        rec = blank_record()
        rec["source"] = "pubmed"
        mc = art.find("MedlineCitation")
        if mc is not None:
            pmid_el = mc.find("PMID")
            if pmid_el is not None:
                rec["ids"]["pmid"] = pmid_el.text
            article = mc.find("Article")
            if article is not None:
                rec["title"] = _text_join(article.find("ArticleTitle"))
                rec["abstract"] = _pubmed_abstract(article)
                rec["authors"] = _pubmed_authors(article)
                jt = article.find("Journal/Title")
                rec["journal_or_server"] = jt.text if jt is not None else None
                rec["published_date"] = _pubmed_date(article)
            rec["mesh"] = [
                d.text for d in mc.findall("MeshHeadingList/MeshHeading/DescriptorName")
                if d.text
            ]
        for aid in art.findall(".//PubmedData/ArticleIdList/ArticleId"):
            idtype = aid.get("IdType")
            if idtype == "doi":
                rec["doi"] = normalize_doi(aid.text)
            elif idtype == "pmc":
                rec["ids"]["pmcid"] = aid.text
        # OA is approximated by PMC presence here; refined against Europe PMC's
        # isOpenAccess flag during normalization (Phase 1).
        if rec["ids"]["pmcid"]:
            rec["is_oa"] = True
            rec["oa_fulltext_url"] = (
                f"https://www.ncbi.nlm.nih.gov/pmc/articles/{rec['ids']['pmcid']}/"
            )
        out.append(rec)
    return out


def _pubmed_abstract(article) -> str | None:
    parts = []
    for ab in article.findall("Abstract/AbstractText"):
        label = ab.get("Label")
        txt = "".join(ab.itertext()).strip()
        if not txt:
            continue
        parts.append(f"{label}: {txt}" if label else txt)
    return "\n".join(parts) or None


def _pubmed_authors(article) -> list[str]:
    out = []
    for a in article.findall("AuthorList/Author"):
        last = a.findtext("LastName")
        fore = a.findtext("ForeName") or a.findtext("Initials")
        coll = a.findtext("CollectiveName")
        if last:
            out.append(f"{last} {fore}".strip() if fore else last)
        elif coll:
            out.append(coll)
    return out


def _pubmed_date(article) -> str | None:
    for path in ("ArticleDate", "Journal/JournalIssue/PubDate"):
        el = article.find(path)
        if el is None:
            continue
        year = el.findtext("Year")
        if year:
            return _assemble_date(year, el.findtext("Month"), el.findtext("Day"))
    return None


# ---------------------------------------------------------------------------
# bioRxiv / medRxiv  (preprints, for recency)
# ---------------------------------------------------------------------------

def harvest_preprints(cfg: dict, date_from: str, date_to: str,
                      session: requests.Session) -> list[dict]:
    """Page the details endpoint for each preprint server and keyword-filter.

    The bioRxiv/medRxiv API has no server-side text search, so it returns every
    preprint posted in the window; we keep only those whose title/abstract match
    the PDAC keyword list (client-side substring, case-insensitive).
    """
    servers = cfg.get("servers", ["biorxiv", "medrxiv"])
    keywords = [k.lower() for k in cfg.get("keywords", [])]
    max_pages = cfg.get("max_pages", 50)
    tmpl = cfg["base_url_tmpl"]

    out: list[dict] = []
    for server in servers:
        cursor = 0
        kept_here = 0
        for page in range(max_pages):
            url = tmpl.format(server=server, date_from=date_from,
                              date_to=date_to, cursor=cursor)
            r = session.get(url, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            data = r.json()
            coll = data.get("collection", []) or []
            if not coll:
                break
            for item in coll:
                if _preprint_matches(item, keywords):
                    out.append(_preprint_to_record(item, server))
                    kept_here += 1
            # Advance by the actual page length — the API's page size is not
            # guaranteed (observed 30, not the documented 100); incrementing by a
            # fixed guess would silently skip records.
            total = _preprint_total(data)
            cursor += len(coll)
            if total is None or cursor >= total:
                break
            time.sleep(POLITE_PAUSE)
        logger.info("%s: kept %d PDAC-matching preprints in window", server, kept_here)
    return out


def _preprint_matches(item: dict, keywords: list[str]) -> bool:
    if not keywords:
        return True
    hay = f"{item.get('title', '')} {item.get('abstract', '')}".lower()
    return any(k in hay for k in keywords)


def _preprint_total(data: dict):
    msgs = data.get("messages")
    if isinstance(msgs, list) and msgs:
        try:
            return int(msgs[0].get("total"))
        except (TypeError, ValueError):
            return None
    return None


def _preprint_to_record(item: dict, server: str) -> dict:
    rec = blank_record()
    rec["source"] = server  # biorxiv | medrxiv
    rec["doi"] = normalize_doi(item.get("doi"))
    rec["title"] = (item.get("title") or "").strip() or None
    rec["abstract"] = (item.get("abstract") or "").strip() or None
    authors = item.get("authors") or ""
    rec["authors"] = [a.strip() for a in authors.split(";") if a.strip()]
    rec["journal_or_server"] = server
    rec["published_date"] = _coerce_date(item.get("date"))
    rec["is_oa"] = True
    rec["is_preprint"] = True
    if rec["doi"]:
        rec["ids"]["preprint_doi"] = rec["doi"]
        version = item.get("version", "1")
        rec["oa_fulltext_url"] = (
            f"https://www.{server}.org/content/{rec['doi']}v{version}.full"
        )
    # 'published' carries the published-version DOI once a preprint is published
    # (preprint→published linkage is finalized in normalize.py).
    published = item.get("published")
    if published and str(published).upper() not in ("NA", "", "NONE"):
        rec["linked_published_doi"] = normalize_doi(published)
    return rec


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def parse_window(window_days: int, today: date | None = None) -> tuple[str, str]:
    today = today or date.today()
    start = today - timedelta(days=window_days)
    return start.isoformat(), today.isoformat()


def load_config(path: Path = CONFIG_PATH) -> dict:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def harvest_all(config: dict, days: int | None = None) -> dict:
    """Run every enabled source and return the combined spike payload.

    Each source is isolated in try/except so one failing API still yields a
    partial spike (and a recorded error) rather than aborting the whole run.
    """
    window_days = days if days is not None else config.get("window_days", 7)
    date_from, date_to = parse_window(window_days)
    session = _session(config.get("contact_email", ""), config.get("tool_name", "lit-agent"))
    api_key = os.environ.get("NCBI_API_KEY")

    records: list[dict] = []
    errors: dict[str, str] = {}

    sources = [
        ("europepmc", config.get("europepmc", {}),
         lambda c: harvest_europepmc(c, date_from, date_to, session)),
        ("pubmed", config.get("pubmed", {}),
         lambda c: harvest_pubmed(c, date_from, date_to, session, api_key)),
        ("preprints", config.get("preprints", {}),
         lambda c: harvest_preprints(c, date_from, date_to, session)),
    ]
    for name, cfg, fn in sources:
        if not cfg or not cfg.get("enabled", True):
            logger.info("Skipping %s (disabled)", name)
            continue
        try:
            got = fn(cfg)
            records.extend(got)
            logger.info("%s: %d records", name, len(got))
        except Exception as exc:  # noqa: BLE001 — isolate per-source failures
            errors[name] = f"{type(exc).__name__}: {exc}"
            logger.error("%s failed: %s", name, exc)

    counts = Counter(r["source"] for r in records)
    return {
        "harvested_at": datetime.now().isoformat(timespec="seconds"),
        "window": {"from": date_from, "to": date_to, "days": window_days},
        "counts": {**dict(counts), "total": len(records)},
        "errors": errors,
        "records": records,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Harvest PDAC literature (Phase 0 spike).")
    ap.add_argument("--days", type=int, default=None, help="Trailing window in days (overrides config).")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="Output JSON path.")
    ap.add_argument("--config", type=Path, default=CONFIG_PATH, help="sources.yaml path.")
    ap.add_argument("-v", "--verbose", action="store_true", help="Verbose logging.")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = load_config(args.config)
    payload = harvest_all(config, days=args.days)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)

    w = payload["window"]
    print(f"\nHarvest window: {w['from']} → {w['to']} ({w['days']} days)")
    print("Counts per source:")
    for src in ("europepmc", "pubmed", "biorxiv", "medrxiv"):
        print(f"  {src:12s} {payload['counts'].get(src, 0)}")
    print(f"  {'TOTAL':12s} {payload['counts']['total']}")
    if payload["errors"]:
        print("Errors:")
        for src, err in payload["errors"].items():
            print(f"  {src}: {err}")
    print(f"\nWrote {payload['counts']['total']} records to {args.out}")


if __name__ == "__main__":
    main()
