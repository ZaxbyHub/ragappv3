"""
CSRF token integration tests, in-process via FastAPI's TestClient.

Ported off the live gate (issue #563 / audit finding C10): this module
previously probed a hardcoded live localhost endpoint at import time and gated
every class on the answer, so all twelve tests silently skipped in CI — and on
any developer machine with an unrelated listener on that port, the suite fired
real register/token requests at that foreign service instead. The four
behaviors below had no other CI coverage, so they are preserved here against
the in-process app; the remaining register-flow 403/200 wiring already runs in
CI via ``tests/test_csrf_auth.py``.

Genuinely network-bound tests are marked ``@pytest.mark.live`` (see
``tests/conftest.py`` for the opt-in mechanism) — none of those live here.
"""

import os
import sys
import tempfile
import unittest

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Stub missing optional dependencies (mirrors test_csrf_auth.py / conftest.py)
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
from app.security import CSRFManager


class TestCSRFIntegrationInProcess(unittest.TestCase):
    """The four CSRF behaviors ported off the live-port gate (issue #563).

    Runs against the real app and the real CSRF validator (this module's
    source mentions csrf, so the conftest test bypass stays off for it).
    """

    def setUp(self):
        """Set up test client with temporary database and CSRF manager."""
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")

        init_db(self.db_path)
        run_migrations(self.db_path)

        self._original_jwt_secret = settings.jwt_secret_key
        self._original_users_enabled = settings.users_enabled
        settings.jwt_secret_key = "test-secret-key-for-testing-at-least-32-chars-long"
        settings.users_enabled = True

        self.test_pool = SQLiteConnectionPool(self.db_path, max_size=5)

        from app.api.deps import get_db
        from app.main import app as main_app

        def get_test_db():
            conn = self.test_pool.get_connection()
            try:
                yield conn
            finally:
                self.test_pool.release_connection(conn)

        main_app.dependency_overrides[get_db] = get_test_db
        main_app.state.csrf_manager = CSRFManager(
            redis_url="redis://localhost:6379/0", ttl=900
        )

        self.client = TestClient(main_app)
        self.app = main_app

    def tearDown(self):
        """Restore settings, overrides, pool, and temp directory."""
        settings.jwt_secret_key = self._original_jwt_secret
        settings.users_enabled = self._original_users_enabled
        self.app.dependency_overrides.clear()
        self.test_pool.close_all()
        import shutil

        try:
            shutil.rmtree(self.temp_dir)
        except Exception:
            pass

    def _get_csrf_pair(self):
        """Fetch a fresh (cookie, header) CSRF pair from the token endpoint."""
        response = self.client.get("/api/csrf-token")
        self.assertEqual(response.status_code, 200)
        token = response.json()["csrf_token"]
        cookie = response.cookies.get("X-CSRF-Token")
        self.assertIsNotNone(cookie, "CSRF cookie should be set")
        self.assertIsNotNone(token, "CSRF token should be returned")
        return cookie, token

    def test_csrf_token_sets_cookie(self):
        """GET /api/csrf-token issues the X-CSRF-Token cookie matching the body token."""
        response = self.client.get("/api/csrf-token")
        self.assertEqual(response.status_code, 200)

        token = response.json()["csrf_token"]
        self.assertIsInstance(token, str)
        self.assertGreater(len(token), 10, "csrf_token should be non-empty")
        self.assertRegex(
            token,
            r"^[A-Za-z0-9_-]+$",
            "csrf_token should be URL-safe base64",
        )

        cookie = response.cookies.get("X-CSRF-Token")
        self.assertIsNotNone(cookie, "Response should set X-CSRF-Token cookie")
        self.assertGreater(len(cookie), 10, "Cookie value should be non-empty")
        self.assertEqual(
            cookie,
            token,
            "csrf_token in body should match X-CSRF-Token cookie",
        )

    def test_cookie_attributes(self):
        """X-CSRF-Token cookie should have appropriate security attributes."""
        response = self.client.get("/api/csrf-token")
        self.assertEqual(response.status_code, 200)

        set_cookie = response.headers.get("set-cookie", "")

        # Cookie should have SameSite (issue_csrf_token sets samesite="lax")
        self.assertTrue(
            "samesite" in set_cookie.lower(),
            f"Cookie should have SameSite attribute, got: {set_cookie}",
        )

        # Cookie should have Max-Age or Expires (issue_csrf_token sets max_age)
        self.assertTrue(
            "max-age" in set_cookie.lower() or "expires" in set_cookie.lower(),
            f"Cookie should have Max-Age or Expires, got: {set_cookie}",
        )

    def test_each_request_generates_new_token(self):
        """Each call to /api/csrf-token should generate a new token."""
        response1 = self.client.get("/api/csrf-token")
        response2 = self.client.get("/api/csrf-token")

        self.assertEqual(response1.status_code, 200)
        self.assertEqual(response2.status_code, 200)

        token1 = response1.json()["csrf_token"]
        token2 = response2.json()["csrf_token"]

        self.assertNotEqual(
            token1, token2, "Each /csrf-token call should generate a new token"
        )
        cookie1 = response1.cookies.get("X-CSRF-Token")
        cookie2 = response2.cookies.get("X-CSRF-Token")
        self.assertNotEqual(
            cookie1, cookie2, "Each /csrf-token call should set a fresh cookie"
        )

    def test_token_used_twice_should_still_work(self):
        """CSRF tokens are not single-use: one pair validates two registers.

        The pair is transmitted request-level (explicit cookie + header) on
        both POSTs: the server rotates the CSRF cookie on protected POSTs,
        which desyncs the TestClient jar (see test_csrf_auth.py). The token
        itself is never consumed — CSRFManager.validate_token only checks
        existence/TTL — so the second register must also succeed.
        """
        cookie, token = self._get_csrf_pair()

        # The GET above put the cookie into the client jar; drop it so the
        # explicit request-level pair below is the only value transmitted.
        self.client.cookies.pop("X-CSRF-Token", None)

        response1 = self.client.post(
            "/api/auth/register",
            json={"username": "reuse_first", "password": "Password123"},
            cookies={"X-CSRF-Token": cookie},
            headers={"X-CSRF-Token": token},
        )
        self.assertEqual(
            response1.status_code,
            200,
            f"First registration failed: {response1.text}",
        )

        # Register rotates the CSRF cookie into the client jar; if left there
        # it would transmit the NEW value ahead of our explicit pair and the
        # strict first-transmitted comparison would 403 (not a token problem).
        self.client.cookies.pop("X-CSRF-Token", None)

        response2 = self.client.post(
            "/api/auth/register",
            json={"username": "reuse_second", "password": "Password123"},
            cookies={"X-CSRF-Token": cookie},
            headers={"X-CSRF-Token": token},
        )
        self.assertEqual(
            response2.status_code,
            200,
            f"CSRF token should remain valid after single use, got: {response2.text}",
        )


if __name__ == "__main__":
    unittest.main()
