"""Issue #513 acceptance check C26 (AC26 / INGEST-006 divergence).

Both ingest entry points (``process_file`` and ``process_existing_file``)
run with identical input and identical flags (KMS + compile-on-ingest
enabled, wiki gating identical, fakes for embedding/vector) must produce
equivalent finalization: same status transitions, and both enqueue a wiki
job AND a KMS ingest job under the same gating.

Discriminating: pre-fix, ``process_existing_file`` lacks the KMS
enqueue block that ``process_file`` has (document_processor finalization
divergence), so the upload path never enqueues a KMS compile-on-ingest job
-> FAIL. Post-fix both entry points finalize identically -> PASS.
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

    tmp_dir = tempfile.mkdtemp(prefix="c26_513_")
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
            for name in ("VA", "VB"):
                conn.execute(
                    "INSERT INTO vaults (name, description, visibility, "
                    "created_at, updated_at) VALUES (?, 'v', 'private', "
                    "'2026-01-01', '2026-01-01')",
                    (name,),
                )
            conn.commit()
            vault_a = conn.execute(
                "SELECT id FROM vaults WHERE name='VA'"
            ).fetchone()[0]
            vault_b = conn.execute(
                "SELECT id FROM vaults WHERE name='VB'"
            ).fetchone()[0]
        finally:
            pool.release_connection(conn)

        # Identical input content for both entry points (same bytes, same
        # schema shape); separate vaults avoid the duplicate-hash check.
        sql_text = "\n".join(
            f"CREATE TABLE t{i} (id INTEGER, name TEXT);" for i in range(5)
        )
        path_a = Path(tmp_dir) / "doc_a.sql"
        path_b = Path(tmp_dir) / "doc_b.sql"
        path_a.write_text(sql_text + "\n", encoding="utf-8")
        path_b.write_text(sql_text + "\n", encoding="utf-8")

        class _OkEmbedding:
            async def embed_batch(self, texts, fail_fast=False):
                return ([[0.1, 0.2, 0.3]] * len(texts), [])

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

        saved = {
            "kms_enabled": settings.kms_enabled,
            "kms_compile_on_ingest": settings.kms_compile_on_ingest,
            "wiki_enabled": settings.wiki_enabled,
            "wiki_compile_on_ingest": settings.wiki_compile_on_ingest,
        }
        settings.kms_enabled = True
        settings.kms_compile_on_ingest = True
        settings.wiki_enabled = True
        settings.wiki_compile_on_ingest = True

        vs = _FakeVectorStore()
        proc = DocumentProcessor(
            chunk_size_chars=500,
            chunk_overlap_chars=0,
            vector_store=vs,
            embedding_service=_OkEmbedding(),
            pool=pool,
        )
        try:
            # -- Entry point 1: process_file (the scan/sync path). -----------
            error_a = None
            try:
                result_a = asyncio.run(proc.process_file(str(path_a), vault_id=vault_a))
            except Exception as exc:  # noqa: BLE001
                error_a = exc
            if error_a is not None:
                print(
                    "C26 CHECK: FAIL: process_file failed: "
                    f"{type(error_a).__name__}: {error_a}"
                )
                return 1
            file_a = result_a.file_id

            # -- Entry point 2: process_existing_file (the async upload path).
            conn = pool.get_connection()
            try:
                import hashlib

                file_b_hash = hashlib.sha256(path_b.read_bytes()).hexdigest()
                conn.execute(
                    "INSERT INTO files (file_path, file_name, file_hash, "
                    "file_size, file_type, vault_id, source, status, "
                    "created_at, modified_at) VALUES (?, ?, ?, ?, '.sql', ?, "
                    "'upload', 'pending', '2026-01-01', '2026-01-01')",
                    (
                        str(path_b),
                        "doc_b.sql",
                        file_b_hash,
                        path_b.stat().st_size,
                        vault_b,
                    ),
                )
                file_b = conn.execute(
                    "SELECT id FROM files WHERE file_path = ?", (str(path_b),)
                ).fetchone()[0]
                conn.commit()
            finally:
                pool.release_connection(conn)

            error_b = None
            try:
                result_b = asyncio.run(
                    proc.process_existing_file(
                        file_id=file_b, file_path=str(path_b), vault_id=vault_b
                    )
                )
            except Exception as exc:  # noqa: BLE001
                error_b = exc
            if error_b is not None:
                print(
                    "C26 CHECK: FAIL: process_existing_file failed: "
                    f"{type(error_b).__name__}: {error_b}"
                )
                return 1

            # -- Equivalence assertions. --------------------------------------
            conn = pool.get_connection()
            try:
                row_a = conn.execute(
                    "SELECT status, chunk_count, chunks_failed FROM files "
                    "WHERE id = ?",
                    (file_a,),
                ).fetchone()
                row_b = conn.execute(
                    "SELECT status, chunk_count, chunks_failed FROM files "
                    "WHERE id = ?",
                    (file_b,),
                ).fetchone()
                wiki_a = conn.execute(
                    "SELECT COUNT(*) FROM wiki_compile_jobs WHERE "
                    "trigger_id = ?",
                    (f"file:{file_a}",),
                ).fetchone()[0]
                wiki_b = conn.execute(
                    "SELECT COUNT(*) FROM wiki_compile_jobs WHERE "
                    "trigger_id = ?",
                    (f"file:{file_b}",),
                ).fetchone()[0]
                kms_a = conn.execute(
                    "SELECT COUNT(*) FROM kms_compile_jobs WHERE "
                    "trigger_id = ?",
                    (f"file:{file_a}",),
                ).fetchone()[0]
                kms_b = conn.execute(
                    "SELECT COUNT(*) FROM kms_compile_jobs WHERE "
                    "trigger_id = ?",
                    (f"file:{file_b}",),
                ).fetchone()[0]
            finally:
                pool.release_connection(conn)

            if row_a["status"] != row_b["status"]:
                print(
                    "C26 CHECK: FAIL: status diverged: process_file -> "
                    f"'{row_a['status']}', process_existing_file -> "
                    f"'{row_b['status']}'"
                )
                return 1
            if (
                row_a["chunk_count"] != row_b["chunk_count"]
                or row_a["chunks_failed"] != row_b["chunks_failed"]
            ):
                print(
                    "C26 CHECK: FAIL: chunk accounting diverged: "
                    f"{dict(row_a)} vs {dict(row_b)}"
                )
                return 1
            if wiki_a != wiki_b or wiki_a != 1:
                print(
                    "C26 CHECK: FAIL: wiki enqueue not identical under the "
                    f"same gating (process_file={wiki_a}, "
                    f"process_existing_file={wiki_b}, expected 1 each)"
                )
                return 1
            if kms_a != kms_b or kms_a != 1:
                print(
                    "C26 CHECK: FAIL: KMS compile-on-ingest enqueue not "
                    "equivalent across entry points (process_file="
                    f"{kms_a}, process_existing_file={kms_b}, expected 1 "
                    "each under identical kms_enabled + "
                    "kms_compile_on_ingest flags)"
                )
                return 1

            print("C26 CHECK: PASS")
            return 0
        finally:
            for key, value in saved.items():
                setattr(settings, key, value)
            pool.close_all()
    except Exception as exc:  # noqa: BLE001 - never crash without a verdict
        print(f"C26 CHECK: FAIL: unexpected error: {type(exc).__name__}: {exc}")
        return 1
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())


def test_c26_finalization_convergence():
    assert main() == 0
