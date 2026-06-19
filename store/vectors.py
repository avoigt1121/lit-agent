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
        self._id_set: set[str] | None = None    # lazy membership cache (see __contains__)

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
        self._matrix = None   # invalidate finalized cache
        self._id_set = None   # invalidate membership cache

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

    def __contains__(self, embedding_id: str) -> bool:
        """Membership test so a resumable backfill can skip already-indexed ids.

        A census reloads the index, reprocesses the current (partial) window, and
        re-embeds its papers — without this guard `add()` would append duplicate
        rows for ids already present (it does not de-dupe). Built lazily and cached;
        invalidated on add()."""
        if self._id_set is None or len(self._id_set) != len(self._ids):
            self._id_set = set(self._ids)
        return embedding_id in self._id_set

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
        matrix = data["vectors"].astype(np.float32)
        # Seed the staging buffer from the loaded matrix so add()-after-load
        # EXTENDS the index instead of replacing it. _finalize() rebuilds
        # _matrix from _rows, so a resumable backfill (load -> add -> save) must
        # find the loaded vectors already staged in _rows, or it silently drops
        # them and save() persists only the newly-added ones.
        idx._rows = [row for row in matrix]
        idx._matrix = matrix  # keep the finalized cache valid until the next add()
        idx.dim = matrix.shape[1] if matrix.size else None
        return idx
