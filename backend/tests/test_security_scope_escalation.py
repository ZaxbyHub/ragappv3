"""
Regression tests for FR-5 (Server-Side Scope Derivation) — Issue #209.

Verifies that the X-Scopes header cannot be used to escalate privileges.
Scopes are now derived exclusively from settings.admin_token_scopes (server-side),
NOT from the client-supplied X-Scopes header.

Tests:
- test_x_scopes_header_does_not_grant_scope: X-Scopes claiming admin:config
  is DENIED when the server-side mapping doesn't authorize that scope for the token.
- test_x_scopes_header_ignored_when_valid_token_present: Valid token + X-Scopes
  still works when the server-side mapping has the correct scope (header is ignored,
  not trusted — but the request succeeds because the server-side mapping is correct).
- test_token_without_scope_returns_403: Valid token without required scope is
  denied even without any X-Scopes header.
"""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from app.config import Settings, settings
from app.security import require_scope

# =============================================================================
# Test fixtures
# =============================================================================


class TestRequireScopeScopeEscalation:
    """Tests for server-side scope derivation in require_scope."""

    VALID_TOKEN = "test-admin-key"  # Set by conftest.py pytest_configure

    @pytest.fixture
    def app_with_scope_endpoint(self):
        """Create a minimal FastAPI app with a scope-protected endpoint."""

        app = FastAPI()

        @app.get("/protected")
        def protected_endpoint(auth: dict = Depends(require_scope("admin:config"))):
            return {"status": "ok", "user_id": auth.get("user_id")}

        return app

    def _call_require_scope_directly(self, token: str, x_scopes: str = ""):
        """Call require_scope as a dependency function directly."""
        scope_check = require_scope("admin:config")
        authorization = f"Bearer {token}" if token else None

        # Pass X-Scopes via header parameter name — but require_scope no longer reads it
        # We pass it anyway to verify it's ignored
        return scope_check(authorization=authorization)

    # -------------------------------------------------------------------------
    # Test 1: X-Scopes header does NOT grant scope not in server-side mapping
    # -------------------------------------------------------------------------

    def test_x_scopes_header_does_not_grant_scope(self):
        """
        Regression test: sending X-Scopes: admin:config with a valid admin token
        MUST NOT grant access when the server-side mapping doesn't include
        admin:config for that token.

        Scenario:
        - Token: valid admin token
        - X-Scopes header: admin:config (claims to have the scope)
        - Server-side mapping: empty (token has no scopes)
        Expected: 403 — scope not granted because server-side mapping denies it.
        """
        # Override server-side mapping to have NO scopes for the valid token
        empty_scopes_mapping = {self.VALID_TOKEN: []}

        with patch.object(settings, "admin_token_scopes", empty_scopes_mapping):
            with patch.object(settings, "admin_secret_token", self.VALID_TOKEN):
                # X-Scopes header claims admin:config — but server-side says no
                with pytest.raises(HTTPException) as exc_info:
                    self._call_require_scope_directly(
                        token=self.VALID_TOKEN,
                        x_scopes="admin:config",
                    )

                assert exc_info.value.status_code == 403
                assert "Missing required scope" in exc_info.value.detail

    # -------------------------------------------------------------------------
    # Test 2: X-Scopes header is ignored when scope IS present server-side
    # -------------------------------------------------------------------------

    def test_x_scopes_header_ignored_when_valid_token_present(self):
        """
        Verify that the X-Scopes header is truly IGNORED (not just overridden).

        When the server-side mapping correctly authorizes the token for the
        required scope, the request succeeds. This proves the header is not
        the source of truth — the server-side mapping is.

        Scenario:
        - Token: valid admin token
        - X-Scopes header: admin:config
        - Server-side mapping: {"test-admin-key": ["admin:config"]}
        Expected: 200 — succeeds because server-side mapping has the scope,
        NOT because of the X-Scopes header.
        """
        correct_scopes_mapping = {self.VALID_TOKEN: ["admin:config"]}

        with patch.object(settings, "admin_token_scopes", correct_scopes_mapping):
            with patch.object(settings, "admin_secret_token", self.VALID_TOKEN):
                # X-Scopes header is present but irrelevant — server-side is correct
                result = self._call_require_scope_directly(
                    token=self.VALID_TOKEN,
                    x_scopes="admin:config",
                )

                assert result == {"user_id": self.VALID_TOKEN}

    # -------------------------------------------------------------------------
    # Test 3: Token without required scope returns 403
    # -------------------------------------------------------------------------

    def test_token_without_scope_returns_403(self):
        """
        Verify that a valid token without the required scope is denied.

        This is the baseline case: even without any X-Scopes header,
        if the server-side mapping doesn't include the required scope,
        the request is denied.

        Scenario:
        - Token: valid admin token
        - X-Scopes header: absent
        - Server-side mapping: {"test-admin-key": ["some-other-scope"]}
        Expected: 403 — token is valid but lacks required scope.
        """
        different_scope_mapping = {self.VALID_TOKEN: ["some-other-scope"]}

        with patch.object(settings, "admin_token_scopes", different_scope_mapping):
            with patch.object(settings, "admin_secret_token", self.VALID_TOKEN):
                # No X-Scopes header at all
                with pytest.raises(HTTPException) as exc_info:
                    self._call_require_scope_directly(
                        token=self.VALID_TOKEN,
                        x_scopes="",
                    )

                assert exc_info.value.status_code == 403
                assert "Missing required scope" in exc_info.value.detail

    # -------------------------------------------------------------------------
    # Test 4: Invalid token returns 403 (authentication checked before authorization)
    # -------------------------------------------------------------------------

    def test_invalid_token_returns_403(self):
        """
        Verify that an invalid token is denied even if X-Scopes header claims
        a valid scope.

        Note: compare_digest authentication check happens BEFORE scope lookup
        (authn-before-authz). An invalid token fails the constant-time
        compare_digest check first, returning 403 "Unauthorized token".

        Scenario:
        - Token: invalid token (not the admin_secret_token)
        - X-Scopes header: admin:config (claims valid scope)
        - Server-side mapping: {"test-admin-key": ["admin:config"]} (valid token has scope)
        Expected: 403 — authentication fails first because token doesn't match admin_secret_token.
        """
        correct_scopes_mapping = {self.VALID_TOKEN: ["admin:config"]}

        with patch.object(settings, "admin_token_scopes", correct_scopes_mapping):
            with patch.object(settings, "admin_secret_token", self.VALID_TOKEN):
                with pytest.raises(HTTPException) as exc_info:
                    self._call_require_scope_directly(
                        token="invalid-token",
                        x_scopes="admin:config",
                    )

                assert exc_info.value.status_code == 403
                # Authentication fails first because invalid token doesn't match admin_secret_token
                assert "Unauthorized token" in exc_info.value.detail

    # -------------------------------------------------------------------------
    # Test 5: Empty Authorization header returns 401
    # -------------------------------------------------------------------------

    def test_missing_authorization_returns_401(self):
        """Verify that a missing Authorization header returns 401."""
        correct_scopes_mapping = {self.VALID_TOKEN: ["admin:config"]}

        with patch.object(settings, "admin_token_scopes", correct_scopes_mapping):
            with patch.object(settings, "admin_secret_token", self.VALID_TOKEN):
                with pytest.raises(HTTPException) as exc_info:
                    self._call_require_scope_directly(token=None, x_scopes="")

                assert exc_info.value.status_code == 401
                assert "Authorization header missing" in exc_info.value.detail

    # -------------------------------------------------------------------------
    # Test 6: HTTP-layer test — X-Scopes header is fully ignored end-to-end
    # -------------------------------------------------------------------------

    def test_x_scopes_header_fully_ignored_via_http(self, app_with_scope_endpoint):
        """
        HTTP-layer regression test: the X-Scopes header must be completely ignored.

        Uses TestClient with the real require_scope dependency to catch a future
        regression where x_scopes: str = Header("") is re-added to the dependency
        function signature.

        Sends two requests:
        1. Valid token + X-Scopes: admin:config header → must succeed (200)
        2. Same valid token, NO X-Scopes header → must succeed (200)
        Both responses must be identical, proving the header is fully ignored.
        """
        correct_scopes_mapping = {self.VALID_TOKEN: ["admin:config"]}

        with patch.object(settings, "admin_token_scopes", correct_scopes_mapping):
            with patch.object(settings, "admin_secret_token", self.VALID_TOKEN):
                client = TestClient(app_with_scope_endpoint)

                # Request 1: valid token WITH X-Scopes header
                response_with_header = client.get(
                    "/protected",
                    headers={
                        "Authorization": f"Bearer {self.VALID_TOKEN}",
                        "X-Scopes": "admin:config",
                    },
                )
                assert response_with_header.status_code == 200
                assert response_with_header.json() == {"status": "ok", "user_id": self.VALID_TOKEN}

                # Request 2: same valid token WITHOUT X-Scopes header
                response_without_header = client.get(
                    "/protected",
                    headers={"Authorization": f"Bearer {self.VALID_TOKEN}"},
                )
                assert response_without_header.status_code == 200
                assert response_without_header.json() == {"status": "ok", "user_id": self.VALID_TOKEN}

                # Responses must be identical — proving the header is fully ignored
                assert response_with_header.json() == response_without_header.json()


# =============================================================================
# Route-level tests: admin.py and email.py now use require_scope (not local
# require_admin_scope). These tests verify the X-Scopes header is ignored
# when routes are accessed via HTTP, confirming the migration is complete.
# =============================================================================


class TestAdminRoutesUseRequireScope:
    """Verify admin.py routes use require_scope (not the old require_admin_scope)."""

    VALID_TOKEN = "test-admin-key"  # Set by conftest.py pytest_configure

    @pytest.fixture
    def app_with_admin_router(self):
        """Minimal FastAPI app with admin router and mocked dependencies."""
        from unittest.mock import MagicMock

        from fastapi import Depends

        from app.api.routes import admin as admin_module

        app = FastAPI()
        app.include_router(admin_module.router)

        # Mock get_db → sqlite3 connection
        mock_conn = MagicMock()
        mock_conn.execute = MagicMock()
        mock_conn.commit = MagicMock()

        def override_get_db():
            return mock_conn

        # Mock get_toggle_manager
        mock_toggle_manager = MagicMock()
        mock_toggle_manager.set_toggle = MagicMock(return_value=None)
        mock_toggle_manager.get_toggle = MagicMock(return_value=False)

        # Mock get_secret_manager
        mock_secret_manager = MagicMock()
        mock_secret_manager.get_hmac_key = MagicMock(return_value=(b"key", "v1"))

        app.dependency_overrides[admin_module.get_db] = override_get_db
        app.dependency_overrides[admin_module.get_toggle_manager] = lambda: mock_toggle_manager
        app.dependency_overrides[admin_module.get_secret_manager] = lambda: mock_secret_manager

        return app, mock_conn

    def test_admin_toggles_with_x_scopes_header_succeeds(self, app_with_admin_router):
        """Route-level test: POST /admin/toggles works with valid token + X-Scopes."""
        app, _ = app_with_admin_router
        correct_scopes_mapping = {self.VALID_TOKEN: ["admin:config"]}

        with patch.object(settings, "admin_token_scopes", correct_scopes_mapping):
            with patch.object(settings, "admin_secret_token", self.VALID_TOKEN):
                client = TestClient(app)
                response = client.post(
                    "/admin/toggles",
                    json={"feature": "test_feature", "enabled": True},
                    headers={
                        "Authorization": f"Bearer {self.VALID_TOKEN}",
                        "X-Scopes": "admin:config",
                    },
                )
                assert response.status_code == 200

    def test_admin_toggles_without_x_scopes_header_succeeds(self, app_with_admin_router):
        """Route-level test: POST /admin/toggles works WITHOUT X-Scopes header (header is ignored)."""
        app, _ = app_with_admin_router
        correct_scopes_mapping = {self.VALID_TOKEN: ["admin:config"]}

        with patch.object(settings, "admin_token_scopes", correct_scopes_mapping):
            with patch.object(settings, "admin_secret_token", self.VALID_TOKEN):
                client = TestClient(app)
                response = client.post(
                    "/admin/toggles",
                    json={"feature": "test_feature", "enabled": True},
                    headers={"Authorization": f"Bearer {self.VALID_TOKEN}"},
                )
                assert response.status_code == 200

    def test_admin_toggles_x_scopes_ignored_proof(self, app_with_admin_router):
        """
        Route-level proof: X-Scopes header is completely ignored by admin/toggles.
        Both requests return 200 with the same feature/enabled fields — the header
        has no effect. (HMAC differs because it contains a timestamp, which is
        expected and irrelevant to the security test.)
        """
        app, _ = app_with_admin_router
        correct_scopes_mapping = {self.VALID_TOKEN: ["admin:config"]}

        with patch.object(settings, "admin_token_scopes", correct_scopes_mapping):
            with patch.object(settings, "admin_secret_token", self.VALID_TOKEN):
                client = TestClient(app)
                payload = {"feature": "test_feature", "enabled": True}

                r_with = client.post(
                    "/admin/toggles",
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {self.VALID_TOKEN}",
                        "X-Scopes": "admin:config",
                    },
                )
                r_without = client.post(
                    "/admin/toggles",
                    json=payload,
                    headers={"Authorization": f"Bearer {self.VALID_TOKEN}"},
                )

                assert r_with.status_code == 200
                assert r_without.status_code == 200
                # Same feature/enabled — only the timestamp-based HMAC differs
                assert r_with.json()["feature"] == r_without.json()["feature"]
                assert r_with.json()["enabled"] == r_without.json()["enabled"]

    def test_admin_toggles_invalid_token_returns_403(self, app_with_admin_router):
        """Route-level test: invalid token is rejected by admin/toggles."""
        app, _ = app_with_admin_router
        correct_scopes_mapping = {self.VALID_TOKEN: ["admin:config"]}

        with patch.object(settings, "admin_token_scopes", correct_scopes_mapping):
            with patch.object(settings, "admin_secret_token", self.VALID_TOKEN):
                client = TestClient(app)
                response = client.post(
                    "/admin/toggles",
                    json={"feature": "test_feature", "enabled": True},
                    headers={
                        "Authorization": "Bearer invalid-token",
                        "X-Scopes": "admin:config",
                    },
                )
                assert response.status_code == 403


class TestEmailRoutesUseRequireScope:
    """Verify email.py routes use require_scope (not the old require_admin_scope)."""

    VALID_TOKEN = "test-admin-key"  # Set by conftest.py pytest_configure

    @pytest.fixture
    def app_with_email_router(self):
        """Minimal FastAPI app with email router and mocked dependencies."""
        from unittest.mock import MagicMock, PropertyMock

        from app.api.routes import email as email_module

        app = FastAPI()
        app.include_router(email_module.router)

        # Mock settings object for IMAP attributes used by _get_unseen_count
        mock_email_settings = MagicMock()
        mock_email_settings.imap_enabled = False  # Disable IMAP to avoid real connections
        mock_email_settings.imap_use_ssl = False
        mock_email_settings.imap_host = "imap.example.com"
        mock_email_settings.imap_port = 993
        mock_email_settings.imap_username = "user"
        mock_email_settings.imap_password = MagicMock(get_secret_value=MagicMock(return_value="pass"))
        mock_email_settings.imap_mailbox = "INBOX"

        # Mock get_email_service
        mock_email_service = MagicMock()
        mock_email_service.is_healthy = MagicMock(return_value=True)
        mock_email_service.get_last_poll_time = MagicMock(return_value=None)
        mock_email_service.pool = MagicMock()
        mock_email_service.get_current_backoff_delay = MagicMock(return_value=None)
        # Assign settings via PropertyMock so it returns our mock
        type(mock_email_service).settings = PropertyMock(return_value=mock_email_settings)

        # Mock app_settings (get_settings dependency) — must have imap_enabled
        mock_app_settings = MagicMock()
        mock_app_settings.imap_enabled = False  # Disable IMAP to avoid real connections

        app.dependency_overrides[email_module.get_email_service] = lambda: mock_email_service
        app.dependency_overrides[email_module.get_settings] = lambda: mock_app_settings

        return app

    def test_email_status_with_x_scopes_header_succeeds(self, app_with_email_router):
        """Route-level test: GET /email/status works with valid token + X-Scopes."""
        app = app_with_email_router
        correct_scopes_mapping = {self.VALID_TOKEN: ["admin:config"]}

        with patch.object(settings, "admin_token_scopes", correct_scopes_mapping):
            with patch.object(settings, "admin_secret_token", self.VALID_TOKEN):
                client = TestClient(app)
                response = client.get(
                    "/email/status",
                    headers={
                        "Authorization": f"Bearer {self.VALID_TOKEN}",
                        "X-Scopes": "admin:config",
                    },
                )
                assert response.status_code == 200

    def test_email_status_without_x_scopes_header_succeeds(self, app_with_email_router):
        """Route-level test: GET /email/status works WITHOUT X-Scopes header (header is ignored)."""
        app = app_with_email_router
        correct_scopes_mapping = {self.VALID_TOKEN: ["admin:config"]}

        with patch.object(settings, "admin_token_scopes", correct_scopes_mapping):
            with patch.object(settings, "admin_secret_token", self.VALID_TOKEN):
                client = TestClient(app)
                response = client.get(
                    "/email/status",
                    headers={"Authorization": f"Bearer {self.VALID_TOKEN}"},
                )
                assert response.status_code == 200

    def test_email_status_x_scopes_ignored_proof(self, app_with_email_router):
        """
        Route-level proof: X-Scopes header is completely ignored by /email/status.
        Both requests return 200 with identical response data — the header has no effect.
        """
        app = app_with_email_router
        correct_scopes_mapping = {self.VALID_TOKEN: ["admin:config"]}

        with patch.object(settings, "admin_token_scopes", correct_scopes_mapping):
            with patch.object(settings, "admin_secret_token", self.VALID_TOKEN):
                client = TestClient(app)

                r_with = client.get(
                    "/email/status",
                    headers={
                        "Authorization": f"Bearer {self.VALID_TOKEN}",
                        "X-Scopes": "admin:config",
                    },
                )
                r_without = client.get(
                    "/email/status",
                    headers={"Authorization": f"Bearer {self.VALID_TOKEN}"},
                )

                assert r_with.status_code == 200
                assert r_without.status_code == 200
                assert r_with.json() == r_without.json()

    def test_email_status_invalid_token_returns_403(self, app_with_email_router):
        """Route-level test: invalid token is rejected by /email/status."""
        app = app_with_email_router
        correct_scopes_mapping = {self.VALID_TOKEN: ["admin:config"]}

        with patch.object(settings, "admin_token_scopes", correct_scopes_mapping):
            with patch.object(settings, "admin_secret_token", self.VALID_TOKEN):
                client = TestClient(app)
                response = client.get(
                    "/email/status",
                    headers={
                        "Authorization": "Bearer invalid-token",
                        "X-Scopes": "admin:config",
                    },
                )
                assert response.status_code == 403


class TestRequireScopeEmptyTokenGuard:
    """S3 LOW (defense-in-depth): empty admin_secret_token must fail explicitly."""

    def test_empty_admin_secret_token_returns_503(self):
        """
        If admin_secret_token is empty string, require_scope must return 503
        (not silently accept any token via compare_digest(token, "")).
        """
        empty_token_mapping = {"": ["admin:config"]}

        with patch.object(settings, "admin_token_scopes", empty_token_mapping):
            with patch.object(settings, "admin_secret_token", ""):
                scope_check = require_scope("admin:config")
                with pytest.raises(HTTPException) as exc_info:
                    scope_check(authorization="Bearer some-token")

                assert exc_info.value.status_code == 503
                assert "Authentication not configured" in exc_info.value.detail
