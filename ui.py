"""
ui.py — Gradio chat UI for grounded literature Q&A (Phase 5).

Mirrors research-coordinator/gradio_ui.py: a streaming chat shell plus a
transparency panel — repurposed here as a "Sources" accordion that shows the
retrieved passages + DOIs behind each answer (so the user sees exactly what
grounded it). The Space serves Q&A + cached analytics ONLY; it never ingests.

Roadmap (CLAUDE.md "Post-v1 roadmap"): grow into one tab per focus area.
"""
from __future__ import annotations

import logging
import os
from datetime import date, timedelta
from pathlib import Path

import gradio as gr
from dotenv import load_dotenv

from pipeline.digest import provenance_sentence
from qa import answer as qa_answer
from qa.retrieve import Retriever

logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent

EXAMPLES = [
    "What's new on KRAS G12D inhibitor resistance?",
    "Summarize recent CAF / stroma findings in PDAC.",
    "What MYC-related mechanisms were reported recently?",
    "Any new early-detection or liquid-biopsy biomarker studies?",
]

# Shown when retrieval finds nothing above the similarity floor — i.e. an
# off-topic or meta question ("what can you do", "what can I ask"). Without this,
# the bot used to feed the corpus's least-bad noise (reply letters, "Talks") to
# the grounding prompt and answer ABOUT the noise.
ORIENTATION = (
    "I answer questions grounded in the ingested **PDAC literature corpus** — "
    "every claim cited by DOI, strictly from retrieved abstracts. I couldn't find "
    "papers matching that, which usually means it was a general/meta question "
    "rather than a literature search. Try a topical PDAC question, e.g.:\n\n"
    + "\n".join(f"- {q}" for q in EXAMPLES)
)


class LitAgentUI:
    def __init__(self):
        load_dotenv(ROOT / ".env")
        self._retriever = None
        self._client = None
        self._init_error = None
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
        passages = self._retriever.retrieve(question, k=6)
        if not passages:
            return ORIENTATION
        if self._client is None:
            return "No API key configured for synthesis. Most relevant passages:\n\n" + self._sources_md(passages)
        return qa_answer.answer(question, passages, self._client)

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
