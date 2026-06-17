"""
qa/answer.py — grounded answer + guards (Phase 5). The trust layer.

Guard (CLAUDE.md prompt skeleton, non-negotiable):
  - Answer ONLY from the retrieved passages, citing DOIs.
  - If the question asks methodology and only an abstract is present (no OA full
    text), say the full text isn't available and summarize what the abstract
    states — DO NOT infer or fabricate methods.
  - Never present a claim absent from the retrieved text.

This is the Q&A analogue of the DecoupleRpy anti-fabrication discipline and is
what eval/ grades for groundedness.

TODO(Phase 5): answer(question, passages) -> stream of tokens with citations;
abstract_only_guard(); assert every claim traces to a passage.
"""
from __future__ import annotations
