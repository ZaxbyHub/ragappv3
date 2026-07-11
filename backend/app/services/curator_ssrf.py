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
    """Read ALLOW_LOCAL_CURATOR=1 at call time so tests can flip it."""
    return os.environ.get(_LOCAL_OPT_IN_ENV, "").strip() in ("1", "true", "True")


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
