"""
pipeline/mentions.py — literal entity-mention index (ADR-0004, layer 1).

DISTINCT from focus-area classification. A focus-area tag (``topic_tags`` /
``focus_areas``) is a CLASSIFIER label — today the ``myc`` tag means the broad
"Oncogenic drivers & gene regulation" area, so "which papers MENTION MYC?" cannot
be answered from it. This module builds a literal index over each paper's
title+abstract (and OA full text where a cached snippet exists) so a literal-term
query is possible.

Two evidence methods, both grounded (no inference):
  - ``literal_scan``    — word-boundary match of a curated gene/entity lexicon
    (config ``relationships.mentions``) against title+abstract. Case-SENSITIVE
    for short all-caps symbols (MYC, KRAS) to avoid English-word false positives
    (MAX, ARE, CAR); case-insensitive for longer terms.
  - ``epmc_annotation`` — Europe PMC text-mined annotations
    (``annotations.genes`` / ``annotations.diseases`` on the normalized record,
    when present). Opt-in live fetch via ``use_epmc_annotations``.

OFFLINE only. Resumable + new-papers-only via ``relationship_progress`` (layer
``mentions``), mirroring the census/backfill patterns. Never runs in the Space.

    python -m pipeline.mentions            # new papers only
    python -m pipeline.mentions --all      # (re)index the whole corpus
"""
from __future__ import annotations

import argparse
import logging
import re
from pathlib import Path

from pipeline.score import load_interest_profile
from store import db

logger = logging.getLogger("mentions")

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "corpus.sqlite"
PROFILE_PATH = ROOT / "config" / "interest_profile.yaml"
LAYER = "mentions"


def build_lexicon(profile: dict) -> dict[str, str]:
    """Return {surface_term: entity_type} from config.

    Sourced from ``relationships.mentions.extra_genes`` + ``tracked_keywords`` +
    each focus area's ``keywords``. Multi-word phrases and generic words are
    dropped — the index is for named entities (genes/targets/drugs), not topics.
    Everything here is typed ``gene`` (the lexicon is gene/target-centric);
    diseases come from EPMC annotations.
    """
    rel = (profile.get("relationships") or {}).get("mentions") or {}
    terms: set[str] = set(rel.get("extra_genes") or [])
    for kws in (profile.get("tracked_keywords") or {}).values():
        terms.update(kws or [])
    for area in profile.get("focus_areas") or []:
        terms.update(area.get("keywords") or [])
    lex: dict[str, str] = {}
    for t in terms:
        t = (t or "").strip().strip('"')
        # Keep single tokens that look like a symbol/identifier: alnum + - .
        # Drop multi-word phrases ("transcription factor") and pure lowercase
        # English words ("oncogene", "resistance") — those are topic words, not
        # entities, and would pollute a literal index.
        if not t or " " in t:
            continue
        if t.islower() and t.isalpha():
            continue
        lex[t] = "gene"
    return lex


def _compile(lexicon: dict[str, str], case_sensitive_max_len: int) -> list[tuple]:
    """Pre-compile (regex, term, entity_type, case_sensitive) for each lexicon term."""
    compiled = []
    for term, etype in lexicon.items():
        cs = len(term.replace("-", "")) <= case_sensitive_max_len and term.upper() == term
        flags = 0 if cs else re.IGNORECASE
        pat = re.compile(rf"(?<![\w-]){re.escape(term)}(?![\w-])", flags)
        compiled.append((pat, term, etype, cs))
    return compiled


def scan_record(rec: dict, compiled: list[tuple]) -> list[dict]:
    """Literal-scan one record's title+abstract; return mention dicts."""
    text = f"{rec.get('title') or ''}\n{rec.get('abstract') or ''}"
    out: list[dict] = []
    for pat, term, etype, _cs in compiled:
        n = len(pat.findall(text))
        if n:
            out.append({"entity_type": etype, "entity": term,
                        "method": "literal_scan", "count": n})
    return out


def index_mentions(db_path: Path = DEFAULT_DB, *, profile_path: Path = PROFILE_PATH,
                   reindex_all: bool = False, batch_commit: int = 500) -> dict:
    """Populate the ``mentions`` table for new (or all) papers. Returns a summary."""
    profile = load_interest_profile(profile_path)
    rel = (profile.get("relationships") or {}).get("mentions") or {}
    lexicon = build_lexicon(profile)
    compiled = _compile(lexicon, int(rel.get("case_sensitive_max_len", 4)))
    logger.info("Mention lexicon: %d terms", len(lexicon))

    conn = db.connect(db_path)
    db.init_schema(conn)
    done = set() if reindex_all else db.relationship_progress_present(conn, LAYER)

    n_papers = n_mentions = 0
    pending: list[str] = []
    for rec in db.iter_papers(conn, include_excluded=False):
        pid = rec["paper_id"]
        if pid in done:
            continue
        mentions = scan_record(rec, compiled)
        # Merge-aware write: replace ONLY this paper's literal_scan rows. The
        # epmc_annotation method is owned by pipeline/annotate.py (richer EPMC
        # Annotations-API data, written via set_mentions_for_method). Using the
        # blanket set_mentions here would DELETE those annotation rows whenever the
        # literal scan runs after them — e.g. a corpus backfill — silently wiping
        # millions of annotation mentions. set_mentions_for_method keeps the two
        # passes independent and order-free (per the set_mentions_for_method contract).
        db.set_mentions_for_method(conn, pid, mentions, method="literal_scan", commit=False)
        n_papers += 1
        n_mentions += len(mentions)
        pending.append(pid)
        if len(pending) >= batch_commit:
            db.mark_relationship_progress(conn, LAYER, pending, commit=True)
            pending.clear()
    if pending:
        db.mark_relationship_progress(conn, LAYER, pending, commit=True)
    conn.close()
    summary = {"papers_indexed": n_papers, "mentions_written": n_mentions,
               "lexicon_size": len(lexicon)}
    logger.info("mentions: %s", summary)
    return summary


def main() -> None:
    ap = argparse.ArgumentParser(description="Build the literal entity-mention index (ADR-0004).")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--all", action="store_true", help="Re-index the whole corpus (not just new papers).")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    s = index_mentions(args.db, reindex_all=args.all)
    print(f"Indexed {s['papers_indexed']} papers, {s['mentions_written']} mentions "
          f"({s['lexicon_size']}-term lexicon).")


if __name__ == "__main__":
    main()
