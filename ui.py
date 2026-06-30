"""
ui.py — Gradio chat UI for grounded literature Q&A (Phase 5).

Mirrors research-coordinator/gradio_ui.py: a streaming chat shell plus a
transparency panel — repurposed here as a "Sources" accordion that shows the
retrieved passages + DOIs behind each answer (so the user sees exactly what
grounded it). The Space serves Q&A + cached analytics ONLY; it never ingests.

Roadmap (CLAUDE.md "Post-v1 roadmap"): grow into one tab per focus area.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date, timedelta
from pathlib import Path

import gradio as gr
import yaml
from dotenv import load_dotenv

from pipeline import analytics, clinicaltrials
from pipeline.digest import provenance_sentence
from qa import answer as qa_answer
from qa import corpus_qa
from qa.planner import QueryPlanner
from qa.retrieve import Retriever

logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent

EXAMPLES = [
    "What are the new papers this week?",
    "What's new on KRAS G12D inhibitor resistance?",
    "Summarize recent CAF / stroma findings in PDAC.",
    "How many papers do you have, and what topics are covered?",
    "New immunotherapy papers this month",
    "Any new early-detection or liquid-biopsy biomarker studies?",
]

# Shown when retrieval finds nothing above the similarity floor — i.e. an
# off-topic or meta question ("what can you do", "what can I ask"). Without this,
# the bot used to feed the corpus's least-bad noise (reply letters, "Talks") to
# the grounding prompt and answer ABOUT the noise.
ORIENTATION = (
    "I answer questions grounded in the ingested **PDAC literature corpus** — "
    "every claim cited by DOI, strictly from retrieved abstracts. I couldn't find "
    "papers matching that. I can answer **topical deep-dives**, **what's-new "
    "listings** (\"new papers this week\"), and **corpus questions** (\"how many "
    "papers?\", \"what topics are covered?\"). Try, e.g.:\n\n"
    + "\n".join(f"- {q}" for q in EXAMPLES)
)


class LitAgentUI:
    def __init__(self):
        load_dotenv(ROOT / ".env")
        self._retriever = None
        self._client = None
        self._planner = None
        self._init_error = None
        self._profile = self._load_profile()  # focus-area names for meta answers
        try:
            self._retriever = Retriever()
        except Exception as exc:  # noqa: BLE001 — surface a clear message in the UI
            self._init_error = f"Corpus not available ({exc}). Run the pipeline / pull the HF Dataset."
            logger.warning(self._init_error)
        try:
            import anthropic
            if os.environ.get("ANTHROPIC_API_KEY"):
                self._client = anthropic.Anthropic()
            else:
                logger.warning("ANTHROPIC_API_KEY unset — answers fall back to raw passages.")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Anthropic client unavailable: %s", exc)
        # The LLM query planner (Tier 1): understands any phrasing and composes
        # topic + window + focus-area + count filters via tool-use. Needs both a
        # corpus and a key; without a key we keep the key-free deterministic path.
        if self._retriever is not None and self._client is not None:
            self._planner = QueryPlanner(self._retriever, self._client, self._profile)
        # Entity-mention leaderboards (ADR-0004): a one-time cheap indexed read at
        # startup from the read-only corpus (same pattern as the retriever loading
        # _papers once) — NOT a per-request recompute. Powers the Trends tab.
        self._entity_leaderboards = {}
        if self._retriever is not None:
            try:
                self._entity_leaderboards = analytics.entity_leaderboards(self._retriever.conn)
            except Exception as exc:  # noqa: BLE001 — Trends tab degrades gracefully
                logger.warning("entity leaderboards unavailable: %s", exc)

    # ------------------------------------------------------------------
    def _sources_md(self, passages) -> str:
        if not passages:
            return "_No matching passages found in the corpus._"
        lines = ["_Passages retrieved for this answer — abstracts; DOIs are cited inline in the reply:_\n"]
        for p in passages:
            link = p.oa_fulltext_url or (f"https://doi.org/{p.doi}" if p.doi else None)
            title = p.title or "(untitled)"
            title_md = f"[{title}]({link})" if link else title
            tag = " · OA" if p.is_oa else ""
            authors = getattr(p, "authors", None) or []
            who = (authors[0] if len(authors) == 1 else f"{authors[0]} et al.") if authors else None
            attribution = " · ".join(x for x in [who, p.published_date] if x)
            attribution = f" — {attribution}" if attribution else ""
            lines.append(f"- {title_md}{attribution} — `{p.doi or p.paper_id}` (sim {p.score:.2f}){tag}")
        return "\n".join(lines)

    def _respond(self, message: str, history: list, since_days):
        if not message or not message.strip():
            yield history, "", gr.skip()
            return
        history = history + [{"role": "user", "content": message},
                             {"role": "assistant", "content": ""}]
        if self._retriever is None:
            history[-1]["content"] = f"⚠️ {self._init_error or 'Corpus unavailable.'}"
            yield history, "", "_No corpus loaded._"
            return

        # Corpus / meta questions ("what are the new papers this week?", "how many
        # papers?", "what topics are covered?", "what can you do?") have no single
        # semantic match, so the vector retriever would drop them below the
        # similarity floor and return the generic orientation. Answer those
        # deterministically from SQLite first; topical questions fall through to
        # grounded synthesis below.
        try:
            meta = corpus_qa.answer_meta(message, self._retriever, self._profile)
        except Exception:  # noqa: BLE001 — a meta failure must degrade to retrieval, not crash the chat
            logger.exception("corpus_qa.answer_meta failed; falling through to retrieval")
            meta = None
        if meta is not None:
            history[-1]["content"] = meta
            yield history, "", "_Answered from the corpus index (no passage retrieval needed)._"
            return

        # Topical or HYBRID question (the deterministic pass deferred it). With a
        # key, the LLM planner handles it: it plans tool calls — composing topic +
        # time window + focus area + count — and answers ONLY from tool results
        # under the same groundedness guard. This is what makes "What new papers
        # this week mention MYC?" return recent MYC papers (search_corpus +
        # since_days) rather than the whole week's list. Tool-use needs full
        # assistant turns, so this path resolves to a complete answer rather than
        # token-streaming; we show a brief working note while it plans.
        if self._planner is not None:
            history[-1]["content"] = "_Planning the search across the corpus…_"
            yield history, "", "_Planning…_"
            try:
                rendered = self._planner.run([{"role": "user", "content": message}])
                passages = self._planner.passages
            except Exception:  # noqa: BLE001 — degrade to the direct retrieval path
                logger.exception("planner.run failed; falling through to direct retrieval")
            else:
                history[-1]["content"] = rendered
                yield history, "", self._sources_md(passages)
                return

        since = None
        try:
            if since_days and int(since_days) > 0:
                since = (date.today() - timedelta(days=int(since_days))).isoformat()
        except (TypeError, ValueError):
            since = None

        passages = self._retriever.retrieve(message, k=6, since=since)
        sources = self._sources_md(passages)

        if not passages:
            msg_out = ORIENTATION
            if since:
                msg_out += ("\n\n_(A time filter is active — widening it may also help.)_")
            history[-1]["content"] = msg_out
            yield history, "", sources
            return

        if self._client is None:
            history[-1]["content"] = ("_No `ANTHROPIC_API_KEY` set, so I can't synthesize — here are "
                                      "the most relevant passages:_\n\n" + sources)
            yield history, "", sources
            return

        accumulated = ""
        for delta in qa_answer.answer_stream(message, passages, self._client):
            accumulated += delta
            history[-1]["content"] = accumulated
            yield history, "", sources
        # Final pass: rewrite the model's bare-DOI brackets into linked
        # author/date citations from real passage metadata (enforced, not prompted).
        rendered = qa_answer.render_citations(accumulated, passages)
        if rendered != accumulated:
            history[-1]["content"] = rendered
            yield history, "", sources

    # ------------------------------------------------------------------
    # Programmatic endpoint for other agents (e.g. the research-coordinator).
    # Call via gradio_client: Client(space).predict(question, api_name="/ask")
    # → a single grounded, DOI-cited answer string (non-streaming). This is the
    # simple one-shot protocol lit-agent exposes for integration (Phase 7).
    def ask(self, question: str) -> str:
        if self._retriever is None:
            return self._init_error or "Corpus unavailable."
        if not question or not question.strip():
            return "Please provide a question."
        try:
            meta = corpus_qa.answer_meta(question, self._retriever, self._profile)
        except Exception:  # noqa: BLE001 — degrade to retrieval rather than error
            logger.exception("corpus_qa.answer_meta failed; falling through to retrieval")
            meta = None
        if meta is not None:
            return meta
        # Topical / hybrid → LLM planner (composes filters, grounded tool-use).
        if self._planner is not None:
            try:
                return self._planner.run([{"role": "user", "content": question}])
            except Exception:  # noqa: BLE001 — degrade to direct retrieval
                logger.exception("planner.run failed; falling through to direct retrieval")
        passages = self._retriever.retrieve(question, k=6)
        if not passages:
            return ORIENTATION
        if self._client is None:
            return "No API key configured for synthesis. Most relevant passages:\n\n" + self._sources_md(passages)
        return qa_answer.answer(question, passages, self._client)

    # ------------------------------------------------------------------
    # Trends tab — the FULL keyword-trend + new-trials lists the weekly email
    # links to ("See all on the site →"). Renders read-only from the offline
    # caches the pipeline produced (data/analytics.json, data/translational_
    # motion.json); the Space never recomputes them.
    def _load_profile(self) -> dict:
        try:
            return yaml.safe_load((ROOT / "config" / "interest_profile.yaml").read_text()) or {}
        except Exception:  # noqa: BLE001
            return {}

    def _pdac_query(self) -> str:
        try:
            cfg = yaml.safe_load((ROOT / "config" / "sources.yaml").read_text()) or {}
            return (cfg.get("europepmc", {}) or {}).get("query", "") or ""
        except Exception:  # noqa: BLE001
            return ""

    def _trends_html(self) -> str:
        profile = self._load_profile()
        parts = ['<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#111;">']

        # What's heating up — full keyword-trend table
        parts.append('<h3 style="margin:6px 0 2px;">What\'s heating up</h3>')
        try:
            adata = json.loads((ROOT / "data" / "analytics.json").read_text())
            movers = adata.get("keyword_movers", {})
            parts.append(analytics.movers_full_html(movers, profile, pdac_query=self._pdac_query()))
        except Exception as exc:  # noqa: BLE001
            parts.append(f'<p style="color:#6b7280;">Keyword-trend data not available yet ({exc}).</p>')

        # Most-mentioned entities — genes / diseases / drugs across the corpus
        parts.append('<h3 style="margin:20px 0 2px;">Most-mentioned entities '
                     '<span style="font-weight:400;color:#6b7280;font-size:13px;">'
                     '(genes · diseases · drugs across the corpus)</span></h3>')
        try:
            parts.append(analytics.entity_leaderboards_html(self._entity_leaderboards))
        except Exception as exc:  # noqa: BLE001
            parts.append(f'<p style="color:#6b7280;">Entity-mention data not available yet ({exc}).</p>')

        # Translational motion — full new-trials list
        parts.append('<h3 style="margin:20px 0 2px;">Translational motion '
                     '<span style="font-weight:400;color:#6b7280;font-size:13px;">'
                     '(new PDAC trial registrations)</span></h3>')
        try:
            summary = json.loads((ROOT / "data" / "translational_motion.json").read_text())
            parts.append(clinicaltrials.translational_motion_full_html(summary))
        except Exception as exc:  # noqa: BLE001
            parts.append(f'<p style="color:#6b7280;">Trial data not available yet ({exc}).</p>')

        parts.append('</div>')
        return "\n".join(parts)

    # ------------------------------------------------------------------
    def build(self) -> gr.Blocks:
        with gr.Blocks(title="BCC PDAC Literature Q&A") as demo:
            gr.Markdown(
                "# BCC PDAC Literature Q&A\n\n"
                "Grounded answers from the ingested PDAC literature — every claim cited by DOI, "
                "strictly from retrieved abstracts. When a question needs detail that isn't in the "
                "abstract, the agent says the full text isn't available rather than infer it."
            )
            gr.Markdown(f"<small>{provenance_sentence()}</small>")

            with gr.Tabs():
                with gr.Tab("Q&A"):
                    chatbot = gr.Chatbot(label="Conversation", height=480, type="messages", show_label=False)
                    with gr.Accordion("Sources (what grounded the answer)", open=False):
                        sources_panel = gr.Markdown("_Ask a question to see the cited passages._")
                    with gr.Row():
                        msg = gr.Textbox(placeholder="Ask about the recent PDAC literature…",
                                         scale=8, show_label=False, container=False)
                        send = gr.Button("Send", variant="primary", scale=1)
                    since = gr.Number(value=0, precision=0,
                                      label="Restrict to papers first seen in the last N days (0 = all)")
                    gr.Examples(examples=EXAMPLES, inputs=msg, label="Example questions")
                    gr.Markdown("_Read-only Q&A over the offline-ingested corpus · answers grounded in "
                                "retrieved abstracts with DOI citations · no ingestion happens here._")

                with gr.Tab("Trends & Translational Motion"):
                    gr.Markdown("Full keyword-trend and new-trial lists behind the weekly digest's "
                                "summaries — read-only, from the latest offline pipeline run.")
                    gr.HTML(self._trends_html())

            inputs = [msg, chatbot, since]
            outputs = [chatbot, msg, sources_panel]
            send.click(self._respond, inputs, outputs)
            msg.submit(self._respond, inputs, outputs)

            # Hidden one-shot API for other agents: client.predict(q, api_name="/ask")
            ask_q = gr.Textbox(visible=False)
            ask_a = gr.Textbox(visible=False)
            ask_btn = gr.Button(visible=False)
            ask_btn.click(self.ask, inputs=ask_q, outputs=ask_a, api_name="ask")
        return demo


if __name__ == "__main__":
    LitAgentUI().build().launch()
