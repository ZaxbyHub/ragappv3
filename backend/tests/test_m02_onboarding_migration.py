"""Issue #782 (UI-ENH-07 stage 2): committed coverage for the
``migrate_add_user_onboarding_state`` migration.

Pins the three properties the frozen route checks cannot see (they create
the table themselves for route-behavior isolation):

1. the REAL migration creates ``user_onboarding_state`` with exactly the
   columns the routes read/write (fresh-DB and legacy-DB paths converge on
   the SCHEMA double-definition convention);
2. the attempt is journaled start/succeeded in ``migration_journal``;
3. the migration is idempotent — a clean rerun converges and does not
   duplicate journal noise that anything could mistake for failure.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

_M02_ENV = {
    "ADMIN_SECRET_TOKEN": "test-secret",
    "USERS_ENABLED": "false",
    "JWT_SECRET_KEY": "test-jwt-secret-key-for-testing-only",
    "REDIS_URL": "",
}

_JOURNAL_NAME = "migrate_add_user_onboarding_state"

_EXPECTED_COLUMNS = {
    "user_id",
    "citation_opened_at",
    "checklist_dismissed_at",
    "updated_at",
}


@pytest.fixture(autouse=True, scope="module")
def _m02_hermetic_env():
    """Set hermetic env BEFORE any app import; restore afterwards."""
    saved = {key: os.environ.get(key) for key in _M02_ENV}
    os.environ.update(_M02_ENV)
    yield
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _legacy_db(tmp_path: Path) -> str:
    """A pre-#782 database: a minimal base schema WITHOUT the table."""
    db_path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            hashed_password TEXT NOT NULL
        );
        CREATE TABLE migration_journal (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            migration_name TEXT NOT NULL,
            phase TEXT NOT NULL,
            outcome TEXT NOT NULL,
            detail TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    conn.commit()
    conn.close()
    return db_path


def _table_columns(db_path: str, table: str) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


def _journal_rows(db_path: str, name: str) -> list[tuple[str, str]]:
    conn = sqlite3.connect(db_path)
    try:
        return [
            (row[0], row[1])
            for row in conn.execute(
                "SELECT phase, outcome FROM migration_journal "
                "WHERE migration_name = ? ORDER BY id",
                (name,),
            )
        ]
    finally:
        conn.close()


def test_migration_creates_table_on_legacy_db(tmp_path: Path) -> None:
    from app.models.database import migrate_add_user_onboarding_state

    db_path = _legacy_db(tmp_path)
    migrate_add_user_onboarding_state(db_path)

    assert _table_columns(db_path, "user_onboarding_state") == _EXPECTED_COLUMNS
    assert _journal_rows(db_path, _JOURNAL_NAME) == [("start", "ok"), ("succeeded", "ok")]


def test_migration_is_idempotent_on_rerun(tmp_path: Path) -> None:
    from app.models.database import migrate_add_user_onboarding_state

    db_path = _legacy_db(tmp_path)
    migrate_add_user_onboarding_state(db_path)
    # A pre-existing state row must survive the rerun (CREATE IF NOT EXISTS).
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO user_onboarding_state (user_id, citation_opened_at) "
        "VALUES (1, '2026-10-03T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()

    migrate_add_user_onboarding_state(db_path)

    assert _table_columns(db_path, "user_onboarding_state") == _EXPECTED_COLUMNS
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT citation_opened_at FROM user_onboarding_state WHERE user_id = 1"
        ).fetchall()
    finally:
        conn.close()
    assert rows == [("2026-10-03T00:00:00+00:00",)]
    # The journal records a second complete attempt (start + succeeded), never
    # a failure — the double-definition convention converges.
    assert _journal_rows(db_path, _JOURNAL_NAME)[-1] == ("succeeded", "ok")


def test_migration_pins_load_bearing_constraints(tmp_path: Path) -> None:
    """PRR-004: the route upserts' ``ON CONFLICT(user_id)`` requires
    ``user_id`` to be the PRIMARY KEY (a name-only check cannot see a
    regression that drops it — every write would then raise
    OperationalError → 503), and the two timestamp columns must stay
    nullable (a state row is created with only one of them set)."""
    from app.models.database import migrate_add_user_onboarding_state

    db_path = _legacy_db(tmp_path)
    migrate_add_user_onboarding_state(db_path)

    conn = sqlite3.connect(db_path)
    try:
        cols = {
            row[1]: row
            for row in conn.execute("PRAGMA table_info(user_onboarding_state)")
        }
    finally:
        conn.close()
    # PRAGMA table_info row shape: (cid, name, type, notnull, dflt_value, pk)
    assert cols["user_id"][5] == 1, "user_id must be the PRIMARY KEY (the ON CONFLICT conflict target)"
    assert cols["citation_opened_at"][3] == 0, "citation_opened_at must stay nullable"
    assert cols["checklist_dismissed_at"][3] == 0, "checklist_dismissed_at must stay nullable"


def test_fresh_db_schema_includes_table() -> None:
    """init_db's SCHEMA concat and the migration must converge (double
    definition): a fresh database reaches the same table shape."""
    from app.models.database import SCHEMA

    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(SCHEMA)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(user_onboarding_state)")}
    finally:
        conn.close()
    assert columns == _EXPECTED_COLUMNS
