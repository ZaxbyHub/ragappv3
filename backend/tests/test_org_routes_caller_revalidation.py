"""Issue #1043 residual 1: six org routes authorize a stale caller.

``add_org_member``, ``create_org_invite``, ``resend_org_invite``,
``revoke_org_invite``, ``set_prompt_override`` and ``clear_prompt_override``
(organizations.py) validate caller privilege ONLY outside their write
transaction: the ``_is_org_admin_or_owner`` to_thread check reads the
org_members row on autocommit, the mutation then runs and commits with no
re-read inside the transaction. A caller demoted to plain member between
that check and the write — the exact window PR #1042 already closed for
``update_org_member_role``/``remove_org_member``/``transfer_ownership`` with
an in-transaction re-check — still has their mutation execute and land.

Each leg here drives the REAL route function through that window. The
interleaving control is fix-agnostic (the proven harness from
test_transfer_ownership_caller_race.py): the to_thread dispatch of
``_is_org_admin_or_owner`` is wrapped so the REAL check runs first, then a
racing ``update_org_member_role`` (driven by the org owner) DEMOTES the
caller to plain member and commits on a second real pool connection, and
only then is the now-stale True result returned to the route. The racing
demotion therefore always lands between the outer check and the write:
at the base tree the stale mutation still executes; after the fix (an
in-transaction re-check mirroring ``_update_member_role``'s) the re-read
sees the demotion and the route refuses before any write.

Contract asserted at BOTH ends of the fix (RED at base, GREEN after the
fix): the demoted caller's mutation is REFUSED with 403 and the mutation
does not land.
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
from datetime import datetime, timedelta, timezone  # noqa: E402
from pathlib import Path  # noqa: E402

from fastapi import HTTPException  # noqa: E402

from app.api.routes import organizations as org_mod  # noqa: E402
from app.api.routes.organizations import (  # noqa: E402
    OrgInviteCreateRequest,
    OrgMemberRequest,
    OrgMemberUpdateRequest,
    PromptOverrideSetRequest,
    add_org_member,
    clear_prompt_override,
    create_org_invite,
    resend_org_invite,
    revoke_org_invite,
    set_prompt_override,
    update_org_member_role,
)
from app.models.database import SQLiteConnectionPool, _pool_cache, init_db  # noqa: E402

ORG_ID = 1

OWNER = {"id": 1, "username": "owner1", "role": "admin"}
# The stale caller: global member, org ADMIN at seed time — demoted to plain
# org member by the racing handler after their privilege check passed.
STALE_CALLER = {"id": 2, "username": "admin2", "role": "member"}

RESEND_TOKEN_HASH = "seed-token-hash-resend-leg-0000000000000000000000000"
REVOKE_TOKEN_HASH = "seed-token-hash-revoke-leg-0000000000000000000000000"


class _MultiConnPool:
    """Pool facade handing every caller its own REAL pool connection.

    The driven route and the racing demotion must run on separate
    connections, exactly as two concurrent requests would — a shared
    connection would collapse the race into one transaction.
    """

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


class _RaceEnv:
    """Per-test world: fresh db, real pool, patched org-route pool binding."""

    def __init__(self, tmp_path: Path) -> None:
        _pool_cache.clear()
        self.db_path = str(tmp_path / "org-stale-caller.db")
        init_db(self.db_path)
        self.pool = SQLiteConnectionPool(self.db_path, max_size=6)
        self.facade: object = _MultiConnPool(self.pool)
        self._saved_get_pool = org_mod.get_pool
        org_mod.get_pool = lambda _p: self.facade

    def seed_org(self, with_override: bool = False) -> None:
        """Users 1 (org owner), 2 (org admin — the stale caller), 4 (a plain
        global member, NOT an org member: the add-member target and the
        invite identifier); org 1 with users 1/2 as owner/admin; a global
        active prompt version 'v1' (and, for the clear leg, an org override
        row for org 1)."""
        seed = self.pool.get_connection()
        try:
            for uid, username, role in (
                (1, "owner1", "admin"),
                (2, "admin2", "member"),
                (4, "invitee4", "member"),
            ):
                seed.execute(
                    "INSERT INTO users (id, username, hashed_password, "
                    "full_name, role, is_active) VALUES (?, ?, 'abc123', ?, ?, 1)",
                    (uid, username, username.title(), role),
                )
            seed.execute(
                "INSERT INTO organizations (id, name, created_by) "
                "VALUES (1, 'StaleCallerOrg', 1)"
            )
            seed.execute(
                "INSERT INTO org_members (org_id, user_id, role) "
                "VALUES (1, 1, 'owner')"
            )
            seed.execute(
                "INSERT INTO org_members (org_id, user_id, role) "
                "VALUES (1, 2, 'admin')"
            )
            seed.execute(
                "INSERT INTO prompt_versions (version, content, is_active, "
                "created_by) VALUES ('v1', 'global active prompt body', 1, '1')"
            )
            if with_override:
                seed.execute(
                    "INSERT INTO prompt_org_overrides (org_id, version, set_by) "
                    "VALUES (1, 'v1', '1')"
                )
            seed.commit()
        finally:
            self.pool.release_connection(seed)

    def seed_pending_invite(self, token_hash: str) -> int:
        """One pending, redeemable invite for user 4's username; returns id."""
        seed = self.pool.get_connection()
        try:
            expires = (
                datetime.now(timezone.utc) + timedelta(days=7)
            ).isoformat()
            created = datetime.now(timezone.utc).isoformat()
            cursor = seed.execute(
                "INSERT INTO org_invites (org_id, email, token_hash, role, "
                "expires_at, created_at, created_by_user_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (ORG_ID, "invitee4", token_hash, "member", expires, created, 1),
            )
            seed.commit()
            return int(cursor.lastrowid)
        finally:
            self.pool.release_connection(seed)

    def member_role(self, user_id: int) -> str | None:
        check = sqlite3.connect(self.db_path)
        try:
            row = check.execute(
                "SELECT role FROM org_members WHERE org_id = ? AND user_id = ?",
                (ORG_ID, user_id),
            ).fetchone()
            return None if row is None else str(row[0])
        finally:
            check.close()

    def scalar(self, sql: str, params: tuple = ()) -> object:
        check = sqlite3.connect(self.db_path)
        try:
            row = check.execute(sql, params).fetchone()
            return None if row is None else row[0]
        finally:
            check.close()

    def close(self) -> None:
        org_mod.get_pool = self._saved_get_pool
        for conn in list(getattr(self.facade, "_out", [])):
            try:
                self.pool.release_connection(conn)
            except Exception:
                pass
        self.pool.close_all()
        _pool_cache.clear()


def _stale_privilege_interleave(racing):  # noqa: ANN001
    """Stateful, fix-agnostic interleaving builder: at the to_thread dispatch
    of ``_is_org_admin_or_owner`` run the REAL check first, then await the
    racing caller-demotion handler to completion (it commits on its own real
    pool connection), then return the real — now stale — True result."""

    state = {"fired": False}

    def build(real_to_thread):  # noqa: ANN001, ANN202
        async def scripted(fn, *args, **kwargs):  # noqa: ANN002, ANN003
            if (
                not state["fired"]
                and getattr(fn, "__name__", "") == "_is_org_admin_or_owner"
            ):
                state["fired"] = True
                result = await real_to_thread(fn, *args, **kwargs)
                await racing()
                return result
            return await real_to_thread(fn, *args, **kwargs)

        return scripted

    return build, state


def _racing_caller_demotion() -> object:
    """The racing handler: the org owner demotes the stale caller (user 2)
    from org admin to plain member after the caller's check has passed."""

    async def race() -> None:
        await update_org_member_role(
            ORG_ID,
            2,
            OrgMemberUpdateRequest(role="member"),
            user=OWNER,
            _csrf_token="t",
        )

    return race()


def _drive_interleaved(build, drive_factory) -> dict:  # noqa: ANN001
    """Run the driven handler with the wrapped asyncio.to_thread."""
    real_to_thread = asyncio.to_thread
    scripted = build(real_to_thread)
    outcome: dict = {}

    async def drive() -> None:
        asyncio.to_thread = scripted
        try:
            result = await drive_factory()
            outcome["ok"] = str(result)
        except HTTPException as exc:
            outcome["refused_status"] = exc.status_code
        finally:
            asyncio.to_thread = real_to_thread

    asyncio.run(drive())
    return outcome


def _assert_armed_then_refused(env: _RaceEnv, state: dict, outcome: dict, defect: str) -> None:
    """Shared non-vacuity + contract assertions: the racing demotion must
    have landed first (otherwise the leg proved nothing), then the route must
    refuse the stale caller with 403."""
    assert state["fired"], (
        "the stale-caller interleave never armed at the "
        "_is_org_admin_or_owner dispatch — otherwise this run proved nothing"
    )
    assert env.member_role(2) == "member", (
        "non-vacuity: the racing update_org_member_role must have demoted "
        "user 2 from org admin to plain member between the outer check and "
        "the write"
    )
    assert "refused_status" in outcome, (
        f"{defect}: the handler returned success ({outcome.get('ok')}) "
        "instead of refusing the demoted caller"
    )
    assert outcome["refused_status"] == 403, (
        f"{defect}: a refusal must be HTTP 403, not "
        f"{outcome['refused_status']}"
    )


def test_add_org_member_stale_caller_is_refused(tmp_path: Path) -> None:
    """RED at base, GREEN after the fix: a caller demoted between
    add_org_member's ``_is_org_admin_or_owner`` check and the membership
    INSERT must be refused 403 and the membership must not land."""
    env = _RaceEnv(tmp_path)
    try:
        env.seed_org()
        build, state = _stale_privilege_interleave(_racing_caller_demotion)
        outcome = _drive_interleaved(
            build,
            lambda: add_org_member(
                ORG_ID,
                OrgMemberRequest(user_id=4, role="member"),
                user=STALE_CALLER,
                _csrf_token="t",
            ),
        )
        _assert_armed_then_refused(
            env,
            state,
            outcome,
            "authorization window: add_org_member",
        )
        assert env.member_role(4) is None, (
            "the refused add_org_member must not have landed the membership "
            "row for the demoted caller"
        )
    finally:
        env.close()


def test_create_org_invite_stale_caller_is_refused(tmp_path: Path) -> None:
    """RED at base, GREEN after the fix: a caller demoted between
    create_org_invite's privilege check and the invite INSERT must be refused
    403 and no invite row may be minted."""
    env = _RaceEnv(tmp_path)
    try:
        env.seed_org()
        build, state = _stale_privilege_interleave(_racing_caller_demotion)
        outcome = _drive_interleaved(
            build,
            lambda: create_org_invite(
                ORG_ID,
                OrgInviteCreateRequest(email="invitee4", role="member"),
                user=STALE_CALLER,
                _csrf_token="t",
            ),
        )
        _assert_armed_then_refused(
            env,
            state,
            outcome,
            "authorization window: create_org_invite",
        )
        assert env.scalar(
            "SELECT COUNT(*) FROM org_invites WHERE org_id = ? AND email = ?",
            (ORG_ID, "invitee4"),
        ) == 0, (
            "the refused create_org_invite must not have minted an invite "
            "row for the demoted caller"
        )
    finally:
        env.close()


def test_resend_org_invite_stale_caller_is_refused(tmp_path: Path) -> None:
    """RED at base, GREEN after the fix: a caller demoted between
    resend_org_invite's privilege check and the token re-mint UPDATE must be
    refused 403 and the invite's token must stay unchanged."""
    env = _RaceEnv(tmp_path)
    try:
        env.seed_org()
        invite_id = env.seed_pending_invite(RESEND_TOKEN_HASH)
        build, state = _stale_privilege_interleave(_racing_caller_demotion)
        outcome = _drive_interleaved(
            build,
            lambda: resend_org_invite(
                ORG_ID, invite_id, user=STALE_CALLER, _csrf_token="t"
            ),
        )
        _assert_armed_then_refused(
            env,
            state,
            outcome,
            "authorization window: resend_org_invite",
        )
        assert env.scalar(
            "SELECT token_hash FROM org_invites WHERE id = ?", (invite_id,)
        ) == RESEND_TOKEN_HASH, (
            "the refused resend_org_invite must not have re-minted the "
            "invite token for the demoted caller"
        )
    finally:
        env.close()


def test_revoke_org_invite_stale_caller_is_refused(tmp_path: Path) -> None:
    """RED at base, GREEN after the fix: a caller demoted between
    revoke_org_invite's privilege check and the revoke UPDATE must be refused
    403 and the invite must stay pending."""
    env = _RaceEnv(tmp_path)
    try:
        env.seed_org()
        invite_id = env.seed_pending_invite(REVOKE_TOKEN_HASH)
        build, state = _stale_privilege_interleave(_racing_caller_demotion)
        outcome = _drive_interleaved(
            build,
            lambda: revoke_org_invite(
                ORG_ID, invite_id, user=STALE_CALLER, _csrf_token="t"
            ),
        )
        _assert_armed_then_refused(
            env,
            state,
            outcome,
            "authorization window: revoke_org_invite",
        )
        assert env.scalar(
            "SELECT revoked_at FROM org_invites WHERE id = ?", (invite_id,)
        ) is None, (
            "the refused revoke_org_invite must not have revoked the invite "
            "for the demoted caller"
        )
    finally:
        env.close()


def test_set_prompt_override_stale_caller_is_refused(tmp_path: Path) -> None:
    """RED at base, GREEN after the fix: a caller demoted between
    set_prompt_override's privilege check (the non-superadmin branch) and the
    override upsert must be refused 403 and no override row may land."""
    env = _RaceEnv(tmp_path)
    try:
        env.seed_org()
        build, state = _stale_privilege_interleave(_racing_caller_demotion)
        outcome = _drive_interleaved(
            build,
            lambda: set_prompt_override(
                ORG_ID,
                PromptOverrideSetRequest(version="v1"),
                user=STALE_CALLER,
                _csrf_token="t",
            ),
        )
        _assert_armed_then_refused(
            env,
            state,
            outcome,
            "authorization window: set_prompt_override",
        )
        assert env.scalar(
            "SELECT COUNT(*) FROM prompt_org_overrides WHERE org_id = ?",
            (ORG_ID,),
        ) == 0, (
            "the refused set_prompt_override must not have landed an "
            "override row for the demoted caller"
        )
    finally:
        env.close()


def test_clear_prompt_override_stale_caller_is_refused(tmp_path: Path) -> None:
    """RED at base, GREEN after the fix: a caller demoted between
    clear_prompt_override's privilege check (the non-superadmin branch) and
    the override DELETE must be refused 403 and the org's override must
    survive."""
    env = _RaceEnv(tmp_path)
    try:
        env.seed_org(with_override=True)
        build, state = _stale_privilege_interleave(_racing_caller_demotion)
        outcome = _drive_interleaved(
            build,
            lambda: clear_prompt_override(
                ORG_ID, user=STALE_CALLER, _csrf_token="t"
            ),
        )
        _assert_armed_then_refused(
            env,
            state,
            outcome,
            "authorization window: clear_prompt_override",
        )
        assert env.scalar(
            "SELECT version FROM prompt_org_overrides WHERE org_id = ?",
            (ORG_ID,),
        ) == "v1", (
            "the refused clear_prompt_override must not have deleted the "
            "org's override for the demoted caller"
        )
    finally:
        env.close()
