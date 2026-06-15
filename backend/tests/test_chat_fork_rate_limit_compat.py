"""Regression test: MagicMock(spec=Request) is compatible with slowapi @limiter.limit.

Verifies that the mock Request pattern added to fix the broken chat route tests
(failed after @limiter.limit decorators were added to chat routes) works correctly.
Specifically, MagicMock(spec=Request) passes slowapi's request attribute access
and key-func extraction (get_client_ip reads request.client.host).
"""

import os
import sys
from unittest.mock import MagicMock

from starlette.requests import Request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _mock_request():
    """Replicates the _mock_request helper from test_chat_fork.py."""
    request = MagicMock(spec=Request)
    request.client.host = "127.0.0.1"
    # Ensure headers.get returns None for missing keys so hmac.compare_digest
    # in _should_whitelist doesn't fail with TypeError (MagicMock != str)
    request.headers.get = MagicMock(return_value=None)
    return request


class TestMockRequestRateLimiterCompat:
    """Regression tests for MagicMock Request compatibility with slowapi."""

    def test_magicmock_spec_request_passes_isinstance_request(self):
        """MagicMock(spec=Request) satisfies isinstance(x, Request) checks.

        slowapi's Limiter._check_request_limit receives a request and may call
        isinstance(request, Request) internally. A bare MagicMock fails this;
        MagicMock(spec=Request) passes because it replicates the Request interface.
        """
        mock = _mock_request()
        assert isinstance(mock, Request)

    def test_magicmock_spec_request_provides_client_host_attribute(self):
        """MagicMock(spec=Request) exposes client.host used by get_client_ip."""
        mock = _mock_request()
        assert hasattr(mock, "client")
        assert hasattr(mock.client, "host")
        assert mock.client.host == "127.0.0.1"

    def test_magicmock_spec_request_client_is_truthy(self):
        """get_client_ip checks 'if request.client' before accessing .host."""
        mock = _mock_request()
        assert mock.client  # must be truthy to reach .host access

    def test_get_client_ip_accepts_magicmock_request(self):
        """get_client_ip from app.limiter accepts a MagicMock(spec=Request)."""
        from app.limiter import get_client_ip

        mock = _mock_request()
        # Must not raise AttributeError or TypeError
        ip = get_client_ip(mock)
        assert ip == "127.0.0.1"

    def test_should_whitelist_returns_false_without_api_key(self):
        """_should_whitelist returns False when no X-API-Key header is set."""
        from app.limiter import _should_whitelist

        mock = _mock_request()
        # Must not raise; returns False (no API key set on mock)
        result = _should_whitelist(mock)
        assert result is False

    def test_limiter_check_request_limit_accepts_magicmock_request(self):
        """WhitelistLimiter._check_request_limit accepts a MagicMock(spec=Request)."""
        from app.limiter import limiter

        mock = _mock_request()
        # Must not raise; rate-limit check runs without error
        limiter._check_request_limit(mock, endpoint_func=None, in_middleware=False)

    def test_decorated_route_accepts_mock_request(self):
        """A route function called with a MagicMock(spec=Request) works through the limiter.

        This is the actual integration: when a test calls a route function directly
        (bypassing the ASGI stack), it passes a MagicMock(spec=Request). The decorator
        must not fail when the decorated function is called with this mock.
        """
        import asyncio
        import sqlite3

        from app.api.routes.chat import AddMessageRequest
        from app.limiter import limiter

        async def dummy_route(request, body, conn, user, rag_engine=None):
            return {"ok": True}

        # Apply the same decorator pattern used on real chat routes
        decorated = limiter.limit("100/minute")(dummy_route)

        mock_req = _mock_request()
        mock_body = AddMessageRequest(role="user", content="hello")
        mock_user = {"id": 1}

        with sqlite3.connect(":memory:") as conn:
            conn.execute("CREATE TABLE chat_sessions (id INTEGER PRIMARY KEY)")
            conn.execute("INSERT INTO chat_sessions (id) VALUES (1)")

            # Should not raise — the decorator's _check_request_limit runs
            result = asyncio.run(
                decorated(
                    request=mock_req,
                    body=mock_body,
                    conn=conn,
                    user=mock_user,
                    rag_engine=None,
                )
            )
            assert result == {"ok": True}
