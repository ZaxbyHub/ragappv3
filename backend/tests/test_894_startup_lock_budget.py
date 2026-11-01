"""Acceptance checks C3-C6 (issue #995): startup audit-redaction lock budget
and journal/data contracts.

At BASE these checks are RED because ``redact_historical_audit_rows``
performed three sequential blocking writes against the write lock (the
journal ``start`` row on autocommit, ``BEGIN IMMEDIATE``, then the terminal
``failed`` row), each contending on the full sqlite busy timeout
(``MIGRATION_CONNECT_TIMEOUT_SECONDS`` = 30 s), so a boot on a database
whose write lock is held stalled ~90 s before failing (issue #995). AFTER
the fix they are GREEN: the routine acquires the write lock exactly once
(``BEGIN IMMEDIATE`` before any journal write, journal rows inside that
transaction, a ``SAVEPOINT`` around the data work) and writes at most one
bounded best-effort ``failed`` row on failure, so a held lock costs one
connect timeout plus the bounded write — inside the budgets asserted below.

* C3/AC3 (DISCRIMINATING, RED at base ~90 s, GREEN after the fix <= ~35 s):
  with the SHIPPED 30 s timeout and a second connection holding
  ``BEGIN IMMEDIATE`` (+ INSERT making it a real write transaction), the
  routine must complete or fail within 40 s wall clock. Failure itself is
  allowed — the elapsed-time budget is the assertion.
* C4/AC4 (DISCRIMINATING, RED at base ~1.5-1.9 s, GREEN after the fix ~0.5 s):
  the same held-lock setup with ``MIGRATION_CONNECT_TIMEOUT_SECONDS``
  monkeypatched to 0.5 in the ``audit_redaction`` module namespace must
  finish under 1.0 s — pinning the STRUCTURE (at most one blocking write),
  not a slow number.
* C5/AC5 (PRESERVING, GREEN at base): when the data work fails after the lock
  is acquired, audit rows are unchanged and a terminal ``failed`` journal row
  exists for migration_name 'redact_historical_audit_rows'. The failure is
  injected with a ``BEFORE UPDATE ... RAISE(ABORT)`` trigger — robust against
  any refactor of the module's SQL text (an alternative injector would be
  monkeypatching ``_ROWS_SQL_WITH_TOKEN``/``_ROWS_SQL_NO_TOKEN``).
* C6/AC6 (PRESERVING, GREEN at base): success path — a seeded row signed with
  the REAL ``_compute_hmac`` from ``app.api.routes.admin`` is redacted, the
  journal carries start+succeeded, the rewritten row's digest verifies under
  the passed key, and a second run is a no-op.

The DB is WAL after ``run_migrations`` (reads still succeed under a held
BEGIN IMMEDIATE, which is why only the three writes block). Mirrors the
fixture shapes of tests/test_audit_hmac_redaction.py.
"""

import hashlib
import hmac
import os
import sqlite3
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.api.routes.admin import _compute_hmac  # noqa: E402
from app.config import settings  # noqa: E402
from app.models.database import run_migrations  # noqa: E402
from app.services import audit_redaction  # noqa: E402
from app.services.audit_redaction import (  # noqa: E402
    redact_historical_audit_rows,
)

CURRENT_TOKEN = "issue995-current-admin-token"
TEST_KEY = b"issue995-redaction-test-key"
TEST_KEY_VERSION = "v-issue995"
SEED_IP = "10.9.8.7"
MIGRATION_NAME = "redact_historical_audit_rows"
_VERIFIED_FEATURE = "issue995_verified_row"


def _migrated_db(tmp_path) -> str:
    db = str(Path(tmp_path) / "lock_budget.db")
    run_migrations(db)
    return db


def _seed_pending_row(db_path: str, feature: str = "issue995_pending") -> None:
    """Seed one legacy token-material row (the pre-#894 before-state)."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute(
            "INSERT INTO audit_toggle_log"
            "(feature, enabled, user_id, ip, timestamp, hmac_sha256, key_version)"
            " VALUES (?, 1, 'legacy-token-xyz', '127.0.0.1',"
            " '2026-01-01T00:00:00', 'deadbeef', 'v1')",
            (feature,),
        )
        conn.commit()
    finally:
        conn.close()


@contextmanager
def _configured_admin():
    """Patch BOTH token settings so the current token is deterministic."""
    with patch.object(settings, "admin_secret_token", CURRENT_TOKEN):
        with patch.object(
            settings, "admin_token_scopes", {CURRENT_TOKEN: ["admin:config"]}
        ):
            yield


@contextmanager
def _held_write_lock(db_path: str):
    """Hold the database write lock from a second connection.

    ``BEGIN IMMEDIATE`` plus an INSERT makes the transaction a real write
    transaction; WAL lets readers through, so every WRITE from the routine
    under test contends on the busy timeout.
    """
    holder = sqlite3.connect(db_path, timeout=0.1, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    holder.execute(
        "INSERT INTO audit_toggle_log"
        "(feature, enabled, user_id, ip, timestamp, hmac_sha256, key_version)"
        " VALUES ('issue995_holder', 1, 'holder', '127.0.0.1',"
        " '2026-01-01T00:00:00', 'x', 'v1')"
    )
    try:
        yield holder
    finally:
        holder.execute("ROLLBACK")
        holder.close()


def _audit_rows(db_path: str) -> list[tuple]:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT feature, enabled, user_id, ip, timestamp, key_version,"
            " hmac_sha256 FROM audit_toggle_log WHERE feature != 'issue995_holder'"
            " ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


def _journal_phases(db_path: str, migration_name: str = MIGRATION_NAME) -> list[str]:
    conn = sqlite3.connect(db_path)
    try:
        return [
            r[0]
            for r in conn.execute(
                "SELECT phase FROM migration_journal"
                " WHERE migration_name = ? ORDER BY id",
                (migration_name,),
            ).fetchall()
        ]
    finally:
        conn.close()


def test_shipped_timeout_held_lock_completes_within_40s(tmp_path):
    """C3/AC3: SHIPPED 30 s busy timeout, held write lock, 40 s budget.

    RED at base (three sequential blocking writes take ~90 s), GREEN after
    the fix (<= ~35 s). An OperationalError from the routine is acceptable —
    only the elapsed wall-clock budget is asserted.
    """
    assert audit_redaction.MIGRATION_CONNECT_TIMEOUT_SECONDS == 30.0, (
        "this check must run against the SHIPPED connect timeout (30 s); "
        "patching it here would void the wall-clock budget"
    )
    db = _migrated_db(tmp_path)
    _seed_pending_row(db)
    with _configured_admin():
        with _held_write_lock(db):
            t0 = time.monotonic()
            try:
                redact_historical_audit_rows(db, TEST_KEY, TEST_KEY_VERSION)
            except sqlite3.OperationalError:
                pass  # failure is allowed; the budget is the assertion
            elapsed = time.monotonic() - t0
    assert elapsed < 40.0, (
        f"ELAPSED {elapsed:.2f}s exceeds budget (issue #995 AC3): with the "
        "shipped 30 s busy timeout and a held write lock, "
        "redact_historical_audit_rows must complete or fail within 40 s "
        "wall clock; ~90 s means three sequential blocking writes "
        "(journal start + BEGIN IMMEDIATE + journal failed)"
    )


def test_patched_timeout_allows_at_most_one_blocking_write(tmp_path, monkeypatch):
    """C4/AC4: connect timeout patched to 0.5 s -> must finish under 1.0 s.

    RED at base (~1.5-1.9 s: three 0.5 s blocking writes), GREEN after the
    fix (~0.5 s: at most one blocking write). This pins the STRUCTURE, not a
    slow number.
    """
    monkeypatch.setattr(
        audit_redaction, "MIGRATION_CONNECT_TIMEOUT_SECONDS", 0.5
    )
    db = _migrated_db(tmp_path)
    _seed_pending_row(db)
    with _configured_admin():
        with _held_write_lock(db):
            t0 = time.monotonic()
            try:
                redact_historical_audit_rows(db, TEST_KEY, TEST_KEY_VERSION)
            except sqlite3.OperationalError:
                pass  # failure is allowed; the budget is the assertion
            elapsed = time.monotonic() - t0
    assert elapsed < 1.0, (
        f"ELAPSED {elapsed:.2f}s exceeds budget (issue #995 AC4): with the "
        "connect timeout patched to 0.5 s the routine may block on at most "
        "ONE write (~0.5 s); ~1.5 s means three sequential blocking writes "
        "remain"
    )


def test_data_failure_keeps_rows_and_journals_terminal_failed(tmp_path):
    """C5/AC5 (PRESERVING, GREEN at base): data work failing after the lock
    is acquired leaves the audit rows unchanged (rollback) and records a
    terminal ``failed`` journal row — the b10 contract shape for the sibling.
    """
    db = _migrated_db(tmp_path)
    _seed_pending_row(db, feature="issue995_pending")
    before = _audit_rows(db)

    # Inject the data failure with a BEFORE UPDATE trigger: the routine's
    # UPDATE of audit_toggle_log aborts INSIDE the transaction, whatever the
    # SQL text looks like (robust to refactors of _ROWS_SQL_*).
    trigger_conn = sqlite3.connect(db)
    try:
        trigger_conn.execute(
            "CREATE TRIGGER issue995_fail_update BEFORE UPDATE ON audit_toggle_log"
            " BEGIN SELECT RAISE(ABORT, 'issue995 injected data failure'); END"
        )
        trigger_conn.commit()
    finally:
        trigger_conn.close()

    with _configured_admin():
        try:
            redact_historical_audit_rows(db, TEST_KEY, TEST_KEY_VERSION)
        except Exception:
            pass  # the routine re-raises after journaling; that is its call

    after = _audit_rows(db)
    assert after == before, (
        "audit rows must be unchanged when the data work fails after the "
        "lock is acquired (issue #995 AC5): the transaction must roll back. "
        f"before={before} after={after}"
    )
    phases = _journal_phases(db)
    assert "failed" in phases, (
        "a terminal 'failed' journal row must exist for migration_name "
        f"'{MIGRATION_NAME}' when the data work fails (issue #995 AC5); "
        f"journal phases={phases}"
    )
    assert "succeeded" not in phases, (
        "a failed data pass must not journal 'succeeded' (issue #995 AC5); "
        f"journal phases={phases}"
    )


def test_success_path_journals_and_reverifies_idempotently(tmp_path):
    """C6/AC6 (PRESERVING, GREEN at base): success path — seeded verified row
    (digest computed with the REAL ``_compute_hmac`` from
    ``app.api.routes.admin``) is rewritten, journal carries start+succeeded,
    the rewritten digest verifies under the passed key, second run no-ops.
    """
    db = _migrated_db(tmp_path)
    digest, timestamp = _compute_hmac(
        TEST_KEY, _VERIFIED_FEATURE, True, CURRENT_TOKEN, SEED_IP
    )
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO audit_toggle_log"
            "(feature, enabled, user_id, ip, timestamp, hmac_sha256, key_version)"
            " VALUES (?, 1, ?, ?, ?, ?, ?)",
            (
                _VERIFIED_FEATURE,
                CURRENT_TOKEN,
                SEED_IP,
                timestamp,
                digest,
                TEST_KEY_VERSION,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    with _configured_admin():
        first = redact_historical_audit_rows(db, TEST_KEY, TEST_KEY_VERSION)
        after_first = _audit_rows(db)
        second = redact_historical_audit_rows(db, TEST_KEY, TEST_KEY_VERSION)
        after_second = _audit_rows(db)

    assert isinstance(first, int) and first >= 1, (
        f"the seeded verified row must be counted as redacted, got {first!r}"
    )
    phases = _journal_phases(db)
    assert "start" in phases and "succeeded" in phases, (
        "a successful pass must journal both 'start' and 'succeeded' rows "
        f"for '{MIGRATION_NAME}' (issue #995 AC6); journal phases={phases}"
    )

    (row,) = after_first
    feature, enabled, user_id, ip, ts, key_version, stored = row
    assert user_id != CURRENT_TOKEN, (
        "the raw admin token must not survive in user_id after a successful "
        "redaction pass"
    )
    message = f"{feature}|{int(bool(enabled))}|{user_id or ''}|{ip or ''}|{ts}"
    expected = hmac.new(
        TEST_KEY, message.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    assert hmac.compare_digest(expected, stored or ""), (
        "the rewritten row must carry a digest that verifies under the "
        "passed key over its post-redaction identity (issue #995 AC6)"
    )
    assert key_version == TEST_KEY_VERSION, (
        f"key_version must be the passed version {TEST_KEY_VERSION!r}, "
        f"got {key_version!r}"
    )
    assert second == 0, f"idempotent re-run must redact 0 rows, got {second}"
    assert after_second == after_first, (
        "idempotent re-run must leave every audit row byte-identical"
    )
