"""
qa/planner.py — LLM-driven query planner (Tier 1 of the chat roadmap).

The rule-based router (qa/corpus_qa.py) is fast and key-free but brittle: it maps
a question to ONE intent and treats "listing" and "topical" as mutually exclusive,
so a hybrid like "What new papers this week mention MYC?" lists the whole week and
ignores MYC. This planner replaces the brittle middle with Anthropic tool-use: the
model PLANS and PARAMETERIZES corpus operations (it can compose topic + time
window + focus area + count), then answers ONLY from the tool results under the
same groundedness guard as qa/answer.py (DOI citations; "full text isn't
available" for methodology not in the abstract; never fabricate).

Every tool here wraps an EXISTING capability — nothing new touches the corpus, and
the Space still never ingests:
  - search_corpus  → Retriever.retrieve (semantic + `since` first_seen filter; this
                     is what fixes the MYC hybrid: search_corpus(query="MYC",
                     since_days=7)).
  - list_recent    → corpus_qa.list_recent_text (first_seen_date window listing).
  - corpus_stats   → corpus_qa.corpus_size_text (counts + deltas).
  - topic_breakdown→ corpus_qa.topic_breakdown_text (focus-area coverage).
  - get_paper      → Retriever.retrieve(paper_id=…) single-record drill-in.

Design hedge for Tier 2 (per-chat memory): ``run()`` already accepts a full
messages/history list; v1 callers pass a single turn. Adding memory later is
passing more turns, not a rewrite — no API change here.

Degradation: this module needs an Anthropic client. When ANTHROPIC_API_KEY is
unset the caller keeps the existing key-free path (deterministic meta answers +
raw-passage fallback); it never constructs a planner.
"""
from __future__ import annotations

import json
import logging
from datetime import date, timedelta

from qa import answer as qa_answer
from qa import corpus_qa

logger = logging.getLogger(__name__)

# Same model + groundedness contract as the direct-synthesis path. The planner
# system prompt = tool-planning instructions + the verbatim qa/answer.py guard, so
# whichever path answers, the trust rules are identical.
PLANNER_MODEL = qa_answer.QA_MODEL
MAX_TOOL_ITERS = 5  # hard stop so a confused model can't loop forever

_PLANNER_PREAMBLE = """You are the query planner for the BCC PDAC literature \
assistant. You answer the user's question by calling the provided tools and then \
synthesizing ONLY from what the tools return — you have no other knowledge of the \
corpus contents.

Plan the right tool calls, composing filters as needed:
- Topical question ("what's new on KRAS G12D resistance?") → search_corpus with the \
topic as `query`.
- Listing / "what's new" → list_recent. If the question is BOTH a listing AND about a \
topic ("new papers this week mentioning MYC"), use search_corpus with that topic as \
`query` and `since_days` for the window — do NOT use list_recent, which ignores the topic.
- "How many papers?" / corpus size → corpus_stats.
- "What topics are covered / most covered?" → topic_breakdown.
- A specific paper by DOI → get_paper.
- "Which / how many papers MENTION <gene/drug/disease>?" (a literal named entity, e.g. \
"papers that mention SMAD4", "how many papers mention gemcitabine") → \
find_papers_mentioning. This is an EXACT index lookup over the corpus's literal + \
text-mined entity annotations — use it instead of search_corpus when the user asks who \
mentions a specific named entity, and combine its count with a search_corpus call if \
they also want the substance of those papers.
You may call several tools (e.g. search two related sub-topics, or stats + a search) \
before answering. Prefer `focus_area` only when the question clearly targets one of the \
defined areas. For a CONCEPTUAL/topical question ("how does MYC drive PDAC?") use \
search_corpus; for a LITERAL "papers mentioning MYC" lookup use find_papers_mentioning.

After the tools return, write the answer. The retrieved passages are ABSTRACTS \
ONLY. Apply these rules exactly:
"""

# Reuse the exact answer-path guard so groundedness is identical across paths.
PLANNER_SYSTEM = _PLANNER_PREAMBLE + "\n" + qa_answer.SYSTEM_GUARD


def tool_specs(profile: dict) -> list[dict]:
    """Anthropic tool definitions. focus_area enum is built from the live profile
    so the model can only name real focus-area ids."""
    area_ids = corpus_qa.valid_area_ids(profile)
    area_desc = ("One of the defined focus-area ids: " + ", ".join(area_ids)
                 if area_ids else "A focus-area id.")
    return [
        {
            "name": "search_corpus",
            "description": (
                "Semantic search over the PDAC corpus abstracts. Returns the most "
                "relevant papers as cited passages (DOIs included). Use for any "
                "topical question, and for hybrid 'new papers about X' questions "
                "(set since_days for the window)."),
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string",
                              "description": "The topic / terms to search for (e.g. 'MYC', 'KRAS G12D inhibitor resistance')."},
                    "since_days": {"type": "integer",
                                   "description": "If set, only papers first seen in the last N days (e.g. 7 for 'this week')."},
                    "focus_area": {"type": "string", "description": area_desc},
                    "k": {"type": "integer", "description": "How many passages to return (default 6, max 12)."},
                },
                "required": ["query"],
            },
        },
        {
            "name": "list_recent",
            "description": (
                "List the papers first seen in the last N days (newest first), "
                "optionally scoped to a focus area. Use ONLY for pure 'what's new' "
                "listings with no specific topic; for a topic, use search_corpus."),
            "input_schema": {
                "type": "object",
                "properties": {
                    "window_days": {"type": "integer", "description": "Window length in days (7 = this week, 30 = this month)."},
                    "focus_area": {"type": "string", "description": area_desc},
                },
                "required": ["window_days"],
            },
        },
        {
            "name": "corpus_stats",
            "description": "Corpus size: total active papers plus new-in-last-week / new-in-last-month counts and the date span.",
            "input_schema": {"type": "object", "properties": {}},
        },
        {
            "name": "topic_breakdown",
            "description": "Focus-area coverage (paper counts per area). Pass window_days to restrict to recently-seen papers, omit for the whole corpus.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "window_days": {"type": "integer", "description": "Optional: only papers first seen in the last N days."},
                },
            },
        },
        {
            "name": "get_paper",
            "description": "Fetch one specific paper by its DOI (drill-in). Returns the paper's abstract as a cited passage.",
            "input_schema": {
                "type": "object",
                "properties": {"doi": {"type": "string", "description": "The paper's DOI, e.g. 10.1234/abc."}},
                "required": ["doi"],
            },
        },
        {
            "name": "find_papers_mentioning",
            "description": (
                "EXACT lookup of papers that LITERALLY mention a named entity (a gene, "
                "drug/chemical, or disease), from the corpus's literal + Europe PMC "
                "text-mined annotation index. Returns the total count plus the most "
                "recent matching papers with DOIs. Use for 'which/how many papers mention "
                "X' — NOT for conceptual questions (use search_corpus for those)."),
            "input_schema": {
                "type": "object",
                "properties": {
                    "entity": {"type": "string",
                               "description": "The named entity exactly as written, e.g. 'SMAD4', 'gemcitabine', 'cholangiocarcinoma'."},
                    "entity_type": {"type": "string", "enum": list(corpus_qa._MENTION_TYPES),
                                    "description": "Optional filter: gene | disease | chemical | organism. Omit to match any type."},
                    "limit": {"type": "integer", "description": "Max papers to list (default 20)."},
                },
                "required": ["entity"],
            },
        },
    ]


class QueryPlanner:
    """Runs the Anthropic tool-use loop, then renders a grounded, DOI-cited answer.

    Collects the Passage objects actually returned to the model (from
    search_corpus / get_paper) so the caller can (a) drive render_citations and
    (b) populate the "Sources" panel — identical to the direct-synthesis path.
    """

    def __init__(self, retriever, client, profile: dict):
        self.retriever = retriever
        self.client = client
        self.profile = profile
        self.passages = []          # accumulated, deduped by paper_id
        self._seen_pids = set()

    # -- tool implementations (all read-only) --------------------------------
    def _since(self, since_days):
        try:
            n = int(since_days)
        except (TypeError, ValueError):
            return None
        return (date.today() - timedelta(days=n)).isoformat() if n > 0 else None

    def _collect(self, passages):
        for p in passages:
            if p.paper_id not in self._seen_pids:
                self._seen_pids.add(p.paper_id)
                self.passages.append(p)

    def _search_corpus(self, query, since_days=None, focus_area=None, k=6):
        try:
            k = max(1, min(int(k), 12))
        except (TypeError, ValueError):
            k = 6
        since = self._since(since_days)
        area = focus_area if focus_area in corpus_qa.valid_area_ids(self.profile) else None
        # Over-fetch when post-filtering by focus_area so the filter doesn't starve k.
        passages = self.retriever.retrieve(query, k=k * 4 if area else k, since=since)
        if area:
            passages = [p for p in passages
                        if area in ((self.retriever._papers.get(p.paper_id) or {}).get("focus_areas") or [])][:k]
        else:
            passages = passages[:k]
        self._collect(passages)
        if not passages:
            scope = []
            if since_days:
                scope.append(f"first seen in the last {since_days} days")
            if area:
                scope.append(f"in focus area '{area}'")
            extra = (" (" + ", ".join(scope) + ")") if scope else ""
            return (f"No passages in the corpus matched that query{extra}. "
                    f"The corpus is PDAC-only; if the question is outside that scope, "
                    f"say you can't answer it from the corpus.")
        return qa_answer.format_passages(passages)

    def _get_paper(self, doi):
        nd = qa_answer._norm_doi(doi)
        match_pid = match_rec = None
        for pid, rec in self.retriever._papers.items():
            if qa_answer._norm_doi(rec.get("doi")) == nd:
                match_pid, match_rec = pid, rec
                break
        if match_pid is None:
            return f"No paper with DOI {doi} is in the corpus."
        passages = self.retriever.retrieve(match_rec.get("title") or doi, k=1, paper_id=match_pid)
        self._collect(passages)
        return qa_answer.format_passages(passages) if passages else f"No abstract stored for DOI {doi}."

    def _dispatch(self, name, args):
        if name == "search_corpus":
            return self._search_corpus(args.get("query", ""), args.get("since_days"),
                                       args.get("focus_area"), args.get("k", 6))
        if name == "list_recent":
            days = int(args.get("window_days") or 7)
            return corpus_qa.list_recent_text(self.retriever, self.profile, days, args.get("focus_area"))
        if name == "corpus_stats":
            return corpus_qa.corpus_size_text(self.retriever)
        if name == "topic_breakdown":
            wd = args.get("window_days")
            return corpus_qa.topic_breakdown_text(self.retriever, self.profile,
                                                  int(wd) if wd else None)
        if name == "get_paper":
            return self._get_paper(args.get("doi", ""))
        if name == "find_papers_mentioning":
            limit = args.get("limit")
            return corpus_qa.papers_mentioning_text(
                self.retriever, args.get("entity", ""), args.get("entity_type"),
                int(limit) if limit else 20)
        return f"Unknown tool: {name}"

    # -- the loop ------------------------------------------------------------
    def run(self, messages: list[dict]) -> str:
        """Drive the tool-use loop over a messages/history list and return the
        final grounded answer (citations rendered). v1 callers pass a single-turn
        list; the signature is history-shaped so Tier 2 memory slots in unchanged.

        Resets accumulated passages each call, so ``self.passages`` after ``run``
        reflects exactly this question's evidence.
        """
        self.passages, self._seen_pids = [], set()
        convo = [dict(m) for m in messages]
        tools = tool_specs(self.profile)
        final_text = ""
        for _ in range(MAX_TOOL_ITERS):
            resp = self.client.messages.create(
                model=PLANNER_MODEL, max_tokens=1500, system=PLANNER_SYSTEM,
                tools=tools, messages=convo)
            if resp.stop_reason == "tool_use":
                convo.append({"role": "assistant", "content": resp.content})
                results = []
                for block in resp.content:
                    if getattr(block, "type", None) == "tool_use":
                        try:
                            out = self._dispatch(block.name, block.input or {})
                        except Exception as exc:  # noqa: BLE001 — one bad tool call shouldn't crash the chat
                            logger.exception("planner tool %s failed", block.name)
                            out = f"Tool error: {exc}"
                        results.append({"type": "tool_result", "tool_use_id": block.id, "content": out})
                convo.append({"role": "user", "content": results})
                continue
            # end_turn: collect the text blocks
            final_text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
            break
        else:
            # Hit the iteration cap mid-plan — make one final, tool-free pass so the
            # model must answer from what it already gathered.
            convo.append({"role": "user", "content": "Answer now from the tool results above; do not call more tools."})
            resp = self.client.messages.create(
                model=PLANNER_MODEL, max_tokens=1500, system=PLANNER_SYSTEM, messages=convo)
            final_text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")

        if not final_text.strip():
            final_text = ("I couldn't find supporting passages in the corpus for that. "
                          "Try a topical question, a 'what's new' listing, or ask how many "
                          "papers / what topics are covered.")
        return qa_answer.render_citations(final_text, self.passages)
