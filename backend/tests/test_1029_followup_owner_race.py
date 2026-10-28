"""Issue #1033 C7: a racing transfer_ownership must never orphan an org.

``remove_org_member`` checks the target's role ('owner' refusal) BEFORE its
write transaction, and the membership ``DELETE`` carries no role predicate.
So a ``transfer_ownership`` that commits between the pre-check and the DELETE
promotes the target to owner and the unconditional DELETE then removes the
NEW owner — the org is left with zero owners.

This check drives the REAL route functions against a real temp DB: the
``asyncio.to_thread`` dispatch of the inner ``_remove_member`` is wrapped so
the racing ``transfer_ownership`` commits to completion FIRST (on its own
real pool connection), then the original delete runs. The org must ALWAYS
retain an owner row afterwards — either the removal refused (403/409) or the
new owner's row survived. Single-purpose: exactly one test function.
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

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from app.api.routes import organizations as org_mod  # noqa: E402
from app.api.routes.organizations import (  # noqa: E402
    TransferOwnershipRequest,
    remove_org_member,
    transfer_ownership,
)
from app.models.database import SQLiteConnectionPool, _pool_cache, init_db  # noqa: E402

ORG_ID = 1
OWNER_ID = 1
MEMBER_ID = 2

OWNER = {"id": OWNER_ID, "username": "owner", "role": "member"}


class _PerCallerPool:
    """Pool facade handing every caller its own REAL pool connection.

    remove_org_member and the racing transfer_ownership must run on separate
    connections, exactly as two concurrent requests would — a shared
    connection would collapse the race into one transaction.
    """

    def __init__(self, real_pool: SQLiteConnectionPool) -> None:
        self._real_pool = real_pool

    async def get_connection_async(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return self._real_pool.get_connection()

    def release_connection(self, conn) -> None:
        self._real_pool.release_connection(conn)


def test_remove_owner_racing_transfer_ownership_never_orphans_org(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pool_cache.clear()
    db_path = str(tmp_path / "owner-race.db")
    init_db(db_path)
    real_pool = SQLiteConnectionPool(db_path, max_size=4)

    seed = real_pool.get_connection()
    try:
        seed.execute(
            "INSERT INTO users (id, username, hashed_password, full_name, role, is_active) "
            "VALUES (?, ?, ?, ?, ?, 1)",
            (OWNER_ID, "owner", "abc123", "Org Owner", "member"),
        )
        seed.execute(
            "INSERT INTO users (id, username, hashed_password, full_name, role, is_active) "
            "VALUES (?, ?, ?, ?, ?, 1)",
            (MEMBER_ID, "member", "abc123", "Org Member", "member"),
        )
        seed.execute(
            "INSERT INTO organizations (name, description, slug, created_by) "
            "VALUES (?, ?, ?, ?)",
            ("RaceOrg", "owner race", "raceorg", OWNER_ID),
        )
        seed.execute(
            "INSERT INTO org_members (org_id, user_id, role) VALUES (?, ?, 'owner')",
            (ORG_ID, OWNER_ID),
        )
        seed.execute(
            "INSERT INTO org_members (org_id, user_id, role) VALUES (?, ?, 'member')",
            (ORG_ID, MEMBER_ID),
        )
        seed.commit()
    finally:
        real_pool.release_connection(seed)

    def _role(user_id: int) -> str:
        check = real_pool.get_connection()
        try:
            row = check.execute(
                "SELECT role FROM org_members WHERE org_id = ? AND user_id = ?",
                (ORG_ID, user_id),
            ).fetchone()
            assert row is not None
            return str(row[0])
        finally:
            real_pool.release_connection(check)

    assert _role(MEMBER_ID) == "member", (
        "precondition: the pre-check must see user 2 as a plain member, so the "
        "'Cannot remove the organization owner' refusal cannot fire early"
    )

    facade = _PerCallerPool(real_pool)
    saved_get_pool = org_mod.get_pool
    org_mod.get_pool = lambda _path: facade

    real_to_thread = asyncio.to_thread
    real_transfer_ownership = transfer_ownership
    race = {"fired": False}

    async def _racing_to_thread(func, /, *args, **kwargs):  # noqa: ANN002, ANN003
        # When remove_org_member dispatches its inner _remove_member, FIRST
        # run the racing transfer_ownership to completion (its commit lands
        # between the route's pre-check and its write transaction), then run
        # the original call. Every other to_thread call passes through.
        if getattr(func, "__name__", "") == "_remove_member" and not race["fired"]:
            race["fired"] = True
            await real_transfer_ownership(
                ORG_ID,
                TransferOwnershipRequest(new_owner_user_id=MEMBER_ID),
                user=OWNER,
                _csrf_token="t",
            )
        return await real_to_thread(func, *args, **kwargs)

    try:
        monkeypatch.setattr(asyncio, "to_thread", _racing_to_thread)
        outcome: dict = {}
        try:
            asyncio.run(
                remove_org_member(
                    ORG_ID, MEMBER_ID, user=OWNER, _csrf_token="t"
                )
            )
            outcome["removed"] = True
        except HTTPException as exc:
            outcome["refused_status"] = exc.status_code

        assert race["fired"], (
            "the racing transfer_ownership must have committed before the "
            "inner delete ran — otherwise this run proved nothing"
        )
        # Evidence the racing transfer really committed: the OLD owner was
        # demoted off the owner role before the delete executed.
        assert _role(OWNER_ID) != "owner", (
            "the racing transfer must have demoted user 1 (still owner means "
            "the race never engaged, making the owner-row check vacuous)"
        )
        if "refused_status" in outcome:
            assert outcome["refused_status"] in (403, 409), (
                "a refusal must be a 403/409 conflict, not "
                f"{outcome['refused_status']}"
            )

        owners = real_pool.get_connection()
        try:
            owner_rows = owners.execute(
                "SELECT user_id FROM org_members WHERE org_id = ? AND role = 'owner'",
                (ORG_ID,),
            ).fetchall()
        finally:
            real_pool.release_connection(owners)
        # Pool connections use sqlite3.Row; project to plain ids so the
        # comparison holds at the fixed head (at base the list is empty
        # either way).
        owner_user_ids = [row[0] for row in owner_rows]

        assert owner_user_ids == [MEMBER_ID], (
            "org 1 was left with zero owners: the racing transfer made user 2 "
            "the owner after the pre-check, and the unconditional membership "
            f"DELETE removed them anyway (owner rows: {owner_user_ids})"
        )
    finally:
        monkeypatch.setattr(asyncio, "to_thread", real_to_thread)
        org_mod.get_pool = saved_get_pool
        real_pool.close_all()
        _pool_cache.clear()
