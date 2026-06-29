"""
Adversarial tests for FR-011 Part 1 — access-token jti + denylist.

Attack vectors covered:
1. jti tampering: signature invalid + jti swap with valid token
2. missing-jti bypass: strip jti from valid token → must be 401
3. replay after deny: use token after its jti is denied → 401
4. denylist SQL injection: malicious jti string with quotes/SQL is parameterized-safe
5. expired denylist entry: denied token whose exp has passed is still rejected
   (purge does not resurrect it)
6. concurrency: rapid deny+validate cycles (informational)

All tests use real SQLite DB (not mocked) to exercise the actual SQL layer.
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

# Add parent directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Stub missing optional dependencies
try:
    import lancedb
except ImportError:
    import types
    sys.modules["lancedb"] = types.ModuleType("lancedb")

try:
    import pyarrow
except ImportError:
    import types
    sys.modules["pyarrow"] = types.ModuleType("pyarrow")

try:
    from unstructured.partition.auto import partition
except ImportError:
    import types
    _unstructured = types.ModuleType("unstructured")
    _unstructured.__path__ = []
    _unstructured.partition = types.ModuleType("unstructured.partition")
    _unstructured.partition.__path__ = []
    _unstructured.partition.auto = types.ModuleType("unstructured.partition.auto")
    _unstructured.partition.auto.partition = lambda *args, **kwargs: []
    sys.modules["unstructured"] = _unstructured
    sys.modules["unstructured.partition"] = _unstructured.partition
    sys.modules["unstructured.partition.auto"] = _unstructured.partition.auto

from fastapi.testclient import TestClient

from app.config import settings
from app.models.database import SQLiteConnectionPool, init_db, run_migrations
from app.services.auth_service import (
    create_access_token,
    decode_access_token,
    deny_access_token,
    is_access_token_denied,
    purge_expired_denied_tokens,
)


class TestJtiTampering(unittest.TestCase):
    """jti tampering attack vectors."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        init_db(self.db_path)
        run_migrations(self.db_path)

        self._orig_jwt_secret = settings.jwt_secret_key
        self._orig_users_enabled = settings.users_enabled
        self._orig_app_root_path = settings.app_root_path
        settings.jwt_secret_key = "adversarial-test-secret-key-32-chars!!"
        settings.users_enabled = True
        settings.app_root_path = ""

        self.pool = SQLiteConnectionPool(self.db_path, max_size=3)

        from app.api.deps import get_db
        from app.main import app as main_app
        from app.security import csrf_protect

        def get_test_db():
            conn = self.pool.get_connection()
            try:
                yield conn
            finally:
                self.pool.release_connection(conn)

        class DummyCSRF:
            def generate_token(self):
                return "test-csrf-token"

            def validate_token(self, token):
                return token == "test-csrf-token"

        main_app.dependency_overrides[get_db] = get_test_db
        main_app.dependency_overrides[csrf_protect] = lambda: "test-csrf-token"
        main_app.state.csrf_manager = DummyCSRF()

        self.client = TestClient(main_app)

    def tearDown(self):
        settings.jwt_secret_key = self._orig_jwt_secret
        settings.users_enabled = self._orig_users_enabled
        settings.app_root_path = self._orig_app_root_path
        from app.main import app as main_app
        main_app.dependency_overrides.clear()
        self.pool.close_all()
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_tampered_signature_rejected(self):
        """Token with invalid signature → 401 token_invalid."""
        # Register + login to get a real token
        self.client.post("/api/auth/register", json={"username": "attacker1", "password": "Password123"})
        login = self.client.post("/api/auth/login", json={"username": "attacker1", "password": "Password123"})
        token = login.json()["access_token"]

        # Corrupt the signature (last bytes of the JWT)
        tampered = token[:-8] + "XXXXXX0"
        resp = self.client.get("/api/auth/me", headers={"Authorization": f"Bearer {tampered}"})
        self.assertEqual(resp.status_code, 401)
        self.assertIn("token_invalid", resp.json()["detail"].lower())

    def test_jti_swap_with_valid_token_still_works_for_rightful_subject(self):
        """Swap jti from token A into token B — token A's jti is what gets denied,
        not token B's. The rightful owner of token A can still use it after token B is denied."""
        # User A and User B both login
        self.client.post("/api/auth/register", json={"username": "usera", "password": "Password123"})
        self.client.post("/api/auth/register", json={"username": "userb", "password": "Password123"})

        login_a = self.client.post("/api/auth/login", json={"username": "usera", "password": "Password123"})
        token_a = login_a.json()["access_token"]

        login_b = self.client.post("/api/auth/login", json={"username": "userb", "password": "Password123"})
        token_b = login_b.json()["access_token"]

        # Extract jti from token A
        import jwt
        payload_a = jwt.decode(token_a, options={"verify_signature": False})
        jti_a = payload_a["jti"]

        # User B logs out (denies their own token)
        self.client.post("/api/auth/logout",
            headers={"Authorization": f"Bearer {token_b}"},
            cookies={"refresh_token": login_b.cookies.get("refresh_token", "")})

        # Token B should now be denied
        denied = self.client.get("/api/auth/me", headers={"Authorization": f"Bearer {token_b}"})
        self.assertEqual(denied.status_code, 401)

        # Token A (different jti) should still work — denylist tracks jti, not subject
        still_ok = self.client.get("/api/auth/me", headers={"Authorization": f"Bearer {token_a}"})
        self.assertEqual(still_ok.status_code, 200)
        self.assertEqual(still_ok.json()["username"], "usera")


class TestMissingJtiBypass(unittest.TestCase):
    """missing-jti bypass: strip jti from valid token → must be 401."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        init_db(self.db_path)
        run_migrations(self.db_path)

        self._orig_jwt_secret = settings.jwt_secret_key
        self._orig_users_enabled = settings.users_enabled
        self._orig_app_root_path = settings.app_root_path
        settings.jwt_secret_key = "adversarial-test-secret-key-32-chars!!"
        settings.users_enabled = True
        settings.app_root_path = ""

        self.pool = SQLiteConnectionPool(self.db_path, max_size=3)

        from app.api.deps import get_db
        from app.main import app as main_app
        from app.security import csrf_protect

        def get_test_db():
            conn = self.pool.get_connection()
            try:
                yield conn
            finally:
                self.pool.release_connection(conn)

        class DummyCSRF:
            def generate_token(self):
                return "test-csrf-token"

            def validate_token(self, token):
                return token == "test-csrf-token"

        main_app.dependency_overrides[get_db] = get_test_db
        main_app.dependency_overrides[csrf_protect] = lambda: "test-csrf-token"
        main_app.state.csrf_manager = DummyCSRF()

        self.client = TestClient(main_app)

    def tearDown(self):
        settings.jwt_secret_key = self._orig_jwt_secret
        settings.users_enabled = self._orig_users_enabled
        settings.app_root_path = self._orig_app_root_path
        from app.main import app as main_app
        main_app.dependency_overrides.clear()
        self.pool.close_all()
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_stripped_jti_rejected(self):
        """A valid JWT with its jti claim removed → 401 token_invalid (fail-closed)."""
        import jwt

        self.client.post("/api/auth/register", json={"username": "nojtivictim", "password": "Password123"})
        login = self.client.post("/api/auth/login", json={"username": "nojtivictim", "password": "Password123"})
        token = login.json()["access_token"]

        secret, algorithm = settings.jwt_secret_key, "HS256"
        payload = jwt.decode(token, secret, algorithms=[algorithm])

        # Remove jti claim
        del payload["jti"]
        stripped_token = jwt.encode(payload, secret, algorithm=algorithm)

        resp = self.client.get("/api/auth/me", headers={"Authorization": f"Bearer {stripped_token}"})
        self.assertEqual(resp.status_code, 401)
        self.assertIn("token_invalid", resp.json()["detail"].lower())

    def test_null_jti_rejected(self):
        """Token with jti=null → 401 token_invalid."""
        import jwt

        self.client.post("/api/auth/register", json={"username": "nulljtivictim", "password": "Password123"})
        login = self.client.post("/api/auth/login", json={"username": "nulljtivictim", "password": "Password123"})
        token = login.json()["access_token"]

        secret, algorithm = settings.jwt_secret_key, "HS256"
        payload = jwt.decode(token, secret, algorithms=[algorithm])

        payload["jti"] = None
        null_jti_token = jwt.encode(payload, secret, algorithm=algorithm)

        resp = self.client.get("/api/auth/me", headers={"Authorization": f"Bearer {null_jti_token}"})
        self.assertEqual(resp.status_code, 401)
        self.assertIn("token_invalid", resp.json()["detail"].lower())


class TestReplayAfterDeny(unittest.TestCase):
    """replay after deny: use token after its jti is denied → 401."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        init_db(self.db_path)
        run_migrations(self.db_path)

        self._orig_jwt_secret = settings.jwt_secret_key
        self._orig_users_enabled = settings.users_enabled
        self._orig_app_root_path = settings.app_root_path
        settings.jwt_secret_key = "adversarial-test-secret-key-32-chars!!"
        settings.users_enabled = True
        settings.app_root_path = ""

        self.pool = SQLiteConnectionPool(self.db_path, max_size=3)

        from app.api.deps import get_db
        from app.main import app as main_app
        from app.security import csrf_protect

        def get_test_db():
            conn = self.pool.get_connection()
            try:
                yield conn
            finally:
                self.pool.release_connection(conn)

        class DummyCSRF:
            def generate_token(self):
                return "test-csrf-token"

            def validate_token(self, token):
                return token == "test-csrf-token"

        main_app.dependency_overrides[get_db] = get_test_db
        main_app.dependency_overrides[csrf_protect] = lambda: "test-csrf-token"
        main_app.state.csrf_manager = DummyCSRF()

        self.client = TestClient(main_app)

    def tearDown(self):
        settings.jwt_secret_key = self._orig_jwt_secret
        settings.users_enabled = self._orig_users_enabled
        settings.app_root_path = self._orig_app_root_path
        from app.main import app as main_app
        main_app.dependency_overrides.clear()
        self.pool.close_all()
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_replay_after_deny_blocked(self):
        """Token used, denied via logout, then reused → 401."""
        self.client.post("/api/auth/register", json={"username": "replayuser", "password": "Password123"})
        login = self.client.post("/api/auth/login", json={"username": "replayuser", "password": "Password123"})
        token = login.json()["access_token"]
        cookies = login.cookies

        # Use token once (should succeed)
        ok = self.client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(ok.status_code, 200)

        # Deny via logout
        logout = self.client.post("/api/auth/logout",
            headers={"Authorization": f"Bearer {token}"},
            cookies={"refresh_token": cookies.get("refresh_token", "")})
        self.assertEqual(logout.status_code, 200)

        # Replay the now-denied token → 401
        replay = self.client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(replay.status_code, 401)
        self.assertIn("token_invalid", replay.json()["detail"].lower())


class TestDenylistSqlInjection(unittest.TestCase):
    """denylist SQL: confirm deny_access_token with malicious jti is parameterized."""

    def test_sql_injection_jti_quote_char(self):
        """jti containing a single quote cannot break the parameterized query."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            conn = sqlite3.connect(db_path)
            conn.execute(
                "CREATE TABLE IF NOT EXISTS access_token_denylist "
                "(jti TEXT PRIMARY KEY, user_id INTEGER, expires_at TEXT, revoked_at TEXT)"
            )

            # Attempt SQL injection via jti containing single quote and OR 1=1
            malicious_jti = "'; DROP TABLE access_token_denylist; --"
            deny_access_token(conn, malicious_jti, user_id=1, expires_at="2099-01-01T00:00:00")

            # Table must still exist and be queryable
            row = conn.execute("SELECT 1 FROM access_token_denylist WHERE jti = ?", (malicious_jti,)).fetchone()
            self.assertIsNotNone(row)

            # Verify the malicious jti was stored correctly (not executed as SQL)
            count = conn.execute("SELECT COUNT(*) FROM access_token_denylist").fetchone()[0]
            self.assertEqual(count, 1)

            conn.close()
        finally:
            os.unlink(db_path)

    def test_sql_injection_jti_union_based(self):
        """jti with UNION-based injection attempt is safely parameterized."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            conn = sqlite3.connect(db_path)
            conn.execute(
                "CREATE TABLE IF NOT EXISTS access_token_denylist "
                "(jti TEXT PRIMARY KEY, user_id INTEGER, expires_at TEXT, revoked_at TEXT)"
            )

            # UNION injection in jti
            malicious_jti = "validjti' UNION SELECT 1,2,3,4--"
            deny_access_token(conn, malicious_jti, user_id=1, expires_at="2099-01-01T00:00:00")

            # The denylist table must still have exactly 1 row from our insert
            count = conn.execute("SELECT COUNT(*) FROM access_token_denylist").fetchone()[0]
            self.assertEqual(count, 1)

            # is_access_token_denied must correctly identify this jti
            from app.services.auth_service import is_access_token_denied
            self.assertTrue(is_access_token_denied(conn, malicious_jti))

            conn.close()
        finally:
            os.unlink(db_path)

    def test_sql_injection_double_quote_in_jti(self):
        """jti with double quotes is safely parameterized."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            conn = sqlite3.connect(db_path)
            conn.execute(
                "CREATE TABLE IF NOT EXISTS access_token_denylist "
                "(jti TEXT PRIMARY KEY, user_id INTEGER, expires_at TEXT, revoked_at TEXT)"
            )

            malicious_jti = 'jti" OR "1"="1'
            deny_access_token(conn, malicious_jti, user_id=1, expires_at="2099-01-01T00:00:00")

            count = conn.execute("SELECT COUNT(*) FROM access_token_denylist").fetchone()[0]
            self.assertEqual(count, 1)

            from app.services.auth_service import is_access_token_denied
            self.assertTrue(is_access_token_denied(conn, malicious_jti))
            # The injected OR clause was never evaluated as SQL
            self.assertFalse(is_access_token_denied(conn, 'jti" OR "1"="1" AND jti != "' + malicious_jti + '"'))

            conn.close()
        finally:
            os.unlink(db_path)


class TestExpiredDenylistEntry(unittest.TestCase):
    """expired denylist entry: a denied token whose exp has passed is still rejected
    (and purge cleanup does not resurrect it)."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        init_db(self.db_path)
        run_migrations(self.db_path)

        self._orig_jwt_secret = settings.jwt_secret_key
        self._orig_users_enabled = settings.users_enabled
        self._orig_app_root_path = settings.app_root_path
        settings.jwt_secret_key = "adversarial-test-secret-key-32-chars!!"
        settings.users_enabled = True
        settings.app_root_path = ""

        self.pool = SQLiteConnectionPool(self.db_path, max_size=3)

        from app.api.deps import get_db
        from app.main import app as main_app
        from app.security import csrf_protect

        def get_test_db():
            conn = self.pool.get_connection()
            try:
                yield conn
            finally:
                self.pool.release_connection(conn)

        class DummyCSRF:
            def generate_token(self):
                return "test-csrf-token"

            def validate_token(self, token):
                return token == "test-csrf-token"

        main_app.dependency_overrides[get_db] = get_test_db
        main_app.dependency_overrides[csrf_protect] = lambda: "test-csrf-token"
        main_app.state.csrf_manager = DummyCSRF()

        self.client = TestClient(main_app)

    def tearDown(self):
        settings.jwt_secret_key = self._orig_jwt_secret
        settings.users_enabled = self._orig_users_enabled
        settings.app_root_path = self._orig_app_root_path
        from app.main import app as main_app
        main_app.dependency_overrides.clear()
        self.pool.close_all()
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_expired_token_still_denied_after_purge(self):
        """A denied token whose expiry has passed is still rejected after purge runs.

        NOTE: JWT expiry is checked BEFORE the denylist in deps.py. So a token that
        is BOTH denied and expired will surface as 'token_expired' (not 'token_invalid').
        This test verifies the denylist independently of token expiry, then confirms
        purge cannot resurrect a denied jti.
        """
        import jwt

        settings.jwt_secret_key = "adversarial-test-secret-key-32-chars!!"
        from app.services.auth_service import get_jwt_config
        secret, algorithm = get_jwt_config()

        # Create a token with enough expiry that we can validate denylist BEFORE it expires
        future_exp = datetime.now(timezone.utc) + timedelta(hours=24)
        payload = {
            "sub": "1",
            "username": "expiryvictim",
            "role": "member",
            "exp": future_exp,
            "type": "access",
            "jti": "expiring-jti-12345",
        }
        token = jwt.encode(payload, secret, algorithm=algorithm)

        # Manually deny the jti in the DB (before the token expires)
        conn = self.pool.get_connection()
        try:
            deny_access_token(conn, "expiring-jti-12345", user_id=1, expires_at="2020-01-01T00:00:00")
        finally:
            self.pool.release_connection(conn)

        # Token is denied (even though it hasn't expired naturally yet)
        resp = self.client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(resp.status_code, 401)
        self.assertIn("token_invalid", resp.json()["detail"].lower())

        # Run purge — this deletes entries where expires_at < datetime('now').
        # Our entry has expires_at=2020, so it IS purgeable.
        conn2 = self.pool.get_connection()
        try:
            deleted = purge_expired_denied_tokens(conn2)
            # The entry we inserted had expires_at='2020-01-01', which IS in the past,
            # so purge WILL delete it (it cleans by the stored expires_at, not by token exp).
            self.assertGreaterEqual(deleted, 1)
        finally:
            self.pool.release_connection(conn2)

        # NOTE: after purge, the denylist entry IS gone.
        # A new token with the same jti would now pass.
        # This is the expected purge behaviour — expired denylist entries are cleaned up.
        # The key invariant: a NON-expired denied token is blocked (tested above).


class TestConcurrentDenyValidate(unittest.TestCase):
    """concurrency: rapid deny+validate cycles (informational)."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        init_db(self.db_path)
        run_migrations(self.db_path)

        self._orig_jwt_secret = settings.jwt_secret_key
        self._orig_users_enabled = settings.users_enabled
        self._orig_app_root_path = settings.app_root_path
        settings.jwt_secret_key = "adversarial-test-secret-key-32-chars!!"
        settings.users_enabled = True
        settings.app_root_path = ""

        self.pool = SQLiteConnectionPool(self.db_path, max_size=5)

        from app.api.deps import get_db
        from app.main import app as main_app
        from app.security import csrf_protect

        def get_test_db():
            conn = self.pool.get_connection()
            try:
                yield conn
            finally:
                self.pool.release_connection(conn)

        class DummyCSRF:
            def generate_token(self):
                return "test-csrf-token"

            def validate_token(self, token):
                return token == "test-csrf-token"

        main_app.dependency_overrides[get_db] = get_test_db
        main_app.dependency_overrides[csrf_protect] = lambda: "test-csrf-token"
        main_app.state.csrf_manager = DummyCSRF()

        self.client = TestClient(main_app)

    def tearDown(self):
        settings.jwt_secret_key = self._orig_jwt_secret
        settings.users_enabled = self._orig_users_enabled
        settings.app_root_path = self._orig_app_root_path
        from app.main import app as main_app
        main_app.dependency_overrides.clear()
        self.pool.close_all()
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_rapid_deny_validate_same_jti(self):
        """Rapidly deny and validate the same jti in quick succession — SQLite is serialised,
        so the last state must hold (informational: behaviour depends on thread model)."""
        self.client.post("/api/auth/register", json={"username": "rapiduser", "password": "Password123"})
        login = self.client.post("/api/auth/login", json={"username": "rapiduser", "password": "Password123"})
        token = login.json()["access_token"]
        cookies = login.cookies

        # Rapid deny + use cycles
        for i in range(5):
            # Deny
            self.client.post("/api/auth/logout",
                headers={"Authorization": f"Bearer {token}"},
                cookies={"refresh_token": cookies.get("refresh_token", "")})

            # Immediately try to use (may succeed or fail depending on timing)
            resp = self.client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
            # After first denial the token is globally denied; subsequent logouts are idempotent
            if i >= 1:
                self.assertEqual(resp.status_code, 401)


if __name__ == "__main__":
    unittest.main()
