"""Issue #1043 residual 2 family coverage: the recovery arms the frozen
test_vault_delete_restore_drain.py does not drive, plus the shared-helper
census.

The frozen file drives only the ``(sqlite3.Error, OSError, RuntimeError)``
arm (a cleanup-statement failure) with cancellations at the rollback and
restore awaits. This file closes the remaining arms and reversion vectors:

- the ``except HTTPException`` arm: a 404-shaped failure inside the purge
  transaction (vault row vanished to a concurrent delete) with a
  cancellation at the rollback await — restore must still complete and the
  route must end cancelled;
- the ``except BaseException`` arm post-commit gate: a cancellation
  arriving when the transaction is ALREADY closed (commit landed) with a
  non-null purge plan must NOT restore — the bytes belong to deleted drafts
  (issue #723 semantics; .trash startup reconcile owns them);
- a shared-helper census: all three arms in vaults.py route through
  ``_drained_vault_recovery`` (catches silent reversion of any arm), and
  the restore callable is passed explicitly (the plan-critic R2
  missing-dependency shape cannot regress unnoticed).
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

from app.api.routes import vaults as vaults_mod  # noqa: E402
from app.api.routes.vaults import delete_vault  # noqa: E402
from app.config import settings  # noqa: E402
from app.models.database import SQLiteConnectionPool, _pool_cache, init_db  # noqa: E402
from app.services.draft_input_storage import DraftInputStorage  # noqa: E402
from app.services.draft_store import DraftStore  # noqa: E402

VAULT_ID = 1
OWNER_ID = 1
DRAFT_INPUT_BYTES = b"vault recovery arms family bytes"

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


def _norm_sql(sql) -> str:
    return " ".join(str(sql).split())


class _VaultEnv:
    """Seeding shape from the frozen file: vault + draft + staged input."""

    def __init__(self, tmp_path: Path) -> None:
        _pool_cache.clear()
        self.db_path = str(tmp_path / "vault-arms.db")
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
                "INSERT INTO vaults (id, name) VALUES (?, 'ArmsVault')", (VAULT_ID,)
            )
            seed.commit()
            draft_store = DraftStore(seed)
            draft = draft_store.create_draft(
                vault_id=VAULT_ID,
                created_by=OWNER_ID,
                title="vault arms draft",
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
        self.conn = self.real_pool.get_connection()
        self._saved_data_dir = settings.data_dir
        settings.data_dir = tmp_path

    def trash_entries(self) -> list[str]:
        trash_dir = self.storage_root / ".trash"
        if not trash_dir.is_dir():
            return []
        return sorted(e.name for e in trash_dir.rglob("*") if e.is_file())

    def close(self) -> None:
        settings.data_dir = self._saved_data_dir
        try:
            if self.conn.in_transaction:
                self.conn.rollback()
        except sqlite3.Error:
            pass
        try:
            self.real_pool.release_connection(
                self.conn._inner if hasattr(self.conn, "_inner") else self.conn
            )
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


def _is_rollback_lambda(fn, args) -> bool:  # noqa: ANN001
    code = getattr(fn, "__code__", None)
    return (
        getattr(fn, "__name__", "") == "<lambda>"
        and not args
        and code is not None
        and "rollback" in code.co_names
    )


def _drive(env: _VaultEnv, matcher, cancel_before_commit: bool = False):  # noqa: ANN001
    real_to_thread = asyncio.to_thread
    state = {"fired": False}
    holder: dict = {"task": None}

    async def scripted(fn, *args, **kwargs):  # noqa: ANN002, ANN003
        if not state["fired"] and matcher(fn, args):
            state["fired"] = True
            holder["task"].cancel()
            await asyncio.sleep(0)
        return await real_to_thread(fn, *args, **kwargs)

    vector_store = MagicMock()
    vector_store.delete_by_vault = AsyncMock(return_value=0)

    async def drive() -> str:
        asyncio.to_thread = scripted
        try:
            holder["task"] = asyncio.ensure_future(
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
                await holder["task"]
                return "returned"
            except asyncio.CancelledError:
                return "cancelled"
            except Exception as exc:  # noqa: BLE001
                return f"error:{type(exc).__name__}"
        finally:
            asyncio.to_thread = real_to_thread

    return asyncio.run(drive()), state


def test_httpexception_arm_semantics_via_helper_unit(tmp_path: Path) -> None:
    """The ``except HTTPException`` arm is defensive at head (nothing inside
    the purge transaction raises HTTPException today — a concurrent delete
    answers via the DELETE's rowcount, which only gates the audit). Its
    drain semantics are the shared helper's, so this leg drives the REAL
    helper directly with the arm's exact inputs: a real connection holding
    an open transaction, a non-null purge plan, and a cancellation at the
    rollback await — restore must complete, CancelledError must propagate,
    and the routing census below pins that the arm actually calls this
    helper."""
    import tempfile

    from app.api.routes.vaults import _drained_vault_recovery

    db_path = str(tmp_path / "helper-unit.db")
    init_db(db_path)
    pool = SQLiteConnectionPool(db_path, max_size=1)
    conn = pool.get_connection()
    restored: list[object] = []
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO vaults (id, name) VALUES (1, 'RollbackOnly')"
        )
        real_to_thread = asyncio.to_thread
        state = {"fired": False}
        holder: dict = {"task": None}

        async def scripted(fn, *args, **kwargs):
            if not state["fired"] and _is_rollback_lambda(fn, args):
                state["fired"] = True
                holder["task"].cancel()
                await asyncio.sleep(0)
            return await real_to_thread(fn, *args, **kwargs)

        async def drive() -> str:
            asyncio.to_thread = scripted
            try:
                holder["task"] = asyncio.ensure_future(
                    _drained_vault_recovery(
                        conn, 1, lambda plan: restored.append(plan), object()
                    )
                )
                try:
                    await holder["task"]
                    return "returned"
                except asyncio.CancelledError:
                    return "cancelled"
            finally:
                asyncio.to_thread = real_to_thread

        outcome = asyncio.run(drive())
        assert state["fired"], "non-vacuity: the rollback hook fired"
        assert outcome == "cancelled", (
            f"the absorbed cancellation must win after the unit (got {outcome!r})"
        )
        assert len(restored) == 1, "the restore callable ran to completion"
        assert not conn.in_transaction, "the rollback completed inside the unit"
    finally:
        try:
            if conn.in_transaction:
                conn.rollback()
        except sqlite3.Error:
            pass
        pool.release_connection(conn)
        pool.close_all()
        _pool_cache.clear()


class _CommitFailConn:
    """Delegates everything to the real connection; commit() always raises
    sqlite3.OperationalError (the transaction stays open — the failure is
    the commit itself)."""

    def __init__(self, inner: sqlite3.Connection) -> None:
        self._inner = inner

    def commit(self) -> None:
        raise sqlite3.OperationalError("scripted commit failure (compound)")

    def __getattr__(self, name):  # noqa: ANN001
        return getattr(self._inner, name)


def test_commit_failure_with_absorbed_cancellation_ends_cancelled(
    tmp_path: Path,
) -> None:
    """Review PRR-004 arm-level reachability: a commit failure racing an
    absorbed caller cancellation must end the route CANCELLED — the tuple
    arm runs the drained recovery (rollback + restore) and then the
    delivered CancelledError wins; the pre-fix shape translated the
    compound into HTTP 500 and the delivered cancellation vanished."""
    env = _VaultEnv(tmp_path)
    env.conn = _CommitFailConn(env.conn)
    try:
        def _is_commit(fn, args):  # noqa: ANN001
            return getattr(fn, "__name__", "") == "commit"

        outcome, state = _drive(env, _is_commit)
        assert state["fired"], "non-vacuity: the commit hook fired"
        assert outcome == "cancelled", (
            "the delivered cancellation must win in the compound — the "
            f"pre-fix shape leaked a 500 instead (got {outcome!r})"
        )
        assert env.storage.exists(env.input_relpath), (
            "the drained recovery must still have restored the tombstoned "
            "Draft Room bytes before the cancellation surfaced"
        )
        assert env.trash_entries() == []
        check = sqlite3.connect(env.db_path)
        try:
            rows = check.execute(
                "SELECT COUNT(*) FROM vaults WHERE id = ?", (VAULT_ID,)
            ).fetchone()[0]
        finally:
            check.close()
        assert rows == 1, (
            "the failed commit must have been rolled back — the vault row "
            "survives"
        )
    finally:
        env.close()


def test_post_commit_cancellation_does_not_restore(tmp_path: Path) -> None:
    """A cancellation delivered at the COMMIT await (absorbed by
    drained_commit, re-raised after the mandatory audit tail) with a
    non-null purge plan must NOT restore: the transaction is already closed,
    the bytes belong to deleted drafts (#723 in_transaction gate — this leg
    drives the commit path whose post-commit state the BaseException arm's
    gate then declines to restore)."""
    env = _VaultEnv(tmp_path)
    try:
        def _is_commit(fn, args):  # noqa: ANN001
            return getattr(fn, "__name__", "") == "commit"

        outcome, state = _drive(env, _is_commit)
        assert state["fired"], "non-vacuity: the commit hook fired"
        # The commit is drained: the cancel is absorbed until the mandatory
        # audit tail, then re-raised — the route ends cancelled.
        assert outcome == "cancelled", f"route must end cancelled (got {outcome!r})"
        # The vault row is gone (durable delete) and the purge was COMMITTED:
        # tombstones stay in .trash for the startup reconcile — restore must
        # NOT have run post-commit.
        check = sqlite3.connect(env.db_path)
        try:
            rows = check.execute(
                "SELECT COUNT(*) FROM vaults WHERE id = ?", (VAULT_ID,)
            ).fetchone()[0]
        finally:
            check.close()
        assert rows == 0, "precondition: the delete committed"
        assert not env.storage.exists(env.input_relpath), (
            "post-commit the input bytes belong to deleted drafts — restoring "
            "them would resurrect purge-orphaned bytes"
        )
    finally:
        env.close()


def test_all_three_arms_route_through_shared_helper() -> None:
    """Census: every recovery arm in delete_vault routes through
    _drained_vault_recovery, and the helper itself takes the restore
    CALLABLE (no unbound draft_deletion reference inside it)."""
    source = Path(vaults_mod.__file__).read_text(encoding="utf-8")
    assert source.count("await _drained_vault_recovery(") == 3, (
        "all three delete_vault recovery arms must route through the shared "
        "drained unit (3 call sites)"
    )
    helper_source = source.split("async def _drained_vault_recovery")[1].split("@router.delete")[0]
    assert "draft_deletion." not in helper_source, (
        "the helper must take the restore callable as a parameter — an "
        "unbound draft_deletion reference is the plan-critic R2 NameError shape"
    )
