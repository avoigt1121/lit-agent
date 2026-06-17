"""
eval/run_eval.py — groundedness + digest-relevance graders (Phase 6).

Adapts research-coordinator/eval/run_eval.py (trace-aware LLM judge, checkpointed
runs, deterministic anti-fabrication backstop, markdown report). Two banks:

  1. Q&A groundedness (questions.json): every claim in the answer must trace to a
     retrieved passage; methods-on-abstract-only must trigger the guard, not
     fabricate. Deterministic backstop: flag answers asserting methodology with
     no full-text passage in the retrieval trace.
  2. Digest relevance precision (relevance_set.json): graded against human labels
     — was each surfaced item actually of interest to the BCC?

Report pass rates; iterate the interest profile + guards against failures.

TODO(Phase 6): port the judge harness; add the groundedness backstop +
relevance-precision grader; wire to qa/ and pipeline/score.py.
"""
from __future__ import annotations
