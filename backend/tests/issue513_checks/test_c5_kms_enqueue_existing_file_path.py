"""C5 - AC5 (INGEST-006): KMS compile-on-ingest enqueue from the existing-file path.

Contract under test (issue #513 AC5):

  With KMS enabled + ``kms_compile_on_ingest`` enabled, an upload processed via
  ``DocumentProcessor.process_existing_file`` (the async-upload / existing-row
  entry point used by ``BackgroundProcessor._process_task`` when
  ``TaskItem.file_id`` is set) must enqueue EXACTLY ONE ``kms_compile_jobs``
  row carrying the file id (``trigger_id = 'file:<id>'``). With the flags
  disabled it must enqueue none.

  ``process_file`` (the synchronous scan/email entry point) already has this
  finalization block; the check runs it as a control so a missing job on BOTH
  paths is reported as a harness failure rather than a silent pass.

Pre-fix expectation: the KMS block exists only in ``process_file``
(document_processor.py ~2887-2911); ``process_existing_file`` finalization
mirrors the wiki block but omits KMS entirely, so the enabled case enqueues 0
jobs -> FAIL.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

# Repo root: this file lives at <repo>/backend/tests/issue513_checks/<name>.py
ROOT = Path(__file__).resolve().parents[3]
_TMPDIR = tempfile.mkdtemp(prefix="issue513_c5_")
sys.path.insert(0, str(ROOT / "backend"))


def _hermetic_env() -> None:
    """Set test env BEFORE any app import (mirrors backend/tests/conftest.py)."""
    os.environ["ADMIN_SECRET_TOKEN"] = "test-secret"
    os.environ["USERS_ENABLED"] = "false"
    os.environ["JWT_SECRET_KEY"] = "test-jwt-secret-key-for-testing-only"
    os.environ["REDIS_URL"] = ""
    os.environ["DATA_DIR"] = _TMPDIR


class _FakeEmbeddingService:
    """Deterministic offline embedding service double."""

    MAX_TEXT_LENGTH = 8192
    embedding_doc_prefix = ""

    def __init__(self, dim: int = 4) -> None:
        self._dim = dim

    async def embed_batch(self, texts, fail_fast=False):  # noqa: ANN001, ANN202
        embeddings = [[float(len(t) % 7) + 0.1 * i for i in range(self._dim)] for t in texts]
        return embeddings, []


class _FakeVectorStore:
    """Minimal vector store double covering the processor's write surface."""

    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}

    async def init_table(self, embedding_dim: int) -> None:  # noqa: ANN001
        return None

    async def add_chunks(self, records):  # noqa: ANN001, ANN202
        for rec in records:
            self.rows[rec["id"]] = rec
        return {"vector_write_ms": 0.0, "optimize_ms": 0.0}

    async def delete_old_generation_by_file(self, file_id, gen_prefix):  # noqa: ANN001, ANN202
        return 0

    async def delete_by_file(self, file_id):  # noqa: ANN001, ANN202
        return 0

    async def count_by_file(self, file_id) -> int:  # noqa: ANN001
        return sum(1 for r in self.rows.values() if r.get("file_id") == str(file_id))


def _kms_job_count(db_path: str, trigger_id: str) -> int:
    import sqlite3

    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM kms_compile_jobs WHERE trigger_id = ?", (trigger_id,)
        ).fetchone()
        return int(row[0])
    finally:
        conn.close()


def _run_existing_path(processor, file_id: int, file_path: str, vault_id: int):  # noqa: ANN202
    from app.services.chunking import ProcessedChunk
    from app.services.document_artifacts import ParsedDocument

    chunk = ProcessedChunk(
        text="alpha beta gamma delta epsilon zeta",
        metadata={"chunk_scale": "default", "raw_text": "alpha beta gamma"},
        chunk_index=0,
    )
    with patch.object(
        processor,
        "_process_document_file",
        new=AsyncMock(
            return_value=( [chunk], "alpha beta gamma", ParsedDocument(atoms=()) )
        ),
    ):
        return asyncio.run(
            processor.process_existing_file(
                file_id=file_id, file_path=file_path, vault_id=vault_id
            )
        )


def _run_control_path(processor, file_id: int, file_path: str, vault_id: int):  # noqa: ANN202
    from app.services.chunking import ProcessedChunk
    from app.services.document_artifacts import ParsedDocument

    chunk = ProcessedChunk(
        text="alpha beta gamma delta epsilon zeta",
        metadata={"chunk_scale": "default", "raw_text": "alpha beta gamma"},
        chunk_index=0,
    )
    with patch.object(processor, "_check_duplicate", return_value=None), \
        patch.object(processor, "_insert_or_get_file_record", return_value=file_id), \
        patch.object(processor, "_get_chunk_enrichment_service", return_value=None), \
        patch.object(
            processor,
            "_process_document_file",
            new=AsyncMock(
                return_value=( [chunk], "alpha beta gamma", ParsedDocument(atoms=()) )
            ),
        ), \
        patch("app.services.document_processor.set_phase"), \
        patch("app.services.document_processor.clear_progress"), \
        patch("app.services.document_processor.compute_parent_windows"), \
        patch("app.services.document_processor.set_wiki_pending"):
        return asyncio.run(processor.process_file(file_path, vault_id=vault_id))


def main() -> int:
    _hermetic_env()
    logging.disable(logging.CRITICAL)
    import sqlite3

    from app.config import settings
    from app.models.database import get_pool, init_db
    from app.services.document_processor import DocumentProcessor

    tmp = Path(tempfile.mkdtemp(prefix="c5_db_"))
    db_path = str(tmp / "app.db")
    init_db(db_path)

    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO vaults (name) VALUES ('v')")
    vault_id = conn.execute("SELECT id FROM vaults LIMIT 1").fetchone()[0]
    conn.execute(
        "INSERT INTO files (vault_id, file_path, file_name, file_hash, file_size, status) "
        "VALUES (?, ?, ?, ?, ?, 'pending')",
        (vault_id, str(tmp / "doc.txt"), "doc.txt", "hash-c5", 16),
    )
    conn.commit()
    file_id = conn.execute("SELECT id FROM files LIMIT 1").fetchone()[0]
    conn.close()

    upload_path = tmp / "doc.txt"
    upload_path.write_text("alpha beta gamma", encoding="utf-8")

    pool = get_pool(db_path, max_size=4)

    def _make_processor() -> DocumentProcessor:
        return DocumentProcessor(
            pool=pool,
            embedding_service=_FakeEmbeddingService(),
            vector_store=_FakeVectorStore(),
        )

    with patch.object(settings, "data_dir", tmp), \
        patch.object(settings, "kms_enabled", True), \
        patch.object(settings, "kms_compile_on_ingest", True), \
        patch.object(settings, "wiki_enabled", False), \
        patch.object(settings, "wiki_compile_on_ingest", False), \
        patch.object(settings, "multi_scale_indexing_enabled", False), \
        patch.object(settings, "contextual_chunking_enabled", False):

        # Control: process_file (synchronous entry point) must enqueue exactly one job.
        try:
            _run_control_path(_make_processor(), file_id, str(upload_path), vault_id)
        except Exception as exc:  # noqa: BLE001
            print(f"C5 CHECK: FAIL: control path (process_file) crashed: {exc!r}")
            return 1
        control_count = _kms_job_count(db_path, f"file:{file_id}")
        if control_count != 1:
            print(
                f"C5 CHECK: FAIL: harness control invalid - process_file enqueued "
                f"{control_count} KMS job(s), expected exactly 1"
            )
            return 1

        # Case 1 (discriminating): existing-file path with flags enabled.
        try:
            _run_existing_path(_make_processor(), file_id, str(upload_path), vault_id)
        except Exception as exc:  # noqa: BLE001
            print(f"C5 CHECK: FAIL: process_existing_file crashed: {exc!r}")
            return 1
        enabled_count = _kms_job_count(db_path, f"file:{file_id}")
        if enabled_count != 2:
            # control contributed 1; the existing-file path must add exactly one more
            print(
                f"C5 CHECK: FAIL: process_existing_file with kms_enabled + "
                f"kms_compile_on_ingest enqueued {enabled_count - 1} KMS job(s) "
                f"(total {enabled_count} incl. control), expected exactly 1 - "
                f"the async upload path skips KMS compilation on ingest (INGEST-006)"
            )
            return 1

        # Case 2: flags disabled -> no additional job from the existing-file path.
        with patch.object(settings, "kms_enabled", False):
            try:
                _run_existing_path(_make_processor(), file_id, str(upload_path), vault_id)
            except Exception as exc:  # noqa: BLE001
                print(f"C5 CHECK: FAIL: process_existing_file (disabled) crashed: {exc!r}")
                return 1
        disabled_count = _kms_job_count(db_path, f"file:{file_id}")
        if disabled_count != 2:
            print(
                f"C5 CHECK: FAIL: process_existing_file with KMS flags disabled "
                f"enqueued {disabled_count - 2} extra KMS job(s), expected 0"
            )
            return 1

    print("C5 CHECK: PASS")
    return 0


def test_c5_kms_enqueue_existing_file_path() -> None:
    assert main() == 0


if __name__ == "__main__":
    raise SystemExit(main())
