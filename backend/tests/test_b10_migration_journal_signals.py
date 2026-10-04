"""Acceptance checks pinning issue #699 criteria (Workstream B PR 10).

The migration journal must carry decision-grade signals: a lock failure on
the swap transaction itself still records a terminal ``failed`` row, a
recorded failure stays visible in the startup summary tail after clean
re-boots, ``rebuilt`` is only written when a rebuild actually happened,
claims dedup ignores foreign-key violations from unrelated tables, the
chat-messages dedup delete journals its deleted-row count, and the
``rebuilt`` signal's reader promise is fulfilled somewhere in backend/app.

Each test's final assert is the frozen acceptance line for the issue.
"""

import ast
import inspect
import os
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.models import database as app_database  # noqa: E402
from app.models.database import (  # noqa: E402
    migrate_add_chat_turn_columns,
    migrate_add_files_content_fts,
    migrate_add_wiki_claims_unique_claim_text,
    run_migrations,
)
from app.models.migration_journal import (  # noqa: E402
    invalidate_derived_data,
    latest_outcomes,
    record_migration_outcome,
)

# Copied verbatim from tests/test_storage_recovery_sqlite.py: the wiki_claims
# DDL shape WITHOUT any inline UNIQUE on (vault_id, claim_text).
_NEW_CLAIMS_DDL = """
CREATE TABLE {name} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vault_id INTEGER NOT NULL,
    page_id INTEGER,
    claim_text TEXT NOT NULL,
    claim_type TEXT NOT NULL DEFAULT 'fact',
    subject TEXT,
    predicate TEXT,
    object TEXT,
    source_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN (
        'active','contradicted','superseded','unverified','archived','needs_review')),
    confidence REAL DEFAULT 0.0,
    created_by INTEGER,
    created_by_kind TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""


class _FailOnceProxy:
    """Wraps a real sqlite3.Connection; the FIRST execute whose SQL contains
    ``needle`` raises ``sqlite3.OperationalError(message)``. Everything else
    — including attribute set/get such as ``isolation_level`` — delegates to
    the real connection (migrations set attributes on the object
    sqlite3.connect hands them)."""

    _OWN = {"_real", "_needle", "_message", "_armed"}

    def __init__(self, real, needle, message="database is locked"):
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "_needle", needle)
        object.__setattr__(self, "_message", message)
        object.__setattr__(self, "_armed", True)

    def execute(self, sql, params=()):
        if self._armed and self._needle in sql:
            object.__setattr__(self, "_armed", False)
            raise sqlite3.OperationalError(self._message)
        return self._real.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._real, name)

    def __setattr__(self, name, value):
        if name in self._OWN:
            object.__setattr__(self, name, value)
        else:
            setattr(self._real, name, value)


def _scalar(db_path, sql, params=()):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(sql, params).fetchone()[0]
    finally:
        conn.close()


def _journal_count(db_path, migration_name):
    return _scalar(
        db_path,
        "SELECT COUNT(*) FROM migration_journal WHERE migration_name = ?",
        (migration_name,),
    )


def _journal_detail_count(db_path, migration_name, needle="1"):
    return _scalar(
        db_path,
        "SELECT COUNT(*) FROM migration_journal"
        " WHERE migration_name = ? AND detail LIKE ?",
        (migration_name, f"%{needle}%"),
    )


def test_begin_immediate_lock_error_records_terminal_failed_row(tmp_path):
    db = str(tmp_path / "a.db")
    run_migrations(db)
    snapshot = _scalar(
        db,
        "SELECT COUNT(*) FROM migration_journal"
        " WHERE migration_name = 'migrate_add_files_content_fts'"
        " AND phase = 'failed'",
    )

    original_connect = app_database.sqlite3.connect

    def _proxied_connect(*args, **kwargs):
        return _FailOnceProxy(original_connect(*args, **kwargs), "BEGIN IMMEDIATE")

    app_database.sqlite3.connect = _proxied_connect
    try:
        with pytest.raises(sqlite3.OperationalError):
            migrate_add_files_content_fts(db)
    finally:
        app_database.sqlite3.connect = original_connect

    new_failed = _scalar(
        db,
        "SELECT COUNT(*) FROM migration_journal"
        " WHERE migration_name = 'migrate_add_files_content_fts'"
        " AND phase = 'failed'",
    ) - snapshot
    assert new_failed == 1


def test_failed_row_survives_two_clean_boots_in_startup_summary(tmp_path):
    db = tmp_path / "b.db"
    run_migrations(str(db))
    conn = sqlite3.connect(str(db))
    try:
        record_migration_outcome(
            conn, migration_name="b10_probe", phase="failed", outcome="error"
        )
        conn.commit()
    finally:
        conn.close()
    run_migrations(str(db))
    run_migrations(str(db))

    rows = latest_outcomes(str(db), limit=3)
    assert sum(1 for r in rows if r["phase"] == "failed") == 1


def test_files_content_fts_second_run_writes_no_rebuilt_row(tmp_path):
    db = tmp_path / "c.db"
    run_migrations(str(db))
    count1 = _scalar(
        str(db),
        "SELECT COUNT(*) FROM migration_journal"
        " WHERE migration_name = 'migrate_add_files_content_fts'"
        " AND outcome = 'rebuilt'",
    )
    migrate_add_files_content_fts(str(db))
    count2 = _scalar(
        str(db),
        "SELECT COUNT(*) FROM migration_journal"
        " WHERE migration_name = 'migrate_add_files_content_fts'"
        " AND outcome = 'rebuilt'",
    )
    rebuilt_rows_added = count2 - count1
    assert rebuilt_rows_added == 0


def test_claims_dedup_ignores_unrelated_fk_violation(tmp_path):
    db = str(tmp_path / "d.db")
    conn = sqlite3.connect(db)
    try:
        conn.execute(_NEW_CLAIMS_DDL.format(name="wiki_claims"))
        conn.execute(
            "CREATE TABLE wiki_claim_sources ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " claim_id INTEGER NOT NULL REFERENCES wiki_claims(id)"
            " ON DELETE CASCADE,"
            " source_kind TEXT NOT NULL DEFAULT 'document')"
        )
        conn.execute("CREATE TABLE tags (id INTEGER PRIMARY KEY)")
        conn.execute(
            "CREATE TABLE document_tags ("
            "file_id INTEGER, tag_id INTEGER REFERENCES tags(id))"
        )
        conn.execute(
            "INSERT INTO wiki_claims (id, vault_id, claim_text, source_type)"
            " VALUES (1, 7, 'Claim one text', 'document')"
        )
        conn.execute(
            "INSERT INTO wiki_claims (id, vault_id, claim_text, source_type)"
            " VALUES (2, 7, 'Claim one text', 'document')"
        )
        conn.execute("INSERT INTO wiki_claim_sources (claim_id) VALUES (1)")
        conn.execute("INSERT INTO wiki_claim_sources (claim_id) VALUES (2)")
        conn.execute("INSERT INTO document_tags VALUES (1, 99)")
        conn.commit()
    finally:
        conn.close()

    try:
        migrate_add_wiki_claims_unique_claim_text(db)
    except RuntimeError:
        pass

    phase = _scalar(
        db,
        "SELECT phase FROM migration_journal"
        " WHERE migration_name = 'migrate_add_wiki_claims_unique_claim_text'"
        " AND phase != 'start' ORDER BY id DESC LIMIT 1",
    )
    assert phase == "succeeded"


def test_chat_messages_dedup_delete_is_journaled_with_count(tmp_path):
    db = tmp_path / "e.db"
    run_migrations(str(db))
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("DROP INDEX idx_chat_messages_session_turn_role")
        conn.execute(
            "INSERT INTO chat_sessions (id, title, vault_id)"
            " VALUES (501, 'b10 probe', 1)"
        )
        conn.execute(
            "INSERT INTO chat_messages (session_id, role, content, turn_id, status)"
            " VALUES (501, 'assistant', 'longer survivor content', 't1', 'complete')"
        )
        conn.execute(
            "INSERT INTO chat_messages (session_id, role, content, turn_id, status)"
            " VALUES (501, 'assistant', 'short', 't1', 'complete')"
        )
        conn.commit()
    finally:
        conn.close()

    snapshot = _journal_detail_count(str(db), "migrate_add_chat_turn_columns")
    migrate_add_chat_turn_columns(str(db))
    rows_with_count = (
        _journal_detail_count(str(db), "migrate_add_chat_turn_columns") - snapshot
    )
    assert rows_with_count >= 1


def test_rebuilt_signal_promise_has_a_reader():
    promise = "know to rebuild" in (inspect.getdoc(invalidate_derived_data) or "")
    app_root = Path(__file__).resolve().parents[1] / "app"
    readers = 0
    for path in sorted(app_root.rglob("*.py")):
        if path.parts[-2:] == ("models", "migration_journal.py"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value == "rebuilt":
                readers += 1
    unfulfilled = bool(promise and readers == 0)
    assert int(unfulfilled) == 0
