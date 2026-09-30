"""Acceptance checks (issue #690, defect 4): legacy-schema startup boot.

Pins the desired post-fix behavior of ``run_migrations``: booting against a
legacy database whose tables predate a newer column (``users.locked_until``,
``memories.vault_id``) must complete the migration instead of aborting the
whole startup with ``sqlite3.OperationalError: no such column``.
"""

import sqlite3

from app.models.database import run_migrations


def test_legacy_users_without_locked_until_migrates(tmp_path, monkeypatch):
    """AC8: a legacy users table lacking locked_until must still boot."""
    monkeypatch.setattr("app.config.settings.data_dir", tmp_path)
    db_path = str(tmp_path / "legacy.db")

    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT,
            hashed_password TEXT,
            full_name TEXT,
            role TEXT,
            is_active INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.commit()
    conn.close()

    run_migrations(db_path)

    conn = sqlite3.connect(db_path)
    try:
        journal_rows = conn.execute(
            "SELECT COUNT(*) FROM migration_journal"
        ).fetchone()[0]
    finally:
        conn.close()
    assert int(journal_rows) >= 1


def test_legacy_memories_without_vault_id_migrates(tmp_path, monkeypatch):
    """AC9: a legacy memories table lacking vault_id must still boot."""
    monkeypatch.setattr("app.config.settings.data_dir", tmp_path)
    db_path = str(tmp_path / "legacy.db")

    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT,
            category TEXT,
            tags TEXT,
            source TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.commit()
    conn.close()

    run_migrations(db_path)

    conn = sqlite3.connect(db_path)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(memories)")}
    finally:
        conn.close()
    assert "vault_id" in columns
