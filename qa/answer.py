"""
qa/answer.py — grounded answer + anti-fabrication guard (Phase 5). The trust layer.

Guard (CLAUDE.md prompt skeleton + the DecoupleRpy groundedness discipline):
  - Answer ONLY from the retrieved passages, citing DOIs.
  - Every passage is an abstract (no full text). If the question asks methodology
    or quantitative detail not in the abstract, say the full text isn't available
    and summarize what the abstract states — never infer or fabricate methods.
  - If the passages don't contain the answer, say so; don't guess.

This is what eval/ grades for groundedness in Phase 6.
"""
from __future__ import annotations

import os

QA_MODEL = os.environ.get("QA_MODEL", "claude-sonnet-4-6")

SYSTEM_GUARD = """You are a literature assistant for pancreatic-cancer (PDAC) researchers. \
Answer the QUESTION strictly from the PASSAGES the user provides. Rules:
- Use ONLY facts stated in the passages. Do not add outside knowledge, and never \
invent results, numbers, mechanisms, or methods.
- After each claim, cite the source DOI in square brackets, e.g. [10.1234/abc].
- Every passage is an ABSTRACT only — no full text is available. If the question \
asks for methodological or quantitative detail the abstract does not state, say \
plainly that the full text isn't available and summarize what the abstract does \
report; do NOT infer or fabricate the methods/results.
- If the passages do not contain the answer, say so directly rather than guessing.
- Be concise and scientific; prefer hedged language ("the abstract reports", \
"consistent with") over overstatement."""


def format_passages(passages) -> str:
    if not passages:
        return "(no passages retrieved)"
    blocks = []
    for i, p in enumerate(passages, 1):
        cite = p.doi or p.paper_id
        src = "full text" if p.is_full_text else "abstract only"
        meta = " · ".join(x for x in [p.title, p.journal_or_server, p.published_date] if x)
        blocks.append(f"[{i}] DOI: {cite} ({src})\n{meta}\n{p.text or '(no abstract available)'}")
    return "\n\n".join(blocks)


def build_user_content(question: str, passages) -> str:
    return f"PASSAGES:\n\n{format_passages(passages)}\n\n---\nQUESTION: {question}"


def answer_stream(question: str, passages, client):
    """Yield grounded answer text deltas (streaming)."""
    content = build_user_content(question, passages)
    with client.messages.stream(
        model=QA_MODEL, max_tokens=1024, system=SYSTEM_GUARD,
        messages=[{"role": "user", "content": content}],
    ) as stream:
        for text in stream.text_stream:
            yield text


def answer(question: str, passages, client) -> str:
    """Non-streaming convenience wrapper."""
    return "".join(answer_stream(question, passages, client))
