"""Issue #1043 residual 2: delete_vault's recovery arms leak a cancellation
and strand the tombstoned Draft Room bytes.

``delete_vault`` (vaults.py) tombstones every draft input file on disk
(``prepare_purge_for_vault``) before its write transaction. When that
transaction fails mid-purge, the recovery arms run
``await asyncio.to_thread(lambda: conn.rollback())`` and
``await asyncio.to_thread(draft_deletion.restore_purge, draft_purge_plan)``
as PLAIN un-drained awaits (the restore guarded only by ``except Exception``
— which does not catch ``asyncio.CancelledError``). A cancellation delivered
to the route task at either await escapes the arm: the request unwinds
cancelled, ``restore_purge`` never runs, and the vault row's drafts keep
pointing at bytes that now live only in ``.trash``.

Both legs drive the REAL ``delete_vault`` through the
``except (sqlite3.Error, OSError, RuntimeError)`` recovery arm (a
vault-scoped cleanup statement raises sqlite3.Error from a wrapped
connection, mid-transaction, after the tombstones exist) and deliver the
cancellation to the ROUTE task (captured by reference — never
``current_task()``) at the rollback await (leg 1) and at the restore_purge
await (leg 2) via the ``to_thread`` hook. The route task must end
cancelled — but the restore MUST still complete: the input file back at its
original path, ``.trash`` empty, the vault row alive.

Contract asserted at BOTH ends of the fix (RED at base, GREEN after the
fix — the whole recovery sequence is deferred through task_drain.drain_await
so a cancellation delivered inside the arm is absorbed until the restore
completes and only then re-raised). Cancel mechanics template:
test_users_delete_restore_drain.py; seeding shape: test_d05
(draft + staged input file + DraftStore reserve/finalize + direct
``delete_vault`` call).
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
from unittest.mock import AsyncMock, MagicMock  # noqa: E402

from starlette.requests import Request  # noqa: E402

from app.api.routes.vaults import delete_vault  # noqa: E402
from app.config import settings  # noqa: E402
from app.models.database import SQLiteConnectionPool, _pool_cache, init_db  # noqa: E402
from app.services.draft_input_storage import DraftInputStorage  # noqa: E402
from app.services.draft_store import DraftStore  # noqa: E402

VAULT_ID = 1
OWNER_ID = 1
DRAFT_INPUT_BYTES = b"vault draft room manuscript bytes"

_ADMIN_USER = {
    "id": OWNER_ID,
    "username": "admin",
    "full_name": "Admin",
    "role": "superadmin",
    "is_active": True,
    "must_change_password": False,
}


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


class _CleanupFailConn:
    """Hands every statement to the real connection; the FIRST vault-scoped
    cleanup statement (``DELETE FROM wiki_pages ...``, the first entry of
    ``_VAULT_SCOPED_CLEANUP_SQL``) raises sqlite3.OperationalError inside the
    open BEGIN IMMEDIATE transaction, so delete_vault's
    ``except (sqlite3.Error, OSError, RuntimeError)`` recovery arm runs."""

    _MATCH_PREFIX = "DELETE FROM wiki_pages"

    def __init__(self, inner: sqlite3.Connection) -> None:
        self._inner = inner
        self.cleanup_failed = False

    def execute(self, sql, params=()):  # noqa: ANN001
        if _norm_sql(sql).startswith(self._MATCH_PREFIX):
            self.cleanup_failed = True
            raise sqlite3.OperationalError(
                "scripted vault-scoped cleanup failure mid-transaction"
            )
        return self._inner.execute(sql, params)

    def __getattr__(self, name):  # noqa: ANN001
        return getattr(self._inner, name)


def _norm_sql(sql) -> str:
    return " ".join(str(sql).split())


def _is_rollback_lambda(fn, args) -> bool:  # noqa: ANN001
    """True only for the arm's zero-argument ``lambda: conn.rollback()``
    dispatch — discriminated from the other no-arg lambdas by the bound
    attribute name in the lambda's code object."""
    code = getattr(fn, "__code__", None)
    return (
        getattr(fn, "__name__", "") == "<lambda>"
        and not args
        and code is not None
        and "rollback" in code.co_names
    )


class _VaultDrainEnv:
    """Per-test world: temp DB, real draft bytes, wrapped cleanup-fail conn,
    data_dir pointed at the temp tree (delete_vault builds its
    DraftInputStorage from ``settings.data_dir / 'draft-room'``)."""

    def __init__(self, tmp_path: Path) -> None:
        _pool_cache.clear()
        self.db_path = str(tmp_path / "vault-restore-drain.db")
        init_db(self.db_path)
        self.real_pool = SQLiteConnectionPool(self.db_path, max_size=2)
        self.storage_root = tmp_path / "draft-room"
        self.storage = DraftInputStorage(self.storage_root)

        seed = self.real_pool.get_connection()
        try:
            seed.execute(
                "INSERT INTO users (id, username, hashed_password, role, is_active) "
                "VALUES (?, ?, ?, ?, ?)",
                (OWNER_ID, "admin", "abc123", "superadmin", 1),
            )
            seed.execute(
                "INSERT INTO vaults (id, name) VALUES (?, 'RestoreDrainVault')",
                (VAULT_ID,),
            )
            seed.commit()
            draft_store = DraftStore(seed)
            draft = draft_store.create_draft(
                vault_id=VAULT_ID,
                created_by=OWNER_ID,
                title="vault restore drain draft",
                mode="compose",
                tier="standard",
                brief_json="{}",
            )
            staged = asyncio.run(
                self.storage.stage_upload(
                    _FakeUpload("notes.txt", DRAFT_INPUT_BYTES),
                    allowed_extensions={".txt"},
                    max_file_bytes=1_000_000,
                )
            )
            record = draft_store.reserve_input(
                draft_id=draft.id,
                owner_id=OWNER_ID,
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
        self.conn = _CleanupFailConn(self.real_pool.get_connection())
        self._saved_data_dir = settings.data_dir
        settings.data_dir = tmp_path

    def scalar(self, sql: str, params: tuple = ()) -> object:
        check = sqlite3.connect(self.db_path)
        try:
            row = check.execute(sql, params).fetchone()
            return None if row is None else row[0]
        finally:
            check.close()

    def trash_entries(self) -> list[str]:
        trash_dir = self.storage_root / ".trash"
        if not trash_dir.is_dir():
            return []
        return sorted(entry.name for entry in trash_dir.rglob("*") if entry.is_file())

    def close(self) -> None:
        settings.data_dir = self._saved_data_dir
        try:
            if self.conn.in_transaction:
                self.conn.rollback()
        except sqlite3.Error:
            pass
        try:
            self.real_pool.release_connection(self.conn._inner)
        except Exception:
            pass
        self.real_pool.close_all()
        _pool_cache.clear()


def _request_for(vault_id: int) -> Request:
    return Request(
        {
            "type": "http",
            "method": "DELETE",
            "headers": [],
            "path": f"/api/vaults/{vault_id}",
            "query_string": b"",
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
            "scheme": "http",
        }
    )


def _drive_with_cancel_at(matcher, env: _VaultDrainEnv) -> tuple[str, dict]:  # noqa: ANN001
    """Run the REAL delete_vault as a route task; when a to_thread dispatch
    matches ``matcher(fn, args)`` (the recovery arm's rollback await or its
    restore_purge await), cancel the ROUTE task before the dispatched work
    runs. Returns (outcome, hook-state)."""

    real_to_thread = asyncio.to_thread
    state = {"fired": False}
    route_holder: dict = {"task": None}

    async def scripted(fn, *args, **kwargs):  # noqa: ANN002, ANN003
        if not state["fired"] and matcher(fn, args):
            state["fired"] = True
            route_holder["task"].cancel()
            await asyncio.sleep(0)
            # At base the arm's awaits run inline on the route task, so the
            # pending cancellation lands at this yield — inside the recovery
            # arm, where only `except Exception` guards the restore. Under a
            # drained fix the recovery sequence runs on a child task; the
            # route's cancellation is absorbed until the rollback AND the
            # restore complete, then propagates.
        return await real_to_thread(fn, *args, **kwargs)

    vector_store = MagicMock()
    vector_store.delete_by_vault = AsyncMock(return_value=0)

    async def drive() -> str:
        asyncio.to_thread = scripted
        try:
            route_holder["task"] = asyncio.ensure_future(
                delete_vault(
                    VAULT_ID,
                    _request_for(VAULT_ID),
                    env.conn,
                    vector_store,
                    dict(_ADMIN_USER),
                    "test-csrf",
                )
            )
            try:
                await route_holder["task"]
                return "returned"
            except asyncio.CancelledError:
                return "cancelled"
            except sqlite3.OperationalError:
                return "cleanup-error-propagated"
            except Exception as exc:  # noqa: BLE001
                return f"error:{type(exc).__name__}"
        finally:
            asyncio.to_thread = real_to_thread

    outcome = asyncio.run(drive())
    return outcome, state


def _assert_restore_completed(env: _VaultDrainEnv, outcome: str, leg: str) -> None:
    assert outcome == "cancelled", (
        f"{leg}: the route task must end cancelled: the delivered "
        f"cancellation propagates after the restore completes (got {outcome!r})"
    )
    vault_rows = env.scalar(
        "SELECT COUNT(*) FROM vaults WHERE id = ?", (VAULT_ID,)
    )
    assert vault_rows == 1, (
        f"{leg}: precondition — the recovery arm ran (the failed transaction "
        "rolled back and the vault row survived)"
    )
    assert env.storage.exists(env.input_relpath), (
        f"{leg}: restore skipped — the tombstoned Draft Room bytes are "
        "stranded in .trash while the vault row (and its cascading draft "
        "rows) survived: the cancellation delivered inside the recovery arm "
        "escaped it and restore_purge never ran "
        f"(.trash entries: {env.trash_entries()})"
    )
    assert env.trash_entries() == [], (
        f"{leg}: restore skipped — tombstoned bytes remain in .trash "
        f"({env.trash_entries()}) while the vault row survived"
    )


def test_cancel_at_rollback_await_still_restores_purge(tmp_path: Path) -> None:
    """RED at base, GREEN after the fix.

    A sqlite3.Error from a vault-scoped cleanup statement mid-transaction
    (after the draft input tombstones exist) drives the route into its
    recovery arm; a cancellation delivered at the arm's guarded ROLLBACK
    await may end the handler task — but the restore MUST still complete:
    input bytes back at the live path, ``.trash`` empty, vault row alive.
    """
    env = _VaultDrainEnv(tmp_path)
    try:
        outcome, state = _drive_with_cancel_at(_is_rollback_lambda, env)
        assert state["fired"], (
            "non-vacuity: the cancellation must have been delivered at the "
            "arm's rollback await (the rollback-lambda hook never fired) — "
            "otherwise this run proved nothing"
        )
        assert env.conn.cleanup_failed, (
            "non-vacuity: the vault-scoped cleanup statement must have "
            "raised sqlite3.Error inside the transaction so the recovery arm ran"
        )
        _assert_restore_completed(env, outcome, "leg 1 (cancel at rollback await)")
    finally:
        env.close()


def test_cancel_at_restore_await_still_restores_purge(tmp_path: Path) -> None:
    """RED at base, GREEN after the fix.

    Same recovery arm, cancellation delivered one await later — at the
    ``restore_purge`` dispatch itself. The route task may end cancelled, but
    the restore must not be skipped: the bytes return to the live path and
    ``.trash`` empties even though the request unwinds cancelled.
    """
    env = _VaultDrainEnv(tmp_path)
    try:

        def _is_restore_purge(fn, args):  # noqa: ANN001
            return getattr(fn, "__name__", "") == "restore_purge"

        outcome, state = _drive_with_cancel_at(_is_restore_purge, env)
        assert state["fired"], (
            "non-vacuity: the cancellation must have been delivered at the "
            "arm's restore_purge await (the hook never fired) — otherwise "
            "this run proved nothing"
        )
        assert env.conn.cleanup_failed, (
            "non-vacuity: the vault-scoped cleanup statement must have "
            "raised sqlite3.Error inside the transaction so the recovery arm ran"
        )
        _assert_restore_completed(env, outcome, "leg 2 (cancel at restore await)")
    finally:
        env.close()
