"""
Regression tests for auth.py single-transaction atomicity (#405, closes #393).

Covers the four defects from the Phase-2 atomicity cluster of #202:

- F-2.1: username uniqueness check must happen INSIDE BEGIN IMMEDIATE so a
  concurrent duplicate registration gets a clean 409, not an IntegrityError
  converted to 500.
- F-2.2: change-password UPDATE + session DELETE + new-session INSERT must
  commit in a single transaction so a crash between cannot leave the user
  with a changed password and no valid session.
- F-2.3: register user INSERT + session INSERT must commit in a single
  transaction so a crash between cannot leave a registered user with no
  session.
- F-2.4: _record_failed_attempt_db must re-read failed_attempts under the
  write lock before deciding to set locked_until, so concurrent failed
  logins cannot bypass the lockout threshold on a stale snapshot.

Tests are written against REAL production functions (imported), not replicas
of the logic. The transaction-boundary tests (Test 2 / Test 6) are true
regressions: they FAIL on pre-fix code because the pre-fix code has no
in-transaction uniqueness SELECT (F-2.1) and no post-UPDATE re-read (F-2.4).
The session-failure monkeypatch tests (Test 3 / Test 4) are true regressions:
they FAIL on pre-fix code because the pre-fix code commits the user/password
row in a separate transaction from the session INSERT.
"""

import os
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Stub missing optional dependencies (matches the pattern in test_auth_routes.py)
try:
    import lancedb  # noqa: F401
except ImportError:
    import types

    sys.modules["lancedb"] = types.ModuleType("lancedb")

try:
    import pyarrow  # noqa: F401
except ImportError:
    import types

    sys.modules["pyarrow"] = types.ModuleType("pyarrow")

try:
    from unstructured.partition.auto import partition  # noqa: F401
except ImportError:
    import types

    _unstructured = types.ModuleType("unstructured")
    _unstructured.__path__ = []
    _unstructured.partition = types.ModuleType("unstructured.partition")
    _unstructured.partition.__path__ = []
    _unstructured.partition.auto = types.ModuleType("unstructured.partition.auto")
    _unstructured.partition.auto.partition = lambda *args, **kwargs: []
    _unstructured.chunking = types.ModuleType("unstructured.chunking")
    _unstructured.chunking.__path__ = []
    _unstructured.chunking.title = types.ModuleType("unstructured.chunking.title")
    _unstructured.chunking.title.chunk_by_title = lambda *args, **kwargs: []
    _unstructured.documents = types.ModuleType("unstructured.documents")
    _unstructured.documents.__path__ = []
    _unstructured.documents.elements = types.ModuleType(
        "unstructured.documents.elements"
    )
    _unstructured.documents.elements.Element = type("Element", (), {})
    sys.modules["unstructured"] = _unstructured
    sys.modules["unstructured.partition"] = _unstructured.partition
    sys.modules["unstructured.partition.auto"] = _unstructured.partition.auto
    sys.modules["unstructured.chunking"] = _unstructured.chunking
    sys.modules["unstructured.chunking.title"] = _unstructured.chunking.title
    sys.modules["unstructured.documents"] = _unstructured.documents
    sys.modules["unstructured.documents.elements"] = _unstructured.documents.elements

from fastapi.testclient import TestClient

from app.config import settings
from app.models.database import SQLiteConnectionPool, init_db, run_migrations


def _make_test_client_and_pool():
    """Build a (client, app, pool, db_path, temp_dir) tuple for an isolated test.

    Mirrors the setUp pattern of test_auth_routes.py.
    """
    temp_dir = tempfile.mkdtemp()
    db_path = os.path.join(temp_dir, "test.db")
    init_db(db_path)
    run_migrations(db_path)

    pool = SQLiteConnectionPool(db_path, max_size=10)

    from app.api.deps import get_db
    from app.main import app as main_app
    from app.security import csrf_protect

    class TestCSRFManager:
        def generate_token(self):
            return "test-csrf-token"

        def validate_token(self, token):
            return token == "test-csrf-token"

    def get_test_db():
        conn = pool.get_connection()
        try:
            yield conn
        finally:
            pool.release_connection(conn)

    main_app.dependency_overrides[get_db] = get_test_db
    main_app.dependency_overrides[csrf_protect] = lambda: "test-csrf-token"
    main_app.state.csrf_manager = TestCSRFManager()

    client = TestClient(main_app)
    return client, main_app, pool, db_path, temp_dir


def _tear_down(app, pool, temp_dir, *, original_users_enabled, original_jwt_secret):
    """Restore settings, clear dependency overrides, close pool, remove temp_dir."""
    import shutil

    settings.users_enabled = original_users_enabled
    settings.jwt_secret_key = original_jwt_secret
    app.dependency_overrides.clear()
    pool.close_all()
    try:
        shutil.rmtree(temp_dir)
    except Exception:
        pass


class TestAuthAtomicity(unittest.TestCase):
    """Regression tests for #393 auth.py transaction atomicity."""

    def setUp(self):
        self._original_jwt_secret = settings.jwt_secret_key
        self._original_users_enabled = settings.users_enabled
        settings.jwt_secret_key = "test-secret-key-for-testing-at-least-32-chars-long"
        settings.users_enabled = True

        client, app, pool, db_path, temp_dir = _make_test_client_and_pool()
        self.client = client
        self.app = app
        self.pool = pool
        self.db_path = db_path
        self.temp_dir = temp_dir

    def tearDown(self):
        _tear_down(
            self.app,
            self.pool,
            self.temp_dir,
            original_users_enabled=self._original_users_enabled,
            original_jwt_secret=self._original_jwt_secret,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_conn(self):
        return self.pool.get_connection()

    def _release_conn(self, conn):
        self.pool.release_connection(conn)

    def _register_user(self, username="admin", password="Password123"):
        """Register a user and return the response."""
        return self.client.post(
            "/api/auth/register",
            json={"username": username, "password": password},
        )

    # ------------------------------------------------------------------
    # F-2.1 — username uniqueness inside BEGIN IMMEDIATE
    # ------------------------------------------------------------------

    def test_register_duplicate_returns_409_not_500(self):
        """F-2.1: a duplicate-username register must return 409, not 500.

        Pre-fix, the uniqueness pre-check ran OUTSIDE BEGIN IMMEDIATE; under
        the race the loser's INSERT hit the UNIQUE constraint and the bare
        ``except Exception`` converted the IntegrityError to 500. Post-fix,
        the in-transaction SELECT (and the IntegrityError→409 translation
        wrapped around the user INSERT) yields a clean 409.

        This is the sequential, non-raced assertion. The concurrent variant
        is ``test_concurrent_register_same_username``.
        """
        first = self._register_user(username="dup")
        self.assertEqual(first.status_code, 200)

        second = self._register_user(username="dup")
        self.assertEqual(
            second.status_code,
            409,
            f"Duplicate register must return 409, got {second.status_code}: {second.text}",
        )
        self.assertIn("already exists", second.json()["detail"].lower())

        # Exactly one user row with that username.
        conn = self._get_conn()
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM users WHERE username = ? COLLATE NOCASE",
                ("dup",),
            ).fetchone()[0]
        finally:
            self._release_conn(conn)
        self.assertEqual(count, 1, "Duplicate register must not create a second row")

    def test_concurrent_register_same_username(self):
        """F-2.1 (concurrent): two near-simultaneous registers of the same
        username must produce exactly one success and exactly one 409, with
        exactly one user row in the DB.

        Mirrors the ThreadPoolExecutor pattern in test_org_invites.py:782.

        Pre-fix, the loser of the race could surface an IntegrityError→500.
        Post-fix, the BEGIN IMMEDIATE serializes the writers and the loser
        observes the committed row in its in-transaction SELECT → 409.
        """
        # Reset rate limiter between sub-tasks by raising the limit via
        # direct limiter reset; /auth/register is capped at 5/hour which is
        # plenty for 2 requests.
        results = {}
        lock = __import__("threading").Lock()

        def _register(idx):
            resp = self.client.post(
                "/api/auth/register",
                json={"username": "racer", "password": "Password123"},
            )
            with lock:
                results[idx] = resp.status_code

        with ThreadPoolExecutor(max_workers=2) as executor:
            f1 = executor.submit(_register, 0)
            f2 = executor.submit(_register, 1)
            f1.result()
            f2.result()

        statuses = list(results.values())
        successes = [s for s in statuses if s == 200]
        conflicts = [s for s in statuses if s == 409]
        self.assertEqual(
            len(successes), 1, f"Expected exactly one 200, got statuses={statuses}"
        )
        self.assertEqual(
            len(conflicts), 1, f"Expected exactly one 409, got statuses={statuses}"
        )
        # No 500s allowed — the race must not surface as an internal error.
        self.assertNotIn(
            500, statuses, f"Race must not produce 500, got statuses={statuses}"
        )

        conn = self._get_conn()
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM users WHERE username = ? COLLATE NOCASE",
                ("racer",),
            ).fetchone()[0]
        finally:
            self._release_conn(conn)
        self.assertEqual(
            count, 1, f"Exactly one user row must exist, got {count}"
        )

    # ------------------------------------------------------------------
    # F-2.1 / F-2.4 — transaction-boundary (mock DB, real function)
    # ------------------------------------------------------------------

    def test_record_failed_attempt_rereads_under_lock(self):
        """F-2.4: _record_failed_attempt_db must issue BEGIN IMMEDIATE,
        UPDATE the counter, then re-read failed_attempts from the DB under
        the lock before deciding whether to set locked_until.

        Imports the REAL production function. Instruments a mock connection
        to record the SQL sequence and to return 5 for the post-UPDATE
        SELECT, then asserts the lockout UPDATE fires.

        TRUE REGRESSION: pre-fix code has signature (db, user_id,
        failed_attempts) and never issues the post-UPDATE SELECT — this
        test's SQL-sequence assertion fails on pre-fix code.
        """
        from app.api.routes.auth import _record_failed_attempt_db

        sql_sequence = []

        class _MockCursor:
            def __init__(self, value):
                self._value = value

            def fetchone(self):
                return (self._value,)

        def fake_execute(sql, *args, **kwargs):
            sql_sequence.append(sql)
            if "SELECT failed_attempts" in sql:
                return _MockCursor(5)
            return _MockCursor(None)

        class _MockDB:
            in_transaction = False

            def execute(self, sql, *args, **kwargs):
                return fake_execute(sql, *args, **kwargs)

            def commit(self):
                sql_sequence.append("COMMIT")

            def rollback(self):
                sql_sequence.append("ROLLBACK")

        db = _MockDB()
        returned = _record_failed_attempt_db(db, user_id=42)

        # F-2.4: the function returns the fresh post-increment count so the
        # caller can issue an accurate trip response (401 vs 423) under
        # concurrency.
        self.assertEqual(returned, 5, "Must return the fresh post-increment count")

        # BEGIN IMMEDIATE must be issued.
        self.assertIn("BEGIN IMMEDIATE", sql_sequence)
        begin_idx = sql_sequence.index("BEGIN IMMEDIATE")
        # UPDATE counter must follow.
        update_sqls = [
            i for i, s in enumerate(sql_sequence)
            if "failed_attempts = failed_attempts + 1" in s
        ]
        self.assertEqual(len(update_sqls), 1, f"Expected one counter UPDATE, sequence={sql_sequence}")
        self.assertGreater(update_sqls[0], begin_idx, "UPDATE must follow BEGIN IMMEDIATE")
        # F-2.4 core assertion: a re-read SELECT must follow the UPDATE.
        select_sqls = [
            i for i, s in enumerate(sql_sequence)
            if "SELECT failed_attempts FROM users" in s
        ]
        self.assertEqual(
            len(select_sqls),
            1,
            f"F-2.4 requires a post-UPDATE re-read; sequence={sql_sequence}",
        )
        self.assertGreater(select_sqls[0], update_sqls[0], "Re-read SELECT must follow the UPDATE")
        # Because fresh == 5 >= 5, the lockout UPDATE must fire.
        lockout_sqls = [s for s in sql_sequence if "locked_until" in s and "UPDATE" in s]
        self.assertEqual(
            len(lockout_sqls),
            1,
            f"Expected one locked_until UPDATE (fresh=5>=5); sequence={sql_sequence}",
        )
        # Commit must come last (no rollback).
        self.assertIn("COMMIT", sql_sequence)
        self.assertNotIn("ROLLBACK", sql_sequence)

    def test_record_failed_attempt_no_lockout_below_threshold(self):
        """F-2.4 companion: when the re-read returns 4 (< 5), no locked_until
        UPDATE must fire. Proves the decision uses the fresh value, not the
        stale parameter (the parameter no longer exists).
        """
        from app.api.routes.auth import _record_failed_attempt_db

        sql_sequence = []

        class _MockCursor:
            def __init__(self, value):
                self._value = value

            def fetchone(self):
                return (self._value,)

        def fake_execute(sql, *args, **kwargs):
            sql_sequence.append(sql)
            if "SELECT failed_attempts" in sql:
                return _MockCursor(4)
            return _MockCursor(None)

        class _MockDB:
            in_transaction = False

            def execute(self, sql, *args, **kwargs):
                return fake_execute(sql, *args, **kwargs)

            def commit(self):
                sql_sequence.append("COMMIT")

            def rollback(self):
                sql_sequence.append("ROLLBACK")

        db = _MockDB()
        returned = _record_failed_attempt_db(db, user_id=42)

        # F-2.4: returns the fresh post-increment count (4) even when no lock.
        self.assertEqual(returned, 4, "Must return the fresh post-increment count")

        lockout_sqls = [s for s in sql_sequence if "locked_until" in s and "UPDATE" in s]
        self.assertEqual(
            len(lockout_sqls),
            0,
            f"fresh=4 < 5 must NOT set locked_until; sequence={sql_sequence}",
        )
        self.assertIn("COMMIT", sql_sequence)

    # ------------------------------------------------------------------
    # F-2.3 — register user+session atomic on session-INSERT failure
    # ------------------------------------------------------------------

    def test_register_user_and_session_atomic_on_session_failure(self):
        """F-2.3: if the session INSERT fails inside _register_db, the user
        row must NOT be committed. Proves the user INSERT and session INSERT
        share one transaction.

        TRUE REGRESSION: pre-fix code commits the user row in _register_db
        and the session INSERT in a separate _register_session_db; under
        this monkeypatch the pre-fix code would leave a dangling user row.
        Post-fix, both roll back together.
        """
        from app.api.deps import get_db
        from app.main import app as main_app

        # A fresh pool that yields proxy connections. The proxy intercepts
        # ``execute`` so we can inject a failure on INSERT INTO user_sessions.
        # We cannot monkeypatch sqlite3.Connection.execute directly (read-only),
        # so the proxy delegates everything else by attribute lookup.
        real_pool = SQLiteConnectionPool(self.db_path, max_size=5)

        class _FailingSessionConnProxy:
            """Delegates to the underlying sqlite3 connection but raises on
            INSERT INTO user_sessions to simulate a session-INSERT failure."""

            def __init__(self, real):
                object.__setattr__(self, "_real", real)

            @property
            def in_transaction(self):
                return self._real.in_transaction

            def execute(self, sql, *args, **kwargs):
                if (
                    isinstance(sql, str)
                    and "INSERT INTO user_sessions" in sql
                ):
                    raise Exception("simulated session-INSERT failure")
                return self._real.execute(sql, *args, **kwargs)

            def commit(self):
                return self._real.commit()

            def rollback(self):
                return self._real.rollback()

            def close(self):
                return self._real.close()

            def __getattr__(self, name):
                return getattr(self._real, name)

        original_get_connection = real_pool.get_connection

        def get_connection_instrumented():
            conn = original_get_connection()
            return _FailingSessionConnProxy(conn)

        def get_test_db():
            conn = real_pool.get_connection()
            try:
                yield conn
            finally:
                real_pool.release_connection(conn._real)

        real_pool.get_connection = get_connection_instrumented
        main_app.dependency_overrides[get_db] = get_test_db

        try:
            response = self.client.post(
                "/api/auth/register",
                json={"username": "atomic_test", "password": "Password123"},
            )

            # The simulated session failure must surface as 500.
            self.assertEqual(
                response.status_code,
                500,
                f"Session-INSERT failure must surface as 500, got {response.status_code}",
            )

            # CRITICAL: no user row must have been committed.
            check_pool = SQLiteConnectionPool(self.db_path, max_size=2)
            check_conn = check_pool.get_connection()
            try:
                count = check_conn.execute(
                    "SELECT COUNT(*) FROM users WHERE username = ? COLLATE NOCASE",
                    ("atomic_test",),
                ).fetchone()[0]
            finally:
                check_conn.close()
                check_pool.close_all()

            self.assertEqual(
                count,
                0,
                "F-2.3: user row must NOT be committed when session INSERT fails "
                "(pre-fix split-commit would leave a dangling row)",
            )
        finally:
            try:
                real_pool.close_all()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # F-2.2 — change-password password+session atomic on session failure
    # ------------------------------------------------------------------

    def test_change_password_password_and_session_atomic_on_session_failure(self):
        """F-2.2: if the new-session INSERT fails inside _change_password_db,
        the password UPDATE and the session DELETE must roll back. Proves
        they share one transaction.

        TRUE REGRESSION: pre-fix code commits password+DELETE in
        _change_password_db and the new-session INSERT in a separate
        _create_session_db; under this monkeypatch the pre-fix code would
        leave the password changed and the old session deleted. Post-fix,
        all three roll back together.
        """
        # Register a user and capture the original password hash + a
        # pre-existing session row. Use the plain self.client pool.
        reg = self._register_user(username="cpuser", password="Password123")
        self.assertEqual(reg.status_code, 200)

        conn = self._get_conn()
        try:
            user_row = conn.execute(
                "SELECT id, hashed_password FROM users WHERE username = ? COLLATE NOCASE",
                ("cpuser",),
            ).fetchone()
            user_id = user_row[0]
            original_hash = user_row[1]
            # Insert a pre-existing session to verify the DELETE rolls back.
            conn.execute(
                "INSERT INTO user_sessions (user_id, refresh_token_hash, expires_at) "
                "VALUES (?, ?, ?)",
                (user_id, "preexisting_hash_" + "0" * 32, "2099-01-01T00:00:00+00:00"),
            )
            conn.commit()
        finally:
            self._release_conn(conn)

        # Login via the plain client pool to obtain a bearer token for the
        # change-password request. The login flow creates its own session row
        # which is fine — we just need the access token.
        login_resp = self.client.post(
            "/api/auth/login",
            json={"username": "cpuser", "password": "Password123"},
        )
        self.assertEqual(login_resp.status_code, 200, login_resp.text)
        access_token = login_resp.json()["access_token"]

        # Now swap in an instrumented pool that fails the change-password
        # session INSERT.
        from app.api.deps import get_db
        from app.main import app as main_app

        real_pool = SQLiteConnectionPool(self.db_path, max_size=5)

        class _FailingSessionConnProxy:
            def __init__(self, real):
                object.__setattr__(self, "_real", real)

            @property
            def in_transaction(self):
                return self._real.in_transaction

            def execute(self, sql, *args, **kwargs):
                if (
                    isinstance(sql, str)
                    and "INSERT INTO user_sessions" in sql
                ):
                    raise Exception("simulated session-INSERT failure")
                return self._real.execute(sql, *args, **kwargs)

            def commit(self):
                return self._real.commit()

            def rollback(self):
                return self._real.rollback()

            def close(self):
                return self._real.close()

            def __getattr__(self, name):
                return getattr(self._real, name)

        original_get_connection = real_pool.get_connection

        def get_connection_instrumented():
            conn = original_get_connection()
            return _FailingSessionConnProxy(conn)

        def get_test_db():
            conn = real_pool.get_connection()
            try:
                yield conn
            finally:
                real_pool.release_connection(conn._real)

        real_pool.get_connection = get_connection_instrumented
        main_app.dependency_overrides[get_db] = get_test_db

        try:
            response = self.client.post(
                "/api/auth/change-password",
                json={
                    "current_password": "Password123",
                    "new_password": "NewPassword456",
                },
                headers={"Authorization": f"Bearer {access_token}"},
            )

            self.assertEqual(
                response.status_code,
                500,
                f"Session-INSERT failure must surface as 500, got {response.status_code}",
            )
        finally:
            try:
                real_pool.close_all()
            except Exception:
                pass

        # CRITICAL assertions:
        # 1. Password hash must be UNCHANGED (UPDATE rolled back).
        # 2. The pre-existing session row must still be present (DELETE rolled back).
        check_pool = SQLiteConnectionPool(self.db_path, max_size=2)
        check_conn = check_pool.get_connection()
        try:
            current_hash = check_conn.execute(
                "SELECT hashed_password FROM users WHERE username = ? COLLATE NOCASE",
                ("cpuser",),
            ).fetchone()[0]
            session_count = check_conn.execute(
                "SELECT COUNT(*) FROM user_sessions WHERE user_id = ?",
                (user_id,),
            ).fetchone()[0]
        finally:
            check_conn.close()
            check_pool.close_all()

        self.assertEqual(
            current_hash,
            original_hash,
            "F-2.2: password must NOT change when session INSERT fails "
            "(pre-fix split-commit would leave the password changed)",
        )
        self.assertGreaterEqual(
            session_count,
            1,
            "F-2.2: pre-existing session must NOT be deleted when session INSERT fails "
            "(pre-fix split-commit would leave the user with no sessions)",
        )

    # ------------------------------------------------------------------
    # F-2.4 behavioral (sequential lockout)
    # ------------------------------------------------------------------

    def test_five_sequential_failed_logins_lock_account(self):
        """F-2.4 behavioral: five sequential wrong-password logins must lock
        the account on the 5th attempt.

        NOTE: this is COVERAGE, not a true regression — pre-fix code locks
        sequentially too (the stale-snapshot defect only manifests under
        concurrency). The true regression is
        ``test_record_failed_attempt_rereads_under_lock``. Included here
        because the sequential lockout path was previously uncovered.
        """
        reg = self._register_user(username="lockme", password="Password123")
        self.assertEqual(reg.status_code, 200)

        statuses = []
        for _ in range(5):
            resp = self.client.post(
                "/api/auth/login",
                json={"username": "lockme", "password": "WrongPassword999"},
            )
            statuses.append(resp.status_code)

        # The first four must be 401 (bad password, not yet locked).
        self.assertEqual(
            statuses[:4],
            [401, 401, 401, 401],
            f"First four wrong logins must be 401, got {statuses}",
        )
        # The 5th must trip the lockout: 423 with Retry-After: 900.
        self.assertEqual(
            statuses[4],
            423,
            f"5th wrong login must return 423 (locked), got {statuses}",
        )

        # DB state: failed_attempts == 5, locked_until is set and in the future.
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT failed_attempts, locked_until FROM users WHERE username = ? COLLATE NOCASE",
                ("lockme",),
            ).fetchone()
        finally:
            self._release_conn(conn)
        self.assertEqual(row[0], 5, f"failed_attempts must be 5, got {row[0]}")
        self.assertIsNotNone(row[1], "locked_until must be set after 5 failures")
        locked_until = __import__("datetime").datetime.fromisoformat(row[1])
        self.assertGreater(
            locked_until,
            __import__("datetime").datetime.now(__import__("datetime").timezone.utc),
            "locked_until must be in the future",
        )

        # A subsequent CORRECT password must still be rejected (account locked).
        correct_resp = self.client.post(
            "/api/auth/login",
            json={"username": "lockme", "password": "Password123"},
        )
        self.assertEqual(
            correct_resp.status_code,
            423,
            f"Correct password during lockout must return 423, got {correct_resp.status_code}",
        )

    # ------------------------------------------------------------------
    # PRR-005 — change-password session INSERT audit-trail parity
    # ------------------------------------------------------------------

    def test_change_password_session_records_ip_and_user_agent(self):
        """PRR-005: the new session created by change-password must include
        ip_address and user_agent for audit-trail parity with register and
        login. Pre-fix, the merged INSERT omitted both columns.
        """
        reg = self._register_user(username="audituser", password="Password123")
        self.assertEqual(reg.status_code, 200)

        login_resp = self.client.post(
            "/api/auth/login",
            json={"username": "audituser", "password": "Password123"},
            headers={"User-Agent": "audit-trail-test/1.0"},
        )
        self.assertEqual(login_resp.status_code, 200, login_resp.text)
        access_token = login_resp.json()["access_token"]

        resp = self.client.post(
            "/api/auth/change-password",
            json={
                "current_password": "Password123",
                "new_password": "NewPassword456",
            },
            headers={
                "Authorization": f"Bearer {access_token}",
                "User-Agent": "audit-trail-test/1.0",
            },
        )
        self.assertEqual(resp.status_code, 200, resp.text)

        # The new session row must carry the request's user_agent (and a
        # non-NULL ip_address — TestClient synthesizes a testclient IP).
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT ip_address, user_agent FROM user_sessions "
                "WHERE user_id = (SELECT id FROM users WHERE username = ? COLLATE NOCASE) "
                "ORDER BY id DESC LIMIT 1",
                ("audituser",),
            ).fetchone()
        finally:
            self._release_conn(conn)

        self.assertIsNotNone(row, "A session row must exist after change-password")
        self.assertEqual(
            row[1],
            "audit-trail-test/1.0",
            f"user_agent must be recorded for audit trail; got {row[1]!r}",
        )
        self.assertIsNotNone(
            row[0],
            "ip_address must be recorded (non-NULL) for audit trail",
        )

    # ------------------------------------------------------------------
    # PRR-002 — _record_failed_attempt_db None-row guard
    # ------------------------------------------------------------------

    def test_record_failed_attempt_handles_deleted_user(self):
        """PRR-002: if the user row is missing on the post-UPDATE re-read
        (concurrent superadmin delete), _record_failed_attempt_db must NOT
        raise TypeError on ``None[0]`` — it must roll back and return 0 so
        the caller issues a clean 401 instead of a 500.

        Imports the REAL production function. Instruments a mock connection
        whose post-UPDATE SELECT returns None (simulating a vanished row).
        """
        from app.api.routes.auth import _record_failed_attempt_db

        sql_sequence = []

        class _NoneCursor:
            def fetchone(self):
                return None

        class _RowCursor:
            def fetchone(self):
                # The defensive guard should return 0 before ever indexing.
                return (99,)  # never reached

        def fake_execute(sql, *args, **kwargs):
            sql_sequence.append(sql)
            if "SELECT failed_attempts" in sql:
                return _NoneCursor()
            return _RowCursor()

        class _MockDB:
            in_transaction = False

            def execute(self, sql, *args, **kwargs):
                return fake_execute(sql, *args, **kwargs)

            def commit(self):
                sql_sequence.append("COMMIT")

            def rollback(self):
                sql_sequence.append("ROLLBACK")

        db = _MockDB()
        # Must NOT raise — the guard returns 0 cleanly.
        returned = _record_failed_attempt_db(db, user_id=999)

        self.assertEqual(
            returned,
            0,
            f"Vanished-user row must return 0 (no lockout decision); got {returned}",
        )
        # The no-op UPDATE must be rolled back (transaction closed cleanly).
        self.assertIn("ROLLBACK", sql_sequence)
        self.assertNotIn(
            "COMMIT",
            sql_sequence,
            f"Vanished-user path must not commit; sequence={sql_sequence}",
        )


if __name__ == "__main__":
    unittest.main()
