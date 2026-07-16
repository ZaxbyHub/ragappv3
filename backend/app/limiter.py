"""Shared slowapi limiter with health check whitelist."""

import hmac
import logging
from typing import Any, Callable, Optional

from slowapi import Limiter
from slowapi.util import get_remote_address
from starlette.requests import Request

from app.config import settings
from app.utils.secrets import redact_url

logger = logging.getLogger("rate_limit")


def get_client_ip(request: Request) -> str:
    """Get client IP address for rate limiting.

    When settings.trust_proxy_headers is True, reads the first IP from
    the X-Forwarded-For header. Use this mode ONLY behind a trusted
    reverse proxy (nginx, Caddy, etc.) that sets this header.

    When False (default), uses the direct connection IP
    (request.client.host) for security against IP spoofing.
    Falls back to get_remote_address if client info is unavailable.
    """
    if settings.trust_proxy_headers:
        forwarded_for = request.headers.get("X-Forwarded-For")
        if forwarded_for:
            # X-Forwarded-For can contain multiple IPs: "client, proxy1, proxy2"
            # The first one is the original client
            client_ip = forwarded_for.split(",")[0].strip()
            if client_ip:
                return client_ip
            # Malformed X-Forwarded-For (empty first entry) — fall through
    host = request.client.host if request.client else None
    if host:
        return host
    # Fallback when client info is unavailable (some ASGI transports)
    return get_remote_address(request)


def _should_whitelist(request: Request) -> bool:
    """Check if request should be whitelisted from rate limiting.

    Returns True if the request has a valid X-API-Key header matching the
    configured health_check_api_key, causing the request to bypass rate limits.
    """
    key = request.headers.get("X-API-Key")
    if key and hmac.compare_digest(key, settings.health_check_api_key):
        logger.info(
            "Whitelist hit", extra={
                "client_ip": request.client.host if request.client else None,
                "request_id": request.headers.get("X-Request-ID"),
                "reason": "health-check whitelist",
            }
        )
        return True
    return False


class WhitelistLimiter(Limiter):
    """Custom limiter that exempts health check requests from rate limiting."""

    def _check_request_limit(
        self,
        request: Request,
        endpoint_func: Optional[Callable[..., Any]],
        in_middleware: bool = True,
    ) -> None:
        """Skip rate limiting if the request is whitelisted."""
        if _should_whitelist(request):
            return
        super()._check_request_limit(request, endpoint_func, in_middleware)


def _resolve_storage_uri(redis_url: str) -> str:
    """Resolve the slowapi storage URI for the rate limiter.

    Returns the Redis URL when one is configured so rate-limit counters are
    shared across workers. Falls back to ``memory://`` (per-process) when no
    Redis URL is configured — e.g. CI runs with ``REDIS_URL=""`` and no Redis
    service, where a ``redis://`` URI would raise ``ConfigurationError`` at
    import and fail-closed (503) on the first rate-limited request.
    """
    if redis_url and redis_url.strip():
        return redis_url.strip()
    return "memory://"


def build_limiter(redis_url: str) -> WhitelistLimiter:
    """Construct the shared rate limiter (non-blocking at import).

    ``storage_uri`` shares counters across workers via Redis when configured,
    closing the per-worker isolation defect where ``uvicorn --workers N``
    multiplied every configured limit by N. Unlike ``CSRFManager`` and
    ``EmbeddingService`` (which probe Redis lazily during lifespan startup),
    the limiter CANNOT be constructed in the lifespan: slowapi's
    ``@limiter.limit(...)`` decorators run at module-import time in every route
    file and require a concrete ``Limiter`` instance to register route limits.
    So the limiter is necessarily built at import. To avoid blocking import on
    a Redis probe (up to ~1s when Redis is unreachable), this constructor does
    NOT probe Redis — it passes the configured ``storage_uri`` straight to
    slowapi, which connects lazily on the first rate-limited request.

    ``in_memory_fallback_enabled`` is set whenever a real (non-memory) backend
    is configured so that, if Redis is unreachable at request time, slowapi
    falls back to a per-process limit instead of failing closed (503 on every
    rate-limited request). NOTE: this runtime fallback uses per-process
    MemoryStorage, so under ``--workers N`` with Redis down the effective limit
    is weakened by roughly the worker count until Redis recovers (slowapi
    re-probes with exponential backoff). This trades weaker limiting for
    availability on a Redis blip — a different choice from the CSRF layer's
    fail-closed behavior (security.py) — so a transient Redis outage does not
    take down all state-changing routes. The tradeoff is intentional.

    A consequence of not probing at construction: if Redis is down at startup,
    the limiter is still wired to ``redis://...`` (not ``memory://``), so the
    first request after startup triggers the lazy connect + fallback flip. In
    a multi-worker deployment the documented path (docker-compose gates app
    startup on ``redis: condition: service_healthy``) keeps Redis up at boot,
    so counters are shared in the normal case.
    """
    storage_uri = _resolve_storage_uri(redis_url)
    if storage_uri == "memory://":
        logger.info("Rate limiter using in-memory storage (no Redis configured)")
    else:
        logger.info(
            "Rate limiter wired to Redis for shared counters: %s",
            redact_url(storage_uri),
        )
    return WhitelistLimiter(
        key_func=get_client_ip,
        storage_uri=storage_uri,
        # Only meaningful with a real (non-memory) backend. With memory://
        # (CI / empty REDIS_URL) no fallback limiter is constructed.
        in_memory_fallback_enabled=storage_uri != "memory://",
    )


# Create the limiter instance
limiter = build_limiter(settings.redis_url)
