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
