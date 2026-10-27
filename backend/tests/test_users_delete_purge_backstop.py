"""Issue #1019 acceptance checks: delete_user purge-recovery backstop.

Arm's-length acceptance checks (issue-tracer Phase 2.5), collated trace
#815/#823/#1019 (AC6/AC8). Behavior is RED at base, GREEN after the fix:

1. test_cancelled_error_in_delete_window_restores_purge_and_rolls_back
   — an ``asyncio.CancelledError`` (a class outside
   ``HTTPException | sqlite3.Error | OSError | RuntimeError``) delivered
   inside the delete transaction AFTER ``prepare_purge_for_user`` has
   tombstoned the Draft Room input bytes must roll the transaction back and
   restore every purged input byte before the exception propagates.
   RED at base: CancelledError matches neither recovery handler, so it
   escapes delete_user with the tombstones stranded in ``.trash`` while the
   rows survive.

2. test_post_commit_cancel_does_not_restore_purge
   — PRESERVING pin: a CancelledError raised AFTER the delete transaction
   committed (at the post-commit ``commit_purge`` await) must NOT restore
   the tombstones — the bytes belong to deleted rows and the startup
   reconcile owns them. This already holds at base (``except Exception``
   around commit_purge lets the CancelledError escape without touching the
   tombstones) and must keep holding after a backstop gated on
   ``conn.in_transaction`` is added.

Harness follows the proven direct-call recipe from
backend/tests/test_d05_vault_delete_integrity.py (real temp DB via init_db,
real SQLiteConnectionPool, real DraftStore/DraftInputStorage bytes on disk,
delete route called directly with a minimal ASGI Request) plus the
users.get_pool patch from test_d04_user_mutation_invariants.py. The pool is
a thin facade so the connection delete_user borrows can be wrapped: the
wrapper lets every statement run and raises CancelledError only after the
user DELETE executes inside the still-open BEGIN IMMEDIATE — the faithful
"cancellation delivered at the to_thread await while the executor thread
completed the detached statement" window.
"""

import asyncio
import os

# Set up test environment BEFORE importing app modules (d04 recipe).
os.environ["JWT_SECRET_KEY"] = (
    "test-jwt-secret-key-for-testing-only-12345678901234567890"
)
os.environ["USERS_ENABLED"] = "true"
os.environ["ADMIN_SECRET_TOKEN"] = (
    "backstop-admin-secret-token-0123456789abcdef0123456789abcdef"
)

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest
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
    """Minimal async-file object accepted by ``DraftInputStorage.stage_upload``."""

    def __init__(self, filename: str, content: bytes) -> None:
        self.filename = filename
        self._content = content
        self._offset = 0

    async def read(self, size: int = -1) -> bytes:
        if size < 0:
            chunk, self._offset = self._content[self._offset :], len(self._content)
            return chunk
        chunk = self._content[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk


class _CancelAfterUserDeleteConn:
    """Connection wrapper raising CancelledError once the user DELETE ran.

    Every statement executes for real on the inner connection; only after
    ``DELETE FROM users ...`` executes (inside the route's open
    ``transaction_context`` BEGIN IMMEDIATE, before any commit) does the
    wrapper raise — modeling a CancelledError delivered at the
    ``await asyncio.to_thread(_delete_user_row)`` cancellation point after
    the detached executor thread completed the statement.
    """

    def __init__(self, inner: sqlite3.Connection, cancel_on_user_delete: bool) -> None:
        self._inner = inner
        self._cancel_on_user_delete = cancel_on_user_delete

    def execute(self, sql, params=()):  # noqa: ANN001 - sqlite3 signature
        result = self._inner.execute(sql, params)
        if self._cancel_on_user_delete and sql.strip().upper().startswith(
            "DELETE FROM USERS"
        ):
            raise asyncio.CancelledError()
        return result

    def __getattr__(self, name):  # noqa: ANN001
        return getattr(self._inner, name)


class _PoolFacade:
    """Pool stand-in handing delete_user the wrapped connection.

    ``release_connection`` mirrors the production pool's contract
    (backend/app/models/database.py SQLiteConnectionPool.release_connection):
    a connection holding an open transaction is rolled back first — at base
    this release-time rollback is exactly what keeps the surviving rows
    intact when the escaping CancelledError skipped every handler.
    """

    def __init__(self, real_pool: SQLiteConnectionPool, conn: _CancelAfterUserDeleteConn):
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


def _delete_scope(user_id: int) -> dict:
    """Minimal ASGI scope for calling ``delete_user`` directly (probe pattern)."""
    return {
        "type": "http",
        "method": "DELETE",
        "headers": [],
        "path": f"/api/users/{user_id}",
        "query_string": b"",
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
        "scheme": "http",
    }


class _Env:
    """Per-test temp world: DB, pool, draft-room storage, patched route bits."""

    def __init__(self, tmp_path: Path, cancel_on_user_delete: bool) -> None:
        _pool_cache.clear()
        self.tmp_path = tmp_path
        self.db_path = str(tmp_path / "backstop.db")
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
            seed.execute("INSERT INTO vaults (id, name) VALUES (1, 'BackstopVault')")
            seed.commit()

            draft_store = DraftStore(seed)
            draft = draft_store.create_draft(
                vault_id=1,
                created_by=2,
                title="backstop draft",
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

        # Sanity precondition: the victim's bytes are live on disk before the
        # route runs.
        assert self.storage.exists(self.input_relpath)

        borrow_conn = _CancelAfterUserDeleteConn(
            self.real_pool.get_connection(), cancel_on_user_delete
        )
        self.facade = _PoolFacade(self.real_pool, borrow_conn)

        self._saved_get_pool = users_mod.get_pool
        users_mod.get_pool = lambda _path: self.facade
        self._saved_data_dir = settings.data_dir
        settings.data_dir = tmp_path

    def call_delete_user(self) -> None:
        asyncio.run(
            delete_user(
                2,
                Request(_delete_scope(2)),
                dict(_ADMIN_USER),
                "test-csrf",
            )
        )

    def committed_conn(self) -> sqlite3.Connection:
        """A fresh connection: only committed state is visible through it."""
        return sqlite3.connect(self.db_path)

    def close(self) -> None:
        users_mod.get_pool = self._saved_get_pool
        settings.data_dir = self._saved_data_dir
        self.real_pool.close_all()
        _pool_cache.clear()


def test_cancelled_error_in_delete_window_restores_purge_and_rolls_back(tmp_path):
    """AC6 (DISCRIMINATING): CancelledError in the delete window must restore.

    RED at base, GREEN after the fix. At base the CancelledError matches
    neither the ``except HTTPException`` nor the
    ``except (sqlite3.Error, OSError, RuntimeError)`` handler, so it escapes
    ``delete_user`` without ``restore_purge``: the rows survive (the DELETE
    stays uncommitted and the pool's release-time rollback discards it) while
    the victim's input bytes stay tombstoned in ``.trash`` — the silent data
    loss the issue files.
    """
    env = _Env(tmp_path, cancel_on_user_delete=True)
    try:
        with pytest.raises(asyncio.CancelledError):
            env.call_delete_user()

        check = env.committed_conn()
        try:
            # Rows side: the user and their draft SURVIVE (rollback).
            user_rows = check.execute(
                "SELECT COUNT(*) FROM users WHERE id = 2"
            ).fetchone()[0]
            assert user_rows == 1, "victim user row must survive the rollback"
            draft_rows = check.execute(
                "SELECT COUNT(*) FROM drafts WHERE created_by = 2"
            ).fetchone()[0]
            assert draft_rows == 1, "victim draft row must survive the rollback"

            # Bytes side: every purged input byte must be BACK at its original
            # path (restore_purge ran before the exception propagated).
            # RED at base: the bytes stay tombstoned in .trash.
            assert env.storage.exists(env.input_relpath), (
                "purged input bytes must be restored to their original path"
            )
            assert (
                env.storage.resolve(env.input_relpath).read_bytes()
                == VICTIM_INPUT_BYTES
            )
        finally:
            check.close()
    finally:
        env.close()


def test_post_commit_cancel_does_not_restore_purge(tmp_path):
    """AC8-adjacent PRESERVING pin: post-commit cancel must NOT restore.

    This one already passes at base and must keep passing after the fix: the
    tombstones belong to rows the committed delete already cascade-deleted,
    so restoring them would resurrect orphaned bytes; the startup reconcile
    owns them instead. A backstop gated on ``conn.in_transaction`` keeps this
    behavior (no transaction is open after COMMIT).
    """
    env = _Env(tmp_path, cancel_on_user_delete=False)
    try:
        with patch.object(
            DraftDeletionService,
            "commit_purge",
            side_effect=asyncio.CancelledError(),
        ):
            with pytest.raises(asyncio.CancelledError):
                env.call_delete_user()

        check = env.committed_conn()
        try:
            # The delete already committed: the user and their drafts are gone
            # and must stay gone.
            user_rows = check.execute(
                "SELECT COUNT(*) FROM users WHERE id = 2"
            ).fetchone()[0]
            assert user_rows == 0
            draft_rows = check.execute(
                "SELECT COUNT(*) FROM drafts WHERE created_by = 2"
            ).fetchone()[0]
            assert draft_rows == 0

            # The tombstones are NOT restored: the bytes stay in .trash for
            # the startup reconcile — they must not reappear at the original
            # path of a deleted row.
            assert not env.storage.exists(env.input_relpath), (
                "post-commit tombstones must not be restored to deleted rows' paths"
            )
        finally:
            check.close()
    finally:
        env.close()
