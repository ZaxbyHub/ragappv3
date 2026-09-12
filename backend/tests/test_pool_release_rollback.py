"""Regression tests for issue #548: pooled connections must be reset on release.

Covers:
- ``release_connection`` rolls back an open transaction before the connection
  returns to the queue (and on the pool-full close path), so the next borrower
  never inherits a dirty connection.
- A dirty release cannot cause a phantom commit: a later borrower's commit()
  must not durably persist an earlier handler's abandoned rows.
- ``PRAGMA foreign_keys`` enforcement is restored after a dirty release (the
  PRAGMA silently no-ops inside a stale transaction).
- ``MaintenanceService.set_flag``'s optimistic-lock version-miss branch ends
  its own transaction instead of relying on the pool-level rollback.
- The release-time rollback emits a WARNING (``pool_release_rollback``) when it
  fires, and never fires for clean connections.
- ``_validate_connection``'s error-path rollback and clean-connection FK
  semantics are preserved.

Coverage note for issue #548's test item (2): the issue prescribes driving
``set_flag`` into its version-miss branch and then having a second borrower
commit an unrelated write. The version-miss UPDATE matches zero rows, so that
path abandons an EMPTY transaction — there is no "first handler's row" for a
set_flag trigger, and the literal composite assertion would be vacuous. The
scenario is therefore covered compositionally: ``test_set_flag_version_miss_rolls_back``
pins the set_flag-dirty-release half and ``test_phantom_commit_prevented`` pins
the abandoned-row half via a raw INSERT (mirroring the audit's P-B probe);
``test_set_flag_miss_then_second_borrower_commit`` additionally runs the
literal composite end-to-end as an integration pin.
"""
import logging
import sqlite3
from pathlib import Path

import pytest

from app.models.database import SQLiteConnectionPool
from app.services.maintenance import (
    MaintenanceError,
    MaintenanceFlag,
    MaintenanceService,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tmp_db_path(tmp_path: Path) -> str:
    return str(tmp_path / "pool-release.db")


@pytest.fixture
def pool(tmp_path):
    """A size-1 pool over a temp database with one trivial table."""
    p = SQLiteConnectionPool(sqlite_path=_tmp_db_path(tmp_path), max_size=1)
    conn = p.get_connection()
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    conn.commit()
    p.release_connection(conn)
    yield p
    p.close_all()


# ---------------------------------------------------------------------------
# AC1 — release-time rollback (queue path + pool-full close path)
# ---------------------------------------------------------------------------


def test_release_connection_rolls_back_open_transaction(pool):
    """Borrow, INSERT without commit, release, re-borrow: the connection must
    come back clean, and the abandoned row must not be visible."""
    c1 = pool.get_connection()
    c1.execute("INSERT INTO t (id, v) VALUES (1, 'leaked')")
    assert c1.in_transaction
    pool.release_connection(c1)

    c2 = pool.get_connection()
    try:
        assert not c2.in_transaction
        # A fresh transaction can be started on the re-borrowed connection.
        c2.execute("BEGIN IMMEDIATE")
        c2.rollback()
        # The uncommitted row is gone from every connection's view.
        assert c2.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0
    finally:
        pool.release_connection(c2)


def test_release_connection_full_branch_closes_rolled_back_connection(pool, caplog):
    """Pool-full release path: the extra connection is closed, the queued
    connection is untouched, and the dirty close is still rolled back+warned
    before closing (guard runs before the Full branch)."""
    queued = pool.get_connection()
    pool.release_connection(queued)  # queue now holds its one slot

    extra = pool._create_connection()
    try:
        extra.execute("INSERT INTO t (id, v) VALUES (2, 'full-branch')")
        assert extra.in_transaction
        with caplog.at_level(logging.WARNING, logger="app.models.database"):
            pool.release_connection(extra)  # queue full -> close
        # The connection was closed...
        with pytest.raises(sqlite3.ProgrammingError):
            extra.execute("SELECT 1")
        # ...after the guard rolled it back and warned (guard precedes Full).
        rollback_warnings = [
            r for r in caplog.records
            if r.levelno == logging.WARNING and "pool_release_rollback" in r.getMessage()
        ]
        assert len(rollback_warnings) == 1
        # The queued connection is unaffected and the pool still works.
        assert not queued.in_transaction
        again = pool.get_connection()
        try:
            assert not again.in_transaction
        finally:
            pool.release_connection(again)
    finally:
        # idempotent-close safety for the failure path
        try:
            extra.close()
        except sqlite3.Error:
            pass


# ---------------------------------------------------------------------------
# AC3 — set_flag version-miss branch ends its own transaction
# ---------------------------------------------------------------------------


def test_set_flag_version_miss_rolls_back(tmp_path, monkeypatch):
    """Drive set_flag into the optimistic-lock miss branch (rowcount == 0):
    every attempt must end its transaction; the connection returns clean and
    the flag row is unchanged."""
    p = SQLiteConnectionPool(sqlite_path=_tmp_db_path(tmp_path), max_size=1)
    conn = p.get_connection()
    conn.execute(
        """
        CREATE TABLE system_flags (
            name TEXT PRIMARY KEY,
            value INTEGER NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            version INTEGER NOT NULL DEFAULT 0,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        "INSERT INTO system_flags (name, value, reason, version) VALUES (?, 0, '', 5)",
        (MaintenanceService.FLAG_NAME,),
    )
    conn.commit()
    p.release_connection(conn)

    svc = MaintenanceService(p)
    # A stale version (0 vs the row's 5) forces UPDATE ... WHERE version = 0
    # to match nothing on every retry.
    monkeypatch.setattr(
        svc,
        "get_flag",
        lambda: MaintenanceFlag(enabled=True, reason="", version=0, updated_at=None),
    )

    with pytest.raises(MaintenanceError):
        svc.set_flag(enabled=True, reason="probe")

    conn2 = p.get_connection()
    try:
        assert not conn2.in_transaction
        row = conn2.execute(
            "SELECT value, reason, version FROM system_flags WHERE name = ?",
            (MaintenanceService.FLAG_NAME,),
        ).fetchone()
        assert tuple(row) == (0, "", 5)
    finally:
        p.release_connection(conn2)
    p.close_all()


# ---------------------------------------------------------------------------
# AC4 — cross-request phantom commit is impossible
# ---------------------------------------------------------------------------


def test_phantom_commit_prevented(pool):
    """Handler A dirty-releases; handler B borrows, writes its own row, and
    commits. B's commit must not durably persist A's abandoned row."""
    a = pool.get_connection()
    a.execute("INSERT INTO t (id, v) VALUES (1, 'abandoned')")
    pool.release_connection(a)

    b = pool.get_connection()
    b.execute("INSERT INTO t (id, v) VALUES (2, 'deliberate')")
    b.commit()
    pool.release_connection(b)

    check = pool.get_connection()
    try:
        rows = {
            row[0]: row[1]
            for row in check.execute("SELECT id, v FROM t").fetchall()
        }
    finally:
        pool.release_connection(check)
    assert 1 not in rows
    assert rows.get(2) == "deliberate"


# ---------------------------------------------------------------------------
# AC5 — PRAGMA foreign_keys restored after a dirty release
# ---------------------------------------------------------------------------


def test_foreign_keys_enforced_after_dirty_release(pool):
    """A connection dirty at release re-borrows with FK enforcement ON (the
    PRAGMA silently no-ops inside the stale transaction it used to inherit)."""
    c1 = pool.get_connection()
    c1.execute("INSERT INTO t (id, v) VALUES (3, 'dirty')")
    assert c1.in_transaction
    pool.release_connection(c1)

    c2 = pool.get_connection()
    try:
        assert not c2.in_transaction
        assert c2.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        pool.release_connection(c2)


# ---------------------------------------------------------------------------
# AC7 — clean connections: no warning, FK semantics unchanged
# ---------------------------------------------------------------------------


def test_clean_release_emits_no_warning_and_preserves_fk(pool, caplog):
    """Never-dirty connections pass through the guard untouched: no
    pool_release_rollback warning and FK stays ON across cycles."""
    c1 = pool.get_connection()
    assert c1.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    with caplog.at_level(logging.WARNING, logger="app.models.database"):
        pool.release_connection(c1)
    assert [r for r in caplog.records if "pool_release_rollback" in r.getMessage()] == []

    c2 = pool.get_connection()
    try:
        assert not c2.in_transaction
        assert c2.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        pool.release_connection(c2)

def test_validate_connection_error_path_rollback_preserved():
    """_validate_connection still rolls back and reports invalid on a hard
    SQLite error (the audit forbade weakening this path)."""
    p = SQLiteConnectionPool(sqlite_path=":memory:", max_size=1)
    conn = p._create_connection()
    try:
        assert p._validate_connection(conn) is True
        conn.close()
        assert p._validate_connection(conn) is False
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass


# ---------------------------------------------------------------------------
# AC8 — the release-time rollback logs a WARNING when it fires
# ---------------------------------------------------------------------------


def test_release_connection_warns_on_dirty_release(pool, caplog):
    """Exactly one WARNING tagged pool_release_rollback fires for a dirty
    release; a subsequent clean release adds none."""
    c1 = pool.get_connection()
    c1.execute("INSERT INTO t (id, v) VALUES (4, 'warn-me')")
    with caplog.at_level(logging.WARNING, logger="app.models.database"):
        pool.release_connection(c1)
    warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "pool_release_rollback" in r.getMessage()
    ]
    assert len(warnings) == 1

    c2 = pool.get_connection()
    try:
        assert not c2.in_transaction
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="app.models.database"):
            pool.release_connection(c2)
        assert [r for r in caplog.records if "pool_release_rollback" in r.getMessage()] == []
    finally:
        pool.release_connection(c2)


# ---------------------------------------------------------------------------
# PR-review feedback additions (PRR-001, PRR-005, PRR-006)
# ---------------------------------------------------------------------------


def test_set_flag_miss_then_second_borrower_commit(tmp_path, monkeypatch):
    """Literal composite of issue #548 test item (2): drive set_flag into its
    version-miss branch, then have a second borrower commit an unrelated
    write. The borrower's own row must be durably visible, the flag row must
    be untouched, and the borrowed connections must come back clean.

    Note: the version-miss UPDATE matches zero rows, so set_flag abandons an
    EMPTY transaction — the phantom-commit mechanism for a real abandoned ROW
    is pinned separately by test_phantom_commit_prevented (see module docstring).
    """
    from app.services.maintenance import MaintenanceFlag

    p = SQLiteConnectionPool(sqlite_path=_tmp_db_path(tmp_path), max_size=1)
    conn = p.get_connection()
    conn.execute(
        """
        CREATE TABLE system_flags (
            name TEXT PRIMARY KEY,
            value INTEGER NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            version INTEGER NOT NULL DEFAULT 0,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        "INSERT INTO system_flags (name, value, reason, version) VALUES (?, 0, '', 5)",
        (MaintenanceService.FLAG_NAME,),
    )
    conn.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY, v TEXT)")
    conn.commit()
    p.release_connection(conn)

    svc = MaintenanceService(p)
    monkeypatch.setattr(
        svc,
        "get_flag",
        lambda: MaintenanceFlag(enabled=True, reason="", version=0, updated_at=None),
    )
    with pytest.raises(MaintenanceError):
        svc.set_flag(enabled=True, reason="composite-probe")

    # Second borrower: its own write must survive its commit.
    b = p.get_connection()
    assert not b.in_transaction
    b.execute("INSERT INTO unrelated (id, v) VALUES (1, 'b-row')")
    b.commit()
    pool_release_connection = p.release_connection
    pool_release_connection(b)

    check = p.get_connection()
    try:
        assert not check.in_transaction
        assert check.execute("SELECT COUNT(*) FROM unrelated").fetchone()[0] == 1
        row = check.execute(
            "SELECT value, reason, version FROM system_flags WHERE name = ?",
            (MaintenanceService.FLAG_NAME,),
        ).fetchone()
        assert tuple(row) == (0, "", 5)
    finally:
        p.release_connection(check)
    p.close_all()


def test_release_connection_with_closed_connection_does_not_raise(tmp_path):
    """PRR-005: releasing an already-closed connection must not raise — the
    guard degrades to no-op (in_transaction raises ProgrammingError, a
    sqlite3.Error subclass) — and the pool self-heals on the next checkout
    (_validate_connection discards the dead connection)."""
    p = SQLiteConnectionPool(sqlite_path=_tmp_db_path(tmp_path), max_size=2)
    dead = p._create_connection()
    dead.close()

    # Must not raise (pre-fix code would not raise either — the guard's
    # wrapped in_transaction access keeps it that way defensively).
    p.release_connection(dead)

    healthy = p.get_connection()
    try:
        assert not healthy.in_transaction
        healthy.execute("SELECT 1")
    finally:
        p.release_connection(healthy)
    p.close_all()


class _RollbackFailingConnProxy:
    """Proxy over a real connection whose rollback() raises sqlite3.Error.

    sqlite3.Connection attributes are read-only C slots, so failure injection
    requires a delegating proxy (same pattern as test_auth_atomicity.py).
    """

    def __init__(self, real: sqlite3.Connection) -> None:
        object.__setattr__(self, "_real", real)

    @property
    def in_transaction(self) -> bool:  # type: ignore[override]
        return object.__getattribute__(self, "_real").in_transaction

    def rollback(self) -> None:
        raise sqlite3.OperationalError("injected rollback failure")

    def close(self) -> None:
        object.__getattribute__(self, "_real").close()

    def execute(self, *args, **kwargs):
        return object.__getattribute__(self, "_real").execute(*args, **kwargs)

    def commit(self) -> None:
        object.__getattribute__(self, "_real").commit()

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_real"), name)


def test_release_connection_rollback_failure_logged_not_raised(tmp_path, caplog):
    """PRR-006: when rollback() itself fails on a dirty connection, the guard
    logs a second pool_release_rollback WARNING and never propagates; the
    pool-full close path still completes."""
    p = SQLiteConnectionPool(sqlite_path=_tmp_db_path(tmp_path), max_size=1)
    queued = p.get_connection()
    queued.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    queued.commit()
    p.release_connection(queued)  # fill the queue -> next release takes Full branch

    real = p._create_connection()
    proxy = _RollbackFailingConnProxy(real)
    proxy.execute("INSERT INTO t (id) VALUES (1)")  # open a transaction on the real conn
    assert proxy.in_transaction

    with caplog.at_level(logging.WARNING, logger="app.models.database"):
        p.release_connection(proxy)  # type: ignore[arg-type]

    warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "pool_release_rollback" in r.getMessage()
    ]
    assert len(warnings) == 2  # dirty-release warning + rollback-failed warning
    assert "rollback_failed=1" in warnings[1].getMessage()
    # The proxy was closed by the Full branch (idempotent on the real conn).
    with pytest.raises(sqlite3.ProgrammingError):
        real.execute("SELECT 1")
    # The queued connection and the pool remain usable.
    again = p.get_connection()
    try:
        assert not again.in_transaction
    finally:
        p.release_connection(again)
    p.close_all()
