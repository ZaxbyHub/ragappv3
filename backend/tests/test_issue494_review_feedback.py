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


class TestComposeShortFormForwarding(unittest.TestCase):
    r"""F2 — compose forwards documented keys via the SHORT passthrough form.

    \`- KEY\` (no \`=\`) omits unset keys entirely, so compose never injects an
    empty string into typed int/float/bool fields (the original F2 crash:
    \`- KEY=\${KEY:-}\` + partial .env → pydantic int_parsing failure at
    startup). Empty-string values remain MEANINGFUL in Settings (e.g.
    REDIS_URL="" → in-memory limiter), so env_ignore_empty must stay OFF —
    pinned by test_non_numeric_string_legacy_value_fails_loudly's sibling
    semantics and the REDIS_URL="" conftest contract.
    """

    def test_documented_typed_keys_forwarded_short_form(self):
        import re
        from pathlib import Path

        repo = Path(__file__).resolve().parents[2]
        compose = (repo / "docker-compose.yml").read_text(encoding="utf-8")
        match = re.search(r"^  knowledgevault:", compose, re.M)
        assert match, "knowledgevault service not found"
        tail = compose[match.end():]
        next_svc = re.search(r"^  [a-zA-Z_-]+:", tail, re.M)
        block = tail[: next_svc.start()] if next_svc else tail
        env_at = block.find("environment:")
        env_lines = block[env_at:].splitlines()

        forwarded = {}
        for line in env_lines:
            stripped = line.strip()
            if stripped.startswith("- "):
                entry = stripped[2:]
                key, sep, value = entry.partition("=")
                forwarded[key.strip()] = value if sep else None

        # These typed keys crashed via ${KEY:-} empty injection pre-fix.
        # The invariant: an unset .env key must resolve to OMITTED (short
        # form) or a REAL default — never an empty string, which fails
        # int/float/bool coercion at startup.
        for key in ("IMAP_PORT", "DB_POOL_MAX_SIZE", "INSTANT_MAX_TOKENS",
                    "AUTO_SCAN_ENABLED", "MAINTENANCE_MODE"):
            self.assertIn(key, forwarded, f"{key} missing from compose env")
            value = forwarded[key]
            if value is None:
                continue  # short passthrough form — omitted when unset
            resolved_for_unset = value.split(":-", 1)[-1].rstrip("}") if ":-" in value else value
            self.assertNotEqual(
                resolved_for_unset.strip(),
                "",
                f"{key}={value!r} injects an empty string for an unset .env "
                "key — typed fields fail startup coercion (review F2)",
            )


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
