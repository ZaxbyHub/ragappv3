"""Unit contract for ``drained_unit`` (issue #1068).

``drained_unit`` extends the one-unit guarantee PR #1067's
``drain_commit_audit`` gives handler-owned connections to handlers whose
commit lives INSIDE a worker/store/helper call: the caller's whole
commit-to-audit sequence runs as one drained unit, so a cancellation
delivered exactly at the inner commit await — after the commit work
completed — can no longer unwind the handler before the audit coroutine
exists. These tests pin the helper's outcome contract directly (event-gated
fake units; no routes needed):

- commit-boundary cancellation: the unit finishes (post-commit steps +
  audit) before the CancelledError is re-raised;
- repeated caller cancellation: absorbed until the unit completes;
- error + absorbed cancellation: the error is logged loudly and the
  delivered cancellation WINS;
- the unit task itself cancelled (loop teardown): logged unrecoverable;
  helper returns normally when no caller cancellation was absorbed;
- exception identity: the unit's own error propagates as the SAME object;
- pre-commit failure: the error propagates and no audit runs;
- ORDINARY post-commit step failure: propagates and SKIPS the audit —
  exact ``drain_commit_audit`` parity (test_task_drain_commit_helpers
  pins the same arm for ``after_commit``);
- audit-write failure: logged, never raised (drain_audit best-effort).
"""

import asyncio
import os

os.environ["JWT_SECRET_KEY"] = (
    "test-jwt-secret-key-for-testing-only-12345678901234567890"
)
os.environ["USERS_ENABLED"] = "false"
os.environ["REDIS_URL"] = ""
os.environ["ADMIN_SECRET_TOKEN"] = (
    "test-admin-secret-token-0123456789abcdef0123456789abcdef"
)

import logging  # noqa: E402
import sqlite3  # noqa: E402
from contextlib import contextmanager  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any  # noqa: E402

from app.services.task_drain import drain_audit, drained_unit  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


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


def test_commit_boundary_cancellation_completes_unit_then_reraises(
    tmp_path: Path,
) -> None:
    """The core #1068 schedule: the unit's commit step is already durable
    when the caller cancellation lands; the unit must finish (post-commit
    steps + audit tail) before the CancelledError propagates."""
    sink: list[str] = []

    async def go() -> str:
        started = asyncio.Event()
        release = asyncio.Event()

        async def _unit() -> None:
            started.set()
            await asyncio.sleep(0)
            sink.append("commit-durable")
            # post-commit steps suspended at the boundary where the
            # cancellation is delivered
            await release.wait()
            sink.append("callback")
            await asyncio.sleep(0)
            sink.append("audit-written")

        task = asyncio.ensure_future(drained_unit(_unit()))
        await started.wait()
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        try:
            await task
            return "returned"
        except asyncio.CancelledError:
            return "cancelled"

    outcome = _run(go())
    assert outcome == "cancelled", "the absorbed cancellation must win after the unit"
    assert sink == ["commit-durable", "callback", "audit-written"], (
        "the unit must complete (post-commit steps + audit) before the "
        f"cancel re-raises (steps ran: {sink!r})"
    )


def test_repeated_caller_cancellation_absorbed(tmp_path: Path) -> None:
    sink: list[str] = []

    async def go() -> str:
        started = asyncio.Event()
        release = asyncio.Event()

        async def _unit() -> None:
            started.set()
            await release.wait()
            sink.append("audit-written")

        task = asyncio.ensure_future(drained_unit(_unit()))
        await started.wait()
        task.cancel()
        await asyncio.sleep(0)
        if not task.done():
            task.cancel()  # second cancellation while the unit still runs
        release.set()
        try:
            await task
            return "returned"
        except asyncio.CancelledError:
            return "cancelled"

    outcome = _run(go())
    assert outcome == "cancelled"
    assert sink == ["audit-written"], (
        "the unit completed despite repeated cancellations"
    )


def test_error_plus_cancel_cancel_wins_logged(tmp_path: Path) -> None:
    """The compound: the unit FAILS while a caller cancellation was
    absorbed — the error is logged loudly and the delivered cancellation
    WINS (mirrors drain_commit_audit's frozen compound contract)."""
    sink: list[str] = []

    async def go() -> str:
        started = asyncio.Event()
        release = asyncio.Event()

        async def _unit() -> None:
            started.set()
            sink.append("commit-durable")
            await release.wait()
            raise RuntimeError("post-commit boom")

        task = asyncio.ensure_future(drained_unit(_unit()))
        await started.wait()
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        try:
            await task
            return "returned"
        except asyncio.CancelledError:
            return "cancelled"
        except RuntimeError:
            return "runtime-error-leaked"

    with self_caplog() as records:
        outcome = _run(go())
    assert outcome == "cancelled", (
        "the delivered cancellation must WIN in the compound — the unit "
        f"error must not leak (got {outcome!r})"
    )
    assert sink == ["commit-durable"]
    assert any(
        "drained_unit_unit_failed" in rec.getMessage() for rec in records
    ), (
        "the unit's own failure must still be logged loudly (exc_info) even "
        f"though the cancellation wins; records: {[r.getMessage() for r in records]}"
    )


def test_inner_unit_cancellation_logged_returns_normally(tmp_path: Path) -> None:
    """A REAL unit-task cancellation (loop teardown shape): the UNIT task
    itself is cancelled while suspended — logged as unrecoverable
    (``drained_unit_unit_cancelled row_state_unverified``) and, with no
    absorbed caller cancellation, the helper RETURNS normally."""
    captured: dict = {"unit_task": None}
    real_ensure_future = asyncio.ensure_future

    def _capturing_ensure_future(coro):
        task = real_ensure_future(coro)
        captured["unit_task"] = task
        return task

    async def drive() -> str:
        started = asyncio.Event()

        async def _unit() -> None:
            started.set()
            await asyncio.Event().wait()  # suspend forever

        async def _cancel_unit_soon() -> None:
            await started.wait()
            await asyncio.sleep(0)
            captured["unit_task"].cancel()

        real_ensure_future(_cancel_unit_soon())
        asyncio.ensure_future = _capturing_ensure_future
        try:
            await drained_unit(_unit())
            return "returned"
        finally:
            asyncio.ensure_future = real_ensure_future

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
        "drained_unit_unit_cancelled" in rec.getMessage()
        and "row_state_unverified" in rec.getMessage()
        for rec in records
    ), (
        "the unit-teardown branch must log drained_unit_unit_cancelled "
        f"row_state_unverified; records: {[r.getMessage() for r in records]}"
    )

def test_simultaneous_teardown_race_caller_cancel_wins(tmp_path: Path) -> None:
    """The unit task's own cancellation lands while the CALLER task also
    carries a pending (not yet delivered) cancellation request: the caller's
    cancellation must WIN — re-raised, never swallowed into a normal return
    (PR #1077 review PRR-006; the pre-fix settle returned normally and every
    converted handler would have run post-unit code on pre-initialized
    values)."""
    real_ensure_future = asyncio.ensure_future

    async def go() -> str:
        started = asyncio.Event()
        state: dict = {"unit_task": None}

        async def _unit() -> None:
            started.set()
            await asyncio.Event().wait()  # suspend forever

        def _capturing_ensure_future(coro):
            task = real_ensure_future(coro)
            if coro.__name__ == "_unit_body":
                state["unit_task"] = task
            return task

        async def go_inner() -> str:
            caller = asyncio.current_task()
            assert caller is not None

            async def _unit_body() -> None:
                started.set()
                await asyncio.Event().wait()

            async def _cancel_both_soon() -> None:
                await started.wait()
                # Tick 1: cancel the UNIT task (its own CancelledError will
                # settle the drained unit through the error path).
                unit_task = state["unit_task"]
                assert unit_task is not None
                unit_task.cancel()
                await asyncio.sleep(0)
                # Tick 2: arm the CALLER's cancellation in the same window —
                # pending (cancelling() > 0) but not yet delivered when the
                # unit's CancelledError reaches _settle_drained.
                caller.cancel()
                await asyncio.sleep(0)

            real_ensure_future(_cancel_both_soon())
            asyncio.ensure_future = _capturing_ensure_future
            try:
                await drained_unit(_unit_body())
                return "returned"
            except asyncio.CancelledError:
                return "cancelled"
            finally:
                asyncio.ensure_future = real_ensure_future

        return await go_inner()

    outcome = _run(go())
    assert outcome == "cancelled", (
        "simultaneous-teardown race: the CALLER's pending cancellation must "
        f"be re-raised, never swallowed into a normal return (got {outcome!r})"
    )




def test_expected_client_error_logs_info_without_traceback(tmp_path: Path) -> None:
    """PRR-003 (PR #1077 review): a unit failure shaped like an expected
    client error (a ``status_code`` < 500, e.g. a duplicate-username 409)
    logs at INFO WITHOUT a stack trace — never at alert grade."""
    fastapi_exc: Any = None
    try:
        from fastapi import HTTPException as _FastAPIHTTPException

        fastapi_exc = _FastAPIHTTPException
    except ImportError:  # pragma: no cover - fastapi is a hard dep in app
        fastapi_exc = None
    assert fastapi_exc is not None, "fastapi must be importable in app tests"

    async def go() -> str:
        async def _unit() -> None:
            raise fastapi_exc(status_code=409, detail="username taken")

        try:
            await drained_unit(_unit())
            return "returned"
        except fastapi_exc:
            return "raised"

    with self_caplog() as records:
        outcome = _run(go())
    assert outcome == "raised", f"the 409 must propagate (got {outcome!r})"
    client_recs = [r for r in records if "unit_client_error" in r.getMessage()]
    assert client_recs, (
        f"the client-error branch must log unit_client_error; "
        f"records: {[r.getMessage() for r in records]}"
    )
    assert all(r.levelno == logging.INFO for r in client_recs), (
        f"expected client errors must log at INFO, never ERROR; levels: "
        f"{[r.levelname for r in client_recs]}"
    )
    assert all(r.exc_info is None for r in client_recs), (
        "the INFO client-error branch must NOT carry exc_info"
    )


def test_unexpected_error_logs_error_with_traceback(tmp_path: Path) -> None:
    """PRR-003 control: a NON-client error (no status_code, e.g. a
    RuntimeError) logs at ERROR WITH exc_info — the alert-grade channel is
    reserved for genuinely unexpected failures."""
    boom = _Boom("unit blew up unexpectedly")

    async def go() -> str:
        async def _unit() -> None:
            raise boom

        try:
            await drained_unit(_unit())
            return "returned"
        except _Boom:
            return "raised"

    with self_caplog() as records:
        outcome = _run(go())
    assert outcome == "raised"
    error_recs = [r for r in records if "unit_failed" in r.getMessage()]
    assert error_recs, (
        f"the unexpected-error branch must log unit_failed; "
        f"records: {[r.getMessage() for r in records]}"
    )
    assert all(r.levelno == logging.ERROR for r in error_recs), (
        f"unexpected errors must log at ERROR; levels: "
        f"{[r.levelname for r in error_recs]}"
    )
    assert any(r.exc_info for r in error_recs), (
        "the ERROR branch must carry exc_info (the traceback is the "
        "operator's diagnostic)"
    )


class _Boom(RuntimeError):
    pass


def test_exception_identity(tmp_path: Path) -> None:
    """The unit's own failure propagates as the SAME exception object (the
    original type and traceback survive; the helper never wraps)."""
    boom = _Boom("unit blew up")

    async def _unit() -> None:
        raise boom

    raised: BaseException | None = None
    try:
        _run(drained_unit(_unit()))
    except BaseException as exc:  # noqa: BLE001
        raised = exc
    assert raised is boom, (
        f"the exact exception object must propagate (got {raised!r})"
    )


def test_pre_commit_failure_no_audit(tmp_path: Path) -> None:
    """A failure BEFORE the commit leaves nothing applied: the error
    propagates with its original type and the audit never runs."""
    audited: list[int] = []

    def _failing_commit() -> None:
        raise sqlite3.OperationalError("disk full before commit")

    async def _unit() -> None:
        _failing_commit()
        audited.append(1)

    raised: BaseException | None = None
    try:
        _run(drained_unit(_unit()))
    except BaseException as exc:  # noqa: BLE001
        raised = exc
    assert isinstance(raised, sqlite3.OperationalError), (
        f"the original error type must propagate (got {raised!r})"
    )
    assert audited == [], "no audit may run when the unit failed before the commit"


def test_post_commit_step_failure_propagates_and_skips_audit(
    tmp_path: Path,
) -> None:
    """ORDINARY post-commit step failure: propagates with its original type
    and SKIPS the audit — exact drain_commit_audit parity for whatever sits
    between the commit and the audit tail (test_task_drain_commit_helpers
    pins the same arm for ``after_commit``)."""
    audited: list[int] = []

    def _bad_post_commit_step() -> None:
        raise RuntimeError("cache invalidation blew up")

    async def _unit() -> None:
        await asyncio.sleep(0)  # the commit step completed
        _bad_post_commit_step()
        audited.append(1)

    raised: BaseException | None = None
    try:
        _run(drained_unit(_unit()))
    except BaseException as exc:  # noqa: BLE001
        raised = exc
    assert isinstance(raised, RuntimeError), (
        f"the post-commit failure must keep its original type (got {raised!r})"
    )
    assert audited == [], (
        "the audit must be SKIPPED after an ordinary post-commit step "
        "failure (drain_commit_audit parity)"
    )


def test_audit_failure_logged_not_raised(tmp_path: Path) -> None:
    """Audit-write failure inside the unit: logged by the real drain_audit
    (best-effort), never raised out of drained_unit."""
    sink: list[str] = []

    async def _failing_audit() -> None:
        raise RuntimeError("audit table locked")

    async def _unit() -> None:
        await asyncio.sleep(0)  # the commit step completed
        sink.append("commit-durable")
        await drain_audit(_failing_audit())
        sink.append("after-audit")

    with self_caplog() as records:
        _run(drained_unit(_unit()))  # must not raise

    assert sink == ["commit-durable", "after-audit"], (
        "the unit completes around the best-effort audit failure"
    )
    assert any(
        "security_audit_record_failed" in rec.getMessage() for rec in records
    ), (
        "the audit failure must be logged by drain_audit; records: "
        f"{[r.getMessage() for r in records]}"
    )
