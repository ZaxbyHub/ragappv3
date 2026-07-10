"""SNI-safe SSRF transport for outbound httpx requests.

The plain SSRF guard (:func:`app.services.ssrf.assert_url_safe`) resolves the
hostname once at guard-call time, validates the resolved IPs, then DISCARDS
them. httpx performs an independent DNS resolution at connection time, which
leaves a TOCTOU (DNS-rebinding) window between the guard and the connect.

This module closes that window for callers that construct an ``httpx`` client
for outbound LLM/embedding/reranker/curator requests, **without** breaking TLS
SNI/cert validation. The approach:

- :class:`SSRFSafeTransport` wraps a real ``httpx.AsyncHTTPTransport``.
- On each request it re-resolves the request hostname (via the same
  :func:`socket.getaddrinfo` the guard uses) and rejects the request if the
  freshly-resolved address is private/loopback/link-local/etc. — *before* the
  underlying transport opens the connection.
- Because the underlying transport still connects by hostname, TLS SNI and
  certificate hostname validation are preserved (unlike a URL-rewrite-to-IP
  approach, which would send the IP as SNI and break https).

This is request-time re-validation rather than IP-pinning: it narrows the
rebinding window from "guard-at-startup → connect-much-later" (seconds to
minutes, exploitable) to "re-resolve → connect" (sub-millisecond for typical
resolver latency — a much smaller window, though not zero). Full IP-pinning
would require a custom resolver not exposed by httpx 0.28.x; the
re-validation approach is the strongest SNI-safe mitigation available on this
httpx version.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Optional
from urllib.parse import urlparse

import httpx

from app.services.ssrf import (
    URLBlocked,
    _is_blocked_address,
    _local_services_opt_in_enabled,
)


def _resolve_host_ips(host: str, port: int) -> list[str]:
    """Return the sorted unique IPs ``getaddrinfo`` resolves ``host`` to."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        return []
    return sorted({info[4][0] for info in infos})


class SSRFSafeTransport(httpx.AsyncBaseTransport):
    """Wraps an :class:`httpx.AsyncHTTPTransport` with request-time SSRF re-validation.

    Pass an instance to ``httpx.AsyncClient(transport=SSRFSafeTransport())``.
    Honors the same ``ALLOW_LOCAL_SERVICES`` opt-in as the guard so local-dev
    configurations are not broken.
    """

    def __init__(self, transport: Optional[httpx.AsyncHTTPTransport] = None):
        self._transport = transport or httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        parsed = urlparse(str(request.url))
        host = parsed.hostname
        if host:
            # Literal-IP hosts short-circuit DNS.
            try:
                ipaddress.ip_address(host)
                candidates = [host]
            except ValueError:
                candidates = _resolve_host_ips(host, parsed.port or 80)
            # Fail-closed to match the startup guard (ssrf.py raises URLBlocked
            # when getaddrinfo returns nothing) — an attacker who can force a
            # transient DNS failure at re-validation must not slip past.
            if not candidates:
                raise URLBlocked(
                    f"URL host {host!r} did not resolve at request time."
                )
            blocked = [ip for ip in candidates if _is_blocked_address(ip)]
            if blocked and not _local_services_opt_in_enabled():
                raise URLBlocked(
                    f"URL host {host!r} resolves to a private / loopback / "
                    f"link-local address."
                )
        return await self._transport.handle_async_request(request)

    async def aclose(self) -> None:
        await self._transport.aclose()


__all__ = ["SSRFSafeTransport"]
