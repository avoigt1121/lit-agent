"""
ui.py — Gradio chat UI (Phase 5), mirroring research-coordinator/gradio_ui.py.

Reuse from the reference:
  - streaming chat shell + Examples + save/load session
  - the transparency-panel pattern, repurposed for literature Q&A: instead of
    Data/Code/Logic, surface the RETRIEVED PASSAGES + DOI citations behind each
    answer (so the user sees exactly what grounded it)

Answers come from qa/answer.py and stream with DOI citations. No ingestion here.

Roadmap (CLAUDE.md "Post-v1 roadmap"): grow into one TAB PER FOCUS AREA — each
tab shows that area's recent papers, its coverage analytics, and Q&A scoped to
it. Keep retrieval/analytics focus-area-filterable so this is a UI change, not a
rewrite.

TODO(Phase 5): LitAgentUI with _respond() calling qa.retrieve + qa.answer,
a "Sources" accordion listing cited DOIs + passages, week/paper filters.
"""
from __future__ import annotations
