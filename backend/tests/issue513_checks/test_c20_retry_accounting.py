"""Issue #513 acceptance check C20 (AC20 / INGEST-004).

Chunk-retry accounting must be idempotent and correct across a cleanup
failure: (1) a retry whose SQLite cleanup fails after the vector write, then
a recovery retry, must end with the CORRECT chunk_count (original + recovered
vectors) and zero failed rows; (2) a third retry must not increment again;
(3) two concurrent retries of the same single failure must recover exactly
one vector and increment exactly once.

Discriminating: pre-fix, cleanup-failure recovery only reconciles rows and
never corrects chunk_count (delta increment leaves the count at its
pre-retry value), and concurrent retries double-write the vector and double
increment -> FAIL.
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT / "backend"))

FILE_ID = 70
CONC_FILE_ID = 71
FILE_HASH = "abc12345" + "0" * 56
CONC_FILE_HASH = "def67890" + "0" * 56


def _seed_failed_chunk(conn, file_id, chunk_index, total_chunks):
    meta = json.dumps(
        {
            "raw_text": f"failed chunk {chunk_index} of {file_id}",
            "chunk_index": chunk_index,
            "chunk_scale": "default",
            "chunk_uid": f"{file_id}_{chunk_index}",
            "chunk_position": chunk_index,
            "parent_window_start": None,
            "parent_window_end": None,
            "page_number": None,
            "chunk_bbox": None,
            "total_chunks": total_chunks,
        }
    )
    conn.execute(
        "INSERT INTO failed_chunks (file_id, chunk_index, chunk_text, "
        "chunk_metadata) VALUES (?, ?, ?, ?)",
        (file_id, chunk_index, f"failed chunk {chunk_index} of {file_id}", meta),
    )


def main() -> int:
    os.environ.setdefault("ADMIN_SECRET_TOKEN", "test-secret")
    os.environ.setdefault("USERS_ENABLED", "false")
    os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret-key-for-testing-only")
    os.environ.setdefault("REDIS_URL", "")

    tmp_dir = tempfile.mkdtemp(prefix="c20_513_")
    try:
        os.environ["DATA_DIR"] = tmp_dir

        from app.config import settings
        from app.models.database import SQLiteConnectionPool, init_db, run_migrations
        from app.services.document_processor import DocumentProcessor

        settings.data_dir = Path(tmp_dir)
        db_path = str(Path(tmp_dir) / "app.db")
        init_db(db_path)
        run_migrations(db_path)
        pool = SQLiteConnectionPool(db_path, max_size=5)

        conn = pool.get_connection()
        try:
            conn.execute(
                "INSERT INTO vaults (name, description, visibility, created_at, "
                "updated_at) VALUES ('V', 'v', 'private', '2026-01-01', "
                "'2026-01-01')"
            )
            vault_id = conn.execute(
                "SELECT id FROM vaults WHERE name='V'"
            ).fetchone()[0]
            for fid, base_count in ((FILE_ID, 3), (CONC_FILE_ID, 3)):
                conn.execute(
                    "INSERT INTO files (id, file_path, file_name, file_hash, "
                    "file_size, file_type, vault_id, status, chunk_count, "
                    "chunks_failed, created_at, modified_at) VALUES "
                    "(?, ?, ?, ?, 10, '.txt', ?, 'indexed', ?, 0, '2026-01-01', "
                    "'2026-01-01')",
                    (
                        fid,
                        f"C:/c20/f{fid}.txt",
                        f"f{fid}.txt",
                        FILE_HASH if fid == FILE_ID else CONC_FILE_HASH,
                        vault_id,
                        base_count,
                    ),
                )
            # File 70: two failed chunks (indices 1 and 3).
            _seed_failed_chunk(conn, FILE_ID, 1, 4)
            _seed_failed_chunk(conn, FILE_ID, 3, 4)
            # File 71: one failed chunk (index 1) for the concurrency probe.
            _seed_failed_chunk(conn, CONC_FILE_ID, 1, 4)
            conn.commit()
        finally:
            pool.release_connection(conn)

        class _OkEmbedding:
            async def embed_batch(self, texts, fail_fast=False):
                return ([[0.1, 0.2, 0.3]] * len(texts), [])

        class _SlowEmbedding:
            """Slow enough that two concurrent retries both pass the
            already-indexed pre-check before either writes the vector."""

            async def embed_batch(self, texts, fail_fast=False):
                await asyncio.sleep(0.05)
                return ([[0.1, 0.2, 0.3]] * len(texts), [])

        class _FakeVectorStore:
            def __init__(self):
                self.added = []
                self._existing = set()

            async def get_chunks_by_uid(self, uids):
                return [{"id": u} for u in uids if u in self._existing]

            async def add_chunks(self, records):
                self.added.extend(records)
                for r in records:
                    self._existing.add(r["id"])
                return None

        import sqlite3

        class _FailingCleanupConn:
            """Delegates everything, but raises once on the failed-chunks
            cleanup DELETE (after the vector write succeeded)."""

            def __init__(self, wrapped):
                self._wrapped = wrapped
                self._armed = True

            def execute(self, sql, params=()):
                if self._armed and "DELETE FROM failed_chunks" in sql:
                    self._armed = False
                    raise sqlite3.OperationalError(
                        "injected cleanup failure after vector write"
                    )
                return self._wrapped.execute(sql, params)

            def __getattr__(self, name):
                return getattr(self._wrapped, name)

        class _CleanupFailingPool:
            """Wraps the second get_connection() (the cleanup phase of retry
            #1) so its DELETE fails while everything else stays real."""

            def __init__(self, inner):
                self.inner = inner
                self.calls = 0

            def get_connection(self):
                self.calls += 1
                conn = self.inner.get_connection()
                if self.calls == 2:
                    return _FailingCleanupConn(conn)
                return conn

            def release_connection(self, conn):
                self.inner.release_connection(
                    conn._wrapped
                    if isinstance(conn, _FailingCleanupConn)
                    else conn
                )

        def _files_row(file_id):
            conn = pool.get_connection()
            try:
                row = conn.execute(
                    "SELECT chunk_count, chunks_failed FROM files WHERE id = ?",
                    (file_id,),
                ).fetchone()
                failed = conn.execute(
                    "SELECT COUNT(*) FROM failed_chunks WHERE file_id = ?",
                    (file_id,),
                ).fetchone()[0]
                return row["chunk_count"], row["chunks_failed"], failed
            finally:
                pool.release_connection(conn)

        vs = _FakeVectorStore()
        orig_safe_order = settings.reupload_safe_order
        settings.reupload_safe_order = True
        try:
            # -- Retry #1: vector write succeeds, SQLite cleanup fails. -------
            proc1 = DocumentProcessor(
                vector_store=vs, embedding_service=_OkEmbedding(),
                pool=_CleanupFailingPool(pool),
            )
            asyncio.run(proc1.retry_failed_chunks(FILE_ID))
            written_70 = [r for r in vs.added if r["file_id"] == str(FILE_ID)]
            if len(written_70) != 2:
                print(
                    "C20 CHECK: FAIL: retry #1 did not write both failed-chunk "
                    f"vectors before the cleanup failure (wrote {len(written_70)})"
                )
                return 1

            # -- Retry #2: recovery must yield correct count + zero rows. ----
            proc2 = DocumentProcessor(
                vector_store=vs, embedding_service=_OkEmbedding(), pool=pool
            )
            asyncio.run(proc2.retry_failed_chunks(FILE_ID))
            chunk_count, chunks_failed, failed_rows = _files_row(FILE_ID)
            if failed_rows != 0 or chunks_failed != 0:
                print(
                    "C20 CHECK: FAIL: failed-chunk rows not cleared after "
                    f"recovery retry (rows={failed_rows}, "
                    f"chunks_failed={chunks_failed})"
                )
                return 1
            if chunk_count != 5:
                print(
                    "C20 CHECK: FAIL: chunk_count not corrected after "
                    "cleanup-failure recovery: "
                    f"{chunk_count} != 5 (3 original + 2 recovered vectors)"
                )
                return 1

            # -- Retry #3: idempotent, must not increment again. -------------
            asyncio.run(proc2.retry_failed_chunks(FILE_ID))
            chunk_count, _cf, _fr = _files_row(FILE_ID)
            if chunk_count != 5:
                print(
                    "C20 CHECK: FAIL: third retry incremented chunk_count again: "
                    f"{chunk_count} != 5"
                )
                return 1

            # -- Concurrent double-retry of one failure. ----------------------
            proc3 = DocumentProcessor(
                vector_store=vs, embedding_service=_SlowEmbedding(), pool=pool
            )

            async def double_retry():
                await asyncio.gather(
                    proc3.retry_failed_chunks(CONC_FILE_ID),
                    proc3.retry_failed_chunks(CONC_FILE_ID),
                )

            asyncio.run(double_retry())
            added_71 = [r for r in vs.added if r["file_id"] == str(CONC_FILE_ID)]
            chunk_count_71, _cf, _fr = _files_row(CONC_FILE_ID)
            if len(added_71) != 1:
                print(
                    "C20 CHECK: FAIL: concurrent double-retry wrote "
                    f"{len(added_71)} vector records for one failed chunk "
                    "(expected exactly 1)"
                )
                return 1
            if chunk_count_71 != 4:
                print(
                    "C20 CHECK: FAIL: concurrent double-retry incremented "
                    f"chunk_count to {chunk_count_71} (expected exactly one "
                    "increment to 4)"
                )
                return 1

            print("C20 CHECK: PASS")
            return 0
        finally:
            settings.reupload_safe_order = orig_safe_order
            pool.close_all()
    except Exception as exc:  # noqa: BLE001 - never crash without a verdict
        print(f"C20 CHECK: FAIL: unexpected error: {type(exc).__name__}: {exc}")
        return 1
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())


def test_c20_retry_accounting():
    assert main() == 0
