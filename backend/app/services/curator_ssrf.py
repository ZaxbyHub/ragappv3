"""SSRF guard for the optional LLM Wiki Curator endpoint.

The curator URL is user-supplied and reaches outbound HTTP. We default-deny
private/loopback targets so a misconfigured (or malicious) admin can't turn
the backend into a port scanner of the internal network. Local-model setups
are the intended use case, so an explicit ``ALLOW_LOCAL_CURATOR=1``
environment opt-in re-enables RFC1918 / loopback / link-local destinations.

Used by the curator-test route (PR B) and the curator client (PR C).
"""

from __future__ import annotations

import os

from .ssrf import _check_url


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
    """Read the ALLOW_LOCAL_CURATOR opt-in at call time.

    Precedence (issue #726, T1-02-K-12): a key PRESENT in the process
    environment wins verbatim — that preserves today's dynamic flipping (a
    test or operator exporting the var after startup) exactly. Only when the
    key is absent from the environment does the Settings field apply, which
    pydantic-settings populates from ``.env`` — closing the gap where a bare
    (non-compose) run with only ``.env`` set was silently ignored.
    """
    if _LOCAL_OPT_IN_ENV in os.environ:
        return os.environ.get(_LOCAL_OPT_IN_ENV, "").strip() in ("1", "true", "True")
    from app.config import settings as _settings

    return bool(_settings.allow_local_curator)


def assert_curator_url_safe(url: str) -> None:
    """Validate a curator URL and raise ``CuratorURLBlocked`` on rejection.

    Delegates to the shared ssrf validation core (``_check_url``) so the
    curator guard cannot drift from the general SSRF guard. Only the exception
    type (``CuratorURLBlocked``) and the local-opt-in env var
    (``ALLOW_LOCAL_CURATOR``) differ; the opt-in callable is read at call time.
    """
    _check_url(
        url,
        blocked_exc=CuratorURLBlocked,
        opt_in_enabled=_local_opt_in_enabled,
        opt_in_hint=_LOCAL_OPT_IN_HINT,
        label="Curator URL",
    )
