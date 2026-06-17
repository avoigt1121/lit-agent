"""
qa/retrieve.py — top-k retrieval over the corpus (Phase 5).

Embed the query with the same model score.py used, search store/vectors.py, and
return the top-k passages with their source paper + DOI + is_oa + whether the
passage is abstract or full text. Filterable by paper or week so the UI can
scope a question. Retrieval feeds answer.py — which may only use what is
returned here.

TODO(Phase 5): retrieve(query, k, paper_id=None, since=None) -> list[Passage].
"""
from __future__ import annotations
