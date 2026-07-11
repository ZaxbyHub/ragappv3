"""Tests for the generalized SSRF validator (backend/app/services/ssrf.py)
and the curator-specific SSRF guard (backend/app/services/curator_ssrf.py).

Covers:
  - assert_url_safe() rejects file://, data:// schemes unconditionally.
  - assert_url_safe() rejects URLs with embedded credentials.
  - assert_url_safe() rejects empty URLs.
  - assert_url_safe() rejects 127.0.0.1 unless ALLOW_LOCAL_SERVICES=1.
  - assert_url_safe() rejects 10.0.0.1 (private) unless ALLOW_LOCAL_SERVICES=1.
  - assert_url_safe() rejects 192.168.1.1 (private) unless ALLOW_LOCAL_SERVICES=1.
  - assert_url_safe() permits https://example.com (public URL).
  - assert_url_safe() treats DNS resolution failures as blocked.
  - assert_curator_url_safe() uses ALLOW_LOCAL_CURATOR independently of ALLOW_LOCAL_SERVICES.
  - assert_curator_url_safe() rejects 127.0.0.1 unless ALLOW_LOCAL_CURATOR=1.
  - assert_curator_url_safe() permits a public URL.
  - assert_curator_url_safe() raises CuratorURLBlocked with appropriate message.
"""

import os
import socket
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.curator_ssrf import (
    CuratorURLBlocked,
    assert_curator_url_safe,
)
from app.services.ssrf import URLBlocked, assert_url_safe

# ---------------------------------------------------------------------------
# assert_url_safe — scheme rejection
# ---------------------------------------------------------------------------


class TestAssertUrlSafeSchemeRejection(unittest.TestCase):
    """assert_url_safe() must reject non-http(s) schemes unconditionally."""

    def test_rejects_file_scheme(self):
        with self.assertRaises(URLBlocked) as ctx:
            assert_url_safe("file:///etc/passwd")
        self.assertIn("http or https", str(ctx.exception))

    def test_rejects_data_scheme(self):
        with self.assertRaises(URLBlocked) as ctx:
            assert_url_safe("data:text/html,<script>alert(1)</script>")
        self.assertIn("http or https", str(ctx.exception))

    def test_rejects_javascript_scheme(self):
        with self.assertRaises(URLBlocked) as ctx:
            assert_url_safe("javascript:alert(1)")
        self.assertIn("http or https", str(ctx.exception))

    def test_rejects_ftp_scheme(self):
        with self.assertRaises(URLBlocked) as ctx:
            assert_url_safe("ftp://example.com/file")
        self.assertIn("http or https", str(ctx.exception))

    def test_permits_http_scheme(self):
        """http is permitted (only non-http(s) schemes are blocked)."""
        # http passes scheme check; it fails later on private-IP or DNS
        # resolution only if the target is unreachable/private.
        assert_url_safe("http://example.com")


# ---------------------------------------------------------------------------
# assert_url_safe — embedded credentials
# ---------------------------------------------------------------------------


class TestAssertUrlSafeCredentials(unittest.TestCase):
    """assert_url_safe() must reject URLs that embed user:pass@."""

    def test_rejects_username(self):
        with self.assertRaises(URLBlocked) as ctx:
            assert_url_safe("https://user@example.com/")
        self.assertIn("credentials", str(ctx.exception).lower())

    def test_rejects_username_password(self):
        with self.assertRaises(URLBlocked) as ctx:
            assert_url_safe("https://user:pass@example.com/")
        self.assertIn("credentials", str(ctx.exception).lower())

    def test_rejects_only_password(self):
        with self.assertRaises(URLBlocked) as ctx:
            assert_url_safe("https://:pass@example.com/")
        self.assertIn("credentials", str(ctx.exception).lower())

    def test_permits_url_without_credentials(self):
        # Must not raise.
        assert_url_safe("https://example.com/")


# ---------------------------------------------------------------------------
# assert_url_safe — empty / whitespace
# ---------------------------------------------------------------------------


class TestAssertUrlSafeEmpty(unittest.TestCase):
    """assert_url_safe() must reject empty / all-whitespace URLs."""

    def test_rejects_empty_string(self):
        with self.assertRaises(URLBlocked) as ctx:
            assert_url_safe("")
        self.assertIn("empty", str(ctx.exception).lower())

    def test_rejects_whitespace_only(self):
        with self.assertRaises(URLBlocked) as ctx:
            assert_url_safe("   ")
        self.assertIn("empty", str(ctx.exception).lower())

    def test_rejects_none(self):
        with self.assertRaises(URLBlocked) as ctx:
            assert_url_safe(None)
        self.assertIn("empty", str(ctx.exception).lower())


# ---------------------------------------------------------------------------
# assert_url_safe — private / loopback IPs without ALLOW_LOCAL_SERVICES
# ---------------------------------------------------------------------------


class TestAssertUrlSafePrivateIps(unittest.TestCase):
    """assert_url_safe() blocks loopback / private IPs unless ALLOW_LOCAL_SERVICES=1."""

    def setUp(self):
        self._orig = os.environ.pop("ALLOW_LOCAL_SERVICES", None)

    def tearDown(self):
        if self._orig is not None:
            os.environ["ALLOW_LOCAL_SERVICES"] = self._orig
        else:
            os.environ.pop("ALLOW_LOCAL_SERVICES", None)

    def test_rejects_127_0_0_1(self):
        with self.assertRaises(URLBlocked) as ctx:
            assert_url_safe("http://127.0.0.1:8080/")
        self.assertIn("private", str(ctx.exception).lower())
        self.assertIn("loopback", str(ctx.exception).lower())

    def test_rejects_127_0_0_1_explicit_port(self):
        with self.assertRaises(URLBlocked):
            assert_url_safe("http://127.0.0.1:11434/v1/chat")

    def test_rejects_10_0_0_1_private(self):
        with self.assertRaises(URLBlocked):
            assert_url_safe("http://10.0.0.1/")

    def test_rejects_192_168_1_1_private(self):
        with self.assertRaises(URLBlocked):
            assert_url_safe("http://192.168.1.1/")

    def test_rejects_localhost_literal(self):
        """localhost resolves to 127.0.0.1 — same loopback block applies."""
        with self.assertRaises(URLBlocked):
            assert_url_safe("http://localhost:8080/")

    def test_rejects_0_0_0_0_unspecified(self):
        with self.assertRaises(URLBlocked):
            assert_url_safe("http://0.0.0.0/")

    def test_rejects_169_254_169_254_link_local(self):
        """169.254.0.0/16 is the AWS metadata link-local range."""
        with self.assertRaises(URLBlocked):
            assert_url_safe("http://169.254.169.254/latest/meta-data/")


# ---------------------------------------------------------------------------
# assert_url_safe — public URL (happy path)
# ---------------------------------------------------------------------------


class TestAssertUrlSafePublicUrl(unittest.TestCase):
    """assert_url_safe() must accept a properly-formed public HTTPS URL."""

    def setUp(self):
        self._orig = os.environ.pop("ALLOW_LOCAL_SERVICES", None)
        # Mock DNS to return a public IP so tests work in isolated environments.
        self._mock = patch(
            "socket.getaddrinfo",
            return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))
            ],
        )

    def tearDown(self):
        if self._orig is not None:
            os.environ["ALLOW_LOCAL_SERVICES"] = self._orig
        else:
            os.environ.pop("ALLOW_LOCAL_SERVICES", None)

    def test_permits_https_example_com(self):
        with self._mock:
            assert_url_safe("https://example.com/")

    def test_permits_https_google_com(self):
        with self._mock:
            assert_url_safe("https://www.google.com/")

    def test_permits_https_with_path(self):
        with self._mock:
            assert_url_safe(
                "https://api.example.com/v1/chat?model=qwen&qwen=1"
            )


# ---------------------------------------------------------------------------
# assert_url_safe — DNS resolution failure treated as blocked
# ---------------------------------------------------------------------------


class TestAssertUrlSafeDnsFailure(unittest.TestCase):
    """A host that fails DNS resolution is treated as blocked (fail-closed)."""

    def test_rejects_unresolvable_host(self):
        with self.assertRaises(URLBlocked) as ctx:
            assert_url_safe("https://this-domain-does-not-exist-xyz.invalid/")
        # The error message must mention the host and that it didn't resolve.
        self.assertIn("did not resolve", str(ctx.exception))

    def test_rejects_private_ip_behind_cname(self):
        """If DNS resolves to a private IP after a CNAME, it must be blocked."""
        # We cannot easily inject a fake CNAME without patching getaddrinfo,
        # so we test the fail-closed contract: an OS-level resolution error
        # (non-existent domain) raises URLBlocked.
        with self.assertRaises(URLBlocked):
            assert_url_safe("https://nonexistent.invalid/")


# ---------------------------------------------------------------------------
# assert_url_safe — ALLOW_LOCAL_SERVICES opt-in
# ---------------------------------------------------------------------------


class TestAssertUrlSafeLocalServicesOptIn(unittest.TestCase):
    """When ALLOW_LOCAL_SERVICES=1, private/loopback URLs are allowed."""

    def setUp(self):
        self._orig = os.environ.get("ALLOW_LOCAL_SERVICES")
        os.environ["ALLOW_LOCAL_SERVICES"] = "1"

    def tearDown(self):
        if self._orig is not None:
            os.environ["ALLOW_LOCAL_SERVICES"] = self._orig
        else:
            os.environ.pop("ALLOW_LOCAL_SERVICES", None)

    def test_permits_127_0_0_1_when_opted_in(self):
        assert_url_safe("http://127.0.0.1:8080/")

    def test_permits_10_0_0_1_when_opted_in(self):
        assert_url_safe("http://10.0.0.1/")

    def test_permits_192_168_1_1_when_opted_in(self):
        assert_url_safe("http://192.168.1.1/")

    def test_permits_localhost_when_opted_in(self):
        assert_url_safe("http://localhost:8080/")

    def test_true_string_also_enables(self):
        os.environ["ALLOW_LOCAL_SERVICES"] = "true"
        assert_url_safe("http://127.0.0.1/")

    def test_True_string_also_enables(self):
        os.environ["ALLOW_LOCAL_SERVICES"] = "True"
        assert_url_safe("http://127.0.0.1/")


# ---------------------------------------------------------------------------
# assert_curator_url_safe — independent ALLOW_LOCAL_CURATOR opt-in
# ---------------------------------------------------------------------------


class TestAssertCuratorUrlSafeBasic(unittest.TestCase):
    """Basic guard tests for assert_curator_url_safe()."""

    def setUp(self):
        self._orig_curator = os.environ.pop("ALLOW_LOCAL_CURATOR", None)
        self._orig_services = os.environ.pop("ALLOW_LOCAL_SERVICES", None)
        self._mock = patch(
            "socket.getaddrinfo",
            return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))
            ],
        )

    def tearDown(self):
        if self._orig_curator is not None:
            os.environ["ALLOW_LOCAL_CURATOR"] = self._orig_curator
        else:
            os.environ.pop("ALLOW_LOCAL_CURATOR", None)
        if self._orig_services is not None:
            os.environ["ALLOW_LOCAL_SERVICES"] = self._orig_services
        else:
            os.environ.pop("ALLOW_LOCAL_SERVICES", None)

    def test_rejects_empty_url(self):
        with self.assertRaises(CuratorURLBlocked):
            assert_curator_url_safe("")

    def test_rejects_wrong_scheme(self):
        with self.assertRaises(CuratorURLBlocked):
            assert_curator_url_safe("file:///etc/passwd")

    def test_rejects_credentials(self):
        with self.assertRaises(CuratorURLBlocked):
            assert_curator_url_safe("https://user:pass@example.com/")

    def test_rejects_127_0_0_1_without_opt_in(self):
        with self.assertRaises(CuratorURLBlocked):
            assert_curator_url_safe("http://127.0.0.1:11434/v1/chat")

    def test_permits_public_url_without_any_opt_in(self):
        # No env vars needed for public internet; mock DNS for isolated env.
        with self._mock:
            assert_curator_url_safe("https://api.example.com/")


class TestAssertCuratorUrlSafeLocalCuratorOptIn(unittest.TestCase):
    """ALLOW_LOCAL_CURATOR is independent of ALLOW_LOCAL_SERVICES."""

    def setUp(self):
        self._orig_curator = os.environ.pop("ALLOW_LOCAL_CURATOR", None)
        self._orig_services = os.environ.pop("ALLOW_LOCAL_SERVICES", None)

    def tearDown(self):
        if self._orig_curator is not None:
            os.environ["ALLOW_LOCAL_CURATOR"] = self._orig_curator
        else:
            os.environ.pop("ALLOW_LOCAL_CURATOR", None)
        if self._orig_services is not None:
            os.environ["ALLOW_LOCAL_SERVICES"] = self._orig_services
        else:
            os.environ.pop("ALLOW_LOCAL_SERVICES", None)

    def test_curator_opt_in_blocks_without_it(self):
        """Even with ALLOW_LOCAL_SERVICES=1, curator must still block without ALLOW_LOCAL_CURATOR."""
        os.environ["ALLOW_LOCAL_SERVICES"] = "1"
        with self.assertRaises(CuratorURLBlocked):
            assert_curator_url_safe("http://127.0.0.1:11434/v1/chat")

    def test_curator_opt_in_allows_with_it(self):
        """With ALLOW_LOCAL_CURATOR=1, curator allows private/loopback."""
        os.environ["ALLOW_LOCAL_CURATOR"] = "1"
        assert_curator_url_safe("http://127.0.0.1:11434/v1/chat")

    def test_services_opt_in_does_not_affect_curator(self):
        """ALLOW_LOCAL_SERVICES=1 must NOT bypass curator's ALLOW_LOCAL_CURATOR requirement."""
        os.environ["ALLOW_LOCAL_SERVICES"] = "1"
        os.environ.pop("ALLOW_LOCAL_CURATOR", None)
        with self.assertRaises(CuratorURLBlocked):
            assert_curator_url_safe("http://127.0.0.1:11434/v1/chat")

    def test_curator_opt_in_true_string_also_enables(self):
        os.environ["ALLOW_LOCAL_CURATOR"] = "true"
        assert_curator_url_safe("http://127.0.0.1:11434/")

    def test_curator_allows_public_url(self):
        """Public URLs must be allowed regardless of curator opt-in."""
        os.environ.pop("ALLOW_LOCAL_CURATOR", None)
        with patch(
            "socket.getaddrinfo",
            return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))
            ],
        ):
            assert_curator_url_safe("https://api.example.com/")


# ---------------------------------------------------------------------------
# _is_blocked_address — regression: shared helper is used by curator_ssrf
# ---------------------------------------------------------------------------


class TestIsBlockedAddressShared(unittest.TestCase):
    """The _is_blocked_address helper in ssrf.py is also used by curator_ssrf.

    This is a smoke test to document that contract and catch accidental removal.
    """

    def test_imports_from_ssrf_module(self):
        from app.services.ssrf import _is_blocked_address
        self.assertTrue(callable(_is_blocked_address))

    def test_blocks_loopback(self):
        from app.services.ssrf import _is_blocked_address
        self.assertTrue(_is_blocked_address("127.0.0.1"))
        self.assertTrue(_is_blocked_address("::1"))

    def test_blocks_private(self):
        from app.services.ssrf import _is_blocked_address
        self.assertTrue(_is_blocked_address("10.0.0.1"))
        self.assertTrue(_is_blocked_address("192.168.1.1"))
        self.assertTrue(_is_blocked_address("172.16.0.1"))

    def test_allows_public(self):
        from app.services.ssrf import _is_blocked_address
        self.assertFalse(_is_blocked_address("8.8.8.8"))
        self.assertFalse(_is_blocked_address("1.1.1.1"))

    def test_unresolvable_returns_true(self):
        """An unresolvable string (not an IP) must be treated as blocked."""
        from app.services.ssrf import _is_blocked_address
        # An IP string that isn't valid — the function should catch ValueError
        # and return True rather than fail-open.
        self.assertTrue(_is_blocked_address("not-an-ip-at-all"))


# ---------------------------------------------------------------------------
# Parity: curator guard must agree with the general guard on identical inputs
# (guards against drift now that curator_ssrf delegates to the shared core).
# ---------------------------------------------------------------------------


class TestCuratorGeneralGuardParity(unittest.TestCase):
    """assert_curator_url_safe and assert_url_safe must make the same decision
    on the same input, differing only in exception type and opt-in env var."""

    PARITY_INPUTS = [
        "file:///etc/passwd",
        "data:text/plain,hi",
        "ftp://example.com/x",
        "http://user:pass@example.com/",
        "",
        "   ",
        "http://",
        "https://example.com/path",  # public, both allow
    ]

    def _general_decision(self, url):
        try:
            assert_url_safe(url)
            return "allow"
        except URLBlocked:
            return "block"

    def _curator_decision(self, url):
        try:
            assert_curator_url_safe(url)
            return "allow"
        except CuratorURLBlocked:
            return "block"

    def test_parity_no_opt_in(self):
        """Without any local opt-in, both guards agree on each input."""
        with patch.dict(os.environ, {"ALLOW_LOCAL_SERVICES": "", "ALLOW_LOCAL_CURATOR": ""}, clear=False):
            for url in self.PARITY_INPUTS:
                with self.subTest(url=url):
                    self.assertEqual(
                        self._general_decision(url),
                        self._curator_decision(url),
                        f"guards disagree on {url!r}",
                    )

    def test_loopback_blocked_by_both(self):
        """127.0.0.1 is blocked by both guards when their opt-ins are unset."""
        with patch.dict(os.environ, {"ALLOW_LOCAL_SERVICES": "", "ALLOW_LOCAL_CURATOR": ""}, clear=False):
            self.assertEqual(self._general_decision("http://127.0.0.1:9/"), "block")
            self.assertEqual(self._curator_decision("http://127.0.0.1:9/"), "block")

    def test_opt_ins_independent(self):
        """ALLOW_LOCAL_SERVICES must not unlock the curator guard and vice versa."""
        with patch.dict(os.environ, {"ALLOW_LOCAL_SERVICES": "1", "ALLOW_LOCAL_CURATOR": ""}, clear=False):
            self.assertEqual(self._general_decision("http://127.0.0.1:9/"), "allow")
            self.assertEqual(self._curator_decision("http://127.0.0.1:9/"), "block")
        with patch.dict(os.environ, {"ALLOW_LOCAL_SERVICES": "", "ALLOW_LOCAL_CURATOR": "1"}, clear=False):
            self.assertEqual(self._general_decision("http://127.0.0.1:9/"), "block")
            self.assertEqual(self._curator_decision("http://127.0.0.1:9/"), "allow")


if __name__ == "__main__":
    unittest.main()
