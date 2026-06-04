"""User management routes (admin/superadmin only)."""

import asyncio
import logging
import sqlite3
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.deps import (
    assign_user_to_default_vault,
    get_db,
    require_admin_role,
    require_role,
)
from app.api.routes.auth import async_hash_password
from app.config import settings
from app.models.database import get_pool
from app.security import csrf_protect
from app.services.auth_service import hash_password, password_strength_check

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

        return {
            "id": row[0],
            "username": row[1],
            "full_name": row[2] or "",
            "role": row[3],
            "is_active": bool(row[4]),
            "created_at": row[5],
        }
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
            search_pattern = f"%{q}%"
            count_cursor = await asyncio.to_thread(conn.execute, "SELECT COUNT(*) FROM users WHERE username LIKE ? OR full_name LIKE ?", (search_pattern, search_pattern))
            total = (await asyncio.to_thread(count_cursor.fetchone))[0]
            cursor = await asyncio.to_thread(conn.execute, """SELECT id, username, full_name, role, is_active, created_at
                   FROM users WHERE username LIKE ? OR full_name LIKE ?
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
    body: UpdateUserRequest,
    user: dict = Depends(require_admin_role),
    db: sqlite3.Connection = Depends(get_db),
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
    body: AdminResetPasswordRequest,
    user: dict = Depends(require_admin_role),
    db: sqlite3.Connection = Depends(get_db),
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
    hashed_password = hash_password(body.new_password)

    # Update password and force password change on next login
    await asyncio.to_thread(db.execute, "UPDATE users SET hashed_password = ?, must_change_password = 1 WHERE id = ?", (hashed_password, user_id))
    await asyncio.to_thread(db.commit)

    return {
        "message": "Password reset successfully",
        "must_change_password": True,
    }


@router.patch("/{user_id}/role")
async def update_user_role(
    user_id: int,
    body: UpdateRoleRequest,
    user: dict = Depends(require_role("superadmin")),
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
    body: UpdateActiveRequest,
    user: dict = Depends(require_role("admin")),
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

        status_str = "activated" if body.is_active else "deactivated"
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
    body: UserOrgsUpdateRequest,
    user: dict = Depends(require_role("admin")),
):
    """Replace user's organization memberships (admin/superadmin only)."""
    pool = get_pool(str(settings.sqlite_path))
    conn = pool.get_connection()
    try:
        cursor = conn.execute("SELECT id FROM users WHERE id = ?", (user_id,))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="User not found")

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
    body: UserGroupsUpdateRequest,
    user: dict = Depends(require_role("admin")),
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

        # If group_ids is empty, just delete all memberships and return empty list
        if not body.group_ids:
            cursor.execute("DELETE FROM group_members WHERE user_id = ?", (user_id,))
            conn.commit()
            return []

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

        # Delete existing memberships
        cursor.execute("DELETE FROM group_members WHERE user_id = ?", (user_id,))

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
        return {"groups": groups}
    finally:
        pool.release_connection(conn)
