"""Issue #697 feedback round (PRR-004 + PRR-005): committed coverage for the
reworked ``migrate_add_document_near_dups``.

The rework (reviewer round 1 on PR #838) added three behaviors that, per the
reviewer's own mutation probe, no committed test pinned:

1. journaling — start/succeeded/failed rows land in ``migration_journal``
   (peer ``migrate_widen_*`` shape; the journal never raises);
2. atomicity — the PRAGMA-guarded ``ALTER TABLE`` and the legacy-row backfill
   run inside ONE ``BEGIN IMMEDIATE`` transaction, so a backfill failure must
   roll the column back too (no column-added/rows-unstamped window for the
   reader's ``embedding_model = ?`` filter to silently exploit);
3. failure semantics — a failed migration journals ``failed`` and re-raises
   (the existing degraded-startup surface in lifespan is unchanged), and a
   clean rerun converges.

These tests pin all three, including the failure path via an injected RAISE
trigger — the exact ad-hoc probe shape the reviewer ran.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

_B08_ENV = {
    "ADMIN_SECRET_TOKEN": "test-secret",
    "USERS_ENABLED": "false",
    "JWT_SECRET_KEY": "test-jwt-secret-key-for-testing-only",
    "REDIS_URL": "",
}

_JOURNAL_NAME = "migrate_add_document_near_dups"


@pytest.fixture(autouse=True, scope="module")
def _b08_hermetic_env():
    """Set hermetic env BEFORE any app import; restore afterwards."""
    saved = {key: os.environ.get(key) for key in _B08_ENV}
    os.environ.update(_B08_ENV)
    yield
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _legacy_db(tmp_path: Path) -> tuple[str, sqlite3.Connection]:
    """A pre-#697 database: document_near_dups WITHOUT embedding_model,
    holding one legacy embedding row (dim 8) and one fingerprint row
    (dim 256)."""
    db_path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE document_near_dups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vault_id INTEGER NOT NULL,
            file_id INTEGER NOT NULL UNIQUE,
            centroid BLOB NOT NULL,
            dim INTEGER NOT NULL,
            group_id TEXT,
            similarity REAL,
            computed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        "INSERT INTO document_near_dups (vault_id, file_id, centroid, dim)"
        " VALUES (1, 11, X'0000000000000000', 8)"
    )
    conn.execute(
        "INSERT INTO document_near_dups (vault_id, file_id, centroid, dim)"
        " VALUES (1, 12, X'0000000000000000', 256)"
    )
    conn.commit()
    return db_path, conn


def _journal_rows(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    try:
        cursor = conn.execute(
            "SELECT phase, outcome FROM migration_journal"
            " WHERE migration_name = ? ORDER BY id",
            (_JOURNAL_NAME,),
        )
    except sqlite3.OperationalError:
        # Journal table never created: nothing was ever written (issue #699
        # makes no-op runs journal-silent).
        return []
    return [(row[0], row[1]) for row in cursor.fetchall()]


def _has_embedding_model(conn: sqlite3.Connection) -> bool:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(document_near_dups)")}
    return "embedding_model" in cols


def test_backfill_stamps_legacy_rows_and_journals(tmp_path) -> None:
    from app.config import settings
    from app.models.database import migrate_add_document_near_dups

    db_path, conn = _legacy_db(tmp_path)
    try:
        migrate_add_document_near_dups(db_path)

        assert _has_embedding_model(conn)
        stamped = dict(
            conn.execute(
                "SELECT file_id, embedding_model FROM document_near_dups"
            ).fetchall()
        )
        assert stamped[11] == str(settings.embedding_model)
        assert stamped[12] is None  # dim=256 fingerprint row stays NULL
        phases = _journal_rows(conn)
        assert phases[0] == ("start", "ok")
        assert phases[-1] == ("succeeded", "ok")

        # Idempotent rerun: no error, stamping unchanged, and — since issue
        # #699 — a no-op rerun is journal-SILENT (the row count does not grow).
        rows_before = _journal_rows(conn)
        migrate_add_document_near_dups(db_path)
        assert stamped == dict(
            conn.execute(
                "SELECT file_id, embedding_model FROM document_near_dups"
            ).fetchall()
        )
        assert _journal_rows(conn) == rows_before
    finally:
        conn.close()


def test_backfill_failure_rolls_back_column_and_journals_failed(tmp_path) -> None:
    from app.models.database import migrate_add_document_near_dups

    db_path, conn = _legacy_db(tmp_path)
    try:
        conn.execute(
            """
            CREATE TRIGGER b08_inject_backfill_failure
            BEFORE UPDATE ON document_near_dups
            BEGIN
                SELECT RAISE(ABORT, 'injected backfill failure');
            END
            """
        )
        conn.commit()

        with pytest.raises(sqlite3.IntegrityError, match="injected backfill failure"):
            migrate_add_document_near_dups(db_path)

        # Atomicity (PRR-005): the ALTER rolled back WITH the failed backfill,
        # so no column-added/rows-unstamped window exists after the failure.
        assert not _has_embedding_model(conn)
        # Journal (PRR-004): the attempt is recorded as failed, never silent.
        phases = _journal_rows(conn)
        assert phases[0] == ("start", "ok")
        assert ("failed", "error") in phases

        # Recovery: removing the trigger, the idempotent rerun converges.
        conn.execute("DROP TRIGGER b08_inject_backfill_failure")
        conn.commit()
        migrate_add_document_near_dups(db_path)
        assert _has_embedding_model(conn)
        assert _journal_rows(conn)[-1] == ("succeeded", "ok")
    finally:
        conn.close()


def test_fresh_db_migration_is_a_journaled_noop(tmp_path) -> None:
    from app.models.database import init_db, migrate_add_document_near_dups

    db_path = str(tmp_path / "fresh.db")
    init_db(db_path)  # _BASE_SCHEMA already carries embedding_model
    conn = sqlite3.connect(db_path)
    try:
        migrate_add_document_near_dups(db_path)
        assert _has_embedding_model(conn)
        # Issue #699: a no-op run (column present, nothing to stamp) is
        # journal-silent — routine per-boot rows no longer bury real signal.
        assert _journal_rows(conn) == []
    finally:
        conn.close()
