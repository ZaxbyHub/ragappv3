"""Permanent tests for app.services.draft_provider_policy (issue #436 §6, SPEC §9.2).

Pure unit tests -- no DB, no HTTP, no network. ``settings.draft_allowed_model_origins``
/ ``settings.draft_sensitive_allowed_model_origins`` are patched per-test with
``unittest.mock.patch.object`` (matching ``test_draft_pipeline.py``'s convention), and
``ALLOW_LOCAL_SERVICES`` is patched via ``patch.dict(os.environ, ...)`` since
``ssrf._local_services_opt_in_enabled()`` reads the environment at call time.

The one loopback origin used throughout (``http://127.0.0.1:11434``) never leaves the
machine: ``assert_url_safe`` only performs local address-family checks against it, no
outbound network call is made.
"""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

try:  # pragma: no cover - CI installs no lancedb; backend/conftest.py stubs it
    import lancedb  # noqa: F401
except ImportError:  # pragma: no cover
    import types

    sys.modules["lancedb"] = types.ModuleType("lancedb")

from app.config import settings
from app.services import draft_provider_policy
from app.services.draft_provider_policy import (
    ProviderPolicyError,
    assert_provider_allowed,
    draft_http_client_kwargs,
    provider_snapshot,
)

LOOPBACK_URL = "http://127.0.0.1:11434"


class ProviderPolicyTestBase(unittest.TestCase):
    """Base class providing a clean-slate allowlist for every test."""

    def setUp(self) -> None:
        self._patches = [
            patch.object(settings, "draft_allowed_model_origins", []),
            patch.object(settings, "draft_sensitive_allowed_model_origins", []),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)


class TestEmptyAllowlistFailsClosed(ProviderPolicyTestBase):
    """SPEC §9.2 / §15: an empty allowlist blocks everything (fail closed)."""

    def test_ordinary_tier_empty_allowlist_rejects_everything(self) -> None:
        with self.assertRaises(ProviderPolicyError) as ctx:
            assert_provider_allowed(LOOPBACK_URL, sensitive=False)
        self.assertEqual(ctx.exception.code, "provider_origin_not_allowed")

    def test_sensitive_tier_empty_allowlist_rejects_everything(self) -> None:
        with patch.dict(os.environ, {"ALLOW_LOCAL_SERVICES": "1"}):
            with self.assertRaises(ProviderPolicyError) as ctx:
                assert_provider_allowed(LOOPBACK_URL, sensitive=True)
        self.assertEqual(ctx.exception.code, "provider_origin_not_allowed")

    def test_empty_allowlist_rejects_a_normal_https_url_too(self) -> None:
        # Not just loopback: an arbitrary allowlisted-looking https origin is
        # also rejected when the allowlist itself is empty.
        with self.assertRaises(ProviderPolicyError) as ctx:
            assert_provider_allowed("https://api.example-provider.test", sensitive=False)
        self.assertEqual(ctx.exception.code, "provider_origin_not_allowed")


class TestAllowlistedOriginPasses(ProviderPolicyTestBase):
    def test_allowlisted_ordinary_origin_passes(self) -> None:
        with (
            patch.object(settings, "draft_allowed_model_origins", [LOOPBACK_URL]),
            patch.dict(os.environ, {"ALLOW_LOCAL_SERVICES": "1"}),
        ):
            assert_provider_allowed(LOOPBACK_URL, sensitive=False)  # must not raise

    def test_non_allowlisted_origin_raises_provider_origin_not_allowed(self) -> None:
        with (
            patch.object(settings, "draft_allowed_model_origins", ["http://127.0.0.1:9999"]),
            patch.dict(os.environ, {"ALLOW_LOCAL_SERVICES": "1"}),
        ):
            with self.assertRaises(ProviderPolicyError) as ctx:
                assert_provider_allowed(LOOPBACK_URL, sensitive=False)
        self.assertEqual(ctx.exception.code, "provider_origin_not_allowed")


class TestTierIsolation(ProviderPolicyTestBase):
    """Ordinary-tier allowlist membership must NOT imply sensitive-tier membership."""

    def test_ordinary_allowlisted_origin_is_not_thereby_sensitive_allowed(self) -> None:
        with (
            patch.object(settings, "draft_allowed_model_origins", [LOOPBACK_URL]),
            patch.object(settings, "draft_sensitive_allowed_model_origins", []),
            patch.dict(os.environ, {"ALLOW_LOCAL_SERVICES": "1"}),
        ):
            # Passes for ordinary...
            assert_provider_allowed(LOOPBACK_URL, sensitive=False)
            # ...but the identical origin is rejected for sensitive because the
            # sensitive allowlist is independent and empty.
            with self.assertRaises(ProviderPolicyError) as ctx:
                assert_provider_allowed(LOOPBACK_URL, sensitive=True)
        self.assertEqual(ctx.exception.code, "provider_origin_not_allowed")

    def test_sensitive_allowlisted_origin_is_not_thereby_ordinary_allowed(self) -> None:
        with (
            patch.object(settings, "draft_allowed_model_origins", []),
            patch.object(settings, "draft_sensitive_allowed_model_origins", [LOOPBACK_URL]),
            patch.dict(os.environ, {"ALLOW_LOCAL_SERVICES": "1"}),
        ):
            assert_provider_allowed(LOOPBACK_URL, sensitive=True)
            with self.assertRaises(ProviderPolicyError) as ctx:
                assert_provider_allowed(LOOPBACK_URL, sensitive=False)
        self.assertEqual(ctx.exception.code, "provider_origin_not_allowed")


class TestSensitiveTierHttpsRequirement(ProviderPolicyTestBase):
    def test_sensitive_tier_cleartext_non_loopback_refused(self) -> None:
        with patch.object(
            settings, "draft_sensitive_allowed_model_origins", ["http://example.com"]
        ):
            with self.assertRaises(ProviderPolicyError) as ctx:
                assert_provider_allowed("http://example.com", sensitive=True)
        self.assertEqual(ctx.exception.code, "provider_scheme_not_allowed")

    def test_sensitive_tier_https_passes_scheme_check(self) -> None:
        # HTTPS to a non-loopback host clears the scheme gate; it still needs
        # SSRF/allowlist membership, exercised separately above -- here we only
        # assert it is NOT rejected for `provider_scheme_not_allowed`.
        with patch.object(settings, "draft_sensitive_allowed_model_origins", []):
            with self.assertRaises(ProviderPolicyError) as ctx:
                assert_provider_allowed("https://api.example-provider.test", sensitive=True)
        self.assertEqual(ctx.exception.code, "provider_origin_not_allowed")


class TestSensitiveLoopbackRequiresBothHalves(ProviderPolicyTestBase):
    """SPEC §9.2: sensitive loopback cleartext needs the allowlist entry AND
    ``ALLOW_LOCAL_SERVICES=1``. Neither one alone is sufficient."""

    def test_allowlist_entry_without_allow_local_services_is_still_blocked(self) -> None:
        with (
            patch.object(settings, "draft_sensitive_allowed_model_origins", [LOOPBACK_URL]),
            patch.dict(os.environ, {}, clear=False),
        ):
            os.environ.pop("ALLOW_LOCAL_SERVICES", None)
            with self.assertRaises(ProviderPolicyError) as ctx:
                assert_provider_allowed(LOOPBACK_URL, sensitive=True)
        # Cleared the scheme gate (loopback), cleared the allowlist gate
        # (entry present) -- SSRF's own local-address block is what fires,
        # re-wrapped as the stable provider-policy code.
        self.assertEqual(ctx.exception.code, "provider_origin_not_allowed")

    def test_allow_local_services_without_allowlist_entry_is_still_blocked(self) -> None:
        with (
            patch.object(settings, "draft_sensitive_allowed_model_origins", []),
            patch.dict(os.environ, {"ALLOW_LOCAL_SERVICES": "1"}),
        ):
            with self.assertRaises(ProviderPolicyError) as ctx:
                assert_provider_allowed(LOOPBACK_URL, sensitive=True)
        self.assertEqual(ctx.exception.code, "provider_origin_not_allowed")

    def test_both_halves_present_passes(self) -> None:
        with (
            patch.object(settings, "draft_sensitive_allowed_model_origins", [LOOPBACK_URL]),
            patch.dict(os.environ, {"ALLOW_LOCAL_SERVICES": "1"}),
        ):
            assert_provider_allowed(LOOPBACK_URL, sensitive=True)  # must not raise


class TestOrderingNoSsrfForRejectedOrigin(ProviderPolicyTestBase):
    """A NON-allowlisted host must trigger ZERO SSRF/DNS resolution.

    ``draft_provider_policy`` imports ``assert_url_safe`` with
    ``from app.services.ssrf import assert_url_safe`` (a direct name binding),
    so patching ``app.services.ssrf.assert_url_safe`` after that import would
    NOT be observed by calls made inside ``draft_provider_policy`` -- the
    module already holds its own reference. To actually observe (or fail to
    observe) a call, the patch target must be the imported name inside
    ``draft_provider_policy`` itself.
    """

    def test_non_allowlisted_host_never_reaches_ssrf(self) -> None:
        calls = []

        def _recording_assert_url_safe(url: str) -> None:
            calls.append(url)

        with (
            patch.object(settings, "draft_allowed_model_origins", ["http://127.0.0.1:9999"]),
            patch.object(
                draft_provider_policy, "assert_url_safe", _recording_assert_url_safe
            ),
        ):
            with self.assertRaises(ProviderPolicyError) as ctx:
                assert_provider_allowed(LOOPBACK_URL, sensitive=False)
        self.assertEqual(ctx.exception.code, "provider_origin_not_allowed")
        self.assertEqual(calls, [], "SSRF guard must never run for a rejected origin")

    def test_allowlisted_host_does_reach_ssrf(self) -> None:
        # Sanity counterpart: once an origin clears the allowlist, the SSRF
        # gate DOES run (both gates are mandatory for an allowed origin).
        calls = []

        def _recording_assert_url_safe(url: str) -> None:
            calls.append(url)

        with (
            patch.object(settings, "draft_allowed_model_origins", [LOOPBACK_URL]),
            patch.object(
                draft_provider_policy, "assert_url_safe", _recording_assert_url_safe
            ),
        ):
            assert_provider_allowed(LOOPBACK_URL, sensitive=False)
        self.assertEqual(calls, [LOOPBACK_URL])


class TestErrorMessageDoesNotLeak(ProviderPolicyTestBase):
    def test_message_omits_allowlist_and_full_rejected_url(self) -> None:
        secret_allowed_origin = "https://configured-secret-provider.internal:8443"
        rejected_url = (
            "https://evil.example.com/v1/secret/path?token=abc123&api_key=zzz"
        )
        with patch.object(
            settings, "draft_allowed_model_origins", [secret_allowed_origin]
        ):
            with self.assertRaises(ProviderPolicyError) as ctx:
                assert_provider_allowed(rejected_url, sensitive=False)
        message = str(ctx.exception)
        self.assertNotIn("configured-secret-provider", message)
        self.assertNotIn("8443", message)
        self.assertNotIn("/v1/secret/path", message)
        self.assertNotIn("token=abc123", message)
        self.assertNotIn("api_key=zzz", message)


class TestProviderSnapshot(unittest.TestCase):
    def test_snapshot_contains_only_non_secret_identifiers(self) -> None:
        class _FakeCircuitBreaker:
            name = "draft-thinking-circuit"

        class _FakeClient:
            base_url = "https://user:s3cr3t-p4ss@api.provider.test:443/v1?api_key=zzz"
            model = "gpt-oss-120b"
            api_key = "sk-should-never-appear"
            authorization = "Bearer sk-should-never-appear"
            _circuit_breaker = _FakeCircuitBreaker()

        snapshot = provider_snapshot(_FakeClient())

        self.assertEqual(set(snapshot.keys()), {"base_host", "model", "logical_mode"})
        self.assertEqual(snapshot["model"], "gpt-oss-120b")
        self.assertEqual(snapshot["logical_mode"], "thinking")
        # No credentials, api key, query string, or auth header anywhere in
        # the snapshot's rendered values.
        rendered = " ".join(snapshot.values())
        self.assertNotIn("s3cr3t-p4ss", rendered)
        self.assertNotIn("zzz", rendered)
        self.assertNotIn("sk-should-never-appear", rendered)
        self.assertNotIn("user:", rendered)
        self.assertNotIn("?api_key", rendered)

    def test_snapshot_infers_instant_logical_mode(self) -> None:
        class _FakeCircuitBreaker:
            name = "draft-instant-circuit"

        class _FakeClient:
            base_url = "http://127.0.0.1:11434"
            model = "small-fast-model"
            _circuit_breaker = _FakeCircuitBreaker()

        snapshot = provider_snapshot(_FakeClient())
        self.assertEqual(snapshot["logical_mode"], "instant")
        self.assertEqual(snapshot["base_host"], "127.0.0.1:11434")


class TestDraftHttpClientKwargs(unittest.TestCase):
    def test_follow_redirects_is_false(self) -> None:
        kwargs = draft_http_client_kwargs()
        self.assertEqual(kwargs.get("follow_redirects"), False)


if __name__ == "__main__":
    unittest.main()
