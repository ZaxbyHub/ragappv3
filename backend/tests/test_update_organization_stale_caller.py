"""Review PRR-003: update_organization is the SEVENTH org route with the
#1043 stale-caller shape — its privilege read ran on autocommit and the
UPDATE committed bare, so a caller demoted between the check and the write
still renamed/re-described the organization. The route used an inline role
query rather than the shared ``_is_org_admin_or_owner`` helper, which is why
the PR's `_is_org_admin_or_owner` census could not see it.

This file drives the REAL ``update_organization`` through that window with
the same fix-agnostic interleaving the frozen
``test_org_routes_caller_revalidation.py`` legs use — except the arming
matches the route's INLINE privilege read (the ``SELECT role FROM
org_members`` to_thread dispatch), because the fixed worker re-reads via the
shared helper INSIDE the transaction: at the base tree the demotion lands
between the outer read and the bare commit (the rename executes and 200s);
after the fix the in-transaction re-read sees the demotion and refuses 403.

Contract asserted at BOTH ends of the fix (RED at the pre-fix head
9db8f1a9, GREEN after the conversion): the demoted caller's rename is
REFUSED with 403 and the organization's name does not change.
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

from fastapi import HTTPException  # noqa: E402

from app.api.routes.organizations import (  # noqa: E402
    OrganizationUpdateRequest,
    update_organization,
)
from app.models.database import SQLiteConnectionPool, _pool_cache, init_db  # noqa: E402

ORG_ID = 1
STALE_CALLER = {"id": 2, "username": "admin2", "role": "member"}


class _MultiConnPool:
    """Every caller gets its own REAL pool connection (two concurrent
    writers must not collapse into one transaction)."""

    def __init__(self, real_pool: SQLiteConnectionPool) -> None:
        self._real_pool = real_pool
        self._out: list = []

    async def get_connection_async(self, *args, **kwargs):  # noqa: ANN002, ANN003
        conn = self._real_pool.get_connection()
        self._out.append(conn)
        return conn

    def release_connection(self, conn) -> None:  # noqa: ANN001
        try:
            if conn.in_transaction:
                conn.rollback()
        except sqlite3.Error:
            pass
        self._real_pool.release_connection(conn)


class _OrgRenameEnv:
    def __init__(self, tmp_path: Path) -> None:
        _pool_cache.clear()
        self.db_path = str(tmp_path / "update-org-stale-caller.db")
        init_db(self.db_path)
        self.pool = SQLiteConnectionPool(self.db_path, max_size=4)
        self.facade: object = _MultiConnPool(self.pool)
        from app.api.routes import organizations as org_mod

        self._org_mod = org_mod
        self._saved_get_pool = org_mod.get_pool
        org_mod.get_pool = lambda _p: self.facade
        seed = self.pool.get_connection()
        try:
            seed.execute(
                "INSERT INTO users (id, username, hashed_password, full_name, role, is_active) "
                "VALUES (1, 'owner1', 'abc123', 'Owner One', 'admin', 1)"
            )
            seed.execute(
                "INSERT INTO users (id, username, hashed_password, full_name, role, is_active) "
                "VALUES (2, 'admin2', 'abc123', 'Admin Two', 'member', 1)"
            )
            seed.execute(
                "INSERT INTO organizations (id, name, slug, description, created_by) "
                "VALUES (1, 'StaleOrg', 'staleorg', 'before', 1)"
            )
            seed.execute(
                "INSERT INTO org_members (org_id, user_id, role) VALUES (1, 1, 'owner')"
            )
            seed.execute(
                "INSERT INTO org_members (org_id, user_id, role) VALUES (1, 2, 'admin')"
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

    def demote_caller_committed(self) -> None:
        """The racing writer: demote user 2 on a SECOND real connection and
        commit — the concurrent transaction the stale read cannot see."""
        conn = self.pool.get_connection()
        try:
            conn.execute(
                "UPDATE org_members SET role = 'member' WHERE org_id = ? AND user_id = ?",
                (ORG_ID, STALE_CALLER["id"]),
            )
            conn.commit()
        finally:
            self.pool.release_connection(conn)

    def close(self) -> None:
        self._org_mod.get_pool = self._saved_get_pool
        for conn in list(getattr(self.facade, "_out", [])):
            try:
                self.pool.release_connection(conn)
            except Exception:
                pass
        self.pool.close_all()
        _pool_cache.clear()


def test_update_organization_stale_caller_is_refused(tmp_path: Path) -> None:
    """RED at the pre-fix head, GREEN after the conversion: a caller demoted
    between update_organization's outer privilege read and the write must be
    refused 403 and the organization's name must not change."""
    env = _OrgRenameEnv(tmp_path)
    real_to_thread = asyncio.to_thread
    state = {"fired": False}

    async def scripted(fn, *args, **kwargs):  # noqa: ANN002, ANN003
        # Arm on the route's INLINE privilege read (its SQL names
        # org_members' role for this user): run the REAL read, then land the
        # racing demotion to completion on a second real connection, then
        # return the now-stale row. The fixed worker's in-transaction
        # re-read runs strictly after this and sees the demotion.
        sql = args[0] if args else ""
        if (
            not state["fired"]
            and getattr(fn, "__name__", "") == "execute"
            and isinstance(sql, str)
            and "SELECT role FROM org_members" in sql
        ):
            state["fired"] = True
            result = await real_to_thread(fn, *args, **kwargs)
            env.demote_caller_committed()
            return result
        return await real_to_thread(fn, *args, **kwargs)

    async def drive():
        asyncio.to_thread = scripted
        try:
            return await update_organization(
                ORG_ID,
                OrganizationUpdateRequest(name="Renamed By Stale Caller"),
                user=STALE_CALLER,
                _csrf_token="t",
            )
        finally:
            asyncio.to_thread = real_to_thread

    async def run():
        try:
            return await drive(), None
        except HTTPException as exc:
            return None, exc

    try:
        result, exc = asyncio.run(run())
    finally:
        env.close()

    assert state["fired"], (
        "non-vacuity: the interleave must have armed at the route's inline "
        "privilege read — otherwise this run proved nothing"
    )
    assert exc is not None and exc.status_code == 403, (
        f"the demoted caller's rename must be refused 403 (got "
        f"result={result!r}, exc={exc!r})"
    )
    # The refusal must have rolled the update back atomically.
    check = sqlite3.connect(str(Path(env.db_path)))
    try:
        name = check.execute(
            "SELECT name FROM organizations WHERE id = ?", (ORG_ID,)
        ).fetchone()[0]
    finally:
        check.close()
    assert name == "StaleOrg", (
        "the refused stale-caller rename must not land: the organization "
        f"name is {name!r}"
    )


def test_update_organization_happy_path_still_renames(tmp_path: Path) -> None:
    """Non-stale success is unchanged by the conversion: an org admin whose
    privilege holds still renames (and the slug follows)."""
    env = _OrgRenameEnv(tmp_path)
    try:
        async def go():
            return await update_organization(
                ORG_ID,
                OrganizationUpdateRequest(name="Fresh Name"),
                user=STALE_CALLER,  # org admin, never demoted here
                _csrf_token="t",
            )

        result = asyncio.run(go())
        assert result["name"] == "Fresh Name"
        assert env.scalar("SELECT name FROM organizations WHERE id = ?", (ORG_ID,)) == "Fresh Name"
    finally:
        env.close()
