"""
Vault API routes for vault management.

Provides endpoints for listing, creating, updating, and deleting vaults.
Vaults are containers for documents, memories, and chat sessions.
"""

import asyncio
import logging
import shutil
import sqlite3
from typing import Callable, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.api.deps import (
    get_current_active_user,
    get_db,
    get_effective_vault_permission,
    get_effective_vault_permissions,
    get_evaluate_policy,
    get_vector_store,
    require_role,
    require_vault_permission,
)
from app.config import settings
from app.limiter import limiter
from app.services.vector_store import VectorStore

logger = logging.getLogger(__name__)


router = APIRouter()


class VaultCreateRequest(BaseModel):
    """Request model for creating a new vault."""

    name: str = Field(..., min_length=1, max_length=255, description="Vault name")
    description: str = Field("", max_length=1000, description="Vault description")
    org_id: Optional[int] = Field(None, description="Organization to assign this vault to")
    visibility: str = Field("private", description="Vault visibility: private, org, or public")

    @field_validator("name", mode="before")
    @classmethod
    def strip_name(cls, v):
        if isinstance(v, str):
            return v.strip()
        return v

    @field_validator("visibility")
    @classmethod
    def validate_visibility(cls, v):
        if v not in ("private", "org", "public"):
            raise ValueError("visibility must be 'private', 'org', or 'public'")
        return v


class VaultUpdateRequest(BaseModel):
    """Request model for updating an existing vault."""

    name: Optional[str] = Field(
        None, min_length=1, max_length=255, description="New vault name"
    )
    description: Optional[str] = Field(
        None, max_length=1000, description="New description"
    )
    visibility: Optional[str] = Field(
        None, description="Vault visibility: private, org, or public"
    )

    @field_validator("name", mode="before")
    @classmethod
    def strip_name(cls, v):
        if isinstance(v, str):
            return v.strip()
        return v

    @field_validator("visibility")
    @classmethod
    def validate_visibility(cls, v):
        if v is not None and v not in ("private", "org", "public"):
            raise ValueError("visibility must be 'private', 'org', or 'public'")
        return v


class VaultResponse(BaseModel):
    """Response model for a vault record."""

    id: int
    name: str
    description: str
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    file_count: int = 0
    memory_count: int = 0
    session_count: int = 0
    org_id: Optional[int] = None
    current_user_permission: Optional[str] = None
    visibility: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class VaultListResponse(BaseModel):
    """Response model for listing vaults."""

    vaults: List[VaultResponse]


def _row_to_vault_response(row, current_user_permission: Optional[str] = None) -> VaultResponse:
    """Map a database row to VaultResponse."""
    vault_id = row[0]
    return VaultResponse(
        id=vault_id,
        name=row[1],
        description=row[2],
        created_at=row[3],
        updated_at=row[4],
        file_count=row[5] or 0,
        memory_count=row[6] or 0,
        session_count=row[7] or 0,
        org_id=row[8],
        visibility=row[9] if len(row) > 9 else None,
        current_user_permission=current_user_permission,
    )


_VAULT_WITH_COUNTS_SQL = """
    SELECT v.id, v.name, v.description, v.created_at, v.updated_at,
           COUNT(DISTINCT f.id) as file_count,
           COUNT(DISTINCT m.id) as memory_count,
           COUNT(DISTINCT cs.id) as session_count,
           v.org_id,
           v.visibility
    FROM vaults v
    LEFT JOIN files f ON f.vault_id = v.id
    LEFT JOIN memories m ON m.vault_id = v.id
    LEFT JOIN chat_sessions cs ON cs.vault_id = v.id
"""


async def _fetch_vault_with_counts(
    conn: sqlite3.Connection, vault_id: int, user: Optional[dict] = None
) -> Optional[VaultResponse]:
    """Fetch a single vault with file/memory/session counts."""
    row = await asyncio.to_thread(
        lambda: conn.execute(
            _VAULT_WITH_COUNTS_SQL + " WHERE v.id = ? GROUP BY v.id",
            (vault_id,),
        ).fetchone()
    )
    if not row:
        return None
    permission = (
        await get_effective_vault_permission(conn, user, vault_id)
        if user
        else None
    )
    return _row_to_vault_response(row, permission)


async def _fetch_all_vaults(
    conn: sqlite3.Connection, user: Optional[dict] = None
) -> List[VaultResponse]:
    """Fetch all vaults with counts, ordered by creation date."""
    rows = await asyncio.to_thread(
        lambda: conn.execute(
            _VAULT_WITH_COUNTS_SQL + " GROUP BY v.id ORDER BY v.created_at ASC LIMIT 1000"
        ).fetchall()
    )
    permissions = (
        await get_effective_vault_permissions(conn, user, [row[0] for row in rows])
        if user
        else {}
    )
    vaults = []
    for row in rows:
        vaults.append(_row_to_vault_response(row, permissions.get(row[0])))
    return vaults


async def _fetch_accessible_vaults(
    conn: sqlite3.Connection, user: Optional[dict] = None
) -> List[VaultResponse]:
    """Fetch only vaults the user might have access to, using SQL prefiltering.

    Builds a UNION query to get candidate vault IDs:
    - (a) vaults where user is directly a member
    - (b) vaults accessible via group membership
    - (c) public/org-visible vaults user belongs to
    - (d) all vaults for admin/superadmin users

    Then applies get_effective_vault_permissions on the reduced set.
    """
    if user is None:
        return []

    user_id = user.get("id")
    user_role = user.get("role", "")

    if not user_id:
        return []

    # For admin/superadmin, fetch all vaults (no filtering needed)
    if user_role in ("admin", "superadmin"):
        return await _fetch_all_vaults(conn, user)

    # Build UNION query for candidate vault IDs
    # (a) vaults where user is directly a member
    # (b) vaults accessible via group membership
    # (c) public/org-visible vaults user belongs to
    # (d) all vaults for superadmin (already handled above)

    union_query = """
        SELECT DISTINCT v.id FROM vaults v
        WHERE v.id IN (
            SELECT vm.vault_id FROM vault_members vm WHERE vm.user_id = ?
            UNION
            SELECT vga.vault_id FROM vault_group_access vga
            JOIN group_members gm ON vga.group_id = gm.group_id
            WHERE gm.user_id = ?
            UNION
            SELECT v.id FROM vaults v WHERE v.visibility = 'public' AND v.org_id IS NULL
            UNION
            SELECT v.id FROM vaults v WHERE v.visibility = 'public' AND v.org_id IS NOT NULL AND EXISTS (
                SELECT 1 FROM org_members om WHERE om.org_id = v.org_id AND om.user_id = ?
            )
            UNION
            SELECT v.id FROM vaults v WHERE v.visibility = 'org' AND v.org_id IS NOT NULL AND EXISTS (
                SELECT 1 FROM org_members om WHERE om.org_id = v.org_id AND om.user_id = ?
            )
        )
    """

    rows = await asyncio.to_thread(
        lambda: conn.execute(union_query, (user_id, user_id, user_id, user_id)).fetchall()
    )

    # Extract vault IDs from rows
    vault_ids = [row[0] for row in rows]

    if not vault_ids:
        return []

    # Get permissions for the filtered vault set
    permissions = await get_effective_vault_permissions(conn, user, vault_ids)

    # Fetch full vault records with counts
    placeholders = ",".join("?" for _ in vault_ids)
    full_query = _VAULT_WITH_COUNTS_SQL + f"""
        WHERE v.id IN ({placeholders})
        GROUP BY v.id
        ORDER BY v.created_at ASC
        LIMIT 1000
    """

    full_rows = await asyncio.to_thread(
        lambda: conn.execute(full_query, tuple(vault_ids)).fetchall()
    )

    # Build vault responses, filtering out any that resolved to None permission
    # (safety net against TOCTOU between UNION and permission check)
    vaults = []
    for row in full_rows:
        v = _row_to_vault_response(row, permissions.get(row[0]))
        if v.current_user_permission is not None:
            vaults.append(v)

    return vaults


@router.get("/vaults/", response_model=VaultListResponse, include_in_schema=False)
@router.get("/vaults", response_model=VaultListResponse)
async def list_vaults(
    user: dict = Depends(require_role("admin")),
    conn: sqlite3.Connection = Depends(get_db),
):
    """List all vaults with document/memory/session counts and permission info. Admin/superadmin only. Regular users should use the /vaults/accessible endpoint."""
    vaults = await _fetch_all_vaults(conn, user)

    return VaultListResponse(vaults=vaults)


@router.get("/vaults/accessible", response_model=VaultListResponse)
async def list_accessible_vaults(
    user: dict = Depends(get_current_active_user),
    conn: sqlite3.Connection = Depends(get_db),
):
    """Return vaults the current user has access to.

    Note: results are capped at 1000 vaults (SQL LIMIT in _fetch_accessible_vaults).
    Pagination is not yet implemented.
    """
    vaults = await _fetch_accessible_vaults(conn, user)
    return VaultListResponse(vaults=vaults)


@router.get("/vaults/{vault_id}", response_model=VaultResponse)
async def get_vault(
    vault_id: int,
    user: dict = Depends(get_current_active_user),
    conn: sqlite3.Connection = Depends(get_db),
    evaluate: Callable = Depends(get_evaluate_policy),
):
    """Get a single vault with counts."""
    vault = await _fetch_vault_with_counts(conn, vault_id, user)
    if vault is None:
        raise HTTPException(
            status_code=404, detail=f"Vault with id {vault_id} not found"
        )

    if not await evaluate(user, "vault", vault_id, "read"):
        raise HTTPException(status_code=403, detail="No read access to this vault")

    return vault


@router.post("/vaults/", response_model=VaultResponse, status_code=201, include_in_schema=False)
@router.post("/vaults", response_model=VaultResponse, status_code=201)
@limiter.limit(settings.vault_create_rate_limit)
async def create_vault(
    request: Request,
    body: VaultCreateRequest,
    conn: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
):
    """
    Create a new vault.

    Creates a new vault with the given name, description, and organization.
    org_id is required unless the caller belongs to exactly one organization
    (in which case it is inferred). Superadmins may pass any org_id.
    Returns 409 if a vault with the same name already exists.
    """
    # Resolve org_id: explicit > inferred > error
    target_org_id = body.org_id
    if target_org_id is None:
        # Try to infer from caller's org memberships
        cursor = await asyncio.to_thread(
            conn.execute,
            "SELECT org_id FROM org_members WHERE user_id = ? ORDER BY org_id LIMIT 2",
            (user["id"],),
        )
        rows = await asyncio.to_thread(cursor.fetchall)
        if len(rows) == 1:
            target_org_id = rows[0][0]
        elif len(rows) > 1:
            raise HTTPException(
                status_code=400,
                detail="User belongs to multiple organizations. Please provide org_id explicitly.",
            )
        # If user has no org, target_org_id remains None (global vault)

    # If org_id provided or inferred, validate it exists and caller is a member
    if target_org_id is not None:
        cursor = await asyncio.to_thread(
            conn.execute,
            "SELECT id FROM organizations WHERE id = ?",
            (target_org_id,),
        )
        if not await asyncio.to_thread(cursor.fetchone):
            raise HTTPException(
                status_code=400,
                detail=f"Organization {target_org_id} not found",
            )
        # Non-superadmins may only create vaults in organizations they belong to
        if user.get("role") != "superadmin":
            cursor = await asyncio.to_thread(
                conn.execute,
                "SELECT 1 FROM org_members WHERE org_id = ? AND user_id = ?",
                (target_org_id, user["id"]),
            )
            if not await asyncio.to_thread(cursor.fetchone):
                raise HTTPException(
                    status_code=403,
                    detail="You are not a member of that organization",
                )

    # Begin explicit transaction for atomic vault + membership creation
    try:
        await asyncio.to_thread(conn.execute, "BEGIN")
        cursor = await asyncio.to_thread(
            conn.execute,
            "INSERT INTO vaults (name, description, org_id, visibility, owner_id) VALUES (?, ?, ?, ?, ?)",
            (body.name, body.description, target_org_id, body.visibility, user["id"]),
        )
        vault_id = cursor.lastrowid
    except sqlite3.IntegrityError:
        try:
            await asyncio.to_thread(conn.rollback)
        except Exception:
            pass
        raise HTTPException(
            status_code=409, detail=f"Vault with name '{body.name}' already exists"
        )
    except Exception:
        try:
            await asyncio.to_thread(conn.rollback)
        except Exception:
            pass
        raise HTTPException(status_code=500, detail="Failed to create vault")

    if vault_id is None:
        try:
            await asyncio.to_thread(conn.rollback)
        except Exception:
            pass
        raise HTTPException(status_code=500, detail="Failed to create vault")

    try:
        await asyncio.to_thread(
            conn.execute,
            "INSERT INTO vault_members (vault_id, user_id, permission) VALUES (?, ?, ?)",
            (vault_id, user["id"], "admin"),
        )
        await asyncio.to_thread(conn.commit)
    except Exception as e:
        try:
            await asyncio.to_thread(conn.rollback)
        except Exception:
            pass
        logger.error(
            "Error creating vault or assigning membership: %s",
            e,
        )
        raise HTTPException(status_code=500, detail="Failed to create vault or assign membership")

    vault = await _fetch_vault_with_counts(conn, vault_id, user)
    return vault


@router.put("/vaults/{vault_id}", response_model=VaultResponse)
async def update_vault(
    vault_id: int,
    request: VaultUpdateRequest,
    conn: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(require_vault_permission("admin")),
):
    """
    Update vault name/description.

    Updates the name and/or description of the vault with the given id.
    Returns 404 if not found.
    Returns 409 if new name conflicts with an existing vault.
    """
    # Check if vault exists
    row = await asyncio.to_thread(
        lambda: conn.execute("SELECT id, name FROM vaults WHERE id = ?", (vault_id,)).fetchone()
    )

    if row is None:
        raise HTTPException(
            status_code=404, detail=f"Vault with id {vault_id} not found"
        )

    # Build update query dynamically based on provided fields
    update_fields = []
    params = []

    if request.name is not None:
        update_fields.append("name = ?")
        params.append(request.name)
    if request.description is not None:
        update_fields.append("description = ?")
        params.append(request.description)
    if request.visibility is not None:
        update_fields.append("visibility = ?")
        params.append(request.visibility)

    if not update_fields:
        # No fields to update, just fetch and return current record
        vault = await _fetch_vault_with_counts(conn, vault_id, user)
        if vault is None:
            raise HTTPException(
                status_code=404, detail=f"Vault with id {vault_id} not found"
            )
        return vault

    # Add vault_id to params
    params.append(vault_id)

    # Execute update with error handling for duplicate name
    try:
        sql = f"""
            UPDATE vaults
            SET {", ".join(update_fields)}, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """
        await asyncio.to_thread(conn.execute, sql, params)
        await asyncio.to_thread(conn.commit)
    except sqlite3.IntegrityError:
        await asyncio.to_thread(conn.rollback)
        raise HTTPException(
            status_code=409, detail=f"Vault with name '{request.name}' already exists"
        )

    # Rename vault folder if name changed
    if request.name is not None:
        try:
            from app.services.upload_path import _rename_vault_folder

            old_name = row[1]  # Original vault name from line 194
            new_name = request.name
            if old_name != new_name:
                await asyncio.to_thread(_rename_vault_folder, vault_id, old_name, new_name)
        except (OSError, shutil.Error) as e:
            # Log but don't fail the rename - folder rename is not critical
            logging.getLogger(__name__).warning(f"Failed to rename vault folder: {e}")

    # Fetch updated record
    vault = await _fetch_vault_with_counts(conn, vault_id, user)

    # Race condition fix: check if vault is None after fetch
    if vault is None:
        raise HTTPException(
            status_code=404, detail=f"Vault with id {vault_id} not found"
        )

    return vault


@router.delete("/vaults/{vault_id}")
async def delete_vault(
    vault_id: int,
    conn: sqlite3.Connection = Depends(get_db),
    vector_store: VectorStore = Depends(get_vector_store),
    user: dict = Depends(require_vault_permission("admin")),
):
    """
    Delete vault with cascade cleanup.

    Deletes the vault and all associated data:
    - All files in the vault (and their chunks in vector store)
    - All chat sessions and messages (via cascade)
    - Vector chunks for this vault
    - Memories: reassign to global (SET vault_id = NULL)

    Returns 404 if not found.
    """
    # Check if vault exists
    row = await asyncio.to_thread(
        lambda: conn.execute("SELECT id, name FROM vaults WHERE id = ?", (vault_id,)).fetchone()
    )

    if row is None:
        raise HTTPException(
            status_code=404, detail=f"Vault with id {vault_id} not found"
        )

    vault_name = row[1]

    try:
        # Start transaction
        await asyncio.to_thread(conn.execute, "BEGIN TRANSACTION")

        # Delete vector chunks for this vault
        try:
            deleted_chunks = await vector_store.delete_by_vault(str(vault_id))
            logger.info(
                "Deleted %d chunks from vector store for vault_id %s",
                deleted_chunks,
                vault_id,
            )
        except (OSError, RuntimeError, ValueError) as e:
            logger.warning("Error deleting chunks from vector store: %s", e)
            # Continue with database deletion even if vector store fails

        # Reassign memories to global (NULL) instead of deleting
        await asyncio.to_thread(
            conn.execute,
            "UPDATE memories SET vault_id = NULL WHERE vault_id = ?",
            (vault_id,),
        )

        # Delete chat_sessions (chat_messages will cascade)
        await asyncio.to_thread(
            conn.execute, "DELETE FROM chat_sessions WHERE vault_id = ?", (vault_id,)
        )

        # Delete files
        await asyncio.to_thread(
            conn.execute, "DELETE FROM files WHERE vault_id = ?", (vault_id,)
        )

        # Delete the vault itself
        await asyncio.to_thread(
            conn.execute, "DELETE FROM vaults WHERE id = ?", (vault_id,)
        )

        # Commit transaction
        await asyncio.to_thread(conn.commit)

        return {
            "message": f"Vault '{vault_name}' (id: {vault_id}) deleted successfully"
        }

    except HTTPException:
        await asyncio.to_thread(lambda: conn.rollback())
        raise
    except (sqlite3.Error, OSError, RuntimeError) as e:
        await asyncio.to_thread(lambda: conn.rollback())
        logger.exception("Error deleting vault %d", vault_id)
        raise HTTPException(status_code=500, detail=f"Delete failed: {e}")


class GroupAccessItem(BaseModel):
    """Single group + permission pair for vault group access updates."""

    group_id: int
    permission: str = "read"

    @field_validator("permission")
    @classmethod
    def validate_permission(cls, v: str) -> str:
        if v not in ("read", "write", "admin"):
            raise ValueError("permission must be read, write, or admin")
        return v


class VaultGroupsUpdateRequest(BaseModel):
    """Request model for updating vault group access.

    Preferred: ``vault_access: [{group_id, permission}]``
    Legacy: ``group_ids: [int]`` (all get 'read' permission)
    """

    vault_access: Optional[List[GroupAccessItem]] = None
    group_ids: Optional[List[int]] = None

    def resolved_access(self) -> List[GroupAccessItem]:
        """Return the canonical list of group+permission pairs."""
        if self.vault_access is not None:
            return self.vault_access
        if self.group_ids:
            return [GroupAccessItem(group_id=gid, permission="read") for gid in self.group_ids]
        return []


class VaultGroupResponse(BaseModel):
    """Response model for a group with vault access."""

    id: int
    name: str
    description: str


class VaultGroupsListResponse(BaseModel):
    """Response model for listing groups with vault access."""

    groups: List[VaultGroupResponse]


@router.get("/vaults/{vault_id}/groups", response_model=VaultGroupsListResponse)
async def get_vault_groups(
    vault_id: int,
    user: dict = Depends(get_current_active_user),
    conn: sqlite3.Connection = Depends(get_db),
    evaluate: Callable = Depends(get_evaluate_policy),
):
    """
    Get all groups that have access to this vault.

    Returns a list of groups with vault access.
    Requires admin permission on the vault.
    Returns 404 if vault doesn't exist.
    """
    # Check if vault exists
    row = await asyncio.to_thread(
        lambda: conn.execute("SELECT id FROM vaults WHERE id = ?", (vault_id,)).fetchone()
    )
    if row is None:
        raise HTTPException(
            status_code=404, detail=f"Vault with id {vault_id} not found"
        )

    # Check admin permission on vault
    if not await evaluate(user, "vault", vault_id, "admin"):
        raise HTTPException(status_code=403, detail="No admin access to this vault")

    # Fetch groups with access to this vault
    rows = await asyncio.to_thread(
        lambda: conn.execute(
            """
            SELECT g.id, g.name, g.description
            FROM groups g
            JOIN vault_group_access vga ON g.id = vga.group_id
            WHERE vga.vault_id = ?
            ORDER BY g.name
            """,
            (vault_id,),
        ).fetchall()
    )

    groups = [
        VaultGroupResponse(id=row[0], name=row[1], description=row[2] or "")
        for row in rows
    ]

    return VaultGroupsListResponse(groups=groups)


@router.put("/vaults/{vault_id}/groups", response_model=VaultGroupsListResponse)
async def update_vault_groups(
    vault_id: int,
    request: VaultGroupsUpdateRequest,
    user: dict = Depends(require_vault_permission("admin")),
    conn: sqlite3.Connection = Depends(get_db),
):
    """
    Update which groups have access to this vault.

    Replaces the current group access list with the provided group_ids.
    Validates all group_ids exist and belong to the same org as the vault.
    Returns the updated list of groups with vault access.
    Returns 400 if any group_ids are invalid or belong to a different org.
    Returns 404 if vault doesn't exist.
    """
    # Check if vault exists and get its org_id
    vault_row = await asyncio.to_thread(
        lambda: conn.execute("SELECT id, org_id FROM vaults WHERE id = ?", (vault_id,)).fetchone()
    )
    if vault_row is None:
        raise HTTPException(
            status_code=404, detail=f"Vault with id {vault_id} not found"
        )
    vault_org_id = vault_row[1]

    access_items = request.resolved_access()

    # If no access items provided, just clear all access and return empty list
    if not access_items:
        await asyncio.to_thread(
            conn.execute,
            "DELETE FROM vault_group_access WHERE vault_id = ?",
            (vault_id,),
        )
        await asyncio.to_thread(conn.commit)
        return VaultGroupsListResponse(groups=[])

    group_ids = [item.group_id for item in access_items]

    # Validate all group_ids exist and belong to the same org as the vault
    placeholders = ",".join("?" * len(group_ids))
    group_rows = await asyncio.to_thread(
        lambda: conn.execute(
            f"""
            SELECT id, org_id FROM groups
            WHERE id IN ({placeholders})
            """,
            tuple(group_ids),
        ).fetchall()
    )

    found_group_ids = {row[0] for row in group_rows}
    missing_group_ids = set(group_ids) - found_group_ids
    if missing_group_ids:
        raise HTTPException(
            status_code=400, detail=f"Group(s) not found: {sorted(missing_group_ids)}"
        )

    # Validate all groups belong to the same org as the vault
    # Safe NULL comparison: only reject if both have non-NULL org_ids that differ
    invalid_org_groups = [
        row[0]
        for row in group_rows
        if (row[1] is not None and vault_org_id is not None and row[1] != vault_org_id)
    ]
    if invalid_org_groups:
        raise HTTPException(
            status_code=400,
            detail=f"Group(s) belong to a different organization: {sorted(invalid_org_groups)}",
        )

    # Delete existing vault_group_access for this vault and insert new records atomically
    await asyncio.to_thread(conn.execute, "BEGIN TRANSACTION")
    try:
        await asyncio.to_thread(
            conn.execute,
            "DELETE FROM vault_group_access WHERE vault_id = ?",
            (vault_id,),
        )

        granted_by = user.get("id")
        for item in access_items:
            await asyncio.to_thread(
                conn.execute,
                """
                    INSERT INTO vault_group_access (vault_id, group_id, permission, granted_by)
                    VALUES (?, ?, ?, ?)
                """,
                (vault_id, item.group_id, item.permission, granted_by),
            )
        await asyncio.to_thread(conn.commit)
    except Exception:
        await asyncio.to_thread(lambda: conn.rollback())
        raise

    # Fetch and return updated groups
    rows = await asyncio.to_thread(
        lambda: conn.execute(
            """
            SELECT g.id, g.name, g.description
            FROM groups g
            JOIN vault_group_access vga ON g.id = vga.group_id
            WHERE vga.vault_id = ?
            ORDER BY g.name
            """,
            (vault_id,),
        ).fetchall()
    )

    groups = [
        VaultGroupResponse(id=row[0], name=row[1], description=row[2] or "")
        for row in rows
    ]

    return VaultGroupsListResponse(groups=groups)
