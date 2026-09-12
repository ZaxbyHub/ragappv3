"""Regression pins for the PR #576 swarm-pr-review round (F1-F3).

F1: apply_legacy_settings_conversion must coerce string legacy values
    (pydantic-settings delivers env/.env values as strings into the
    mode="before" validator) — "512" * 4 must be 2048, not "512512512512".
F2: Settings must ignore empty env values (env_ignore_empty=True) so the
    compose `- KEY=${KEY:-}` forwarding of unset keys behaves like unset.
F3: SettingsUpdate must accept instant_enable_thinking=False (the bool was
    grouped into a positive-int validator, rejecting the documented default).
"""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

BASE_KWARGS = {
    "ADMIN_SECRET_TOKEN": "x" * 48,
    "JWT_SECRET_KEY": "y" * 48,
    "USERS_ENABLED": False,
}


class TestLegacyStringConversion(unittest.TestCase):
    """F1 — string legacy values convert numerically."""

    def _settings_with(self, **legacy):
        from app.config import Settings

        return Settings(**BASE_KWARGS, **legacy)

    def test_env_string_chunk_size_converts_numerically(self):
        with patch.dict(
            os.environ, {"CHUNK_SIZE": "512", "CHUNK_OVERLAP": "64"}, clear=False
        ):
            s = self._settings_with()
            self.assertEqual(s.chunk_size_chars, 2048)
            self.assertEqual(s.chunk_overlap_chars, 256)

    def test_dict_string_chunk_size_converts_numerically(self):
        s = self._settings_with(chunk_size="512")
        self.assertEqual(s.chunk_size_chars, 2048)

    def test_non_numeric_string_legacy_value_fails_loudly(self):
        # Genuinely invalid legacy input keeps the pre-PR contract: the
        # converter skips it and pydantic field validation rejects the
        # value loudly instead of silently substituting the default.
        from pydantic import ValidationError

        with self.assertRaises(ValidationError):
            self._settings_with(chunk_size="not-a-number")


class TestEnvIgnoreEmpty(unittest.TestCase):
    """F2 — empty env values behave like unset (compose ${KEY:-} contract)."""

    def test_empty_typed_env_vars_use_defaults(self):
        with patch.dict(
            os.environ,
            {"IMAP_PORT": "", "DB_POOL_MAX_SIZE": "", "INSTANT_MAX_TOKENS": ""},
            clear=False,
        ):
            from app.config import Settings

            s = Settings(**BASE_KWARGS)
            # Defaults per config.py (993 / 10 / 4096); the point is: no
            # ValidationError. Assert the exact documented defaults so a
            # default change surfaces here intentionally.
            self.assertEqual(s.imap_port, 993)
            self.assertEqual(s.db_pool_max_size, 10)
            self.assertEqual(s.instant_max_tokens, 4096)


class TestInstantEnableThinkingUpdate(unittest.TestCase):
    """F3 — SettingsUpdate accepts the documented default (False)."""

    def test_settings_update_accepts_false_and_true(self):
        from app.api.routes.settings import SettingsUpdate

        off = SettingsUpdate(instant_enable_thinking=False)
        self.assertIs(off.instant_enable_thinking, False)
        on = SettingsUpdate(instant_enable_thinking=True)
        self.assertIs(on.instant_enable_thinking, True)


if __name__ == "__main__":
    unittest.main()
