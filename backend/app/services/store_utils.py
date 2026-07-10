"""Shared helpers for store/service modules.

These were previously copy-pasted across tag_store/folder_store (vault-file-id
filtering) and across memory_store/context_distiller/chunking (cosine
similarity, with behavioral divergence — only memory_store guarded length
mismatch). Centralizing them removes the duplication and the silent
truncation bug in the unguarded copies (F3-3, F3-5).
"""

from __future__ import annotations

import math
import sqlite3
from typing import Any, List


def vault_file_ids(db: sqlite3.Connection, vault_id: int, file_ids: list[int]) -> list[int]:
    """Return the subset of ``file_ids`` that belong to ``vault_id``.

    Shared by TagStore and FolderStore (previously duplicated byte-for-byte).
    """
    if not file_ids:
        return []
    placeholders = ",".join("?" * len(file_ids))
    rows = db.execute(
        f"SELECT id FROM files WHERE vault_id = ? AND id IN ({placeholders})",
        (vault_id, *file_ids),
    ).fetchall()
    return [r["id"] for r in rows]


def cosine_similarity(a: List[float], b: List[float]) -> float:
    """Cosine similarity between two equal-length float vectors.

    Returns 0.0 for length mismatch or zero vectors so callers don't have to
    special-case those edge conditions. The strict length guard is the
    behavior previously only in memory_store; the unguarded copies in
    context_distiller/chunking silently truncated to the shorter vector via
    ``zip()`` — this shared helper fixes that divergence (F3-5).
    """
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


__all__ = ["vault_file_ids", "cosine_similarity", "DualPoolMixin"]


class DualPoolMixin:
    """Adapter for the two pool interfaces used across retrieval services.

    Production uses ``SQLiteConnectionPool`` (``get_connection`` /
    ``release_connection``); tests inject a ``queue.Queue``-style pool
    (``get`` / ``put``). This mixin was previously copy-pasted as
    ``_acquire``/``_release`` in both KMSRetrievalService and
    WikiRetrievalService (F3-4). Mix into any service that stores its pool as
    ``self._pool``.
    """

    _pool: Any

    def _acquire(self) -> sqlite3.Connection:
        if hasattr(self._pool, "get_connection"):
            return self._pool.get_connection()
        return self._pool.get()

    def _release(self, conn: sqlite3.Connection) -> None:
        if hasattr(self._pool, "release_connection"):
            self._pool.release_connection(conn)
        else:
            self._pool.put(conn)
