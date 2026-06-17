"""
pipeline/normalize.py — dedup + preprint→published linkage (Phase 1).

harvest.py already emits the normalized record shape; this stage collapses the
same paper appearing across sources (bioRxiv → medRxiv → PubMed → Europe PMC with
drifting titles) into one canonical record, so the digest doesn't double-count
and analytics don't inflate (CLAUDE.md §9.2). Three passes:

  1. exact merge on a canonical id (normalized DOI, else pmid/preprint_doi, else
     a stable synthetic id from the title);
  2. explicit preprint→published linkage via the preprint's linked_published_doi;
  3. conservative fuzzy-title dedup (rapidfuzz) for records without a shared DOI,
     corroborated by first-author or year so near-identical titles of *different*
     papers are not wrongly merged.

"New" is defined by first_seen_date, stamped at harvest and preserved here (a
merge keeps the earliest first_seen_date).

Run standalone to see dedup stats:
    python -m pipeline.normalize data/spike.json
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

from rapidfuzz import fuzz

# Source preference when merging duplicates: Europe PMC (richest: OA flags,
# annotations) > PubMed (MeSH) > preprint servers.
SOURCE_PRIORITY = {"europepmc": 0, "pubmed": 1, "biorxiv": 2, "medrxiv": 3}

FUZZY_TITLE_THRESHOLD = 95   # token_set_ratio; high to avoid false merges
_SCALARS = ("title", "abstract", "journal_or_server", "published_date",
            "oa_fulltext_url", "linked_published_doi")


def _norm_title(title: str | None) -> str:
    if not title:
        return ""
    t = re.sub(r"[^a-z0-9 ]+", " ", title.lower())
    return re.sub(r"\s+", " ", t).strip()


def _first_author_last(rec: dict) -> str:
    authors = rec.get("authors") or []
    if not authors:
        return ""
    # authors are "Last F" (PubMed/EPMC) or "First Last" (preprints); take the
    # token most likely to be a surname for a loose corroboration check.
    first = authors[0].replace(",", " ").split()
    return (first[0] if first else "").lower()


def _year(rec: dict) -> str:
    return (rec.get("published_date") or "")[:4]


def assign_paper_id(rec: dict) -> str:
    """Stable primary key: DOI if present, else pmid/preprint_doi, else a title hash."""
    if rec.get("doi"):
        return rec["doi"]
    ids = rec.get("ids") or {}
    if ids.get("pmid"):
        return f"pmid:{ids['pmid']}"
    if ids.get("preprint_doi"):
        return ids["preprint_doi"]
    nt = _norm_title(rec.get("title"))
    digest = hashlib.sha1(nt.encode()).hexdigest()[:16] if nt else "unknown"
    return f"title:{digest}"


def _union(a, b) -> list:
    seen, out = set(), []
    for x in (a or []) + (b or []):
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _merge_group(recs: list[dict]) -> dict:
    """Merge records sharing a canonical id into one, preferring richer sources."""
    recs = sorted(recs, key=lambda r: SOURCE_PRIORITY.get(r.get("source"), 9))
    out = json.loads(json.dumps(recs[0]))  # deep copy of highest-priority record
    for r in recs[1:]:
        for f in _SCALARS:
            if not out.get(f) and r.get(f):
                out[f] = r[f]
        # abstract: keep the longest (most complete)
        if (r.get("abstract") or "") and len(r["abstract"]) > len(out.get("abstract") or ""):
            out["abstract"] = r["abstract"]
        # authors: keep the most complete list rather than union (formats differ)
        if len(r.get("authors") or []) > len(out.get("authors") or []):
            out["authors"] = r["authors"]
        out["is_oa"] = bool(out.get("is_oa")) or bool(r.get("is_oa"))
        for k in ("pmid", "pmcid", "preprint_doi"):
            if not out["ids"].get(k) and (r.get("ids") or {}).get(k):
                out["ids"][k] = r["ids"][k]
        out["mesh"] = _union(out.get("mesh"), r.get("mesh"))
        out["annotations"]["genes"] = _union(
            out["annotations"].get("genes"), (r.get("annotations") or {}).get("genes"))
        out["annotations"]["diseases"] = _union(
            out["annotations"].get("diseases"), (r.get("annotations") or {}).get("diseases"))
        out["linked_published_doi"] = out.get("linked_published_doi") or r.get("linked_published_doi")
        if r.get("first_seen_date"):
            out["first_seen_date"] = min(out["first_seen_date"], r["first_seen_date"])
    return out


def _fold_preprint_into_published(pub: dict, pre: dict) -> None:
    """Published version is canonical; retain the preprint's DOI + earliest sighting."""
    pub["ids"]["preprint_doi"] = (pub["ids"].get("preprint_doi")
                                  or pre.get("doi") or (pre.get("ids") or {}).get("preprint_doi"))
    pub["first_seen_date"] = min(pub.get("first_seen_date", "9999"),
                                 pre.get("first_seen_date", "9999"))
    if not pub.get("abstract") and pre.get("abstract"):
        pub["abstract"] = pre["abstract"]


def normalize_records(records: list[dict]) -> dict:
    """Return {'records': deduped, 'stats': {...}}. Input is harvested records."""
    n_in = len(records)

    # Pass 1 — exact merge on canonical id
    groups: dict[str, list[dict]] = {}
    for r in records:
        r["paper_id"] = assign_paper_id(r)
        groups.setdefault(r["paper_id"], []).append(r)
    items = [_merge_group(g) for g in groups.values()]
    n_after_exact = len(items)

    # Pass 2 — explicit preprint → published linkage
    by_doi = {r["doi"]: r for r in items if r.get("doi") and not r.get("is_preprint")}
    survivors, linked = [], 0
    for r in items:
        tgt = r.get("linked_published_doi")
        if r.get("is_preprint") and tgt and tgt in by_doi and by_doi[tgt] is not r:
            _fold_preprint_into_published(by_doi[tgt], r)
            linked += 1
            continue
        survivors.append(r)
    items = survivors

    # Pass 3 — conservative fuzzy-title dedup (catches no-DOI dups and
    # preprint/published pairs lacking an explicit link)
    accepted: list[dict] = []
    fuzzy_merged = 0
    for r in items:
        nt = _norm_title(r.get("title"))
        match = None
        if nt:
            for a in accepted:
                if fuzz.token_set_ratio(nt, a["_nt"]) >= FUZZY_TITLE_THRESHOLD and _corroborated(r, a):
                    match = a
                    break
        if match is not None:
            _merge_fuzzy(match, r)
            fuzzy_merged += 1
        else:
            r["_nt"] = nt
            accepted.append(r)
    for a in accepted:
        a.pop("_nt", None)

    return {
        "records": accepted,
        "stats": {
            "harvested": n_in,
            "after_exact_merge": n_after_exact,
            "exact_dups_collapsed": n_in - n_after_exact,
            "preprints_linked_to_published": linked,
            "fuzzy_dups_collapsed": fuzzy_merged,
            "unique": len(accepted),
        },
    }


def _corroborated(r: dict, a: dict) -> bool:
    """A 95+ title match alone is risky; require author or year agreement."""
    ra, aa = _first_author_last(r), _first_author_last(a)
    if ra and aa and ra == aa:
        return True
    ry, ay = _year(r), _year(a)
    if ry and ay and abs(int(ry) - int(ay)) <= 1:  # preprint/published may differ by a year
        return True
    return False


def _merge_fuzzy(keeper: dict, other: dict) -> None:
    """Merge `other` into `keeper`, preferring the published version as canonical."""
    if keeper.get("is_preprint") and not other.get("is_preprint"):
        # swap identities: keep the published record's fields as canonical
        keeper_copy = dict(keeper)
        keeper.clear()
        keeper.update(other)
        keeper["_nt"] = keeper_copy.get("_nt", _norm_title(other.get("title")))
        _fold_preprint_into_published(keeper, keeper_copy)
        return
    if other.get("is_preprint") and not keeper.get("is_preprint"):
        _fold_preprint_into_published(keeper, other)
        return
    # same type: keep the higher-priority source, fold ids + earliest sighting
    for k in ("pmid", "pmcid", "preprint_doi"):
        if not keeper["ids"].get(k) and (other.get("ids") or {}).get(k):
            keeper["ids"][k] = other["ids"][k]
    keeper["first_seen_date"] = min(keeper.get("first_seen_date", "9999"),
                                    other.get("first_seen_date", "9999"))


def main() -> None:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data/spike.json")
    payload = json.loads(path.read_text())
    records = payload.get("records", payload) if isinstance(payload, dict) else payload
    result = normalize_records(records)
    print(f"Normalized {path}:")
    for k, v in result["stats"].items():
        print(f"  {k:32s} {v}")


if __name__ == "__main__":
    main()
