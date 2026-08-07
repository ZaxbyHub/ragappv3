"""Tests for the multimodal artifact schema migration (issue #460).

Covers: fresh vs migrated schema convergence (beyond the base-table count),
migration idempotency on rerun, and ON DELETE CASCADE from files to the new
artifact tables.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _table_name_set(conn) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {r[0] for r in rows}


def _tables_and_indexes(db_path: str) -> set[tuple]:
    conn = sqlite3.connect(db_path)
    try:
        tables = conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type IN ('table','index')"
        ).fetchall()
        return {(r[0], r[1]) for r in tables}
    finally:
        conn.close()


def test_fresh_and_migrated_schemas_converge():
    from app.models.database import (
        SCHEMA,
        init_db,
        migrate_add_multimodal_artifact_tables,
    )

    # Fresh database: init_db creates everything from SCHEMA.
    fresh_path = os.path.join(tempfile.mkdtemp(), "fresh.db")
    init_db(fresh_path)

    # Migrated database: simulate an OLD database that predates the artifact
    # tables but lacks the active_generation_hash column, then migrate.
    migrated_path = os.path.join(tempfile.mkdtemp(), "migrated.db")
    conn = sqlite3.connect(migrated_path)
    # Build only the legacy base files table (pre-issue #460) so the migration
    # must ADD the new tables + column.
    conn.executescript(
        """
        CREATE TABLE files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vault_id INTEGER NOT NULL,
            file_path TEXT NOT NULL,
            file_name TEXT NOT NULL,
            file_hash TEXT,
            file_size INTEGER NOT NULL,
            file_type TEXT,
            chunk_count INTEGER DEFAULT 0,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    conn.commit()
    conn.close()

    migrate_add_multimodal_artifact_tables(migrated_path)

    fresh = _tables_and_indexes(fresh_path)
    migrated = _tables_and_indexes(migrated_path)
    artifact_objects = {
        (name, sql)
        for name, sql in fresh
        if any(
            token in name
            for token in ("document_atoms", "document_assets", "ingestion_stage_states", "artifact_delete_pending")
        )
    }
    for name, sql in artifact_objects:
        assert (name, sql) in migrated, f"artifact object {name} missing/divergent in migrated schema"

    # active_generation_hash column added by migration.
    c = sqlite3.connect(migrated_path)
    columns = {row[1] for row in c.execute("PRAGMA table_info(files)").fetchall()}
    c.close()
    assert "active_generation_hash" in columns


def test_migration_is_idempotent():
    from app.models.database import migrate_add_multimodal_artifact_tables

    db_path = os.path.join(tempfile.mkdtemp(), "idem.db")
    # Seed the legacy files table, as a real pre-issue database would have
    # (run_migrations always calls init_db first, so files always exists).
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE files (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "vault_id INTEGER NOT NULL, file_path TEXT NOT NULL, "
        "file_name TEXT NOT NULL, file_size INTEGER NOT NULL)"
    )
    conn.commit()
    conn.close()

    migrate_add_multimodal_artifact_tables(db_path)
    first = _tables_and_indexes(db_path)
    migrate_add_multimodal_artifact_tables(db_path)
    second = _tables_and_indexes(db_path)
    assert first == second


def test_artifact_tables_cascade_from_files_delete():
    db_path = os.path.join(tempfile.mkdtemp(), "cascade.db")
    from app.models.database import init_db, run_migrations

    init_db(db_path)
    run_migrations(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "INSERT INTO files (vault_id, file_path, file_name, file_size, status) "
        "VALUES (1, '/tmp/x', 'x', 1, 'indexed')"
    )
    file_id = conn.execute("SELECT id FROM files").fetchone()["id"]
    conn.execute(
        "INSERT INTO document_atoms "
        "(atom_id, schema_version, file_id, generation_hash, ordinal, kind) "
        "VALUES ('a1', 1, ?, 'g', 0, 'text')",
        (file_id,),
    )
    conn.execute(
        "INSERT INTO document_assets "
        "(asset_id, file_id, generation_hash, sha256, rel_path, byte_size) "
        "VALUES ('s1', ?, 'g', 's1', 'g/s1', 1)",
        (file_id,),
    )
    conn.execute(
        "INSERT INTO ingestion_stage_states "
        "(file_id, generation_hash, stage, status) VALUES (?, 'g', 'parse', 'succeeded')",
        (file_id,),
    )
    conn.commit()

    conn.execute("DELETE FROM files WHERE id = ?", (file_id,))
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM document_atoms").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM document_assets").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM ingestion_stage_states").fetchone()[0] == 0
    conn.close()
