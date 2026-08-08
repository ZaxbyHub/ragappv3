"""Draft Room provider-origin enforcement (issue #436 §6, SPEC §9.2).

Compatibility wrapper over the generic provider policy in
:mod:`app.services.model_provider_policy` (issue #461). This module keeps its own
module-level ``assert_url_safe`` binding and its own two-gate ``assert_provider_allowed``
body so Draft Room behavior is byte-identical and its ordering/parity tests keep passing
unchanged; only the pure helpers (origin/allowlist parsing, snapshot, client kwargs) are
shared with the generic core, so the fail-closed exact-origin invariants and redaction
guarantees cannot drift between Draft Room and future multimodal providers.

Serves as a complementary gate on top of :mod:`app.services.ssrf`: the allowlist runs
BEFORE DNS, and the SSRF blocklist is a mandatory second gate that only runs for an
already-allowlisted origin.
"""

from __future__ import annotations

from app.config import settings
from app.services.model_provider_policy import (
    ProviderPolicyError,
    _parse_allowlist,
    _parse_strict_origin,
    http_client_kwargs,
    model_provider_snapshot,
)
from app.services.ssrf import URLBlocked, assert_url_safe

__all__ = [
    "ProviderPolicyError",
    "assert_provider_allowed",
    "provider_snapshot",
    "draft_http_client_kwargs",
]

# Literal loopback hostnames SPEC §9.2 permits over cleartext for the sensitive tier
# ONLY when that exact origin is itself present in the sensitive allowlist.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

_DEFAULT_PORTS = {"http": 80, "https": 443}


def assert_provider_allowed(url: str, *, sensitive: bool) -> None:
    """Enforce the Draft Room provider-origin allowlist for ``url`` (SPEC §9.2).

    Must be called BEFORE a compile job is enqueued and again immediately before
    every model call during that job, so a mid-job policy revocation is honored
    before the next call rather than only at enqueue time.

    Composes with (does not replace) :func:`app.services.ssrf.assert_url_safe`; the
    SSRF blocklist and this allowlist are both mandatory gates. ORDER MATTERS and the
    allowlist runs FIRST (lexical, no DNS) so a host this policy rejects never causes
    an outbound DNS lookup. ``sensitive`` additionally requires HTTPS except a
    literal loopback origin present in ``settings.draft_sensitive_allowed_model_origins``;
    loopback also needs ``ALLOW_LOCAL_SERVICES=1`` for the SSRF gate.

    Raises:
        ProviderPolicyError: with a stable ``code`` (``provider_scheme_not_allowed``,
            ``provider_origin_not_allowed``, ``provider_policy_misconfigured``). The
            message never contains the configured allowlist or the URL's full form.
    """
    # Gate 1: origin parsing + allowlist membership (lexical; no DNS).
    scheme, host, port = _parse_strict_origin(url, allow_path=True)

    tier = "sensitive" if sensitive else "ordinary"
    allowlist_setting = (
        settings.draft_sensitive_allowed_model_origins
        if sensitive
        else settings.draft_allowed_model_origins
    )
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

    # Gate 2: the shared SSRF blocklist. Runs only for an already-allowlisted origin,
    # so no DNS lookup is ever issued for a host this policy rejects. Its URLBlocked
    # is re-raised as ProviderPolicyError; `from None` keeps resolved-address detail
    # out of the propagated traceback.
    try:
        assert_url_safe(url)
    except URLBlocked as exc:
        raise ProviderPolicyError(
            f"Provider origin failed SSRF safety checks (host={host!r}): {exc}",
            code="provider_origin_not_allowed",
        ) from None


def provider_snapshot(client: object) -> dict[str, str]:
    """Return only NON-SECRET identifiers describing a provider client (SPEC §9.2)."""
    return model_provider_snapshot(client)


def draft_http_client_kwargs() -> dict:
    """Return httpx client kwargs required for Draft Room provider calls."""
    return http_client_kwargs()
