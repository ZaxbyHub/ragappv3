"""Generalized SSRF protection for outbound HTTP requests.

This module provides a shared URL safety validator that can be used by
multiple services. The validator blocks private/loopback/link-local/
multicast/reserved/unspecified IP addresses by default, but offers an
env var opt-in for local development scenarios.

Common patterns:
  - URLBlocked exception: safe to surface in API responses (never
    echoes resolved IPs back to the client).
  - ALLOW_LOCAL_SERVICES=1: enables local service endpoints.
  - Follow redirects with ``follow_redirects=False`` to prevent 30x
    bypass of the SSRF guard.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import urlparse


class URLBlocked(Exception):
    """Raised when a URL fails the SSRF guard.

    The message is safe to surface in API responses — it never echoes
    the resolved IP back to the client (only the offending hostname),
    so we don't expose internal DNS data.
    """


_LOCAL_SERVICES_ENV = "ALLOW_LOCAL_SERVICES"
_LOCAL_SERVICES_HINT = "Local service endpoints require ALLOW_LOCAL_SERVICES=1."


def _local_services_opt_in_enabled() -> bool:
    """Read ALLOW_LOCAL_SERVICES=1 at call time so tests can flip it."""
    return os.environ.get(_LOCAL_SERVICES_ENV, "").strip() in ("1", "true", "True")


def _is_blocked_address(ip: str) -> bool:
    """Return True for private / loopback / link-local / multicast / reserved / unspecified IPs."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        # Unresolvable bytes — treat as blocked rather than fail-open.
        return True
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


def assert_url_safe(url: str) -> None:
    """Validate a URL and raise URLBlocked on rejection.

    Rules:
      - URL must be http:// or https://. Other schemes (file://, data://,
        javascript:) are denied unconditionally.
      - URL may not include credentials (``user:pass@``).
      - The hostname must resolve. If any resolved address is private /
        loopback / link-local / multicast / reserved / unspecified, the
        URL is denied unless ``ALLOW_LOCAL_SERVICES=1`` is set.
      - Empty URL is rejected.

    The caller is expected to also pass ``follow_redirects=False`` to its
    HTTP client so a 30x to a private host can't bypass the guard.
    """
    _check_url(
        url,
        blocked_exc=URLBlocked,
        opt_in_enabled=_local_services_opt_in_enabled,
        opt_in_hint=_LOCAL_SERVICES_HINT,
        label="URL",
    )


def _check_url(
    url: str,
    blocked_exc,
    opt_in_enabled,
    opt_in_hint: str,
    label: str = "URL",
) -> None:
    """Shared SSRF validation core used by assert_url_safe and curator_ssrf.

    Parameters:
      - blocked_exc: exception class to raise on rejection (must accept a str).
      - opt_in_enabled: zero-arg callable evaluated at call time (NOT module
        load) so tests can flip the env var; must return True when local
        destinations are allowed.
      - opt_in_hint: hint string appended to the private-address message.
      - label: noun used in the empty/scheme/credentials/hostname messages
        (e.g. "URL" or "Curator URL").

    Kept network-aware (DNS resolution) and identical in semantics across all
    callers so the curator SSRF guard cannot drift from the general one.
    """
    if not url or not url.strip():
        raise blocked_exc(f"{label} is empty.")

    try:
        parsed = urlparse(url.strip())
        parsed_port = parsed.port
    except ValueError as exc:
        raise blocked_exc(f"{label} is malformed: {exc}") from exc
    if parsed.scheme not in ("http", "https"):
        raise blocked_exc(
            f"{label} scheme must be http or https (got {parsed.scheme!r})."
        )
    if parsed.username or parsed.password:
        raise blocked_exc(
            f"{label} must not embed credentials (user:pass@host)."
        )
    host = parsed.hostname
    if not host:
        raise blocked_exc(f"{label} has no hostname.")

    # Hostname literal IPs short-circuit DNS.
    try:
        ipaddress.ip_address(host)
        candidates = [host]
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, parsed_port or 80, type=socket.SOCK_STREAM)
        except OSError as e:
            raise blocked_exc(
                f"{label} host {host!r} did not resolve: {e}"
            ) from e
        candidates = sorted({info[4][0] for info in infos})
        if not candidates:
            raise blocked_exc(f"{label} host {host!r} resolved to nothing.")

    blocked = [ip for ip in candidates if _is_blocked_address(ip)]
    if blocked and not opt_in_enabled():
        # Don't echo the resolved IPs back — saying "private/loopback"
        # is enough for the operator and avoids leaking internal DNS.
        raise blocked_exc(
            f"{label} host {host!r} resolves to a private / loopback / "
            f"link-local address. {opt_in_hint}"
        )
