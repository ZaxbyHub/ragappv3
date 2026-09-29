"""Issue #692 acceptance checks AC1-AC3 (B03): vault deletion disk hygiene.

Discriminating expectations at the unfixed HEAD (each check FAILS for the
defect reason, not for a harness error):

  AC1 - ``vaults.delete_vault`` never unlinks the vault's uploaded files:
        the bytes survive the delete -> ``assert 1 == 0`` FAILS. Post-fix the
        uploaded file is gone -> ``present == 0`` passes.
  AC2 - ``vaults.delete_vault`` never removes the per-vault directory:
        ``data/vaults/<id>`` survives the delete -> ``assert 1 == 0`` FAILS.
        The path is built from ``settings.data_dir`` directly because the
        ``settings.vaults_dir`` property mkdirs its parent (asserting against
        it would pollute the check).
  AC3 - an in-flight ``process_existing_file`` whose parse step deletes the
        vault still lands its chunks afterwards (nothing revalidates the
        vault before ``add_chunks``), so the vector store keeps chunks whose
        relational rows are gone -> ``assert 1 == 0`` FAILS. Fix-agnostic by
        design: refusing, deferring, or re-checkpointing the write all pass.

Harness mirrors ``tests/issue513_checks/test_c19_vault_delete_rollback.py``
(tmp DATA_DIR before app imports, init_db + run_migrations, real sqlite3
connection passed straight into the route function) and
``tests/issue513_checks/test_c9_reindex_dimension_migration.py`` (real
VectorStore + get_pool + DocumentProcessor with a patched
``_process_document_file`` for AC3).
"""

import asyncio
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

CSRF_TEST_POLICY = "naive"

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "backend"))


def _env_defaults() -> None:
    """Test env BEFORE any app import (mirrors test_c19 / conftest.py)."""
    os.environ.setdefault("ADMIN_SECRET_TOKEN", "test-secret")
    os.environ.setdefault("USERS_ENABLED", "false")
    os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret-key-for-testing-only")
    os.environ.setdefault("REDIS_URL", "")


class _FakeEmbeddingService:
    """Deterministic embedder (copied from test_c9, dim configurable)."""

    MAX_TEXT_LENGTH = 8192
    embedding_doc_prefix = ""

    def __init__(self, dim: int) -> None:
        self._dim = dim

    async def embed_batch(self, texts, fail_fast=False):  # noqa: ANN001, ANN202
        return [
            [((i + 1) * 0.25) for i in range(self._dim)] for _ in texts
        ], []


class _FakeVectorStore:
    """Fake vector store whose async delete_by_vault returns 0."""

    def __init__(self) -> None:
        self.deleted_vaults: list = []
        self.db = None

    async def delete_by_vault(self, vault_id_str):  # noqa: ANN001
        self.deleted_vaults.append(vault_id_str)
        return 0

    async def delete_by_file(self, file_id_str):  # noqa: ANN001
        return 0


def _seed(tmp_dir: str, vault_name: str, file_hash: str, status: str):
    """Init a tmp DATA_DIR DB with one vault and one real file row.

    Returns (conn, vault_id, upload_path, db_path). The upload bytes are
    really written under ``settings.vault_uploads_dir(vault_id)``.
    """
    os.environ["DATA_DIR"] = tmp_dir

    from app.config import settings
    from app.models.database import init_db, run_migrations

    settings.data_dir = Path(tmp_dir)
    db_path = str(settings.data_dir / "app.db")
    init_db(db_path)
    run_migrations(db_path)

    # check_same_thread=False: the route drives this same connection through
    # asyncio.to_thread workers (the working c19 pattern).
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "INSERT INTO vaults (name, description, visibility, created_at, "
        "updated_at) VALUES (?, ?, 'private', '2026-01-01', '2026-01-01')",
        (vault_name, "b03"),
    )
    vault_id = conn.execute(
        "SELECT id FROM vaults WHERE name = ?", (vault_name,)
    ).fetchone()[0]
    upload_path = settings.vault_uploads_dir(vault_id) / "doc.txt"
    upload_path.write_text("b03 uploaded bytes", encoding="utf-8")
    conn.execute(
        "INSERT INTO files (vault_id, file_path, file_name, file_hash, "
        "file_size, status) VALUES (?, ?, ?, ?, ?, ?)",
        (vault_id, str(upload_path), "doc.txt", file_hash, 18, status),
    )
    conn.commit()
    return conn, vault_id, upload_path, db_path


def _ac1_unlinks_uploaded_file() -> int:
    """AC1: deleting a vault must unlink its uploaded file bytes."""
    _env_defaults()
    tmp_dir = tempfile.mkdtemp(prefix="b03_ac1_")
    try:
        conn, vault_id, upload_path, _db_path = _seed(
            tmp_dir, "B03V1", "hashb03ac1", "indexed"
        )

        from app.api.routes.vaults import delete_vault

        asyncio.run(
            delete_vault(
                vault_id=vault_id,
                conn=conn,
                vector_store=_FakeVectorStore(),
                user={},
                _csrf_token=None,
            )
        )

        present = int(upload_path.exists())
        assert present == 0
        conn.close()
        return 0
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _ac2_removes_vault_directory() -> int:
    """AC2: deleting a vault must remove its per-vault storage directory."""
    _env_defaults()
    tmp_dir = tempfile.mkdtemp(prefix="b03_ac2_")
    try:
        from app.config import settings

        conn, vault_id, _upload_path, _db_path = _seed(
            tmp_dir, "B03V2", "hashb03ac2", "indexed"
        )

        from app.api.routes.vaults import delete_vault

        asyncio.run(
            delete_vault(
                vault_id=vault_id,
                conn=conn,
                vector_store=_FakeVectorStore(),
                user={},
                _csrf_token=None,
            )
        )

        # Built from data_dir directly: the settings.vaults_dir property
        # mkdirs its parent and would pollute the assertion.
        present = int((settings.data_dir / "vaults" / str(vault_id)).exists())
        assert present == 0
        conn.close()
        return 0
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


async def _ac3_ingest_vs_delete() -> int:
    """AC3: a chunk written while its vault is deleted must not survive.

    The parse step of an in-flight ingest deletes the vault (the racing
    delete_vault), then the ingest continues. Fix-agnostic: the final state
    must not hold vector chunks for a vault with no relational rows.
    """
    _env_defaults()
    tmp_dir = tempfile.mkdtemp(prefix="b03_ac3_")
    try:
        from fastapi import HTTPException

        from app.api.routes.vaults import delete_vault
        from app.config import settings
        from app.models.database import get_pool
        from app.services.chunking import ProcessedChunk
        from app.services.document_artifacts import ParsedDocument
        from app.services.document_processor import DocumentProcessor
        from app.services.vector_store import VectorStore

        conn, vault_id, upload_path, db_path = _seed(
            tmp_dir, "B03V3", "hashb03ac3", "processing"
        )
        file_id = conn.execute(
            "SELECT id FROM files WHERE vault_id = ?", (vault_id,)
        ).fetchone()[0]

        pool = get_pool(db_path, max_size=3)
        store = VectorStore(db_path=Path(tmp_dir) / "lancedb")

        chunk = ProcessedChunk(
            text="chunk written while the vault is being deleted",
            metadata={"chunk_scale": "default", "raw_text": "chunk written"},
            chunk_index=0,
        )
        processor = DocumentProcessor(
            pool=pool,
            embedding_service=_FakeEmbeddingService(dim=4),
            vector_store=store,
        )

        async def _parse_with_mid_flight_delete(*args, **kwargs):  # noqa: ANN002, ANN003
            try:
                await delete_vault(
                    vault_id=vault_id,
                    conn=conn,
                    vector_store=store,
                    user={},
                    _csrf_token=None,
                )
            except HTTPException:
                pass
            return (
                [chunk],
                "chunk written while the vault is being deleted",
                ParsedDocument(atoms=()),
            )

        with patch.object(
            processor,
            "_process_document_file",
            new=AsyncMock(side_effect=_parse_with_mid_flight_delete),
        ), patch(
            "app.services.document_processor.compute_file_hash",
            return_value="hashb03ac3",
        ), patch(
            "app.services.document_processor.compute_parent_windows"
        ), patch.object(
            settings, "wiki_enabled", False
        ), patch.object(
            settings, "wiki_compile_on_ingest", False
        ), patch.object(
            settings, "kms_enabled", False
        ), patch.object(
            settings, "multi_scale_indexing_enabled", False
        ), patch.object(
            settings, "contextual_chunking_enabled", False
        ), patch.object(
            settings, "optimize_mode", "manual"
        ):
            try:
                await processor.process_existing_file(
                    file_id=file_id,
                    file_path=str(upload_path),
                    vault_id=vault_id,
                )
            except Exception:  # noqa: BLE001 - the race may fail the ingest
                pass

        chunks = await store.count_by_file(str(file_id))
        vault_rows = conn.execute(
            "SELECT COUNT(*) FROM vaults WHERE id = ?", (vault_id,)
        ).fetchone()[0]

        orphan = int(vault_rows == 0 and chunks > 0)
        assert orphan == 0
        conn.close()
        return 0
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_vault_delete_unlinks_uploaded_file():
    assert _ac1_unlinks_uploaded_file() == 0


def test_vault_delete_removes_vault_directory():
    assert _ac2_removes_vault_directory() == 0


def test_vault_delete_during_ingest_leaves_no_orphan_chunks():
    assert asyncio.run(_ac3_ingest_vs_delete()) == 0


if __name__ == "__main__":
    raise SystemExit(
        _ac1_unlinks_uploaded_file()
        or _ac2_removes_vault_directory()
        or asyncio.run(_ac3_ingest_vs_delete())
    )
