"""
Tests for LLM pool configuration (Task 2.1).

Verifies:
1. Settings defaults: llm_max_connections=100, llm_max_keepalive_connections=50
2. LLMClient.start() creates httpx.Limits with settings values
3. Custom settings values propagated to httpx.Limits
4. _log_pool_stats() fallback behavior (uses hardcoded 5 for keepalive - BUG)
"""

# Ensure correct import path for the test environment
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add backend to path
backend_path = Path(__file__).parent.parent.parent
sys.path.insert(0, str(backend_path))


def mock_assert_url_safe(url):
    """No-op assert_url_safe for testing."""
    pass


class TestLLMPoolConfigDefaults:
    """Test 1: Settings default values are correct."""

    def test_llm_max_connections_default(self):
        """Verify llm_max_connections defaults to 100."""
        from app.config import Settings
        settings = Settings()
        assert settings.llm_max_connections == 100

    def test_llm_max_keepalive_connections_default(self):
        """Verify llm_max_keepalive_connections defaults to 50."""
        from app.config import Settings
        settings = Settings()
        assert settings.llm_max_keepalive_connections == 50


class TestLLMClientPoolConfig:
    """Test 2 & 3: LLMClient.start() uses settings values for httpx.Limits."""

    @pytest.mark.asyncio
    async def test_start_uses_settings_values(self):
        """Verify LLMClient.start() creates httpx.Limits with default settings values."""
        with patch("app.services.llm_client.assert_url_safe", mock_assert_url_safe):
            from app.config import settings
            from app.services.llm_client import LLMClient

            client = LLMClient()
            await client.start()

            try:
                # Access the internal pool's connection limits. `_transport`
                # is now SSRFSafeTransport, which wraps the real
                # httpx.AsyncHTTPTransport as `_transport` (not `_pool`) —
                # unwrap it to reach the actual connection pool.
                assert client._client is not None
                pool = client._client._transport._transport._pool
                assert pool._max_connections == settings.llm_max_connections
                assert pool._max_keepalive_connections == settings.llm_max_keepalive_connections
            finally:
                await client.close()

    @pytest.mark.asyncio
    async def test_start_uses_custom_settings_values(self):
        """Verify LLMClient.start() uses custom settings values propagated to httpx.Limits."""
        custom_max_conn = 200
        custom_keepalive = 75

        mock_settings = MagicMock()
        mock_settings.llm_max_connections = custom_max_conn
        mock_settings.llm_max_keepalive_connections = custom_keepalive
        mock_settings.ollama_chat_url = "http://localhost:11434"
        mock_settings.chat_model = "test-model"

        with patch("app.services.llm_client.assert_url_safe", mock_assert_url_safe):
            with patch("app.services.llm_client.settings", mock_settings):
                from app.services.llm_client import LLMClient

                client = LLMClient()
                await client.start()

                try:
                    # See comment in test_start_uses_settings_values above —
                    # `_transport` is SSRFSafeTransport, unwrap to the real pool.
                    assert client._client is not None
                    pool = client._client._transport._transport._pool
                    assert pool._max_connections == custom_max_conn
                    assert pool._max_keepalive_connections == custom_keepalive
                finally:
                    await client.close()


class TestLogPoolStatsFallback:
    """Test 4: _log_pool_stats() fallback behavior.

    Note: The current implementation has a bug where _log_pool_stats()
    uses hardcoded 5 for max_keepalive_connections fallback instead of
    settings.llm_max_keepalive_connections. The code tries to access
    pool._limits which doesn't exist in httpx, so it falls back to hardcoded 5.
    """

    @pytest.mark.asyncio
    async def test_log_pool_stats_uses_pool_limits_when_available(self):
        """Verify _log_pool_stats uses actual pool limits when they are available."""
        with patch("app.services.llm_client.assert_url_safe", mock_assert_url_safe):
            from app.services.llm_client import LLMClient

            mock_settings = MagicMock()
            mock_settings.llm_max_connections = 100
            mock_settings.llm_max_keepalive_connections = 50
            mock_settings.ollama_chat_url = "http://localhost:11434"
            mock_settings.chat_model = "test-model"

            with patch("app.services.llm_client.settings", mock_settings):
                client = LLMClient()
                await client.start()

                try:
                    with patch("app.services.llm_client.logger") as mock_logger:
                        client._log_pool_stats()

                        mock_logger.info.assert_called_once()
                        log_call_args = mock_logger.info.call_args[0][0]

                        # Pool was initialized with max_connections=100, max_keepalive_connections=50
                        assert "/100 connections" in log_call_args
                        assert "/50 keepalive" in log_call_args
                finally:
                    await client.close()

    def test_log_pool_stats_no_client_returns_early(self):
        """Verify _log_pool_stats returns early if client is not started."""
        with patch("app.services.llm_client.assert_url_safe", mock_assert_url_safe):
            from app.services.llm_client import LLMClient

            mock_settings = MagicMock()
            mock_settings.llm_max_connections = 100
            mock_settings.llm_max_keepalive_connections = 50
            mock_settings.ollama_chat_url = "http://localhost:11434"
            mock_settings.chat_model = "test-model"

            with patch("app.services.llm_client.settings", mock_settings):
                client = LLMClient()
                # Don't start client

                with patch("app.services.llm_client.logger") as mock_logger:
                    client._log_pool_stats()

                    # Should return early when client is None - no logging
                    mock_logger.info.assert_not_called()
                    mock_logger.debug.assert_not_called()
