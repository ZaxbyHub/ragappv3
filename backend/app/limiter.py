"""Shared slowapi limiter with health check whitelist."""

import hmac
import logging
from typing import Any, Callable, Optional

from slowapi import Limiter
from slowapi.util import get_remote_address
from starlette.requests import Request

from app.config import settings

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


def _redis_reachable(redis_url: str, timeout: float = 1.0) -> bool:
    """Probe whether the configured Redis is reachable.

    Mirrors the CSRFManager startup probe (security.py): if Redis is not up at
    construction, the limiter cannot share counters, so the caller falls back
    to in-memory storage rather than constructing a RedisStorage whose
    ``reset()`` / first-request would error out in Redis-less environments.
    The probe is a single ``PING`` with a short socket timeout.

    Never raises: a malformed URL (``ValueError``), an unsupported scheme, or
    any connection error is treated as unreachable. This matches the
    CSRFManager pattern (``except Exception`` at security.py) — the probe must
    not crash ``import app.limiter``.
    """
    import contextlib

    import redis

    try:
        client = redis.from_url(
            redis_url, socket_connect_timeout=timeout, socket_timeout=timeout
        )
    except Exception:
        # Malformed URL or unsupported scheme (e.g. redis+sentinel://) —
        # redis.from_url raises ValueError, not a RedisError subclass.
        return False
    try:
        client.ping()
        return True
    except Exception:
        # Broad catch (like CSRFManager.__init__): covers redis.RedisError
        # (TimeoutError, ConnectionError) and any other probe failure.
        return False
    finally:
        # close() can raise on an already-broken connection; suppress rather
        # than use try/except: pass (bandit B110). The ping result above is
        # the value that matters.
        with contextlib.suppress(Exception):
            client.close()


def build_limiter(redis_url: str) -> WhitelistLimiter:
    """Construct the shared rate limiter.

    ``storage_uri`` shares counters across workers via Redis when configured,
    closing the per-worker isolation defect where ``uvicorn --workers N``
    multiplied every configured limit by N. When Redis is configured but not
    reachable at construction (e.g. local dev without Redis), the limiter
    falls back to ``memory://`` — consistent with how ``security.py`` handles
    Redis failover for CSRF — so the module import and ``limiter.reset()`` do
    not error out in Redis-less environments.

    ``in_memory_fallback_enabled`` additionally keeps a per-process limit
    enforcing if a reachable Redis becomes unreachable later at runtime, rather
    than failing closed (503 on every rate-limited request). NOTE: this runtime
    fallback uses per-process MemoryStorage, so under ``--workers N`` with
    Redis down the effective limit is weakened by roughly the worker count
    until Redis recovers. This trades weaker limiting for availability on a
    Redis blip — the opposite of the CSRF layer's fail-closed choice
    (security.py) — so a transient Redis outage does not take down all
    state-changing routes. The tradeoff is intentional.
    """
    resolved = _resolve_storage_uri(redis_url)
    if resolved == "memory://":
        # No Redis configured (CI / REDIS_URL empty).
        storage_uri = "memory://"
    elif _redis_reachable(resolved):
        logger.info("Rate limiter connected to Redis for shared counters")
        storage_uri = resolved
    else:
        logger.warning(
            "Redis unreachable for rate limiter (%s); using per-process "
            "in-memory counters (limits not shared across workers)",
            resolved,
        )
        storage_uri = "memory://"
    return WhitelistLimiter(
        key_func=get_client_ip,
        storage_uri=storage_uri,
        # Only meaningful with a real (non-memory) backend. When storage is
        # memory:// (no Redis configured, or Redis unreachable at startup) no
        # fallback limiter is constructed — behavior identical to pre-fix.
        in_memory_fallback_enabled=storage_uri != "memory://",
    )


# Create the limiter instance
limiter = build_limiter(settings.redis_url)
