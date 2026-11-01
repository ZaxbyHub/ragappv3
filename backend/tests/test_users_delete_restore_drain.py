"""Issue #1036 C4: delete_user's non-commit restore path must survive a
second cancellation, and the settle-wait must settle on the event truth.

Inside ``delete_user``'s ``except BaseException`` arm, the non-commit path
(users.py) guards its rollback await and its ``restore_purge`` await only
with ``except Exception`` handlers. A worker failure of BaseException class
inside the write transaction (the class the arm exists for) followed by a
SECOND task cancellation delivered at the rollback await escapes the arm, so
``restore_purge`` never runs and the tombstoned Draft Room input bytes stay
stranded in ``.trash`` while the user row survived.

Leg 1 (RED at base, GREEN after the fix) drives the REAL route in exactly
that interleaving: the connection's ``execute`` raises a BaseException
subclass at the user DELETE (the transaction stays open), and a cancellation
is delivered to the ROUTE task (captured by reference — never
``current_task()``, which would aim at whatever drained child task a fixed
design runs the rollback on) at the rollback await via the ``to_thread``
lambda hook. The route task must end cancelled — but the restore MUST still
complete: the input file back at its original path, ``.trash`` empty, the
user row alive.

Leg 2 (PASSES at base and must keep passing — it pins the #1035 drained
settle-wait behavior at the boundary): the first settle-wait task
(``to_thread(delete_settled.wait)``) fails once while the worker is parked
mid-transaction; the arm must settle on the EVENT truth before acting — the
committed disposition still runs (rows gone, audit row, ``.trash``
discarded), never a restore/rollback against a mid-transaction worker.

Harness copied from test_users_delete_post_commit_on_cancel.py /
probe_restore_drain.py (draft + staged input file + DraftStore
reserve/finalize + pool facade + starlette Request construction).
"""

import asyncio
import os

os.environ["JWT_SECRET_KEY"] = (
    "test-jwt-secret-key-for-testing-only-12345678901234567890"
)
os.environ["USERS_ENABLED"] = "true"
os.environ["ADMIN_SECRET_TOKEN"] = (
    "restore-drain-admin-secret-0123456789abcdef0123456789abcdef"
)

import sqlite3  # noqa: E402
import threading  # noqa: E402
from pathlib import Path  # noqa: E402

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

VICTIM_USER_ID = 2
VICTIM_INPUT_BYTES = b"victim user's private manuscript bytes"


class Boom(BaseException):
    """A BaseException-class worker failure: skips transaction_context's
    ``except Exception`` rollback exactly like asyncio.CancelledError would."""


class _FakeUpload:
    def __init__(self, filename: str, content: bytes) -> None:
        self.filename = filename
        self._content = content
        self._offset = 0

    async def read(self, size: int = -1) -> bytes:
        if size < 0:
            chunk = self._content[self._offset :]
            self._offset = len(self._content)
            return chunk
        chunk = self._content[self._offset : self._offset + size]
        self._offset += size
        return chunk


class _BoomConn:
    """Hands every statement to the real connection; the user DELETE raises
    Boom(BaseException) inside the worker's transaction, so the connection
    stays inside its open transaction (the exact class the ``except
    BaseException`` arm's guarded rollback exists for)."""

    def __init__(self, inner: sqlite3.Connection) -> None:
        self._inner = inner

    def execute(self, sql, params=()):  # noqa: ANN001
        if sql.strip().upper().startswith("DELETE FROM USERS"):
            raise Boom("scripted worker failure inside the delete transaction")
        return self._inner.execute(sql, params)

    def __getattr__(self, name):  # noqa: ANN001
        return getattr(self._inner, name)


class _ParkConn:
    """Parks the worker thread INSIDE the user DELETE until the release gate:
    ``delete_settled`` stays unset while the route is already cancelled, so
    the arm's settle-wait loop must wait on the real event."""

    def __init__(self, inner: sqlite3.Connection, release_gate: threading.Event) -> None:
        self._inner = inner
        self._release_gate = release_gate
        self.after_delete_ran = threading.Event()

    def execute(self, sql, params=()):  # noqa: ANN001
        result = self._inner.execute(sql, params)
        if sql.strip().upper().startswith("DELETE FROM USERS"):
            self.after_delete_ran.set()
            self._release_gate.wait(timeout=30)
        return result

    def __getattr__(self, name):  # noqa: ANN001
        return getattr(self._inner, name)


class _PoolFacade:
    """Pool facade over the real pool handing the route the scripted conn."""

    def __init__(self, real_pool: SQLiteConnectionPool, conn) -> None:
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


class _RestoreEnv:
    """Per-test world: temp DB, real draft bytes, scripted conn, patched pool."""

    def __init__(self, tmp_path: Path) -> None:
        _pool_cache.clear()
        self.db_path = str(tmp_path / "restore-drain.db")
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
                (VICTIM_USER_ID, "victim", "abc123", "member", 1),
            )
            seed.execute(
                "INSERT INTO vaults (id, name) VALUES (1, 'RestoreDrainVault')"
            )
            seed.commit()
            draft_store = DraftStore(seed)
            draft = draft_store.create_draft(
                vault_id=1,
                created_by=VICTIM_USER_ID,
                title="restore drain draft",
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
                owner_id=VICTIM_USER_ID,
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
        self.conn = None
        self.facade = None
        self._saved_get_pool = users_mod.get_pool
        users_mod.get_pool = lambda _path: self.facade
        self._saved_data_dir = settings.data_dir
        settings.data_dir = tmp_path

    def bind(self, conn) -> None:
        """Hand the route its scripted connection through the facade."""
        self.conn = conn
        self.facade = _PoolFacade(self.real_pool, conn)

    def scalar(self, sql: str, params: tuple = ()) -> object:
        check = sqlite3.connect(self.db_path)
        try:
            return check.execute(sql, params).fetchone()[0]
        finally:
            check.close()

    def trash_entries(self) -> list[str]:
        trash_dir = self.storage_root / ".trash"
        if not trash_dir.is_dir():
            return []
        return sorted(entry.name for entry in trash_dir.rglob("*") if entry.is_file())

    def close(self) -> None:
        users_mod.get_pool = self._saved_get_pool
        settings.data_dir = self._saved_data_dir
        try:
            self.real_pool.release_connection(self.conn._inner)
        except Exception:
            pass
        self.real_pool.close_all()
        _pool_cache.clear()


def test_second_cancel_at_rollback_await_still_restores_purge(tmp_path: Path) -> None:
    """RED at base, GREEN after the fix.

    A BaseException-class worker failure inside the delete transaction (rows
    survive, transaction stays open) followed by a cancellation delivered at
    the arm's guarded rollback await may end the handler task — but the
    restore MUST still complete: input bytes back at the live path, ``.trash``
    empty, user row alive.
    """
    env = _RestoreEnv(tmp_path)
    env.bind(_BoomConn(env.real_pool.get_connection()))
    real_to_thread = asyncio.to_thread
    state = {"rollback_cancel_fired": False}
    route_holder: dict = {"task": None}

    async def scripted(fn, *args, **kwargs):  # noqa: ANN002, ANN003
        if (
            not state["rollback_cancel_fired"]
            and getattr(fn, "__name__", "") == "<lambda>"
            and not args
        ):
            state["rollback_cancel_fired"] = True
            route_holder["task"].cancel()
            await asyncio.sleep(0)
            # At base the wrapper runs inline on the route task, so the
            # pending cancellation lands at this yield — inside the `except
            # BaseException` arm, where only `except Exception` handlers
            # guard it. Under a drained fix the rollback runs on a child
            # task; the route's cancellation is absorbed until the rollback
            # (and the restore below it) complete, then propagates.
        return await real_to_thread(fn, *args, **kwargs)

    async def drive() -> str:
        asyncio.to_thread = scripted
        route_holder["task"] = asyncio.ensure_future(
            delete_user(
                VICTIM_USER_ID,
                _request_for(VICTIM_USER_ID),
                user=_ADMIN_USER,
                _csrf_token="t",
            )
        )
        try:
            await route_holder["task"]
            return "returned"
        except asyncio.CancelledError:
            return "cancelled"
        except Boom:
            return "boom-propagated"
        finally:
            asyncio.to_thread = real_to_thread

    try:
        outcome = asyncio.run(drive())
    finally:
        env.close()

    assert state["rollback_cancel_fired"], (
        "non-vacuity: the cancellation must have been delivered at the "
        "arm's guarded rollback await (the to_thread lambda hook never "
        "fired) — otherwise this run proved nothing"
    )
    assert outcome == "cancelled", (
        "the route task must end cancelled: the delivered cancellation "
        "propagates after the restore completes (got "
        f"{outcome!r})"
    )
    user_rows = env.scalar("SELECT COUNT(*) FROM users WHERE id = ?", (VICTIM_USER_ID,))
    assert user_rows == 1, (
        "precondition: the non-commit path ran (the user row must have "
        "survived the BaseException-class worker failure)"
    )
    assert env.storage.exists(env.input_relpath), (
        "restore skipped: tombstoned bytes stranded in .trash while the "
        "user row survived — the cancellation delivered at the guarded "
        "rollback await escaped the `except BaseException` arm and "
        "restore_purge never ran (handler outcome: "
        f"{outcome}; .trash entries: {env.trash_entries()})"
    )
    assert env.trash_entries() == [], (
        "restore skipped: tombstoned bytes stranded in .trash "
        f"({env.trash_entries()}) while the user row survived"
    )


def test_failing_settle_wait_task_settles_on_event_truth(tmp_path: Path) -> None:
    """PASSES at base and must keep passing (pins the #1035 drained
    settle-wait contract at this boundary): a settle-wait task that FAILS
    while the worker is parked mid-transaction must be retried until the
    worker's event settles — the arm never mistakes the failed wait for an
    observed settlement and never acts (rollback/restore) while the worker is
    still inside its transaction."""
    gate = threading.Event()
    env = _RestoreEnv(tmp_path)
    env.bind(_ParkConn(env.real_pool.get_connection(), gate))
    real_to_thread = asyncio.to_thread
    state = {"first_wait_failed": False}

    async def scripted(fn, *args, **kwargs):  # noqa: ANN002, ANN003
        if (
            not state["first_wait_failed"]
            and getattr(fn, "__name__", "") == "wait"
            and not args
        ):
            state["first_wait_failed"] = True
            raise RuntimeError("scripted settle-wait failure (must be retried)")
        return await real_to_thread(fn, *args, **kwargs)

    try:
        async def scenario() -> None:
            asyncio.to_thread = scripted
            try:
                task = asyncio.ensure_future(
                    delete_user(
                        VICTIM_USER_ID,
                        _request_for(VICTIM_USER_ID),
                        user=_ADMIN_USER,
                        _csrf_token="t",
                    )
                )
                deadline = asyncio.get_event_loop().time() + 30
                while not env.conn.after_delete_ran.is_set():
                    if asyncio.get_event_loop().time() > deadline:
                        raise AssertionError("worker never reached the user DELETE")
                    await asyncio.sleep(0.01)
                await asyncio.sleep(0.05)  # route parks on the delete await
                task.cancel()  # enters the except BaseException arm
                while not state["first_wait_failed"]:
                    if asyncio.get_event_loop().time() > deadline:
                        raise AssertionError("settle-wait was never dispatched")
                    await asyncio.sleep(0.01)
                gate.set()  # worker COMMITS; delete_settled settles
                with pytest.raises(asyncio.CancelledError):
                    await task
            finally:
                asyncio.to_thread = real_to_thread

        asyncio.run(scenario())
    finally:
        gate.set()
        env.close()

    assert state["first_wait_failed"], (
        "non-vacuity: the first settle-wait task must actually have failed"
    )
    user_rows = env.scalar("SELECT COUNT(*) FROM users WHERE id = ?", (VICTIM_USER_ID,))
    assert user_rows == 0, (
        "the arm settled on the event truth: the parked worker COMMITTED — "
        "a failed settle-wait must never be mistaken for an observed "
        "non-commit (rollback/restore while the worker was mid-transaction)"
    )
    audit_rows = env.scalar(
        "SELECT COUNT(*) FROM security_audit_log "
        "WHERE event_type = 'user.deleted' AND target_user_id = ?",
        (VICTIM_USER_ID,),
    )
    assert audit_rows == 1, "the committed disposition (audit row) still ran"
    assert env.trash_entries() == [], "commit_purge finalized the discard"
    assert not env.storage.exists(env.input_relpath), (
        "the committed disposition discarded the input bytes"
    )
