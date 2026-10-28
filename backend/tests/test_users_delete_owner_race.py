"""Issue #1037 item 1 (collated with #1036): delete_user's org-owner
pre-check race must never leave an organization with zero owners.

``delete_user`` reads its org-owner refusal (``SELECT org_id FROM
org_members WHERE user_id = ? AND role = 'owner'``) OUTSIDE the write
transaction, and ``_delete_user_row`` never re-checks org ownership inside
it — while ``org_members.user_id`` cascades on DELETE. A
``transfer_ownership`` committing between the pre-check and the delete
transaction promotes the delete-target to owner; the delete then cascades
their membership row away and the organization is left with ZERO owners
(the probe-verified base outcome: HTTP 200).

This check drives the REAL ``delete_user`` route against a real temp DB
through a per-caller pool facade: the ``asyncio.to_thread`` dispatch of the
fast-path ``_vaults_orphaned_by_user_delete`` call (which runs AFTER the
owner pre-check and BEFORE the delete worker's own transaction — the worker
itself runs on a dedicated executor, not to_thread) is wrapped so the racing
``transfer_ownership`` commits to completion FIRST on its own real pool
connection, then the original call proceeds. Contract asserted at BOTH ends
of the fix (RED at base, GREEN after the fix): the delete REFUSES (400-class
— the in-transaction recheck shape) and the organization keeps exactly one
owner, the promoted target.
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
from starlette.requests import Request  # noqa: E402

from app.api.routes import organizations as orgs_mod  # noqa: E402
from app.api.routes import users as users_mod  # noqa: E402
from app.api.routes.organizations import (  # noqa: E402
    TransferOwnershipRequest,
    transfer_ownership,
)
from app.api.routes.users import delete_user  # noqa: E402
from app.config import settings  # noqa: E402
from app.models.database import SQLiteConnectionPool, _pool_cache, init_db  # noqa: E402

ORG_ID = 1
VICTIM_USER_ID = 2

OWNER = {"id": 1, "username": "owner1", "role": "admin"}
SUPERADMIN = {"id": 99, "username": "root", "role": "superadmin", "is_active": True}


class _MultiConnPool:
    """Pool facade handing every caller its own REAL pool connection.

    The driven delete and the racing transfer must run on separate
    connections, exactly as two concurrent requests would."""

    def __init__(self, real_pool: SQLiteConnectionPool) -> None:
        self._real_pool = real_pool
        self._out: list = []

    async def get_connection_async(self, *args, **kwargs):  # noqa: ANN002, ANN003
        conn = self._real_pool.get_connection()
        self._out.append(conn)
        return conn

    def release_connection(self, conn) -> None:
        try:
            if conn.in_transaction:
                conn.rollback()
        except sqlite3.Error:
            pass
        self._real_pool.release_connection(conn)


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


def test_delete_user_racing_ownership_transfer_is_refused(tmp_path: Path) -> None:
    """RED at base, GREEN after the fix.

    A transfer_ownership committing between delete_user's org-owner
    pre-check and the delete transaction promotes the delete-target to
    owner; the delete must REFUSE (400-class) instead of cascading the new
    owner's membership away and leaving the org with zero owners."""
    _pool_cache.clear()
    db_path = str(tmp_path / "delete-owner-race.db")
    init_db(db_path)
    real_pool = SQLiteConnectionPool(db_path, max_size=6)

    seed = real_pool.get_connection()
    try:
        for uid, username, role in (
            (1, "owner1", "admin"),
            (VICTIM_USER_ID, "member2", "member"),
            (99, "root", "superadmin"),
        ):
            seed.execute(
                "INSERT INTO users (id, username, hashed_password, full_name, "
                "role, is_active) VALUES (?, ?, 'abc123', ?, ?, 1)",
                (uid, username, username.title(), role),
            )
        seed.execute(
            "INSERT INTO organizations (id, name, created_by) "
            "VALUES (1, 'DeleteRaceOrg', 1)"
        )
        seed.execute(
            "INSERT INTO org_members (org_id, user_id, role) "
            "VALUES (1, 1, 'owner')"
        )
        seed.execute(
            "INSERT INTO org_members (org_id, user_id, role) "
            "VALUES (1, ?, 'member')",
            (VICTIM_USER_ID,),
        )
        seed.commit()
    finally:
        real_pool.release_connection(seed)

    def _member_role(user_id: int) -> str | None:
        # Raw connection read: usable both before and after the pool teardown
        # in the test's finally block.
        check = sqlite3.connect(db_path)
        try:
            row = check.execute(
                "SELECT role FROM org_members WHERE org_id = ? AND user_id = ?",
                (ORG_ID, user_id),
            ).fetchone()
            return None if row is None else str(row[0])
        finally:
            check.close()

    def _owner_ids() -> list[int]:
        check = sqlite3.connect(db_path)
        try:
            rows = check.execute(
                "SELECT user_id FROM org_members "
                "WHERE org_id = ? AND role = 'owner' ORDER BY user_id",
                (ORG_ID,),
            ).fetchall()
        finally:
            check.close()
        return [row[0] for row in rows]

    assert _owner_ids() == [1], (
        "precondition: user 1 must start as the sole owner, so the delete's "
        "org-owner pre-check passes for user 2"
    )

    facade = _MultiConnPool(real_pool)
    saved_orgs_get_pool = orgs_mod.get_pool
    saved_users_get_pool = users_mod.get_pool
    saved_data_dir = settings.data_dir
    orgs_mod.get_pool = lambda _p: facade
    users_mod.get_pool = lambda _p: facade
    settings.data_dir = tmp_path

    real_to_thread = asyncio.to_thread
    race = {"fired": False}

    async def _racing_to_thread(func, /, *args, **kwargs):  # noqa: ANN002, ANN003
        # At delete_user's fast-path to_thread(_vaults_orphaned_by_user_delete)
        # — after the owner pre-check read, before the delete worker's
        # transaction — FIRST run the racing transfer_ownership to completion
        # on its own pool connection (its commit promotes user 2 to owner),
        # then run the original call. Every other to_thread passes through.
        if getattr(func, "__name__", "") == "_vaults_orphaned_by_user_delete" and not race["fired"]:
            race["fired"] = True
            await transfer_ownership(
                ORG_ID,
                TransferOwnershipRequest(new_owner_user_id=VICTIM_USER_ID),
                user=OWNER,
                _csrf_token="t",
            )
        return await real_to_thread(func, *args, **kwargs)

    outcome: dict = {}
    try:
        async def drive() -> None:
            asyncio.to_thread = _racing_to_thread
            try:
                result = await delete_user(
                    VICTIM_USER_ID,
                    _request_for(VICTIM_USER_ID),
                    user=SUPERADMIN,
                    _csrf_token="t",
                )
                outcome["ok"] = str(result)
            except HTTPException as exc:
                outcome["refused_status"] = exc.status_code
            finally:
                asyncio.to_thread = real_to_thread

        asyncio.run(drive())
    finally:
        asyncio.to_thread = real_to_thread
        orgs_mod.get_pool = saved_orgs_get_pool
        users_mod.get_pool = saved_users_get_pool
        settings.data_dir = saved_data_dir
        for conn in list(facade._out):
            try:
                real_pool.release_connection(conn)
            except Exception:
                pass
        real_pool.close_all()
        _pool_cache.clear()

    assert race["fired"], (
        "the racing transfer_ownership must have committed at the fast-path "
        "orphan check — otherwise this run proved nothing"
    )
    assert _member_role(1) != "owner", (
        "non-vacuity: the racing transfer must have demoted user 1 off the "
        "owner role before the delete transaction ran"
    )
    assert "refused_status" in outcome, (
        "organization left with zero owners: a transfer_ownership committed "
        "between delete_user's owner pre-check and its delete transaction, "
        "promoting the delete-target to owner, but the delete still returned "
        f"success ({outcome.get('ok')}) and cascaded the new owner's "
        "membership away"
    )
    assert 400 <= outcome["refused_status"] < 500, (
        "the refusal must be a 400-class conflict, not "
        f"{outcome['refused_status']}"
    )
    assert _owner_ids() == [VICTIM_USER_ID], (
        "organization left with zero owners: the refused delete must keep "
        "the promoted target (user 2) as the org's sole owner"
    )
    survivor_rows = sqlite3.connect(db_path).execute(
        "SELECT COUNT(*) FROM users WHERE id = ?", (VICTIM_USER_ID,)
    ).fetchone()[0]
    assert survivor_rows == 1, (
        "the refused delete must leave the delete-target's user row alive"
    )
