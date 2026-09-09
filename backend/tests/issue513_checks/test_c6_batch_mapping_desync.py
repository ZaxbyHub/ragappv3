"""Issue #513 acceptance check C6 (AC6 / INGEST-007).

When one embedding batch fails mid-flight and the live
``settings.embedding_batch_size`` changes between the embed call and the
caller's failure-position mapping, exactly the originally-failed TEXT
positions may be dropped/retried and every other chunk must index.

Discriminating: pre-fix, ``document_processor`` re-reads
``settings.embedding_batch_size`` to map batch indices -> chunk ranges, so a
batch-size change during the embed call mis-maps the failed positions, kept
embeddings still contain None placeholders, and the ingest aborts to
status='error' -> FAIL. Post-fix the per-text None placeholders identify the
failed positions regardless of batching -> indexed with the right chunks.
"""

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT / "backend"))


def main() -> int:
    os.environ.setdefault("ADMIN_SECRET_TOKEN", "test-secret")
    os.environ.setdefault("USERS_ENABLED", "false")
    os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret-key-for-testing-only")
    os.environ.setdefault("REDIS_URL", "")

    tmp_dir = tempfile.mkdtemp(prefix="c6_513_")
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
                "updated_at) VALUES ('V', 'v', 'private', '2026-01-01', '2026-01-01')"
            )
            vault_id = conn.execute(
                "SELECT id FROM vaults WHERE name='V'"
            ).fetchone()[0]
            conn.commit()
        finally:
            pool.release_connection(conn)

        # 5 CREATE TABLE blocks -> 5 chunks. Batching with the ORIGINAL size 2:
        # batch 0 = chunks 0,1 (ok), batch 1 = chunks 2,3 (FAILS), batch 2 = chunk 4.
        n_tables = 5
        sql_text = "\n".join(
            f"CREATE TABLE t{i} (id INTEGER, name TEXT);" for i in range(n_tables)
        )
        sql_path = Path(tmp_dir) / "schema.sql"
        sql_path.write_text(sql_text, encoding="utf-8")

        original_batch_size = settings.embedding_batch_size

        class _DesyncEmbeddingService:
            """Returns per-text None for the originally-failed batch (texts 2,3)
            and mutates the live batch-size setting before returning, exactly as
            if an admin changed it while the embed call was in flight."""

            async def embed_batch(self, texts, batch_size=None, fail_fast=False):
                embeddings = []
                for i in range(len(texts)):
                    if i in (2, 3):  # original batch 1 under batch_size=2
                        embeddings.append(None)
                    else:
                        embeddings.append([float(i), 1.0, 0.5])
                # Live setting change between the embed call and the mapping.
                settings.embedding_batch_size = 4
                return (embeddings, [1])  # batch index 1 failed

        class _FakeVectorStore:
            def __init__(self):
                self.added = []

            async def init_table(self, dim):
                return None

            async def add_chunks(self, records):
                self.added.extend(records)
                return None

            async def delete_old_generation_by_file(self, file_id, hash_prefix):
                return 0

            async def count_by_file(self, file_id):
                return sum(1 for r in self.added if r["file_id"] == file_id)

        vs = _FakeVectorStore()
        emb = _DesyncEmbeddingService()
        proc = DocumentProcessor(
            chunk_size_chars=500,
            chunk_overlap_chars=0,
            vector_store=vs,
            embedding_service=emb,
            pool=pool,
        )

        settings.embedding_batch_size = 2
        try:
            ingest_error = None
            try:
                asyncio.run(proc.process_file(str(sql_path), vault_id=vault_id))
            except Exception as exc:  # noqa: BLE001 - pre-fix aborts the ingest
                ingest_error = exc

            if ingest_error is not None:
                print(
                    "C6 CHECK: FAIL: ingest aborted after embedding partial "
                    "failure with a mid-flight batch-size change "
                    f"({type(ingest_error).__name__}: {ingest_error}); "
                    "failed-text positions were not mapped from the embed "
                    "results"
                )
                return 1

            conn = pool.get_connection()
            try:
                row = conn.execute(
                    "SELECT id, status, chunk_count, chunks_failed FROM files "
                    "WHERE file_path = ?",
                    (str(sql_path),),
                ).fetchone()
                if row is None:
                    print("C6 CHECK: FAIL: files row not created")
                    return 1
                if row["status"] != "indexed":
                    print(
                        "C6 CHECK: FAIL: expected status 'indexed', got "
                        f"'{row['status']}'"
                    )
                    return 1
                failed_positions = {
                    r[0]
                    for r in conn.execute(
                        "SELECT chunk_index FROM failed_chunks WHERE file_id = ?",
                        (row["id"],),
                    ).fetchall()
                }
                chunks_failed = row["chunks_failed"]
            finally:
                pool.release_connection(conn)

            expected_failed = {2, 3}
            if failed_positions != expected_failed:
                print(
                    "C6 CHECK: FAIL: retried/marked failed positions "
                    f"{sorted(failed_positions)} != originally-failed text "
                    f"positions {sorted(expected_failed)}"
                )
                return 1
            if chunks_failed != len(expected_failed):
                print(
                    "C6 CHECK: FAIL: files.chunks_failed = "
                    f"{chunks_failed}, expected {len(expected_failed)}"
                )
                return 1

            indexed_tables = sorted(
                r["text"].split()[2]
                for r in vs.added
                if "CREATE TABLE" in r["text"]
            )
            expected_indexed = ["t0", "t1", "t4"]
            if indexed_tables != expected_indexed:
                print(
                    "C6 CHECK: FAIL: indexed chunk texts "
                    f"{indexed_tables} != non-failed texts {expected_indexed}"
                )
                return 1

            print("C6 CHECK: PASS")
            return 0
        finally:
            settings.embedding_batch_size = original_batch_size
            pool.close_all()
    except Exception as exc:  # noqa: BLE001 - never crash without a verdict
        print(f"C6 CHECK: FAIL: unexpected error: {type(exc).__name__}: {exc}")
        return 1
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())


def test_c6_batch_mapping_desync():
    assert main() == 0
