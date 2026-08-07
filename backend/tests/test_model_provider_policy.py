"""Tests for the generic model-provider policy (issue #461) + Draft parity.

Covers the generic core :mod:`app.services.model_provider_policy` with injected
allowlists (not Draft settings), and asserts the Draft Room compatibility wrapper
keeps byte-identical behavior (message-level parity), so extracting the core cannot
silently change Draft Room semantics.
"""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:  # pragma: no cover - CI installs no lancedb; backend/conftest.py stubs it
    import lancedb  # noqa: F401
except ImportError:  # pragma: no cover
    import types

    sys.modules["lancedb"] = types.ModuleType("lancedb")

from app.config import settings
from app.services import draft_provider_policy, model_provider_policy
from app.services.draft_provider_policy import assert_provider_allowed
from app.services.model_provider_policy import (
    ProviderPolicyError,
    assert_model_provider_allowed,
    http_client_kwargs,
    model_provider_snapshot,
)

LOOPBACK_URL = "http://127.0.0.1:11434"


def _assert_no_allowlist(url, sensitive=False):
    return assert_model_provider_allowed(
        url,
        sensitive=sensitive,
        ordinary_allowlist=[],
        sensitive_allowlist=[],
    )


class TestGenericEmptyAllowlistFailsClosed(unittest.TestCase):
    def test_ordinary_empty_blocks(self) -> None:
        with self.assertRaises(ProviderPolicyError) as ctx:
            _assert_no_allowlist(LOOPBACK_URL, sensitive=False)
        self.assertEqual(ctx.exception.code, "provider_origin_not_allowed")

    def test_sensitive_empty_blocks(self) -> None:
        with patch.dict(os.environ, {"ALLOW_LOCAL_SERVICES": "1"}):
            with self.assertRaises(ProviderPolicyError) as ctx:
                _assert_no_allowlist(LOOPBACK_URL, sensitive=True)
        self.assertEqual(ctx.exception.code, "provider_origin_not_allowed")


class TestGenericExactOrigin(unittest.TestCase):
    def test_allowlisted_origin_passes(self) -> None:
        with patch.dict(os.environ, {"ALLOW_LOCAL_SERVICES": "1"}):
            assert_model_provider_allowed(
                LOOPBACK_URL, sensitive=False, ordinary_allowlist=[LOOPBACK_URL], sensitive_allowlist=[]
            )

    def test_origin_mismatch_blocks(self) -> None:
        with patch.dict(os.environ, {"ALLOW_LOCAL_SERVICES": "1"}):
            with self.assertRaises(ProviderPolicyError) as ctx:
                assert_model_provider_allowed(
                    LOOPBACK_URL, sensitive=False,
                    ordinary_allowlist=["http://127.0.0.1:9999"], sensitive_allowlist=[],
                )
        self.assertEqual(ctx.exception.code, "provider_origin_not_allowed")

    def test_path_in_allowlist_entry_is_misconfiguration(self) -> None:
        with self.assertRaises(ProviderPolicyError) as ctx:
            assert_model_provider_allowed(
                "http://x.test/v1/chat", sensitive=False,
                ordinary_allowlist=["http://x.test/v1/chat"], sensitive_allowlist=[],
            )
        self.assertEqual(ctx.exception.code, "provider_policy_misconfigured")

    def test_wildcard_is_misconfiguration(self) -> None:
        with self.assertRaises(ProviderPolicyError) as ctx:
            assert_model_provider_allowed(
                "https://api.test", sensitive=False,
                ordinary_allowlist=["https://*.test"], sensitive_allowlist=[],
            )
        self.assertEqual(ctx.exception.code, "provider_policy_misconfigured")

    def test_credentials_in_url_blocked(self) -> None:
        with self.assertRaises(ProviderPolicyError) as ctx:
            _assert_no_allowlist("http://user:pass@host.test", sensitive=False)
        self.assertEqual(ctx.exception.code, "provider_scheme_not_allowed")


class TestGenericSensitiveTiers(unittest.TestCase):
    def test_sensitive_cleartext_non_loopback_refused(self) -> None:
        with self.assertRaises(ProviderPolicyError) as ctx:
            assert_model_provider_allowed(
                "http://example.com", sensitive=True,
                ordinary_allowlist=[], sensitive_allowlist=["http://example.com"],
            )
        self.assertEqual(ctx.exception.code, "provider_scheme_not_allowed")

    def test_sensitive_loopback_needs_allowlist_and_allow_local(self) -> None:
        # Allowlist entry but no ALLOW_LOCAL_SERVICES => SSRF gate blocks.
        os.environ.pop("ALLOW_LOCAL_SERVICES", None)
        try:
            with self.assertRaises(ProviderPolicyError) as ctx:
                assert_model_provider_allowed(
                    LOOPBACK_URL, sensitive=True,
                    ordinary_allowlist=[], sensitive_allowlist=[LOOPBACK_URL],
                )
            self.assertEqual(ctx.exception.code, "provider_origin_not_allowed")
        finally:
            os.environ.pop("ALLOW_LOCAL_SERVICES", None)

    def test_sensitive_loopback_both_defaults_pass(self) -> None:
        with patch.dict(os.environ, {"ALLOW_LOCAL_SERVICES": "1"}):
            assert_model_provider_allowed(
                LOOPBACK_URL, sensitive=True,
                ordinary_allowlist=[], sensitive_allowlist=[LOOPBACK_URL],
            )


class TestGenericSSRFOrdering(unittest.TestCase):
    def test_no_ssrf_for_rejected_origin(self) -> None:
        calls = []
        with (
            patch.object(model_provider_policy, "assert_url_safe", lambda u: calls.append(u)),
            patch.dict(os.environ, {"ALLOW_LOCAL_SERVICES": "1"}),
        ):
            with self.assertRaises(ProviderPolicyError):
                assert_model_provider_allowed(
                    LOOPBACK_URL, sensitive=False,
                    ordinary_allowlist=["http://127.0.0.1:9999"], sensitive_allowlist=[],
                )
        self.assertEqual(calls, [])

    def test_ssrf_runs_for_allowlisted_origin(self) -> None:
        calls = []
        with (
            patch.object(model_provider_policy, "assert_url_safe", lambda u: calls.append(u)),
            patch.dict(os.environ, {"ALLOW_LOCAL_SERVICES": "1"}),
        ):
            assert_model_provider_allowed(
                LOOPBACK_URL, sensitive=False,
                ordinary_allowlist=[LOOPBACK_URL], sensitive_allowlist=[],
            )
        self.assertEqual(calls, [LOOPBACK_URL])


class TestGenericRedaction(unittest.TestCase):
    def test_message_leaks_nothing(self) -> None:
        with self.assertRaises(ProviderPolicyError) as ctx:
            assert_model_provider_allowed(
                "https://evil.test/v1/secret?api_key=zzz", sensitive=False,
                ordinary_allowlist=["https://configured-secret.internal:8443"],
                sensitive_allowlist=[],
            )
        msg = str(ctx.exception)
        for needle in ("configured-secret", "8443", "/v1/secret", "api_key=zzz"):
            self.assertNotIn(needle, msg)


class TestGenericSnapshotAndKwargs(unittest.TestCase):
    def test_snapshot_safe_fields(self) -> None:
        class _CB:
            name = "multimodal-thinking-circuit"

        class _Fake:
            base_url = "https://user:pw@api.test:443/v1?key=zzz"
            model = "my-vlm"
            api_key = "sk-secret"
            _circuit_breaker = _CB()

        snap = model_provider_snapshot(_Fake())
        self.assertEqual(set(snap.keys()), {"base_host", "model", "logical_mode"})
        rendered = " ".join(snap.values())
        for needle in ("pw", "zzz", "sk-secret", "user:"):
            self.assertNotIn(needle, rendered)

    def test_kwargs_redirects_off(self) -> None:
        self.assertEqual(http_client_kwargs(), {"follow_redirects": False})


class TestDraftWrapperMessageParity(unittest.TestCase):
    """Draft wrapper must keep byte-identical messages via the shared parser."""

    def setUp(self) -> None:
        self._patches = [
            patch.object(settings, "draft_allowed_model_origins", []),
            patch.object(settings, "draft_sensitive_allowed_model_origins", []),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def test_wildcard_message_identity(self) -> None:
        with patch.object(settings, "draft_allowed_model_origins", ["https://*.test"]):
            with self.assertRaises(ProviderPolicyError) as ctx:
                assert_provider_allowed("https://api.test", sensitive=False)
        self.assertEqual(
            str(ctx.exception),
            "ordinary provider allowlist contains an unsupported wildcard entry.",
        )

    def test_invalid_entry_message_identity(self) -> None:
        with patch.object(settings, "draft_allowed_model_origins", ["not-a-url"]):
            with self.assertRaises(ProviderPolicyError) as ctx:
                assert_provider_allowed("https://api.test", sensitive=False)
        self.assertEqual(
            str(ctx.exception),
            "ordinary provider allowlist contains an invalid origin entry.",
        )


if __name__ == "__main__":
    unittest.main()
