import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from app.api.deps import require_vault_permission
from app.config import settings
from app.models.database import get_pool

router = APIRouter(prefix="/vaults/{vault_id}/members", tags=["vault-members"])
group_access_router = APIRouter(
    prefix="/vaults/{vault_id}/group-access", tags=["vault-group-access"]
)

VALID_PERMISSIONS = ("read", "write", "admin")


class VaultMemberCreateRequest(BaseModel):
    member_user_id: int = Field(..., gt=0)
    permission: str = Field(...)

    @field_validator("permission")
    @classmethod
    def validate_permission(cls, v):
        if v not in VALID_PERMISSIONS:
            raise ValueError(
                f"Permission must be one of: {', '.join(VALID_PERMISSIONS)}"
            )
        return v


class VaultMemberUpdateRequest(BaseModel):
    permission: str = Field(...)

    @field_validator("permission")
    @classmethod
    def validate_permission(cls, v):
        if v not in VALID_PERMISSIONS:
            raise ValueError(
                f"Permission must be one of: {', '.join(VALID_PERMISSIONS)}"
            )
        return v


class VaultGroupAccessCreateRequest(BaseModel):
    group_id: int = Field(..., gt=0)
    permission: str = Field(...)

    @field_validator("permission")
    @classmethod
    def validate_permission(cls, v):
        if v not in VALID_PERMISSIONS:
            raise ValueError(
                f"Permission must be one of: {', '.join(VALID_PERMISSIONS)}"
            )
        return v


class VaultGroupAccessUpdateRequest(BaseModel):
    permission: str = Field(...)

    @field_validator("permission")
    @classmethod
    def validate_permission(cls, v):
        if v not in VALID_PERMISSIONS:
            raise ValueError(
                f"Permission must be one of: {', '.join(VALID_PERMISSIONS)}"
            )
        return v


@router.get("/")
def list_vault_members(
    vault_id: int,
    current_user: dict = Depends(require_vault_permission("read")),
):
    """List all members of a vault."""
    pool = get_pool(str(settings.sqlite_path))
    conn = pool.get_connection()
    try:
        cursor = conn.cursor()

        # Verify vault exists
        cursor.execute("SELECT id FROM vaults WHERE id = ?", (vault_id,))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="Vault not found")

        # Count total members
        cursor.execute(
            "SELECT COUNT(*) FROM vault_members WHERE vault_id = ?", (vault_id,)
        )
        total_count = cursor.fetchone()[0]

        # Fetch members with user info
        cursor.execute(
            """
            SELECT vm.user_id, u.username, u.full_name, vm.permission, vm.granted_at, vm.granted_by
            FROM vault_members vm JOIN users u ON vm.user_id = u.id
            WHERE vm.vault_id = ? ORDER BY u.username
            """,
            (vault_id,),
        )
        rows = cursor.fetchall()

        members = [
            {
                "user_id": row[0],
                "username": row[1],
                "full_name": row[2] if row[2] else "",
                "permission": row[3],
                "granted_at": row[4],
                "granted_by": row[5],
            }
            for row in rows
        ]

        return {"members": members, "total": total_count}
    finally:
        pool.release_connection(conn)


@router.post("/")
def add_vault_member(
    vault_id: int,
    request: VaultMemberCreateRequest,
    current_user: dict = Depends(require_vault_permission("admin")),
):
    """Add a member to a vault."""
    pool = get_pool(str(settings.sqlite_path))
    conn = pool.get_connection()
    try:
        cursor = conn.cursor()

        # Verify vault exists
        cursor.execute("SELECT id FROM vaults WHERE id = ?", (vault_id,))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="Vault not found")

        # Verify target user exists and is active
        cursor.execute(
            "SELECT id FROM users WHERE id = ? AND is_active = 1",
            (request.member_user_id,),
        )
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="User not found or inactive")

        # Check not already member
        cursor.execute(
            "SELECT user_id FROM vault_members WHERE vault_id = ? AND user_id = ?",
            (vault_id, request.member_user_id),
        )
        if cursor.fetchone():
            raise HTTPException(status_code=409, detail="User is already a member")

        # Insert new member
        granted_by = current_user.get("id")
        cursor.execute(
            "INSERT INTO vault_members (vault_id, user_id, permission, granted_by) VALUES (?, ?, ?, ?)",
            (vault_id, request.member_user_id, request.permission, granted_by),
        )
        conn.commit()

        # Fetch and return the new member
        cursor.execute(
            """
            SELECT vm.user_id, u.username, u.full_name, vm.permission, vm.granted_at, vm.granted_by
            FROM vault_members vm JOIN users u ON vm.user_id = u.id
            WHERE vm.vault_id = ? AND vm.user_id = ?
            """,
            (vault_id, request.member_user_id),
        )
        row = cursor.fetchone()

        return {
            "user_id": row[0],
            "username": row[1],
            "full_name": row[2] if row[2] else "",
            "permission": row[3],
            "granted_at": row[4],
            "granted_by": row[5],
        }
    except sqlite3.IntegrityError:
        conn.rollback()
        raise HTTPException(
            status_code=409, detail="User is already a member of this vault"
        )
    finally:
        pool.release_connection(conn)


@router.patch("/{member_user_id}")
def update_vault_member(
    vault_id: int,
    member_user_id: int,
    request: VaultMemberUpdateRequest,
    current_user: dict = Depends(require_vault_permission("admin")),
):
    """Update a vault member's permission."""
    pool = get_pool(str(settings.sqlite_path))
    conn = pool.get_connection()
    try:
        cursor = conn.cursor()

        # Verify vault exists
        cursor.execute("SELECT id FROM vaults WHERE id = ?", (vault_id,))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="Vault not found")

        # Check member exists
        cursor.execute(
            "SELECT user_id FROM vault_members WHERE vault_id = ? AND user_id = ?",
            (vault_id, member_user_id),
        )
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="Member not found")

        # Update member permission
        granted_by = current_user.get("id")
        try:
            cursor.execute(
                "UPDATE vault_members SET permission = ?, granted_by = ? WHERE vault_id = ? AND user_id = ?",
                (request.permission, granted_by, vault_id, member_user_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        # Fetch and return updated member
        cursor.execute(
            """
            SELECT vm.user_id, u.username, u.full_name, vm.permission, vm.granted_at, vm.granted_by
            FROM vault_members vm JOIN users u ON vm.user_id = u.id
            WHERE vm.vault_id = ? AND vm.user_id = ?
            """,
            (vault_id, member_user_id),
        )
        row = cursor.fetchone()

        return {
            "user_id": row[0],
            "username": row[1],
            "full_name": row[2] if row[2] else "",
            "permission": row[3],
            "granted_at": row[4],
            "granted_by": row[5],
        }
    finally:
        pool.release_connection(conn)


@router.delete("/{member_user_id}")
def remove_vault_member(
    vault_id: int,
    member_user_id: int,
    current_user: dict = Depends(require_vault_permission("admin")),
):
    """Remove a member from a vault."""
    pool = get_pool(str(settings.sqlite_path))
    conn = pool.get_connection()
    try:
        cursor = conn.cursor()

        # Verify vault exists
        cursor.execute("SELECT id FROM vaults WHERE id = ?", (vault_id,))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="Vault not found")

        # Check member exists
        cursor.execute(
            "SELECT user_id FROM vault_members WHERE vault_id = ? AND user_id = ?",
            (vault_id, member_user_id),
        )
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="Member not found")

        # Guard against self-removal
        if member_user_id == current_user.get("id"):
            raise HTTPException(
                status_code=400, detail="Cannot remove yourself from a vault"
            )

        # Delete member
        try:
            cursor.execute(
                "DELETE FROM vault_members WHERE vault_id = ? AND user_id = ?",
                (vault_id, member_user_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        return {
            "message": "Member removed",
            "vault_id": vault_id,
            "user_id": member_user_id,
        }
    finally:
        pool.release_connection(conn)


# =============================================================================
# Vault Group Access Routes
# =============================================================================


@group_access_router.get("/")
def list_vault_group_access(
    vault_id: int,
    current_user: dict = Depends(require_vault_permission("read")),
):
    """List all groups with access to a vault."""
    pool = get_pool(str(settings.sqlite_path))
    conn = pool.get_connection()
    try:
        cursor = conn.cursor()

        # Verify vault exists
        cursor.execute("SELECT id FROM vaults WHERE id = ?", (vault_id,))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="Vault not found")

        # Count total group access entries
        cursor.execute(
            "SELECT COUNT(*) FROM vault_group_access WHERE vault_id = ?", (vault_id,)
        )
        total_count = cursor.fetchone()[0]

        # Fetch group access with group and org info
        cursor.execute(
            """
            SELECT vga.group_id, g.name, o.name, vga.permission, vga.granted_at, vga.granted_by
            FROM vault_group_access vga
            JOIN groups g ON vga.group_id = g.id
            JOIN organizations o ON g.org_id = o.id
            WHERE vga.vault_id = ? ORDER BY g.name
            """,
            (vault_id,),
        )
        rows = cursor.fetchall()

        group_access = [
            {
                "group_id": row[0],
                "group_name": row[1],
                "org_name": row[2],
                "permission": row[3],
                "granted_at": row[4],
                "granted_by": row[5],
            }
            for row in rows
        ]

        return {"group_access": group_access, "total": total_count}
    finally:
        pool.release_connection(conn)


@group_access_router.post("/")
def grant_vault_group_access(
    vault_id: int,
    request: VaultGroupAccessCreateRequest,
    current_user: dict = Depends(require_vault_permission("admin")),
):
    """Grant a group access to a vault."""
    pool = get_pool(str(settings.sqlite_path))
    conn = pool.get_connection()
    try:
        cursor = conn.cursor()

        # Verify vault exists and get org_id
        cursor.execute("SELECT id, org_id FROM vaults WHERE id = ?", (vault_id,))
        vault_row = cursor.fetchone()
        if not vault_row:
            raise HTTPException(status_code=404, detail="Vault not found")
        vault_org_id = vault_row[1]

        # Verify target group exists and get org_id
        cursor.execute("SELECT id, org_id FROM groups WHERE id = ?", (request.group_id,))
        group_row = cursor.fetchone()
        if not group_row:
            raise HTTPException(status_code=404, detail="Group not found")
        group_org_id = group_row[1]

        # Check not already granted
        cursor.execute(
            "SELECT group_id FROM vault_group_access WHERE vault_id = ? AND group_id = ?",
            (vault_id, request.group_id),
        )
        if cursor.fetchone():
            raise HTTPException(
                status_code=409, detail="Group already has access to this vault"
            )

        # Validate group and vault belong to the same org (NULL-safe comparison)
        # Only reject if both have non-NULL org_ids that differ
        if (group_org_id is not None and vault_org_id is not None and group_org_id != vault_org_id):
            raise HTTPException(
                status_code=400,
                detail="Group belongs to a different organization than this vault",
            )

        # Insert new group access
        granted_by = current_user.get("id")
        cursor.execute(
            "INSERT INTO vault_group_access (vault_id, group_id, permission, granted_by) VALUES (?, ?, ?, ?)",
            (vault_id, request.group_id, request.permission, granted_by),
        )
        conn.commit()

        # Fetch and return the new group access
        cursor.execute(
            """
            SELECT vga.group_id, g.name, o.name, vga.permission, vga.granted_at, vga.granted_by
            FROM vault_group_access vga
            JOIN groups g ON vga.group_id = g.id
            JOIN organizations o ON g.org_id = o.id
            WHERE vga.vault_id = ? AND vga.group_id = ?
            """,
            (vault_id, request.group_id),
        )
        row = cursor.fetchone()

        return {
            "group_id": row[0],
            "group_name": row[1],
            "org_name": row[2],
            "permission": row[3],
            "granted_at": row[4],
            "granted_by": row[5],
        }
    except HTTPException:
        conn.rollback()
        raise
    except sqlite3.IntegrityError:
        conn.rollback()
        raise HTTPException(
            status_code=409, detail="Group already has access to this vault"
        )
    finally:
        pool.release_connection(conn)


@group_access_router.patch("/{group_id}")
def update_vault_group_access(
    vault_id: int,
    group_id: int,
    request: VaultGroupAccessUpdateRequest,
    current_user: dict = Depends(require_vault_permission("admin")),
):
    """Update a group's permission for a vault."""
    pool = get_pool(str(settings.sqlite_path))
    conn = pool.get_connection()
    try:
        cursor = conn.cursor()

        # Verify vault exists
        cursor.execute("SELECT id FROM vaults WHERE id = ?", (vault_id,))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="Vault not found")

        # Check group access exists
        cursor.execute(
            "SELECT group_id FROM vault_group_access WHERE vault_id = ? AND group_id = ?",
            (vault_id, group_id),
        )
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="Group access not found")

        # Update group permission
        granted_by = current_user.get("id")
        try:
            cursor.execute(
                "UPDATE vault_group_access SET permission = ?, granted_by = ? WHERE vault_id = ? AND group_id = ?",
                (request.permission, granted_by, vault_id, group_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        # Fetch and return updated group access
        cursor.execute(
            """
            SELECT vga.group_id, g.name, o.name, vga.permission, vga.granted_at, vga.granted_by
            FROM vault_group_access vga
            JOIN groups g ON vga.group_id = g.id
            JOIN organizations o ON g.org_id = o.id
            WHERE vga.vault_id = ? AND vga.group_id = ?
            """,
            (vault_id, group_id),
        )
        row = cursor.fetchone()

        return {
            "group_id": row[0],
            "group_name": row[1],
            "org_name": row[2],
            "permission": row[3],
            "granted_at": row[4],
            "granted_by": row[5],
        }
    finally:
        pool.release_connection(conn)


@group_access_router.delete("/{group_id}")
def revoke_vault_group_access(
    vault_id: int,
    group_id: int,
    current_user: dict = Depends(require_vault_permission("admin")),
):
    """Remove a group's access from a vault."""
    pool = get_pool(str(settings.sqlite_path))
    conn = pool.get_connection()
    try:
        cursor = conn.cursor()

        # Verify vault exists
        cursor.execute("SELECT id FROM vaults WHERE id = ?", (vault_id,))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="Vault not found")

        # Check group access exists
        cursor.execute(
            "SELECT group_id FROM vault_group_access WHERE vault_id = ? AND group_id = ?",
            (vault_id, group_id),
        )
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="Group access not found")

        # Delete group access
        try:
            cursor.execute(
                "DELETE FROM vault_group_access WHERE vault_id = ? AND group_id = ?",
                (vault_id, group_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        return {
            "message": "Group access revoked",
            "vault_id": vault_id,
            "group_id": group_id,
        }
    finally:
        pool.release_connection(conn)
