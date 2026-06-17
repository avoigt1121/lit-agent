"""
store/vectors.py — embedding index read/write (Phase 1).

A deliberately small vector index: an in-memory float32 matrix + parallel id
list, persisted as a single .npz. At the corpus size here (hundreds/week,
low-tens-of-thousands/year) an exact numpy cosine scan is fast and dependency-
light — no managed vector DB needed. Vectors are L2-normalized so cosine == dot.

Keyed by embedding_id (== paper_id for abstract embeddings; paper_id#chunk once
chunked OA full text is added). Read-only at Space startup; written by score.py.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np


class VectorIndex:
    def __init__(self, dim: int | None = None):
        self.dim = dim
        self._ids: list[str] = []
        self._rows: list[np.ndarray] = []   # staged adds before finalize
        self._matrix: np.ndarray | None = None  # finalized (n, dim), normalized

    @staticmethod
    def _normalize(v: np.ndarray) -> np.ndarray:
        v = np.asarray(v, dtype=np.float32)
        n = np.linalg.norm(v)
        return v / n if n else v

    def add(self, embedding_id: str, vector) -> None:
        v = self._normalize(vector)
        if self.dim is None:
            self.dim = v.shape[0]
        elif v.shape[0] != self.dim:
            raise ValueError(f"dim mismatch: got {v.shape[0]}, expected {self.dim}")
        self._ids.append(embedding_id)
        self._rows.append(v)
        self._matrix = None  # invalidate

    def _finalize(self) -> None:
        if self._matrix is None:
            self._matrix = (np.vstack(self._rows).astype(np.float32)
                            if self._rows else np.zeros((0, self.dim or 0), np.float32))

    def search(self, query_vector, k: int = 5, exclude: set[str] | None = None
               ) -> list[tuple[str, float]]:
        """Top-k by cosine similarity. Returns [(embedding_id, score), ...]."""
        self._finalize()
        if self._matrix.shape[0] == 0:
            return []
        q = self._normalize(query_vector)
        sims = self._matrix @ q
        order = np.argsort(-sims)
        out: list[tuple[str, float]] = []
        for i in order:
            eid = self._ids[i]
            if exclude and eid in exclude:
                continue
            out.append((eid, float(sims[i])))
            if len(out) >= k:
                break
        return out

    def __len__(self) -> int:
        return len(self._ids)

    def save(self, path: str | Path) -> None:
        self._finalize()
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        np.savez(path, ids=np.array(self._ids, dtype=object), vectors=self._matrix)

    @classmethod
    def load(cls, path: str | Path) -> "VectorIndex":
        data = np.load(Path(path), allow_pickle=True)
        idx = cls()
        idx._ids = list(data["ids"])
        idx._matrix = data["vectors"].astype(np.float32)
        idx.dim = idx._matrix.shape[1] if idx._matrix.size else None
        return idx
