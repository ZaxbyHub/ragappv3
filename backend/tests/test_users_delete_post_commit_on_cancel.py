"""Issue #1031 C1: post-commit bookkeeping must survive cancel-at-await-with-commit.

The delete_user recovery backstop (issue #1019) made the tombstone outcome
worker-aware, but its ``except BaseException`` arm still re-raises after
observing the commit — so the post-commit bookkeeping that lives AFTER the
try/except block never runs in exactly the interleaving where a detached
worker COMMITS while the request is cancelled at
``await asyncio.wrap_future(delete_future)``:

- ``commit_purge`` — the tombstoned Draft Room input bytes stay stranded in
  ``.trash`` instead of being permanently discarded;
- ``invalidate_active_user_cache(user_id)`` — the active-user cache keeps the
  deleted principal;
- the ``user.deleted`` ``security_audit_log`` event — the deletion leaves no
  tamper-evident audit trail.

This check drives the REAL route with the same gate interleaving proven in
``test_users_delete_purge_recovery_outcomes.py::test_cancel_at_await_worker_commits_keeps_tombstones``
(the worker parks on a gate after the user DELETE; the route await is
cancelled; the gate is released so the detached worker COMMITS), then asserts
all three bookkeeping outcomes ran. Exactly ONE test lives in this file.
"""

import asyncio
import os
import threading

os.environ["JWT_SECRET_KEY"] = (
    "test-jwt-secret-key-for-testing-only-12345678901234567890"
)
os.environ["USERS_ENABLED"] = "true"
os.environ["ADMIN_SECRET_TOKEN"] = (
    "postcommit-admin-secret-token-0123456789abcdef0123456789abcdef"
)

import sqlite3  # noqa: E402
from pathlib import Path  # noqa: E402
from unittest.mock import patch  # noqa: E402

import pytest  # noqa: E402
from starlette.requests import Request  # noqa: E402

from app.api.routes import users as users_mod  # noqa: E402
from app.api.routes.users import delete_user  # noqa: E402
from app.config import settings  # noqa: E402
from app.models.database import SQLiteConnectionPool, _pool_cache, init_db  # noqa: E402
from app.services.draft_input_storage import DraftInputStorage  # noqa: E402
from app.services.draft_store import DraftStore  # noqa: E402

_ADMIN_USER = {
    "id": 1,
    "username": "admin",
    "full_name": "Admin",
    "role": "superadmin",
    "is_active": True,
    "must_change_password": False,
}

VICTIM_INPUT_BYTES = b"victim user's private manuscript bytes"
VICTIM_USER_ID = 2


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
    """Connection wrapper parking the worker thread after the user DELETE.

    Same gate pattern as test_users_delete_purge_recovery_outcomes: after the
    ``DELETE FROM users`` statement the WORKER THREAD blocks on
    ``release_gate`` (the test owns when the worker's commit becomes
    observable), while the route's await is cancelled.
    """

    def __init__(self, inner: sqlite3.Connection, release_gate: threading.Event) -> None:
        self._inner = inner
        self._release_gate = release_gate
        self.after_delete_ran = threading.Event()

    def execute(self, sql, params=()):  # noqa: ANN001
        result = self._inner.execute(sql, params)
        if sql.strip().upper().startswith("DELETE FROM USERS"):
            self.after_delete_ran.set()
            self._release_gate.wait(timeout=10)
        return result

    def __getattr__(self, name):  # noqa: ANN001
        return getattr(self._inner, name)


class _ReleaseTrackingPool:
    """Pool facade over the real pool handing the route the scripted conn."""

    def __init__(self, real_pool: SQLiteConnectionPool, conn: _ScriptedConn) -> None:
        self._real_pool = real_pool
        self._conn = conn

    async def get_connection_async(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return self._conn

    def release_connection(self, _conn) -> None:
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


class _CancelCommitEnv:
    """Per-test world: temp DB, real draft bytes, scripted conn, patched pool."""

    def __init__(self, tmp_path: Path) -> None:
        _pool_cache.clear()
        self.db_path = str(tmp_path / "postcommit.db")
        init_db(self.db_path)
        self.real_pool = SQLiteConnectionPool(self.db_path, max_size=2)
        self.storage_root = tmp_path / "draft-room"
        self.storage = DraftInputStorage(self.storage_root)

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
            seed.execute("INSERT INTO vaults (id, name) VALUES (1, 'PostCommitVault')")
            seed.commit()
            draft_store = DraftStore(seed)
            draft = draft_store.create_draft(
                vault_id=1,
                created_by=2,
                title="postcommit draft",
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
        self.gate = threading.Event()
        self.conn = _ScriptedConn(self.real_pool.get_connection(), self.gate)
        self.facade = _ReleaseTrackingPool(self.real_pool, self.conn)
        self._saved_get_pool = users_mod.get_pool
        users_mod.get_pool = lambda _path: self.facade
        self._saved_data_dir = settings.data_dir
        settings.data_dir = tmp_path

    def scalar(self, sql: str, params: tuple) -> object:
        check = self.real_pool.get_connection()
        try:
            return check.execute(sql, params).fetchone()[0]
        finally:
            self.real_pool.release_connection(check)

    def trash_entries(self) -> list[str]:
        trash_dir = self.storage_root / ".trash"
        if not trash_dir.is_dir():
            return []
        return sorted(entry.name for entry in trash_dir.iterdir())

    def close(self) -> None:
        users_mod.get_pool = self._saved_get_pool
        settings.data_dir = self._saved_data_dir
        self.real_pool.close_all()
        _pool_cache.clear()


def test_cancel_at_await_with_commit_still_runs_post_commit_bookkeeping(
    tmp_path: Path,
) -> None:
    """Cancel at the await + detached worker COMMITS -> bookkeeping still runs.

    At the pre-fix base the ``except BaseException`` arm re-raises right after
    observing the commit, so commit_purge, invalidate_active_user_cache and
    the ``user.deleted`` audit event are all skipped — every assertion below
    fails there.
    """
    env = _CancelCommitEnv(tmp_path)
    try:
        with patch.object(
            users_mod, "invalidate_active_user_cache"
        ) as invalidate_spy:

            async def drive() -> None:
                task = asyncio.create_task(
                    delete_user(
                        VICTIM_USER_ID,
                        _request_for(VICTIM_USER_ID),
                        user=_ADMIN_USER,
                        _csrf_token="t",
                    )
                )
                while not env.conn.after_delete_ran.is_set():
                    await asyncio.sleep(0.01)
                await asyncio.sleep(0.05)  # worker parked; await ready to cancel
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

            async def runner() -> None:
                task = asyncio.create_task(drive())
                while not env.conn.after_delete_ran.is_set():
                    await asyncio.sleep(0.01)
                await asyncio.sleep(0.05)
                env.gate.set()  # worker wakes and COMMITS the delete
                await task

            asyncio.run(runner())

        # The interleaving itself held: the detached worker committed the
        # delete (rows gone) — otherwise the assertions below would be
        # vacuous.
        user_rows = env.scalar(
            "SELECT COUNT(*) FROM users WHERE id = ?", (VICTIM_USER_ID,)
        )
        assert user_rows == 0, "the detached worker must have committed the delete"

        # (a) exactly one tamper-evident user.deleted audit row for the victim.
        audit_rows = env.scalar(
            "SELECT COUNT(*) FROM security_audit_log "
            "WHERE event_type = 'user.deleted' AND target_user_id = ?",
            (VICTIM_USER_ID,),
        )
        assert audit_rows == 1, (
            "the user.deleted security_audit_log event must be recorded exactly "
            f"once for the deleted victim (found {audit_rows})"
        )

        # (b) the active-user cache was invalidated for the victim id.
        invalidate_calls_for_victim = [
            call
            for call in invalidate_spy.call_args_list
            if call.args == (VICTIM_USER_ID,)
            or call.kwargs.get("user_id") == VICTIM_USER_ID
        ]
        assert invalidate_calls_for_victim, (
            "invalidate_active_user_cache must run for the deleted victim even "
            "when the request is cancelled at the await and the worker commits"
        )

        # (c) the tombstoned Draft Room input bytes were PERMANENTLY
        # discarded: the live path is gone AND .trash no longer holds the
        # tombstone (entry or sidecar).
        assert not env.storage.exists(env.input_relpath), (
            "the deleted user's input bytes must not be back at their live path"
        )
        assert env.trash_entries() == [], (
            "commit_purge must permanently discard the tombstoned bytes "
            f"(.trash still holds: {env.trash_entries()})"
        )
    finally:
        env.gate.set()
        env.close()
