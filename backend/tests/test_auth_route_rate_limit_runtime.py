"""Real-ASGI runtime 429 enforcement tests for the auth rate limits (issue #659).

The source-scan tests in test_auth_rate_limiting.py prove the decorator is
present with the correct order; these tests prove the policy ENFORCES by
driving the real POST /auth/{register,login,refresh} routes through TestClient
against a fully-wired app (auth + DB + middleware). Mirrors the proven pattern
in backend/tests/test_change_password.py::TestChangePasswordRateLimit.

Background (issue #659, F1): the three routes carried `@limiter.limit(...)`
stacked ABOVE `@router.post(...)`, so the router stored the unwrapped function
and the limit never fired — no request was ever throttled. The fix swapped the
order to router-outermost (matching change-password and the repo's other 35
correct limiter sites). Each test below exceeds the documented window and
asserts the (N+1)th call returns HTTP 429.

slowapi checks the limit inside the decorator wrapper AFTER FastAPI resolves
dependencies, so every call must carry a parseable body to count. Pre-limit
calls fail inside the handler body (401/400) but still consume the bucket:

- login: 11 calls against NON-EXISTENT usernames — 1-10 return 401 via the
  unknown-user path, the 11th returns 429. (A real username would trip the
  5-strike account lockout and return 423 from call 5, a different behavior.)
- register: 6 calls with a too-weak password — 1-5 return 400 (nothing is
  created), the 6th returns 429.
- refresh: 31 calls with no refresh cookie — 1-30 return 401, the 31st returns
  429.

Per-test isolation: the rate limiter is process-global (per xdist worker); the
autouse `_reset_rate_limiter()` fixture in backend/tests/conftest.py resets it
before and after every test, and the suite forces in-memory storage
(REDIS_URL=""), so each method below starts from a clean bucket.

The RateLimitExceeded -> 429 translation is performed by SlowAPIMiddleware's
default handler (no explicit exception handler registration in main.py).
"""

import os
import shutil
import sys
import tempfile
import unittest

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Stub missing optional dependencies (mirrors test_change_password.py)
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

from fastapi.testclient import TestClient  # noqa: E402

from app.config import settings  # noqa: E402
from app.models.database import (  # noqa: E402
    SQLiteConnectionPool,
    init_db,
    run_migrations,
)


class _AuthRouteRateLimitBase(unittest.TestCase):
    """Shared wiring: real app, temp DB, dependency overrides.

    Mirrors backend/tests/test_change_password.py::TestChangePasswordRateLimit.
    """

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        init_db(self.db_path)
        run_migrations(self.db_path)

        self._original_jwt_secret = settings.jwt_secret_key
        self._original_users_enabled = settings.users_enabled
        settings.jwt_secret_key = "test-secret-key-for-testing-at-least-32-chars-long"
        # Register's handler rejects with 403 before the password check when
        # users are disabled; conftest defaults USERS_ENABLED=false, so the
        # register sequence needs this override to reach the 400 path.
        settings.users_enabled = True
        self.addCleanup(
            setattr, settings, "jwt_secret_key", self._original_jwt_secret
        )
        self.addCleanup(
            setattr, settings, "users_enabled", self._original_users_enabled
        )

        self.test_pool = SQLiteConnectionPool(self.db_path, max_size=5)
        self.addCleanup(self.test_pool.close_all)

        from app.api.deps import get_db
        from app.main import app as main_app
        from app.security import CSRFManager, csrf_protect

        def get_test_db():
            conn = self.test_pool.get_connection()
            try:
                yield conn
            finally:
                self.test_pool.release_connection(conn)

        main_app.dependency_overrides[get_db] = get_test_db
        self.addCleanup(main_app.dependency_overrides.pop, get_db, None)
        self.csrf_manager = CSRFManager(
            redis_url="redis://localhost:6379/0", ttl=900
        )
        main_app.state.csrf_manager = self.csrf_manager
        main_app.dependency_overrides[csrf_protect] = lambda: "test-csrf-token"
        self.addCleanup(main_app.dependency_overrides.pop, csrf_protect, None)

        self.client = TestClient(main_app)
        self.app = main_app

        self.addCleanup(lambda: shutil.rmtree(self.temp_dir, ignore_errors=True))

    def _assert_route_is_limiter_wrapped(self, path_fragment):
        """The router must store the limiter-WRAPPED endpoint for the route.

        Guards the decorator ORDER: @router.post must be outermost so the
        router stores the wrapper produced by @limiter.limit. The reverse
        order (limiter above router) silently breaks enforcement because the
        router stores the unwrapped function and the limit check never fires.
        """
        from app.api.routes.auth import router

        for route in router.routes:
            if hasattr(route, "path") and path_fragment in route.path:
                self.assertTrue(
                    hasattr(route.endpoint, "__wrapped__"),
                    f"route {route.path} must be the limiter-wrapped function "
                    "(decorator order: @router.post outermost, @limiter.limit below)",
                )
                return
        self.fail(f"route containing {path_fragment!r} not found in auth router")


class TestLoginRouteRuntimeRateLimit(_AuthRouteRateLimitBase):
    """10/minute on POST /auth/login: the 11th call inside the window 429s."""

    def test_login_route_endpoint_is_wrapped(self):
        self._assert_route_is_limiter_wrapped("/login")

    def test_eleventh_login_returns_429(self):
        """Calls 1-10 return 401 (unknown user) and count; call 11 returns 429.

        Non-existent usernames keep every pre-limit call on the unknown-user
        401 path (a real username would trip the 5-strike account lockout at
        auth.py:495-515 and return 423 from call 5).
        """
        for i in range(10):
            resp = self.client.post(
                "/api/auth/login",
                json={"username": f"rate-limit-probe-{i}", "password": "WrongPass123"},
            )
            self.assertEqual(
                resp.status_code,
                401,
                f"call {i + 1}/10 should return 401 (unknown user), "
                f"got {resp.status_code}",
            )
        resp = self.client.post(
            "/api/auth/login",
            json={"username": "rate-limit-probe-final", "password": "WrongPass123"},
        )
        self.assertEqual(
            resp.status_code,
            429,
            f"11th call should return 429 (rate limited), got {resp.status_code}",
        )


class TestRegisterRouteRuntimeRateLimit(_AuthRouteRateLimitBase):
    """5/hour on POST /auth/register: the 6th call inside the window 429s."""

    def test_register_route_endpoint_is_wrapped(self):
        self._assert_route_is_limiter_wrapped("/register")

    def test_sixth_register_returns_429(self):
        """Calls 1-5 return 400 (weak password, no user created); call 6 429s.

        The limiter check runs before the handler body, so the rejected
        registrations still consume the bucket.
        """
        for i in range(5):
            resp = self.client.post(
                "/api/auth/register",
                json={"username": f"register-probe-{i}", "password": "short"},
            )
            self.assertEqual(
                resp.status_code,
                400,
                f"call {i + 1}/5 should return 400 (weak password), "
                f"got {resp.status_code}",
            )
        resp = self.client.post(
            "/api/auth/register",
            json={"username": "register-probe-final", "password": "short"},
        )
        self.assertEqual(
            resp.status_code,
            429,
            f"6th call should return 429 (rate limited), got {resp.status_code}",
        )


class TestRefreshRouteRuntimeRateLimit(_AuthRouteRateLimitBase):
    """30/minute on POST /auth/refresh: the 31st call inside the window 429s."""

    def test_refresh_route_endpoint_is_wrapped(self):
        self._assert_route_is_limiter_wrapped("/refresh")

    def test_thirty_first_refresh_returns_429(self):
        """Calls 1-30 return 401 (no refresh cookie) and count; call 31 429s."""
        for i in range(30):
            resp = self.client.post("/api/auth/refresh")
            self.assertEqual(
                resp.status_code,
                401,
                f"call {i + 1}/30 should return 401 (missing refresh token), "
                f"got {resp.status_code}",
            )
        resp = self.client.post("/api/auth/refresh")
        self.assertEqual(
            resp.status_code,
            429,
            f"31st call should return 429 (rate limited), got {resp.status_code}",
        )


if __name__ == "__main__":
    unittest.main()
