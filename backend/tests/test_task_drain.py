"""Unit tests for app.services.task_drain (issue #1036 AC1 support).

Covers the three drain_audit outcomes the route-level frozen checks cannot
isolate at unit granularity:

1. a cancellation delivered to the CALLER is absorbed until the audit
   coroutine completes, then re-raised (row written, cancellation propagates);
2. a failure of the audit coroutine itself is logged, never re-raised;
3. a cancellation OF the audit task itself (loop teardown — nothing else
   cancels it) writes no row and is logged loudly instead of returning
   silently (swarm-pr-review F-06: the silent-return defect).
"""

import asyncio
import logging

import pytest

from app.services.task_drain import drain_audit, drain_await


def test_drain_audit_absorbs_caller_cancellation_and_reraises_after() -> None:
    # Deterministic, event-driven shape (the original sleep/cancel-loop
    # version was xdist-timing-flaky on CI): the caller parks inside
    # drain_await's shield (proven by the audit having started — drain_audit
    # creates the audit task before the caller awaits it), exactly one
    # cancellation is delivered at that suspension point, then the audit is
    # released. The audit MUST complete (the "row was written" analog) and
    # the caller MUST re-raise CancelledError only after that completion.
    audit_started = asyncio.Event()
    release = asyncio.Event()

    async def audit() -> None:
        audit_started.set()
        await asyncio.to_thread(release.wait, 10)
        release.set()

    async def caller() -> None:
        await drain_audit(audit())

    async def scenario() -> str:
        caller_task = asyncio.ensure_future(caller())
        # The audit starting proves the caller is inside drain_await's
        # shield (drain_audit creates the audit task first, then awaits it).
        await asyncio.wait_for(audit_started.wait(), 10)
        caller_task.cancel()      # one cancellation, delivered at the shield
        await asyncio.sleep(0)    # hand off so the CancelledError is absorbed
        release.set()             # the audit completes
        try:
            await caller_task
            return "no-reraise"
        except asyncio.CancelledError:
            assert release.is_set(), (
                "cancellation must propagate only AFTER the audit completed"
            )
            return "reraised-after-completion"

    assert asyncio.run(scenario()) == "reraised-after-completion"
    assert release.is_set()


def test_drain_audit_logs_child_failure_without_reraising(caplog) -> None:
    async def failing_audit() -> None:
        raise RuntimeError("disk on fire")

    async def scenario() -> str:
        with caplog.at_level(logging.WARNING, logger="app.services.task_drain"):
            await drain_audit(failing_audit())
        return "returned"

    assert asyncio.run(scenario()) == "returned"
    assert any(
        "security_audit_record_failed" in r.message and "RuntimeError" in r.message
        for r in caplog.records
    )


def test_drain_audit_logs_when_the_audit_task_itself_is_cancelled(caplog) -> None:
    async def self_cancelling_audit() -> None:
        # Simulate loop teardown: the audit task cancels ITSELF before the
        # row is written (nothing else cancels it — route cancellations are
        # absorbed by the shield).
        asyncio.current_task().cancel()
        await asyncio.sleep(30)

    async def scenario() -> str:
        with caplog.at_level(logging.WARNING, logger="app.services.task_drain"):
            await drain_audit(self_cancelling_audit())
        return "returned"

    asyncio.run(scenario())
    assert any(
        "security_audit_record_cancelled" in r.message
        and "row_not_written" in r.message
        for r in caplog.records
    ), "a cancelled audit task must be logged loudly, never silently dropped"


def test_drain_await_reports_task_failure() -> None:
    async def scenario() -> tuple[bool, str | None]:
        async def boom() -> None:
            raise ValueError("boom")

        cancelled, error = await drain_await(asyncio.ensure_future(boom()))
        return cancelled, type(error).__name__ if error else None

    cancelled, error_name = asyncio.run(scenario())
    assert cancelled is False
    assert error_name == "ValueError"


def test_drain_await_is_noop_for_a_clean_task() -> None:
    async def scenario() -> tuple[bool, str | None]:
        async def clean() -> str:
            return "ok"

        cancelled, error = await drain_await(asyncio.ensure_future(clean()))
        return cancelled, type(error).__name__ if error else None

    cancelled, error_name = asyncio.run(scenario())
    assert cancelled is False
    assert error_name is None
