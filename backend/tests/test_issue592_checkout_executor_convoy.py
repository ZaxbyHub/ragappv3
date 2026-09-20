"""Regression tests for the #592 final-critic blocker: pool checkouts must run
on the pool's DEDICATED checkout executor, not the event loop's default
executor.

Under the default executor, a burst of exhausted-pool checkouts occupies its
workers while holding no connections; the SQL work of handlers ALREADY holding
connections queues behind those blocked checkouts, so holders cannot release,
the pool stays exhausted, and queued checkouts mass-fail after the wait budget
(the release-starvation convoy: measured checkout_returns=10 / sql_starts=0 /
first SQL at ~15 s in the blocker probe). The dedicated executor keeps holder
SQL on the default executor so slots free and waiting checkouts drain.
"""

import asyncio
import sqlite3
import threading
import time

import pytest

from app.api.routes import prompts as prompts_routes
from app.models.database import SQLiteConnectionPool


def _make_pool(tmp_path, max_size):
    db_path = tmp_path / f"{threading.get_ident()}-{max_size}.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE prompt_versions ("
        " id INTEGER PRIMARY KEY, version TEXT, content TEXT, created_at TEXT,"
        " is_active INTEGER, created_by TEXT)"
    )
    conn.commit()
    conn.close()
    return SQLiteConnectionPool(str(db_path), max_size=max_size)


async def _drive_handler(pool):
    """Drive the real route coroutine against the given pool (C2 shape)."""
    return await prompts_routes.list_prompt_versions()


@pytest.mark.asyncio
async def test_no_release_starvation_convoy_under_burst(tmp_path, monkeypatch):
    """40 concurrent handlers vs a max_size=2 pool that frees mid-burst: every
    handler must complete well within the checkout wait budget once capacity
    is released — none may spurious-fail with the pool-exhaustion RuntimeError.

    Under the pre-blocker shape (checkouts on the default executor) the 32-cap
    default executor saturates with blocked checkouts, holder SQL queues behind
    them, released slots stay unused, and a large share of handlers fail after
    ~15 s. With the dedicated checkout executor the convoy cannot form.
    """
    pool = _make_pool(tmp_path, max_size=2)
    monkeypatch.setattr(prompts_routes, "get_pool", lambda path: pool)

    hogs = [pool.get_connection() for _ in range(2)]

    async def handler(i):
        try:
            await _drive_handler(pool)
            return ("ok", i)
        except RuntimeError:
            return ("runtime-error", i)

    try:
        tasks = [asyncio.create_task(handler(i)) for i in range(40)]
        await asyncio.sleep(0.3)  # let every checkout reach its blocking wait

        t0 = time.monotonic()
        for hog in hogs:
            pool.release_connection(hog)

        results = await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        pool.close_all()
    elapsed = time.monotonic() - t0

    unexpected = [r for r in results if isinstance(r, BaseException)]
    assert not unexpected, (
        f"{len(unexpected)}/40 handlers raised non-RuntimeError exceptions "
        f"(would have masked the convoy assertion without "
        f"return_exceptions=True): {unexpected!r}"
    )
    errors = [r for r in results if r[0] == "runtime-error"]
    assert not errors, (
        f"release-starvation convoy: {len(errors)}/40 handlers spurious-failed "
        f"with pool-exhaustion RuntimeError {elapsed:.2f}s after capacity "
        f"was released"
    )
    assert elapsed < 14.0, (
        f"handlers took {elapsed:.2f}s to drain after capacity release "
        f"(default-executor convoy would hold SQL for the full wait budget)"
    )


@pytest.mark.asyncio
async def test_checkout_runs_on_dedicated_pool_checkout_thread(tmp_path):
    """The async checkout must execute on the pool's dedicated executor
    (thread name prefix 'pool-checkout'), not the loop default executor."""
    pool = _make_pool(tmp_path, max_size=1)
    observed = {}

    real_get = pool.get_connection

    def recording_get(*args, **kwargs):
        observed["thread"] = threading.current_thread().name
        return real_get(*args, **kwargs)

    pool.get_connection = recording_get
    try:
        conn = await pool.get_connection_async(max_wait_attempts=1)
        pool.release_connection(conn)
    finally:
        pool.close_all()

    assert observed.get("thread", "").startswith("pool-checkout"), (
        f"checkout ran on {observed.get('thread')!r}, not the dedicated "
        f"pool-checkout executor"
    )


@pytest.mark.asyncio
async def test_cancelled_checkout_returns_connection_to_pool(tmp_path):
    """PRR-003: a task cancelled during get_connection_async must not leak the
    connection its checkout worker already acquired — the pool's cancellation
    done-callback returns it, keeping _created_count consistent."""
    pool = _make_pool(tmp_path, max_size=1)
    hog = pool.get_connection()  # exhaust the pool

    async def handler():
        return await pool.get_connection_async()

    task = asyncio.create_task(handler())
    await asyncio.sleep(0.05)  # worker now blocked in Queue.get(timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    pool.release_connection(hog)  # the blocked worker now acquires
    await asyncio.sleep(0.2)  # worker validates + cancellation callback releases

    recovered = pool.get_connection(max_wait_attempts=1)
    pool.release_connection(recovered)
    pool.close_all()
    # The cancelled checkout must not have permanently inflated bookkeeping.
    assert pool._created_count == 1, (
        f"_created_count drifted to {pool._created_count}: a cancelled "
        f"checkout leaked its connection"
    )


@pytest.mark.asyncio
async def test_get_connection_async_closed_pool_raises_immediately(tmp_path):
    """Contract preservation: the async checkout surfaces the same RuntimeError
    on a closed pool — WITHOUT lazily creating an unowned checkout executor or
    live pool-checkout threads (#592 final-critic round-2 blocker)."""
    pool = _make_pool(tmp_path, max_size=1)
    pool.close_all()
    # Snapshot by thread IDENTITY, not name (#592 review PRR-009): executor
    # thread names are pool-checkout_<N> with N recycled PER EXECUTOR, so a
    # new orphan thread from this pool can share a name with a foreign
    # winding-down thread and a name-set filter would false-pass.
    before = {t.ident for t in threading.enumerate() if t.name.startswith("pool-checkout")}
    with pytest.raises(RuntimeError, match="closed"):
        await pool.get_connection_async(max_wait_attempts=1)
    assert pool._checkout_executor is None, (
        "a closed-pool async checkout must not lazily create a checkout executor"
    )
    new = [
        t
        for t in threading.enumerate()
        if t.name.startswith("pool-checkout") and t.ident not in before
    ]
    assert not new, f"checkout threads created after close_all: {[t.name for t in new]}"
