"""Regression tests for refresh-token rotation observability.

Issue #41, item 3: ``_rotate_refresh_token_block`` previously swallowed
``sqlite3.OperationalError`` around ``BEGIN EXCLUSIVE`` silently, hiding
lock/transaction problems from production logs. These tests assert the
function now emits a warning-level log record on the auth logger so the
fallback path is observable.

The tests use a ``MagicMock`` connection — the function under test does not
require a real SQLite database. We stub the optional heavy dependencies
that ``app.api.routes.auth`` imports transitively (lancedb, pyarrow,
unstructured) because the CI dependency set omits them, mirroring the
existing per-file stub pattern in this repo.
"""

import logging
import os
import sqlite3
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Test env — must be set before importing app.* modules
# ---------------------------------------------------------------------------
os.environ.setdefault("USERS_ENABLED", "false")
os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret-key-for-testing-only-1234567890")
os.environ.setdefault("ADMIN_SECRET_TOKEN", "test-admin-secret-token-for-testing-only-12345678")


# ---------------------------------------------------------------------------
# Stub optional heavy deps that may be missing in the CI environment.
# Mirrors backend/tests/test_deps_auth_to_thread.py.
# ---------------------------------------------------------------------------
def _stub_optional_deps() -> None:
    for mod_name in ("lancedb", "pyarrow"):
        sys.modules.setdefault(mod_name, types.ModuleType(mod_name))

    if "unstructured" not in sys.modules:
        _unstructured = types.ModuleType("unstructured")
        _unstructured.__path__ = []
        _partition = types.ModuleType("unstructured.partition")
        _partition.__path__ = []
        _auto = types.ModuleType("unstructured.partition.auto")
        _auto.partition = lambda *args, **kwargs: []
        _chunking = types.ModuleType("unstructured.chunking")
        _chunking.__path__ = []
        _title = types.ModuleType("unstructured.chunking.title")
        _title.chunk_by_title = lambda *args, **kwargs: []
        _documents = types.ModuleType("unstructured.documents")
        _documents.__path__ = []
        _elements = types.ModuleType("unstructured.documents.elements")
        _elements.Element = type("Element", (), {})
        sys.modules["unstructured"] = _unstructured
        sys.modules["unstructured.partition"] = _partition
        sys.modules["unstructured.partition.auto"] = _auto
        sys.modules["unstructured.chunking"] = _chunking
        sys.modules["unstructured.chunking.title"] = _title
        sys.modules["unstructured.documents"] = _documents
        sys.modules["unstructured.documents.elements"] = _elements


_stub_optional_deps()


# Imported after env + stubs so the Settings() validator sees test values.
from app.api.routes import auth as auth_routes  # noqa: E402

LOGGER_NAME = "app.api.routes.auth"


def _make_db_mock(begin_exclusive_raises: bool) -> MagicMock:
    """Return a mock connection that handles the rotate call.

    The function executes, in order:
      1. ``BEGIN EXCLUSIVE`` (or raises OperationalError)
      2. ``SELECT id FROM user_sessions ...``
      3. ``INSERT INTO user_sessions ...``
      4. ``DELETE FROM user_sessions ...``
      5. ``COMMIT``

    We make (2) find the session so the happy path is exercised; the only
    behaviour we vary is whether (1) raises.
    """
    db = MagicMock()

    def execute_side_effect(sql: str, *args, **kwargs):
        if begin_exclusive_raises and "BEGIN EXCLUSIVE" in sql:
            raise sqlite3.OperationalError(
                "cannot start a transaction within a transaction"
            )
        cur = MagicMock()
        # SELECT path: pretend the session is valid
        cur.fetchone.return_value = (1,)
        return cur

    db.execute.side_effect = execute_side_effect
    return db


def _new_expires_at() -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=30)


class TestRotateRefreshTokenBlockLogging(unittest.TestCase):
    """Issue #41: BEGIN EXCLUSIVE fallback must be observable."""

    def test_warns_when_begin_exclusive_raises(self) -> None:
        """When BEGIN EXCLUSIVE raises OperationalError, a WARNING is logged."""
        db = _make_db_mock(begin_exclusive_raises=True)

        with self.assertLogs(LOGGER_NAME, level="WARNING") as captured:
            auth_routes._rotate_refresh_token_block(
                db,
                session_id=1,
                token_hash="x" * 64,
                user_id=1,
                new_refresh_token_hash="y" * 64,
                new_expires_at=_new_expires_at(),
            )

        # At least one WARNING must be from our message, not a spurious import-time
        # warning. We check the message text. (Matches the production log message
        # in app/api/routes/auth.py::_rotate_refresh_token_block; previously this
        # test asserted "BEGIN EXCLUSIVE unavailable" but master already merged
        # the slightly more informative "BEGIN EXCLUSIVE failed for session %s
        # (user %s)" variant via PR #157, so we match that on the merge.)
        matching = [
            record for record in captured.records
            if "BEGIN EXCLUSIVE failed" in record.getMessage()
        ]
        self.assertEqual(
            len(matching),
            1,
            f"expected exactly one BEGIN EXCLUSIVE warning, got {len(matching)}: "
            f"{[r.getMessage() for r in captured.records]}",
        )
        self.assertEqual(matching[0].levelno, logging.WARNING)

    def test_no_warning_when_begin_exclusive_succeeds(self) -> None:
        """When BEGIN EXCLUSIVE succeeds, no warning is logged for the lock path."""
        db = _make_db_mock(begin_exclusive_raises=False)

        # assertNoLogs will FAIL if any WARNING/ERROR is emitted on the logger.
        with self.assertNoLogs(LOGGER_NAME, level="WARNING"):
            auth_routes._rotate_refresh_token_block(
                db,
                session_id=1,
                token_hash="x" * 64,
                user_id=1,
                new_refresh_token_hash="y" * 64,
                new_expires_at=_new_expires_at(),
            )


if __name__ == "__main__":
    unittest.main()
