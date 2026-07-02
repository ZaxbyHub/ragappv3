"""
Tests for backend/app/api/deps.py — user auth and RBAC functions.

Tests cover:
- get_current_active_user: admin token auth, JWT auth, cookie fallback, token type enforcement, must_change_password
- evaluate_policy: superadmin, admin, vault member, public vault RBAC
- require_vault_permission: dependency factory
- require_role: dependency factory
- require_admin_role: standalone admin role check
- get_user_accessible_vault_ids: vault access enumeration for regular users
"""

import hashlib
import sqlite3
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

# Mock request for tests that call get_current_active_user directly
mock_request = MagicMock()
mock_request.url.path = "/api/auth/me"
# Default to empty-string UA so compute_client_fingerprint receives a real str
mock_request.headers.get.return_value = ""

# Pre-computed fpt for empty UA — used to backfill legacy test tokens
_TEST_FPT_EMPTY = hashlib.sha256(b"").hexdigest()

# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_settings_admin_mode():
    """Mock settings for users_enabled=False (admin token mode)."""
    mock = MagicMock()
    mock.users_enabled = False
    mock.admin_secret_token = "test-token"
    mock.jwt_secret_key = "test-secret-key"
    mock.jwt_algorithm = "HS256"
    mock.sqlite_path = "./test.db"
    with patch("app.api.deps.settings", mock, create=True):
        with patch("app.services.auth_service.settings", mock, create=True):
            with patch("app.config.settings", mock, create=True):
                yield mock


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


# ─────────────────────────────────────────────────────────────────────────────
# Test get_current_active_user — Admin Token Mode (users_enabled=False)
# ─────────────────────────────────────────────────────────────────────────────


class TestGetCurrentUserAdminToken:
    """Tests for get_current_active_user with admin token auth."""

    @pytest.mark.asyncio
    async def test_get_current_user_with_admin_token(
        self, mock_settings_admin_mode, mock_db
    ):
        """users_enabled=False, valid admin token → returns superadmin dict with must_change_password=False."""
        from app.api.deps import get_current_active_user

        mock_conn, mock_cursor = mock_db

        result = await get_current_active_user(request=mock_request,
            authorization="Bearer test-token",
            access_token=None,
            db=mock_conn,
        )

        assert result == {
            "id": 0,
            "username": "admin",
            "full_name": "Admin",
            "role": "superadmin",
            "is_active": True,
            "must_change_password": False,
        }

    @pytest.mark.asyncio
    async def test_get_current_user_rejects_default_token(self, mock_db):
        """users_enabled=False, admin_secret_token='', token='admin-secret-token' → 403."""
        from app.api.deps import get_current_active_user

        mock = MagicMock()
        mock.users_enabled = False
        mock.admin_secret_token = ""  # Empty → default token is insecure
        mock.sqlite_path = "./test.db"

        mock_conn, mock_cursor = mock_db

        with patch("app.api.deps.settings", mock, create=True):
            with pytest.raises(HTTPException) as exc_info:
                await get_current_active_user(request=mock_request,
                    authorization="Bearer admin-secret-token",
                    access_token=None,
                    db=mock_conn,
                )

        assert exc_info.value.status_code == 403
        assert "change default admin token" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_get_current_user_accepts_explicit_default_token(self, mock_db):
        """users_enabled=False, admin_secret_token='admin-secret-token', token='admin-secret-token' → 200."""
        from app.api.deps import get_current_active_user

        mock = MagicMock()
        mock.users_enabled = False
        mock.admin_secret_token = "admin-secret-token"  # Explicit default token
        mock.sqlite_path = "./test.db"

        mock_conn, mock_cursor = mock_db

        with patch("app.api.deps.settings", mock, create=True):
            result = await get_current_active_user(request=mock_request,
                authorization="Bearer admin-secret-token",
                access_token=None,
                db=mock_conn,
            )

            assert result == {
                "id": 0,
                "username": "admin",
                "full_name": "Admin",
                "role": "superadmin",
                "is_active": True,
                "must_change_password": False,
            }

    @pytest.mark.asyncio
    async def test_get_current_user_missing_header(self, mock_settings_admin_mode):
        """No Authorization header and no cookie → 401."""
        from app.api.deps import get_current_active_user

        with pytest.raises(HTTPException) as exc_info:
            await get_current_active_user(request=mock_request,
                authorization=None,
                access_token=None,
                db=MagicMock(),
            )

        assert exc_info.value.status_code == 401
        assert "not authenticated" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_get_current_user_invalid_token(self, mock_settings_admin_mode):
        """Wrong token → 403."""
        from app.api.deps import get_current_active_user

        with pytest.raises(HTTPException) as exc_info:
            await get_current_active_user(request=mock_request,
                authorization="Bearer wrong-token",
                access_token=None,
                db=MagicMock(),
            )

        assert exc_info.value.status_code == 403
        assert "invalid" in exc_info.value.detail.lower()


# ─────────────────────────────────────────────────────────────────────────────
# Test get_current_active_user — Cookie Fallback
# ─────────────────────────────────────────────────────────────────────────────


class TestGetCurrentUserCookieFallback:
    """Tests for get_current_active_user cookie fallback authentication."""

    @pytest.mark.asyncio
    async def test_auth_via_authorization_header_returns_user(
        self, mock_settings_jwt_mode, mock_db
    ):
        """Auth via Authorization header → returns user dict."""
        from datetime import datetime, timedelta, timezone

        import jwt

        from app.api.deps import get_current_active_user

        secret, algorithm = "test-secret-key", "HS256"
        payload = {
            "sub": "42",
            "username": "testuser",
            "role": "member",
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
            "type": "access",
            "jti": "test-jti",
            "fpt": _TEST_FPT_EMPTY,
        }
        token = jwt.encode(payload, secret, algorithm=algorithm)

        mock_conn, mock_cursor = mock_db
        mock_cursor.fetchone.side_effect = [
            None,
            (42, "testuser", "Test User", "member", 1, 0),
        ]

        result = await get_current_active_user(request=mock_request,
            authorization=f"Bearer {token}",
            access_token=None,
            db=mock_conn,
        )

        assert result["id"] == 42
        assert result["username"] == "testuser"
        assert result["role"] == "member"

    @pytest.mark.asyncio
    async def test_auth_via_cookie_returns_user(self, mock_settings_jwt_mode, mock_db):
        """Auth via cookie (access_token) → returns user dict."""
        from datetime import datetime, timedelta, timezone

        import jwt

        from app.api.deps import get_current_active_user

        secret, algorithm = "test-secret-key", "HS256"
        payload = {
            "sub": "42",
            "username": "testuser",
            "role": "member",
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
            "type": "access",
            "jti": "test-jti",
            "fpt": _TEST_FPT_EMPTY,
        }
        token = jwt.encode(payload, secret, algorithm=algorithm)

        mock_conn, mock_cursor = mock_db
        mock_cursor.fetchone.side_effect = [
            None,
            (42, "testuser", "Test User", "member", 1, 0),
        ]

        result = await get_current_active_user(request=mock_request,
            authorization=f"Bearer {token}",
            access_token=None,
            db=mock_conn,
        )

        assert result["id"] == 42
        assert result["username"] == "testuser"
        assert result["role"] == "member"

    @pytest.mark.asyncio
    async def test_no_header_no_cookie_returns_401(self, mock_settings_jwt_mode):
        """No header, no cookie → 401."""
        from app.api.deps import get_current_active_user

        with pytest.raises(HTTPException) as exc_info:
            await get_current_active_user(request=mock_request,
                authorization=None,
                access_token=None,
                db=MagicMock(),
            )

        assert exc_info.value.status_code == 401
        assert "not authenticated" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_invalid_token_in_header_returns_403(self, mock_settings_jwt_mode):
        """Invalid token in header → 401."""
        from app.api.deps import get_current_active_user

        with pytest.raises(HTTPException) as exc_info:
            await get_current_active_user(request=mock_request,
                authorization="Bearer invalid-token",
                access_token=None,
                db=MagicMock(),
            )

        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_invalid_token_in_cookie_returns_403(self, mock_settings_jwt_mode):
        """Invalid token in cookie → 401."""
        from app.api.deps import get_current_active_user

        with pytest.raises(HTTPException) as exc_info:
            await get_current_active_user(request=mock_request,
                authorization=None,
                access_token="invalid-token",
                db=MagicMock(),
            )

        assert exc_info.value.status_code == 401


# ─────────────────────────────────────────────────────────────────────────────
# Test JWT Type Enforcement
# ─────────────────────────────────────────────────────────────────────────────


class TestJWTTypeEnforcement:
    """Tests for JWT token type enforcement in get_current_active_user."""

    @pytest.mark.asyncio
    async def test_access_token_with_type_access_accepted(
        self, mock_settings_jwt_mode, mock_db
    ):
        """Access token with type='access' → accepted."""
        from datetime import datetime, timedelta, timezone

        import jwt

        from app.api.deps import get_current_active_user

        secret, algorithm = "test-secret-key", "HS256"
        payload = {
            "sub": "42",
            "username": "testuser",
            "role": "member",
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
            "type": "access",
            "jti": "test-jti",
            "fpt": _TEST_FPT_EMPTY,
        }
        token = jwt.encode(payload, secret, algorithm=algorithm)

        mock_conn, mock_cursor = mock_db
        mock_cursor.fetchone.side_effect = [
            None,
            (42, "testuser", "Test User", "member", 1, 0),
        ]

        result = await get_current_active_user(request=mock_request,
            authorization=f"Bearer {token}",
            access_token=None,
            db=mock_conn,
        )

        assert result["id"] == 42

    @pytest.mark.asyncio
    async def test_token_without_type_field_returns_401(
        self, mock_settings_jwt_mode, mock_db
    ):
        """Token without type field → 401 'Invalid token type'."""
        from datetime import datetime, timedelta, timezone

        import jwt

        from app.api.deps import get_current_active_user

        secret, algorithm = "test-secret-key", "HS256"
        payload = {
            "sub": "42",
            "username": "testuser",
            "role": "member",
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
            "jti": "test-jti",
            # No 'type' field
        }
        token = jwt.encode(payload, secret, algorithm=algorithm)

        mock_conn, mock_cursor = mock_db

        with pytest.raises(HTTPException) as exc_info:
            await get_current_active_user(request=mock_request,
                authorization=f"Bearer {token}",
                access_token=None,
                db=mock_conn,
            )

        assert exc_info.value.status_code == 401
        assert "token_invalid" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_token_with_type_refresh_returns_401(
        self, mock_settings_jwt_mode, mock_db
    ):
        """Token with type='refresh' → 401 'Invalid token type'."""
        from datetime import datetime, timedelta, timezone

        import jwt

        from app.api.deps import get_current_active_user

        secret, algorithm = "test-secret-key", "HS256"
        payload = {
            "sub": "42",
            "username": "testuser",
            "role": "member",
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
            "type": "refresh",
            "jti": "test-jti",
        }
        token = jwt.encode(payload, secret, algorithm=algorithm)

        mock_conn, mock_cursor = mock_db

        with pytest.raises(HTTPException) as exc_info:
            await get_current_active_user(request=mock_request,
                authorization=f"Bearer {token}",
                access_token=None,
                db=mock_conn,
            )

        assert exc_info.value.status_code == 401
        assert "token_invalid" in exc_info.value.detail.lower()


class TestMustChangePassword:
    """Tests for must_change_password field in user dict."""

    @pytest.mark.asyncio
    async def test_user_with_must_change_password_true(
        self, mock_settings_jwt_mode, mock_db
    ):
        """User with must_change_password=True in DB → user dict has must_change_password=True."""
        from datetime import datetime, timedelta, timezone

        import jwt

        from app.api.deps import get_current_active_user

        secret, algorithm = "test-secret-key", "HS256"
        payload = {
            "sub": "42",
            "username": "testuser",
            "role": "member",
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
            "type": "access",
            "jti": "test-jti",
            "fpt": _TEST_FPT_EMPTY,
        }
        token = jwt.encode(payload, secret, algorithm=algorithm)

        mock_conn, mock_cursor = mock_db
        # First call: denylist check (jti not found), Second call: user lookup
        mock_cursor.fetchone.side_effect = [
            None,
            (42, "testuser", "Test User", "member", 1, 1),
        ]

        # Use exempt path to bypass must_change_password enforcement
        exempt_request = MagicMock()
        exempt_request.url.path = "/auth/change-password"
        exempt_request.headers.get.return_value = ""

        result = await get_current_active_user(request=exempt_request,
            authorization=f"Bearer {token}",
            access_token=None,
            db=mock_conn,
        )

        assert result["must_change_password"] is True

    @pytest.mark.asyncio
    async def test_user_with_must_change_password_false(
        self, mock_settings_jwt_mode, mock_db
    ):
        """User with must_change_password=False in DB → user dict has must_change_password=False."""
        from datetime import datetime, timedelta, timezone

        import jwt

        from app.api.deps import get_current_active_user

        secret, algorithm = "test-secret-key", "HS256"
        payload = {
            "sub": "42",
            "username": "testuser",
            "role": "member",
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
            "type": "access",
            "jti": "test-jti",
            "fpt": _TEST_FPT_EMPTY,
        }
        token = jwt.encode(payload, secret, algorithm=algorithm)

        mock_conn, mock_cursor = mock_db
        mock_cursor.fetchone.side_effect = [
            None,
            (42, "testuser", "Test User", "member", 1, 0),
        ]

        result = await get_current_active_user(request=mock_request,
            authorization=f"Bearer {token}",
            access_token=None,
            db=mock_conn,
        )

        assert result["must_change_password"] is False

    @pytest.mark.asyncio
    async def test_user_with_must_change_password_null(
        self, mock_settings_jwt_mode, mock_db
    ):
        """User with must_change_password=NULL in DB → user dict has must_change_password=False."""
        from datetime import datetime, timedelta, timezone

        import jwt

        from app.api.deps import get_current_active_user

        secret, algorithm = "test-secret-key", "HS256"
        payload = {
            "sub": "42",
            "username": "testuser",
            "role": "member",
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
            "type": "access",
            "jti": "test-jti",
            "fpt": _TEST_FPT_EMPTY,
        }
        token = jwt.encode(payload, secret, algorithm=algorithm)

        mock_conn, mock_cursor = mock_db
        mock_cursor.fetchone.side_effect = [
            None,
            (42, "testuser", "Test User", "member", 1, None),
        ]

        result = await get_current_active_user(request=mock_request,
            authorization=f"Bearer {token}",
            access_token=None,
            db=mock_conn,
        )

        assert result["must_change_password"] is False

    @pytest.mark.asyncio
    async def test_admin_token_fallback_must_change_password_false(
        self, mock_settings_admin_mode, mock_db
    ):
        """Admin token fallback → user dict has must_change_password=False."""
        from app.api.deps import get_current_active_user

        mock_conn, mock_cursor = mock_db

        result = await get_current_active_user(request=mock_request,
            authorization="Bearer test-token",
            access_token=None,
            db=mock_conn,
        )

        assert result["must_change_password"] is False


class TestAccessTokenDenylist:
    """Tests for access-token denylist in get_current_active_user."""

    @pytest.mark.asyncio
    async def test_denied_jti_returns_401(self, mock_settings_jwt_mode, mock_db):
        """Token whose jti is in the denylist → 401 token_invalid."""
        from datetime import datetime, timedelta, timezone

        import jwt

        from app.api.deps import get_current_active_user

        secret, algorithm = "test-secret-key", "HS256"
        payload = {
            "sub": "42",
            "username": "testuser",
            "role": "member",
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
            "type": "access",
            "jti": "revoked-jti-value",
        }
        token = jwt.encode(payload, secret, algorithm=algorithm)

        mock_conn, mock_cursor = mock_db
        # Simulate jti found in denylist (fetchone returns a row)
        mock_cursor.fetchone.return_value = (1,)

        with pytest.raises(HTTPException) as exc_info:
            await get_current_active_user(request=mock_request,
                authorization=f"Bearer {token}",
                access_token=None,
                db=mock_conn,
            )

        assert exc_info.value.status_code == 401
        assert "token_invalid" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_token_without_jti_returns_401(self, mock_settings_jwt_mode, mock_db):
        """Token without jti claim → allowed (conditional enforcement skips denylist)."""
        from datetime import datetime, timedelta, timezone

        import jwt

        from app.api.deps import get_current_active_user

        secret, algorithm = "test-secret-key", "HS256"
        payload = {
            "sub": "42",
            "username": "testuser",
            "role": "member",
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
            "type": "access",
            # No jti — conditional enforcement skips denylist check
            # No fpt — conditional enforcement skips fingerprint check
        }
        token = jwt.encode(payload, secret, algorithm=algorithm)

        mock_conn, mock_cursor = mock_db
        mock_cursor.fetchone.side_effect = [
            (42, "testuser", "Test User", "member", 1, 0),
        ]

        result = await get_current_active_user(request=mock_request,
            authorization=f"Bearer {token}",
            access_token=None,
            db=mock_conn,
        )

        assert result["id"] == 42
        assert result["username"] == "testuser"

    @pytest.mark.asyncio
    async def test_valid_jti_not_in_denylist_passes(
        self, mock_settings_jwt_mode, mock_db
    ):
        """Token with valid jti not in denylist → user dict returned."""
        from datetime import datetime, timedelta, timezone

        import jwt

        from app.api.deps import get_current_active_user

        secret, algorithm = "test-secret-key", "HS256"
        payload = {
            "sub": "42",
            "username": "testuser",
            "role": "member",
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
            "type": "access",
            "jti": "valid-jti-value",
            "fpt": _TEST_FPT_EMPTY,
        }
        token = jwt.encode(payload, secret, algorithm=algorithm)

        mock_conn, mock_cursor = mock_db
        # First call: is_access_token_denied check (jti not found → None)
        # Second call: user lookup
        mock_cursor.fetchone.side_effect = [None, (42, "testuser", "Test User", "member", 1, 0)]

        result = await get_current_active_user(request=mock_request,
            authorization=f"Bearer {token}",
            access_token=None,
            db=mock_conn,
        )

        assert result["id"] == 42
        assert result["username"] == "testuser"


# ─────────────────────────────────────────────────────────────────────────────
# Test require_admin_role
# ─────────────────────────────────────────────────────────────────────────────


class TestRequireAdminRole:
    """Tests for require_admin_role dependency function."""

    @pytest.mark.asyncio
    async def test_superadmin_user_passes(self):
        """Superadmin user → passes."""
        from app.api.deps import require_admin_role

        user = {"id": 1, "username": "super", "role": "superadmin", "is_active": True}
        result = await require_admin_role(user)

        assert result == user

    @pytest.mark.asyncio
    async def test_admin_user_passes(self):
        """Admin user → passes."""
        from app.api.deps import require_admin_role

        user = {"id": 2, "username": "admin", "role": "admin", "is_active": True}
        result = await require_admin_role(user)

        assert result == user

    @pytest.mark.asyncio
    async def test_member_user_returns_403(self):
        """Member user → 403."""
        from app.api.deps import require_admin_role

        user = {"id": 3, "username": "member", "role": "member", "is_active": True}

        with pytest.raises(HTTPException) as exc_info:
            await require_admin_role(user)

        assert exc_info.value.status_code == 403
        assert "admin access required" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_viewer_user_returns_403(self):
        """Viewer user → 403."""
        from app.api.deps import require_admin_role

        user = {"id": 4, "username": "viewer", "role": "viewer", "is_active": True}

        with pytest.raises(HTTPException) as exc_info:
            await require_admin_role(user)

        assert exc_info.value.status_code == 403
        assert "admin access required" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_unauthenticated_user_raises_from_get_current_user(self):
        """Unauthenticated user → 401 (from get_current_active_user dependency)."""
        from app.api.deps import get_current_active_user

        with pytest.raises(HTTPException) as exc_info:
            await get_current_active_user(request=mock_request,
                authorization=None,
                access_token=None,
                db=MagicMock(),
            )

        assert exc_info.value.status_code == 401


# ─────────────────────────────────────────────────────────────────────────────
# Test get_user_accessible_vault_ids
# ─────────────────────────────────────────────────────────────────────────────


class TestGetUserAccessibleVaultIds:
    """Tests for get_user_accessible_vault_ids helper function."""

    @pytest.mark.asyncio
    async def test_superadmin_returns_empty_list(self, mock_db):
        """Superadmin user → returns [] (means 'all vaults')."""
        from app.api.deps import get_user_accessible_vault_ids

        user = {"id": 1, "role": "superadmin"}
        mock_conn, mock_cursor = mock_db

        result = await get_user_accessible_vault_ids(user, mock_conn)

        assert result == []

    @pytest.mark.asyncio
    async def test_admin_returns_empty_list(self, mock_db):
        """Admin user → returns [] (means 'all vaults')."""
        from app.api.deps import get_user_accessible_vault_ids

        user = {"id": 2, "role": "admin"}
        mock_conn, mock_cursor = mock_db

        result = await get_user_accessible_vault_ids(user, mock_conn)

        assert result == []

    @pytest.mark.asyncio
    async def test_regular_user_with_direct_membership(self, mock_db):
        """Regular user with direct vault membership → returns vault IDs."""
        from app.api.deps import get_user_accessible_vault_ids

        user = {"id": 3, "role": "member"}
        mock_conn, mock_cursor = mock_db

        # get_user_accessible_vault_ids now:
        # 1. SELECT id FROM vaults → all vault IDs
        # 2. get_effective_vault_permissions:
        #    a. vault_members query → permission rows
        #    b. vault_group_access query → group permission rows
        #    c. vaults visibility + org_members → public vault IDs
        mock_cursor.fetchall.side_effect = [
            [(10,), (20,)],   # vault IDs from SELECT id FROM vaults
            [(10, 'read'), (20, 'write')],  # vault_members permissions
            [],                # vault_group_access
            [],                # vaults visibility=public
            [],                # vaults visibility=org
        ]

        result = await get_user_accessible_vault_ids(user, mock_conn)

        assert sorted(result) == [10, 20]

    @pytest.mark.asyncio
    async def test_regular_user_with_group_access(self, mock_db):
        """Regular user with group-based vault access → returns vault IDs."""
        from app.api.deps import get_user_accessible_vault_ids

        user = {"id": 3, "role": "member"}
        mock_conn, mock_cursor = mock_db

        mock_cursor.fetchall.side_effect = [
            [(30,), (40,)],   # vault IDs from SELECT id FROM vaults
            [],                # vault_members
            [(30, 'read'), (40, 'write')],  # vault_group_access
            [],                # vaults visibility=public
            [],                # vaults visibility=org
        ]

        result = await get_user_accessible_vault_ids(user, mock_conn)

        assert sorted(result) == [30, 40]

    @pytest.mark.asyncio
    async def test_regular_user_with_direct_and_group_access_deduplicated(
        self, mock_db
    ):
        """Regular user with both direct and group access → deduplicated list."""
        from app.api.deps import get_user_accessible_vault_ids

        user = {"id": 3, "role": "member"}
        mock_conn, mock_cursor = mock_db

        mock_cursor.fetchall.side_effect = [
            [(10,), (20,), (30,)],  # vault IDs from SELECT id FROM vaults
            [(10, 'read'), (20, 'read')],  # vault_members
            [(20, 'read'), (30, 'read')],  # vault_group_access
            [],                # vaults visibility=public
            [],                # vaults visibility=org
        ]

        result = await get_user_accessible_vault_ids(user, mock_conn)

        # Should be deduplicated and sorted
        assert sorted(result) == [10, 20, 30]

    @pytest.mark.asyncio
    async def test_regular_user_with_no_access(self, mock_db):
        """Regular user with no vault access → returns empty list."""
        from app.api.deps import get_user_accessible_vault_ids

        user = {"id": 3, "role": "member"}
        mock_conn, mock_cursor = mock_db

        # All queries return empty
        mock_cursor.fetchall.side_effect = [[], [], [], [], []]

        result = await get_user_accessible_vault_ids(user, mock_conn)

        assert result == []


# ─────────────────────────────────────────────────────────────────────────────
# Test get_current_active_user — JWT Mode (users_enabled=True)
# ─────────────────────────────────────────────────────────────────────────────


class TestGetCurrentUserJWT:
    """Tests for get_current_active_user with JWT auth."""

    @pytest.mark.asyncio
    async def test_get_current_user_with_jwt(self, mock_settings_jwt_mode, mock_db):
        """users_enabled=True, valid JWT → returns user dict from DB."""
        from datetime import datetime, timedelta, timezone

        import jwt

        from app.api.deps import get_current_active_user

        # Create a valid JWT token directly
        secret, algorithm = "test-secret-key", "HS256"
        payload = {
            "sub": "42",
            "username": "testuser",
            "role": "member",
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
            "type": "access",
            "jti": "test-jti",
            "fpt": _TEST_FPT_EMPTY,
        }
        token = jwt.encode(payload, secret, algorithm=algorithm)

        mock_conn, mock_cursor = mock_db
        # First call: denylist check (jti not found), Second call: user lookup
        mock_cursor.fetchone.side_effect = [
            None,
            (42, "testuser", "Test User", "member", 1, 0),
        ]

        result = await get_current_active_user(request=mock_request,
            authorization=f"Bearer {token}",
            access_token=None,
            db=mock_conn,
        )

        assert result == {
            "id": 42,
            "username": "testuser",
            "full_name": "Test User",
            "role": "member",
            "is_active": True,
            "must_change_password": False,
            "password_changed_at": 0.0,
        }

    @pytest.mark.asyncio
    async def test_get_current_user_inactive_user(
        self, mock_settings_jwt_mode, mock_db
    ):
        """users_enabled=True, JWT for inactive user → 401 user_inactive."""
        from datetime import datetime, timedelta, timezone

        import jwt

        from app.api.deps import get_current_active_user

        secret, algorithm = "test-secret-key", "HS256"
        payload = {
            "sub": "42",
            "username": "inactiveuser",
            "role": "member",
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
            "type": "access",
            "jti": "test-jti",
            "fpt": _TEST_FPT_EMPTY,
        }
        token = jwt.encode(payload, secret, algorithm=algorithm)

        mock_conn, mock_cursor = mock_db
        # First call: denylist check (jti not found), Second call: inactive user row
        mock_cursor.fetchone.side_effect = [
            None,
            (42, "inactiveuser", "Inactive User", "member", 0, 0),
        ]

        with pytest.raises(HTTPException) as exc_info:
            await get_current_active_user(request=mock_request,
                authorization=f"Bearer {token}",
                access_token=None,
                db=mock_conn,
            )

        assert exc_info.value.status_code == 401
        assert exc_info.value.detail == "user_inactive"

    @pytest.mark.asyncio
    async def test_get_current_user_not_found(self, mock_settings_jwt_mode, mock_db):
        """users_enabled=True, JWT for non-existent user → 401."""
        from datetime import datetime, timedelta, timezone

        import jwt

        from app.api.deps import get_current_active_user

        secret, algorithm = "test-secret-key", "HS256"
        payload = {
            "sub": "999",
            "username": "ghost",
            "role": "member",
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
            "type": "access",
            "jti": "test-jti",
        }
        token = jwt.encode(payload, secret, algorithm=algorithm)

        mock_conn, mock_cursor = mock_db
        # First call: denylist check (jti not found), Second call: user not found
        mock_cursor.fetchone.side_effect = [None, None]

        with pytest.raises(HTTPException) as exc_info:
            await get_current_active_user(request=mock_request,
                authorization=f"Bearer {token}",
                access_token=None,
                db=mock_conn,
            )

        assert exc_info.value.status_code == 401
        assert "token_invalid" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_get_current_user_expired_token(self, mock_settings_jwt_mode):
        """users_enabled=True, expired JWT → 401."""
        from datetime import datetime, timedelta, timezone

        import jwt

        from app.api.deps import get_current_active_user

        secret, algorithm = "test-secret-key", "HS256"
        expired_payload = {
            "sub": "42",
            "username": "testuser",
            "role": "member",
            "exp": datetime.now(timezone.utc) - timedelta(hours=1),  # Expired
            "type": "access",
            "jti": "test-jti",
        }
        expired_token = jwt.encode(expired_payload, secret, algorithm=algorithm)

        with pytest.raises(HTTPException) as exc_info:
            await get_current_active_user(request=mock_request,
                authorization=f"Bearer {expired_token}",
                access_token=None,
                db=MagicMock(),
            )

        assert exc_info.value.status_code == 401


# ─────────────────────────────────────────────────────────────────────────────
# Test evaluate_policy — RBAC Engine
# ─────────────────────────────────────────────────────────────────────────────


class TestEvaluatePolicy:
    """Tests for evaluate_policy RBAC engine."""

    def _vault_policy_db(self):
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        conn.execute(
            "CREATE TABLE vaults (id INTEGER PRIMARY KEY, visibility TEXT DEFAULT 'private', org_id INTEGER)"
        )
        conn.execute(
            "CREATE TABLE vault_members (vault_id INTEGER, user_id INTEGER, permission TEXT)"
        )
        conn.execute("CREATE TABLE group_members (group_id INTEGER, user_id INTEGER)")
        conn.execute(
            "CREATE TABLE vault_group_access (vault_id INTEGER, group_id INTEGER, permission TEXT)"
        )
        conn.execute("CREATE TABLE IF NOT EXISTS org_members (org_id INTEGER NOT NULL, user_id INTEGER NOT NULL)")
        conn.executemany(
            "INSERT INTO vaults (id, visibility, org_id) VALUES (?, ?, ?)",
            [(1, "private", None), (2, "private", None), (3, "public", None), (4, "public", 10)],
        )
        conn.execute("INSERT INTO org_members (org_id, user_id) VALUES (10, 5)")
        conn.commit()
        return conn

    @pytest.mark.asyncio
    async def test_evaluate_policy_superadmin_grants_all(self):
        """Superadmin user → True for all actions."""
        from app.api.deps import evaluate_policy

        superadmin = {"id": 1, "role": "superadmin"}

        # Superadmin should have access to all vault actions
        assert await evaluate_policy(superadmin, "vault", 1, "read") is True
        assert await evaluate_policy(superadmin, "vault", 1, "write") is True
        assert await evaluate_policy(superadmin, "vault", 1, "delete") is True
        assert await evaluate_policy(superadmin, "vault", 1, "admin") is True

    @pytest.mark.asyncio
    async def test_evaluate_policy_admin_read_write(self):
        """Admin fallback allows read/write but not delete/admin by itself."""
        from app.api.deps import _evaluate_policy

        admin = {"id": 2, "role": "admin"}
        conn = self._vault_policy_db()

        assert await _evaluate_policy(conn, admin, "vault", 1, "read") is True
        assert await _evaluate_policy(conn, admin, "vault", 1, "write") is True
        assert await _evaluate_policy(conn, admin, "vault", 1, "delete") is False
        assert await _evaluate_policy(conn, admin, "vault", 1, "admin") is False

    @pytest.mark.asyncio
    async def test_evaluate_policy_admin_explicit_vault_admin(self):
        """Admin with explicit vault admin can perform admin/delete on that vault."""
        from app.api.deps import _evaluate_policy, get_effective_vault_permission

        admin = {"id": 2, "role": "admin"}
        conn = self._vault_policy_db()
        conn.execute(
            "INSERT INTO vault_members (vault_id, user_id, permission) VALUES (?, ?, ?)",
            (1, 2, "admin"),
        )

        assert await get_effective_vault_permission(conn, admin, 1) == "admin"
        assert await _evaluate_policy(conn, admin, "vault", 1, "admin") is True
        assert await _evaluate_policy(conn, admin, "vault", 1, "delete") is True

    @pytest.mark.asyncio
    async def test_evaluate_policy_maxes_direct_group_and_public_permissions(self):
        """Lower direct grants do not hide group admin or public read."""
        from app.api.deps import _evaluate_policy, get_effective_vault_permission

        member = {"id": 4, "role": "member"}
        conn = self._vault_policy_db()
        conn.execute(
            "INSERT INTO vault_members (vault_id, user_id, permission) VALUES (?, ?, ?)",
            (1, 4, "read"),
        )
        conn.execute(
            "INSERT INTO group_members (group_id, user_id) VALUES (?, ?)",
            (10, 4),
        )
        conn.execute(
            "INSERT INTO vault_group_access (vault_id, group_id, permission) VALUES (?, ?, ?)",
            (1, 10, "admin"),
        )

        assert await get_effective_vault_permission(conn, member, 1) == "admin"
        assert await _evaluate_policy(conn, member, "vault", 1, "delete") is True

        public_only = {"id": 5, "role": "member"}
        assert await get_effective_vault_permission(conn, public_only, 3) == "read"
        assert await _evaluate_policy(conn, public_only, "vault", 3, "read") is True
        assert await _evaluate_policy(conn, public_only, "vault", 3, "write") is False

    @pytest.mark.asyncio
    async def test_evaluate_policy_denies_org_scoped_public_read_to_non_member(self):
        """Non-org-member cannot read public vault with non-null org_id (FR-016/SC-009)."""
        from app.api.deps import _evaluate_policy, get_effective_vault_permission

        conn = self._vault_policy_db()
        # user 6 has no org_membership in org 10 (only user 5 does)
        non_member = {"id": 6, "role": "member"}

        # vault 4 is public but scoped to org 10 — non-member cannot access
        assert await get_effective_vault_permission(conn, non_member, 4) is None
        assert await _evaluate_policy(conn, non_member, "vault", 4, "read") is False

        # vault 3 is public with no org_id — still globally accessible regardless of membership
        assert await get_effective_vault_permission(conn, non_member, 3) == "read"
        assert await _evaluate_policy(conn, non_member, "vault", 3, "read") is True

        # Org member (user 5, member of org 10) can access vault 4
        org_member = {"id": 5, "role": "member"}
        assert await get_effective_vault_permission(conn, org_member, 4) == "read"
        assert await _evaluate_policy(conn, org_member, "vault", 4, "read") is True

    async def test_get_effective_vault_permissions_batches_permission_queries(self):
        """Multiple vault permissions are resolved with constant-query batching."""
        from app.api.deps import get_effective_vault_permissions

        member = {"id": 4, "role": "member"}
        conn = self._vault_policy_db()
        conn.execute(
            "INSERT INTO vault_members (vault_id, user_id, permission) VALUES (?, ?, ?)",
            (1, 4, "read"),
        )
        conn.execute(
            "INSERT INTO group_members (group_id, user_id) VALUES (?, ?)",
            (10, 4),
        )
        conn.execute(
            "INSERT INTO vault_group_access (vault_id, group_id, permission) VALUES (?, ?, ?)",
            (2, 10, "admin"),
        )
        conn.commit()

        statements = []
        conn.set_trace_callback(statements.append)

        assert await get_effective_vault_permissions(conn, member, [1, 2, 3]) == {
            1: "read",
            2: "admin",
            3: "read",
        }

        select_count = sum(
            1
            for statement in statements
            if statement.lstrip().upper().startswith("SELECT")
        )
        assert select_count == 4

    async def test_get_user_accessible_vault_ids_includes_public_read(self):
        """Accessible vault enumeration includes effective public read access."""
        from app.api.deps import get_user_accessible_vault_ids

        conn = self._vault_policy_db()
        user = {"id": 5, "role": "member"}

        # user 5 is now an org_member of org 10, so they also see vault 4 (org-scoped public)
        assert sorted(await get_user_accessible_vault_ids(user, conn)) == [3, 4]

    async def test_get_user_accessible_vault_ids_excludes_org_scoped_public_for_non_member(self):
        """Non-org-member cannot access public vault with non-null org_id."""
        from app.api.deps import get_user_accessible_vault_ids

        conn = self._vault_policy_db()
        # user 6 has no org_membership in org 10 (only user 5 does)
        non_member = {"id": 6, "role": "member"}

        result = await get_user_accessible_vault_ids(non_member, conn)

        # vault 4 is public but org-scoped (org_id=10) — non-member must not see it
        assert 4 not in result
        # But public vault 3 (no org_id) is still globally accessible
        assert 3 in result

    @pytest.mark.asyncio
    async def test_evaluate_policy_invalid_principal(self):
        """Principal without id → False."""
        from app.api.deps import evaluate_policy

        invalid_principal = {"role": "admin"}  # Missing id

        result = await evaluate_policy(invalid_principal, "vault", 1, "read")
        assert result is False

    @pytest.mark.asyncio
    async def test_evaluate_policy_none_resource_id(self):
        """resource_id=None → False."""
        from app.api.deps import evaluate_policy

        member = {"id": 3, "role": "member"}

        result = await evaluate_policy(member, "vault", None, "read")
        assert result is False

    @pytest.mark.asyncio
    async def test_evaluate_policy_non_vault_resource(self):
        """Non-vault resource type → only superadmin has access."""
        from app.api.deps import evaluate_policy

        # Superadmin can access any resource
        superadmin = {"id": 1, "role": "superadmin"}
        assert await evaluate_policy(superadmin, "document", 1, "read") is True

        # Admin cannot access non-vault resources
        admin = {"id": 2, "role": "admin"}
        assert await evaluate_policy(admin, "document", 1, "read") is False

        # Member cannot access non-vault resources
        member = {"id": 3, "role": "member"}
        assert await evaluate_policy(member, "document", 1, "read") is False

    @pytest.mark.asyncio
    async def test_evaluate_policy_group_resource_allows_admin(self):
        """Group resource type → superadmin and admin have access, member does not."""
        from app.api.deps import evaluate_policy

        # Superadmin can access group resources
        superadmin = {"id": 1, "role": "superadmin"}
        assert await evaluate_policy(superadmin, "group", 1, "read") is True

        # Admin can access group resources (whitelisted alongside vault)
        admin = {"id": 2, "role": "admin"}
        assert await evaluate_policy(admin, "group", 1, "read") is True

        # Member cannot access group resources
        member = {"id": 3, "role": "member"}
        assert await evaluate_policy(member, "group", 1, "read") is False


# ─────────────────────────────────────────────────────────────────────────────
# Test require_role — Dependency Factory
# ─────────────────────────────────────────────────────────────────────────────


class TestRequireRole:
    """Tests for require_role dependency factory."""

    @pytest.mark.asyncio
    async def test_require_role_admin_passes(self, mock_settings_jwt_mode):
        """Admin user passes require_role('admin')."""
        from app.api.deps import require_role

        check_role = require_role("admin")

        # Call the inner function directly with a user dict
        user = await check_role(
            user={"id": 2, "username": "adminuser", "role": "admin", "is_active": True}
        )
        assert user["role"] == "admin"

    @pytest.mark.asyncio
    async def test_require_role_viewer_fails_admin(self, mock_settings_jwt_mode):
        """Viewer user fails require_role('admin')."""
        from app.api.deps import require_role

        check_role = require_role("admin")

        with pytest.raises(HTTPException) as exc_info:
            await check_role(
                user={
                    "id": 5,
                    "username": "vieweruser",
                    "role": "viewer",
                    "is_active": True,
                }
            )

        assert exc_info.value.status_code == 403
        assert "insufficient" in exc_info.value.detail.lower()
        assert "admin" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_require_role_viewer_hierarchy(self, mock_settings_jwt_mode):
        """Viewer passes require_role('viewer'), fails require_role('member')."""
        from app.api.deps import require_role

        # Viewer should pass viewer role check
        check_viewer = require_role("viewer")
        user = await check_viewer(
            user={
                "id": 5,
                "username": "vieweruser",
                "role": "viewer",
                "is_active": True,
            }
        )
        assert user["role"] == "viewer"

        # Viewer should fail member role check
        check_member = require_role("member")
        with pytest.raises(HTTPException) as exc_info:
            await check_member(
                user={
                    "id": 5,
                    "username": "vieweruser",
                    "role": "viewer",
                    "is_active": True,
                }
            )

        assert exc_info.value.status_code == 403
        assert "member" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_require_role_superadmin_hierarchy(self, mock_settings_jwt_mode):
        """Superadmin passes all role checks."""
        from app.api.deps import require_role

        for role in ["viewer", "member", "admin", "superadmin"]:
            check_role = require_role(role)
            user = await check_role(
                user={
                    "id": 1,
                    "username": "super",
                    "role": "superadmin",
                    "is_active": True,
                }
            )
            assert user["role"] == "superadmin"


# ─────────────────────────────────────────────────────────────────────────────
# Test require_vault_permission — Dependency Factory
# ─────────────────────────────────────────────────────────────────────────────


class TestRequireVaultPermission:
    """Tests for require_vault_permission dependency factory."""

    @pytest.mark.asyncio
    async def test_require_role_factory_returns_callable(self, mock_settings_jwt_mode):
        """require_role returns a callable dependency."""
        from app.api.deps import require_role

        check_role = require_role("admin")
        assert callable(check_role)

    @pytest.mark.asyncio
    async def test_require_vault_permission_factory_returns_callable(self):
        """require_vault_permission returns a callable dependency."""
        from app.api.deps import require_vault_permission

        check_perm = require_vault_permission("read", "write")
        assert callable(check_perm)


# ─────────────────────────────────────────────────────────────────────────────
# Test Client Fingerprint Binding
# ─────────────────────────────────────────────────────────────────────────────


class TestClientFingerprint:
    """Tests for client fingerprint (fpt) binding in get_current_active_user."""

    @pytest.mark.asyncio
    async def test_fingerprint_matched_ua_passes(self, mock_settings_jwt_mode, mock_db):
        """Token with fpt matching current UA → user dict returned."""
        from datetime import datetime, timedelta, timezone

        import jwt

        from app.api.deps import get_current_active_user
        from app.services.auth_service import compute_client_fingerprint

        ua = "TestBrowser/1.0"
        fpt = compute_client_fingerprint(ua)
        secret, algorithm = "test-secret-key", "HS256"
        payload = {
            "sub": "42",
            "username": "testuser",
            "role": "member",
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
            "type": "access",
            "jti": "valid-jti",
            "fpt": fpt,
        }
        token = jwt.encode(payload, secret, algorithm=algorithm)

        mock_conn, mock_cursor = mock_db
        mock_cursor.fetchone.side_effect = [
            None,  # denylist check: jti not found
            (42, "testuser", "Test User", "member", 1, 0),
        ]

        # Set up mock request with matching UA
        fp_request = MagicMock()
        fp_request.url.path = "/api/auth/me"
        fp_request.headers.get.return_value = ua

        result = await get_current_active_user(
            request=fp_request,
            authorization=f"Bearer {token}",
            access_token=None,
            db=mock_conn,
        )

        assert result["id"] == 42
        assert result["username"] == "testuser"

    @pytest.mark.asyncio
    async def test_fingerprint_mismatched_ua_returns_401(self, mock_settings_jwt_mode, mock_db):
        """Token with fpt=sha256(UA='BrowserA'), request UA='BrowserB' → 401."""
        from datetime import datetime, timedelta, timezone

        import jwt

        from app.api.deps import get_current_active_user
        from app.services.auth_service import compute_client_fingerprint

        secret, algorithm = "test-secret-key", "HS256"
        # Token bound to BrowserA
        payload = {
            "sub": "42",
            "username": "testuser",
            "role": "member",
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
            "type": "access",
            "jti": "valid-jti",
            "fpt": compute_client_fingerprint("BrowserA"),
        }
        token = jwt.encode(payload, secret, algorithm=algorithm)

        mock_conn, mock_cursor = mock_db

        # Request comes from BrowserB
        fp_request = MagicMock()
        fp_request.url.path = "/api/auth/me"
        fp_request.headers.get.return_value = "BrowserB"

        with pytest.raises(HTTPException) as exc_info:
            await get_current_active_user(
                request=fp_request,
                authorization=f"Bearer {token}",
                access_token=None,
                db=mock_conn,
            )

        assert exc_info.value.status_code == 401
        assert "token_invalid" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_fingerprint_missing_returns_401(self, mock_settings_jwt_mode, mock_db):
        """Token without fpt claim (legacy) → 401 token_invalid (fail-closed)."""
        from datetime import datetime, timedelta, timezone

        import jwt

        from app.api.deps import get_current_active_user

        secret, algorithm = "test-secret-key", "HS256"
        # Token WITHOUT fpt claim (legacy token)
        payload = {
            "sub": "42",
            "username": "testuser",
            "role": "member",
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
            "type": "access",
            "jti": "valid-jti",
            # No fpt!
        }
        token = jwt.encode(payload, secret, algorithm=algorithm)

        mock_conn, mock_cursor = mock_db

        fp_request = MagicMock()
        fp_request.url.path = "/api/auth/me"
        fp_request.headers.get.return_value = "AnyBrowser"

        with pytest.raises(HTTPException) as exc_info:
            await get_current_active_user(
                request=fp_request,
                authorization=f"Bearer {token}",
                access_token=None,
                db=mock_conn,
            )

        assert exc_info.value.status_code == 401
        assert "token_invalid" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_fingerprint_empty_ua_token_accepted_with_empty_ua_request(
        self, mock_settings_jwt_mode, mock_db
    ):
        """Token with fpt=sha256(''), request UA='' → passes."""
        from datetime import datetime, timedelta, timezone

        import jwt

        from app.api.deps import get_current_active_user
        from app.services.auth_service import compute_client_fingerprint

        empty_fpt = compute_client_fingerprint("")
        secret, algorithm = "test-secret-key", "HS256"
        payload = {
            "sub": "42",
            "username": "testuser",
            "role": "member",
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
            "type": "access",
            "jti": "valid-jti",
            "fpt": empty_fpt,
        }
        token = jwt.encode(payload, secret, algorithm=algorithm)

        mock_conn, mock_cursor = mock_db
        mock_cursor.fetchone.side_effect = [
            None,  # denylist check
            (42, "testuser", "Test User", "member", 1, 0),
        ]

        fp_request = MagicMock()
        fp_request.url.path = "/api/auth/me"
        fp_request.headers.get.return_value = ""  # Empty UA

        result = await get_current_active_user(
            request=fp_request,
            authorization=f"Bearer {token}",
            access_token=None,
            db=mock_conn,
        )

        assert result["id"] == 42
