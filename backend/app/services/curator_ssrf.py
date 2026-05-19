"""SSRF guard for the optional LLM Wiki Curator endpoint.

The curator URL is user-supplied and reaches outbound HTTP. We default-deny
private/loopback targets so a misconfigured (or malicious) admin can't turn
the backend into a port scanner of the internal network. Local-model setups
are the intended use case, so an explicit ``ALLOW_LOCAL_CURATOR=1``
environment opt-in re-enables RFC1918 / loopback / link-local destinations.

Used by the curator-test route (PR B) and the curator client (PR C).
"""

from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import urlparse

from .ssrf import _is_blocked_address


class CuratorURLBlocked(Exception):
    """Raised when a curator URL fails the SSRF guard.

    The message is safe to surface in API responses — it never echoes
    the resolved IP back to the client (only the offending hostname),
    so we don't expose internal DNS data.
    """


_LOCAL_OPT_IN_ENV = "ALLOW_LOCAL_CURATOR"
_LOCAL_OPT_IN_HINT = (
    "Local curator endpoints require ALLOW_LOCAL_CURATOR=1."
)


def _local_opt_in_enabled() -> bool:
    """Read ALLOW_LOCAL_CURATOR=1 at call time so tests can flip it."""
    return os.environ.get(_LOCAL_OPT_IN_ENV, "").strip() in ("1", "true", "True")


def assert_curator_url_safe(url: str) -> None:
    """Validate a curator URL and raise ``CuratorURLBlocked`` on rejection.

    Wraps the shared ssrf.assert_url_safe() logic but uses curator-specific
    exception type and ALLOW_LOCAL_CURATOR opt-in (independent of ALLOW_LOCAL_SERVICES).
    """
    if not url or not url.strip():
        raise CuratorURLBlocked("Curator URL is empty.")

    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https"):
        raise CuratorURLBlocked(
            f"Curator URL scheme must be http or https (got {parsed.scheme!r})."
        )
    if parsed.username or parsed.password:
        raise CuratorURLBlocked(
            "Curator URL must not embed credentials (user:pass@host)."
        )
    host = parsed.hostname
    if not host:
        raise CuratorURLBlocked("Curator URL has no hostname.")

    # Hostname literal IPs short-circuit DNS.
    try:
        ipaddress.ip_address(host)
        candidates = [host]
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, parsed.port or 80, type=socket.SOCK_STREAM)
        except OSError as e:
            raise CuratorURLBlocked(
                f"Curator URL host {host!r} did not resolve: {e}"
            ) from e
        candidates = sorted({info[4][0] for info in infos})
        if not candidates:
            raise CuratorURLBlocked(f"Curator URL host {host!r} resolved to nothing.")

    blocked = [ip for ip in candidates if _is_blocked_address(ip)]
    if blocked and not _local_opt_in_enabled():
        # Don't echo the resolved IPs back — saying "private/loopback"
        # is enough for the operator and avoids leaking internal DNS.
        raise CuratorURLBlocked(
            f"Curator URL host {host!r} resolves to a private / loopback / "
            f"link-local address. {_LOCAL_OPT_IN_HINT}"
        )
