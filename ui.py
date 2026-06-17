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
            lines.append(f"- {title_md} — `{p.doi or p.paper_id}` (sim {p.score:.2f}){tag}")
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
            history[-1]["content"] = ("I couldn't find any matching papers in the ingested corpus"
                                      + (" for that window." if since else ".")
                                      + " Try rephrasing or widening the time filter.")
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

    # ------------------------------------------------------------------
    def build(self) -> gr.Blocks:
        with gr.Blocks(title="BCC PDAC Literature Q&A") as demo:
            gr.Markdown(
                "# BCC PDAC Literature Q&A\n\n"
                "Grounded answers from the ingested PDAC literature — every claim cited by DOI, "
                "strictly from retrieved abstracts. When a question needs detail that isn't in the "
                "abstract, the agent says the full text isn't available rather than infer it."
            )
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
        return demo


if __name__ == "__main__":
    LitAgentUI().build().launch()
