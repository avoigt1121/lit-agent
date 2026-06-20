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
import re

QA_MODEL = os.environ.get("QA_MODEL", "claude-sonnet-4-6")

SYSTEM_GUARD = """You are a literature assistant for pancreatic-cancer (PDAC) researchers. \
Answer the QUESTION strictly from the PASSAGES the user provides. Rules:
- Use ONLY facts stated in the passages. Do not add outside knowledge, and never \
invent results, numbers, mechanisms, or methods.
- After each claim, cite its source DOI(s) in square brackets and NOTHING else — \
e.g. [10.1234/abc] or [10.1234/abc; 10.5678/def]. Do not write the author, date, or a \
URL inside the brackets: those are added automatically from the passage metadata. \
Never label anyone as the "PI" or name a lab/institution (not in the passages).
- Every passage is an ABSTRACT only — no full text is available. If the question \
asks for methodological or quantitative detail the abstract does not state, say \
plainly that the full text isn't available and summarize what the abstract does \
report; do NOT infer or fabricate the methods/results.
- If the passages do not contain the answer, say so directly rather than guessing.
- Be concise and scientific; prefer hedged language ("the abstract reports", \
"consistent with") over overstatement."""


def _authors_str(authors) -> str:
    """Lead author + 'et al.' for attribution — never labelled PI, no lab inferred."""
    if not authors:
        return "author n/a"
    return authors[0] if len(authors) == 1 else f"{authors[0]} et al."


def format_passages(passages) -> str:
    if not passages:
        return "(no passages retrieved)"
    blocks = []
    for i, p in enumerate(passages, 1):
        cite = p.doi or p.paper_id
        src = "full text" if p.is_full_text else "abstract only"
        # Header carries the citation parts the guard must reproduce: author, date, DOI.
        head = " · ".join(x for x in [
            _authors_str(getattr(p, "authors", None)), p.published_date,
            f"DOI: {cite}", f"({src})"] if x)
        meta = " · ".join(x for x in [p.title, p.journal_or_server] if x)
        blocks.append(f"[{i}] {head}\n{meta}\n{p.text or '(no abstract available)'}")
    return "\n\n".join(blocks)


def build_user_content(question: str, passages) -> str:
    return f"PASSAGES:\n\n{format_passages(passages)}\n\n---\nQUESTION: {question}"


# --- Enforced citations -----------------------------------------------------
# The model cites bare DOIs in brackets (guarded above + DOI-hallucination
# backstopped in eval/run_eval.py). render_citations() then deterministically
# rewrites each [DOI] into a linked [author, date](url) using the REAL metadata
# of the passage that DOI came from — so author/date/link can never be wrong or
# fabricated by the model: it never writes them. A cited DOI with no matching
# passage (a hallucination) is left as a plain doi.org link so the eval backstop
# still sees the bare DOI and flags it.
_RENDER_DOI_RE = re.compile(r"10\.\d{4,9}/[-._()/:a-z0-9]+", re.I)  # no ';'/',' → splits DOI lists
_CITE_RE = re.compile(r"\[([^\[\]]*?10\.\d{4,9}/[^\[\]]+?)\]")


def _norm_doi(d: str | None) -> str:
    return (d or "").strip().lower().rstrip(".,;")


def _passage_links(passages) -> dict:
    """normalized DOI -> (display_label, url) drawn only from passage metadata."""
    out: dict[str, tuple[str, str]] = {}
    for p in passages:
        if not p.doi:
            continue
        url = p.oa_fulltext_url or f"https://doi.org/{p.doi}"
        label = _authors_str(getattr(p, "authors", None))
        if p.published_date:
            label = f"{label}, {p.published_date}"
        out[_norm_doi(p.doi)] = (label, url)
    return out


def render_citations(text: str, passages) -> str:
    """Rewrite the model's bare-DOI brackets into linked author/date citations."""
    links = _passage_links(passages)

    def _repl(m: re.Match) -> str:
        rendered = []
        for raw in _RENDER_DOI_RE.findall(m.group(1)):
            nd = _norm_doi(raw)
            if nd in links:
                label, url = links[nd]
                rendered.append(f"[{label}]({url})")
            else:  # not from a retrieved passage — keep the DOI so eval can flag it
                rendered.append(f"[{raw}](https://doi.org/{nd})")
        return "(" + "; ".join(rendered) + ")" if rendered else m.group(0)

    return _CITE_RE.sub(_repl, text)


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
    """Non-streaming convenience wrapper, with citations rendered (enforced)."""
    return render_citations("".join(answer_stream(question, passages, client)), passages)
