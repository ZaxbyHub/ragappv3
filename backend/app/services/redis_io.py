"""Bounded, event-loop-safe wrappers for the OPTIONAL sync Redis cache I/O.

The cache clients in this app (embedding L2 cache, query-transform/planner
caches) use the synchronous ``redis`` client. Calling ``.get``/``.setex``
directly inside an async request path blocks the whole event loop for the
duration of the socket round-trip; a slow Redis therefore stalls every
concurrent request, not just the caller (issue #511, FULL-ENH-04).

``redis_call`` runs the sync call in a worker thread and abandons it past
``settings.redis_io_timeout_seconds``. A timeout or any error is raised to the
caller, whose existing ``try/except`` cache-miss fallbacks treat it as a cache
miss — the optional caches must degrade, never fail the request.

NOT for auth-critical Redis use (CSRF token store), which has its own
fail-closed contract and must stay on its current path.
"""

from __future__ import annotations

import asyncio
import functools
from typing import Any, Callable, TypeVar

from app.config import settings

T = TypeVar("T")


async def redis_call(func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run a sync Redis client call off the event loop under a bounded timeout.

    Raises whatever ``func`` raises, plus ``asyncio.TimeoutError`` when the
    call exceeds ``settings.redis_io_timeout_seconds`` (the in-flight thread
    call itself is abandoned, not killed — Redis clients are not cancellable
    mid-socket-read; the abandoned result is simply discarded).
    """
    timeout = getattr(settings, "redis_io_timeout_seconds", 1.0)
    return await asyncio.wait_for(
        asyncio.to_thread(functools.partial(func, *args, **kwargs)),
        timeout=timeout,
    )
