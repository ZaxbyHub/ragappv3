"""Issue #1019 recovery-outcome matrix (unfrozen hardening tests).

Plan-critic rounds 1-5 drove this matrix beyond the frozen C6 check. Every
test is GREEN only after the outcome-aware backstop; the rev-1 design
(verbatim vaults in_transaction mirror) fails the ValueError case, and the
rev-3 design (asyncio-future cancelled()) fails the queued case.

Interleavings, per the approved plan (rev 6):
- ValueError after the DELETE inside the worker (an ordinary non-legacy
  Exception the two route handlers do not name): transaction_context rolls
  back, the backstop must still restore — the in_transaction-only gate of
  the rejected design skipped exactly this case.
- Queued executor work item cancelled before entry: nothing ran, restore
  without waiting (a queued-then-cancelled item never signals its settle
  event — an event-only wait would hang forever).
- Cancellation at the await while the worker is blocked mid-transaction,
  then released to fail inside the transaction: rows survive -> restore;
  CancelledError propagates; the connection is not released before the
  worker settles (release modeled by the test's request-scope finally,
  mirroring the Depends(get_db) teardown in production).
- Cancellation at the await, worker completes the commit afterwards:
  tombstones stay discarded (startup reconcile owns them).
- Cleanup failures (rollback raises inside the backstop; restore_purge
  raises) never replace the primary exception.
"""

import asyncio
import concurrent.futures
import os
import threading
import time

os.environ["JWT_SECRET_KEY"] = (
    "test-jwt-secret-key-for-testing-only-12345678901234567890"
)
os.environ["USERS_ENABLED"] = "true"
os.environ["ADMIN_SECRET_TOKEN"] = (
    "outcomes-admin-secret-token-0123456789abcdef0123456789abcdef"
)

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.api import deps as deps_mod
from app.api.routes import users as users_mod
from app.api.routes.users import delete_user
from app.config import settings
from app.models.database import SQLiteConnectionPool, _pool_cache, init_db
from app.services.draft_deletion import DraftDeletionService
from app.services.draft_input_storage import DraftInputStorage
from app.services.draft_store import DraftStore

_ADMIN_USER = {
    "id": 1,
    "username": "admin",
    "full_name": "Admin",
    "role": "superadmin",
    "is_active": True,
    "must_change_password": False,
}

VICTIM_INPUT_BYTES = b"victim user's private manuscript bytes"


class _FakeUpload:
    def __init__(self, filename: str, content: bytes) -> None:
        self.filename = filename
        self._content = content
        self._offset = 0

    async def read(self, size: int = -1) -> bytes:
        if size < 0:
            chunk, self._offset = self._content[self._offset :], len(self._content)
            return chunk
        chunk = self._content[self._offset : self._offset + size]
        self._offset += size
        return chunk


class _ScriptedConn:
    """Connection wrapper scripting the delete window statement by statement.

    Hooks (all optional):
    - raise_after_user_delete: exception raised right after the user DELETE
      executes inside the open transaction (worker-side failure).
    - block_after_user_delete: release_gate — after the DELETE the WORKER
      THREAD parks on ``release_gate`` (models a worker mid-transaction while
      the route await is cancelled). The test owns the gate, so it decides
      exactly when the worker's terminal outcome becomes observable.
    - fail_rollback: ``rollback()`` raises (backstop cleanup-failure probe;
      only reachable for worker BaseExceptions that skip
      transaction_context's own rollback).

    ``hook_done`` is set once the post-DELETE hook (block or raise) fully
    completed — the release-ordering assertion reads it.
    """

    def __init__(
        self,
        inner: sqlite3.Connection,
        raise_after_user_delete: BaseException | None = None,
        block_after_user_delete: threading.Event | None = None,
        fail_rollback: bool = False,
    ) -> None:
        self._inner = inner
        self._raise_after = raise_after_user_delete
        self._block_release = block_after_user_delete
        self._fail_rollback = fail_rollback
        self.after_delete_ran = threading.Event()
        self.hook_done = threading.Event()
        # True when the block gate (not its 10s fallback) let the worker go.
        self.block_released_by_gate: bool | None = None

    def execute(self, sql, params=()):  # noqa: ANN001
        result = self._inner.execute(sql, params)
        if sql.strip().upper().startswith("DELETE FROM USERS"):
            self.after_delete_ran.set()
            try:
                if self._block_release is not None:
                    self.block_released_by_gate = self._block_release.wait(timeout=10)
                if self._raise_after is not None:
                    raise self._raise_after
            finally:
                self.hook_done.set()
        return result

    def rollback(self):  # noqa: ANN201
        if self._fail_rollback:
            raise sqlite3.OperationalError("scripted rollback failure")
        return self._inner.rollback()

    def __getattr__(self, name):  # noqa: ANN001
        return getattr(self._inner, name)


class _ReleaseTrackingPool:
    """Pool stand-in recording whether release happened before hook settle."""

    def __init__(self, real_pool: SQLiteConnectionPool, conn: _ScriptedConn):
        self._real_pool = real_pool
        self._conn = conn
        self.hook_done_at_release: list[bool] = []

    async def get_connection_async(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return self._conn

    def release_connection(self, _conn) -> None:
        self.hook_done_at_release.append(self._conn.hook_done.is_set())
        try:
            if self._conn.in_transaction:
                self._conn.rollback()
        except sqlite3.Error:
            pass
        self._real_pool.release_connection(self._conn._inner)


def _request_for(user_id: int) -> Request:
    return Request(
        {
            "type": "http",
            "method": "DELETE",
            "headers": [],
            "path": f"/api/users/{user_id}",
            "query_string": b"",
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
            "scheme": "http",
        }
    )


class _OutcomesEnv:
    """Per-test world: DB, scripted connection, real draft bytes, patched get_pool."""

    def __init__(self, tmp_path: Path, **conn_kwargs) -> None:  # noqa: ANN003
        _pool_cache.clear()
        self.db_path = str(tmp_path / "outcomes.db")
        init_db(self.db_path)
        self.real_pool = SQLiteConnectionPool(self.db_path, max_size=2)
        self.storage = DraftInputStorage(tmp_path / "draft-room")

        seed = self.real_pool.get_connection()
        try:
            seed.execute(
                "INSERT INTO users (id, username, hashed_password, role, is_active) "
                "VALUES (?, ?, ?, ?, ?)",
                (1, "admin", "abc123", "superadmin", 1),
            )
            seed.execute(
                "INSERT INTO users (id, username, hashed_password, role, is_active) "
                "VALUES (?, ?, ?, ?, ?)",
                (2, "victim", "abc123", "member", 1),
            )
            seed.execute("INSERT INTO vaults (id, name) VALUES (1, 'OutcomesVault')")
            seed.commit()
            draft_store = DraftStore(seed)
            draft = draft_store.create_draft(
                vault_id=1,
                created_by=2,
                title="outcomes draft",
                mode="compose",
                tier="standard",
                brief_json="{}",
            )
            staged = asyncio.run(
                self.storage.stage_upload(
                    _FakeUpload("notes.txt", VICTIM_INPUT_BYTES),
                    allowed_extensions={".txt"},
                    max_file_bytes=1_000_000,
                )
            )
            record = draft_store.reserve_input(
                draft_id=draft.id,
                owner_id=2,
                role="reference",
                authority="unknown",
                as_of_date=None,
                original_name=staged.original_name,
                stored_name=staged.stored_name,
                extension=staged.extension,
                media_type=staged.media_type,
                size_bytes=staged.size_bytes,
                content_sha256=staged.content_sha256,
                max_inputs=100,
                max_total_input_bytes=10_000_000,
            )
            self.storage.finalize(staged, record.storage_relpath)
            seed.commit()
            self.input_relpath = record.storage_relpath
        finally:
            self.real_pool.release_connection(seed)

        assert self.storage.exists(self.input_relpath)
        self.conn = _ScriptedConn(self.real_pool.get_connection(), **conn_kwargs)
        self.facade = _ReleaseTrackingPool(self.real_pool, self.conn)
        self._saved_get_pool = deps_mod.get_pool
        deps_mod.get_pool = lambda _path: self.facade
        self._saved_data_dir = settings.data_dir
        settings.data_dir = tmp_path

    def rows_survive(self) -> bool:
        check = self.real_pool.get_connection()
        try:
            row = check.execute("SELECT COUNT(*) FROM users WHERE id = 2").fetchone()
            return row[0] == 1
        finally:
            self.real_pool.release_connection(check)

    def input_live(self) -> bool:
        return self.storage.exists(self.input_relpath)

    def close(self) -> None:
        deps_mod.get_pool = self._saved_get_pool
        settings.data_dir = self._saved_data_dir
        self.real_pool.close_all()
        _pool_cache.clear()


def test_value_error_after_delete_restores_purge(tmp_path: Path) -> None:
    """Plan-critic round 1's disproof of the verbatim-mirror design."""
    env = _OutcomesEnv(tmp_path, raise_after_user_delete=ValueError)
    try:
        with pytest.raises(ValueError):
            asyncio.run(delete_user(2, _request_for(2), user=_ADMIN_USER, _csrf_token="t", db=env.conn))
        assert env.rows_survive(), "rollback must keep the user row"
        assert env.input_live(), "the backstop must restore the tombstoned bytes"
    finally:
        env.close()


def test_queued_cancel_before_entry_restores_without_waiting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plan-critic round 2's hole: a queued-then-cancelled item never settles."""
    env = _OutcomesEnv(tmp_path)
    blocker_release = threading.Event()

    class _PreSaturatedExecutor(concurrent.futures.ThreadPoolExecutor):
        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
            super().__init__(*args, **kwargs)
            # Occupy the single worker so the route's item queues behind it.
            super().submit(lambda: blocker_release.wait(timeout=10))

    monkeypatch.setattr(
        users_mod.concurrent.futures, "ThreadPoolExecutor", _PreSaturatedExecutor
    )
    try:

        async def scenario() -> None:
            task = asyncio.create_task(
                delete_user(2, _request_for(2), user=_ADMIN_USER, _csrf_token="t", db=env.conn)
            )
            await asyncio.sleep(0.05)  # route submits its item behind the blocker
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(scenario())
        assert env.rows_survive(), "nothing ran; the rows trivially survived"
        assert env.input_live(), "queued-cancel must restore without waiting"
    finally:
        blocker_release.set()
        env.close()


def test_cancel_at_await_waits_for_worker_then_restores(tmp_path: Path) -> None:
    """Release ordering + composite interleaving: cancel, worker unblocks to
    raise inside the still-open transaction (BaseException -> no
    transaction_context rollback), backstop rolls back and restores.
    PR #1198 feedback F-04: (1) a deterministic cancellation-observed
    handshake — with the worker still parked on `release`, the cancel must be
    HELD by the settle-wait for the whole wait window (the skip-the-settle-wait
    mutant completes inside it, before release.set()); (2) the finally samples
    the settled state BEFORE releasing — settlement (restore applied) must
    already hold when the release runs."""
    release = threading.Event()
    env = _OutcomesEnv(
        tmp_path,
        block_after_user_delete=release,
        raise_after_user_delete=asyncio.CancelledError(),
    )
    release_time_state: list[tuple[bool, bool]] = []
    settle_wait_entries: list[bool] = []
    try:

        async def drive() -> None:
            task = asyncio.create_task(
                delete_user(2, _request_for(2), user=_ADMIN_USER, _csrf_token="t", db=env.conn)
            )
            try:
                while not env.conn.after_delete_ran.is_set():
                    await asyncio.sleep(0.01)
                task.cancel()
                # PR #1198 feedback F-04 (Stage-B reviews r1+r2): the handshake
                # now OBSERVES settle-wait ENTRY, not just task pendingness. A
                # spy on asyncio.to_thread records the backstop's settlement
                # wait (`delete_settled.wait`, a bound threading.Event method)
                # the moment the BaseException arm reaches it; the poll below
                # fires only after that entry. The skip-the-settle-wait mutant
                # NEVER calls delete_settled.wait (its recovery path threads
                # only the rollback lambda and restore_purge), so the poll
                # times out and the test fails deterministically — independent
                # of how long the mutant's delayed recovery takes.
                orig_to_thread = users_mod.asyncio.to_thread

                def _spy_to_thread(fn, *args, **kwargs):
                    if (
                        getattr(fn, "__name__", "") == "wait"
                        and isinstance(getattr(fn, "__self__", None), threading.Event)
                    ):
                        settle_wait_entries.append(True)
                    return orig_to_thread(fn, *args, **kwargs)

                with patch.object(
                    users_mod.asyncio, "to_thread", _spy_to_thread
                ):
                    deadline = time.monotonic() + 2.0
                    while not settle_wait_entries and time.monotonic() < deadline:
                        await asyncio.sleep(0.01)
                assert settle_wait_entries, (
                    "settle-wait ENTRY was never observed: the backstop "
                    "skipped the settlement wait (delete_settled.wait never "
                    "called)"
                )
                release.set()  # worker wakes, raises CancelledError inside txn
                with pytest.raises(asyncio.CancelledError):
                    await task
            finally:
                # Request-scoped release (the Depends(get_db) finally in
                # production): after the coroutine settles, on any outcome.
                # Sample BEFORE releasing (PR #1198 feedback F-04): the
                # settled restore must hold at release time.
                release_time_state.append((env.rows_survive(), env.input_live()))
                env.facade.release_connection(env.conn)

        asyncio.run(drive())
        assert env.rows_survive()
        assert env.input_live(), "rolled-back worker outcome must restore"
        assert env.facade.hook_done_at_release, "connection was released"
        assert release_time_state == [(True, True)], (
            "settlement (restore applied) must already hold when the release runs"
        )
        assert all(
            env.facade.hook_done_at_release
        ), "connection released only after the worker settled"
    finally:
        release.set()
        env.close()


def test_cancel_at_await_worker_commits_keeps_tombstones(tmp_path: Path) -> None:
    """Interleaving (d): commit after cancellation keeps tombstones discarded."""
    release = threading.Event()
    env = _OutcomesEnv(tmp_path, block_after_user_delete=release)
    try:

        async def drive() -> None:
            task = asyncio.create_task(
                delete_user(2, _request_for(2), user=_ADMIN_USER, _csrf_token="t", db=env.conn)
            )
            while not env.conn.after_delete_ran.is_set():
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        async def runner() -> None:
            task = asyncio.create_task(drive())
            while not env.conn.after_delete_ran.is_set():
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.05)
            release.set()  # worker completes: transaction_context commits
            await task

        asyncio.run(runner())
        assert not env.rows_survive(), "the detached worker committed the delete"
        assert not env.input_live(), (
            "tombstones of deleted rows must stay discarded for the reconcile"
        )
    finally:
        release.set()
        env.close()


def test_restore_failure_logs_and_original_exception_wins(tmp_path: Path) -> None:
    env = _OutcomesEnv(tmp_path, raise_after_user_delete=ValueError)
    restore_calls: list[tuple[object, ...]] = []

    def _failing_restore(*args, **kwargs):
        # PR #1198 feedback F-05: assert the backstop actually runs — record
        # the attempt before the scripted failure.
        restore_calls.append(args)
        raise OSError("scripted restore failure")

    try:
        with (
            patch.object(
                DraftDeletionService,
                "restore_purge",
                side_effect=_failing_restore,
            ),
            pytest.raises(ValueError),
        ):
            asyncio.run(delete_user(2, _request_for(2), user=_ADMIN_USER, _csrf_token="t", db=env.conn))
        assert restore_calls, (
            "the restore backstop must actually be attempted before the "
            "primary exception is reported"
        )
    finally:
        env.close()


def test_rollback_failure_does_not_replace_primary_exception(
    tmp_path: Path,
) -> None:
    """KeyboardInterrupt inside the worker skips transaction_context's
    rollback; the backstop's own (scripted-to-fail) rollback must be guarded
    so the KeyboardInterrupt still propagates and the restore still runs."""
    env = _OutcomesEnv(
        tmp_path,
        raise_after_user_delete=KeyboardInterrupt,
        fail_rollback=True,
    )
    try:
        with pytest.raises(KeyboardInterrupt):
            asyncio.run(delete_user(2, _request_for(2), user=_ADMIN_USER, _csrf_token="t", db=env.conn))
        assert env.rows_survive()
        assert env.input_live(), "restore still runs after a failed rollback"
    finally:
        env.close()


def test_executor_submit_failure_restores_purge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Review PRR-003: executor construction/submission lives INSIDE the
    recovery boundary, so a thread-start failure (RuntimeError "can't start
    new thread" under resource exhaustion) takes the same
    rollback-plus-restore path as every other failure in the delete window.
    At base the dispatch was wrapped by the except tuples; leaving it
    outside would have been a regression."""
    env = _OutcomesEnv(tmp_path)

    real_tpe = concurrent.futures.ThreadPoolExecutor

    class _SubmitFailsExecutor(real_tpe):
        """Fails ONLY the delete route's dedicated executor.

        The patch is narrowed to `thread_name_prefix="user_delete"` because
        asyncio's default executor (which `asyncio.to_thread` uses for the
        route's own statements) is also a ThreadPoolExecutor: an unfiltered
        patch would kill the route at its FIRST to_thread — before the purge
        is even prepared — and the test would then assert against a case it
        never exercised (reviewer round-4 catch)."""

        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
            self._fails = kwargs.get("thread_name_prefix") == "user_delete"
            super().__init__(*args, **kwargs)

        def submit(self, fn, *args, **kwargs):  # noqa: ANN001, ANN003
            if self._fails:
                raise RuntimeError("can't start new thread")
            return super().submit(fn, *args, **kwargs)

    monkeypatch.setattr(
        users_mod.concurrent.futures, "ThreadPoolExecutor", _SubmitFailsExecutor
    )
    try:
        # With the fix the submit failure is caught by the recovery arms and
        # surfaces as a 500; with construction/submit outside the boundary
        # (the reverted shape) the RuntimeError propagates instead — either
        # way the tombstones must be restored, which is the discriminator.
        with pytest.raises((HTTPException, RuntimeError)):
            asyncio.run(delete_user(2, _request_for(2), user=_ADMIN_USER, _csrf_token="t", db=env.conn))
        assert env.rows_survive(), "the delete did not commit"
        assert env.input_live(), "a submit failure must still restore the tombstones"
    finally:
        env.close()
        # monkeypatch's own teardown restores the module attribute.


def test_second_cancel_during_settle_wait_is_drained_not_abandoning(tmp_path: Path) -> None:
    """Issue #1031 hardening of the former PRR-005 disclosed residual: a
    SECOND cancellation delivered while the backstop waits for the detached
    worker no longer aborts the wait — the settle-wait is DRAINED, so
    settlement is observed, the (non-committed) restore runs, and only then
    does the original CancelledError propagate. Containment still holds:
    the worker reaches its terminal outcome, and the connection is released
    exactly once by the test's own request-scope finally (mirroring the
    Depends(get_db) teardown in production) — after settlement. The route
    itself holds no release call at head; the facade records that single
    release (PR #1198 feedback F-04 docstring correction)."""
    release = threading.Event()
    env = _OutcomesEnv(
        tmp_path,
        block_after_user_delete=release,
        raise_after_user_delete=asyncio.CancelledError(),
    )
    release_calls: list[bool] = []
    hook_done_at_second_cancel: list[bool] = []
    orig_release = env.facade.release_connection

    def _release_probe(c) -> None:  # noqa: ANN001
        release_calls.append(env.conn.hook_done.is_set())
        orig_release(c)

    env.facade.release_connection = _release_probe
    try:

        async def drive() -> None:
            task = asyncio.create_task(
                delete_user(2, _request_for(2), user=_ADMIN_USER, _csrf_token="t", db=env.conn)
            )
            while not env.conn.after_delete_ran.is_set():
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.05)  # backstop parks on the settle-wait
            task.cancel()  # first cancellation
            await asyncio.sleep(0.05)
            task.cancel()  # second: absorbed by the drained settle-wait
            # Sample BEFORE the worker is released: the assertions below are
            # only meaningful if both cancellations landed while the worker
            # was still parked in its hook.
            hook_done_at_second_cancel.append(env.conn.hook_done.is_set())
            # Release the worker NOW: it raises CancelledError inside the
            # still-open transaction (non-commit outcome), the drain
            # observes settlement, the restore runs, and the route's
            # original cancellation propagates only afterwards.
            release.set()
            try:
                with pytest.raises(asyncio.CancelledError):
                    await task
            finally:
                # Request-scoped release (the Depends(get_db) finally in
                # production): after the coroutine settles, on any outcome.
                env.facade.release_connection(env.conn)

        asyncio.run(drive())
        assert hook_done_at_second_cancel == [False], (
            "both cancellations must land while the worker is still parked, "
            "otherwise the assertions below are vacuous"
        )
        assert env.conn.block_released_by_gate is True, (
            "the worker was released by the gate this test owns, not by the "
            "scripted hook's 10s fallback"
        )
        assert env.conn.hook_done.wait(timeout=5), "the detached worker finished its item"
        assert len(release_calls) == 1, "connection released exactly once"
        assert release_calls == [True], (
            "post-#1031 contract: the drained backstop releases the "
            "connection only AFTER the worker settled — the restore is no "
            "longer abandoned by a second cancellation"
        )
        assert env.rows_survive(), "the rolled-back delete kept the user row"
        assert env.input_live(), (
            "the restore ran even after the second cancellation — the "
            "disclosed residual is closed"
        )
    finally:
        release.set()
        env.close()
