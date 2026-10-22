"""Supplementary guards for the 987-993-995 collated fix (non-frozen).

Three concerns the frozen checks do not bind on their own:

1. A POSITIVE #993-AC1 binding: in the single-admin posture the audit key
   must EQUAL the configured ADMIN_SECRET_TOKEN (the frozen C1 check only
   proves the key is not the placeholder — the public development constant
   would also pass it), and a genuinely-set JWT secret must stay preferred.
2. The single-``failed``-row invariant and the exact journal shapes of the
   reworked startup redaction (#995): BEGIN failure journals exactly
   ``[failed]`` (the #995 realignment: ``start`` is journaled only once the
   write lock is held), data failure journals exactly ``[start, failed]``
   with the audit rows reverted.
3. Unit coverage for ``constant_time_equals`` (#993-AC2's helper): the
   non-ASCII str pair that made ``secrets.compare_digest`` raise ``TypeError``
   must compare equal as utf-8 bytes, unequal ASCII must stay False, and
   non-``str`` input must fail closed instead of raising.
"""

import sqlite3
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from app.config import _INSECURE_DEFAULT_JWT_SECRET, settings
from app.models.database import run_migrations
from app.security import constant_time_equals
from app.services.audit_keys import derive_audit_key
from app.services.audit_redaction import redact_historical_audit_rows

REAL_ADMIN_TOKEN = "issue993-real-admin-token-4d5e6f"
REAL_JWT_SECRET = "issue993-real-jwt-secret-0123456789abcdef"
TEST_KEY = b"issue995-extra-guards-key"
TEST_KEY_VERSION = "v-extra-guards"
MIGRATION = "redact_historical_audit_rows"


@contextmanager
def _settings_attrs(**overrides):
    """patch.object-based settings override (census-visible restore shape)."""
    tokens = {
        field: patch.object(settings, field, value, create=True)
        for field, value in overrides.items()
    }
    for token in tokens.values():
        token.start()
    try:
        yield
    finally:
        for token in tokens.values():
            token.stop()


class TestAuditKeyDerivation:
    """#993-AC1 positive binding (companion to the frozen C1 check)."""

    def test_single_admin_posture_derives_the_admin_token_bytes(self):
        with _settings_attrs(
            jwt_secret_key=_INSECURE_DEFAULT_JWT_SECRET,
            admin_secret_token=REAL_ADMIN_TOKEN,
        ):
            key = derive_audit_key()
        assert key == REAL_ADMIN_TOKEN.encode("utf-8"), (
            "with the JWT secret left at its public placeholder the audit "
            "HMAC key must be the configured ADMIN_SECRET_TOKEN, not the "
            "placeholder and not the public development constant"
        )

    def test_changed_jwt_secret_stays_preferred(self):
        with _settings_attrs(
            jwt_secret_key=REAL_JWT_SECRET,
            admin_secret_token=REAL_ADMIN_TOKEN,
        ):
            key = derive_audit_key()
        assert key == REAL_JWT_SECRET.encode("utf-8")


class _FailOnBeginConnection:
    """Real-connection proxy whose BEGIN IMMEDIATE raises (wiring-test shape)."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def execute(self, sql, *args, **kwargs):
        if str(sql).strip().upper().startswith("BEGIN IMMEDIATE"):
            raise sqlite3.OperationalError("issue995 injected BEGIN failure")
        return self._conn.execute(sql, *args, **kwargs)


def _connect_failing_on_begin(*args, **kwargs):
    return _FailOnBeginConnection(sqlite3.Connection(*args, **kwargs))


def _temp_db_with_pending_row(tmp_path) -> str:
    db = str(tmp_path / "guards.db")
    run_migrations(db)
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute(
        "INSERT INTO audit_toggle_log (feature, enabled, user_id, ip, timestamp,"
        " hmac_sha256, key_version) VALUES ('issue995', 1, 'legacy-token-abc',"
        " '127.0.0.1', '2026-01-01T00:00:00', 'deadbeef', 'v-old')"
    )
    conn.commit()
    conn.close()
    return db


def _journal(db: str) -> list[tuple[str, str, str]]:
    conn = sqlite3.connect(db)
    try:
        return [
            (r[0], r[1], r[2])
            for r in conn.execute(
                "SELECT migration_name, phase, outcome FROM migration_journal"
                " WHERE migration_name = ? ORDER BY id",
                (MIGRATION,),
            ).fetchall()
        ]
    finally:
        conn.close()


class TestRedactionJournalShapes:
    """#995 single-``failed``-row invariant and exact journal shapes."""

    def test_begin_failure_journals_exactly_one_failed_row(self, tmp_path):
        db = _temp_db_with_pending_row(tmp_path)
        with patch.object(sqlite3, "connect", _connect_failing_on_begin):
            with pytest.raises(sqlite3.OperationalError):
                redact_historical_audit_rows(db, TEST_KEY, TEST_KEY_VERSION)
        assert _journal(db) == [(MIGRATION, "failed", "error")], (
            "a BEGIN IMMEDIATE failure must journal exactly the terminal "
            "failed row (start is journaled only once the lock is held)"
        )

    def test_data_failure_journals_start_then_failed_and_reverts_data(self, tmp_path):
        db = _temp_db_with_pending_row(tmp_path)
        trigger_conn = sqlite3.connect(db)
        trigger_conn.execute(
            "CREATE TRIGGER issue995_guards_fail_update BEFORE UPDATE ON"
            " audit_toggle_log BEGIN SELECT RAISE(ABORT, 'injected'); END"
        )
        trigger_conn.commit()
        trigger_conn.close()
        # RAISE(ABORT) surfaces as sqlite3.IntegrityError (probed).
        with pytest.raises(sqlite3.IntegrityError, match="injected"):
            redact_historical_audit_rows(db, TEST_KEY, TEST_KEY_VERSION)
        assert _journal(db) == [
            (MIGRATION, "start", "ok"),
            (MIGRATION, "failed", "error"),
        ], (
            "a data failure after the lock is held must keep the in-txn "
            "start row and append exactly one terminal failed row"
        )
        conn = sqlite3.connect(db)
        try:
            row = conn.execute(
                "SELECT user_id FROM audit_toggle_log WHERE feature ="
                " 'issue995'"
            ).fetchone()
            assert row[0] == "legacy-token-abc", "data must be reverted"
        finally:
            conn.close()


class TestConstantTimeEquals:
    """#993-AC2 helper totality (companion to the frozen C2 check)."""

    def test_non_ascii_equal_pair_compares_equal(self):
        assert constant_time_equals("tökén-Ω", "tökén-Ω") is True

    def test_non_ascii_pair_does_not_raise_against_ascii(self):
        assert constant_time_equals("tökén", "plain-token") is False

    def test_ascii_unequal_stays_false(self):
        assert constant_time_equals("alice-token", "bob-token") is False

    def test_non_str_fails_closed(self):
        assert constant_time_equals(None, "token") is False
        assert constant_time_equals(12345, "token") is False
        assert constant_time_equals(b"bytes-token", "token") is False

    def test_empty_pair_equals(self):
        assert constant_time_equals("", "") is True

    def test_lone_surrogate_fails_closed_not_raises(self):
        # PR #996 review round (hermes HF-1): a lone surrogate (e.g. a JWT
        # claim JSON-decoded from "\ud800") cannot utf-8-encode; the helper
        # must fail closed instead of raising UnicodeEncodeError.
        lone = "left side \ud800 claim"
        assert constant_time_equals(lone, "token") is False
        assert constant_time_equals("token", lone) is False
        # Fail-closed semantics: malformed material never compares True,
        # even against itself (there is no meaningful equal to report).
        both = "a \ud800 b"
        assert constant_time_equals(both, both) is False
