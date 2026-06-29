"""
qa/corpus_qa.py — deterministic answers for CORPUS / META questions (capability
expansion on top of the semantic retriever).

The vector retriever (qa/retrieve.py) answers TOPICAL questions ("what's new on
KRAS G12D resistance?") by embedding the query and matching paper abstracts. But
users also ask CORPUS-shaped questions that have no single semantic match and so
fell through the similarity floor to the generic orientation message:

  - "What are the new papers this week?"        → list by first_seen_date window
  - "How many papers do you have?"              → corpus size
  - "What topics are covered?" / "most covered" → focus-area breakdown
  - "What can you do?" / "what can I ask?"       → capability help

This router catches those classes and answers them straight from SQLite (real
rows, real DOIs — still grounded, no fabrication) so the chat handles both
generic questions and topical deep dives. Anything it doesn't recognise returns
``None`` and the caller falls through to vector retrieval as before.

Rule-based on purpose: these intents are cheap to detect by surface form, need no
API key, and must stay grounded in the actual corpus. Keep the patterns loose but
conservative — when in doubt, return ``None`` and let retrieval handle it.
"""
from __future__ import annotations

import re
from datetime import date, timedelta

# Intent labels (RETRIEVE = "not a meta question; use the vector retriever").
LIST_RECENT = "list_recent"
CORPUS_SIZE = "corpus_size"
TOPIC_BREAKDOWN = "topic_breakdown"
HELP = "help"
RETRIEVE = "retrieve"

_PAPER_WORD = r"(?:papers?|articles?|publications?|studies|preprints?|literature|research)"

# Word → window length in days. "this week" is the headline case the user hit.
_WINDOW_WORDS = [
    (re.compile(r"\b(today|past day|last 24)\b", re.I), 1),
    (re.compile(r"\b(this|past|last|the)\s+week\b", re.I), 7),
    (re.compile(r"\b(this|past|last|the)\s+month\b", re.I), 30),
    (re.compile(r"\b(this|past|last|the)\s+(year|12 months)\b", re.I), 365),
    (re.compile(r"\b(this|past|last)\s+quarter\b", re.I), 92),
]
_LAST_N_DAYS = re.compile(r"\blast\s+(\d{1,4})\s+days?\b", re.I)

_PAPER_RE = re.compile(rf"\b{_PAPER_WORD}\b", re.I)
# Listing verbs that unambiguously mean "enumerate corpus rows" (vs. a topical
# ask). Deliberately NOT plain "new"/"recent": "any new liquid-biopsy studies?"
# is a TOPICAL question, so bare "new" must fall through to retrieval. A listing
# is signalled by an explicit time window (handled in classify_intent) or one of
# these verbs.
_RE_LIST_RECENT = re.compile(
    rf"\b(newest|latest|show me|list|added|ingested|came in)\b.*\b{_PAPER_WORD}\b"
    rf"|\b{_PAPER_WORD}\b.*\b(this|past|last)\s+(week|month|year|quarter)\b"
    # bare "what's new" = a listing ask, UNLESS it's "what's new on/about <topic>"
    # (that's a topical deep-dive → let it fall through to retrieval).
    rf"|\bwhat'?s new\b(?!\s+(on|about|in|with|for|regarding|re)\b)", re.I)
_RE_SIZE = re.compile(
    rf"\b(how many|number of|count of|total)\b.*\b{_PAPER_WORD}\b"
    rf"|\b{_PAPER_WORD}\b.*\b(do you have|are there|in the corpus|indexed)\b"
    rf"|\b(corpus|index)\s+size\b|\bhow big\b", re.I)
_RE_TOPICS = re.compile(
    r"\b(what|which)\b.*\b(topics?|focus areas?|themes?|areas?|subjects?|categories)\b"
    r"|\b(topic|focus[- ]area|theme)\s+(breakdown|distribution|coverage)\b"
    r"|\b(most|heavily)\s+covered\b|\bbreakdown by (topic|area)\b", re.I)
_RE_HELP = re.compile(
    r"\bwhat can (you|i) (do|ask|help)\b|\bwhat (are|do) you\b"
    r"|\bhow (do|does) (you|this|it) work\b|\bwhat questions\b|\bhelp\b"
    r"|\bwhat are your capabilities\b|\bwhat can this (do|answer)\b", re.I)


def classify_intent(question: str) -> str:
    q = (question or "").strip()
    if not q:
        return RETRIEVE
    # Order matters: a count question ("how many recent papers") is a size answer,
    # but "how many ... this week" reads better as a recent-list — size first only
    # when no window is named.
    if _RE_TOPICS.search(q):
        return TOPIC_BREAKDOWN
    if _RE_SIZE.search(q) and not _window_days(q):
        return CORPUS_SIZE
    if _RE_LIST_RECENT.search(q):
        return LIST_RECENT
    # An explicit window plus a paper word ("papers from the last 14 days") is a
    # listing ask even without a "new/recent" trigger word.
    if _window_days(q) and _PAPER_RE.search(q):
        return LIST_RECENT
    if _RE_HELP.search(q):
        return HELP
    return RETRIEVE


def _window_days(question: str) -> int | None:
    m = _LAST_N_DAYS.search(question)
    if m:
        return max(1, int(m.group(1)))
    for pat, days in _WINDOW_WORDS:
        if pat.search(question):
            return days
    return None


# --- focus-area helpers ------------------------------------------------------

def _area_index(profile: dict) -> list[tuple[str, str, list[str]]]:
    """[(id, display_name, [match_terms...])] from interest_profile.yaml."""
    out = []
    for a in (profile or {}).get("focus_areas", []) or []:
        aid = a.get("id")
        if not aid:
            continue
        name = a.get("name") or aid
        terms = [aid.replace("_", " "), name] + list(a.get("keywords", []) or [])
        out.append((aid, name, [t.lower() for t in terms if t]))
    return out


def _name_for(area_id: str, profile: dict) -> str:
    for aid, name, _ in _area_index(profile):
        if aid == area_id:
            return name
    return area_id


def _detect_area(question: str, profile: dict) -> str | None:
    """Best-effort focus-area scope from the question (e.g. 'new immunotherapy
    papers this week' → immunology_immunotherapy). Conservative: needs a clear
    term hit, longest match wins, else None (unscoped)."""
    q = (question or "").lower()
    best, best_len = None, 0
    for aid, _name, terms in _area_index(profile):
        for t in terms:
            if len(t) >= 4 and t in q and len(t) > best_len:
                best, best_len = aid, len(t)
    return best


# --- data access (read-only, excluded rows dropped) --------------------------

def _recent_rows(conn, days: int, limit: int, area_id: str | None):
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    if area_id:
        sql = (
            "SELECT p.doi, p.title, p.authors, p.journal_or_server, p.published_date, "
            "p.first_seen_date, p.is_oa, p.oa_fulltext_url, p.paper_id "
            "FROM papers p JOIN topic_tags t ON t.paper_id = p.paper_id "
            "WHERE p.excluded=0 AND p.first_seen_date >= ? AND t.focus_area = ? "
            "GROUP BY p.paper_id "
            "ORDER BY p.first_seen_date DESC, p.relevance_score DESC LIMIT ?")
        rows = conn.execute(sql, (cutoff, area_id, limit)).fetchall()
    else:
        sql = (
            "SELECT doi, title, authors, journal_or_server, published_date, "
            "first_seen_date, is_oa, oa_fulltext_url, paper_id FROM papers "
            "WHERE excluded=0 AND first_seen_date >= ? "
            "ORDER BY first_seen_date DESC, relevance_score DESC LIMIT ?")
        rows = conn.execute(sql, (cutoff, limit)).fetchall()
    return rows, cutoff


def _count_recent(conn, days: int, area_id: str | None) -> int:
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    if area_id:
        return conn.execute(
            "SELECT COUNT(DISTINCT p.paper_id) FROM papers p "
            "JOIN topic_tags t ON t.paper_id=p.paper_id "
            "WHERE p.excluded=0 AND p.first_seen_date >= ? AND t.focus_area = ?",
            (cutoff, area_id)).fetchone()[0]
    return conn.execute(
        "SELECT COUNT(*) FROM papers WHERE excluded=0 AND first_seen_date >= ?",
        (cutoff,)).fetchone()[0]


def _topic_counts(conn, days: int | None) -> list[tuple[str, int]]:
    if days:
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        sql = ("SELECT t.focus_area, COUNT(DISTINCT t.paper_id) FROM topic_tags t "
               "JOIN papers p ON p.paper_id=t.paper_id "
               "WHERE p.excluded=0 AND p.first_seen_date >= ? "
               "GROUP BY t.focus_area ORDER BY 2 DESC")
        rows = conn.execute(sql, (cutoff,)).fetchall()
    else:
        sql = ("SELECT t.focus_area, COUNT(DISTINCT t.paper_id) FROM topic_tags t "
               "JOIN papers p ON p.paper_id=t.paper_id "
               "WHERE p.excluded=0 GROUP BY t.focus_area ORDER BY 2 DESC")
        rows = conn.execute(sql).fetchall()
    return [(r[0], r[1]) for r in rows]


# --- formatting --------------------------------------------------------------

def _window_phrase(days: int) -> str:
    return {1: "the last day", 7: "the past week", 30: "the past month",
            92: "the past quarter", 365: "the past year"}.get(days, f"the last {days} days")


def _authors_str(authors) -> str:
    if not authors:
        return ""
    return authors[0] if len(authors) == 1 else f"{authors[0]} et al."


def _format_recent(rows, days: int, area_name: str | None, total: int) -> str:
    import json
    phrase = _window_phrase(days)
    scope = f" in **{area_name}**" if area_name else ""
    if not rows:
        return (f"No papers were first seen{scope} in {phrase} "
                f"(novelty is measured by `first_seen_date`). The weekly pipeline "
                f"adds new papers each Monday — try a wider window (e.g. "
                f"\"last 30 days\") or ask a topical question instead.")
    head = (f"**{total}** paper{'s' if total != 1 else ''} first seen{scope} in "
            f"{phrase}" + (f" — showing the {len(rows)} most recent:" if total > len(rows)
                           else ":"))
    lines = [head, ""]
    for r in rows:
        d = dict(r)
        authors = json.loads(d["authors"]) if d.get("authors") else []
        who = _authors_str(authors)
        link = d.get("oa_fulltext_url") or (f"https://doi.org/{d['doi']}" if d.get("doi") else None)
        title = d.get("title") or "(untitled)"
        title_md = f"[{title}]({link})" if link else title
        bits = [x for x in [who, d.get("journal_or_server"), d.get("published_date")] if x]
        tag = " · OA" if d.get("is_oa") else ""
        meta = (" — " + " · ".join(bits)) if bits else ""
        cite = f" — `{d.get('doi') or d.get('paper_id')}`"
        lines.append(f"- {title_md}{meta}{cite}{tag}")
    if not area_name:
        lines.append("\n_Tip: scope it to a focus area (e.g. \"new immunotherapy "
                     "papers this week\") or ask a topical question for a synthesized answer._")
    return "\n".join(lines)


def _format_size(conn) -> str:
    total = conn.execute("SELECT COUNT(*) FROM papers WHERE excluded=0").fetchone()[0]
    n7 = _count_recent(conn, 7, None)
    n30 = _count_recent(conn, 30, None)
    oldest = conn.execute(
        "SELECT MIN(first_seen_date), MAX(first_seen_date) FROM papers WHERE excluded=0"
    ).fetchone()
    span = ""
    if oldest and oldest[0] and oldest[1]:
        span = f" Records span first-seen dates **{oldest[0]} → {oldest[1]}**."
    return (f"The corpus holds **{total:,}** active PDAC papers (deduped, abstract-"
            f"bearing).{span}\n\n"
            f"- New in the past week: **{n7:,}**\n"
            f"- New in the past month: **{n30:,}**\n\n"
            f"_\"New\" is keyed on `first_seen_date`. Quarantined off-topic / "
            f"abstract-less records are excluded from every count._")


def _format_topics(conn, profile: dict, days: int | None) -> str:
    counts = _topic_counts(conn, days)
    phrase = f" first seen in {_window_phrase(days)}" if days else " across the whole corpus"
    if not counts:
        return (f"No classified papers{phrase} yet. Topic labels are assigned by the "
                f"offline classifier; if you scoped to a recent window it may simply "
                f"have no new classified papers yet.")
    lines = [f"Focus-area coverage{phrase} (papers may carry more than one label):", ""]
    for area_id, n in counts:
        lines.append(f"- **{_name_for(area_id, profile)}** — {n:,}")
    lines.append("\n_Full keyword-level trends and \"what's heating up\" live on the "
                 "**Trends & Translational Motion** tab._")
    return "\n".join(lines)


_HELP = (
    "I'm the BCC PDAC literature assistant. I can answer:\n\n"
    "- **Topical / deep-dive questions** — synthesized from retrieved abstracts, "
    "every claim cited by DOI. _e.g._ \"What's new on KRAS G12D inhibitor "
    "resistance?\", \"Summarize recent CAF / stroma findings.\"\n"
    "- **What's new** — \"What are the new papers this week?\" (also this month / "
    "year, or \"last 30 days\"), optionally scoped to a focus area "
    "(\"new immunotherapy papers this week\").\n"
    "- **Corpus questions** — \"How many papers do you have?\", \"What topics are "
    "covered?\" / \"most covered?\"\n\n"
    "I answer strictly from the ingested corpus — if the full text isn't open-access "
    "I summarize the abstract rather than infer methods. For keyword trends and new "
    "trial registrations, see the **Trends & Translational Motion** tab."
)


# --- public entry point ------------------------------------------------------

def answer_meta(question: str, retriever, profile: dict) -> str | None:
    """Return a deterministic answer for a corpus/meta question, or ``None`` to
    signal the caller should fall through to vector retrieval.

    ``retriever`` is a qa.retrieve.Retriever (its ``.conn`` is read-only here).
    """
    intent = classify_intent(question)
    if intent == RETRIEVE:
        return None
    conn = retriever.conn
    if intent == HELP:
        return _HELP
    if intent == CORPUS_SIZE:
        return _format_size(conn)
    if intent == TOPIC_BREAKDOWN:
        return _format_topics(conn, profile, _window_days(question))
    if intent == LIST_RECENT:
        days = _window_days(question) or 7
        area_id = _detect_area(question, profile)
        area_name = _name_for(area_id, profile) if area_id else None
        rows, _ = _recent_rows(conn, days, limit=20, area_id=area_id)
        total = _count_recent(conn, days, area_id)
        return _format_recent(rows, days, area_name, total)
    return None
