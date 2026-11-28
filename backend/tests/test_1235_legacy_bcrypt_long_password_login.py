"""Issue #1235: legacy bcrypt users with >72-byte passwords can log in again.

bcrypt 4.x silently hashed only the first 72 bytes of a secret; bcrypt 5.0.0
raises ValueError instead, which turned these users' logins into 401s. This
drives the real /api/auth/login route against a hash bcrypt 4.3.0 actually
wrote and pins the transparent rehash to argon2id on success.
"""

import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# The login flow below never exercises CSRF enforcement (csrf_protect is
# overridden), so the module must not be classified as CSRF-managing.
CSRF_TEST_POLICY = "naive"

from app.api.deps import get_db  # noqa: E402
from app.config import settings  # noqa: E402
from app.models.database import (  # noqa: E402
    SQLiteConnectionPool,
    init_db,
    run_migrations,
)
from app.security import csrf_protect  # noqa: E402
from app.services.auth_service import verify_password  # noqa: E402

# Generated with bcrypt 4.3.0 from the FULL 80-byte password (see the matching
# fixture in tests/test_auth_service.py): bcrypt.hashpw(pw.encode(), gensalt(4)).
LEGACY_PASSWORD = "Legacy1" + "x" * 73
LEGACY_HASH_BCRYPT4 = "$2b$04$rSa1evkYXBKOP6jn7ZIM1ODr.fzsnVPM6YH2PagjUDTtgq2r8XGxS"
USERNAME = "legacy72"


class TestLegacyBcryptLongPasswordLogin(unittest.TestCase):
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

        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO users (username, hashed_password, full_name, role, is_active) "
                "VALUES (?, ?, ?, ?, ?)",
                (USERNAME, LEGACY_HASH_BCRYPT4, "Legacy User", "member", True),
            )
            conn.commit()
        finally:
            conn.close()

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
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _stored_hash(self) -> str:
        conn = self.test_pool.get_connection()
        try:
            row = conn.execute(
                "SELECT hashed_password FROM users WHERE username = ?", (USERNAME,)
            ).fetchone()
            return row[0]
        finally:
            self.test_pool.release_connection(conn)

    def _login(self, password: str):
        return self.client.post(
            "/api/auth/login", json={"username": USERNAME, "password": password}
        )

    def test_long_password_login_succeeds_and_rehashes_to_argon2id(self):
        resp = self._login(LEGACY_PASSWORD)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["user"]["username"], USERNAME)

        new_hash = self._stored_hash()
        self.assertTrue(
            new_hash.startswith("$argon2id$"),
            f"legacy hash must be replaced by argon2id on login, got {new_hash[:10]}",
        )
        # The replacement covers the full password, not just 72 bytes.
        self.assertTrue(verify_password(LEGACY_PASSWORD, new_hash))
        self.assertFalse(verify_password(LEGACY_PASSWORD[:72] + "TAIL", new_hash))

        # The user keeps logging in with the same password after the upgrade...
        self.assertEqual(self._login(LEGACY_PASSWORD).status_code, 200)
        # ...and bytes past 72 now count: a tail-only variant is rejected.
        self.assertEqual(self._login(LEGACY_PASSWORD[:72] + "TAIL").status_code, 401)

    def test_wrong_long_password_is_rejected_and_hash_untouched(self):
        wrong = "M" + LEGACY_PASSWORD[1:]  # >72 bytes, differs in byte 0
        resp = self._login(wrong)
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(self._stored_hash(), LEGACY_HASH_BCRYPT4)


if __name__ == "__main__":
    unittest.main()
