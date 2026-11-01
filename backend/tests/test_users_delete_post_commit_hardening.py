"""Issue #1031 hardening regressions (unfrozen, plan-critic rounds 1-4).

Beyond the frozen C1 check (cancel-at-await + committing worker -> the
three post-commit outcomes still run), this suite pins the drained-
finalization contract of the approved plan (rev 4):

(a) a SECOND cancellation while settlement is blocked is absorbed — the
    settle-wait is drained, the committed outcome is observed, and all
    three bookkeeping outcomes still land before the CancelledError
    propagates;
(b) a NORMAL-PATH cancellation delivered while commit_purge is in flight
    is absorbed until the bookkeeping completes, then CancelledError
    propagates as the terminal route outcome (never a silent 200);
(c) the equivalent cancellation delivered while the audit event is being
    written — the audit row lands exactly once, then cancellation
    propagates;
(d) an audit-layer EXCEPTION alone (no cancellation) never 500s a
    committed delete: the failure is logged and the route returns its
    success payload (no UnboundLocalError path either);
(e) cancellation DURING finalization combined with an audit-layer
    exception: finalization completes (or fails observably) first and the
    cancellation still propagates.

Harness imported from the frozen C1 file (repo cross-test-import
convention, cf. test_c10_guardrails.py).
"""

import asyncio
import os
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_users_delete_post_commit_on_cancel import (  # noqa: E402
    _ADMIN_USER,
    _CancelCommitEnv,
    _request_for,
)

from app.api.routes import users as users_mod  # noqa: E402
from app.api.routes.users import delete_user  # noqa: E402
from app.services.draft_deletion import DraftDeletionService  # noqa: E402


def _audit_count(env: _CancelCommitEnv) -> int:
    return int(
        env.scalar(
            "SELECT COUNT(*) FROM security_audit_log "
            "WHERE event_type = 'user.deleted' AND target_user_id = ?",
            (2,),
        )
    )


def _user_gone(env: _CancelCommitEnv) -> bool:
    return int(env.scalar("SELECT COUNT(*) FROM users WHERE id = 2", ())) == 0


def test_second_cancel_while_settling_with_commit(tmp_path: Path) -> None:
    """(a) second cancellation during the settle-wait is drained."""
    env = _CancelCommitEnv(tmp_path)
    try:
        invalidate_calls: list[int] = []
        real_invalidate = users_mod.invalidate_active_user_cache

        def _spy(user_id: int) -> None:
            invalidate_calls.append(user_id)
            real_invalidate(user_id)

        async def scenario() -> None:
            task = asyncio.create_task(
                delete_user(2, _request_for(2), user=_ADMIN_USER, _csrf_token="t")
            )
            _deadline = time.monotonic() + 30
            while not env.conn.after_delete_ran.is_set():
                if time.monotonic() > _deadline:
                    raise AssertionError("worker never reached the user DELETE")
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.05)  # route parks on the settle-wait
            task.cancel()  # first cancellation
            await asyncio.sleep(0.05)
            task.cancel()  # second: absorbed by the drained settle-wait
            env.gate.set()  # worker COMMITS
            with pytest.raises(asyncio.CancelledError):
                await task

        with patch.object(
            users_mod, "invalidate_active_user_cache", side_effect=_spy
        ):
            asyncio.run(scenario())

        assert _user_gone(env), "the detached worker committed the delete"
        assert _audit_count(env) == 1, "the audit row landed exactly once"
        assert invalidate_calls == [2], "the cache invalidation ran"
        assert env.trash_entries() == [], "commit_purge finalized the discard"
    finally:
        env.gate.set()
        env.close()


def test_normal_path_cancel_during_commit_purge(tmp_path: Path) -> None:
    """(b) normal-path cancellation during the commit_purge await."""
    env = _CancelCommitEnv(tmp_path)
    purge_entered = threading.Event()
    purge_release = threading.Event()
    real_commit = DraftDeletionService.commit_purge

    def _blocking_commit_purge(self, plan):  # noqa: ANN001
        purge_entered.set()
        purge_release.wait(timeout=10)
        return real_commit(self, plan)

    try:
        async def scenario() -> None:
            task = asyncio.create_task(
                delete_user(2, _request_for(2), user=_ADMIN_USER, _csrf_token="t")
            )
            _deadline = time.monotonic() + 30
            while not env.conn.after_delete_ran.is_set():
                if time.monotonic() > _deadline:
                    raise AssertionError("worker never reached the user DELETE")
                await asyncio.sleep(0.01)
            env.gate.set()  # worker commits; route proceeds to finalize
            _deadline = time.monotonic() + 30
            while not purge_entered.is_set():
                if time.monotonic() > _deadline:
                    raise AssertionError("finalize never reached the awaited step")
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.05)  # finalize is parked inside commit_purge
            task.cancel()  # lands on the drain await — absorbed
            purge_release.set()
            with pytest.raises(asyncio.CancelledError):
                await task

        with patch.object(
            DraftDeletionService, "commit_purge", _blocking_commit_purge
        ), patch.object(users_mod, "invalidate_active_user_cache") as inv:
            asyncio.run(scenario())
            assert _user_gone(env)
            assert _audit_count(env) == 1, "the audit row landed exactly once"
            assert env.trash_entries() == [], "commit_purge completed first"
            assert any(c.args == (2,) for c in inv.call_args_list), (
                "the cache invalidation ran before cancellation propagated"
            )
    finally:
        purge_release.set()
        env.gate.set()
        env.close()


def test_normal_path_cancel_during_audit_await(tmp_path: Path) -> None:
    """(c) normal-path cancellation during the audit write itself."""
    env = _CancelCommitEnv(tmp_path)
    audit_entered = threading.Event()
    audit_release = threading.Event()
    real_record = users_mod.safe_record_security_event

    def _wait_for_release() -> None:
        audit_release.wait(timeout=10)

    async def _blocking_record(*args, **kwargs):  # noqa: ANN002, ANN003
        audit_entered.set()
        await asyncio.to_thread(_wait_for_release)
        return await real_record(*args, **kwargs)

    try:
        async def scenario() -> None:
            task = asyncio.create_task(
                delete_user(2, _request_for(2), user=_ADMIN_USER, _csrf_token="t")
            )
            _deadline = time.monotonic() + 30
            while not env.conn.after_delete_ran.is_set():
                if time.monotonic() > _deadline:
                    raise AssertionError("worker never reached the user DELETE")
                await asyncio.sleep(0.01)
            env.gate.set()
            _deadline = time.monotonic() + 30
            while not audit_entered.is_set():
                if time.monotonic() > _deadline:
                    raise AssertionError("finalize never reached the awaited step")
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.05)
            task.cancel()
            audit_release.set()
            with pytest.raises(asyncio.CancelledError):
                await task

        with patch.object(users_mod, "safe_record_security_event", _blocking_record):
            asyncio.run(scenario())
        assert _user_gone(env)
        assert _audit_count(env) == 1, "the audit row landed exactly once"
        assert env.trash_entries() == [], "commit_purge finalized before the audit"
    finally:
        audit_release.set()
        env.gate.set()
        env.close()


def test_cache_invalidation_failure_still_records_audit(tmp_path: Path) -> None:
    """(d2) multi-stage review: an exception from invalidate_active_user_cache
    itself must not skip the user.deleted audit event for a committed
    delete — the invalidation is best-effort, the audit is mandatory."""
    env = _CancelCommitEnv(tmp_path)

    def _failing_invalidate(user_id):  # noqa: ANN001
        raise ValueError("scripted cache-invalidation failure")

    try:
        with patch.object(
            users_mod, "invalidate_active_user_cache", _failing_invalidate
        ):

            async def scenario() -> None:
                # Mid-test gate release: the 10s scripted fallback must never
                # be this test's timing source (fix-review round 1).
                task = asyncio.create_task(
                    delete_user(2, _request_for(2), user=_ADMIN_USER, _csrf_token="t")
                )
                _deadline = time.monotonic() + 30
                while not env.conn.after_delete_ran.is_set():
                    if time.monotonic() > _deadline:
                        raise AssertionError("worker never reached the user DELETE")
                    await asyncio.sleep(0.01)
                env.gate.set()
                return await task

            result = asyncio.run(scenario())
        assert result == {"message": "User deleted", "user_id": 2}
        assert _user_gone(env)
        assert _audit_count(env) == 1, (
            "the audit event must run even when the cache invalidation raised"
        )
        assert env.trash_entries() == [], "commit_purge still finalized"
    finally:
        env.gate.set()
        env.close()


def test_audit_exception_alone_returns_success(tmp_path: Path) -> None:
    """(d) audit-layer exception alone: no 500, logged, success payload."""
    env = _CancelCommitEnv(tmp_path)

    async def _failing_record(*args, **kwargs):  # noqa: ANN002, ANN003
        raise ValueError("scripted audit-layer failure")

    try:
        with patch.object(users_mod, "safe_record_security_event", _failing_record):

            async def scenario() -> None:
                # Drive as a task and release the worker gate as soon as the
                # user DELETE lands — waiting out the scripted 10s fallback
                # made this test a dead-clock pass (fix-review round 1).
                task = asyncio.create_task(
                    delete_user(2, _request_for(2), user=_ADMIN_USER, _csrf_token="t")
                )
                _deadline = time.monotonic() + 30
                while not env.conn.after_delete_ran.is_set():
                    if time.monotonic() > _deadline:
                        raise AssertionError("worker never reached the user DELETE")
                    await asyncio.sleep(0.01)
                env.gate.set()
                return await task

            result = asyncio.run(scenario())
        assert result == {"message": "User deleted", "user_id": 2}
        assert _user_gone(env)
        assert _audit_count(env) == 0, "the scripted failure wrote no row"
        assert env.trash_entries() == [], "commit_purge still finalized"
    finally:
        env.close()


def test_cancel_during_finalization_combined_with_audit_exception(
    tmp_path: Path,
) -> None:
    """(e) cancellation + audit-layer exception: cancellation still wins."""
    env = _CancelCommitEnv(tmp_path)
    purge_entered = threading.Event()
    purge_release = threading.Event()
    real_commit = DraftDeletionService.commit_purge

    def _blocking_commit_purge(self, plan):  # noqa: ANN001
        purge_entered.set()
        purge_release.wait(timeout=10)
        return real_commit(self, plan)

    async def _failing_record(*args, **kwargs):  # noqa: ANN002, ANN003
        raise ValueError("scripted audit-layer failure")

    try:
        async def scenario() -> None:
            task = asyncio.create_task(
                delete_user(2, _request_for(2), user=_ADMIN_USER, _csrf_token="t")
            )
            _deadline = time.monotonic() + 30
            while not env.conn.after_delete_ran.is_set():
                if time.monotonic() > _deadline:
                    raise AssertionError("worker never reached the user DELETE")
                await asyncio.sleep(0.01)
            env.gate.set()
            _deadline = time.monotonic() + 30
            while not purge_entered.is_set():
                if time.monotonic() > _deadline:
                    raise AssertionError("finalize never reached the awaited step")
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.05)
            task.cancel()
            purge_release.set()
            with pytest.raises(asyncio.CancelledError):
                await task

        with patch.object(
            DraftDeletionService, "commit_purge", _blocking_commit_purge
        ), patch.object(users_mod, "safe_record_security_event", _failing_record):
            asyncio.run(scenario())
        assert _user_gone(env)
        assert env.trash_entries() == [], "finalization-first: the purge completed"
        assert _audit_count(env) == 0, "the scripted audit failure wrote no row"
    finally:
        purge_release.set()
        env.gate.set()
        env.close()
