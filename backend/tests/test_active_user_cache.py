"""Tests for the active user cache in deps.get_current_active_user."""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.api.deps import (
    _ACTIVE_USER_CACHE,
    _ACTIVE_USER_CACHE_LOCK,
    _build_active_user_cache_key,
    get_current_active_user,
    invalidate_active_user_cache,
)
from app.config import Settings


@pytest.fixture(autouse=True)
def _reset_active_user_cache():
    """Ensure the module-level active-user cache is empty between tests."""
    with _ACTIVE_USER_CACHE_LOCK:
        _ACTIVE_USER_CACHE.clear()
    yield
    with _ACTIVE_USER_CACHE_LOCK:
        _ACTIVE_USER_CACHE.clear()


@pytest.fixture
def mock_settings():
    with patch("app.api.deps.settings") as mock_settings:
        mock_settings.users_enabled = True
        mock_settings.active_user_cache_ttl_seconds = 30
        yield mock_settings


@pytest.fixture
def mock_decode():
    with patch("app.api.deps.decode_access_token") as mock_decode:
        mock_decode.return_value = {"sub": "123", "type": "access"}
        yield mock_decode


def _make_db_row():
    return (123, "testuser", "Test User", "member", 1, 0)


def _make_mock_db():
    mock_db = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = _make_db_row()
    mock_db.execute.return_value = mock_cursor
    return mock_db


def _make_request():
    return MagicMock()


# =============================================================================
# Cache behavior tests
# =============================================================================


class TestActiveUserCache:
    """Behavioral tests for the active user cache."""

    @pytest.mark.asyncio
    async def test_cache_hit_returns_cached_user_without_db_query(
        self, mock_settings, mock_decode
    ):
        """Cache hit returns cached user and does not query the database."""
        user_id = 123
        cache_key = _build_active_user_cache_key(user_id)
        cached_user = {
            "id": user_id,
            "username": "cacheduser",
            "full_name": "Cached User",
            "role": "admin",
            "is_active": True,
            "must_change_password": False,
        }
        with _ACTIVE_USER_CACHE_LOCK:
            _ACTIVE_USER_CACHE[cache_key] = (cached_user, time.monotonic() + 60)

        mock_db = _make_mock_db()
        result = await get_current_active_user(
            request=_make_request(),
            authorization="Bearer valid.token.here",
            db=mock_db,
        )

        assert result == cached_user
        mock_db.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_cache_miss_queries_db_and_populates_cache(
        self, mock_settings, mock_decode
    ):
        """Cache miss queries the database and stores the result in cache."""
        user_id = 123
        cache_key = _build_active_user_cache_key(user_id)
        mock_db = _make_mock_db()

        assert cache_key not in _ACTIVE_USER_CACHE

        result = await get_current_active_user(
            request=_make_request(),
            authorization="Bearer valid.token.here",
            db=mock_db,
        )

        assert result["id"] == user_id
        assert result["username"] == "testuser"
        mock_db.execute.assert_called_once()
        with _ACTIVE_USER_CACHE_LOCK:
            assert cache_key in _ACTIVE_USER_CACHE
            cached_user, expires_at = _ACTIVE_USER_CACHE[cache_key]
        assert cached_user["id"] == user_id
        assert expires_at > time.monotonic()

    @pytest.mark.asyncio
    async def test_ttl_expiry_removes_stale_entry_and_next_call_refreshes(
        self, mock_settings, mock_decode
    ):
        """After expiry, the stale entry is evicted and a subsequent call refreshes from DB."""
        user_id = 123
        cache_key = _build_active_user_cache_key(user_id)
        stale_user = {
            "id": user_id,
            "username": "stale",
            "full_name": "Stale User",
            "role": "viewer",
            "is_active": True,
            "must_change_password": False,
        }
        with _ACTIVE_USER_CACHE_LOCK:
            _ACTIVE_USER_CACHE[cache_key] = (stale_user, time.monotonic() - 1)

        mock_db = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = (
            123,
            "refreshed",
            "Refreshed User",
            "admin",
            1,
            0,
        )
        mock_db.execute.return_value = mock_cursor

        result = await get_current_active_user(
            request=_make_request(),
            authorization="Bearer valid.token.here",
            db=mock_db,
        )

        assert result["username"] == "refreshed"
        mock_db.execute.reset_mock()

        result = await get_current_active_user(
            request=_make_request(),
            authorization="Bearer valid.token.here",
            db=mock_db,
        )

        assert result["username"] == "refreshed"
        mock_db.execute.assert_not_called()
        with _ACTIVE_USER_CACHE_LOCK:
            cached_user, expires_at = _ACTIVE_USER_CACHE[cache_key]
        assert cached_user["username"] == "refreshed"

    @pytest.mark.asyncio
    async def test_invalidate_active_user_cache_removes_entry(
        self, mock_settings, mock_decode
    ):
        """invalidate_active_user_cache removes the cached entry for the user."""
        user_id = 123
        cache_key = _build_active_user_cache_key(user_id)
        cached_user = {
            "id": user_id,
            "username": "cacheduser",
            "full_name": "Cached User",
            "role": "admin",
            "is_active": True,
            "must_change_password": False,
        }
        with _ACTIVE_USER_CACHE_LOCK:
            _ACTIVE_USER_CACHE[cache_key] = (cached_user, time.monotonic() + 60)

        assert cache_key in _ACTIVE_USER_CACHE

        invalidate_active_user_cache(user_id)

        assert cache_key not in _ACTIVE_USER_CACHE


class TestActiveUserCacheTTLValidation:
    """Validation tests for the active_user_cache_ttl_seconds config field."""

    def test_ttl_below_minimum_raises(self):
        """Values below 5 are rejected."""
        with pytest.raises(ValueError, match="active_user_cache_ttl_seconds must be >= 5"):
            Settings(active_user_cache_ttl_seconds=4)

    def test_ttl_at_minimum_accepts(self):
        """Value at the minimum boundary (5) is accepted."""
        settings = Settings(active_user_cache_ttl_seconds=5)
        assert settings.active_user_cache_ttl_seconds == 5

    def test_ttl_at_maximum_accepts(self):
        """Value at the maximum boundary (300) is accepted."""
        settings = Settings(active_user_cache_ttl_seconds=300)
        assert settings.active_user_cache_ttl_seconds == 300

    def test_ttl_above_maximum_raises(self):
        """Values above 300 are rejected."""
        with pytest.raises(ValueError, match="active_user_cache_ttl_seconds must be <= 300"):
            Settings(active_user_cache_ttl_seconds=301)

    def test_default_ttl_is_thirty(self):
        """The default TTL matches the spec default of 30 seconds."""
        settings = Settings()
        assert settings.active_user_cache_ttl_seconds == 30
