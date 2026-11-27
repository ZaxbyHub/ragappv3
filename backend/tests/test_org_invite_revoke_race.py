"""Issue #1219 (follow-up from PR #1216): a revoked org invite must never
mint membership.

``accept_org_invite`` reads the invite's ``revoked_at`` only in its
pre-transaction ``SELECT``; the membership ``INSERT`` and the
``accepted_at`` ``UPDATE`` then run inside a ``BEGIN IMMEDIATE``
transaction that never re-reads the invite. A ``revoke_org_invite``
committing between the accept's pre-transaction read and its
``BEGIN IMMEDIATE`` is therefore silently ignored: the revoked invitee
becomes a real ``org_members`` row (base outcome: HTTP 200 + membership).

Contract asserted at BOTH ends of the fix (RED at base, GREEN after the
fix), proven by a two-connection race spec that drives the REAL
``accept_org_invite`` and ``revoke_org_invite`` handlers:

  RACE leg — the revoke is committed to completion (on its own pool
  connection) in the window between the accept's pre-transaction read and
  its ``BEGIN IMMEDIATE`` — implemented fix-agnostically: the accept's
  to_thread dispatch of ITS OWN connection's ``BEGIN IMMEDIATE`` is
  intercepted, the racing revoke is awaited to completion FIRST, then the
  real ``BEGIN IMMEDIATE`` runs. Gating on the driven connection's
  identity keeps the racing revoke's own transaction statements out of
  the trigger (``transaction_context`` also issues a raw
  ``conn.execute("BEGIN IMMEDIATE")``).

  CONTROL no-race leg — no concurrent revoke: the accept mints exactly
  one membership and marks the invite accepted (guards against the fix
  refusing valid accepts).

  CONTROL revoke-first leg — the revoke is fully committed BEFORE the
  accept starts: the accept refuses 400 'Invalid or expired invite' and
  mints nothing (guards against the race leg passing vacuously).

Non-vacuity: the race leg asserts the revoke actually committed
(``revoked_at`` set, revoke handler returned success) BEFORE the accept's
transaction opened.
"""

import asyncio
import hashlib
import os

os.environ["JWT_SECRET_KEY"] = (
    "test-jwt-secret-key-for-testing-only-12345678901234567890"
)
os.environ["USERS_ENABLED"] = "false"
os.environ["REDIS_URL"] = ""
os.environ["ADMIN_SECRET_TOKEN"] = (
    "test-admin-secret-0123456789abcdef0123456789abcdef0123456789abcdef"
)

import secrets  # noqa: E402
import sqlite3  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402
from pathlib import Path  # noqa: E402

from fastapi import HTTPException  # noqa: E402

from app.api.routes.organizations import (  # noqa: E402
    OrgInviteAcceptRequest,
    accept_org_invite,
    revoke_org_invite,
)
from app.models.database import SQLiteConnectionPool, _pool_cache, init_db  # noqa: E402

ORG_ID = 1
OWNER_ID = 1
INVITEE_ID = 3

OWNER = {"id": OWNER_ID, "username": "owner1", "role": "admin"}
INVITEE = {"id": INVITEE_ID, "username": "invitee@x.com", "role": "member"}


def _norm_sql(sql) -> str:
    return " ".join(str(sql).split())


class _MultiConnPool:
    """Pool facade handing every caller its own REAL pool connection.

    The driven accept and the racing revoke must run on separate
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


class _RaceEnv:
    """Per-test world: fresh db, real pool, per-caller connections.

    The org routes take a request-scoped ``db=`` connection (#798 AC4), so
    direct handler calls check their connection out of the facade at the
    call site — one fresh connection per simulated request."""

    def __init__(self, tmp_path: Path) -> None:
        _pool_cache.clear()
        self.db_path = str(tmp_path / "org-invite-revoke-race.db")
        init_db(self.db_path)
        self.pool = SQLiteConnectionPool(self.db_path, max_size=6)
        self.facade: object = _MultiConnPool(self.pool)
        self.raw_token, self.invite_id = self._seed()

    def _seed(self) -> tuple[str, int]:
        """Users 1 (org owner), 3 (invitee); org 1; one fresh invite."""
        seed = self.pool.get_connection()
        try:
            for uid, username, role in (
                (OWNER_ID, "owner1", "admin"),
                (INVITEE_ID, "invitee@x.com", "member"),
            ):
                seed.execute(
                    "INSERT INTO users (id, username, hashed_password, full_name, "
                    "role, is_active) VALUES (?, ?, 'abc123', ?, ?, 1)",
                    (uid, username, username.title(), role),
                )
            seed.execute(
                "INSERT INTO organizations (id, name, created_by) "
                "VALUES (1, 'InviteRevokeRaceOrg', 1)"
            )
            seed.execute(
                "INSERT INTO org_members (org_id, user_id, role) "
                "VALUES (1, 1, 'owner')"
            )
            raw_token = f"inv_{secrets.token_urlsafe(32)}"
            token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
            now = datetime.now(timezone.utc)
            cursor = seed.execute(
                """INSERT INTO org_invites
                   (org_id, email, token_hash, role, expires_at, created_at,
                    created_by_user_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    ORG_ID,
                    "invitee@x.com",
                    token_hash,
                    "member",
                    (now + timedelta(days=7)).isoformat(),
                    now.isoformat(),
                    OWNER_ID,
                ),
            )
            invite_id = cursor.lastrowid
            seed.commit()
            return raw_token, invite_id
        finally:
            self.pool.release_connection(seed)

    def invite_state(self) -> tuple:
        # (revoked_at, accepted_at, accepted_by) — raw read, usable after
        # pool teardown.
        check = sqlite3.connect(self.db_path)
        try:
            return check.execute(
                "SELECT revoked_at, accepted_at, accepted_by_user_id "
                "FROM org_invites WHERE id = ?",
                (self.invite_id,),
            ).fetchone()
        finally:
            check.close()

    def member_row(self) -> tuple | None:
        check = sqlite3.connect(self.db_path)
        try:
            return check.execute(
                "SELECT user_id, role FROM org_members "
                "WHERE org_id = ? AND user_id = ?",
                (ORG_ID, INVITEE_ID),
            ).fetchone()
        finally:
            check.close()

    def close(self) -> None:
        for conn in list(getattr(self.facade, "_out", [])):
            try:
                self.pool.release_connection(conn)
            except Exception:
                pass
        self.pool.close_all()
        _pool_cache.clear()


def _drive(env: _RaceEnv, drive_factory) -> dict:  # noqa: ANN001
    """Run the driven handler without any to_thread patching. The driven
    call takes its request-scoped ``db=`` connection from the env facade
    (fresh per simulated request) and this CALLER releases it afterwards
    (the handler no longer releases db, #798 AC4)."""
    outcome: dict = {}

    async def drive() -> None:
        conn = await env.facade.get_connection_async()
        try:
            result = await drive_factory(conn)
            outcome["ok"] = str(result)
        except HTTPException as exc:
            outcome["refused_status"] = exc.status_code
        except Exception as exc:  # noqa: BLE001
            outcome["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            env.facade.release_connection(conn)

    asyncio.run(drive())
    return outcome


def _drive_revoke_before_begin(env: _RaceEnv, interleave) -> tuple[dict, dict]:  # noqa: ANN001
    """Run the driven accept with the racing revoke committed INSIDE the
    window between the accept's pre-transaction invite read and its
    ``BEGIN IMMEDIATE``.

    Fix-agnostic: the intercept fires at the to_thread dispatch of the
    DRIVEN connection's own ``BEGIN IMMEDIATE`` (matched on the bound
    connection's identity, so the racing revoke's own BEGIN — also a raw
    ``conn.execute("BEGIN IMMEDIATE")`` via transaction_context — never
    triggers it), awaits the racing revoke to completion FIRST, then runs
    the real ``BEGIN IMMEDIATE``."""

    state = {"fired": False}
    real_to_thread = asyncio.to_thread
    outcome: dict = {}

    async def scripted(fn, *args, **kwargs):  # noqa: ANN002, ANN003
        if (
            not state["fired"]
            and getattr(fn, "__name__", "") == "execute"
            and args
            and isinstance(args[0], str)
            and _norm_sql(args[0]).upper() == "BEGIN IMMEDIATE"
            and getattr(fn, "__self__", None) is _accept_conn_ref[0]
        ):
            state["fired"] = True
            await interleave()
        return await real_to_thread(fn, *args, **kwargs)

    _accept_conn_ref: list = []

    async def drive() -> None:
        conn = await env.facade.get_connection_async()
        _accept_conn_ref.append(conn)
        asyncio.to_thread = scripted
        try:
            result = await accept_org_invite(
                OrgInviteAcceptRequest(token=env.raw_token),
                user=INVITEE,
                _csrf_token="t",
                db=conn,
            )
            outcome["ok"] = str(result)
        except HTTPException as exc:
            outcome["refused_status"] = exc.status_code
        except Exception as exc:  # noqa: BLE001
            outcome["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            asyncio.to_thread = real_to_thread
            env.facade.release_connection(conn)

    asyncio.run(drive())
    return outcome, state


def test_revoke_committed_inside_accept_window_mints_no_membership(
    tmp_path: Path,
) -> None:
    """RED at base, GREEN after the fix (issue #1219 AC1).

    The invite is revoked while an acceptance of that invite is in flight
    — between the accept's pre-transaction read and its BEGIN IMMEDIATE —
    so the accept must mint NO org_members row for it."""
    env = _RaceEnv(tmp_path)
    try:
        assert env.invite_state()[0] is None, (
            "precondition: the invite must start un-revoked"
        )
        assert env.member_row() is None, "precondition: invitee is not a member"

        async def racing_revoke() -> dict:
            conn = await env.facade.get_connection_async()
            try:
                result = await revoke_org_invite(
                    ORG_ID,
                    env.invite_id,
                    user=OWNER,
                    _csrf_token="t",
                    db=conn,
                )
                return {"ok": str(result)}
            except HTTPException as exc:
                return {"refused_status": exc.status_code}
            except Exception as exc:  # noqa: BLE001
                return {"error": f"{type(exc).__name__}: {exc}"}
            finally:
                env.facade.release_connection(conn)

        revoke_outcome: dict = {}

        async def interleave() -> None:
            revoke_outcome.update(await racing_revoke())

        outcome, state = _drive_revoke_before_begin(env, interleave)

        assert state["fired"], (
            "the racing revoke must have committed after the accept's "
            "pre-transaction read and before its BEGIN IMMEDIATE — "
            "otherwise this run proved nothing"
        )
        revoked_at, accepted_at, _ = env.invite_state()
        assert revoked_at is not None, (
            "non-vacuity: the racing revoke must have set revoked_at "
            "before the accept's transaction opened"
        )
        assert "ok" in revoke_outcome, (
            "non-vacuity: the racing revoke must have succeeded (outcome: "
            f"{revoke_outcome})"
        )
        assert "refused_status" in outcome, (
            "revoked invite accepted: the accept returned success "
            f"({outcome.get('ok')}) instead of refusing"
        )
        assert outcome["refused_status"] == 400, (
            "the refusal must be the 400 'Invalid or expired invite' "
            "class, not " f"{outcome['refused_status']}"
        )
        assert env.member_row() is None, (
            "RACE REPRODUCED: a REVOKED invite produced a membership row"
        )
        assert accepted_at is None, (
            "the revoked invite must not be marked accepted either"
        )
    finally:
        env.close()


def test_control_no_race_accept_mints_membership(tmp_path: Path) -> None:
    """CONTROL leg (issue #1219 AC1): with no concurrent revoke the accept
    mints exactly one membership and marks the invite accepted — the fix
    must not refuse valid accepts."""
    env = _RaceEnv(tmp_path)
    try:
        outcome = _drive(
            env,
            lambda db: accept_org_invite(
                OrgInviteAcceptRequest(token=env.raw_token),
                user=INVITEE,
                _csrf_token="t",
                db=db,
            ),
        )

        assert "ok" in outcome, (
            "the no-race accept must succeed (outcome: " f"{outcome})"
        )
        member = env.member_row()
        assert member == (INVITEE_ID, "member"), (
            "the no-race accept must mint exactly the invitee membership "
            f"with the invited role (got {member})"
        )
        revoked_at, accepted_at, accepted_by = env.invite_state()
        assert revoked_at is None and accepted_at is not None, (
            "the no-race accept must mark the invite accepted and leave "
            "it un-revoked"
        )
        assert accepted_by == INVITEE_ID
    finally:
        env.close()


def test_control_revoke_committed_first_refuses_accept(tmp_path: Path) -> None:
    """CONTROL leg (issue #1219 AC1): with the revoke fully committed
    BEFORE the accept starts, the accept refuses 400 'Invalid or expired
    invite' and mints nothing — the race leg cannot pass vacuously."""
    env = _RaceEnv(tmp_path)
    try:
        revoke_outcome = _drive(
            env,
            lambda db: revoke_org_invite(
                ORG_ID, env.invite_id, user=OWNER, _csrf_token="t", db=db
            ),
        )
        assert "ok" in revoke_outcome, (
            "the revoke-first control must revoke successfully (outcome: "
            f"{revoke_outcome})"
        )

        outcome = _drive(
            env,
            lambda db: accept_org_invite(
                OrgInviteAcceptRequest(token=env.raw_token),
                user=INVITEE,
                _csrf_token="t",
                db=db,
            ),
        )

        assert outcome.get("refused_status") == 400, (
            "revoke committed first: the accept must refuse with the 400 "
            "'Invalid or expired invite' class (outcome: "
            f"{outcome.get('refused_status', outcome)})"
        )
        assert env.member_row() is None, "no membership may be minted"
        revoked_at, accepted_at, _ = env.invite_state()
        assert revoked_at is not None and accepted_at is None
    finally:
        env.close()
