"""Issue #692 non-frozen regression tests (B03): migration row bookkeeping.

Complements the frozen acceptance checks (test_b03_upload_migration_rows.py,
hash-pinned in the trace manifest) with coverage for the new behavior the
fix itself introduces:

  1. ``rollback_migration`` moves rows back with the bytes (the mirror half
     of the R2-L3-C01 fix — stores must agree in both directions).
  2. The rollback collision-rename destination updates the row to the actual
     (suffixed) new name.
  3. A failed row UPDATE during ``migrate_uploads`` reverts the bytes to the
     legacy path the row still names (stores stay in agreement).
  4. When the source name reappears before the revert can run, the surviving
     copy at the vault destination is KEPT (never delete the only copy).

Harness mirrors the frozen file: tmp DATA_DIR before app imports, real
init_db + run_migrations, real settings; the failure cases patch
``app.models.database.get_pool`` exactly like
test_orphan_vault_id_removal.py does.
"""

import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

CSRF_TEST_POLICY = "naive"

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "backend"))


def _env_defaults() -> None:
    """Test env BEFORE any app import (mirrors test_c19 / conftest.py)."""
    os.environ.setdefault("ADMIN_SECRET_TOKEN", "test-secret")
    os.environ.setdefault("USERS_ENABLED", "false")
    os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret-key-for-testing-only")
    os.environ.setdefault("REDIS_URL", "")


def _seed_db(tmp_dir: str, vault_name: str = "B03X"):
    """Init a tmp DATA_DIR DB with one vault; returns (conn, vault_id)."""
    os.environ["DATA_DIR"] = tmp_dir

    from app.config import settings
    from app.models.database import init_db, run_migrations

    settings.data_dir = Path(tmp_dir)
    db_path = str(settings.data_dir / "app.db")
    init_db(db_path)
    run_migrations(db_path)

    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "INSERT INTO vaults (name, description, visibility, created_at, "
        "updated_at) VALUES (?, 'b03x', 'private', '2026-01-01', '2026-01-01')",
        (vault_name,),
    )
    vault_id = conn.execute(
        "SELECT id FROM vaults WHERE name = ?", (vault_name,)
    ).fetchone()[0]
    return conn, vault_id


def _insert_file_row(conn, vault_id: int, path: Path, name: str, tag: str):
    conn.execute(
        "INSERT INTO files (vault_id, file_path, file_name, file_hash, "
        "file_size, status) VALUES (?, ?, ?, ?, 4, 'indexed')",
        (vault_id, str(path), name, f"hashx{tag}"),
    )
    conn.commit()
    return conn.execute(
        "SELECT id FROM files WHERE file_hash = ?", (f"hashx{tag}",)
    ).fetchone()[0]


def _failing_pool(vault_id: int, recreate_source: Path | None):
    """Pool double whose UPDATE always fails; SELECTs resolve ``vault_id``.

    ``recreate_source`` models a concurrent writer re-creating the legacy
    source name between the migration's rename and its revert.
    """

    class _Result:
        def __init__(self, row):
            self._row = row

        def fetchone(self):
            return self._row

    class _Conn:
        def execute(self, sql, params=()):
            if sql.lstrip().upper().startswith("UPDATE"):
                if recreate_source is not None:
                    recreate_source.write_text("reappeared", encoding="utf-8")
                raise sqlite3.OperationalError("injected UPDATE failure")
            return _Result((vault_id,))

        def commit(self):
            return None

    pool = MagicMock()
    pool.get_connection.return_value = _Conn()
    return pool


def _zero_row_pool(vault_id: int):
    """Pool double whose UPDATE succeeds but matches zero rows (path drift)."""

    class _Result:
        def __init__(self, row):
            self._row = row

        def fetchone(self):
            return self._row

    class _UpdateResult:
        rowcount = 0

    class _Conn:
        def execute(self, sql, params=()):
            if sql.lstrip().upper().startswith("UPDATE"):
                return _UpdateResult()
            return _Result((vault_id,))

        def commit(self):
            return None

    pool = MagicMock()
    pool.get_connection.return_value = _Conn()
    return pool


def _zero_row_update_reverts_bytes() -> int:
    _env_defaults()
    tmp_dir = tempfile.mkdtemp(prefix="b03_z0_")
    try:
        conn, vault_id = _seed_db(tmp_dir)
        from app.config import settings
        from app.services.upload_path import migrate_uploads

        legacy_path = settings.uploads_dir / "legacy.txt"
        legacy_path.parent.mkdir(parents=True, exist_ok=True)
        legacy_path.write_text("drift probe bytes", encoding="utf-8")
        _insert_file_row(conn, vault_id, legacy_path, "legacy.txt", "z0")
        dest_path = settings.vault_uploads_dir(vault_id) / "legacy.txt"

        with patch(
            "app.models.database.get_pool",
            return_value=_zero_row_pool(vault_id),
        ):
            result = migrate_uploads(False)

        assert result.migrated == 0, f"zero-row match must not count: {result}"
        assert "legacy.txt" in result.failed, result.failed
        assert legacy_path.exists(), "bytes must be restored to the legacy path"
        assert not dest_path.exists(), "the vault copy must be dropped on revert"
        row_path = conn.execute(
            "SELECT file_path FROM files WHERE file_hash = 'hashxz0'"
        ).fetchone()[0]
        assert row_path == str(legacy_path), (
            f"row must keep naming the restored bytes, got {row_path}"
        )
        conn.close()
        return 0
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _rollback_rows_follow_bytes() -> int:
    _env_defaults()
    tmp_dir = tempfile.mkdtemp(prefix="b03_rb1_")
    try:
        conn, vault_id = _seed_db(tmp_dir)
        from app.config import settings
        from app.services.upload_path import migrate_uploads, rollback_migration

        legacy_path = settings.uploads_dir / "legacy.txt"
        legacy_path.parent.mkdir(parents=True, exist_ok=True)
        legacy_path.write_text("rollback probe bytes", encoding="utf-8")
        _insert_file_row(conn, vault_id, legacy_path, "legacy.txt", "rb1")

        result = migrate_uploads(False)
        assert result.migrated == 1, f"expected forward migration: {result}"

        rollback_migration()

        row_path = conn.execute(
            "SELECT file_path FROM files WHERE file_hash = 'hashxrb1'"
        ).fetchone()[0]
        assert Path(row_path).exists(), f"row names missing path: {row_path}"
        assert row_path == str(legacy_path), (
            f"row must follow the bytes back to the flat location, got {row_path}"
        )
        conn.close()
        return 0
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _rollback_collision_updates_row_to_renamed_destination() -> int:
    _env_defaults()
    tmp_dir = tempfile.mkdtemp(prefix="b03_rb2_")
    try:
        conn, first_vault_id = _seed_db(tmp_dir)
        from app.config import settings
        from app.services.upload_path import rollback_migration

        conn.execute(
            "INSERT INTO vaults (name, description, visibility, created_at, "
            "updated_at) VALUES ('B03X2', 'b03x', 'private', '2026-01-01', "
            "'2026-01-01')"
        )
        conn.commit()
        second_vault_id = conn.execute(
            "SELECT id FROM vaults WHERE name = 'B03X2'"
        ).fetchone()[0]
        for vault_id, suffix in zip(
            (first_vault_id, second_vault_id), ("c1", "c2")
        ):
            path = settings.vault_uploads_dir(vault_id) / "same.txt"
            path.write_text(f"vault copy {suffix}", encoding="utf-8")
            _insert_file_row(conn, vault_id, path, "same.txt", suffix)

        rollback_migration()

        rows = [
            conn.execute(
                "SELECT file_path FROM files WHERE file_hash = ?", (f"hashx{t}",)
            ).fetchone()[0]
            for t in ("c1", "c2")
        ]
        plain = str(settings.uploads_dir / "same.txt")
        suffixed = str(settings.uploads_dir / "same_1.txt")
        assert sorted(rows) == sorted([plain, suffixed]), (
            f"rows must follow the collision-renamed bytes, got {rows}"
        )
        for row in rows:
            assert Path(row).exists(), f"row names missing path: {row}"
        conn.close()
        return 0
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _update_failure_reverts_bytes() -> int:
    _env_defaults()
    tmp_dir = tempfile.mkdtemp(prefix="b03_f1_")
    try:
        conn, vault_id = _seed_db(tmp_dir)
        from app.config import settings
        from app.services.upload_path import migrate_uploads

        legacy_path = settings.uploads_dir / "legacy.txt"
        legacy_path.parent.mkdir(parents=True, exist_ok=True)
        legacy_path.write_text("revert probe bytes", encoding="utf-8")
        _insert_file_row(conn, vault_id, legacy_path, "legacy.txt", "f1")
        dest_path = settings.vault_uploads_dir(vault_id) / "legacy.txt"

        with patch(
            "app.models.database.get_pool",
            return_value=_failing_pool(vault_id, None),
        ):
            result = migrate_uploads(False)

        assert result.migrated == 0, f"failed update must not count: {result}"
        assert "legacy.txt" in result.failed, result.failed
        assert legacy_path.exists(), "bytes must be restored to the legacy path"
        assert not dest_path.exists(), "the vault copy must be dropped on revert"
        assert not legacy_path.with_suffix(".txt.migrated").exists(), (
            "the .migrated backup must be restored to the source name"
        )
        row_path = conn.execute(
            "SELECT file_path FROM files WHERE file_hash = 'hashxf1'"
        ).fetchone()[0]
        assert row_path == str(legacy_path), (
            f"row must keep naming the restored bytes, got {row_path}"
        )
        conn.close()
        return 0
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _update_failure_keeps_bytes_when_source_reappears() -> int:
    _env_defaults()
    tmp_dir = tempfile.mkdtemp(prefix="b03_f2_")
    try:
        conn, vault_id = _seed_db(tmp_dir)
        from app.config import settings
        from app.services.upload_path import migrate_uploads

        legacy_path = settings.uploads_dir / "legacy.txt"
        legacy_path.parent.mkdir(parents=True, exist_ok=True)
        legacy_path.write_text("keep probe bytes", encoding="utf-8")
        _insert_file_row(conn, vault_id, legacy_path, "legacy.txt", "f2")
        dest_path = settings.vault_uploads_dir(vault_id) / "legacy.txt"

        with patch(
            "app.models.database.get_pool",
            return_value=_failing_pool(vault_id, recreate_source=legacy_path),
        ):
            result = migrate_uploads(False)

        assert result.migrated == 0, f"failed update must not count: {result}"
        assert "legacy.txt" in result.failed, result.failed
        assert dest_path.exists(), (
            "the surviving copy at the vault destination must be KEPT"
        )
        assert legacy_path.exists(), "the reappeared source must be untouched"
        row_path = conn.execute(
            "SELECT file_path FROM files WHERE file_hash = 'hashxf2'"
        ).fetchone()[0]
        assert row_path == str(legacy_path), (
            f"row must be left for manual repair, got {row_path}"
        )
        conn.close()
        return 0
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_rollback_rows_follow_bytes():
    assert _rollback_rows_follow_bytes() == 0


def test_rollback_collision_updates_row_to_renamed_destination():
    assert _rollback_collision_updates_row_to_renamed_destination() == 0


def test_update_failure_reverts_bytes():
    assert _update_failure_reverts_bytes() == 0


def test_update_failure_keeps_bytes_when_source_reappears():
    assert _update_failure_keeps_bytes_when_source_reappears() == 0


def test_zero_row_update_reverts_bytes():
    assert _zero_row_update_reverts_bytes() == 0


# --- Review follow-up coverage (PR #824 feedback round) -------------------
# F-002/F-005/F-006: destination resolution + revert safety.
# F-003/F-004: staleness gates on the remaining write paths + post-write
# compensation. All non-frozen regression tests.


def _dest_exists_skip_preserves_user_bytes() -> int:
    _env_defaults()
    tmp_dir = tempfile.mkdtemp(prefix="b03_d1_")
    try:
        conn, vault_id = _seed_db(tmp_dir)
        from app.config import settings
        from app.services.upload_path import migrate_uploads

        legacy_path = settings.uploads_dir / "legacy.txt"
        legacy_path.parent.mkdir(parents=True, exist_ok=True)
        legacy_path.write_text("legacy bytes", encoding="utf-8")
        _insert_file_row(conn, vault_id, legacy_path, "legacy.txt", "d1")
        dest_path = settings.vault_uploads_dir(vault_id) / "legacy.txt"
        dest_path.write_text("USER-NEW-UPLOAD", encoding="utf-8")

        result = migrate_uploads(False)

        assert result.migrated == 0, f"dest-exists must not count: {result}"
        assert "legacy.txt" in result.failed, result.failed
        assert dest_path.read_text(encoding="utf-8") == "USER-NEW-UPLOAD", (
            "a pre-existing destination must never be overwritten or reverted away"
        )
        assert legacy_path.exists(), "the legacy source must be untouched"
        row_path = conn.execute(
            "SELECT file_path FROM files WHERE file_hash = 'hashxd1'"
        ).fetchone()[0]
        assert row_path == str(legacy_path)
        conn.close()
        return 0
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _drifted_row_skips_before_copy() -> int:
    _env_defaults()
    tmp_dir = tempfile.mkdtemp(prefix="b03_d2_")
    try:
        conn, vault_id = _seed_db(tmp_dir)
        from app.config import settings
        from app.services.upload_path import migrate_uploads

        legacy_path = settings.uploads_dir / "legacy.txt"
        legacy_path.parent.mkdir(parents=True, exist_ok=True)
        legacy_path.write_text("drift probe bytes", encoding="utf-8")
        drifted = str(Path(tmp_dir) / "somewhere" / "else.txt")
        conn.execute(
            "INSERT INTO files (vault_id, file_path, file_name, file_hash, "
            "file_size, status) VALUES (?, ?, 'legacy.txt', 'hashxd2', 4, "
            "'indexed')",
            (vault_id, drifted),
        )
        conn.commit()
        dest_path = settings.vault_uploads_dir(vault_id) / "legacy.txt"

        result = migrate_uploads(False)

        assert result.migrated == 0, f"drift must not count: {result}"
        assert "legacy.txt" in result.failed, result.failed
        assert not dest_path.exists(), "no copy may be made for a drifted row"
        assert legacy_path.exists(), "the legacy source must be untouched"
        row_path = conn.execute(
            "SELECT file_path FROM files WHERE file_hash = 'hashxd2'"
        ).fetchone()[0]
        assert row_path == drifted, "the drifted row must be left for repair"
        conn.close()
        return 0
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _rollback_then_migrate_round_trip() -> int:
    """Closes the executed cross-vault chain: after a rollback that collision-
    renames one of two same-named files, a re-migration must file each row
    into its OWN vault (path-keyed destination), never the other's."""
    _env_defaults()
    tmp_dir = tempfile.mkdtemp(prefix="b03_rt_")
    try:
        conn, first_vault_id = _seed_db(tmp_dir)
        from app.config import settings
        from app.services.upload_path import migrate_uploads, rollback_migration

        conn.execute(
            "INSERT INTO vaults (name, description, visibility, created_at, "
            "updated_at) VALUES ('B03RT2', 'rt', 'private', '2026-01-01', "
            "'2026-01-01')"
        )
        conn.commit()
        second_vault_id = conn.execute(
            "SELECT id FROM vaults WHERE name = 'B03RT2'"
        ).fetchone()[0]

        for vault_id, tag in zip(
            (first_vault_id, second_vault_id), ("rt1", "rt2")
        ):
            path = settings.vault_uploads_dir(vault_id) / "same.txt"
            path.write_text(f"vault {tag} bytes", encoding="utf-8")
            _insert_file_row(conn, vault_id, path, "same.txt", tag)

        # The executed chain's poison state: a rollback of two same-named
        # files leaves flat/same.txt + flat/same_1.txt with BOTH rows named
        # 'same.txt' in different vaults. Re-migrating must resolve each
        # destination by the row's exact path, never by file_name.
        assert rollback_migration()["moved"], "rollback must move both back"
        result = migrate_uploads(False)
        assert result.migrated == 2, f"re-migration must move both: {result}"

        for tag in ("rt1", "rt2"):
            row_path, row_vault = conn.execute(
                "SELECT file_path, vault_id FROM files WHERE file_hash = ?",
                (f"hashx{tag}",),
            ).fetchone()
            assert Path(row_path).exists(), f"row {tag} names missing bytes"
            expected_dir = settings.data_dir / "vaults" / str(row_vault)
            assert str(Path(row_path).parent.parent) == str(expected_dir), (
                f"row {tag} must land in its OWN vault, got {row_path}"
            )
        conn.close()
        return 0
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


DIM = 4


class _FakeEmbeddingService:
    MAX_TEXT_LENGTH = 8192
    embedding_doc_prefix = ""

    def __init__(self, dim: int) -> None:
        self._dim = dim

    async def embed_batch(self, texts, fail_fast=False):  # noqa: ANN001, ANN202
        return [
            [((i + 1) * 0.25) for i in range(self._dim)] for _ in texts
        ], []


class _RecordingVectorStore:
    """Minimal stand-in recording the calls the gates must (not) make."""

    def __init__(self) -> None:
        self.proxy_writes: list = []
        self.file_deletes: list = []

    async def add_chunks_then_delete_ids(self, new, stale):  # noqa: ANN001, ANN202
        self.proxy_writes.append((new, stale))
        return len(new)

    async def delete_by_file(self, file_id, **kwargs):  # noqa: ANN001, ANN003
        self.file_deletes.append(file_id)
        return 1


def _ingest_harness(tmp_dir: str):
    """Shared c9-style harness: real VectorStore + pool + seeded vault."""
    os.environ["DATA_DIR"] = tmp_dir
    from app.config import settings
    from app.models.database import get_pool, init_db, run_migrations
    from app.services.vector_store import VectorStore

    settings.data_dir = Path(tmp_dir)
    db_path = str(settings.data_dir / "app.db")
    init_db(db_path)
    run_migrations(db_path)
    store = VectorStore(db_path=Path(tmp_dir) / "lancedb")
    pool = get_pool(db_path, max_size=4)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "INSERT INTO vaults (name, description, visibility, created_at, "
        "updated_at) VALUES ('V', 'v', 'private', '2026-01-01', '2026-01-01')"
    )
    conn.commit()
    vault_id = conn.execute("SELECT id FROM vaults WHERE name='V'").fetchone()[0]
    return conn, vault_id, store, pool, settings


def _settings_patches(settings):  # noqa: ANN001, ANN202
    from unittest.mock import patch

    return [
        patch.object(settings, "reupload_safe_order", False),
        patch.object(settings, "wiki_enabled", False),
        patch.object(settings, "wiki_compile_on_ingest", False),
        patch.object(settings, "kms_enabled", False),
        patch.object(settings, "multi_scale_indexing_enabled", False),
        patch.object(settings, "contextual_chunking_enabled", False),
    ]


def _process_file_gate_discards_generation() -> int:
    """F-003(a): the process_file pre-write gate fires when the row dies
    during parse, and no chunks land for the dead row."""
    _env_defaults()
    tmp_dir = tempfile.mkdtemp(prefix="b03_g1_")
    try:
        import asyncio
        from contextlib import ExitStack
        from unittest.mock import AsyncMock

        from app.services.chunking import ProcessedChunk
        from app.services.document_artifacts import ParsedDocument
        from app.services.document_processor import DocumentProcessor

        conn, vault_id, store, pool, settings = _ingest_harness(tmp_dir)
        upload_path = settings.vault_uploads_dir(vault_id) / "g1.txt"
        upload_path.write_text("process_file gate probe", encoding="utf-8")

        processor = DocumentProcessor(
            pool=pool,
            embedding_service=_FakeEmbeddingService(dim=DIM),
            vector_store=store,
        )
        chunk = ProcessedChunk(
            text="gate probe", metadata={"chunk_scale": "default"}, chunk_index=0
        )
        deleted_ids: list = []

        async def _parse_delete_row(file_path, file_id, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
            conn.execute("DELETE FROM files WHERE id = ?", (file_id,))
            conn.commit()
            deleted_ids.append(file_id)
            return [chunk], "gate probe", ParsedDocument(atoms=())

        with ExitStack() as stack:
            for p in _settings_patches(settings):
                stack.enter_context(p)
            stack.enter_context(patch.object(
                processor, "_process_document_file",
                new=AsyncMock(side_effect=_parse_delete_row),
            ))
            stack.enter_context(patch(
                "app.services.document_processor.compute_file_hash",
                return_value="hashg1",
            ))
            stack.enter_context(patch(
                "app.services.document_processor.compute_parent_windows"
            ))
            try:
                asyncio.run(
                    processor.process_file(str(upload_path), vault_id,
                                           source="upload")
                )
            except Exception:  # noqa: BLE001 — the gate raise is expected
                pass

        assert deleted_ids, "process_file must have inserted then parsed a row"
        orphans = asyncio.run(store.count_by_file(str(deleted_ids[0])))
        assert orphans == 0, f"chunks landed for a deleted row: {orphans}"
        remaining = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        assert remaining == 0, "the parse-deleted row must stay deleted"
        conn.close()
        return 0
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _proxy_gate_skips_vector_write() -> int:
    """F-003(b): with the files row gone, _write_atom_proxies's pre-write
    gate skips the vector write entirely (no add_chunks_then_delete_ids) and
    the compensating discard targets the dead file."""
    _env_defaults()
    tmp_dir = tempfile.mkdtemp(prefix="b03_g2_")
    try:
        import asyncio
        from types import SimpleNamespace

        from app.services.background_tasks import BackgroundProcessor
        from app.services.document_processor import DocumentProcessingError

        conn, _vault_id, _store, pool, _settings = _ingest_harness(tmp_dir)
        vec_store = _RecordingVectorStore()

        def _raise_row_gone(file_id):  # noqa: ANN001
            raise DocumentProcessingError("File row removed mid-ingest")

        shim = SimpleNamespace(
            processor=SimpleNamespace(
                pool=pool,
                embedding_service=_FakeEmbeddingService(dim=DIM),
                vector_store=vec_store,
                _raise_if_file_row_missing=_raise_row_gone,
            )
        )

        asyncio.run(
            BackgroundProcessor._write_atom_proxies(
                shim,
                [{
                    "atom_id": "a1",
                    "fingerprint": "f1",
                    "status": "succeeded",
                    "proxy_text": "proxy text",
                }],
                file_id=999999,
                vault_id=1,
                generation_hash="g" * 32,
            )
        )

        assert vec_store.proxy_writes == [], (
            f"proxy vectors must not be written for a dead row: "
            f"{vec_store.proxy_writes}"
        )
        assert vec_store.file_deletes == [], (
            "the pre-write gate path writes nothing, so there is nothing to "
            f"discard: {vec_store.file_deletes}"
        )
        conn.close()
        return 0
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _post_write_discard_removes_orphans() -> int:
    """F-004: a delete landing between the pre-write gate and add_chunks is
    compensated — the just-written chunks are discarded, none remain."""
    _env_defaults()
    tmp_dir = tempfile.mkdtemp(prefix="b03_g3_")
    try:
        import asyncio
        from contextlib import ExitStack
        from unittest.mock import AsyncMock

        from app.services.chunking import ProcessedChunk
        from app.services.document_artifacts import ParsedDocument
        from app.services.document_processor import DocumentProcessor

        conn, vault_id, store, pool, settings = _ingest_harness(tmp_dir)
        upload_path = settings.vault_uploads_dir(vault_id) / "g3.txt"
        upload_path.write_text("post-write discard probe", encoding="utf-8")
        conn.execute(
            "INSERT INTO files (vault_id, file_path, file_name, file_hash, "
            "file_size, status) VALUES (?, ?, 'g3.txt', 'hashg3', 4, "
            "'processing')",
            (vault_id, str(upload_path)),
        )
        conn.commit()
        file_id = conn.execute(
            "SELECT id FROM files WHERE file_hash = 'hashg3'"
        ).fetchone()[0]

        processor = DocumentProcessor(
            pool=pool,
            embedding_service=_FakeEmbeddingService(dim=DIM),
            vector_store=store,
        )
        chunk = ProcessedChunk(
            text="discard probe",
            metadata={"chunk_scale": "default"},
            chunk_index=0,
        )

        real_init_table = store.init_table

        async def _init_table_delete_row(dim, **kwargs):  # noqa: ANN001, ANN003
            conn.execute("DELETE FROM files WHERE id = ?", (file_id,))
            conn.commit()
            await real_init_table(dim, **kwargs)

        raised = None
        with ExitStack() as stack:
            for p in _settings_patches(settings):
                stack.enter_context(p)
            stack.enter_context(patch.object(
                processor, "_process_document_file",
                new=AsyncMock(return_value=(
                    [chunk], "discard probe", ParsedDocument(atoms=())
                )),
            ))
            stack.enter_context(patch(
                "app.services.document_processor.compute_file_hash",
                return_value="hashg3",
            ))
            stack.enter_context(patch(
                "app.services.document_processor.compute_parent_windows"
            ))
            stack.enter_context(patch.object(
                store, "init_table", side_effect=_init_table_delete_row
            ))
            try:
                asyncio.run(
                    processor.process_existing_file(
                        file_id=file_id,
                        file_path=str(upload_path),
                        vault_id=vault_id,
                    )
                )
            except Exception as exc:  # noqa: BLE001 — the gate raise is expected
                raised = exc

        chunks = asyncio.run(store.count_by_file(str(file_id)))
        assert chunks == 0, (
            f"post-write compensation must discard orphans, got {chunks}"
        )
        assert raised is not None, "the staleness error must surface"
        conn.close()
        return 0
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_dest_exists_skip_preserves_user_bytes():
    assert _dest_exists_skip_preserves_user_bytes() == 0


def test_drifted_row_skips_before_copy():
    assert _drifted_row_skips_before_copy() == 0


def test_rollback_then_migrate_round_trip():
    assert _rollback_then_migrate_round_trip() == 0


def test_process_file_gate_discards_generation():
    assert _process_file_gate_discards_generation() == 0


def test_proxy_gate_skips_vector_write():
    assert _proxy_gate_skips_vector_write() == 0


def test_post_write_discard_removes_orphans():
    assert _post_write_discard_removes_orphans() == 0
