"""Durable corpus store: SQLite schema + embedding index.

Modeled on the biodata-registry package shape (a thin loader over a collection
of items). The pipeline writes here; the Space loads it READ-ONLY at startup.
HF Space storage is ephemeral, so the SQLite file + index are committed to a
durable store (HF Dataset repo or external DB) at the end of each run.
"""
