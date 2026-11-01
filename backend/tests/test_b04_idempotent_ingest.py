"""B04 acceptance checks (issue #693) — idempotent ingest + no scan double-enqueue.

Frozen spec for the fix (RED at base dc894f49). Two contracts:

1. ``test_same_hash_reprocess_does_not_duplicate_vectors`` (AC1) —
   reprocessing a file whose content hash is UNCHANGED must leave exactly one
   vector row per chunk. At the base the same-hash reprocess appends duplicate
   LanceDB rows: the chunk record id is ``{file_id}_{hash[:8]}_{scale}_{idx}``
   (``document_processor.py``) but LanceDB does not enforce record-id
   uniqueness, and the pre-add delete-by-prefix is a no-op
   (``vector_store.py``), so pass 2 and pass 3 each append a second and third
   copy of every chunk. RED evidence: three passes -> ``assert 3 == 1``.

2. ``test_queued_path_not_reenqueued_by_scan`` (AC7) — a file path that is
   already queued (e.g. enqueued by the email path with ``source="email"``,
   which carries no ``file_id``) must NOT be enqueued a second time by the
   FileWatcher scan. At the base ``BackgroundProcessor.enqueue`` inserts a new
   ``jobs`` row per call in lease mode with no path-level dedupe, and
   ``FileWatcher.scan_once`` cannot see the queued path (no ``files`` row
   exists yet), so one attachment ends up with TWO ingestion jobs. RED
   evidence: ``assert 2 == 1``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.models.database import run_migrations  # noqa: E402


class _FakeEmbeddingService:
    """Embedding double (b02 harness pattern): ``embed_batch -> (vectors, [])``."""

    MAX_TEXT_LENGTH = 8192
    embedding_doc_prefix = ""

    def __init__(self, dim: int) -> None:
        self._dim = dim

    async def embed_batch(self, texts, fail_fast=False):  # noqa: ANN001, ANN202
        return [[[(i + 1) * 0.25 for i in range(self._dim)] for _ in texts], []]


def _first_vault_id(db_path: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return int(conn.execute("SELECT id FROM vaults LIMIT 1").fetchone()[0])
    finally:
        conn.close()


async def test_same_hash_reprocess_does_not_duplicate_vectors(
    tmp_path: Path,
) -> None:
    """AC1: same-hash reprocess of an indexed file must not add vector rows.

    Drives the REAL ``DocumentProcessor.process_existing_file`` (the async
    upload worker's entry point) three times against one indexed ``files``
    row, with parsing/hash/parent-windows stubbed to a constant hash and a
    single chunk (the b02 ``_ingest`` pattern). Idempotent semantics: the
    third pass must still leave exactly ONE vector row for the file.
    """
    from app.config import settings
    from app.models.database import get_pool, init_db, run_migrations
    from app.services.chunking import ProcessedChunk
    from app.services.document_artifacts import ParsedDocument
    from app.services.document_processor import DocumentProcessor
    from app.services.vector_store import VectorStore

    tmp = tmp_path / "store"
    tmp.mkdir()
    db_path = str(tmp / "app.db")
    init_db(db_path)
    run_migrations(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO vaults (name) VALUES ('v1')")
    vid = int(conn.execute("SELECT id FROM vaults LIMIT 1").fetchone()[0])
    conn.execute(
        "INSERT INTO files (vault_id, file_path, file_name, file_hash, file_size, status) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (vid, str(tmp / "doc.txt"), "doc.txt", "aaaa1111aaaa", 12, "indexed"),
    )
    file_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.commit()
    conn.close()

    src = tmp / "doc.txt"
    src.write_text("idempotent probe content", encoding="utf-8")

    pool = get_pool(db_path, max_size=3)
    store = VectorStore(db_path=tmp / "lancedb")
    await store.init_table(8)
    processor = DocumentProcessor(
        pool=pool, embedding_service=_FakeEmbeddingService(8), vector_store=store
    )
    chunk = ProcessedChunk(
        text="idempotent probe content",
        metadata={
            "chunk_scale": "default",
            "raw_text": "idempotent probe content",
        },
        chunk_index=0,
    )
    with (
        patch.object(settings, "data_dir", tmp),
        patch.object(settings, "wiki_enabled", False),
        patch.object(settings, "wiki_compile_on_ingest", False),
        patch.object(
            processor,
            "_process_document_file",
            new=AsyncMock(
                return_value=(
                    [chunk],
                    "idempotent probe content",
                    ParsedDocument(atoms=()),
                )
            ),
        ),
        patch(
            "app.services.document_processor.compute_file_hash",
            return_value="aaaa1111aaaa",
        ),
        patch("app.services.document_processor.compute_parent_windows"),
    ):
        for _ in range(3):
            await processor.process_existing_file(
                file_id=file_id, file_path=str(src), vault_id=vid
            )

    n = await store.count_by_file(str(file_id))
    assert n == 1


async def test_queued_path_not_reenqueued_by_scan(tmp_path: Path) -> None:
    """AC7: the scanner must not enqueue a path that already holds a job.

    One attachment arrives via the email path (``source="email"``, no
    ``file_id`` — no ``files`` row exists yet), then a FileWatcher scan runs
    over the same vault uploads directory. Exactly ONE ingestion job must
    exist for that path afterwards.
    """
    from app.config import settings
    from app.models.database import SQLiteConnectionPool, init_db
    from app.services.background_tasks import BackgroundProcessor
    from app.services.file_watcher import FileWatcher
    from app.services.job_lease import ensure_jobs_schema

    db_path = str(tmp_path / "app.db")
    init_db(db_path)
    run_migrations(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO vaults (name) VALUES ('v7')")
    conn.commit()
    ensure_jobs_schema(conn)
    conn.close()
    vid = _first_vault_id(db_path)

    pool = SQLiteConnectionPool(db_path, max_size=4)
    try:
        with (
            patch.object(settings, "data_dir", tmp_path),
            patch.object(settings, "ingestion_job_lease_enabled", True),
        ):
            att = settings.vault_uploads_dir(vid) / "att.txt"
            att.write_text("attachment", encoding="utf-8")

            bp = BackgroundProcessor(pool=pool, retry_delay=0.05)
            await bp.enqueue(str(att), vault_id=vid, source="email")

            fw = FileWatcher(processor=bp, pool=pool)
            await fw.scan_once()
    finally:
        pool.close_all()

    conn = sqlite3.connect(db_path)
    try:
        n = int(
            conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE queue = 'ingestion'"
            ).fetchone()[0]
        )
    finally:
        conn.close()
    assert n == 1
