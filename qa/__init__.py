"""Grounded Q&A over the ingested corpus (online, served by the Space).

Retrieval-augmented: retrieve.py pulls top-k passages; answer.py answers ONLY
from them with DOI citations and the anti-fabrication guard. Mirrors the
DecoupleRpy_Agent groundedness discipline (answer from real evidence, refuse to
fabricate) — not its domain logic.
"""
