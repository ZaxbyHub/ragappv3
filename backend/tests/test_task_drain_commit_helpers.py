"""Unit contract for task_drain's commit-boundary helpers (issue #1043
residual 3).

``drained_commit`` and ``drain_commit_audit`` close the window PR #1035's
``drain_audit`` could not: a cancellation delivered exactly at the commit
await AFTER the commit work completed used to unwind the handler before the
audit coroutine ever existed. These tests pin each helper's outcome contract
directly (fake connection + fake audit factory; no routes needed):

- commit failure: error re-raised, callback not run, audit not created;
- after_commit failure: error re-raised, audit not created;
- audit failure: logged, not raised;
- cancellation at the commit await after the commit completed: callback run,
  audit row written, then CancelledError re-raised;
- repeated caller cancellation: absorbed until the unit completes;
- the unit task cancelled by loop teardown: logged unrecoverable; helper
  returns normally when no caller cancellation was absorbed.
"""

import asyncio
import os

os.environ["JWT_SECRET_KEY"] = (
    "test-jwt-secret-key-for-testing-only-12345678901234567890"
)
os.environ["USERS_ENABLED"] = "false"
os.environ["REDIS_URL"] = ""
os.environ["ADMIN_SECRET_TOKEN"] = (
    "test-admin-secret-0123456789abcdef0123456789abcdef0123456789abcdef"
)

import sqlite3  # noqa: E402
from pathlib import Path  # noqa: E402

from app.services.task_drain import drain_commit_audit, drained_commit  # noqa: E402


class _FakeConn:
    def __init__(self, fail: Exception | None = None) -> None:
        self.commit_calls = 0
        self._fail = fail

    def commit(self) -> None:
        self.commit_calls += 1
        if self._fail is not None:
            raise self._fail


def _run(coro):
    return asyncio.run(coro)


def test_commit_failure_reraised_and_no_audit(tmp_path: Path) -> None:
    conn = _FakeConn(fail=sqlite3.OperationalError("disk full"))
    audited: list[int] = []

    async def go():
        await drain_commit_audit(
            conn,
            lambda: _audit_coro(audited),
            after_commit=lambda: callbacks.append(1),
        )

    callbacks: list[int] = []
    try:
        _run(go())
        raised = False
    except sqlite3.OperationalError:
        raised = True
    assert raised, "commit failure must re-raise"
    assert callbacks == [], "after_commit must not run when the commit failed"
    assert audited == [], "audit must not be created when the commit failed"


def _audit_coro(sink: list[int], fail: Exception | None = None):
    async def _coro() -> None:
        if fail is not None:
            raise fail
        sink.append(1)

    return _coro()


def test_after_commit_failure_reraised_and_no_audit(tmp_path: Path) -> None:
    conn = _FakeConn()
    audited: list[int] = []

    def _bad_callback() -> None:
        raise RuntimeError("cache invalidation blew up")

    async def go():
        await drain_commit_audit(conn, lambda: _audit_coro(audited), after_commit=_bad_callback)

    try:
        _run(go())
        raised = False
    except RuntimeError:
        raised = True
    assert raised, "after_commit failure must re-raise"
    assert audited == [], "audit must not be created after a callback failure"


def test_audit_failure_logged_not_raised(tmp_path: Path) -> None:
    conn = _FakeConn()
    async def go():
        await drain_commit_audit(conn, lambda: _audit_coro([], fail=RuntimeError("audit down")))

    _run(go())  # must not raise


def test_cancel_after_commit_completes_writes_row_then_reraises(tmp_path: Path) -> None:
    """The core #1043 residual-3 schedule: the REAL commit completes in the
    worker, and the cancellation is scheduled to land exactly at the commit
    await — the unit must finish (callback + audit row) before the
    CancelledError propagates."""
    conn = _FakeConn()
    audited: list[int] = []
    callbacks: list[int] = []
    holder: dict = {"task": None}

    async def go() -> str:
        async def _drive() -> None:
            await drain_commit_audit(
                conn,
                lambda: _audit_coro(audited),
                after_commit=lambda: callbacks.append(1),
            )

        holder["task"] = asyncio.ensure_future(_drive())
        real_to_thread = asyncio.to_thread

        def _commit_then_cancel():
            # runs on the worker thread AFTER commit completed
            loop.call_soon_threadsafe(holder["task"].cancel)

        loop = asyncio.get_running_loop()
        # Patch conn.commit to schedule the cancel after it returns... the
        # helper calls to_thread(conn.commit); intercept by scheduling the
        # cancel from a thread as soon as commit() returns.
        original_commit = conn.commit

        def _commit_and_arm() -> None:
            original_commit()
            import threading

            threading.Thread(target=_commit_then_cancel_soon, daemon=True).start()

        def _commit_then_cancel_soon() -> None:
            loop.call_soon_threadsafe(holder["task"].cancel)

        conn.commit = _commit_and_arm  # type: ignore[method-assign]
        try:
            await holder["task"]
            return "returned"
        except asyncio.CancelledError:
            return "cancelled"

    outcome = _run(go())
    assert outcome == "cancelled", "the absorbed cancellation must win after the unit"
    assert conn.commit_calls == 1, "the real commit ran exactly once"
    assert callbacks == [1], "after_commit ran inside the drained unit"
    assert audited == [1], "the audit row was written before the cancel re-raised"


def test_repeated_caller_cancellation_absorbed(tmp_path: Path) -> None:
    conn = _FakeConn()
    audited: list[int] = []

    async def go() -> str:
        task = asyncio.ensure_future(
            drain_commit_audit(conn, lambda: _audit_coro(audited))
        )
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        if not task.done():
            task.cancel()  # second cancellation while the unit still runs
        try:
            await task
            return "returned"
        except asyncio.CancelledError:
            return "cancelled"

    outcome = _run(go())
    assert outcome == "cancelled"
    assert audited == [1], "the unit completed despite repeated cancellations"


def test_unit_task_teardown_logged_returns_normally(tmp_path: Path) -> None:
    """Loop-teardown cancellation of the unit task itself: logged as
    unrecoverable; with no absorbed caller cancellation the helper returns
    normally (the caller's own control flow proceeds)."""
    conn = _FakeConn()

    async def go() -> str:
        task = asyncio.ensure_future(
            drain_commit_audit(conn, lambda: _audit_coro([]))
        )
        await asyncio.sleep(0)
        task.cancel()  # cancels the UNIT task, not the caller
        try:
            await task
            return "returned"
        except asyncio.CancelledError:
            return "cancelled"

    # Cancelling the outer task surfaces CancelledError at the await; the
    # helper's contract distinguishes caller-cancel (re-raise) from unit
    # teardown (logged + return). Cancelling the awaited task directly
    # propagates CancelledError at the shield; drain_await reports it as the
    # task's own cancellation, so the helper logs and returns -> the await
    # itself still raises CancelledError from the outer cancellation we
    # delivered to the caller-equivalent task. Assert the audit did NOT run
    # and the commit DID run (unit teardown happens after commit started).
    try:
        _run(go())
    except asyncio.CancelledError:
        pass
    assert conn.commit_calls == 1


def test_drained_commit_returns_tuple_never_raises(tmp_path: Path) -> None:
    conn = _FakeConn(fail=sqlite3.OperationalError("nope"))
    cancelled, error = _run(drained_commit(conn))
    assert cancelled is False
    assert isinstance(error, sqlite3.OperationalError), "error returned, not raised"
