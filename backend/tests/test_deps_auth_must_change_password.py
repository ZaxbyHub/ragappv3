"""
Tests for must_change_password enforcement in get_current_active_user (deps.py).

The must_change_password check (lines 262-269 in deps.py):
- User with must_change_password=True is blocked from all routes EXCEPT:
  - /auth/change-password
  - /auth/login
- Blocked requests receive 403 with detail="must_change_password"
- Users with must_change_password=False or missing flag are unaffected
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import jwt
import pytest
from fastapi import HTTPException

# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_settings_jwt_mode():
    """Mock settings for users_enabled=True (JWT auth mode)."""
    mock = MagicMock()
    mock.users_enabled = True
    mock.admin_secret_token = "test-token"
    mock.jwt_secret_key = "test-secret-key"
    mock.jwt_algorithm = "HS256"
    mock.sqlite_path = "./test.db"
    with patch("app.api.deps.settings", mock, create=True):
        with patch("app.services.auth_service.settings", mock, create=True):
            with patch("app.config.settings", mock, create=True):
                yield mock


@pytest.fixture
def mock_db():
    """Mock database connection."""
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.execute.return_value = mock_cursor
    return mock_conn, mock_cursor


def _make_token(user_id: int, secret: str = "test-secret-key") -> str:
    """Create a valid access token for the given user_id."""
    payload = {
        "sub": str(user_id),
        "username": "testuser",
        "role": "member",
        "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        "type": "access",
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def _mock_request(path: str, method: str = "GET") -> MagicMock:
    """Create a mock Request with a specific URL path and HTTP method."""
    mock_req = MagicMock()
    mock_req.url.path = path
    mock_req.method = method
    return mock_req


# ─────────────────────────────────────────────────────────────────────────────
# Test must_change_password route blocking enforcement
# ─────────────────────────────────────────────────────────────────────────────


class TestMustChangePasswordEnforcement:
    """Tests for must_change_password route-blocking enforcement."""

    @pytest.mark.asyncio
    async def test_must_change_password_allowed_on_me_route(
        self, mock_settings_jwt_mode, mock_db
    ):
        """
        User with must_change_password=1 accessing GET /api/auth/me → allowed.

        /auth/me is exempt: it is a read-only view of the caller's own identity
        (including the must_change_password flag), which the SPA needs to rehydrate
        the user on reload and route to the forced change-password screen instead of
        being logged out. (Previously this route returned 403; the exemption was
        added so the forced-change flow works end-to-end — issue #230.)
        """
        from app.api.deps import get_current_active_user

        token = _make_token(42)
        mock_conn, mock_cursor = mock_db
        mock_cursor.fetchone.return_value = (
            42, "testuser", "Test User", "member", 1, 1  # must_change_password=1
        )

        mock_request = _mock_request("/api/auth/me", method="GET")

        result = await get_current_active_user(
            request=mock_request,
            authorization=f"Bearer {token}",
            access_token=None,
            db=mock_conn,
        )

        assert result["id"] == 42
        assert result["must_change_password"] is True

    @pytest.mark.asyncio
    async def test_must_change_password_blocked_on_patch_me_route(
        self, mock_settings_jwt_mode, mock_db
    ):
        """
        User with must_change_password=1 doing PATCH /api/auth/me → 403.

        Only the read-only GET /auth/me is exempt. PATCH /auth/me (update_me)
        mutates the profile/password and must stay blocked so the forced
        change-password flow is the only way out.
        """
        from app.api.deps import get_current_active_user

        token = _make_token(42)
        mock_conn, mock_cursor = mock_db
        mock_cursor.fetchone.return_value = (
            42, "testuser", "Test User", "member", 1, 1
        )

        mock_request = _mock_request("/api/auth/me", method="PATCH")

        with pytest.raises(HTTPException) as exc_info:
            await get_current_active_user(
                request=mock_request,
                authorization=f"Bearer {token}",
                access_token=None,
                db=mock_conn,
            )

        assert exc_info.value.status_code == 403
        assert exc_info.value.detail == "must_change_password"

    @pytest.mark.asyncio
    async def test_must_change_password_allowed_on_change_password_route(
        self, mock_settings_jwt_mode, mock_db
    ):
        """
        User with must_change_password=1 accessing POST /auth/change-password → allowed.

        /auth/change-password is on the exempt list so it should not raise.
        """
        from app.api.deps import get_current_active_user

        token = _make_token(42)
        mock_conn, mock_cursor = mock_db
        mock_cursor.fetchone.return_value = (
            42, "testuser", "Test User", "member", 1, 1  # must_change_password=1
        )

        mock_request = _mock_request("/auth/change-password")

        result = await get_current_active_user(
            request=mock_request,
            authorization=f"Bearer {token}",
            access_token=None,
            db=mock_conn,
        )

        assert result["id"] == 42
        assert result["must_change_password"] is True

    @pytest.mark.asyncio
    async def test_must_change_password_allowed_on_prefixed_change_password_route(
        self, mock_settings_jwt_mode, mock_db
    ):
        """
        User with must_change_password=1 accessing the *real* runtime path
        POST /api/auth/change-password → allowed.

        Regression test for the deadlock: the auth router mounts under "/api",
        so request.url.path is "/api/auth/change-password" at runtime. An exact
        match against "/auth/change-password" never fired, permanently locking
        flagged users out of the only recovery endpoint. Suffix matching fixes it.
        """
        from app.api.deps import get_current_active_user

        token = _make_token(42)
        mock_conn, mock_cursor = mock_db
        mock_cursor.fetchone.return_value = (
            42, "testuser", "Test User", "member", 1, 1  # must_change_password=1
        )

        mock_request = _mock_request("/api/auth/change-password")

        result = await get_current_active_user(
            request=mock_request,
            authorization=f"Bearer {token}",
            access_token=None,
            db=mock_conn,
        )

        assert result["id"] == 42
        assert result["must_change_password"] is True

    @pytest.mark.asyncio
    async def test_must_change_password_allowed_on_login_route(
        self, mock_settings_jwt_mode, mock_db
    ):
        """
        User with must_change_password=1 accessing POST /auth/login → allowed.

        /auth/login is on the exempt list so it should not raise.
        Note: In normal flow, a flagged user wouldn't be re-logging in, but the
        route is exempt by design (the login route itself doesn't require the flag check
        to be passed in order to call it — the user needs to log in to then hit change-password).
        """
        from app.api.deps import get_current_active_user

        token = _make_token(42)
        mock_conn, mock_cursor = mock_db
        mock_cursor.fetchone.return_value = (
            42, "testuser", "Test User", "member", 1, 1  # must_change_password=1
        )

        mock_request = _mock_request("/auth/login")

        result = await get_current_active_user(
            request=mock_request,
            authorization=f"Bearer {token}",
            access_token=None,
            db=mock_conn,
        )

        assert result["id"] == 42
        assert result["must_change_password"] is True

    @pytest.mark.asyncio
    async def test_must_change_password_flag_off_accessing_me(
        self, mock_settings_jwt_mode, mock_db
    ):
        """
        User with must_change_password=0 (no flag) accessing /api/auth/me → 200, unaffected.

        Users without the flag should have no restrictions.
        """
        from app.api.deps import get_current_active_user

        token = _make_token(42)
        mock_conn, mock_cursor = mock_db
        mock_cursor.fetchone.return_value = (
            42, "testuser", "Test User", "member", 1, 0  # must_change_password=0
        )

        mock_request = _mock_request("/api/auth/me")

        result = await get_current_active_user(
            request=mock_request,
            authorization=f"Bearer {token}",
            access_token=None,
            db=mock_conn,
        )

        assert result["id"] == 42
        assert result["must_change_password"] is False

    @pytest.mark.asyncio
    async def test_superadmin_must_change_password_false_unaffected(
        self, mock_settings_jwt_mode, mock_db
    ):
        """
        Superadmin user with must_change_password=False → unaffected, normal access.

        Admin token fallback users always have must_change_password=False so this
        verifies superadmin path is not impacted by the flag check.
        """
        from app.api.deps import get_current_active_user

        token = _make_token(1)
        mock_conn, mock_cursor = mock_db
        # Simulate DB returning a superadmin user with must_change_password=False
        mock_cursor.fetchone.return_value = (
            1, "superadmin", "Super Admin", "superadmin", 1, 0
        )

        mock_request = _mock_request("/api/auth/me")

        result = await get_current_active_user(
            request=mock_request,
            authorization=f"Bearer {token}",
            access_token=None,
            db=mock_conn,
        )

        assert result["role"] == "superadmin"
        assert result["must_change_password"] is False

    @pytest.mark.asyncio
    async def test_must_change_password_blocked_on_vaults_list(
        self, mock_settings_jwt_mode, mock_db
    ):
        """
        User with must_change_password=1 accessing /vaults → 403.

        Tests that blocking is not limited to /auth/* routes but applies to any route.
        """
        from app.api.deps import get_current_active_user

        token = _make_token(42)
        mock_conn, mock_cursor = mock_db
        mock_cursor.fetchone.return_value = (
            42, "testuser", "Test User", "member", 1, 1
        )

        mock_request = _mock_request("/vaults")

        with pytest.raises(HTTPException) as exc_info:
            await get_current_active_user(
                request=mock_request,
                authorization=f"Bearer {token}",
                access_token=None,
                db=mock_conn,
            )

        assert exc_info.value.status_code == 403
        assert exc_info.value.detail == "must_change_password"

    @pytest.mark.asyncio
    async def test_must_change_password_blocked_on_auth_refresh(
        self, mock_settings_jwt_mode, mock_db
    ):
        """
        User with must_change_password=1 accessing /auth/refresh → 403.

        /auth/refresh is NOT on the exempt list, so flagged users must be blocked.
        """
        from app.api.deps import get_current_active_user

        token = _make_token(42)
        mock_conn, mock_cursor = mock_db
        mock_cursor.fetchone.return_value = (
            42, "testuser", "Test User", "member", 1, 1
        )

        mock_request = _mock_request("/auth/refresh")

        with pytest.raises(HTTPException) as exc_info:
            await get_current_active_user(
                request=mock_request,
                authorization=f"Bearer {token}",
                access_token=None,
                db=mock_conn,
            )

        assert exc_info.value.status_code == 403
        assert exc_info.value.detail == "must_change_password"

    @pytest.mark.asyncio
    async def test_must_change_password_blocked_on_change_password_subpath(
        self, mock_settings_jwt_mode, mock_db
    ):
        """
        User with must_change_password=1 accessing /auth/change-password/confirm → 403.

        The exempt check uses exact path suffix matching (endswith), so subpaths
        like /auth/change-password/confirm are NOT exempt and must be blocked.
        """
        from app.api.deps import get_current_active_user

        token = _make_token(42)
        mock_conn, mock_cursor = mock_db
        mock_cursor.fetchone.return_value = (
            42, "testuser", "Test User", "member", 1, 1
        )

        mock_request = _mock_request("/auth/change-password/confirm")

        with pytest.raises(HTTPException) as exc_info:
            await get_current_active_user(
                request=mock_request,
                authorization=f"Bearer {token}",
                access_token=None,
                db=mock_conn,
            )

        assert exc_info.value.status_code == 403
        assert exc_info.value.detail == "must_change_password"

    @pytest.mark.asyncio
    async def test_must_change_password_null_treated_as_false(
        self, mock_settings_jwt_mode, mock_db
    ):
        """
        User with must_change_password=NULL in DB → treated as False, no blocking.

        The code uses: bool(row[5]) if len(row) > 5 and row[5] is not None else False
        So NULL/missing column value should default to False (no blocking).
        """
        from app.api.deps import get_current_active_user

        token = _make_token(42)
        mock_conn, mock_cursor = mock_db
        mock_cursor.fetchone.return_value = (
            42, "testuser", "Test User", "member", 1, None
        )

        mock_request = _mock_request("/api/auth/me")

        result = await get_current_active_user(
            request=mock_request,
            authorization=f"Bearer {token}",
            access_token=None,
            db=mock_conn,
        )

        assert result["must_change_password"] is False
        # No exception should be raised

    @pytest.mark.asyncio
    async def test_must_change_password_flagged_with_admin_role_blocked(
        self, mock_settings_jwt_mode, mock_db
    ):
        """
        User with must_change_password=1 AND role=admin accessing a non-exempt
        route (/vaults) → 403.

        The flag applies regardless of role — even admins must change password on
        first login. (Uses /vaults rather than /auth/me, which is now exempt — see
        issue #230.)
        """
        from app.api.deps import get_current_active_user

        token = _make_token(99)
        mock_conn, mock_cursor = mock_db
        mock_cursor.fetchone.return_value = (
            99, "adminuser", "Admin User", "admin", 1, 1  # admin + flagged
        )

        mock_request = _mock_request("/vaults")

        with pytest.raises(HTTPException) as exc_info:
            await get_current_active_user(
                request=mock_request,
                authorization=f"Bearer {token}",
                access_token=None,
                db=mock_conn,
            )

        assert exc_info.value.status_code == 403
        assert exc_info.value.detail == "must_change_password"

    @pytest.mark.asyncio
    async def test_must_change_password_cookie_auth_blocked_on_protected_route(
        self, mock_settings_jwt_mode, mock_db
    ):
        """
        User with must_change_password=1 authenticating via cookie accessing a
        non-exempt route (/vaults) → 403.

        The must_change_password check must work for cookie-based auth too. (Uses
        /vaults rather than /auth/me, which is now exempt — see issue #230.)
        """
        from app.api.deps import get_current_active_user

        token = _make_token(42)
        mock_conn, mock_cursor = mock_db
        mock_cursor.fetchone.return_value = (
            42, "testuser", "Test User", "member", 1, 1
        )

        mock_request = _mock_request("/vaults")

        with pytest.raises(HTTPException) as exc_info:
            await get_current_active_user(
                request=mock_request,
                authorization=None,
                access_token=token,
                db=mock_conn,
            )

        assert exc_info.value.status_code == 403
        assert exc_info.value.detail == "must_change_password"

    @pytest.mark.asyncio
    async def test_must_change_password_cookie_auth_exempt_route_allowed(
        self, mock_settings_jwt_mode, mock_db
    ):
        """
        User with must_change_password=1 authenticating via cookie on /auth/change-password → allowed.
        """
        from app.api.deps import get_current_active_user

        token = _make_token(42)
        mock_conn, mock_cursor = mock_db
        mock_cursor.fetchone.return_value = (
            42, "testuser", "Test User", "member", 1, 1
        )

        mock_request = _mock_request("/auth/change-password")

        result = await get_current_active_user(
            request=mock_request,
            authorization=None,
            access_token=token,
            db=mock_conn,
        )

        assert result["id"] == 42

    @pytest.mark.asyncio
    async def test_must_change_password_blocked_on_users_route(
        self, mock_settings_jwt_mode, mock_db
    ):
        """
        User with must_change_password=1 AND role=admin accessing GET /api/users → 403.

        Regression test for F-001: /api/users was in the exempt_paths set, allowing
        flagged admin users to bypass must_change_password enforcement and access
        user management routes (create/list users).
        """
        from app.api.deps import get_current_active_user

        token = _make_token(99)
        mock_conn, mock_cursor = mock_db
        mock_cursor.fetchone.return_value = (
            99, "adminuser", "Admin User", "admin", 1, 1  # admin + flagged
        )

        mock_request = _mock_request("/api/users")

        with pytest.raises(HTTPException) as exc_info:
            await get_current_active_user(
                request=mock_request,
                authorization=f"Bearer {token}",
                access_token=None,
                db=mock_conn,
            )

        assert exc_info.value.status_code == 403
        assert exc_info.value.detail == "must_change_password"
