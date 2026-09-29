"""Issue #687 acceptance check AC10 (frozen) — stalled optional-Redis cache
calls must not starve the shared default executor (T1-02-S2-02).

``redis_call`` (redis_io.py L29-L41) wraps ``asyncio.to_thread(...)`` in
``asyncio.wait_for(..., settings.redis_io_timeout_seconds)``. On timeout the
await is abandoned but the thread keeps blocking in its socket read (the
sync Redis clients are built without socket timeouts), and
``asyncio.to_thread`` runs on the loop's single default executor — so
enough abandoned Redis threads starve UNRELATED to_thread work.
"""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

from app.services import redis_io
from app.services.redis_io import redis_call


async def test_stalled_cache_calls_do_not_starve_default_executor(monkeypatch):
    """AC10: when optional Redis cache calls hang past their timeout,
    unrelated ``asyncio.to_thread`` work must stay responsive.

    The loop's default executor is a 2-worker ThreadPoolExecutor (restored
    in teardown) and ``settings.redis_io_timeout_seconds`` is 0.05. Four
    ``redis_call(blocking_fn)`` calls are issued, each blocked on a
    ``threading.Event``; each times out and is abandoned (both workers stay
    blocked). The probe ``asyncio.to_thread(lambda: 1)`` under a 0.5s
    ``wait_for`` can then never run, so ``got`` stays -1 and the assertion
    fails as ``assert -1 == 1``. Teardown sets the events so the abandoned
    threads exit and the executor can shut down.
    """
    monkeypatch.setattr(
        redis_io.settings, "redis_io_timeout_seconds", 0.05
    )
    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=2)
    previous_executor = getattr(loop, "_default_executor", None)
    loop.set_default_executor(executor)
    events = [threading.Event() for _ in range(4)]

    def blocking_cache_call(event: threading.Event):
        # A sync Redis client stuck in a socket read; the event stands in
        # for the reply that never arrives (30s cap so teardown can never
        # hang on a forgotten thread).
        event.wait(30)
        return "never"

    async def issue(event: threading.Event):
        try:
            await redis_call(blocking_cache_call, event)
            return "completed"
        except (asyncio.TimeoutError, TimeoutError):
            return "timeout"

    try:
        results = await asyncio.gather(*(issue(event) for event in events))
        assert results == ["timeout"] * 4, (
            f"harness: expected all four stalled cache calls to time out, "
            f"got {results}"
        )

        got = -1
        try:
            got = await asyncio.wait_for(asyncio.to_thread(lambda: 1), 0.5)
        except (asyncio.TimeoutError, TimeoutError):
            got = -1
        assert got == 1
    finally:
        for event in events:
            event.set()  # let the abandoned threads finish
        # Restore the loop's default-executor state (set_default_executor
        # requires an instance, so a fresh executor stands in for the lazy
        # default) and join the now-unblocked worker threads.
        loop.set_default_executor(previous_executor or ThreadPoolExecutor())
        executor.shutdown(wait=True)
