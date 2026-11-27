"""
Tests for admin_secret_token security warning at startup (#799 AC9 rewrite).

Reachability matrix per Settings.reject_insecure_defaults: in single-admin
mode (USERS_ENABLED=False) an EMPTY token and the PUBLISHED literal both
REFUSE TO START — no runtime warning can occur there. Under
USERS_ENABLED=True the published literal is deliberately permitted as the
bootstrap/first-admin credential (config.py decision record, issue #1001
Item A); that is the one reachable corner, and main.py's WARNING targets
exactly it (rotate-after-bootstrap guidance). The pre-#799 tests patched
the settings OBJECT attribute to synthesize states the validator refuses —
they pinned unreachable behavior and were removed with it.
"""

import logging
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class AdminTokenWarningTestBase(unittest.TestCase):
    """Base class with common setup for admin token warning tests."""

    def setUp(self):
        """Set up log capture before each test."""
        self.log_records = []
        self.handler = logging.Handler()
        self.handler.emit = lambda record: self.log_records.append(record)

        self.logger = logging.getLogger("app.main")
        self.logger.addHandler(self.handler)
        self.original_level = self.logger.level
        self.logger.setLevel(logging.DEBUG)

        # Snapshot the app.* module table so tearDown can restore it (the
        # re-import dance must not mint a second settings singleton).
        self._app_modules_snapshot = {
            k: v for k, v in sys.modules.items() if k == "app" or k.startswith("app.")
        }

    def tearDown(self):
        """Clean up log handler after each test."""
        self.logger.removeHandler(self.handler)
        self.logger.setLevel(self.original_level)

        for mod in [k for k in sys.modules if k == "app" or k.startswith("app.")]:
            if mod not in self._app_modules_snapshot:
                del sys.modules[mod]
        sys.modules.update(self._app_modules_snapshot)

    def _reimport_main(self):
        if "app.main" in sys.modules:
            del sys.modules["app.main"]
        import app.main  # noqa: F401

    def _messages(self, level):
        return [r.getMessage() for r in self.log_records if r.levelno >= level]


class TestBootstrapLiteralWarning(AdminTokenWarningTestBase):
    """The one REACHABLE corner: USERS_ENABLED=True + published literal."""

    def test_bootstrap_literal_emits_warning(self):
        with patch("app.config.settings.users_enabled", True), patch(
            "app.config.settings.admin_secret_token", "admin-secret-token"
        ):
            self._reimport_main()
        warnings = [
            m for m in self._messages(logging.WARNING) if "bootstrap" in m
        ]
        self.assertTrue(
            warnings,
            "Expected the rotate-after-bootstrap WARNING under "
            "USERS_ENABLED=True with the published literal",
        )

    def test_bootstrap_warning_names_rotation_command(self):
        with patch("app.config.settings.users_enabled", True), patch(
            "app.config.settings.admin_secret_token", "admin-secret-token"
        ):
            self._reimport_main()
        warnings = [
            m for m in self._messages(logging.WARNING) if "token_urlsafe" in m
        ]
        self.assertTrue(
            warnings, "Expected the WARNING to carry the rotation one-liner"
        )

    def test_no_critical_level_for_bootstrap_corner(self):
        with patch("app.config.settings.users_enabled", True), patch(
            "app.config.settings.admin_secret_token", "admin-secret-token"
        ):
            self._reimport_main()
        self.assertEqual(
            self._messages(logging.CRITICAL),
            [],
            "The reachable corner is a WARNING (a permitted bootstrap state), "
            "not a CRITICAL",
        )


class TestUnreachableStatesEmitNothing(AdminTokenWarningTestBase):
    """The states the old CRITICAL described cannot occur at runtime.

    Single-admin mode (USERS_ENABLED=False) with an empty or literal token
    refuses to construct Settings, so main.py never loads with them; the
    tests below patch the settings object anyway and assert the module-load
    warning block stays silent for them (no users_enabled -> no warning).
    """

    def test_single_admin_empty_token_no_warning(self):
        with patch("app.config.settings.users_enabled", False), patch(
            "app.config.settings.admin_secret_token", ""
        ):
            self._reimport_main()
        self.assertEqual(
            [
                m
                for m in self._messages(logging.WARNING)
                if "admin_secret_token" in m
            ],
            [],
            "The empty-token state is refused at construction in single-admin "
            "mode; the runtime warning block must not fire for it",
        )

    def test_users_enabled_default_jwt_no_warning(self):
        # The pre-#799 JWT-default warning was unreachable in every config
        # (USERS_ENABLED=True + default JWT refuses to start; False never
        # evaluated the guard) — assert the block is gone entirely.
        with patch("app.config.settings.users_enabled", True), patch(
            "app.config.settings.jwt_secret_key",
            "change-me-to-a-random-64-char-string",
        ), patch(
            "app.config.settings.admin_secret_token", "not-the-literal-token"
        ):
            self._reimport_main()
        self.assertEqual(
            [m for m in self._messages(logging.WARNING) if "jwt_secret_key" in m],
            [],
            "The unreachable JWT-default warning must not exist",
        )


class TestSecureAdminToken(AdminTokenWarningTestBase):
    """A rotated token emits nothing."""

    def test_secure_token_no_warning(self):
        with patch("app.config.settings.users_enabled", True), patch(
            "app.config.settings.admin_secret_token", "a-real-random-token"
        ):
            self._reimport_main()
        self.assertEqual(
            [
                m
                for m in self._messages(logging.WARNING)
                if "admin_secret_token" in m
            ],
            [],
            "A rotated token must not warn",
        )
