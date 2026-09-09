"""Issue #513 acceptance check C15 (AC15 / INGEST-018).

An old-generation failed-chunk retry that is paused before embedding while a
NEWER generation for the same file completes must publish nothing for the old
generation: no old-generation vector write and no chunk-count mutation on the
new generation.

Discriminating: pre-fix, ``retry_failed_chunks`` reads the file row once
(no generation check), awaits the embedding with the old hash captured, then
writes the vector record built from the OLD generation's file_hash and bumps
``files.chunk_count`` on the row that now belongs to the NEW generation ->
FAIL. Post-fix a generation re-check after the await suppresses the stale
write and counter mutation -> PASS.
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

OLD_HASH = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
NEW_HASH = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


def main() -> int:
    os.environ.setdefault("ADMIN_SECRET_TOKEN", "test-secret")
    os.environ.setdefault("USERS_ENABLED", "false")
    os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret-key-for-testing-only")
    os.environ.setdefault("REDIS_URL", "")

    tmp_dir = tempfile.mkdtemp(prefix="c15_513_")
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

        file_id = 70
        new_generation_chunk_count = 7

        conn = pool.get_connection()
        try:
            conn.execute(
                "INSERT INTO vaults (name, description, visibility, created_at, "
                "updated_at) VALUES ('V', 'v', 'private', '2026-01-01', '2026-01-01')"
            )
            vault_id = conn.execute(
                "SELECT id FROM vaults WHERE name='V'"
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO files (id, file_path, file_name, file_hash, "
                "file_size, file_type, vault_id, status, chunk_count, "
                "chunks_failed, created_at, modified_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'indexed', 3, 1, ?, ?)",
                (
                    file_id,
                    "C:/c15/old.doc",
                    "old.doc",
                    OLD_HASH,
                    10,
                    ".doc",
                    vault_id,
                    "2026-01-01",
                    "2026-01-01",
                ),
            )
            meta = json.dumps(
                {
                    "raw_text": "failed old-generation chunk",
                    "chunk_index": 2,
                    "chunk_scale": "default",
                    "chunk_uid": f"{file_id}_2",
                    "chunk_position": 2,
                    "parent_window_start": None,
                    "parent_window_end": None,
                    "page_number": None,
                    "chunk_bbox": None,
                    "total_chunks": 3,
                }
            )
            conn.execute(
                "INSERT INTO failed_chunks (file_id, chunk_index, chunk_text, "
                "chunk_metadata) VALUES (?, 2, 'failed old-generation chunk', ?)",
                (file_id, meta),
            )
            conn.commit()
        finally:
            pool.release_connection(conn)

        class _PausingEmbeddingService:
            """Signals when the retry reaches the embedding step, then parks
            until the scenario releases it (controllable pause point)."""

            def __init__(self):
                self.reached = asyncio.Event()
                self.release = asyncio.Event()

            async def embed_batch(self, texts, fail_fast=False):
                self.reached.set()
                await self.release.wait()
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

        emb = _PausingEmbeddingService()
        vs = _FakeVectorStore()
        proc = DocumentProcessor(
            vector_store=vs, embedding_service=emb, pool=pool
        )

        async def scenario():
            task = asyncio.create_task(proc.retry_failed_chunks(file_id))
            await asyncio.wait_for(emb.reached.wait(), timeout=5)
            # While the old-generation retry is parked before embedding, a NEW
            # generation completes for this file: new hash, new chunk count.
            c = pool.get_connection()
            try:
                c.execute(
                    "UPDATE files SET file_hash = ?, chunk_count = ?, "
                    "chunks_failed = 0 WHERE id = ?",
                    (NEW_HASH, new_generation_chunk_count, file_id),
                )
                c.commit()
            finally:
                pool.release_connection(c)
            emb.release.set()
            return await asyncio.wait_for(task, timeout=5)

        orig_safe_order = settings.reupload_safe_order
        settings.reupload_safe_order = True
        try:
            scenario_error = None
            try:
                asyncio.run(scenario())
            except Exception as exc:  # noqa: BLE001
                scenario_error = exc
            if scenario_error is not None:
                print(
                    "C15 CHECK: FAIL: retry raised "
                    f"{type(scenario_error).__name__}: {scenario_error}"
                )
                return 1
        finally:
            settings.reupload_safe_order = orig_safe_order

        stale_writes = [
            r["id"] for r in vs.added if OLD_HASH[:8] in str(r["id"])
        ]
        conn = pool.get_connection()
        try:
            row = conn.execute(
                "SELECT chunk_count, file_hash FROM files WHERE id = ?",
                (file_id,),
            ).fetchone()
            chunk_count = row["chunk_count"]
        finally:
            pool.release_connection(conn)

        if stale_writes:
            print(
                "C15 CHECK: FAIL: old-generation vector records were written "
                f"after a newer generation completed: {stale_writes}"
            )
            return 1
        if chunk_count != new_generation_chunk_count:
            print(
                "C15 CHECK: FAIL: new generation's chunk_count mutated by the "
                f"stale retry: {chunk_count} != {new_generation_chunk_count}"
            )
            return 1

        print("C15 CHECK: PASS")
        return 0
    except Exception as exc:  # noqa: BLE001 - never crash without a verdict
        print(f"C15 CHECK: FAIL: unexpected error: {type(exc).__name__}: {exc}")
        return 1
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())


def test_c15_stale_generation_retry():
    assert main() == 0
