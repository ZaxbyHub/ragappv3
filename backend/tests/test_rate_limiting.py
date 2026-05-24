"""
Rate limiting verification tests for chat, search, vaults, and memories endpoints.

Tests cover:
1. @limiter.limit decorators present on correct endpoints
2. Rate limit settings are configurable from settings
3. Whitelist logic allows health check API key to bypass rate limiting
4. Rate limit values match settings defaults
"""

import os
import re
import sys
import unittest
from unittest.mock import MagicMock, patch

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# File paths
CHAT_PY = os.path.join(os.path.dirname(__file__), "..", "app", "api", "routes", "chat.py")
SEARCH_PY = os.path.join(os.path.dirname(__file__), "..", "app", "api", "routes", "search.py")
VAULTS_PY = os.path.join(os.path.dirname(__file__), "..", "app", "api", "routes", "vaults.py")
MEMORIES_PY = os.path.join(os.path.dirname(__file__), "..", "app", "api", "routes", "memories.py")
CONFIG_PY = os.path.join(os.path.dirname(__file__), "..", "app", "config.py")
LIMITER_PY = os.path.join(os.path.dirname(__file__), "..", "app", "limiter.py")


def _read_file(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


class TestRateLimitingDecoratorsChat(unittest.TestCase):
    """Verify rate limiting decorators on chat endpoints."""

    @classmethod
    def setUpClass(cls):
        cls.src = _read_file(CHAT_PY)

    def test_limiter_import_present(self):
        """chat.py must import limiter from app.limiter."""
        self.assertIn(
            "from app.limiter import limiter",
            self.src,
            "Missing 'from app.limiter import limiter' import in chat.py",
        )

    def test_chat_endpoint_has_rate_limit(self):
        """POST /chat endpoint must have @limiter.limit decorator."""
        # Pattern: @router.post("/chat") followed by @limiter.limit(...)
        # (decorators apply bottom-up, so limiter is closer to function)
        pattern = (
            r"@router\.post\s*\(\s*[\"']\/chat[\"']"
            r".*?\n\s*"
            r"@limiter\.limit\s*\("
        )
        match = re.search(pattern, self.src, re.DOTALL)
        self.assertIsNotNone(
            match,
            "Could not find @router.post('/chat') followed by @limiter.limit decorator",
        )

    def test_chat_endpoint_uses_settings(self):
        """POST /chat endpoint must use settings.chat_rate_limit."""
        pattern = (
            r"@router\.post\s*\(\s*[\"']\/chat[\"']"
            r".*?\n\s*"
            r"@limiter\.limit\s*\(\s*settings\.chat_rate_limit\s*\)"
        )
        match = re.search(pattern, self.src, re.DOTALL)
        self.assertIsNotNone(
            match,
            "POST /chat must use @limiter.limit(settings.chat_rate_limit)",
        )

    def test_chat_stream_endpoint_has_rate_limit(self):
        """POST /chat/stream endpoint must have @limiter.limit decorator."""
        pattern = (
            r"@router\.post\s*\(\s*[\"']\/chat\/stream[\"']"
            r".*?\n\s*"
            r"@limiter\.limit\s*\("
        )
        match = re.search(pattern, self.src, re.DOTALL)
        self.assertIsNotNone(
            match,
            "Could not find @router.post('/chat/stream') followed by @limiter.limit decorator",
        )

    def test_chat_stream_endpoint_uses_settings(self):
        """POST /chat/stream endpoint must use settings.chat_rate_limit."""
        pattern = (
            r"@router\.post\s*\(\s*[\"']\/chat\/stream[\"']"
            r".*?\n\s*"
            r"@limiter\.limit\s*\(\s*settings\.chat_rate_limit\s*\)"
        )
        match = re.search(pattern, self.src, re.DOTALL)
        self.assertIsNotNone(
            match,
            "POST /chat/stream must use @limiter.limit(settings.chat_rate_limit)",
        )


class TestRateLimitingDecoratorsSearch(unittest.TestCase):
    """Verify rate limiting decorators on search endpoints."""

    @classmethod
    def setUpClass(cls):
        cls.src = _read_file(SEARCH_PY)

    def test_limiter_import_present(self):
        """search.py must import limiter from app.limiter."""
        self.assertIn(
            "from app.limiter import limiter",
            self.src,
            "Missing 'from app.limiter import limiter' import in search.py",
        )

    def test_search_endpoint_has_rate_limit(self):
        """POST /search endpoint must have @limiter.limit decorator."""
        pattern = (
            r"@router\.post\s*\(\s*[\"']\/search[\"']"
            r".*?\n\s*"
            r"@limiter\.limit\s*\("
        )
        match = re.search(pattern, self.src, re.DOTALL)
        self.assertIsNotNone(
            match,
            "Could not find @router.post('/search') followed by @limiter.limit decorator",
        )

    def test_search_endpoint_uses_settings(self):
        """POST /search endpoint must use settings.search_rate_limit."""
        pattern = (
            r"@router\.post\s*\(\s*[\"']\/search[\"']"
            r".*?\n\s*"
            r"@limiter\.limit\s*\(\s*settings\.search_rate_limit\s*\)"
        )
        match = re.search(pattern, self.src, re.DOTALL)
        self.assertIsNotNone(
            match,
            "POST /search must use @limiter.limit(settings.search_rate_limit)",
        )


class TestRateLimitingDecoratorsVaults(unittest.TestCase):
    """Verify rate limiting decorators on vault endpoints."""

    @classmethod
    def setUpClass(cls):
        cls.src = _read_file(VAULTS_PY)

    def test_limiter_import_present(self):
        """vaults.py must import limiter from app.limiter."""
        self.assertIn(
            "from app.limiter import limiter",
            self.src,
            "Missing 'from app.limiter import limiter' import in vaults.py",
        )

    def test_vault_create_endpoint_has_rate_limit(self):
        """POST /vaults endpoint must have @limiter.limit decorator."""
        pattern = (
            r"@router\.post\s*\(\s*[\"']\/vaults[\"']"
            r".*?\n\s*"
            r"@limiter\.limit\s*\("
        )
        match = re.search(pattern, self.src, re.DOTALL)
        self.assertIsNotNone(
            match,
            "Could not find @router.post('/vaults') followed by @limiter.limit decorator",
        )

    def test_vault_create_endpoint_uses_settings(self):
        """POST /vaults endpoint must use settings.vault_create_rate_limit."""
        pattern = (
            r"@router\.post\s*\(\s*[\"']\/vaults[\"']"
            r".*?\n\s*"
            r"@limiter\.limit\s*\(\s*settings\.vault_create_rate_limit\s*\)"
        )
        match = re.search(pattern, self.src, re.DOTALL)
        self.assertIsNotNone(
            match,
            "POST /vaults must use @limiter.limit(settings.vault_create_rate_limit)",
        )


class TestRateLimitingDecoratorsMemories(unittest.TestCase):
    """Verify rate limiting decorators on memory endpoints."""

    @classmethod
    def setUpClass(cls):
        cls.src = _read_file(MEMORIES_PY)

    def test_limiter_import_present(self):
        """memories.py must import limiter from app.limiter."""
        self.assertIn(
            "from app.limiter import limiter",
            self.src,
            "Missing 'from app.limiter import limiter' import in memories.py",
        )

    def test_memory_create_endpoint_has_rate_limit(self):
        """POST /memories endpoint must have @limiter.limit decorator."""
        pattern = (
            r"@router\.post\s*\(\s*[\"']\/memories[\"']"
            r".*?\n\s*"
            r"@limiter\.limit\s*\("
        )
        match = re.search(pattern, self.src, re.DOTALL)
        self.assertIsNotNone(
            match,
            "Could not find @router.post('/memories') followed by @limiter.limit decorator",
        )

    def test_memory_create_endpoint_uses_settings(self):
        """POST /memories endpoint must use settings.memory_mutation_rate_limit."""
        pattern = (
            r"@router\.post\s*\(\s*[\"']\/memories[\"']"
            r".*?\n\s*"
            r"@limiter\.limit\s*\(\s*settings\.memory_mutation_rate_limit\s*\)"
        )
        match = re.search(pattern, self.src, re.DOTALL)
        self.assertIsNotNone(
            match,
            "POST /memories must use @limiter.limit(settings.memory_mutation_rate_limit)",
        )

    def test_memory_update_endpoint_has_rate_limit(self):
        """PUT /memories/{memory_id} endpoint must have @limiter.limit decorator."""
        pattern = (
            r"@router\.put\s*\(\s*[\"']\/memories\/\{memory_id\}[\"']"
            r".*?\n\s*"
            r"@limiter\.limit\s*\("
        )
        match = re.search(pattern, self.src, re.DOTALL)
        self.assertIsNotNone(
            match,
            "Could not find @router.put('/memories/{memory_id}') followed by @limiter.limit decorator",
        )

    def test_memory_update_endpoint_uses_settings(self):
        """PUT /memories/{memory_id} endpoint must use settings.memory_mutation_rate_limit."""
        pattern = (
            r"@router\.put\s*\(\s*[\"']\/memories\/\{memory_id\}[\"']"
            r".*?\n\s*"
            r"@limiter\.limit\s*\(\s*settings\.memory_mutation_rate_limit\s*\)"
        )
        match = re.search(pattern, self.src, re.DOTALL)
        self.assertIsNotNone(
            match,
            "PUT /memories/{memory_id} must use @limiter.limit(settings.memory_mutation_rate_limit)",
        )

    def test_memory_delete_endpoint_has_rate_limit(self):
        """DELETE /memories/{memory_id} endpoint must have @limiter.limit decorator."""
        pattern = (
            r"@router\.delete\s*\(\s*[\"']\/memories\/\{memory_id\}[\"']"
            r".*?\n\s*"
            r"@limiter\.limit\s*\("
        )
        match = re.search(pattern, self.src, re.DOTALL)
        self.assertIsNotNone(
            match,
            "Could not find @router.delete('/memories/{memory_id}') followed by @limiter.limit decorator",
        )

    def test_memory_delete_endpoint_uses_settings(self):
        """DELETE /memories/{memory_id} endpoint must use settings.memory_mutation_rate_limit."""
        pattern = (
            r"@router\.delete\s*\(\s*[\"']\/memories\/\{memory_id\}[\"']"
            r".*?\n\s*"
            r"@limiter\.limit\s*\(\s*settings\.memory_mutation_rate_limit\s*\)"
        )
        match = re.search(pattern, self.src, re.DOTALL)
        self.assertIsNotNone(
            match,
            "DELETE /memories/{memory_id} must use @limiter.limit(settings.memory_mutation_rate_limit)",
        )


class TestRateLimitSettingsConfigurable(unittest.TestCase):
    """Verify rate limit settings are configurable and have correct defaults."""

    @classmethod
    def setUpClass(cls):
        cls.src = _read_file(CONFIG_PY)

    def test_chat_rate_limit_setting_exists(self):
        """config.py must have chat_rate_limit setting."""
        self.assertIn(
            "chat_rate_limit",
            self.src,
            "Missing 'chat_rate_limit' in config.py",
        )

    def test_chat_rate_limit_default(self):
        """chat_rate_limit must have default of '30/minute'."""
        # Look for the field definition with default
        pattern = r'chat_rate_limit:\s*str\s*=\s*["\'](\d+/\w+)["\']'
        match = re.search(pattern, self.src)
        self.assertIsNotNone(match, "Could not find chat_rate_limit default value")
        self.assertEqual(
            match.group(1),
            "30/minute",
            f"Expected chat_rate_limit default '30/minute', got '{match.group(1)}'",
        )

    def test_search_rate_limit_setting_exists(self):
        """config.py must have search_rate_limit setting."""
        self.assertIn(
            "search_rate_limit",
            self.src,
            "Missing 'search_rate_limit' in config.py",
        )

    def test_search_rate_limit_default(self):
        """search_rate_limit must have default of '30/minute'."""
        pattern = r'search_rate_limit:\s*str\s*=\s*["\'](\d+/\w+)["\']'
        match = re.search(pattern, self.src)
        self.assertIsNotNone(match, "Could not find search_rate_limit default value")
        self.assertEqual(
            match.group(1),
            "30/minute",
            f"Expected search_rate_limit default '30/minute', got '{match.group(1)}'",
        )

    def test_vault_create_rate_limit_setting_exists(self):
        """config.py must have vault_create_rate_limit setting."""
        self.assertIn(
            "vault_create_rate_limit",
            self.src,
            "Missing 'vault_create_rate_limit' in config.py",
        )

    def test_vault_create_rate_limit_default(self):
        """vault_create_rate_limit must have default of '30/minute'."""
        pattern = r'vault_create_rate_limit:\s*str\s*=\s*["\'](\d+/\w+)["\']'
        match = re.search(pattern, self.src)
        self.assertIsNotNone(match, "Could not find vault_create_rate_limit default value")
        self.assertEqual(
            match.group(1),
            "30/minute",
            f"Expected vault_create_rate_limit default '30/minute', got '{match.group(1)}'",
        )

    def test_memory_mutation_rate_limit_setting_exists(self):
        """config.py must have memory_mutation_rate_limit setting."""
        self.assertIn(
            "memory_mutation_rate_limit",
            self.src,
            "Missing 'memory_mutation_rate_limit' in config.py",
        )

    def test_memory_mutation_rate_limit_default(self):
        """memory_mutation_rate_limit must have default of '30/minute'."""
        pattern = r'memory_mutation_rate_limit:\s*str\s*=\s*["\'](\d+/\w+)["\']'
        match = re.search(pattern, self.src)
        self.assertIsNotNone(match, "Could not find memory_mutation_rate_limit default value")
        self.assertEqual(
            match.group(1),
            "30/minute",
            f"Expected memory_mutation_rate_limit default '30/minute', got '{match.group(1)}'",
        )


class TestRateLimitSettingsDocstrings(unittest.TestCase):
    """Verify rate limit settings have proper docstrings."""

    @classmethod
    def setUpClass(cls):
        cls.src = _read_file(CONFIG_PY)

    def test_chat_rate_limit_docstring(self):
        """chat_rate_limit must have a docstring describing it."""
        # Look for field definition followed by docstring on next line
        pattern = r'chat_rate_limit:\s*str\s*=\s*["\'][^"\']+["\']\s*\n\s*"""Rate limit for chat endpoints."""'
        self.assertRegex(
            self.src,
            pattern,
            "chat_rate_limit must have a docstring",
        )

    def test_search_rate_limit_docstring(self):
        """search_rate_limit must have a docstring describing it."""
        pattern = r'search_rate_limit:\s*str\s*=\s*["\'][^"\']+["\']\s*\n\s*"""Rate limit for search endpoints."""'
        self.assertRegex(
            self.src,
            pattern,
            "search_rate_limit must have a docstring",
        )

    def test_vault_create_rate_limit_docstring(self):
        """vault_create_rate_limit must have a docstring describing it."""
        pattern = r'vault_create_rate_limit:\s*str\s*=\s*["\'][^"\']+["\']\s*\n\s*"""Rate limit for vault creation endpoints."""'
        self.assertRegex(
            self.src,
            pattern,
            "vault_create_rate_limit must have a docstring",
        )

    def test_memory_mutation_rate_limit_docstring(self):
        """memory_mutation_rate_limit must have a docstring describing it."""
        pattern = r'memory_mutation_rate_limit:\s*str\s*=\s*["\'][^"\']+["\']\s*\n\s*"""Rate limit for memory mutation endpoints \(create, update, delete\)."""'
        self.assertRegex(
            self.src,
            pattern,
            "memory_mutation_rate_limit must have a docstring",
        )


class TestWhitelistLimiter(unittest.TestCase):
    """Verify WhitelistLimiter allows health check API key to bypass rate limiting."""

    def test_whitelist_limiter_imports(self):
        """limiter.py must import necessary dependencies."""
        src = _read_file(LIMITER_PY)
        self.assertIn("from slowapi import Limiter", src)
        self.assertIn("from slowapi.util import get_remote_address", src)
        self.assertIn("from starlette.requests import Request", src)

    def test_whitelist_limiter_class_exists(self):
        """WhitelistLimiter class must exist in limiter.py."""
        src = _read_file(LIMITER_PY)
        self.assertIn("class WhitelistLimiter", src)

    def test_should_whitelist_function_exists(self):
        """_should_whitelist function must exist in limiter.py."""
        src = _read_file(LIMITER_PY)
        self.assertIn("def _should_whitelist", src)

    def test_whitelist_uses_health_check_api_key(self):
        """_should_whitelist must check X-API-Key header against health_check_api_key."""
        src = _read_file(LIMITER_PY)
        self.assertIn("X-API-Key", src)
        self.assertIn("health_check_api_key", src)

    def test_whitelist_uses_hmac_compare_digest(self):
        """_should_whitelist must use hmac.compare_digest for timing-safe comparison."""
        src = _read_file(LIMITER_PY)
        self.assertIn("hmac.compare_digest", src)

    def test_limiter_instance_exported(self):
        """limiter instance must be exported from limiter.py."""
        src = _read_file(LIMITER_PY)
        self.assertIn("limiter = WhitelistLimiter", src)

    def test_whitelist_limiter_extends_limiter(self):
        """WhitelistLimiter must extend Limiter from slowapi."""
        src = _read_file(LIMITER_PY)
        pattern = r"class WhitelistLimiter\s*\(\s*Limiter\s*\)"
        self.assertRegex(src, pattern)


class TestWhitelistLogicBypass(unittest.TestCase):
    """Test that whitelist logic properly bypasses rate limiting."""

    def test_whitelist_returns_true_with_valid_key(self):
        """_should_whitelist should return True when X-API-Key matches health_check_api_key."""
        from starlette.requests import Request

        from app.limiter import _should_whitelist

        # Create mock request with matching API key
        mock_request = MagicMock(spec=Request)
        mock_request.headers.get.return_value = "test-api-key"
        mock_request.client.host = "127.0.0.1"
        mock_request.headers.get.return_value = "valid-key"

        with patch("app.limiter.settings") as mock_settings:
            mock_settings.health_check_api_key = "valid-key"
            result = _should_whitelist(mock_request)
            self.assertTrue(result)

    def test_whitelist_returns_false_with_invalid_key(self):
        """_should_whitelist should return False when X-API-Key does not match."""
        from starlette.requests import Request

        from app.limiter import _should_whitelist

        # Create mock request with non-matching API key
        mock_request = MagicMock(spec=Request)
        mock_request.headers.get.return_value = "wrong-key"
        mock_request.client.host = "127.0.0.1"

        with patch("app.limiter.settings") as mock_settings:
            mock_settings.health_check_api_key = "correct-key"
            result = _should_whitelist(mock_request)
            self.assertFalse(result)

    def test_whitelist_returns_false_when_no_key(self):
        """_should_whitelist should return False when X-API-Key header is not present."""
        from starlette.requests import Request

        from app.limiter import _should_whitelist

        # Create mock request with no API key header
        mock_request = MagicMock(spec=Request)
        mock_request.headers.get.return_value = None
        mock_request.client.host = "127.0.0.1"

        with patch("app.limiter.settings") as mock_settings:
            mock_settings.health_check_api_key = "some-key"
            result = _should_whitelist(mock_request)
            self.assertFalse(result)


class TestNoRateLimitOnReadOnlyEndpoints(unittest.TestCase):
    """Verify that read-only endpoints do NOT have rate limiting."""

    def test_vaults_list_no_rate_limit(self):
        """GET /vaults should NOT have rate limiting (admin-only read)."""
        src = _read_file(VAULTS_PY)
        # Check that @limiter.limit does NOT appear immediately before GET /vaults
        # Pattern: @router.get followed immediately by @limiter.limit (on next line)
        pattern = (
            r"@router\.get\s*\(\s*[\"']\/vaults[\"']"
            r"[^\n]*\n\s*"
            r"@limiter\.limit\s*\("
        )
        match = re.search(pattern, src)
        self.assertIsNone(
            match,
            "GET /vaults should NOT have @limiter.limit decorator immediately before it",
        )

    def test_vaults_accessible_no_rate_limit(self):
        """GET /vaults/accessible should NOT have rate limiting (user-specific read)."""
        src = _read_file(VAULTS_PY)
        pattern = (
            r"@router\.get\s*\(\s*[\"']\/vaults\/accessible[\"']"
            r"[^\n]*\n\s*"
            r"@limiter\.limit\s*\("
        )
        match = re.search(pattern, src)
        self.assertIsNone(
            match,
            "GET /vaults/accessible should NOT have @limiter.limit decorator immediately before it",
        )

    def test_vault_get_no_rate_limit(self):
        """GET /vaults/{vault_id} should NOT have rate limiting."""
        src = _read_file(VAULTS_PY)
        pattern = (
            r"@router\.get\s*\(\s*[\"']\/vaults\/\{vault_id\}[\"']"
            r"[^\n]*\n\s*"
            r"@limiter\.limit\s*\("
        )
        match = re.search(pattern, src)
        self.assertIsNone(
            match,
            "GET /vaults/{vault_id} should NOT have @limiter.limit decorator immediately before it",
        )

    def test_memories_list_no_rate_limit(self):
        """GET /memories should NOT have rate limiting (read-only)."""
        src = _read_file(MEMORIES_PY)
        pattern = (
            r"@router\.get\s*\(\s*[\"']\/memories[\"']"
            r"[^\n]*\n\s*"
            r"@limiter\.limit\s*\("
        )
        match = re.search(pattern, src)
        self.assertIsNone(
            match,
            "GET /memories should NOT have @limiter.limit decorator immediately before it",
        )

    def test_memories_search_no_rate_limit(self):
        """GET /memories/search should NOT have rate limiting (read-only)."""
        src = _read_file(MEMORIES_PY)
        pattern = (
            r"@router\.get\s*\(\s*[\"']\/memories\/search[\"']"
            r"[^\n]*\n\s*"
            r"@limiter\.limit\s*\("
        )
        match = re.search(pattern, src)
        self.assertIsNone(
            match,
            "GET /memories/search should NOT have @limiter.limit decorator immediately before it",
        )

    def test_chat_sessions_list_no_rate_limit(self):
        """GET /chat/sessions should NOT have rate limiting (read-only)."""
        src = _read_file(CHAT_PY)
        pattern = (
            r"@router\.get\s*\(\s*[\"']\/chat\/sessions[\"']"
            r"[^\n]*\n\s*"
            r"@limiter\.limit\s*\("
        )
        match = re.search(pattern, src)
        self.assertIsNone(
            match,
            "GET /chat/sessions should NOT have @limiter.limit decorator immediately before it",
        )


if __name__ == "__main__":
    unittest.main()
