"""
Auth rate limiting verification tests.

Tests cover:
1. Rate limiting decorators on register (5/hour), login (10/minute), refresh (30/minute)
2. register handler accepts request: Request parameter
3. Other endpoints (logout, setup-status, me, me PATCH) do NOT have rate limiting
4. limiter import is present in auth.py
5. .env.example contains JWT_SECRET_KEY, USERS_ENABLED, JWT_ALGORITHM
6. main.py has JWT warning log and CORS wildcard warning log
"""

import os
import re
import sys

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import unittest

AUTH_PY = os.path.join(
    os.path.dirname(__file__), "..", "app", "api", "routes", "auth.py"
)
MAIN_PY = os.path.join(os.path.dirname(__file__), "..", "app", "main.py")
ENV_EXAMPLE = os.path.join(os.path.dirname(__file__), "..", "..", ".env.example")


def _read_file(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


class TestRateLimitingDecorators(unittest.TestCase):
    """Verify rate limiting decorators are present on the correct endpoints."""

    @classmethod
    def setUpClass(cls):
        cls.src = _read_file(AUTH_PY)

    def test_limiter_import_present(self):
        """auth.py must import limiter from app.limiter."""
        self.assertIn(
            "from app.limiter import limiter",
            self.src,
            "Missing 'from app.limiter import limiter' import in auth.py",
        )

    def test_register_has_5_per_hour_limit(self):
        """Register endpoint must have @limiter.limit('5/hour')."""
        match = re.search(
            r'@limiter\.limit\(\s*["\'](\d+/\w+)["\']\s*\)\s*\n\s*@router\.post\(\s*["\']\/register["\']',
            self.src,
        )
        self.assertIsNotNone(
            match,
            "Could not find @limiter.limit decorator before @router.post('/register')",
        )
        self.assertEqual(
            match.group(1),
            "5/hour",
            f"Expected rate limit '5/hour' on register, got '{match.group(1)}'",
        )

    def test_login_has_10_per_minute_limit(self):
        """Login endpoint must have @limiter.limit('10/minute')."""
        match = re.search(
            r'@limiter\.limit\(\s*["\'](\d+/\w+)["\']\s*\)\s*\n\s*@router\.post\(\s*["\']\/login["\']',
            self.src,
        )
        self.assertIsNotNone(
            match,
            "Could not find @limiter.limit decorator before @router.post('/login')",
        )
        self.assertEqual(
            match.group(1),
            "10/minute",
            f"Expected rate limit '10/minute' on login, got '{match.group(1)}'",
        )

    def test_refresh_has_30_per_minute_limit(self):
        """Refresh endpoint must have @limiter.limit('30/minute')."""
        match = re.search(
            r'@limiter\.limit\(\s*["\'](\d+/\w+)["\']\s*\)\s*\n\s*@router\.post\(\s*["\']\/refresh["\']',
            self.src,
        )
        self.assertIsNotNone(
            match,
            "Could not find @limiter.limit decorator before @router.post('/refresh')",
        )
        self.assertEqual(
            match.group(1),
            "30/minute",
            f"Expected rate limit '30/minute' on refresh, got '{match.group(1)}'",
        )

    def test_change_password_has_10_per_minute_limit(self):
        """change-password endpoint must have @limiter.limit('10/minute').

        Regression for #387 MED-8: this endpoint was previously unthrottled,
        leaving an authenticated brute-force vector against current_password.
        The decorator must sit immediately above @router.post('/change-password'),
        matching the register/login/refresh convention.
        """
        match = re.search(
            r'@limiter\.limit\(\s*["\'](\d+/\w+)["\']\s*\)\s*\n\s*@router\.post\(\s*["\']\/change-password["\']',
            self.src,
        )
        self.assertIsNotNone(
            match,
            "Could not find @limiter.limit decorator before "
            "@router.post('/change-password')",
        )
        self.assertEqual(
            match.group(1),
            "10/minute",
            f"Expected rate limit '10/minute' on change-password, "
            f"got '{match.group(1)}'",
        )


class TestRegisterRequestParameter(unittest.TestCase):
    """Verify register handler accepts request: Request parameter."""

    @classmethod
    def setUpClass(cls):
        cls.src = _read_file(AUTH_PY)

    def test_register_has_request_param(self):
        """register function must accept 'request: Request' parameter."""
        # Find the async def register( ... ) block
        match = re.search(r"async def register\s*\((.*?)\)\s*:", self.src, re.DOTALL)
        self.assertIsNotNone(match, "Could not find 'async def register' function")
        params = match.group(1)
        self.assertRegex(
            params,
            r"request\s*:\s*Request",
            "register() must accept 'request: Request' parameter",
        )


class TestNoRateLimitOnOtherEndpoints(unittest.TestCase):
    """Verify logout, setup-status, me, and me PATCH do NOT have rate limiting."""

    @classmethod
    def setUpClass(cls):
        cls.src = _read_file(AUTH_PY)

    def _has_limiter_before_route(self, method, path):
        """Check if @limiter.limit appears immediately before a route definition."""
        pattern = (
            r"@limiter\.limit\("
            r".*?\)\s*\n\s*"
            r"@router\.{method}\(\s*['\"]{path}['\"]".format(method=method, path=path)
        )
        return re.search(pattern, self.src, re.DOTALL) is not None

    def test_logout_no_rate_limit(self):
        self.assertFalse(
            self._has_limiter_before_route("post", "/logout"),
            "logout must NOT have @limiter.limit decorator",
        )

    def test_setup_status_no_rate_limit(self):
        self.assertFalse(
            self._has_limiter_before_route("get", "/setup-status"),
            "setup-status must NOT have @limiter.limit decorator",
        )

    def test_me_get_no_rate_limit(self):
        self.assertFalse(
            self._has_limiter_before_route("get", "/me"),
            "GET /me must NOT have @limiter.limit decorator",
        )

    def test_me_patch_no_rate_limit(self):
        self.assertFalse(
            self._has_limiter_before_route("patch", "/me"),
            "PATCH /me must NOT have @limiter.limit decorator",
        )


class TestEnvExample(unittest.TestCase):
    """Verify .env.example contains JWT auth variables."""

    @classmethod
    def setUpClass(cls):
        cls.src = _read_file(ENV_EXAMPLE)

    def test_jwt_secret_key_present(self):
        self.assertIn(
            "JWT_SECRET_KEY", self.src, ".env.example must contain JWT_SECRET_KEY"
        )

    def test_users_enabled_present(self):
        self.assertIn(
            "USERS_ENABLED", self.src, ".env.example must contain USERS_ENABLED"
        )

    def test_jwt_algorithm_present(self):
        self.assertIn(
            "JWT_ALGORITHM", self.src, ".env.example must contain JWT_ALGORITHM"
        )


class TestMainPySecurityWarnings(unittest.TestCase):
    """Verify main.py has JWT warning and CORS wildcard warning."""

    @classmethod
    def setUpClass(cls):
        cls.src = _read_file(MAIN_PY)

    def test_jwt_default_key_warning(self):
        """main.py must warn when jwt_secret_key is using default value."""
        self.assertIn("jwt_secret_key", self.src, "main.py must check jwt_secret_key")
        self.assertRegex(
            self.src,
            r"SECURITY WARNING.*jwt_secret_key",
            "main.py must log a SECURITY WARNING about jwt_secret_key default",
        )

    def test_cors_wildcard_warning(self):
        """main.py must warn when CORS origins contain wildcard."""
        self.assertIn(
            "backend_cors_origins",
            self.src,
            "main.py must reference backend_cors_origins",
        )
        self.assertRegex(
            self.src,
            r"SECURITY WARNING.*CORS.*wildcard",
            "main.py must log a SECURITY WARNING about CORS wildcard",
        )


class TestChangePasswordRuntimeEnforcement(unittest.TestCase):
    """Runtime proof that a 10/minute limit actually blocks the 11th call.

    The source-scan test above proves the decorator is present; this test
    proves the policy enforces. Mirrors the established runtime pattern in
    backend/tests/test_rate_limiting.py::TestRuntimeRateLimitEnforcement.

    Note: a full HTTP 429 integration test through the ASGI app is brittle in
    CI because slowapi's in-memory limiter state leaks across tests in the same
    process (see backend/tests/conftest.py note about 429 interference). The
    in-process RateLimitExceeded assertion is the load-bearing claim: if the
    decorator enforces at the wrapper level, SlowAPIMiddleware (registered in
    main.py) translates the raised RateLimitExceeded into an HTTP 429 via
    slowapi's default _rate_limit_exceeded_handler.
    """

    def _make_request(self, lim):
        """Build a real Starlette Request wired with the slowapi state the
        SlowAPIMiddleware normally sets. Mirrors the proven helper in
        backend/tests/test_rate_limiting.py::TestRuntimeRateLimitEnforcement.
        """
        from types import SimpleNamespace

        from starlette.applications import Starlette
        from starlette.requests import Request

        app = Starlette()
        app.state.limiter = lim
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/change-password",
            "headers": [],
            "query_string": b"",
            "client": ("testclient", 12345),
            "server": ("testserver", 80),
            "scheme": "http",
            "root_path": "",
            "app": app,
        }
        req = Request(scope)
        # The middleware normally initializes view_rate_limit on request.state
        # before the decorated handler reads it.
        req.state.view_rate_limit = SimpleNamespace()
        return req

    def test_change_password_limit_blocks_after_10(self):
        """A handler under a 10/minute limit must raise on the 11th call."""
        from slowapi.errors import RateLimitExceeded

        from app.limiter import WhitelistLimiter

        lim = WhitelistLimiter(
            key_func=lambda _request=None: "testclient",
            storage_uri="memory://",
        )

        @lim.limit("10/minute")
        def handler(request):
            return "ok"

        # First 10 calls are within the limit (fresh request per call, matching
        # the established runtime pattern).
        for i in range(10):
            self.assertEqual(
                handler(self._make_request(lim)), "ok",
                f"call {i + 1} should pass",
            )
        # 11th call must be rejected.
        with self.assertRaises(RateLimitExceeded):
            handler(self._make_request(lim))


if __name__ == "__main__":
    unittest.main()
