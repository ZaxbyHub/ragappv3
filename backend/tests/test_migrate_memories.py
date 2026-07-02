"""Regression tests for scripts/migrate_memories.py maintenance toggling."""

import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts import migrate_memories


@pytest.fixture
def scratch_dir():
    root = Path(__file__).resolve().parents[2] / "tmp" / "pytest-issue-273"
    root.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(dir=root))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _maintenance_enabled(db_path: Path) -> bool:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT value FROM system_flags WHERE name = 'maintenance'"
        ).fetchone()
        return bool(row[0])
    finally:
        conn.close()


def test_migrate_constructs_maintenance_service_with_pool_and_cleans_up(
    scratch_dir, monkeypatch
):
    db_path = scratch_dir / "app.db"
    monkeypatch.setattr(migrate_memories.settings, "data_dir", scratch_dir)
    monkeypatch.setattr(migrate_memories, "backup_sqlite", None)
    monkeypatch.setattr(migrate_memories, "HAS_BACKUP_MODULE", False)
    monkeypatch.setattr(migrate_memories, "run_migrations", lambda _path: None)
    monkeypatch.chdir(scratch_dir)

    migrate_memories.migrate(rollback=False, backup=None, retention=30)

    assert _maintenance_enabled(db_path) is False
    db_path.unlink()


def test_migrate_rollbacks_return_after_pool_cleanup(scratch_dir, monkeypatch):
    db_path = scratch_dir / "app.db"
    backup_path = scratch_dir / "backup.db"
    backup_path.write_bytes(b"backup")
    monkeypatch.setattr(migrate_memories.settings, "data_dir", scratch_dir)
    monkeypatch.setattr(migrate_memories, "decrypt_backup", lambda _backup: db_path)

    migrate_memories.migrate(rollback=True, backup=backup_path, retention=30)

    db_path.unlink()


def test_migrate_exception_disables_maintenance_and_closes_pool(
    scratch_dir, monkeypatch
):
    db_path = scratch_dir / "app.db"
    monkeypatch.setattr(migrate_memories.settings, "data_dir", scratch_dir)
    monkeypatch.setattr(migrate_memories, "backup_sqlite", None)
    monkeypatch.setattr(migrate_memories, "HAS_BACKUP_MODULE", False)
    monkeypatch.chdir(scratch_dir)

    def fail_migration(_path):
        raise RuntimeError("migration failed")

    monkeypatch.setattr(migrate_memories, "run_migrations", fail_migration)

    with pytest.raises(RuntimeError, match="migration failed"):
        migrate_memories.migrate(rollback=False, backup=None, retention=30)

    assert _maintenance_enabled(db_path) is False
    db_path.unlink()
