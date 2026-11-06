"""Issue #1068 AC3: the vaults.py helper-committed audit tails keep
their security-audit row across a cancellation delivered exactly at the
commit boundary.

At base d00bc160 each of the mutating vaults.py tails still named by
the #1068 census commits inside a worker whose transaction is closed by a
helper (``transaction_context`` commits on exit) and only after the commit
await resumes does the handler create the mandatory
``safe_record_security_event`` coroutine for ``drain_audit``:

- ``update_vault`` organization adoption — the ``org_id`` UPDATE commits
  inside ``_apply_vault_update``'s ``transaction_context``, then the gated
  ``vault.org_id_updated`` tail (gated on ``adoption_applied``);
- ``update_vault`` visibility — the same commit, then the
  ``vault.visibility_updated`` tail;
- ``update_vault`` combined — ONE request adopting an organization AND
  changing visibility crosses the SAME single committing-helper boundary,
  then owes BOTH conditional tails (``vault.org_id_updated`` gated on
  ``adoption_applied`` AND ``vault.visibility_updated``) — the combined leg
  pins that a cancellation at that one boundary cannot lose either tail;
- ``update_vault_groups`` — the access replacement commits inside
  ``_replace_access``'s ``transaction_context``, then the
  ``vault.group_access_replaced`` tail.

A cancellation delivered exactly at the commit await — after the commit work
completed in the worker — unwinds the handler before the audit coroutine
exists, losing the tamper-evident row for an already-applied mutation. PR
#1067 already converted this file's delete_vault tail to the drained-unit
shape; these legs pin the same contract for the remaining family
members.

Each leg drives the REAL handler coroutine with a deterministic
commit-boundary schedule: the driven connection is a proxy whose FIRST
transaction end (``conn.commit()`` — ``transaction_context`` closes its
transaction exactly that way) runs the REAL commit and, once it has
completed, schedules the driving task's cancellation onto the event loop
from the worker thread — queued before the ``to_thread`` completion
callback can resume the route's commit-bearing await, so the CancelledError
lands exactly there.

Contract asserted at BOTH ends (RED at base, GREEN after the fix — the
helper-closed commit and the audit tail become one drained unit, so the
cancellation is absorbed until the audit row is written and only then
re-raised):

- non-vacuity: the commit hook fired AND the mutation is durable;
- the handler task ends cancelled (the delivered cancellation is re-raised
  after the drained unit completes, per the #1067 outcome contract);
- the mutation's security-audit row exists in ``security_audit_log``
  (``vault.org_id_updated`` / ``vault.visibility_updated`` /
  ``vault.group_access_replaced``).
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

from starlette.requests import Request  # noqa: E402

from app.api.routes import vaults as vaults_mod  # noqa: E402
from app.api.routes.vaults import (  # noqa: E402
    VaultGroupsUpdateRequest,
    VaultUpdateRequest,
    update_vault,
    update_vault_groups,
)
from app.models.database import (  # noqa: E402
    SQLiteConnectionPool,
    _pool_cache,
    init_db,
    run_migrations,
)

VAULT_ID = 9001

_SUPERADMIN = {
    "id": 1,
    "username": "root",
    "full_name": "Root",
    "role": "superadmin",
    "is_active": True,
    "must_change_password": False,
}


class _CancelAtCommitConn:
    """Delegating sqlite3 connection proxy that arms the deterministic
    cancellation at the FIRST transaction end on the driven connection.

    The real commit completes FIRST, then the driving route task's
    cancellation is scheduled onto the loop from the worker thread via
    ``loop.call_soon_threadsafe`` — deterministically ahead of the
    to_thread-future completion callback, so the CancelledError is delivered
    exactly at the commit-bearing await, after the commit work completed.
    Only fires once (the security-audit write's own commit must not re-arm).
    """

    def __init__(self, real: sqlite3.Connection, arm) -> None:  # noqa: ANN001
        self._real = real
        self._arm = arm

    def commit(self) -> None:
        self._real.commit()
        self._arm()

    def execute(self, sql, *args, **kwargs):  # noqa: ANN001, ANN202, ANN003
        return self._real.execute(sql, *args, **kwargs)

    def __getattr__(self, name):  # noqa: ANN001
        return getattr(self._real, name)


class _VaultsWorld:
    """Per-test world: temp DB, real pool, one seeded org-less vault."""

    def __init__(self, tmp_path: Path, db_name: str) -> None:
        _pool_cache.clear()
        self.db_path = str(tmp_path / db_name)
        init_db(self.db_path)
        # organizations / groups / vault_group_access arrive through the
        # migration journal, not the base schema.
        run_migrations(self.db_path)
        self.pool = SQLiteConnectionPool(self.db_path, max_size=2)
        self.real_conn = self.pool.get_connection()
        seed = self.pool.get_connection()
        try:
            seed.execute(
                "INSERT INTO users (id, username, hashed_password, role, is_active) "
                "VALUES (1, 'root', 'abc123', 'superadmin', 1)"
            )
            seed.execute(
                "INSERT INTO vaults (id, name, description, visibility) "
                "VALUES (?, 'BoundaryVault', 'before', 'private')",
                (VAULT_ID,)
            )
            seed.execute(
                "INSERT INTO organizations (id, name) VALUES (1, 'Org One')"
            )
            seed.execute(
                "INSERT INTO groups (id, org_id, name) VALUES (1, 1, 'Group One')"
            )
            seed.commit()
        finally:
            self.pool.release_connection(seed)

    def scalar(self, sql: str, params: tuple = ()) -> object:
        check = sqlite3.connect(self.db_path)
        try:
            row = check.execute(sql, params).fetchone()
            return None if row is None else row[0]
        finally:
            check.close()

    def close(self) -> None:
        try:
            if self.real_conn.in_transaction:
                self.real_conn.rollback()
        except sqlite3.Error:
            pass
        try:
            self.pool.release_connection(self.real_conn)
        except Exception:  # noqa: BLE001
            pass
        self.pool.close_all()
        _pool_cache.clear()


def _request_for(method: str, path: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": method,
            "headers": [],
            "path": path,
            "query_string": b"",
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
            "scheme": "http",
        }
    )


def _drive(world: _VaultsWorld, make_call) -> tuple[str, dict]:  # noqa: ANN001
    """Deterministic commit-boundary schedule: the first transaction end on
    the world's driven connection arms the driving task's cancellation."""

    async def drive() -> str:
        loop = asyncio.get_running_loop()
        state = {"fired": False}
        holder: dict = {"task": None}

        def _arm() -> None:
            if not state["fired"]:
                state["fired"] = True
                loop.call_soon_threadsafe(holder["task"].cancel)

        conn = _CancelAtCommitConn(world.real_conn, _arm)
        holder["task"] = asyncio.ensure_future(make_call(conn))
        try:
            await holder["task"]
            return "returned"
        except asyncio.CancelledError:
            return "cancelled"
        except Exception as exc:  # noqa: BLE001
            return f"error:{type(exc).__name__}"
        finally:
            state_holder["state"] = state

    state_holder: dict = {}
    outcome = asyncio.run(drive())
    return outcome, state_holder["state"]


def _audit_rows(world: _VaultsWorld, event_type: str) -> int:
    return int(
        world.scalar(
            "SELECT COUNT(*) FROM security_audit_log WHERE event_type = ?",
            (event_type,),
        )
    )


# named-mutation: drop the drained unit around _apply_vault_update at the
# update_vault adoption site (await the worker bare, leaving the
# vault.org_id_updated tail outside any unit) ->
# test_update_vault_org_adoption_cancel_at_commit_still_audits regresses.


def test_update_vault_org_adoption_cancel_at_commit_still_audits(
    tmp_path: Path,
) -> None:
    """RED at base, GREEN after the fix. The org adoption UPDATE commits
    inside ``transaction_context``; a cancellation landing at that commit
    await must not lose the gated ``vault.org_id_updated`` row."""
    world = _VaultsWorld(tmp_path, "vaults-org-adoption.db")
    try:
        outcome, state = _drive(
            world,
            lambda conn: update_vault(
                VAULT_ID,
                VaultUpdateRequest(org_id=1),
                _request_for("PUT", f"/api/vaults/{VAULT_ID}"),
                conn=conn,
                user=dict(_SUPERADMIN),
                _csrf_token="t",
            ),
        )
        assert state["fired"], "non-vacuity: the commit hook must have fired"
        assert outcome == "cancelled", f"route must unwind cancelled (got {outcome!r})"
        assert world.scalar("SELECT org_id FROM vaults WHERE id = ?", (VAULT_ID,)) == 1, (
            "precondition: the org-adoption commit completed "
            "(vaults.org_id(id=1) == 1)"
        )
        assert _audit_rows(world, "vault.org_id_updated") == 1, (
            "commit-boundary audit loss: the org adoption was applied (the "
            "commit completed) but its vault.org_id_updated security-audit "
            "row was never recorded"
        )
    finally:
        world.close()


def test_update_vault_visibility_cancel_at_commit_still_audits(
    tmp_path: Path,
) -> None:
    """RED at base, GREEN after the fix. The visibility UPDATE commits inside
    ``transaction_context``; a cancellation landing at that commit await must
    not lose the ``vault.visibility_updated`` row."""
    world = _VaultsWorld(tmp_path, "vaults-visibility.db")
    try:
        outcome, state = _drive(
            world,
            lambda conn: update_vault(
                VAULT_ID,
                VaultUpdateRequest(visibility="public"),
                _request_for("PUT", f"/api/vaults/{VAULT_ID}"),
                conn=conn,
                user=dict(_SUPERADMIN),
                _csrf_token="t",
            ),
        )
        assert state["fired"], "non-vacuity: the commit hook must have fired"
        assert outcome == "cancelled", f"route must unwind cancelled (got {outcome!r})"
        assert (
            world.scalar("SELECT visibility FROM vaults WHERE id = ?", (VAULT_ID,)) == "public"
        ), "precondition: the visibility commit completed"
        assert _audit_rows(world, "vault.visibility_updated") == 1, (
            "commit-boundary audit loss: the visibility change was applied "
            "(the commit completed) but its vault.visibility_updated "
            "security-audit row was never recorded"
        )
    finally:
        world.close()


def test_update_vault_combined_org_and_visibility_cancel_at_commit_still_audits(
    tmp_path: Path,
) -> None:
    """RED at base, GREEN after the fix. ONE update_vault request that adopts
    an organization AND changes visibility crosses the SAME single
    committing-helper boundary (``_apply_vault_update``'s
    ``transaction_context`` commit); after a cancellation at that boundary
    BOTH conditional audit tails are owed — ``vault.org_id_updated`` (gated
    on ``adoption_applied``) AND ``vault.visibility_updated`` — and both
    must be recorded."""
    world = _VaultsWorld(tmp_path, "vaults-combined.db")
    try:
        outcome, state = _drive(
            world,
            lambda conn: update_vault(
                VAULT_ID,
                VaultUpdateRequest(org_id=1, visibility="org"),
                _request_for("PUT", f"/api/vaults/{VAULT_ID}"),
                conn=conn,
                user=dict(_SUPERADMIN),
                _csrf_token="t",
            ),
        )
        assert state["fired"], "non-vacuity: the commit hook must have fired"
        assert outcome == "cancelled", f"route must unwind cancelled (got {outcome!r})"
        assert world.scalar("SELECT org_id FROM vaults WHERE id = ?", (VAULT_ID,)) == 1, (
            "precondition: the org-adoption half of the combined commit "
            "completed (vaults.org_id(id=1) == 1)"
        )
        assert (
            world.scalar("SELECT visibility FROM vaults WHERE id = ?", (VAULT_ID,))
            == "org"
        ), "precondition: the visibility half of the combined commit completed"
        assert _audit_rows(world, "vault.org_id_updated") == 1, (
            "commit-boundary audit loss: the combined update was applied "
            "(adoption + visibility both committed) but its gated "
            "vault.org_id_updated security-audit row was never recorded"
        )
        assert _audit_rows(world, "vault.visibility_updated") == 1, (
            "commit-boundary audit loss: the combined update was applied "
            "(adoption + visibility both committed) but its "
            "vault.visibility_updated security-audit row was never recorded"
        )
    finally:
        world.close()


def test_update_vault_groups_cancel_at_commit_still_audits(tmp_path: Path) -> None:
    """RED at base, GREEN after the fix. The group-access replacement commits
    inside ``transaction_context``; a cancellation landing at that commit
    await must not lose the ``vault.group_access_replaced`` row."""
    world = _VaultsWorld(tmp_path, "vaults-groups.db")
    try:
        outcome, state = _drive(
            world,
            lambda conn: update_vault_groups(
                VAULT_ID,
                VaultGroupsUpdateRequest(group_ids=[1]),
                _request_for("PUT", f"/api/vaults/{VAULT_ID}/groups"),
                user=dict(_SUPERADMIN),
                conn=conn,
                _csrf_token="t",
            ),
        )
        assert state["fired"], "non-vacuity: the commit hook must have fired"
        assert outcome == "cancelled", f"route must unwind cancelled (got {outcome!r})"
        assert (
            world.scalar(
                "SELECT COUNT(*) FROM vault_group_access WHERE vault_id = ? "
                "AND group_id = 1",
                (VAULT_ID,),
            )
            == 1
        ), "precondition: the group-access replacement commit completed"
        assert _audit_rows(world, "vault.group_access_replaced") == 1, (
            "commit-boundary audit loss: the vault group access replacement "
            "was applied (the commit completed) but its "
            "vault.group_access_replaced security-audit row was never recorded"
        )
    finally:
        world.close()
