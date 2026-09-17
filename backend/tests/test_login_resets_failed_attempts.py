"""Successful login resets failed_attempts and locked_until (issue #202, AC3).

Pins the login success path (auth.py: UPDATE users SET failed_attempts = 0,
locked_until = NULL) so a refactor cannot silently drop the reset and accumulate
failures across successful logins (eventual spurious 423 lockouts).
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Module must not be misclassified as CSRF-managing by an incidental substring
# (the register/login flow below never exercises CSRF enforcement).
CSRF_TEST_POLICY = "naive"

from app.api.deps import get_db  # noqa: E402
from app.config import settings  # noqa: E402
from app.models.database import (  # noqa: E402
    SQLiteConnectionPool,
    init_db,
    run_migrations,
)
from app.security import csrf_protect  # noqa: E402


class TestLoginResetsFailedAttempts(unittest.TestCase):
    """Fail N times, log in successfully, assert the counters are cleared."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        init_db(self.db_path)
        run_migrations(self.db_path)

        self._original_jwt_secret = settings.jwt_secret_key
        self._original_users_enabled = settings.users_enabled
        self._original_app_root_path = settings.app_root_path
        settings.jwt_secret_key = "test-secret-key-for-testing-at-least-32-chars-long"
        settings.users_enabled = True
        settings.app_root_path = ""

        self.test_pool = SQLiteConnectionPool(self.db_path, max_size=5)

        from fastapi.testclient import TestClient

        from app.main import app as main_app

        class TestCSRFManager:
            def generate_token(self):
                return "test-csrf-token"

            def validate_token(self, token):
                return token == "test-csrf-token"

        def get_test_db():
            conn = self.test_pool.get_connection()
            try:
                yield conn
            finally:
                self.test_pool.release_connection(conn)

        main_app.dependency_overrides[get_db] = get_test_db
        main_app.dependency_overrides[csrf_protect] = lambda: "test-csrf-token"
        main_app.state.csrf_manager = TestCSRFManager()
        self.app = main_app
        self.client = TestClient(main_app)

    def tearDown(self):
        settings.jwt_secret_key = self._original_jwt_secret
        settings.users_enabled = self._original_users_enabled
        settings.app_root_path = self._original_app_root_path
        # Per-key pops, not clear(): this suite shares the main_app singleton
        # with other suites in the same xdist worker.
        self.app.dependency_overrides.pop(get_db, None)
        self.app.dependency_overrides.pop(csrf_protect, None)
        self.test_pool.close_all()
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _failed_attempts(self, username: str):
        conn = self.test_pool.get_connection()
        try:
            row = conn.execute(
                "SELECT failed_attempts, locked_until FROM users WHERE username = ?",
                (username,),
            ).fetchone()
            return int(row[0]), row[1]
        finally:
            self.test_pool.release_connection(conn)

    def test_successful_login_resets_failed_attempts(self):
        register = self.client.post(
            "/api/auth/register",
            json={"username": "resetter", "password": "OldPass123"},
        )
        self.assertIn(register.status_code, (200, 201))

        # Four failures stay below the lockout threshold of five.
        for _ in range(4):
            resp = self.client.post(
                "/api/auth/login",
                json={"username": "resetter", "password": "WrongPass999"},
            )
            self.assertEqual(resp.status_code, 401)
        failed, locked_until = self._failed_attempts("resetter")
        self.assertEqual(failed, 4)
        self.assertIsNone(locked_until)

        # Successful login must clear the counter and any lock timestamp.
        resp = self.client.post(
            "/api/auth/login",
            json={"username": "resetter", "password": "OldPass123"},
        )
        self.assertEqual(resp.status_code, 200)
        failed, locked_until = self._failed_attempts("resetter")
        self.assertEqual(failed, 0, "successful login must reset failed_attempts")
        self.assertIsNone(locked_until)

        # The counter genuinely restarted: one failure after the successful
        # login must NOT lock (a dropped reset would make this the 5th strike).
        resp = self.client.post(
            "/api/auth/login",
            json={"username": "resetter", "password": "WrongPass999"},
        )
        # 401 (not 423): one failure after the reset cannot lock — the reset is
        # proven by the counter assertion below.
        self.assertEqual(resp.status_code, 401)
        failed, _ = self._failed_attempts("resetter")
        self.assertEqual(failed, 1)


if __name__ == "__main__":
    unittest.main()
