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
  worker settles.
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
        self._saved_get_pool = users_mod.get_pool
        users_mod.get_pool = lambda _path: self.facade
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
        users_mod.get_pool = self._saved_get_pool
        settings.data_dir = self._saved_data_dir
        self.real_pool.close_all()
        _pool_cache.clear()


def test_value_error_after_delete_restores_purge(tmp_path: Path) -> None:
    """Plan-critic round 1's disproof of the verbatim-mirror design."""
    env = _OutcomesEnv(tmp_path, raise_after_user_delete=ValueError)
    try:
        with pytest.raises(ValueError):
            asyncio.run(delete_user(2, _request_for(2), user=_ADMIN_USER, _csrf_token="t"))
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
                delete_user(2, _request_for(2), user=_ADMIN_USER, _csrf_token="t")
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
    transaction_context rollback), backstop rolls back and restores."""
    release = threading.Event()
    env = _OutcomesEnv(
        tmp_path,
        block_after_user_delete=release,
        raise_after_user_delete=asyncio.CancelledError(),
    )
    try:

        async def drive() -> None:
            task = asyncio.create_task(
                delete_user(2, _request_for(2), user=_ADMIN_USER, _csrf_token="t")
            )
            while not env.conn.after_delete_ran.is_set():
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.05)  # backstop parks on the settle-wait
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        async def runner() -> None:
            task = asyncio.create_task(drive())
            while not env.conn.after_delete_ran.is_set():
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.05)
            release.set()  # worker wakes, raises CancelledError inside txn
            await task

        asyncio.run(runner())
        assert env.rows_survive()
        assert env.input_live(), "rolled-back worker outcome must restore"
        assert env.facade.hook_done_at_release, "connection was released"
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
                delete_user(2, _request_for(2), user=_ADMIN_USER, _csrf_token="t")
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
    try:
        with (
            patch.object(
                DraftDeletionService,
                "restore_purge",
                side_effect=OSError("scripted restore failure"),
            ),
            pytest.raises(ValueError),
        ):
            asyncio.run(delete_user(2, _request_for(2), user=_ADMIN_USER, _csrf_token="t"))
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
            asyncio.run(delete_user(2, _request_for(2), user=_ADMIN_USER, _csrf_token="t"))
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
            asyncio.run(delete_user(2, _request_for(2), user=_ADMIN_USER, _csrf_token="t"))
        assert env.rows_survive(), "the delete did not commit"
        assert env.input_live(), "a submit failure must still restore the tombstones"
    finally:
        env.close()
        # monkeypatch's own teardown restores the module attribute.


def test_second_cancel_during_settle_wait_is_disclosed_and_contained(tmp_path: Path) -> None:
    """Review PRR-005: a SECOND cancellation delivered while the backstop
    waits for the detached worker aborts the restore. The protocol discloses
    this residual; this test pins the CONTAINMENT that the disclosure rests
    on — the worker still reaches its terminal outcome, the connection is
    released exactly once, and the route's own finally completes (no leak of
    the executor handle)."""
    release = threading.Event()
    env = _OutcomesEnv(tmp_path, block_after_user_delete=release)
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
                delete_user(2, _request_for(2), user=_ADMIN_USER, _csrf_token="t")
            )
            while not env.conn.after_delete_ran.is_set():
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.05)  # backstop parks on the settle-wait
            task.cancel()  # first cancellation
            await asyncio.sleep(0.05)
            task.cancel()  # second: aborts the wait (the disclosed residual)
            # Sample BEFORE the worker is released: the containment assertions
            # below are only meaningful if the route was abandoned while the
            # worker was still parked in its hook. A worker that had already
            # finished would make them vacuously true.
            hook_done_at_second_cancel.append(env.conn.hook_done.is_set())
            with pytest.raises(asyncio.CancelledError):
                await task
            # The route has unwound. Release the worker here — not in this
            # test's finally — so its terminal outcome is observed promptly
            # instead of waiting out the scripted hook's 10s fallback.
            release.set()

        asyncio.run(drive())
        assert hook_done_at_second_cancel == [False], (
            "the second cancellation must land while the worker is still "
            "parked, otherwise the containment assertions below are vacuous"
        )
        assert env.conn.block_released_by_gate is True, (
            "the worker was released by the gate this test owns, not by the "
            "scripted hook's 10s fallback — a timeout-released worker is what "
            "made the pre-fix ordering pass"
        )
        # The worker always reaches its terminal outcome even though the
        # route abandoned it, and the connection is still released exactly
        # once — the containment the disclosed residual rests on.
        assert env.conn.hook_done.wait(timeout=5), "the detached worker finished its item"
        assert len(release_calls) == 1, "connection released exactly once"
        assert release_calls == [False], (
            "the disclosed residual: the route released the connection while "
            "the worker was still parked, and the worker settled anyway"
        )
    finally:
        release.set()
        env.close()
