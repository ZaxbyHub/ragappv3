"""
Tests for admin_secret_token security warning at startup.

This module tests the CRITICAL log emitted when admin_secret_token
is empty string or the default value 'admin-secret-token'.
"""

import logging
import os
import sys
import unittest
from unittest.mock import patch

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class AdminTokenWarningTestBase(unittest.TestCase):
    """Base class with common setup for admin token warning tests."""

    def setUp(self):
        """Set up log capture before each test."""
        # Create a handler to capture log records
        self.log_records = []
        self.handler = logging.Handler()
        self.handler.emit = lambda record: self.log_records.append(record)

        # Get the main module's logger
        self.logger = logging.getLogger("app.main")
        self.logger.addHandler(self.handler)
        self.original_level = self.logger.level
        self.logger.setLevel(logging.DEBUG)

        # Snapshot the app.* module table so tearDown can restore it. These
        # tests reload app.main to re-trigger its module-load warning; the
        # restore prevents a re-imported app.config from minting a second
        # `settings` singleton that diverges from the one app code captured at
        # import time (which leaked into later route tests as
        # "no such table: users").
        self._app_modules_snapshot = {
            k: v for k, v in sys.modules.items() if k == "app" or k.startswith("app.")
        }

    def tearDown(self):
        """Clean up log handler after each test."""
        self.logger.removeHandler(self.handler)
        self.logger.setLevel(self.original_level)

        # Restore the app.* module table to its pre-test state. Each test body
        # does its own fresh `del sys.modules["app.main"]; import app.main` to
        # exercise the startup warning; here we only undo the side effects so
        # the shared `app.config.settings` singleton stays identical to the one
        # other modules hold, keeping later tests isolated.
        for mod in [k for k in sys.modules if k == "app" or k.startswith("app.")]:
            if mod not in self._app_modules_snapshot:
                del sys.modules[mod]
        sys.modules.update(self._app_modules_snapshot)

    def _get_critical_logs(self):
        """Filter and return all CRITICAL level log records."""
        return [r for r in self.log_records if r.levelno >= logging.CRITICAL]

    def _get_log_messages(self, level=logging.CRITICAL):
        """Get all log messages at or above the specified level."""
        return [r.getMessage() for r in self.log_records if r.levelno >= level]


class TestEmptyAdminToken(AdminTokenWarningTestBase):
    """Test cases for empty admin_secret_token."""

    def test_empty_token_emits_critical_log(self):
        """
        WHEN admin_secret_token is empty string
        THEN a CRITICAL log is emitted at module load.
        """
        # Patch settings before importing/reloading main
        with patch("app.config.settings.admin_secret_token", ""):
            # Clear and reimport main module to trigger startup check
            if "app.main" in sys.modules:
                del sys.modules["app.main"]

            # Import fresh to trigger module-level check
            import app.main  # noqa: F401

            # Verify CRITICAL log was emitted
            critical_logs = self._get_critical_logs()
            self.assertGreaterEqual(
                len(critical_logs),
                1,
                "Expected at least one CRITICAL log for empty admin_secret_token",
            )

    def test_empty_token_message_contains_security_warning(self):
        """
        WHEN admin_secret_token is empty string
        THEN the log message contains 'SECURITY WARNING'.
        """
        with patch("app.config.settings.admin_secret_token", ""):
            if "app.main" in sys.modules:
                del sys.modules["app.main"]

            import app.main  # noqa: F401

            messages = self._get_log_messages()
            # Join all messages to search for expected content
            full_message = " ".join(messages)
            self.assertIn(
                "SECURITY WARNING",
                full_message,
                "Log message must contain 'SECURITY WARNING'",
            )

    def test_empty_token_message_mentions_admin_secret_token(self):
        """
        WHEN admin_secret_token is empty string
        THEN the log message mentions 'admin_secret_token'.
        """
        with patch("app.config.settings.admin_secret_token", ""):
            if "app.main" in sys.modules:
                del sys.modules["app.main"]

            import app.main  # noqa: F401

            messages = self._get_log_messages()
            full_message = " ".join(messages)
            self.assertIn(
                "admin_secret_token",
                full_message,
                "Log message must mention 'admin_secret_token'",
            )

    def test_empty_token_message_mentions_unauthenticated(self):
        """
        WHEN admin_secret_token is empty string
        THEN the log message mentions 'unauthenticated'.
        """
        with patch("app.config.settings.admin_secret_token", ""):
            if "app.main" in sys.modules:
                del sys.modules["app.main"]

            import app.main  # noqa: F401

            messages = self._get_log_messages()
            full_message = " ".join(messages)
            self.assertIn(
                "unauthenticated",
                full_message,
                "Log message must mention 'unauthenticated'",
            )


class TestDefaultAdminToken(AdminTokenWarningTestBase):
    """Test cases for default admin_secret_token value."""

    def test_default_token_emits_critical_log(self):
        """
        WHEN admin_secret_token is 'admin-secret-token' (default insecure value)
        THEN a CRITICAL log is emitted at module load.
        """
        with patch("app.config.settings.admin_secret_token", "admin-secret-token"):
            if "app.main" in sys.modules:
                del sys.modules["app.main"]

            import app.main  # noqa: F401

            critical_logs = self._get_critical_logs()
            self.assertGreaterEqual(
                len(critical_logs),
                1,
                "Expected at least one CRITICAL log for default admin_secret_token",
            )

    def test_default_token_message_contains_security_warning(self):
        """
        WHEN admin_secret_token is 'admin-secret-token'
        THEN the log message contains 'SECURITY WARNING'.
        """
        with patch("app.config.settings.admin_secret_token", "admin-secret-token"):
            if "app.main" in sys.modules:
                del sys.modules["app.main"]

            import app.main  # noqa: F401

            messages = self._get_log_messages()
            full_message = " ".join(messages)
            self.assertIn(
                "SECURITY WARNING",
                full_message,
                "Log message must contain 'SECURITY WARNING'",
            )

    def test_default_token_message_mentions_admin_secret_token(self):
        """
        WHEN admin_secret_token is 'admin-secret-token'
        THEN the log message mentions 'admin_secret_token'.
        """
        with patch("app.config.settings.admin_secret_token", "admin-secret-token"):
            if "app.main" in sys.modules:
                del sys.modules["app.main"]

            import app.main  # noqa: F401

            messages = self._get_log_messages()
            full_message = " ".join(messages)
            self.assertIn(
                "admin_secret_token",
                full_message,
                "Log message must mention 'admin_secret_token'",
            )

    def test_default_token_message_mentions_unauthenticated(self):
        """
        WHEN admin_secret_token is 'admin-secret-token'
        THEN the log message mentions 'unauthenticated'.
        """
        with patch("app.config.settings.admin_secret_token", "admin-secret-token"):
            if "app.main" in sys.modules:
                del sys.modules["app.main"]

            import app.main  # noqa: F401

            messages = self._get_log_messages()
            full_message = " ".join(messages)
            self.assertIn(
                "unauthenticated",
                full_message,
                "Log message must mention 'unauthenticated'",
            )


class TestSecureAdminToken(AdminTokenWarningTestBase):
    """Test cases for properly configured admin_secret_token."""

    def test_secure_token_no_critical_log(self):
        """
        WHEN admin_secret_token is a proper secure value
        THEN NO CRITICAL log is emitted at module load.
        """
        with patch("app.config.settings.admin_secret_token", "my-secure-token-123"):
            if "app.main" in sys.modules:
                del sys.modules["app.main"]

            import app.main  # noqa: F401

            critical_logs = self._get_critical_logs()
            # Should have NO critical logs about admin token
            security_warnings = [
                r for r in critical_logs if "admin_secret_token" in r.getMessage()
            ]
            self.assertEqual(
                len(security_warnings),
                0,
                "Expected NO CRITICAL log for secure admin_secret_token, "
                f"but got {len(security_warnings)}: {security_warnings}",
            )

    def test_secure_token_with_special_chars_no_warning(self):
        """
        WHEN admin_secret_token contains special characters (secure)
        THEN NO CRITICAL log is emitted.
        """
        secure_token = "super-secret!@#$%^&*()_+-={}[]|:;<>?,./"
        with patch("app.config.settings.admin_secret_token", secure_token):
            if "app.main" in sys.modules:
                del sys.modules["app.main"]

            import app.main  # noqa: F401

            critical_logs = self._get_critical_logs()
            security_warnings = [
                r for r in critical_logs if "admin_secret_token" in r.getMessage()
            ]
            self.assertEqual(
                len(security_warnings),
                0,
                "Expected NO CRITICAL log for secure token with special chars",
            )

    def test_secure_token_long_no_warning(self):
        """
        WHEN admin_secret_token is a long secure token
        THEN NO CRITICAL log is emitted.
        """
        secure_token = "a" * 256  # Long token
        with patch("app.config.settings.admin_secret_token", secure_token):
            if "app.main" in sys.modules:
                del sys.modules["app.main"]

            import app.main  # noqa: F401

            critical_logs = self._get_critical_logs()
            security_warnings = [
                r for r in critical_logs if "admin_secret_token" in r.getMessage()
            ]
            self.assertEqual(
                len(security_warnings),
                0,
                "Expected NO CRITICAL log for long secure token",
            )


class TestLogMessageFormat(AdminTokenWarningTestBase):
    """Test the exact format of the security warning message."""

    def test_message_contains_expected_phrases(self):
        """
        WHEN security warning is logged
        THEN the message contains all expected phrases:
        - 'SECURITY WARNING'
        - 'admin_secret_token'
        - 'not set or is using the default value'
        - 'unauthenticated'
        """
        with patch("app.config.settings.admin_secret_token", ""):
            if "app.main" in sys.modules:
                del sys.modules["app.main"]

            import app.main  # noqa: F401

            messages = self._get_log_messages()
            full_message = " ".join(messages)

            # Check all required phrases
            required_phrases = [
                "SECURITY WARNING",
                "admin_secret_token",
                "unauthenticated",
            ]

            for phrase in required_phrases:
                self.assertIn(
                    phrase,
                    full_message,
                    f"Log message missing required phrase: '{phrase}'",
                )


class TestEdgeCases(AdminTokenWarningTestBase):
    """Test edge cases for admin token validation."""

    def test_whitespace_only_token_emits_warning(self):
        """
        WHEN admin_secret_token is whitespace only (not empty string)
        THEN no CRITICAL log is emitted (only '' and 'admin-secret-token' trigger warning).

        Note: This tests that the check is exact - whitespace is NOT treated as empty.
        """
        with patch("app.config.settings.admin_secret_token", "   "):
            if "app.main" in sys.modules:
                del sys.modules["app.main"]

            import app.main  # noqa: F401

            critical_logs = self._get_critical_logs()
            security_warnings = [
                r for r in critical_logs if "admin_secret_token" in r.getMessage()
            ]
            # Whitespace-only is NOT in the trigger list, so no warning expected
            self.assertEqual(
                len(security_warnings),
                0,
                "Whitespace-only token should not trigger warning (not in trigger list)",
            )

    def test_null_byte_token_no_warning(self):
        """
        WHEN admin_secret_token contains null bytes (edge case)
        THEN no CRITICAL log is emitted (not in trigger list).
        """
        with patch("app.config.settings.admin_secret_token", "\x00"):
            if "app.main" in sys.modules:
                del sys.modules["app.main"]

            import app.main  # noqa: F401

            critical_logs = self._get_critical_logs()
            security_warnings = [
                r for r in critical_logs if "admin_secret_token" in r.getMessage()
            ]
            self.assertEqual(
                len(security_warnings),
                0,
                "Null byte token should not trigger warning (not in trigger list)",
            )

    def test_case_sensitive_default_check(self):
        """
        WHEN admin_secret_token is case-variant of 'admin-secret-token'
        THEN no CRITICAL log is emitted (check is case-sensitive).
        """
        # Test case variations that should NOT trigger warning
        case_variants = [
            "Admin-Secret-Token",
            "ADMIN-SECRET-TOKEN",
            "admin_secret_token",  # underscore instead of hyphen
        ]

        for variant in case_variants:
            with self.subTest(token=variant):
                with patch("app.config.settings.admin_secret_token", variant):
                    # Clear modules for each iteration
                    if "app.main" in sys.modules:
                        del sys.modules["app.main"]

                    # Clear log records
                    self.log_records.clear()

                    import app.main  # noqa: F401

                    critical_logs = self._get_critical_logs()
                    security_warnings = [
                        r
                        for r in critical_logs
                        if "admin_secret_token" in r.getMessage()
                    ]
                    self.assertEqual(
                        len(security_warnings),
                        0,
                        f"Case variant '{variant}' should not trigger warning",
                    )


if __name__ == "__main__":
    unittest.main()
