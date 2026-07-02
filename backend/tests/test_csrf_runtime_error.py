"""
Regression tests for F-008: RuntimeError not caught in validate_token expire()
and revoke_token delete().

These tests verify that when the CSRF store's expire() or delete() methods raise
RuntimeError("CSRF storage unavailable") (e.g., from sqlite3.Error), the
CSRFManager methods handle it gracefully:
- validate_token: catches RuntimeError from store.expire() in validate_token, logs
  warning "Failed to extend CSRF token TTL", returns True
- revoke_token: catches RuntimeError from store.delete() in revoke_token, logs
  warning "Failed to revoke CSRF token (storage error)", returns None

Prior to the F-008 fix, only redis.RedisError, ConnectionError, and TimeoutError
were caught — RuntimeError escaped and propagated as a 500 error.
"""

import logging
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, ".")

import redis

from app.security import CSRFManager


class TestCSRFManagerRuntimeErrorRegressionF008(unittest.TestCase):
    """Regression tests for F-008: RuntimeError handling in CSRF store operations."""

    def test_validate_token_catches_runtime_error_from_expire_and_returns_true(self):
        """
        validate_token calls store.expire() which raises RuntimeError.
        F-008 fix: RuntimeError is now caught in validate_token, logged at warning level,
        and validate_token returns True (token is still considered valid).
        Prior to F-008, RuntimeError was NOT in the except clause and would
        propagate as an unhandled exception / 500 error.
        """
        manager = CSRFManager(redis_url="redis://localhost:6379/0", ttl=900)

        # Mock the store returned by _get_store() so expire() raises RuntimeError
        mock_store = MagicMock()
        mock_store.get.return_value = "1"  # Token exists
        mock_store.expire.side_effect = RuntimeError("CSRF storage unavailable")

        with patch.object(manager, "_get_store", return_value=mock_store):
            with self.assertLogs("security", level="WARNING") as log_ctx:
                result = manager.validate_token("test_token")

        self.assertTrue(result, "validate_token should return True when expire fails")
        self.assertEqual(mock_store.expire.call_count, 1, "expire should be called once")
        # Verify warning was logged
        self.assertTrue(
            any("Failed to extend CSRF token TTL" in msg for msg in log_ctx.output),
            f"Expected warning about failing to extend TTL, got: {log_ctx.output}",
        )

    def test_validate_token_does_not_raise_when_expire_raises_runtime_error(self):
        """
        validate_token must not raise when store.expire() raises RuntimeError.
        The token existence check (store.get) already succeeded, so the token
        is valid even if extending the TTL fails.
        """
        manager = CSRFManager(redis_url="redis://localhost:6379/0", ttl=900)

        mock_store = MagicMock()
        mock_store.get.return_value = "1"
        mock_store.expire.side_effect = RuntimeError("CSRF storage unavailable")

        with patch.object(manager, "_get_store", return_value=mock_store):
            # Should not raise
            try:
                result = manager.validate_token("test_token")
            except RuntimeError as exc:
                self.fail(
                    f"validate_token raised RuntimeError instead of catching it: {exc}"
                )
            self.assertTrue(result)

    def test_revoke_token_catches_runtime_error_from_delete_and_returns_none(self):
        """
        revoke_token calls store.delete() which raises RuntimeError.
        F-008 fix: RuntimeError is now caught in revoke_token, logged at warning level,
        and revoke_token returns None. Prior to F-008, RuntimeError was NOT in the
        except clause and would propagate as an unhandled exception / 500 error.
        """
        manager = CSRFManager(redis_url="redis://localhost:6379/0", ttl=900)

        mock_store = MagicMock()
        mock_store.delete.side_effect = RuntimeError("CSRF storage unavailable")

        with patch.object(manager, "_get_store", return_value=mock_store):
            with self.assertLogs("security", level="WARNING") as log_ctx:
                result = manager.revoke_token("test_token")

        self.assertIsNone(result, "revoke_token should return None on storage error")
        self.assertEqual(mock_store.delete.call_count, 1, "delete should be called once")
        # Verify warning was logged
        self.assertTrue(
            any(
                "Failed to revoke CSRF token" in msg and "storage error" in msg
                for msg in log_ctx.output
            ),
            f"Expected warning about failing to revoke token, got: {log_ctx.output}",
        )

    def test_revoke_token_does_not_raise_when_delete_raises_runtime_error(self):
        """
        revoke_token must not raise when store.delete() raises RuntimeError.
        Revocation is best-effort; a storage failure should not crash the request.
        """
        manager = CSRFManager(redis_url="redis://localhost:6379/0", ttl=900)

        mock_store = MagicMock()
        mock_store.delete.side_effect = RuntimeError("CSRF storage unavailable")

        with patch.object(manager, "_get_store", return_value=mock_store):
            try:
                result = manager.revoke_token("test_token")
            except RuntimeError as exc:
                self.fail(
                    f"revoke_token raised RuntimeError instead of catching it: {exc}"
                )
            self.assertIsNone(result)

    def test_validate_token_redis_error_still_caught(self):
        """
        Sanity check: validate_token still catches redis.RedisError from expire()
        (this was already covered before F-008, but we verify the exception
        handling chain still works for the original exception types).
        """
        manager = CSRFManager(redis_url="redis://localhost:6379/0", ttl=900)

        mock_store = MagicMock()
        mock_store.get.return_value = "1"
        mock_store.expire.side_effect = redis.RedisError("Redis connection lost")

        with patch.object(manager, "_get_store", return_value=mock_store):
            with self.assertLogs("security", level="WARNING") as log_ctx:
                result = manager.validate_token("test_token")

        self.assertTrue(result)
        self.assertTrue(
            any("Failed to extend CSRF token TTL" in msg for msg in log_ctx.output)
        )

    def test_revoke_token_redis_error_still_caught(self):
        """
        Sanity check: revoke_token still catches redis.RedisError from delete()
        (this was already covered before F-008).
        """
        manager = CSRFManager(redis_url="redis://localhost:6379/0", ttl=900)

        mock_store = MagicMock()
        mock_store.delete.side_effect = redis.RedisError("Redis connection lost")

        with patch.object(manager, "_get_store", return_value=mock_store):
            with self.assertLogs("security", level="WARNING") as log_ctx:
                result = manager.revoke_token("test_token")

        self.assertIsNone(result)
        self.assertTrue(
            any(
                "Failed to revoke CSRF token" in msg and "storage error" in msg
                for msg in log_ctx.output
            )
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
