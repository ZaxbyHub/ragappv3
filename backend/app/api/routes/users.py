"""User management routes (admin/superadmin only)."""

import asyncio
import logging
import sqlite3
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

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
from app.services.security_audit import safe_record_security_event

router = APIRouter(prefix="/users", tags=["users"])
logger = logging.getLogger(__name__)


class UpdateRoleRequest(BaseModel):
    role: str = Field(...)


class UpdateActiveRequest(BaseModel):
    is_active: bool = Field(...)


class UpdateUserRequest(BaseModel):
    username: Optional[str] = Field(default=None, max_length=255)
    full_name: Optional[str] = Field(default=None, max_length=255)
    role: Optional[str] = Field(default=None)


class AdminResetPasswordRequest(BaseModel):
    new_password: str = Field(..., max_length=128)


class CreateUserRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=255)
    password: str = Field(..., min_length=8, max_length=128)
    full_name: str = Field(default="", max_length=255)
    role: str = Field(default="member")


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

    Only superadmin can create other superadmins.
    """
    valid_roles = ["superadmin", "admin", "member", "viewer"]
    if body.role not in valid_roles:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid role. Must be one of: {', '.join(valid_roles)}",
        )

    # Only superadmin can create other superadmins
    if body.role == "superadmin" and user.get("role") != "superadmin":
        raise HTTPException(
            status_code=403,
            detail="Only superadmin can create superadmin users",
        )

    # Validate password strength
    try:
        password_strength_check(body.password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Hash the password
    hashed_password = await async_hash_password(body.password)

    pool = get_pool(str(settings.sqlite_path))
    conn = pool.get_connection()

    try:
        # Check username uniqueness (case-insensitive)
        cursor = await asyncio.to_thread(conn.execute, "SELECT id FROM users WHERE username = ? COLLATE NOCASE", (body.username,))
        if await asyncio.to_thread(cursor.fetchone):
            raise HTTPException(
                status_code=400,
                detail=f"Username '{body.username}' already exists",
            )

        # Insert the new user and default assignments in one transaction.
        cursor = await asyncio.to_thread(conn.execute, """INSERT INTO users (username, hashed_password, full_name, role, is_active)
            VALUES (?, ?, ?, ?, 1)""", (body.username, hashed_password, body.full_name, body.role))
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
    skip: int = 0,
    limit: int = 100,
    q: str | None = None,
    user: dict = Depends(require_role("admin")),
):
    """List all users (admin/superadmin only). Optional ?q= search filter."""
    pool = get_pool(str(settings.sqlite_path))
    conn = pool.get_connection()

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
    conn = pool.get_connection()

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

        # Only superadmin can assign superadmin role
        if body.role == "superadmin" and user.get("role") != "superadmin":
            raise HTTPException(
                status_code=403,
                detail="Only superadmin can assign superadmin role",
            )
        if target_row[3] == "superadmin" and user.get("role") != "superadmin":
            raise HTTPException(
                status_code=403,
                detail="Only superadmin can change role of superadmin users",
            )

        valid_roles = ["superadmin", "admin", "member", "viewer"]
        if body.role not in valid_roles:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid role. Must be one of: {', '.join(valid_roles)}",
            )

        # Privilege-escalation guard: only a superadmin may grant the
        # superadmin role or modify a user who is already a superadmin. An
        # ordinary admin must not be able to promote anyone (including via a
        # second account) past their own level.
        if user.get("role") != "superadmin" and (
            body.role == "superadmin" or target_row[3] == "superadmin"
        ):
            raise HTTPException(
                status_code=403,
                detail="Only a superadmin can grant or modify the superadmin role",
            )

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

    # Execute update
    update_values.append(user_id)
    await asyncio.to_thread(db.execute, f"UPDATE users SET {', '.join(update_fields)} WHERE id = ?", tuple(update_values))
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
    cursor = await asyncio.to_thread(db.execute, "SELECT id FROM users WHERE id = ?", (user_id,))
    if not await asyncio.to_thread(cursor.fetchone):
        raise HTTPException(status_code=404, detail="User not found")

    # Validate password strength
    try:
        password_strength_check(body.new_password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Hash the new password
    hashed_password = await async_hash_password(body.new_password)

    def _admin_reset_password_db():
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
        metadata={"must_change_password": True, "sessions_revoked": True},
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
    valid_roles = ["superadmin", "admin", "member", "viewer"]
    if body.role not in valid_roles:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid role. Must be one of: {', '.join(valid_roles)}",
        )

    pool = get_pool(str(settings.sqlite_path))
    conn = pool.get_connection()

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
    conn = pool.get_connection()

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
    user: dict = Depends(require_role("superadmin")),
    _csrf_token: str = Depends(csrf_protect),
):
    """Delete user (superadmin only). Cannot delete last superadmin or self."""
    pool = get_pool(str(settings.sqlite_path))
    conn = pool.get_connection()

    try:
        cursor = await asyncio.to_thread(conn.execute, "SELECT role FROM users WHERE id = ?", (user_id,))
        target_row = await asyncio.to_thread(cursor.fetchone)

        if not target_row:
            raise HTTPException(status_code=404, detail="User not found")

        target_role = target_row[0]

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

        invalidate_active_user_cache(user_id)

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
    conn = pool.get_connection()
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
    """Replace user's organization memberships (admin/superadmin only)."""
    pool = get_pool(str(settings.sqlite_path))
    conn = pool.get_connection()

    def _update_orgs():
        # Verify user exists
        cursor = conn.execute("SELECT id FROM users WHERE id = ?", (user_id,))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="User not found")

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
            # Delete existing memberships (except owner roles to protect org ownership)
            conn.execute(
                "DELETE FROM org_members WHERE user_id = ? AND role != 'owner'",
                (user_id,),
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
    conn = pool.get_connection()

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
    conn = pool.get_connection()

    def _update_groups():
        # Verify user exists
        cursor = conn.execute("SELECT id FROM users WHERE id = ?", (user_id,))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="User not found")

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

        # Check user is a member of each group's organization (skip for superadmins)
        if user.get("role") != "superadmin":
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

        # Insert new memberships
        for group_id in body.group_ids:
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
