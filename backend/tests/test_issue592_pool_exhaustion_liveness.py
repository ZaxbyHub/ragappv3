"""Issue #592 (AC2): an exhausted pool must not freeze unrelated requests.

Fix-agnostic behavioral probe: drives the REAL route-handler coroutine
`app.api.routes.prompts.list_prompt_versions` against a REAL
`SQLiteConnectionPool(max_size=1)` whose only connection is already checked
out (exhausted), while an unrelated trivial coroutine runs concurrently.

Contract under test (behavior only — nothing about HOW the route avoids it):
while one DB route is stalling on the exhausted pool, an unrelated
concurrent request must still complete promptly (< 3.0 s). On the buggy
tree the blocking pool checkout runs on the event loop, freezing the loop
for the full wait budget (3 x 5 s); the unrelated coroutine then completes
only after ~15 s — which this check reports as a failure via the
elapsed-time comparison (NOT via TimeoutError handling: the loop freeze
also freezes `asyncio.wait`'s timer, so the wait can return with the task
"done" but ~15 s late).

Mirrors the monkeypatch pattern of backend/tests/test_chat_turns.py
(patch the route module's `get_pool` to return a real test pool).

NOTE: on the buggy tree this test intentionally takes ~15-20 s — that is
the red evidence, not a hang. No signal alarms are used (Windows-safe).
"""

import asyncio
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.models.database import SQLiteConnectionPool, init_db, run_migrations

# Threshold for "completes promptly". The pool's stall budget is 3 x 5 s,
# so a responsive loop finishes in milliseconds; anything >= 3.0 s means the
# loop was frozen by the route's pool checkout.
_PROMPT_SECONDS = 3.0

# Sentinel strings the trace manifest greps for.
_SENTINEL_PASS = "OBS-592 CHECK: PASS"
_SENTINEL_FAIL = "OBS-592 CHECK: FAIL"


def _describe_task(task):
    if not task.done():
        return "pending"
    if task.cancelled():
        return "cancelled"
    exc = task.exception()
    if exc is not None:
        return f"done (exception={type(exc).__name__}: {exc})"
    return "done (returned)"


async def test_unrelated_request_completes_while_route_waits_on_exhausted_pool(
    tmp_path, monkeypatch
):
    from app.api.routes import prompts

    db_path = str(tmp_path / "issue592_liveness.db")
    init_db(db_path)
    run_migrations(db_path)
    pool = SQLiteConnectionPool(db_path, max_size=1)

    # Exhaust the pool: its only connection is checked out and held.
    hog = pool.get_connection()

    # Route module's pool factory -> the real exhausted pool. The sqlite_path
    # argument is irrelevant while get_pool is patched (test_chat_turns
    # precedent); the Depends parameters of the handler are ignored when the
    # coroutine is invoked directly.
    monkeypatch.setattr(prompts, "get_pool", lambda path: pool)

    async def unrelated_request():
        await asyncio.sleep(0)
        return "ok"

    handler_task = asyncio.create_task(prompts.list_prompt_versions())
    unrelated_task = asyncio.create_task(unrelated_request())

    try:
        t0 = time.monotonic()
        done, _pending = await asyncio.wait(
            {unrelated_task}, timeout=_PROMPT_SECONDS
        )
        elapsed = time.monotonic() - t0
        completed = unrelated_task in done
        handler_state = _describe_task(handler_task)

        # The assertion is ONLY about the unrelated coroutine's completion
        # time: elapsed-time comparison decides, so a late-but-completed
        # unrelated task (loop frozen for the whole stall budget) still fails.
        if completed and elapsed < _PROMPT_SECONDS:
            print(
                f"{_SENTINEL_PASS} — unrelated coroutine completed in "
                f"{elapsed:.3f}s (< {_PROMPT_SECONDS}s) while the route "
                f"handler was still {handler_state}; loop stayed responsive "
                f"under pool exhaustion."
            )
        else:
            outcome = (
                "completed, but late (loop was frozen)"
                if completed
                else "did NOT complete"
            )
            detail = (
                f"{_SENTINEL_FAIL} — unrelated coroutine {outcome} "
                f"within {_PROMPT_SECONDS}s: elapsed={elapsed:.3f}s "
                f"(threshold {_PROMPT_SECONDS}s); route handler state: "
                f"{handler_state}. The route's pool checkout blocked the "
                f"event loop under pool exhaustion, freezing concurrent "
                f"requests."
            )
            print(detail)
            pytest.fail(detail)
    finally:
        # Restore the route module before anything else can observe the patch.
        monkeypatch.undo()
        # Release the hog FIRST so any worker-thread checkout still waiting
        # on the pool queue can return (keeps cancellation prompt).
        try:
            pool.release_connection(hog)
        except Exception:
            pass
        # Consume the handler task: cancel if still running, then await it,
        # tolerating both the cancellation and the pool-exhaustion
        # RuntimeError the stall budget ends with.
        if not handler_task.done():
            handler_task.cancel()
        try:
            await handler_task
        except (asyncio.CancelledError, RuntimeError):
            pass
        pool.close_all()
        # Best-effort tmp db teardown (pytest removes tmp_path itself).
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(db_path + suffix)
            except OSError:
                pass
