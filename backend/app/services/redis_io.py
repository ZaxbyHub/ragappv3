"""Bounded, event-loop-safe wrappers for the OPTIONAL sync Redis cache I/O.

The cache clients in this app (embedding L2 cache, query-transform/planner
caches) use the synchronous ``redis`` client. Calling ``.get``/``.setex``
directly inside an async request path blocks the whole event loop for the
duration of the socket round-trip; a slow Redis therefore stalls every
concurrent request, not just the caller (issue #511, FULL-ENH-04).

``redis_call`` runs the sync call on a small DEDICATED thread pool and
abandons it past ``settings.redis_io_timeout_seconds``. The dedicated pool
is the isolation boundary (#687, T1-02-S2-02): a timed-out call's thread
stays blocked in its socket read (Redis clients are not cancellable
mid-read), and on the shared default executor enough of those abandoned
threads starve unrelated ``asyncio.to_thread`` work. Bounded at
``_EXECUTOR_MAX_WORKERS`` threads, a hung Redis can never consume more than
that pool — the default executor stays responsive. The cache clients are
also built with socket timeouts derived from the same setting (see
``embeddings.py``/``query_transformer.py``), so abandoned threads return by
themselves at the same deadline.

A timeout or any error is raised to the caller, whose existing
``try/except`` cache-miss fallbacks treat it as a cache miss — the optional
caches must degrade, never fail the request.

NOT for auth-critical Redis use (CSRF token store), which has its own
fail-closed contract and must stay on its current path.
"""

from __future__ import annotations

import asyncio
import functools
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, TypeVar

from app.config import settings

T = TypeVar("T")

_EXECUTOR_MAX_WORKERS = 8

# Dedicated, bounded pool for optional cache I/O only. Created lazily so
# importing this module never spawns threads; workers are named so a stack
# dump shows exactly which pool a blocked thread belongs to.
_executor: ThreadPoolExecutor | None = None


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(
            max_workers=_EXECUTOR_MAX_WORKERS,
            thread_name_prefix="redis-io",
        )
    return _executor


async def redis_call(func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run a sync Redis client call off the event loop under a bounded timeout.

    Raises whatever ``func`` raises, plus ``asyncio.TimeoutError`` when the
    call exceeds ``settings.redis_io_timeout_seconds`` (the in-flight thread
    call itself is abandoned on the dedicated pool, not killed — Redis
    clients are not cancellable mid-socket-read; the abandoned result is
    simply discarded, and the client's socket timeout eventually frees the
    thread).
    """
    timeout = getattr(settings, "redis_io_timeout_seconds", 1.0)
    loop = asyncio.get_running_loop()
    return await asyncio.wait_for(
        loop.run_in_executor(
            _get_executor(), functools.partial(func, *args, **kwargs)
        ),
        timeout=timeout,
    )
