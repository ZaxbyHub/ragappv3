"""
Verification tests for health_check_api_key default change (Task 2.3).

Tests verify:
1. Settings().health_check_api_key defaults to empty string (changed from "health-api-key")
2. Custom env var value propagated correctly via HEALTH_CHECK_API_KEY
3. Existing code (limiter.py) handles empty string gracefully — empty string means no bypass
"""

import os
from unittest.mock import patch

import pytest
from backend.app.config import Settings


class TestHealthCheckApiKeyDefault:
    """Tests for health_check_api_key default value."""

    def test_health_check_api_key_defaults_to_empty_string(self):
        """
        health_check_api_key should default to '' (empty string).
        Previously defaulted to "health-api-key" which was a security bypass risk.
        Must be set via HEALTH_CHECK_API_KEY env var for any whitelist behavior.
        """
        settings = Settings()
        assert settings.health_check_api_key == ""

    def test_health_check_api_key_empty_string_type(self):
        """health_check_api_key should be a string type."""
        settings = Settings()
        assert isinstance(settings.health_check_api_key, str)

    def test_health_check_api_key_empty_string_is_falsy(self):
        """Empty string health_check_api_key is falsy (important for limiter logic)."""
        settings = Settings()
        assert not settings.health_check_api_key  # empty string is falsy


class TestHealthCheckApiKeyEnvOverride:
    """Tests for health_check_api_key environment variable override."""

    def test_health_check_api_key_env_override(self):
        """Custom HEALTH_CHECK_API_KEY env var value should be propagated correctly."""
        env_key = "test-secret-api-key-12345"
        with patch.dict(os.environ, {"HEALTH_CHECK_API_KEY": env_key}):
            settings = Settings()
            assert settings.health_check_api_key == env_key

    def test_health_check_api_key_env_override_with_special_chars(self):
        """Env var with special characters should be preserved."""
        env_key = "key-with-special-chars-!@#$%^&*()"
        with patch.dict(os.environ, {"HEALTH_CHECK_API_KEY": env_key}):
            settings = Settings()
            assert settings.health_check_api_key == env_key

    def test_health_check_api_key_env_override_unicode(self):
        """Env var with unicode characters should be preserved."""
        env_key = "key-中文-日本語-한국어"
        with patch.dict(os.environ, {"HEALTH_CHECK_API_KEY": env_key}):
            settings = Settings()
            assert settings.health_check_api_key == env_key

    def test_health_check_api_key_env_override_very_long(self):
        """Very long env var values should be preserved."""
        env_key = "x" * 1000
        with patch.dict(os.environ, {"HEALTH_CHECK_API_KEY": env_key}):
            settings = Settings()
            assert settings.health_check_api_key == env_key


class TestHealthCheckApiKeyLimiterBehavior:
    """Tests verifying limiter.py handles empty string health_check_api_key gracefully."""

    def test_empty_key_prevents_bypass(self):
        """
        When health_check_api_key is empty string, the whitelist check in limiter.py
        should NOT match any provided key (except an empty key, which is rejected by 'if key').

        The limiter check is:
            if key and hmac.compare_digest(key, settings.health_check_api_key):

        With empty health_check_api_key:
        - 'if key' requires a non-empty header value
        - Even if key is provided, compare_digest(key, "") only matches if key is ""
        - Since key must be non-empty, empty health_check_api_key effectively disables bypass
        """
        import hmac
        settings = Settings()  # health_check_api_key == ""
        provided_key = "any-random-key"

        # Key must be truthy (non-empty)
        assert provided_key  # key from header would be truthy

        # compare_digest with empty string as second arg
        # This only matches if the first arg is also empty
        assert not hmac.compare_digest(provided_key, settings.health_check_api_key)

    def test_non_empty_key_does_not_match_empty_config(self):
        """
        A provided API key should NEVER match an empty string configuration.
        """
        import hmac
        settings = Settings()

        # Various non-empty keys should not match empty string
        for key in ["", " ", "a", "health-api-key", "secret", "x" * 100]:
            if key:  # Only test truthy keys (what limiter.py checks first)
                assert not hmac.compare_digest(key, settings.health_check_api_key)

    def test_whitelist_condition_with_empty_config(self):
        """
        Simulate the actual limiter.py whitelist check with empty health_check_api_key.

        From limiter.py:
            key = request.headers.get("X-API-Key")
            if key and hmac.compare_digest(key, settings.health_check_api_key):
                return True  # whitelisted

        With empty health_check_api_key, this should always return False
        because:
        1. key must be truthy (non-empty string from header)
        2. compare_digest(non_empty_key, "") will be False
        """
        import hmac

        settings = Settings()  # health_check_api_key == ""

        # Simulate header key provided
        header_key = "some-api-key-from-header"

        # The actual limiter check
        key = header_key  # This would be from request.headers.get("X-API-Key")
        if key and hmac.compare_digest(key, settings.health_check_api_key):
            whitelisted = True
        else:
            whitelisted = False

        assert whitelisted is False

    def test_whitelist_condition_with_configured_key(self):
        """
        When a proper key is configured, the whitelist should work.

        From limiter.py:
            key = request.headers.get("X-API-Key")
            if key and hmac.compare_digest(key, settings.health_check_api_key):
                return True  # whitelisted
        """
        import hmac

        configured_key = "my-secret-health-check-key"
        with patch.dict(os.environ, {"HEALTH_CHECK_API_KEY": configured_key}):
            settings = Settings()
            assert settings.health_check_api_key == configured_key

            # Correct key should be whitelisted
            header_key = configured_key
            key = header_key
            if key and hmac.compare_digest(key, settings.health_check_api_key):
                whitelisted = True
            else:
                whitelisted = False
            assert whitelisted is True

            # Wrong key should NOT be whitelisted
            wrong_key = "wrong-key"
            key = wrong_key
            if key and hmac.compare_digest(key, settings.health_check_api_key):
                whitelisted = True
            else:
                whitelisted = False
            assert whitelisted is False
