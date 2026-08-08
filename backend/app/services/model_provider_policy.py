"""Generic model-provider origin policy for multimodal RAG (issue #461).

This module is the provider-agnostic core extracted from Draft Room's
:mod:`app.services.draft_provider_policy`. It enforces a fail-closed, exact-origin
allowlist gate for ANY external model endpoint that receives vault/document content,
composed with (but never replaced by) the shared SSRF blocklist in
:mod:`app.services.ssrf`:

- Exact ``scheme://host:port`` origin parsed and compared **lexically before DNS**.
- Empty allowlist fails closed (blocks everything).
- SSRF runs as an **independent second gate only after** allowlist approval, so no
  DNS lookup is ever issued for a rejected host.
- Redirect following stays disabled (a redirect target is never implicitly authorized).
- The ``sensitive`` tier requires HTTPS except a literal loopback origin
  (``127.0.0.1`` / ``::1`` / ``localhost``) explicitly present in the allowlist.
- Errors are mapped to stable bounded codes; messages and snapshots never leak
  allowlist contents, raw endpoints, credentials, or secret query data.

SSRF safety is an *egress* blocklist, NOT authorization to transmit content. Only the
combination of explicit global enablement, vault opt-in, exact-origin allowlisting,
and SSRF safety authorizes an outbound call (the caller composes those gates).

Draft Room behavior is preserved with byte-identical semantics via a compatibility
wrapper (:mod:`app.services.draft_provider_policy`) that re-exports the helpers and
wires them to the ``settings.draft_*`` allowlists while keeping its own module-level
``assert_url_safe`` binding (so the existing Draft no-SSRF-for-rejected-origin tests
keep asserting ordering).
"""

from __future__ import annotations

from collections.abc import Sequence
from urllib.parse import urlparse

from app.services.ssrf import URLBlocked, assert_url_safe

__all__ = [
    "ProviderPolicyError",
    "assert_model_provider_allowed",
    "model_provider_snapshot",
    "http_client_kwargs",
]

# Literal loopback hostnames permitted over cleartext for the sensitive tier ONLY
# when that exact origin is itself present in the sensitive allowlist. Excludes any
# LAN/Docker hostname (e.g. "host.docker.internal"), which stays HTTPS-only.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

_DEFAULT_PORTS = {"http": 80, "https": 443}


class ProviderPolicyError(Exception):
    """Raised when a provider URL fails the exact-origin allowlist policy.

    Attributes:
        code: stable machine-readable error code. One of
            ``"provider_origin_not_allowed"``, ``"provider_scheme_not_allowed"``,
            or ``"provider_policy_misconfigured"``.

    The exception message deliberately never contains the configured allowlist
    contents or the rejected URL in full (path/query/fragment/credentials are always
    stripped); at most the bare hostname is included. Treat the message as safe to
    log or return to a caller, but callers should still prefer surfacing only
    ``code`` to end users.
    """

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


def _parse_strict_origin(url: str, *, allow_path: bool = False) -> tuple[str, str, int]:
    """Parse ``url`` into a normalized ``(scheme, host, port)`` origin tuple.

    Enforces the exact ``http[s]://host:port`` contract: rejects userinfo, path
    (other than empty or ``/``), query, and fragment components, since any of those
    would let two different destinations compare equal to an allowlist entry (or
    vice versa) by accident. When ``allow_path`` is True a path/query/fragment is
    tolerated and ignored (for request URLs checked against an origin allowlist);
    allowlist *entries* are always parsed strictly.

    Raises:
        ProviderPolicyError(code="provider_scheme_not_allowed"): scheme is not
            exactly ``http`` or ``https``, or the URL is structurally invalid.
    """
    try:
        parsed = urlparse((url or "").strip())
    except ValueError as exc:
        raise ProviderPolicyError(
            "Provider URL is malformed.", code="provider_scheme_not_allowed"
        ) from exc

    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        raise ProviderPolicyError(
            "Provider URL scheme is not allowed.", code="provider_scheme_not_allowed"
        )
    if parsed.username or parsed.password:
        raise ProviderPolicyError(
            "Provider URL must not embed credentials.",
            code="provider_scheme_not_allowed",
        )
    host = parsed.hostname
    if not host:
        raise ProviderPolicyError(
            "Provider URL has no host.", code="provider_scheme_not_allowed"
        )
    if not allow_path:
        if parsed.path not in ("", "/"):
            raise ProviderPolicyError(
                "Provider URL must not include a path.",
                code="provider_scheme_not_allowed",
            )
        if parsed.query or parsed.fragment:
            raise ProviderPolicyError(
                "Provider URL must not include a query or fragment.",
                code="provider_scheme_not_allowed",
            )

    port = parsed.port
    if port is None:
        port = _DEFAULT_PORTS[scheme]
    return scheme, host.lower(), port


def _parse_allowlist(
    raw: str | Sequence[str] | None, *, tier: str
) -> frozenset[tuple[str, str, int]]:
    """Parse an origin allowlist into origin tuples.

    Accepts either the parsed ``list[str]`` a ``Settings`` model produces or a raw
    comma-separated string, so the guard behaves identically either way. An
    empty/``None`` value yields an empty set, which fails every lookup closed. Any
    configured entry that is not a clean ``scheme://host[:port]`` origin (wildcard,
    suffix, path, query, credentials, bad scheme, ...) is an operator
    misconfiguration and raises rather than being silently skipped.

    Raises:
        ProviderPolicyError(code="provider_policy_misconfigured"): a non-empty
            entry could not be parsed as a strict origin, or contains a wildcard.
    """
    if not raw:
        return frozenset()
    entries = raw.split(",") if isinstance(raw, str) else list(raw)

    origins: set[tuple[str, str, int]] = set()
    for entry in entries:
        entry = entry.strip()
        if not entry:
            continue
        if "*" in entry:
            raise ProviderPolicyError(
                f"{tier} provider allowlist contains an unsupported wildcard entry.",
                code="provider_policy_misconfigured",
            )
        try:
            origins.add(_parse_strict_origin(entry))
        except ProviderPolicyError as exc:
            raise ProviderPolicyError(
                f"{tier} provider allowlist contains an invalid origin entry.",
                code="provider_policy_misconfigured",
            ) from exc
    return frozenset(origins)


def assert_model_provider_allowed(
    url: str,
    *,
    sensitive: bool,
    ordinary_allowlist: str | Sequence[str] | None,
    sensitive_allowlist: str | Sequence[str] | None,
) -> None:
    """Enforce the exact-origin allowlist policy for ``url``.

    Must be called BEFORE a job is enqueued and again immediately before every model
    call, so a mid-job policy revocation is honored before the next call rather than
    only at enqueue time.

    Two mandatory, independent gates, in order:

    * Gate 1 (this module): purely lexical -- parse ``scheme/host/port`` with no DNS,
      enforce sensitive-tier HTTPS (except literal loopback in the sensitive
      allowlist), and require exact membership in the appropriate allowlist. Empty
      allowlist fails closed.
    * Gate 2 (``:func:`app.services.ssrf.assert_url_safe``): the shared SSRF blocklist.
      Runs ONLY for an already-allowlisted origin, so no DNS lookup is ever issued
      for a host this policy rejects. Its ``URLBlocked`` is re-wrapped as
      ``ProviderPolicyError`` (code ``"provider_origin_not_allowed"``) for a stable
      provider-policy code; ``from None`` keeps resolved-address detail out of the
      traceback.

    Args:
        url: candidate provider base URL.
        sensitive: check against ``sensitive_allowlist`` and require HTTPS (except
            literal loopback). False uses ``ordinary_allowlist`` with no scheme
            restriction beyond http/https.
        ordinary_allowlist / sensitive_allowlist: the allowlist values (settings
            or literal env strings) to enforce.

    Raises:
        ProviderPolicyError: with a stable ``code``.
    """
    # Gate 1: origin parsing + allowlist membership (lexical; no DNS).
    scheme, host, port = _parse_strict_origin(url, allow_path=True)

    tier = "sensitive" if sensitive else "ordinary"
    allowlist_setting = sensitive_allowlist if sensitive else ordinary_allowlist
    allowed_origins = _parse_allowlist(allowlist_setting, tier=tier)

    if sensitive:
        is_loopback = host in _LOOPBACK_HOSTS
        if scheme != "https" and not is_loopback:
            raise ProviderPolicyError(
                f"Provider scheme not allowed for sensitive tier (host={host!r}).",
                code="provider_scheme_not_allowed",
            )

    if (scheme, host, port) not in allowed_origins:
        raise ProviderPolicyError(
            f"Provider origin not allowed (host={host!r}).",
            code="provider_origin_not_allowed",
        )

    # Gate 2: SSRF blocklist; only for an allowlisted origin.
    try:
        assert_url_safe(url)
    except URLBlocked as exc:
        raise ProviderPolicyError(
            f"Provider origin failed SSRF safety checks (host={host!r}): {exc}",
            code="provider_origin_not_allowed",
        ) from None


def model_provider_snapshot(client: object) -> dict[str, str]:
    """Return only NON-SECRET identifiers describing a provider client.

    Safe for audit logs and API responses: reads attrs duck-typed off ``client``
    (expected to be an ``app.services.llm_client.LLMClient`` or the multimodal
    provider client), never an API key, auth header, or secret-bearing query string.
    Returns keys ``"base_host"``, ``"model"``, ``"logical_mode"``.
    """
    base_url = getattr(client, "base_url", "") or ""
    try:
        parsed = urlparse(str(base_url))
        host = parsed.hostname or ""
        base_host = f"{host}:{parsed.port}" if parsed.port else host
    except ValueError:
        base_host = ""

    model = str(getattr(client, "model", "") or "")

    # Duck-typed: a client may expose a semantic ``logical_mode`` (e.g. the
    # multimodal provider client reads settings.multimodal_mode). Fall back to
    # the circuit-breaker name used by LLMClient, else "unknown".
    logical_mode = str(getattr(client, "logical_mode", "") or "")
    if not logical_mode:
        cb_name = ""
        circuit_breaker = getattr(client, "_circuit_breaker", None)
        if circuit_breaker is not None:
            cb_name = str(getattr(circuit_breaker, "name", "") or "")
        if "instant" in cb_name:
            logical_mode = "instant"
        elif "thinking" in cb_name:
            logical_mode = "thinking"
        else:
            logical_mode = "unknown"

    return {
        "base_host": base_host,
        "model": model,
        "logical_mode": logical_mode,
    }


def http_client_kwargs() -> dict:
    """Return httpx client kwargs required for any model provider call.

    ``follow_redirects`` must stay ``False``; a redirect target is never implicitly
    authorized. Always spread the result into the client constructor and translate a
    ``3xx`` response into a stable ``provider_redirect_blocked``-style failure.
    """
    return {"follow_redirects": False}
