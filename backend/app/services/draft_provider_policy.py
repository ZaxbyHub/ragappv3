"""Draft Room provider-origin enforcement (issue #436 §6, SPEC §9.2).

This module is an ADDITIONAL, complementary gate on top of the generic SSRF
guard in :mod:`app.services.ssrf`. ``ssrf.assert_url_safe`` is a *blocklist*:
it denies private/loopback/link-local/reserved addresses unless local
services are explicitly opted in. That is necessary but not sufficient for
Draft Room, which must send project manuscript text and vault passages to a
model endpoint: SPEC §9.2 requires an explicit *allowlist* of exact
``scheme://host:port`` origins the deployment has vetted for that purpose,
with an empty allowlist blocking everything (fail closed).

Two independent tiers exist:

- **ordinary** — checked against ``settings.draft_allowed_model_origins``.
- **sensitive** — checked against
  ``settings.draft_sensitive_allowed_model_origins`` and additionally
  requires HTTPS, except a literal loopback origin (``127.0.0.1``, ``::1``,
  or ``localhost``) that the deployment has explicitly placed in the
  *sensitive* allowlist itself (SPEC.md §9.2: "sensitive requires HTTPS or a
  literal loopback origin ...; cleartext LAN/Docker hostnames remain blocked
  even if listed"). Membership for ordinary does NOT imply membership for
  sensitive and vice versa — the two allowlists are evaluated independently
  against independent settings.

Call ``assert_provider_allowed`` twice per compile: once before the job is
enqueued and once again immediately before every model call (a mid-job
policy change must not be honored past that point). Both calls raise
:class:`ProviderPolicyError`, whose message intentionally omits the
configured allowlist contents and the full rejected URL — it surfaces the
bare hostname at most, matching SPEC §9.2's "never return the allowlist or
raw endpoint to non-admin clients."
"""

from __future__ import annotations

from collections.abc import Sequence
from urllib.parse import urlparse

from app.config import settings
from app.services.ssrf import URLBlocked, assert_url_safe

__all__ = [
    "ProviderPolicyError",
    "assert_provider_allowed",
    "provider_snapshot",
    "draft_http_client_kwargs",
]

# Literal loopback hostnames SPEC §9.2 permits over cleartext for the
# sensitive tier -- ONLY when that exact origin is itself present in the
# sensitive allowlist. This set intentionally excludes any LAN/Docker
# hostname (e.g. "host.docker.internal"): those remain HTTPS-only even if a
# deployment mistakenly lists them.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

_DEFAULT_PORTS = {"http": 80, "https": 443}


class ProviderPolicyError(Exception):
    """Raised when a provider URL fails Draft Room's origin-allowlist policy.

    Attributes:
        code: stable machine-readable error code. One of
            ``"provider_origin_not_allowed"``, ``"provider_scheme_not_allowed"``,
            or ``"provider_policy_misconfigured"``.

    The exception message deliberately never contains the configured
    allowlist contents or the rejected URL in full (path/query/fragment/
    credentials are always stripped); at most the bare hostname is included.
    Treat the message as safe to log or return to a caller, but callers
    should still prefer surfacing only ``code`` to end users.
    """

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


def _parse_strict_origin(url: str, *, allow_path: bool = False) -> tuple[str, str, int]:
    """Parse ``url`` into a normalized ``(scheme, host, port)`` origin tuple.

    Enforces SPEC §9.2's "exact `http[s]://host:port`" contract: rejects
    userinfo, path (other than empty or ``/``), query, and fragment
    components, since any of those would let two different destinations
    compare equal to an allowlist entry (or vice versa) by accident.

    Args:
        url: The URL to parse.
        allow_path: When ``True``, a path/query/fragment is tolerated and
            ignored, and only the origin is returned. Callers check a
            *request* URL (e.g. ``http://host:11434/v1/chat/completions``)
            against an origin allowlist, so rejecting the path outright
            would make the guard unusable at the only call sites that
            matter. Allowlist *entries* are still parsed with the default
            ``allow_path=False`` so a path in configuration remains an
            operator misconfiguration.

    Raises:
        ProviderPolicyError(code="provider_scheme_not_allowed"): scheme is
            not exactly ``http`` or ``https``, or the URL is structurally
            invalid in a way that prevents establishing a scheme/host.
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
    """Parse an origin allowlist setting into origin tuples.

    Accepts either the parsed ``list[str]`` that ``Settings`` produces for
    ``draft_allowed_model_origins`` / ``draft_sensitive_allowed_model_origins``
    or a raw comma-separated string, so the guard behaves identically whether
    it is handed a settings value or a literal env string.

    An empty/``None`` value yields an empty set, which fails every lookup
    closed (SPEC §9.2 / §15: "empty blocks compile"). Any configured entry
    that is not a clean ``scheme://host[:port]`` origin (wildcard, suffix,
    path, query, credentials, bad scheme, ...) is treated as an operator
    misconfiguration and raises rather than being silently skipped, because
    silently dropping a bad entry could leave a deployment believing an
    origin is protected when it is not.

    Raises:
        ProviderPolicyError(code="provider_policy_misconfigured"): a
            non-empty entry could not be parsed as a strict origin, or
            contains a wildcard.
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


def assert_provider_allowed(url: str, *, sensitive: bool) -> None:
    """Enforce the Draft Room provider-origin allowlist for ``url``.

    Must be called BEFORE a compile job is enqueued and again immediately
    before every model call made during that job (SPEC §9.2), so a
    mid-job policy revocation is honored before the next call rather than
    only at enqueue time.

    Composes with (does not replace) :func:`app.services.ssrf.assert_url_safe`:
    the SSRF guard's blocklist and this allowlist are both mandatory gates.

    ORDER MATTERS, and the allowlist runs FIRST:

    * ``assert_url_safe`` performs DNS resolution. Running it first would
      resolve hostnames that are not on the allowlist at all, turning a
      rejected configuration into an outbound DNS lookup for an arbitrary
      host -- a side channel this gate exists to prevent. The allowlist
      check is purely lexical (scheme/host/port) and cannot leak.
    * SPEC §9.2 requires failing closed with a *stable provider-policy
      error*. ``assert_url_safe`` raises ``URLBlocked``, which callers
      classifying non-retryable provider-policy failures would not
      recognize, so its failure is wrapped in ``ProviderPolicyError``.

    Both gates still run for an allowlisted origin; neither is optional.

    OPERATIONAL NOTE -- sensitive-tier loopback: listing a loopback origin in
    ``draft_sensitive_allowed_model_origins`` is necessary but NOT sufficient.
    ``assert_url_safe`` independently blocks loopback/private addresses unless
    the operator sets ``ALLOW_LOCAL_SERVICES=1``. Both are required, which is
    what SPEC §9.2's "literal loopback cleartext explicitly allowed by policy"
    means in this codebase: the allowlist names the origin, and the
    environment opt-in authorizes local destinations.

    Args:
        url: the candidate provider base URL (e.g. an ``LLMClient.base_url``).
        sensitive: True for the "sensitive" draft tier, which is checked
            against the stricter, independent
            ``settings.draft_sensitive_allowed_model_origins`` allowlist
            and additionally requires HTTPS -- except a literal loopback
            origin (``127.0.0.1`` / ``[::1]`` / ``localhost``) that is
            itself explicitly present in that sensitive allowlist (SPEC.md
            §9.2: "sensitive requires HTTPS or a literal loopback origin
            (localhost, 127.0.0.1, or [::1]); cleartext LAN/Docker
            hostnames remain blocked even if listed"). False uses the
            ordinary ``settings.draft_allowed_model_origins`` allowlist
            with no scheme restriction beyond http/https.

    Raises:
        ProviderPolicyError: with a stable ``code`` of
            ``"provider_scheme_not_allowed"``, ``"provider_origin_not_allowed"``,
            or ``"provider_policy_misconfigured"``. The message never
            contains the configured allowlist or the URL's full form -- at
            most the bare hostname.
    """
    # Gate 1: origin parsing (this module's own scheme/shape checks). A
    # request URL legitimately carries a path (``/v1/chat/completions``), so
    # only its origin is compared; allowlist entries stay strictly origin-only.
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
        # Cleartext LAN/Docker hostnames remain blocked even if listed: a
        # non-loopback http origin never reaches this point (raised above),
        # so no allowlist membership can override that HTTPS requirement.

    if (scheme, host, port) not in allowed_origins:
        raise ProviderPolicyError(
            f"Provider origin not allowed (host={host!r}).",
            code="provider_origin_not_allowed",
        )

    # Gate 2: the shared SSRF blocklist (embedded credentials, resolved
    # address safety). Runs only for an already-allowlisted origin, so no
    # DNS lookup is ever issued for a host this policy rejects. Its
    # URLBlocked is re-raised as ProviderPolicyError so callers get the
    # stable provider-policy code SPEC §9.2 requires; the original message
    # already honors the no-leakage contract, and `from None` keeps any
    # resolved-address detail out of the propagated traceback.
    try:
        assert_url_safe(url)
    except URLBlocked as exc:
        raise ProviderPolicyError(
            f"Provider origin failed SSRF safety checks (host={host!r}): {exc}",
            code="provider_origin_not_allowed",
        ) from None


def provider_snapshot(client: object) -> dict[str, str]:
    """Return only NON-SECRET identifiers describing a provider client.

    Safe for audit logs, SSE messages, and API responses per SPEC §9.2
    ("Persist provider kind, model name, ... Never persist API keys,
    authorization headers, or secret-bearing endpoint query strings.") and
    §9.2's "never return the allowlist or raw endpoint to non-admin
    clients."

    Reads attributes duck-typed off ``client`` (expected to be an
    ``app.services.llm_client.LLMClient`` instance or compatible) rather
    than importing that module, to avoid a hard dependency on its shape:

    - ``client.base_url`` -> reduced to bare ``host`` or ``host:port``
      (scheme, path, query, credentials all dropped).
    - ``client.model`` -> the model name string.
    - ``client._circuit_breaker.name`` -> used only to infer a coarse
      ``logical_mode`` ("instant" or "thinking") from the naming convention
      established in ``llm_client.create_instant_client`` /
      ``create_thinking_client``; falls back to ``"unknown"``.

    Returns:
        dict[str, str] with keys ``"base_host"``, ``"model"``, and
        ``"logical_mode"``. Never includes an API key, full URL with
        credentials, or auth header -- those are not read from ``client``
        at all.
    """
    base_url = getattr(client, "base_url", "") or ""
    try:
        parsed = urlparse(str(base_url))
        host = parsed.hostname or ""
        base_host = f"{host}:{parsed.port}" if parsed.port else host
    except ValueError:
        base_host = ""

    model = str(getattr(client, "model", "") or "")

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


def draft_http_client_kwargs() -> dict:
    """Return httpx client kwargs required for Draft Room provider calls.

    SPEC §9.2: "Model HTTP clients set `follow_redirects=False`. Any 3xx
    fails with `provider_redirect_blocked`; never replay manuscript content
    or authorization headers to a redirect target." ``ssrf.py`` documents
    this as caller discipline rather than something it can enforce itself;
    this helper exists so that discipline cannot be silently forgotten by a
    call site -- always spread its result into the client constructor:

        httpx.AsyncClient(**draft_http_client_kwargs(), timeout=...)

    Returns:
        dict with ``follow_redirects: False``. Callers that observe a 3xx
        response from a provider must translate it into a
        ``provider_redirect_blocked`` failure themselves (this helper only
        prevents httpx from transparently following it).
    """
    return {"follow_redirects": False}
