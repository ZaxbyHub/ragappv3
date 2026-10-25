"""User management routes (admin/superadmin only)."""

import asyncio
import logging
import sqlite3
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Query
from pydantic import BaseModel, Field, field_validator

from app.api.deps import (
    assign_user_to_default_vault,
    get_db,
    invalidate_active_user_cache,
    require_admin_role,
    require_role,
)
from app.api.routes.auth import async_hash_password
from app.config import settings
from app.models.database import get_pool
from app.security import csrf_protect
from app.services.auth_service import password_strength_check
from app.services.draft_deletion import DraftDeletionService
from app.services.draft_input_storage import DraftInputStorage, DraftInputStorageError
from app.services.draft_store import DraftStore
from app.services.security_audit import safe_record_security_event

router = APIRouter(prefix="/users", tags=["users"])
logger = logging.getLogger(__name__)

VALID_ROLES = ["superadmin", "admin", "member", "viewer"]


def assert_can_assign_role(
    actor_role: Optional[str], target_role: Optional[str], new_role: str
) -> None:
    """The single rule for who may assign which ``users.role`` value.

    Every surface that mutates ``users.role`` (``create_user``,
    ``update_user``, ``update_user_role``) must go through this helper so
    the routes cannot drift apart again (issue #560 C22):

    - Re-asserting a target's current role is a no-op, not an assignment
      (the Edit dialog always sends the role field, so a pure name change
      must not 403).
    - Any actual role change on an existing user requires a superadmin
      actor (matching the superadmin-only ``/role`` endpoint and the
      admin-guide role glossary).
    - At creation (``target_role`` is None) an admin may create member and
      viewer accounts; granting ``admin`` or ``superadmin`` requires a
      superadmin actor.
    """
    if new_role not in VALID_ROLES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid role. Must be one of: {', '.join(VALID_ROLES)}",
        )
    if actor_role == "superadmin":
        return
    if target_role is not None:
        if new_role == target_role:
            return
        raise HTTPException(
            status_code=403,
            detail="Only a superadmin can change user roles",
        )
    if new_role not in ("member", "viewer"):
        raise HTTPException(
            status_code=403,
            detail="Only a superadmin can assign the admin or superadmin role",
        )


def assert_can_modify_target(actor_role: Optional[str], target_role: Optional[str]) -> None:
    """The single rule for who may modify a superadmin target (issue #722).

    Every route that edits a target user's state (``update_user``,
    ``admin_reset_password``, ``update_user_active``,
    ``update_user_organizations``, ``update_user_groups``) must invoke this
    helper after reading the target row so the routes cannot drift apart
    (same pattern as ``assert_can_assign_role``, issue #560): a
    non-superadmin actor can never modify any field of a superadmin target —
    not even a no-op field on the account (admin-guide role-assignment rule).
    """
    if target_role == "superadmin" and actor_role != "superadmin":
        raise HTTPException(
            status_code=403,
            detail="Only superadmin can modify superadmin users",
        )


class UpdateRoleRequest(BaseModel):
    role: str = Field(...)


class UpdateActiveRequest(BaseModel):
    is_active: bool = Field(...)


def _validate_username_after_strip(v: Optional[str]) -> Optional[str]:
    """Reject usernames shorter than 3 characters AFTER stripping.

    Validation-only: the stored value is never rewritten (issue #725,
    T1-21-KR-12 — the raw min_length=3 floor left whitespace-padded short
    names accepted). Shared by admin create and update so both ends of the
    contract are identical.
    """
    if v is None:
        return v
    if len(v.strip()) < 3:
        raise ValueError("username must be at least 3 characters (after trimming whitespace)")
    return v


class UpdateUserRequest(BaseModel):
    username: Optional[str] = Field(default=None, min_length=3, max_length=255)
    full_name: Optional[str] = Field(default=None, max_length=255)
    role: Optional[str] = Field(default=None)

    _strip_check = field_validator("username")(_validate_username_after_strip)


class AdminResetPasswordRequest(BaseModel):
    new_password: str = Field(..., max_length=128)


class CreateUserRequest(BaseModel):
    # 3-char username floor matches the public registration path (RegisterRequest
    # in auth.py, enforced manually for a clean 400). Here it is a Pydantic
    # constraint because the admin path returns a different error contract.
    # Rationale documented in issue #395 LOW-4.
    username: str = Field(..., min_length=3, max_length=255)
    password: str = Field(..., min_length=8, max_length=128)
    full_name: str = Field(default="", max_length=255)
    role: str = Field(default="member")

    _strip_check = field_validator("username")(_validate_username_after_strip)


class OrgMembershipItem(BaseModel):
    org_id: int
    role: str = Field(default="member")


class UserOrgsUpdateRequest(BaseModel):
    """Replace user org memberships.

    Canonical form: ``memberships`` list with per-org roles.
    Legacy form: ``org_ids`` + single ``role`` applied to all.
    """

    memberships: Optional[List[OrgMembershipItem]] = None
    org_ids: Optional[List[int]] = None
    role: str = Field(default="member")

    def any_field_set(self) -> bool:
        """True when at least one replace-set field was explicitly provided.

        Omitted/null fields are rejected upstream (422) so an empty-body PUT
        can never clear all memberships (issue #725, T1-21-KR-11).
        """
        return self.memberships is not None or self.org_ids is not None

    def resolved_memberships(self) -> List[OrgMembershipItem]:
        """Return canonical per-org membership list."""
        if self.memberships is not None:
            return self.memberships
        if self.org_ids is not None:
            return [OrgMembershipItem(org_id=oid, role=self.role) for oid in self.org_ids]
        return []


class UserGroupsUpdateRequest(BaseModel):
    group_ids: List[int]


class UserGroupResponse(BaseModel):
    id: int
    name: str
    description: str
    org_id: int


@router.post("", include_in_schema=False)
@router.post("/")
async def create_user(
    request: Request,
    body: CreateUserRequest,
    user: dict = Depends(require_admin_role),
    _csrf_token: str = Depends(csrf_protect),
):
    """Create a new user (admin/superadmin only).

    Admins may create member and viewer accounts; granting admin or
    superadmin requires a superadmin actor.
    """
    # One rule for who may assign which role (issue #560 C22).
    assert_can_assign_role(user.get("role"), None, body.role)

    # Validate password strength
    try:
        password_strength_check(body.password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Hash the password
    hashed_password = await async_hash_password(body.password)

    pool = get_pool(str(settings.sqlite_path))
    conn = await pool.get_connection_async()

    try:
        # Check username uniqueness (case-insensitive)
        cursor = await asyncio.to_thread(conn.execute, "SELECT id FROM users WHERE username = ? COLLATE NOCASE", (body.username,))
        if await asyncio.to_thread(cursor.fetchone):
            raise HTTPException(
                status_code=400,
                detail=f"Username '{body.username}' already exists",
            )

        # Insert the new user and default assignments in one transaction.
        cursor = await asyncio.to_thread(conn.execute, """INSERT INTO users (username, hashed_password, full_name, role, is_active, must_change_password)
            VALUES (?, ?, ?, ?, 1, 1)""", (body.username, hashed_password, body.full_name, body.role))
        user_id = cursor.lastrowid

        # Fetch the created user before commit so response construction remains
        # part of the same rollback boundary as creation and default grants.
        cursor = await asyncio.to_thread(conn.execute, "SELECT id, username, full_name, role, is_active, created_at FROM users WHERE id = ?", (user_id,))
        row = await asyncio.to_thread(cursor.fetchone)
        if row is None:
            raise RuntimeError("Failed to retrieve created user")

        assign_user_to_default_vault(conn, user_id)

        await asyncio.to_thread(conn.commit)

        response = {
            "id": row[0],
            "username": row[1],
            "full_name": row[2] or "",
            "role": row[3],
            "is_active": bool(row[4]),
            "created_at": row[5],
        }
        await safe_record_security_event(
            conn,
            event_type="user.created",
            actor=user,
            target_user_id=row[0],
            target_username=row[1],
            request=request,
            metadata={"role": row[3]},
        )
        return response
    except HTTPException:
        await asyncio.to_thread(conn.rollback)
        raise
    except Exception as exc:
        await asyncio.to_thread(conn.rollback)
        logger.error("Failed to create user with default assignments", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="An internal error occurred. Please try again later.",
        ) from exc
    finally:
        pool.release_connection(conn)


@router.get("", include_in_schema=False)
@router.get("/")
async def list_users(
    skip: int = Query(0, ge=0, description="Rows to skip (non-negative)"),
    limit: int = Query(
        100, ge=1, le=1000, description="Rows to return (1-1000; matches the vault-list cap)"
    ),
    q: str | None = None,
    user: dict = Depends(require_role("admin")),
):
    """List all users (admin/superadmin only). Optional ?q= search filter."""
    pool = get_pool(str(settings.sqlite_path))
    conn = await pool.get_connection_async()

    try:
        if q:
            escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            search_pattern = f"%{escaped}%"
            count_cursor = await asyncio.to_thread(conn.execute, "SELECT COUNT(*) FROM users WHERE username LIKE ? ESCAPE '\\' OR full_name LIKE ? ESCAPE '\\'", (search_pattern, search_pattern))
            total = (await asyncio.to_thread(count_cursor.fetchone))[0]
            cursor = await asyncio.to_thread(conn.execute, """SELECT id, username, full_name, role, is_active, created_at
                   FROM users WHERE username LIKE ? ESCAPE '\\' OR full_name LIKE ? ESCAPE '\\'
                   ORDER BY id LIMIT ? OFFSET ?""", (search_pattern, search_pattern, limit, skip))
        else:
            count_cursor = await asyncio.to_thread(conn.execute, "SELECT COUNT(*) FROM users")
            total = (await asyncio.to_thread(count_cursor.fetchone))[0]
            cursor = await asyncio.to_thread(conn.execute, """SELECT id, username, full_name, role, is_active, created_at
                   FROM users ORDER BY id LIMIT ? OFFSET ?""", (limit, skip))
        rows = await asyncio.to_thread(cursor.fetchall)

        users = []
        for row in rows:
            users.append(
                {
                    "id": row[0],
                    "username": row[1],
                    "full_name": row[2] or "",
                    "role": row[3],
                    "is_active": bool(row[4]),
                    "created_at": row[5],
                }
            )

        return {"users": users, "total": total}
    finally:
        pool.release_connection(conn)


@router.get("/{user_id}")
async def get_user(
    user_id: int,
    user: dict = Depends(require_role("admin")),
):
    """Get user details (admin/superadmin only)."""
    pool = get_pool(str(settings.sqlite_path))
    conn = await pool.get_connection_async()

    try:
        cursor = await asyncio.to_thread(conn.execute, "SELECT id, username, full_name, role, is_active, created_at FROM users WHERE id = ?", (user_id,))
        row = await asyncio.to_thread(cursor.fetchone)

        if not row:
            raise HTTPException(status_code=404, detail="User not found")

        return {
            "id": row[0],
            "username": row[1],
            "full_name": row[2] or "",
            "role": row[3],
            "is_active": bool(row[4]),
            "created_at": row[5],
        }
    finally:
        pool.release_connection(conn)


@router.patch("/{user_id}")
async def update_user(
    user_id: int,
    request: Request,
    body: UpdateUserRequest,
    user: dict = Depends(require_admin_role),
    db: sqlite3.Connection = Depends(get_db),
    _csrf_token: str = Depends(csrf_protect),
):
    """Edit user fields (admin/superadmin only).

    Column whitelist: only username, full_name, role can be updated.
    Cannot change own role to prevent admin locking themselves out.
    """
    # Verify target user exists
    cursor = await asyncio.to_thread(db.execute, "SELECT id, username, full_name, role, is_active, created_at FROM users WHERE id = ?", (user_id,))
    target_row = await asyncio.to_thread(cursor.fetchone)

    if not target_row:
        raise HTTPException(status_code=404, detail="User not found")

    # Superadmin targets are modifiable by superadmins only, across every
    # field combination (issue #722 AC3) — runs before any field application,
    # including for bodies that omit the role field entirely.
    assert_can_modify_target(user.get("role"), target_row[3])

    # Build update fields based on whitelist
    update_fields = []
    update_values = []

    if body.username is not None:
        # Check username uniqueness (case-insensitive)
        dup_cursor = await asyncio.to_thread(db.execute, "SELECT id FROM users WHERE username = ? COLLATE NOCASE AND id != ?", (body.username, user_id))
        if await asyncio.to_thread(dup_cursor.fetchone):
            raise HTTPException(
                status_code=409, detail="Username already taken"
            )
        update_fields.append("username = ?")
        update_values.append(body.username)

    if body.full_name is not None:
        update_fields.append("full_name = ?")
        update_values.append(body.full_name)

    if body.role is not None:
        # Prevent changing own role
        if user_id == user.get("id"):
            raise HTTPException(status_code=400, detail="Cannot change your own role")

        # One rule for who may assign which role (issue #560 C22), shared
        # with create_user and update_user_role. Re-asserting the target's
        # current role is a no-op so the Edit dialog's always-sent role
        # field does not 403 unrelated saves. The superadmin-target half of
        # the rule is assert_can_modify_target above.
        assert_can_assign_role(user.get("role"), target_row[3], body.role)

        update_fields.append("role = ?")
        update_values.append(body.role)

    if not update_fields:
        # No fields to update, return current user
        return {
            "id": target_row[0],
            "username": target_row[1],
            "full_name": target_row[2] or "",
            "role": target_row[3],
            "is_active": bool(target_row[4]),
            "created_at": target_row[5],
        }

    # Execute update. Demoting an active superadmin carries the same atomic
    # at-least-one-active-superadmin guard update_user_role uses (issue #722
    # AC6): the count predicate rides the WHERE clause of this single
    # statement, so on guard rejection NONE of the co-submitted fields apply
    # (all-or-nothing, stated intentionally). The is_active-aware trigger is
    # deliberately stricter than update_user_role's (which ignores is_active)
    # — demoting an already-inactive superadmin cannot lock out actives.
    demotes_last_superadmin = (
        body.role is not None
        and target_row[3] == "superadmin"
        and body.role != "superadmin"
        and bool(target_row[4])
    )
    update_values.append(user_id)
    sql = f"UPDATE users SET {', '.join(update_fields)} WHERE id = ?"
    if demotes_last_superadmin:
        sql += " AND (SELECT COUNT(*) FROM users WHERE role = 'superadmin' AND is_active = 1) > 1"
    update_cursor = await asyncio.to_thread(db.execute, sql, tuple(update_values))
    if demotes_last_superadmin and await asyncio.to_thread(lambda: update_cursor.rowcount) == 0:
        raise HTTPException(
            status_code=400, detail="Cannot demote the last superadmin"
        )

    if body.username is not None and body.username != target_row[1]:
        # Org invites bind to the invitee's username at creation time and
        # acceptance compares that stored identifier against the CURRENT
        # username (organizations.py), so a rename must re-bind the user's own
        # pending invites or they become unacceptably orphaned (issue #721
        # AC7). Both statements run in the same transaction as the rename.
        now_iso = datetime.now(timezone.utc).isoformat()
        # Step 1: revoke PRE-EXISTING pending invites addressed to the NEW
        # username (username-form invites may target not-yet-provisioned
        # users, #300) — after the rename they would re-bind to the renaming
        # user. Runs BEFORE the rewrite and excludes the old-name rows so it
        # can never touch the user's own invites; revoked_at format matches
        # revoke_org_invite.
        await asyncio.to_thread(
            db.execute,
            """UPDATE org_invites SET revoked_at = ?
                WHERE email = ? COLLATE NOCASE
                  AND accepted_at IS NULL AND revoked_at IS NULL
                  AND email != ? COLLATE NOCASE""",
            (now_iso, body.username.lower(), target_row[1].lower()),
        )
        # Step 2: re-bind the user's own pending invites to the new username
        # so the renamed user can still accept them.
        await asyncio.to_thread(
            db.execute,
            """UPDATE org_invites SET email = ?
                WHERE email = ? COLLATE NOCASE
                  AND accepted_at IS NULL AND revoked_at IS NULL""",
            (body.username.lower(), target_row[1].lower()),
        )
    await asyncio.to_thread(db.commit)

    invalidate_active_user_cache(user_id)
    await safe_record_security_event(
        db,
        event_type="user.updated",
        actor=user,
        target_user_id=user_id,
        target_username=body.username or target_row[1],
        request=request,
        metadata={
            "fields": [field.split(" = ")[0] for field in update_fields],
            "role": body.role,
        },
    )

    # Fetch updated user
    cursor = await asyncio.to_thread(db.execute, "SELECT id, username, full_name, role, is_active, created_at FROM users WHERE id = ?", (user_id,))
    row = await asyncio.to_thread(cursor.fetchone)
    if row is None:
        # Post-commit re-read guard: a concurrent delete must answer 404, not
        # an untyped TypeError 500 (issue #726 census, T1-01-S2-08 class).
        raise HTTPException(status_code=404, detail=f"User with id {user_id} not found")

    return {
        "id": row[0],
        "username": row[1],
        "full_name": row[2] or "",
        "role": row[3],
        "is_active": bool(row[4]),
        "created_at": row[5],
    }


@router.patch("/{user_id}/password")
async def admin_reset_password(
    user_id: int,
    request: Request,
    body: AdminResetPasswordRequest,
    user: dict = Depends(require_admin_role),
    db: sqlite3.Connection = Depends(get_db),
    _csrf_token: str = Depends(csrf_protect),
):
    """Admin reset password (admin/superadmin only).

    Forces must_change_password=1 for the target user.
    """
    # Verify target user exists
    cursor = await asyncio.to_thread(db.execute, "SELECT id, role FROM users WHERE id = ?", (user_id,))
    target_row = await asyncio.to_thread(cursor.fetchone)
    if not target_row:
        raise HTTPException(status_code=404, detail="User not found")

    # Superadmin targets are modifiable by superadmins only (issue #722 AC4).
    assert_can_modify_target(user.get("role"), target_row[1])

    # Validate password strength
    try:
        password_strength_check(body.new_password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Hash the new password
    hashed_password = await async_hash_password(body.new_password)

    sessions_revoked = False

    def _admin_reset_password_db():
        nonlocal sessions_revoked
        try:
            db.execute("BEGIN EXCLUSIVE")
        except sqlite3.OperationalError:
            pass

        try:
            new_epoch = datetime.now(timezone.utc).timestamp()
            db.execute(
                "UPDATE users SET hashed_password = ?, must_change_password = 1, failed_attempts = 0, locked_until = NULL, password_changed_at = ? WHERE id = ?",
                (hashed_password, new_epoch, user_id),
            )
            try:
                db.execute("DELETE FROM user_sessions WHERE user_id = ?", (user_id,))
                sessions_revoked = True
            except sqlite3.OperationalError as e:
                err_str = str(e)
                if "no such table" in err_str:
                    # user_sessions table may not exist yet (e.g., test fixtures, pre-migration)
                    logger.warning("Could not delete user_sessions for user %d (table may not exist)", user_id)
                else:
                    raise
            db.execute("COMMIT")
        except Exception:
            try:
                db.execute("ROLLBACK")
            except Exception:
                pass
            raise

    await asyncio.to_thread(_admin_reset_password_db)

    invalidate_active_user_cache(user_id)
    await safe_record_security_event(
        db,
        event_type="user.password_reset",
        actor=user,
        target_user_id=user_id,
        request=request,
        metadata={"must_change_password": True, "sessions_revoked": sessions_revoked},
    )

    return {
        "message": "Password reset successfully",
        "must_change_password": True,
    }


@router.patch("/{user_id}/role")
async def update_user_role(
    user_id: int,
    request: Request,
    body: UpdateRoleRequest,
    user: dict = Depends(require_role("superadmin")),
    _csrf_token: str = Depends(csrf_protect),
):
    """Update user role (superadmin only). Cannot demote last superadmin."""
    # One rule for who may assign which role (issue #560 C22): shared with
    # create_user and update_user. The route gate already requires a
    # superadmin actor, so the helper's value validation (400) is what does
    # the work here.
    assert_can_assign_role(user.get("role"), None, body.role)

    pool = get_pool(str(settings.sqlite_path))
    conn = await pool.get_connection_async()

    try:
        cursor = await asyncio.to_thread(conn.execute, "SELECT role FROM users WHERE id = ?", (user_id,))
        target_row = await asyncio.to_thread(cursor.fetchone)

        if not target_row:
            raise HTTPException(status_code=404, detail="User not found")

        current_role = target_row[0]

        if current_role == "superadmin" and body.role != "superadmin":
            # Atomic guard: only demote if there are other active superadmins
            await asyncio.to_thread(cursor.execute, """UPDATE users SET role = ?
                   WHERE id = ? AND (SELECT COUNT(*) FROM users WHERE role = 'superadmin' AND is_active = 1) > 1""", (body.role, user_id))
            if await asyncio.to_thread(lambda: cursor.rowcount) == 0:
                raise HTTPException(
                    status_code=400, detail="Cannot demote the last superadmin"
                )
        else:
            await asyncio.to_thread(cursor.execute, "UPDATE users SET role = ? WHERE id = ?", (body.role, user_id))
        await asyncio.to_thread(conn.commit)

        invalidate_active_user_cache(user_id)
        await safe_record_security_event(
            conn,
            event_type="user.role_updated",
            actor=user,
            target_user_id=user_id,
            request=request,
            metadata={"old_role": current_role, "new_role": body.role},
        )

        return {
            "message": f"User role updated to {body.role}",
            "user_id": user_id,
            "role": body.role,
        }
    finally:
        pool.release_connection(conn)


@router.patch("/{user_id}/active")
async def update_user_active(
    user_id: int,
    request: Request,
    body: UpdateActiveRequest,
    user: dict = Depends(require_role("admin")),
    _csrf_token: str = Depends(csrf_protect),
):
    """Activate/deactivate user (admin/superadmin only). Cannot deactivate last superadmin.

    Note: admins can deactivate other admins and members. The last-superadmin guard
    prevents operational lockout. Self-deactivation is prevented by an explicit guard
    before the database operation.
    """
    pool = get_pool(str(settings.sqlite_path))
    conn = await pool.get_connection_async()

    try:
        # Cannot deactivate your own account
        if user_id == user.get("id") and not body.is_active:
            raise HTTPException(
                status_code=400, detail="Cannot deactivate your own account"
            )

        cursor = await asyncio.to_thread(conn.execute, "SELECT role, is_active FROM users WHERE id = ?", (user_id,))
        target_row = await asyncio.to_thread(cursor.fetchone)

        if not target_row:
            raise HTTPException(status_code=404, detail="User not found")

        target_role = target_row[0]
        currently_active = bool(target_row[1])

        # Superadmin targets are modifiable by superadmins only, before any
        # guard below can answer (issue #722 AC5: with a second active
        # superadmin the last-superadmin guard cannot fire, so without this
        # check a plain admin could deactivate a superadmin).
        assert_can_modify_target(user.get("role"), target_role)

        if target_role == "superadmin" and not body.is_active and currently_active:
            # Atomic guard: only deactivate if there are other active superadmins
            await asyncio.to_thread(cursor.execute, """UPDATE users SET is_active = ?
                   WHERE id = ? AND (SELECT COUNT(*) FROM users WHERE role = 'superadmin' AND is_active = 1) > 1""", (0, user_id))
            if await asyncio.to_thread(lambda: cursor.rowcount) == 0:
                raise HTTPException(
                    status_code=400, detail="Cannot deactivate the last superadmin"
                )
        else:
            await asyncio.to_thread(cursor.execute, "UPDATE users SET is_active = ? WHERE id = ?", (1 if body.is_active else 0, user_id))

        if not body.is_active:
            # Deactivation revokes the user's sessions in the same transaction
            # as the is_active flip (issue #722 AC2), so a refresh cookie
            # issued before deactivation can never mint tokens again after
            # reactivation. Missing-table tolerance mirrors
            # admin_reset_password (pre-migration DBs / test fixtures).
            try:
                await asyncio.to_thread(
                    conn.execute,
                    "DELETE FROM user_sessions WHERE user_id = ?",
                    (user_id,),
                )
            except sqlite3.OperationalError as e:
                if "no such table" in str(e):
                    logger.warning(
                        "Could not delete user_sessions for user %d (table may not exist)",
                        user_id,
                    )
                else:
                    raise
        await asyncio.to_thread(conn.commit)

        invalidate_active_user_cache(user_id)

        status_str = "activated" if body.is_active else "deactivated"
        await safe_record_security_event(
            conn,
            event_type="user.active_updated",
            actor=user,
            target_user_id=user_id,
            request=request,
            metadata={"is_active": body.is_active, "old_is_active": currently_active},
        )
        return {
            "message": f"User {status_str}",
            "user_id": user_id,
            "is_active": body.is_active,
        }
    finally:
        pool.release_connection(conn)


@router.delete("/{user_id}")
async def delete_user(
    user_id: int,
    request: Request,
    user: dict = Depends(require_role("superadmin")),
    _csrf_token: str = Depends(csrf_protect),
):
    """Delete user (superadmin only). Cannot delete last superadmin or self."""
    pool = get_pool(str(settings.sqlite_path))
    conn = await pool.get_connection_async()

    try:
        cursor = await asyncio.to_thread(conn.execute, "SELECT role, username FROM users WHERE id = ?", (user_id,))
        target_row = await asyncio.to_thread(cursor.fetchone)

        if not target_row:
            raise HTTPException(status_code=404, detail="User not found")

        target_role = target_row[0]
        target_username = target_row[1]

        if user_id == user.get("id"):
            raise HTTPException(
                status_code=400, detail="Cannot delete your own account"
            )

        # Prevent orphaning organizations by deleting their sole owner
        cursor = await asyncio.to_thread(conn.execute, "SELECT org_id FROM org_members WHERE user_id = ? AND role = 'owner'", (user_id,))
        owned_orgs = [row[0] for row in await asyncio.to_thread(cursor.fetchall)]
        if owned_orgs:
            raise HTTPException(
                status_code=400,
                detail=f"User owns organization(s) {owned_orgs}. Transfer ownership before deleting.",
            )

        # Prepare the Draft Room purge before the DELETE below: drafts.created_by
        # cascades on user delete (ON DELETE CASCADE), losing the rows that
        # name the files to remove. `prepare_purge_for_user` only tombstones
        # input files on disk — it touches no draft rows and opens no
        # transaction of its own, so it cannot conflict with the delete/commit
        # below. Ordering matches vaults.py::delete_vault for consistency:
        # tombstones are only permanently discarded (commit_purge) after the
        # delete below COMMITS, and are restored (restore_purge) if it fails
        # instead — see the try/except around the delete for why that guard
        # is needed even though this handler previously had no rollback path
        # at all. Not gated on draft_room_enabled (SPEC 9.2) — cleanup must
        # run even if disabled.
        draft_deletion = DraftDeletionService(
            DraftInputStorage(settings.data_dir / "draft-room")
        )
        draft_purge_plan = None
        try:
            draft_store = DraftStore(conn)
            draft_purge_plan = await asyncio.to_thread(
                draft_deletion.prepare_purge_for_user, draft_store, user_id
            )
            logger.info(
                "draft_room_user_purge_prepared user_id=%s draft_count=%s",
                user_id,
                len(draft_purge_plan.draft_owner_pairs),
            )
        except (sqlite3.Error, OSError, RuntimeError, DraftInputStorageError) as e:
            # Never block the user delete on a purge failure.
            # DraftInputStorageError is listed explicitly: it derives from
            # Exception, not OSError, so a tombstone/resolve failure would
            # otherwise escape and 500 the delete, contradicting this promise.
            logger.warning(
                "draft_room_user_purge_prepare_failed user_id=%s reason=%s",
                user_id,
                type(e).__name__,
            )

        try:
            # Detach historical org authorship first: organizations.created_by
            # has no ON DELETE action, so a user who created an org they no
            # longer own would otherwise trip the FK and 500 the delete
            # (issue #722 AC8). Same transaction as the DELETE below.
            await asyncio.to_thread(
                conn.execute,
                "UPDATE organizations SET created_by = NULL WHERE created_by = ?",
                (user_id,),
            )
            if target_role == "superadmin":
                # Atomic guard: only delete if there are other active superadmins
                await asyncio.to_thread(cursor.execute, """DELETE FROM users
                       WHERE id = ? AND (SELECT COUNT(*) FROM users WHERE role = 'superadmin' AND is_active = 1) > 1""", (user_id,))
                if await asyncio.to_thread(lambda: cursor.rowcount) == 0:
                    raise HTTPException(
                        status_code=400, detail="Cannot delete the last superadmin"
                    )
            else:
                await asyncio.to_thread(cursor.execute, "DELETE FROM users WHERE id = ?", (user_id,))
            await asyncio.to_thread(conn.commit)
        except HTTPException:
            await asyncio.to_thread(conn.rollback)
            if draft_purge_plan is not None:
                try:
                    await asyncio.to_thread(draft_deletion.restore_purge, draft_purge_plan)
                except Exception as restore_exc:
                    logger.error(
                        "draft_room_user_purge_restore_failed user_id=%s reason=%s",
                        user_id,
                        type(restore_exc).__name__,
                    )
            raise
        except (sqlite3.Error, OSError, RuntimeError) as e:
            await asyncio.to_thread(conn.rollback)
            if draft_purge_plan is not None:
                try:
                    await asyncio.to_thread(draft_deletion.restore_purge, draft_purge_plan)
                except Exception as restore_exc:
                    logger.error(
                        "draft_room_user_purge_restore_failed user_id=%s reason=%s",
                        user_id,
                        type(restore_exc).__name__,
                    )
            logger.exception("Error deleting user %d", user_id)
            raise HTTPException(
                status_code=500, detail="Failed to delete user"
            ) from e

        if draft_purge_plan is not None:
            try:
                await asyncio.to_thread(draft_deletion.commit_purge, draft_purge_plan)
            except Exception as e:
                # Bytes are already tombstoned in `.trash`; failing to finish
                # discarding them must not turn a successful user delete into
                # an error response.
                logger.warning(
                    "draft_room_user_purge_commit_failed user_id=%s reason=%s",
                    user_id,
                    type(e).__name__,
                )

        invalidate_active_user_cache(user_id)

        # Security-relevant event, same helper/tamper-evident contract as
        # every other users.py mutation (issue #722 AC1). security_audit_log
        # carries no FK on user ids, so recording after the delete commits
        # cannot violate constraints; metadata snapshots the pre-delete row.
        await safe_record_security_event(
            conn,
            event_type="user.deleted",
            actor=user,
            target_user_id=user_id,
            target_username=target_username,
            request=request,
            metadata={"role": target_role, "username": target_username},
        )

        return {"message": "User deleted", "user_id": user_id}
    finally:
        pool.release_connection(conn)


@router.get("/{user_id}/organizations")
async def get_user_organizations(
    user_id: int,
    user: dict = Depends(require_role("admin")),
):
    """Get all organizations a user belongs to (admin/superadmin only)."""
    pool = get_pool(str(settings.sqlite_path))
    conn = await pool.get_connection_async()
    try:
        cursor = await asyncio.to_thread(conn.execute, "SELECT id FROM users WHERE id = ?", (user_id,))
        if not await asyncio.to_thread(cursor.fetchone):
            raise HTTPException(status_code=404, detail="User not found")

        # For non-superadmins, verify caller shares an org with target user
        if user.get("role") != "superadmin":
            # Get target user's orgs
            await asyncio.to_thread(cursor.execute, "SELECT org_id FROM org_members WHERE user_id = ?", (user_id,))
            target_orgs = {row[0] for row in await asyncio.to_thread(cursor.fetchall)}
            # Get caller's orgs
            caller_id = user.get("id")
            await asyncio.to_thread(cursor.execute, "SELECT org_id FROM org_members WHERE user_id = ?", (caller_id,))
            caller_orgs = {row[0] for row in await asyncio.to_thread(cursor.fetchall)}
            # Check overlap
            if not target_orgs & caller_orgs:  # intersection
                raise HTTPException(
                    status_code=403,
                    detail="Cannot view organizations of users outside your organization",
                )

        cursor = await asyncio.to_thread(conn.execute, """SELECT o.id, o.name, o.description, om.role, om.joined_at
               FROM org_members om JOIN organizations o ON om.org_id = o.id
               WHERE om.user_id = ? ORDER BY o.name""", (user_id,))
        orgs = []
        for row in await asyncio.to_thread(cursor.fetchall):
            orgs.append({
                "id": row[0],
                "name": row[1],
                "description": row[2] or "",
                "role": row[3],
                "joined_at": row[4],
            })
        return {"organizations": orgs}
    finally:
        pool.release_connection(conn)


@router.put("/{user_id}/organizations")
async def update_user_organizations(
    user_id: int,
    request: Request,
    body: UserOrgsUpdateRequest,
    user: dict = Depends(require_role("admin")),
    _csrf_token: str = Depends(csrf_protect),
):
    """Replace a user's organization memberships (admin/superadmin only).

    Superadmin callers replace the full membership set; other admins replace
    only memberships in organizations they themselves belong to (issue #721
    AC5). Dropped organizations' group memberships are revoked in the same
    transaction.
    """
    # Omitted (or null) list fields are NOT an explicit clear: reject with 422
    # before any DELETE runs (issue #725, T1-21-KR-11). An explicit empty list
    # is the only way to clear all memberships.
    if not body.any_field_set():
        raise HTTPException(
            status_code=422,
            detail=(
                "Provide at least one of 'memberships' or 'org_ids' "
                "(an explicit empty list clears all organization memberships)"
            ),
        )

    pool = get_pool(str(settings.sqlite_path))
    conn = await pool.get_connection_async()

    def _update_orgs():
        # Verify user exists
        cursor = conn.execute("SELECT id, role FROM users WHERE id = ?", (user_id,))
        target_row = cursor.fetchone()
        if not target_row:
            raise HTTPException(status_code=404, detail="User not found")

        # Superadmin targets are modifiable by superadmins only (issue #722,
        # five-writer parity for assert_can_modify_target).
        assert_can_modify_target(user.get("role"), target_row[1])

        # For non-superadmins, verify caller shares an org with target user
        # and can only assign orgs they belong to (parity + scope restriction).
        caller_orgs = None
        if user.get("role") != "superadmin":
            cursor = conn.execute(
                "SELECT org_id FROM org_members WHERE user_id = ?", (user_id,)
            )
            target_orgs = {row[0] for row in cursor.fetchall()}
            caller_id = user.get("id")
            cursor = conn.execute(
                "SELECT org_id FROM org_members WHERE user_id = ?", (caller_id,)
            )
            caller_orgs = {row[0] for row in cursor.fetchall()}
            if not target_orgs & caller_orgs:  # intersection
                raise HTTPException(
                    status_code=403,
                    detail="Cannot update organization memberships of users outside your organization",
                )

        memberships = body.resolved_memberships()
        valid_roles = ("admin", "member")

        # Validate roles
        for m in memberships:
            if m.role not in valid_roles:
                raise HTTPException(status_code=400, detail=f"Invalid role '{m.role}'. Must be one of: {', '.join(valid_roles)}")

        # Validate all org_ids exist
        if memberships:
            org_ids = [m.org_id for m in memberships]
            placeholders = ",".join("?" * len(org_ids))
            cursor = conn.execute(
                f"SELECT id FROM organizations WHERE id IN ({placeholders})",
                tuple(org_ids),
            )
            found = {row[0] for row in cursor.fetchall()}
            missing = set(org_ids) - found
            if missing:
                raise HTTPException(status_code=400, detail=f"Organizations not found: {sorted(missing)}")

        # Non-superadmins can only assign memberships to orgs they belong to
        if caller_orgs is not None and memberships:
            assigned_org_ids = {m.org_id for m in memberships}
            unauthorized = assigned_org_ids - caller_orgs
            if unauthorized:
                raise HTTPException(
                    status_code=403,
                    detail=f"Cannot assign to organizations you are not a member of: {sorted(unauthorized)}",
                )

        try:
            # Take the write lock BEFORE the before-snapshot: without an
            # explicit transaction, before_orgs is read in autocommit and
            # after_orgs lands inside the write txn the first DELETE opens —
            # two different snapshots — so a concurrent grant committed
            # between them could be deleted by this request while its
            # group_members rows survived the dropped-set derivation
            # (issue #721 review F-001). BEGIN IMMEDIATE pins the write lock
            # and both reads into one snapshot.
            conn.execute("BEGIN IMMEDIATE")

            # Snapshot the target's current non-owner orgs before the replace so
            # group memberships can be cleaned up for orgs this call actually
            # drops (issue #721 AC3). Owner rows are excluded by the same
            # role != 'owner' filter the DELETE below uses.
            cursor = conn.execute(
                "SELECT org_id FROM org_members WHERE user_id = ? AND role != 'owner'",
                (user_id,),
            )
            before_orgs = {row[0] for row in cursor.fetchall()}

            # Delete existing memberships (except owner roles to protect org
            # ownership). Scoped to the caller's own orgs for non-superadmins —
            # an org admin replaces only the memberships they can see (issue
            # #721 AC5), mirroring update_user_groups' scoped delete below.
            # The caller's scope is expressed as a subquery (their org_members
            # rows) rather than dynamically interpolated placeholders.
            if caller_orgs is None:
                conn.execute(
                    "DELETE FROM org_members WHERE user_id = ? AND role != 'owner'",
                    (user_id,),
                )
            else:
                conn.execute(
                    """DELETE FROM org_members
                        WHERE user_id = ? AND role != 'owner'
                          AND org_id IN (SELECT org_id FROM org_members WHERE user_id = ?)""",
                    (user_id, user.get("id")),
                )

            # Insert new memberships
            for m in memberships:
                cursor = conn.execute(
                    "SELECT 1 FROM org_members WHERE org_id = ? AND user_id = ?",
                    (m.org_id, user_id),
                )
                if not cursor.fetchone():
                    conn.execute(
                        "INSERT INTO org_members (org_id, user_id, role) VALUES (?, ?, ?)",
                        (m.org_id, user_id, m.role),
                    )

            # Group membership implies organization membership: drop the
            # target's group_members rows for orgs this call actually removed.
            # The dropped set is derived purely from the target's own row
            # state (present in the before-snapshot, absent after the
            # replace): whatever the scoped or unscoped DELETE above actually
            # removed is exactly before − after — owner-protected rows never
            # entered the snapshot, re-inserted orgs are in after, and orgs
            # outside the DELETE's scope survive into after. Deriving the set
            # from a second caller-orgs read instead would race the DELETE's
            # own subquery (implementation-review finding: a caller-orgs
            # addition committed between two reads could orphan rows).
            cursor = conn.execute(
                "SELECT org_id FROM org_members WHERE user_id = ?",
                (user_id,),
            )
            after_orgs = {row[0] for row in cursor.fetchall()}
            dropped_orgs = before_orgs - after_orgs
            for org_id in dropped_orgs:
                conn.execute(
                    """DELETE FROM group_members
                        WHERE user_id = ?
                          AND group_id IN (SELECT id FROM groups WHERE org_id = ?)""",
                    (user_id, org_id),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        # Return updated list
        cursor = conn.execute(
            """SELECT o.id, o.name, o.description, om.role, om.joined_at
               FROM org_members om JOIN organizations o ON om.org_id = o.id
               WHERE om.user_id = ? ORDER BY o.name""",
            (user_id,),
        )
        orgs = []
        for row in cursor.fetchall():
            orgs.append({
                "id": row[0],
                "name": row[1],
                "description": row[2] or "",
                "role": row[3],
                "joined_at": row[4],
            })
        return {"organizations": orgs}

    try:
        result = await asyncio.to_thread(_update_orgs)
        await safe_record_security_event(
            conn,
            event_type="membership.organizations_replaced",
            actor=user,
            target_user_id=user_id,
            request=request,
            metadata={
                "memberships": [
                    {"org_id": m.org_id, "role": m.role}
                    for m in body.resolved_memberships()
                ]
            },
        )
        return result
    finally:
        pool.release_connection(conn)


@router.get("/{user_id}/groups")
async def get_user_groups(
    user_id: int,
    user: dict = Depends(require_role("admin")),
):
    """Get all groups a user is a member of (admin/superadmin only)."""
    pool = get_pool(str(settings.sqlite_path))
    conn = await pool.get_connection_async()

    try:
        # Verify user exists
        cursor = await asyncio.to_thread(conn.execute, "SELECT id FROM users WHERE id = ?", (user_id,))
        if not await asyncio.to_thread(cursor.fetchone):
            raise HTTPException(status_code=404, detail="User not found")

        # For non-superadmins, verify caller shares an org with target user
        if user.get("role") != "superadmin":
            # Get target user's orgs
            await asyncio.to_thread(cursor.execute, "SELECT org_id FROM org_members WHERE user_id = ?", (user_id,))
            target_orgs = {row[0] for row in await asyncio.to_thread(cursor.fetchall)}
            # Get caller's orgs
            caller_id = user.get("id")
            await asyncio.to_thread(cursor.execute, "SELECT org_id FROM org_members WHERE user_id = ?", (caller_id,))
            caller_orgs = {row[0] for row in await asyncio.to_thread(cursor.fetchall)}
            # Check overlap
            if not target_orgs & caller_orgs:  # intersection
                raise HTTPException(
                    status_code=403,
                    detail="Cannot view group memberships of users outside your organization",
                )

        # Get groups the user is a member of
        cursor = await asyncio.to_thread(conn.execute, """SELECT g.id, g.name, g.description, g.org_id
               FROM groups g
               JOIN group_members gm ON g.id = gm.group_id
               WHERE gm.user_id = ?""", (user_id,))
        rows = await asyncio.to_thread(cursor.fetchall)

        groups = []
        for row in rows:
            groups.append(
                {
                    "id": row[0],
                    "name": row[1],
                    "description": row[2] or "",
                    "org_id": row[3],
                }
            )

        return {"groups": groups}
    finally:
        pool.release_connection(conn)


@router.put("/{user_id}/groups")
async def update_user_groups(
    user_id: int,
    request: Request,
    body: UserGroupsUpdateRequest,
    user: dict = Depends(require_role("admin")),
    _csrf_token: str = Depends(csrf_protect),
):
    """Replace user's group memberships (admin/superadmin only).

    Validates that all groups exist and that the user is a member of each group's organization.
    """
    pool = get_pool(str(settings.sqlite_path))
    conn = await pool.get_connection_async()

    def _update_groups():
        # Verify user exists
        cursor = conn.execute("SELECT id, role FROM users WHERE id = ?", (user_id,))
        target_row = cursor.fetchone()
        if not target_row:
            raise HTTPException(status_code=404, detail="User not found")

        # Superadmin targets are modifiable by superadmins only (issue #722,
        # five-writer parity for assert_can_modify_target).
        assert_can_modify_target(user.get("role"), target_row[1])

        # For non-superadmins, verify caller shares an org with target user
        # (parity with get_user_groups and update_user_organizations).
        caller_orgs = None
        if user.get("role") != "superadmin":
            cursor = conn.execute(
                "SELECT org_id FROM org_members WHERE user_id = ?", (user_id,)
            )
            target_orgs = {row[0] for row in cursor.fetchall()}
            caller_id = user.get("id")
            cursor = conn.execute(
                "SELECT org_id FROM org_members WHERE user_id = ?", (caller_id,)
            )
            caller_orgs = {row[0] for row in cursor.fetchall()}
            if not target_orgs & caller_orgs:  # intersection
                raise HTTPException(
                    status_code=403,
                    detail="Cannot update group memberships of users outside your organization",
                )

        # If group_ids is empty, clear only the caller-managed org scope.
        if not body.group_ids:
            if caller_orgs is None:
                conn.execute("DELETE FROM group_members WHERE user_id = ?", (user_id,))
            else:
                placeholders = ",".join("?" * len(caller_orgs))
                conn.execute(
                    f"""DELETE FROM group_members
                        WHERE user_id = ?
                          AND group_id IN (
                              SELECT id FROM groups WHERE org_id IN ({placeholders})
                          )""",
                    (user_id, *caller_orgs),
                )
            conn.commit()
            cursor = conn.execute(
                """SELECT g.id, g.name, g.description, g.org_id
                   FROM groups g
                   JOIN group_members gm ON g.id = gm.group_id
                   WHERE gm.user_id = ?""",
                (user_id,),
            )
            return [
                {
                    "id": row[0],
                    "name": row[1],
                    "description": row[2] or "",
                    "org_id": row[3],
                }
                for row in cursor.fetchall()
            ]

        # Validate all group_ids exist and get their org_ids
        placeholders = ",".join("?" * len(body.group_ids))
        cursor.execute(
            f"SELECT id, org_id FROM groups WHERE id IN ({placeholders})",
            tuple(body.group_ids),
        )
        found_groups = {row[0]: row[1] for row in cursor.fetchall()}

        missing_groups = set(body.group_ids) - set(found_groups.keys())
        if missing_groups:
            raise HTTPException(
                status_code=400, detail=f"Groups not found: {sorted(missing_groups)}"
            )

        if caller_orgs is not None:
            requested_orgs = set(found_groups.values())
            unauthorized = requested_orgs - caller_orgs
            if unauthorized:
                raise HTTPException(
                    status_code=403,
                    detail=f"Cannot assign groups from organizations you are not a member of: {sorted(unauthorized)}",
                )

        # Check the TARGET user is a member of each group's organization. This
        # is a data-integrity precondition of group membership (groups.py
        # enforces the same one), so it must run for every caller including
        # superadmins; the caller-authorization bypass above (caller_orgs is
        # None for superadmins) is what superadmin privilege waives, not this
        # check (issue #721 AC1).
        for group_id, org_id in found_groups.items():
            cursor.execute(
                "SELECT 1 FROM org_members WHERE user_id = ? AND org_id = ?",
                (user_id, org_id),
            )
            if not cursor.fetchone():
                raise HTTPException(
                    status_code=400,
                    detail=f"User is not a member of organization for group {group_id}",
                )

        # Replace only memberships in the caller-managed org scope.
        if caller_orgs is None:
            cursor.execute("DELETE FROM group_members WHERE user_id = ?", (user_id,))
        else:
            placeholders = ",".join("?" * len(caller_orgs))
            cursor.execute(
                f"""DELETE FROM group_members
                    WHERE user_id = ?
                      AND group_id IN (
                          SELECT id FROM groups WHERE org_id IN ({placeholders})
                      )""",
                (user_id, *caller_orgs),
            )

        # Insert new memberships; dedupe by group id first: group_members has
        # UNIQUE(group_id, user_id) and a duplicate id in one body would 500
        # on the second insert (issue #725, S02-SK-03; groups.py dedupes the
        # same way).
        seen_group_ids = set()
        for group_id in body.group_ids:
            if group_id in seen_group_ids:
                continue
            seen_group_ids.add(group_id)
            cursor.execute(
                "INSERT INTO group_members (group_id, user_id) VALUES (?, ?)",
                (group_id, user_id),
            )

        conn.commit()

        # Return updated list of groups
        cursor.execute(
            """SELECT g.id, g.name, g.description, g.org_id
               FROM groups g
               JOIN group_members gm ON g.id = gm.group_id
               WHERE gm.user_id = ?""",
            (user_id,),
        )
        rows = cursor.fetchall()

        groups = []
        for row in rows:
            groups.append(
                {
                    "id": row[0],
                    "name": row[1],
                    "description": row[2] or "",
                    "org_id": row[3],
                }
            )

        return groups

    try:
        groups = await asyncio.to_thread(_update_groups)
        await safe_record_security_event(
            conn,
            event_type="membership.groups_replaced",
            actor=user,
            target_user_id=user_id,
            request=request,
            metadata={"group_ids": list(body.group_ids)},
        )
        return {"groups": groups}
    finally:
        pool.release_connection(conn)
