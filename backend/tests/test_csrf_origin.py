"""Unit tests for the CSRF Origin/Referer validation (issue #242 / #248, A6).

The check is defense-in-depth on top of the double-submit cookie + SameSite=Lax.
It must be conservative: only reject when a browser-supplied Origin/Referer is
present AND a non-wildcard allowlist is configured AND the source is not trusted.
"""

import unittest
from unittest.mock import patch

from fastapi import HTTPException
from starlette.datastructures import Headers

from app.security import (
    _csrf_allowed_origins,
    _normalize_origin,
    _validate_csrf_origin,
)


class _FakeRequest:
    """Minimal stand-in exposing a case-insensitive ``headers`` like Starlette."""

    def __init__(self, headers: dict[str, str]):
        self.headers = Headers(headers)


class TestNormalizeOrigin(unittest.TestCase):
    def test_reduces_url_to_scheme_and_authority(self):
        self.assertEqual(
            _normalize_origin("https://app.example.com/some/path?x=1"),
            "https://app.example.com",
        )

    def test_lowercases(self):
        self.assertEqual(
            _normalize_origin("HTTPS://App.Example.COM"),
            "https://app.example.com",
        )

    def test_preserves_nonstandard_port(self):
        self.assertEqual(
            _normalize_origin("http://localhost:5173"),
            "http://localhost:5173",
        )

    def test_strips_default_ports(self):
        self.assertEqual(
            _normalize_origin("https://app.example.com:443"),
            "https://app.example.com",
        )
        self.assertEqual(
            _normalize_origin("http://app.example.com:80"),
            "http://app.example.com",
        )
        # A non-default port that merely ends in the digits must be preserved.
        self.assertEqual(
            _normalize_origin("https://app.example.com:8443"),
            "https://app.example.com:8443",
        )

    def test_returns_none_for_unparseable(self):
        self.assertIsNone(_normalize_origin("not-a-url"))
        self.assertIsNone(_normalize_origin(""))


class TestAllowedOrigins(unittest.TestCase):
    def test_derived_from_cors_origins(self):
        with patch(
            "app.security.settings.backend_cors_origins",
            ["http://localhost:5173", "https://app.example.com/"],
        ):
            self.assertEqual(
                _csrf_allowed_origins(),
                {"http://localhost:5173", "https://app.example.com"},
            )

    def test_wildcard_disables_enforcement(self):
        with patch("app.security.settings.backend_cors_origins", ["*"]):
            self.assertIsNone(_csrf_allowed_origins())

    def test_empty_disables_enforcement(self):
        with patch("app.security.settings.backend_cors_origins", []):
            self.assertIsNone(_csrf_allowed_origins())


class TestValidateCsrfOrigin(unittest.TestCase):
    ALLOWLIST = ["http://localhost:5173", "https://app.example.com"]

    def test_allows_when_no_origin_or_referer(self):
        # Non-browser / legacy clients: do not block.
        with patch("app.security.settings.backend_cors_origins", self.ALLOWLIST):
            _validate_csrf_origin(_FakeRequest({}))  # must not raise

    def test_allows_trusted_origin(self):
        with patch("app.security.settings.backend_cors_origins", self.ALLOWLIST):
            _validate_csrf_origin(
                _FakeRequest({"origin": "https://app.example.com"})
            )

    def test_rejects_untrusted_origin(self):
        with patch("app.security.settings.backend_cors_origins", self.ALLOWLIST):
            with self.assertRaises(HTTPException) as ctx:
                _validate_csrf_origin(
                    _FakeRequest({"origin": "https://evil.example.com"})
                )
            self.assertEqual(ctx.exception.status_code, 403)

    def test_falls_back_to_referer(self):
        with patch("app.security.settings.backend_cors_origins", self.ALLOWLIST):
            # Trusted referer (full URL with path) is allowed.
            _validate_csrf_origin(
                _FakeRequest({"referer": "https://app.example.com/chat"})
            )
            # Untrusted referer is rejected.
            with self.assertRaises(HTTPException):
                _validate_csrf_origin(
                    _FakeRequest({"referer": "https://evil.example.com/x"})
                )

    def test_origin_takes_precedence_over_referer(self):
        with patch("app.security.settings.backend_cors_origins", self.ALLOWLIST):
            # Trusted Origin wins even if Referer would be untrusted.
            _validate_csrf_origin(
                _FakeRequest(
                    {
                        "origin": "https://app.example.com",
                        "referer": "https://evil.example.com/x",
                    }
                )
            )

    def test_wildcard_allows_any_origin(self):
        with patch("app.security.settings.backend_cors_origins", ["*"]):
            _validate_csrf_origin(
                _FakeRequest({"origin": "https://evil.example.com"})
            )  # must not raise


if __name__ == "__main__":
    unittest.main()
