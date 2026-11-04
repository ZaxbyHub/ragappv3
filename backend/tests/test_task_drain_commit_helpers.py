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
import threading  # noqa: E402
from contextlib import contextmanager  # noqa: E402
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
    """Review PRR-010 (rewritten): a REAL unit-task cancellation — the UNIT
    task itself is cancelled while suspended at its commit await, so
    ``task.cancelled()`` is True and drain_await returns the unit's own
    cancellation as ``error`` — is logged as unrecoverable
    (``commit_audit_unit_cancelled row_state_unverified``), and with NO
    absorbed caller cancellation the helper RETURNS normally. The
    raise-path for an absorbed caller cancellation is pinned separately by
    test_cancel_after_commit_completes_writes_row_then_reraises, so deleting
    either the log-and-return branch or the ``if cancelled: raise`` line now
    fails this file."""
    conn = _FakeConn()
    captured: dict = {"unit_task": None}
    real_ensure_future = asyncio.ensure_future

    def _capturing_ensure_future(coro):
        task = real_ensure_future(coro)
        captured["unit_task"] = task
        return task

    async def go() -> str:
        asyncio.ensure_future = _capturing_ensure_future
        try:
            await drain_commit_audit(conn, lambda: _audit_coro([]))
            return "returned"
        finally:
            asyncio.ensure_future = real_ensure_future

    async def drive() -> str:
        async def _cancel_unit_soon() -> None:
            # Cancel the UNIT task once it is suspended at its commit await.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            captured["unit_task"].cancel()

        asyncio.ensure_future(_cancel_unit_soon())
        return await go()

    with self_caplog() as records:
        outcome = _run(drive())
    assert captured["unit_task"] is not None and captured["unit_task"].cancelled(), (
        "non-vacuity: the UNIT task must have ended cancelled"
    )
    assert outcome == "returned", (
        "with no absorbed caller cancellation the helper must RETURN "
        f"normally after logging the unit teardown (got {outcome!r})"
    )
    assert any(
        "commit_audit_unit_cancelled" in rec.getMessage()
        and "row_state_unverified" in rec.getMessage()
        for rec in records
    ), (
        "the unit-teardown branch must log commit_audit_unit_cancelled "
        f"row_state_unverified; records: {[r.getMessage() for r in records]}"
    )


def self_caplog():
    """Capture records from the task_drain logger without the caplog
    fixture (these tests run as plain pytest functions)."""

    @contextmanager
    def _capture():
        import logging

        records: list[logging.LogRecord] = []
        handler = logging.Handler()
        handler.emit = records.append
        logger = logging.getLogger("app.services.task_drain")
        logger.addHandler(handler)
        old_level = logger.level
        logger.setLevel(logging.DEBUG)
        try:
            yield records
        finally:
            logger.removeHandler(handler)
            logger.setLevel(old_level)

    return _capture()


def test_commit_failure_with_absorbed_cancellation_raises_cancelled(
    tmp_path: Path,
) -> None:
    """Review PRR-004: the compound — the unit FAILS (the commit raises)
    while a caller cancellation was absorbed — must surface the DELIVERED
    CancelledError, not the unit error: the error is logged loudly and the
    cancellation wins. The pre-fix shape re-raised the error before the
    cancelled check and the delivered cancellation vanished."""
    conn = _FakeConn(fail=sqlite3.OperationalError("scripted commit failure"))
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
        loop = asyncio.get_running_loop()

        async def scripted(fn, *args, **kwargs):  # noqa: ANN002, ANN003
            if getattr(fn, "__name__", "") == "commit":
                # The REAL (failing) commit runs on the worker thread; the
                # caller cancellation is queued onto the loop BEFORE the
                # failure resumes the unit (the frozen-C3 ordering), so the
                # caller cancellation is ABSORBED while the unit settles.
                def _fail_then_cancel() -> None:
                    try:
                        fn(*args, **kwargs)
                    except sqlite3.OperationalError:
                        loop.call_soon_threadsafe(holder["task"].cancel)
                        raise

                return await real_to_thread(_fail_then_cancel)
            return await real_to_thread(fn, *args, **kwargs)

        asyncio.to_thread = scripted
        try:
            try:
                await holder["task"]
                return "returned"
            except asyncio.CancelledError:
                return "cancelled"
            except sqlite3.OperationalError:
                return "operational-error-leaked"
        finally:
            asyncio.to_thread = real_to_thread

    with self_caplog() as records:
        outcome = _run(go())
    assert outcome == "cancelled", (
        "the delivered cancellation must WIN in the compound — the pre-fix "
        f"shape leaked the unit error instead (got {outcome!r})"
    )
    assert callbacks == [], "after_commit must not run when the commit failed"
    assert audited == [], "the audit must not be created when the commit failed"
    assert any(
        "commit_audit_unit_failed" in rec.getMessage() for rec in records
    ), (
        "the unit's own failure must still be logged loudly (exc_info) even "
        f"though the cancellation wins; records: {[r.getMessage() for r in records]}"
    )


def test_drained_commit_returns_tuple_never_raises(tmp_path: Path) -> None:
    conn = _FakeConn(fail=sqlite3.OperationalError("nope"))
    cancelled, error = _run(drained_commit(conn))
    assert cancelled is False
    assert isinstance(error, sqlite3.OperationalError), "error returned, not raised"
