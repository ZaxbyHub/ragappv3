"""B04 acceptance check (issue #693) — 'partial' rows must count as in-flight.

Frozen spec for the fix (RED at base dc894f49).

``test_partial_row_is_in_flight_duplicate`` (AC6):
``DocumentProcessor._check_duplicate_in_flight`` — the duplicate collapse the
async upload route relies on — filters ``status IN ('pending','processing',
'indexed')`` and misses ``'partial'``. A file whose ingestion ended in the
partial state therefore slips past the in-flight check and is ingested a
second time concurrently. The 'partial' row must be matched. RED evidence:
``assert None is not None``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


class _FakeEmbeddingService:
    """Embedding double (b02 harness pattern): ``embed_batch -> (vectors, [])``."""

    MAX_TEXT_LENGTH = 8192
    embedding_doc_prefix = ""

    def __init__(self, dim: int) -> None:
        self._dim = dim

    async def embed_batch(self, texts, fail_fast=False):  # noqa: ANN001, ANN202
        return [[[(i + 1) * 0.25 for i in range(self._dim)] for _ in texts], []]


async def test_partial_row_is_in_flight_duplicate(tmp_path: Path) -> None:
    """AC6: a status='partial' row with the same hash is an in-flight duplicate.

    Calls the REAL ``_check_duplicate_in_flight`` through a real pooled
    connection against a seeded 'partial' row with the queried hash — no
    vector store connection is needed (the check is pure SQLite).
    """
    from app.models.database import get_pool, init_db
    from app.services.document_processor import DocumentProcessor
    from app.services.vector_store import VectorStore

    tmp = tmp_path / "store"
    tmp.mkdir()
    db_path = str(tmp / "app.db")
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO vaults (name) VALUES ('v6')")
    vid = int(conn.execute("SELECT id FROM vaults LIMIT 1").fetchone()[0])
    conn.execute(
        "INSERT INTO files (vault_id, file_path, file_name, file_hash, file_size, status) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (vid, str(tmp / "p.txt"), "p.txt", "deadbeefdead", 5, "partial"),
    )
    conn.commit()
    conn.close()

    pool = get_pool(db_path, max_size=3)
    processor = DocumentProcessor(
        pool=pool,
        embedding_service=_FakeEmbeddingService(8),
        vector_store=VectorStore(db_path=tmp / "lancedb"),
    )
    with pool.connection() as conn:
        row = processor._check_duplicate_in_flight(
            "deadbeefdead", conn, vault_id=vid
        )

    assert row is not None
