"""Groups management routes (admin panel)."""

import asyncio
import sqlite3
from collections.abc import Callable
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.api.deps import (
    MultipleOrgError,
    get_db,
    get_evaluate_policy,
    get_user_primary_org,
    require_role,
)
from app.models.database import transaction_context
from app.security import csrf_protect

router = APIRouter(prefix="/groups", tags=["groups"])


class GroupResponse(BaseModel):
    """Response model for a group."""

    id: int
    org_id: int
    name: str
    description: str
    created_at: Optional[str] = None
    organization_name: str

    model_config = ConfigDict(from_attributes=True)


class GroupListResponse(BaseModel):
    """Response model for listing groups."""

    groups: List[GroupResponse]
    total: int
    page: int
    per_page: int


class GroupCreateRequest(BaseModel):
    """Request model for creating a group."""

    name: str = Field(..., min_length=1)
    description: Optional[str] = None
    org_id: Optional[int] = None


class GroupUpdateRequest(BaseModel):
    """Request model for updating a group."""

    name: Optional[str] = None
    description: Optional[str] = None


class GroupMemberResponse(BaseModel):
    """Response model for a group member."""

    id: int
    username: str
    full_name: str


class GroupMembersUpdateRequest(BaseModel):
    """Request model for updating group members."""

    user_ids: List[int]


class GroupVaultResponse(BaseModel):
    """Response model for a vault accessible by a group, including permission level."""

    id: int
    name: str
    org_id: Optional[int] = None
    permission: str = "read"


class VaultAccessItem(BaseModel):
    """Single vault + permission pair for group vault access updates."""

    vault_id: int
    permission: str = "read"

    @field_validator("permission")
    @classmethod
    def validate_permission(cls, v: str) -> str:
        if v not in ("read", "write", "admin"):
            raise ValueError("permission must be read, write, or admin")
        return v


class GroupVaultsUpdateRequest(BaseModel):
    """Request model for updating group vault access with explicit permissions.

    Accepts either the canonical ``vault_access`` list (preferred) or the
    legacy ``vault_ids`` list (backward compat, defaults to 'read').
    """

    vault_access: Optional[List[VaultAccessItem]] = None
    vault_ids: Optional[List[int]] = None  # legacy; maps to read permission

    def resolved_access(self) -> List[VaultAccessItem]:
        """Return the canonical list of vault+permission pairs."""
        if self.vault_access is not None:
            return self.vault_access
        if self.vault_ids is not None:
            return [VaultAccessItem(vault_id=vid, permission="read") for vid in self.vault_ids]
        return []



@router.get("", response_model=GroupListResponse, include_in_schema=False)
@router.get("/", response_model=GroupListResponse)
async def list_groups(
    page: int = 1,
    per_page: int = 100,
    search: Optional[str] = None,
    user: dict = Depends(require_role("admin")),
    db: sqlite3.Connection = Depends(get_db),
    evaluate: Callable = Depends(get_evaluate_policy),
):
    """
    List all groups (for admin panel use).

    Returns all groups with their associated organization name.
    Requires admin or superadmin role.
    """
    # Policy check for listing groups
    if not await evaluate(user, "group", 0, "list"):
        raise HTTPException(status_code=403, detail="No access to list groups")

    # Cap per_page to prevent memory pressure
    if per_page > 1000:
        per_page = 1000
    # Ensure page is at least 1
    page = max(1, page)
    # Calculate skip from page
    skip = (page - 1) * per_page

    # Non-superadmin users only see groups in their own organization(s)
    user_role = user.get("role", "")
    user_id = user.get("id")
    user_org_ids = []
    if user_role != "superadmin":
        # Get the user's org memberships
        org_cursor = await asyncio.to_thread(
            db.execute,
            "SELECT org_id FROM org_members WHERE user_id = ?",
            (user_id,),
        )
        user_org_rows = await asyncio.to_thread(org_cursor.fetchall)
        user_org_ids = [row[0] for row in user_org_rows]

    # Build WHERE clause for search and org filtering
    where_clause_parts = []
    params = []
    if search:
        # Escape special LIKE characters in search term
        escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where_clause_parts.append("g.name LIKE '%' || ? || '%' ESCAPE '\\'")
        params.append(escaped)
    if user_role != "superadmin":
        if user_org_ids:
            placeholders = ",".join("?" for _ in user_org_ids)
            where_clause_parts.append(f"g.org_id IN ({placeholders})")
            params.extend(user_org_ids)
        else:
            # Non-superadmin with no org memberships sees nothing
            where_clause_parts.append("0=1")
    where_clause = ("WHERE " + " AND ".join(where_clause_parts)) if where_clause_parts else ""

    # Get total count with search filter
    total_query = f"SELECT COUNT(*) FROM groups g {where_clause}"
    total_cursor = await asyncio.to_thread(
        db.execute,
        total_query,
        tuple(params),
    )
    total_row = await asyncio.to_thread(total_cursor.fetchone)
    total = total_row[0]

    # Fetch groups with search filter
    query_params = params + [per_page, skip]
    cursor = await asyncio.to_thread(
        db.execute,
        f"""
        SELECT
            g.id,
            g.org_id,
            g.name,
            g.description,
            g.created_at,
            o.name as organization_name
        FROM groups g
        JOIN organizations o ON g.org_id = o.id
        {where_clause}
        ORDER BY g.name
        LIMIT ? OFFSET ?
        """,
        tuple(query_params),
    )
    rows = await asyncio.to_thread(cursor.fetchall)

    groups = [
        GroupResponse(
            id=row[0],
            org_id=row[1],
            name=row[2],
            description=row[3] or "",
            created_at=row[4],
            organization_name=row[5],
        )
        for row in rows
    ]

    return GroupListResponse(groups=groups, total=total, page=page, per_page=per_page)


@router.post("", response_model=GroupResponse, include_in_schema=False)
@router.post("/", response_model=GroupResponse)
async def create_group(
    request: GroupCreateRequest,
    user: dict = Depends(require_role("admin")),
    db: sqlite3.Connection = Depends(get_db),
    evaluate: Callable = Depends(get_evaluate_policy),
    _csrf_token: str = Depends(csrf_protect),
):
    """
    Create a new group.

    Requires admin role. Group is created in the user's organization.
    Superadmins can create groups in any organization.
    """
    # Policy check for creating groups
    if not await evaluate(user, "group", 0, "create"):
        raise HTTPException(status_code=403, detail="No access to create groups")

    user_role = user.get("role", "")
    user_id = user.get("id")

    # Determine target org_id: use explicit org_id if provided, otherwise use helper
    if request.org_id is not None:
        target_org_id = request.org_id
    else:
        try:
            target_org_id = await get_user_primary_org(user_id, db)
        except MultipleOrgError:
            raise HTTPException(
                status_code=400,
                detail="User belongs to multiple organizations. Please specify org_id explicitly.",
            )

    if not target_org_id:
        raise HTTPException(
            status_code=400,
            detail="User is not associated with an organization",
        )

    # Check that org_id exists
    cursor = await asyncio.to_thread(
        db.execute,
        "SELECT id FROM organizations WHERE id = ?",
        (target_org_id,),
    )
    org_row = await asyncio.to_thread(cursor.fetchone)
    if not org_row:
        raise HTTPException(status_code=404, detail="Organization not found")

    # Verify user is superadmin or member of the target org
    if user_role != "superadmin":
        cursor = await asyncio.to_thread(
            db.execute,
            "SELECT 1 FROM org_members WHERE org_id = ? AND user_id = ?",
            (target_org_id, user_id),
        )
        membership = await asyncio.to_thread(cursor.fetchone)
        if not membership:
            raise HTTPException(
                status_code=403,
                detail="You must be a member of the organization to create groups",
            )

    cursor = await asyncio.to_thread(
        db.execute,
        """
        INSERT INTO groups (org_id, name, description)
        VALUES (?, ?, ?)
        """,
        (target_org_id, request.name, request.description),
    )
    group_id = cursor.lastrowid
    await asyncio.to_thread(db.commit)

    # Fetch the created group with organization name
    cursor = await asyncio.to_thread(
        db.execute,
        """
        SELECT
            g.id,
            g.org_id,
            g.name,
            g.description,
            g.created_at,
            o.name as organization_name
        FROM groups g
        JOIN organizations o ON g.org_id = o.id
        WHERE g.id = ?
        """,
        (group_id,),
    )
    row = await asyncio.to_thread(cursor.fetchone)

    if not row:
        raise HTTPException(status_code=404, detail="Group not found after creation")

    return GroupResponse(
        id=row[0],
        org_id=row[1],
        name=row[2],
        description=row[3] or "",
        created_at=row[4],
        organization_name=row[5],
    )



@router.get("/{group_id}", response_model=GroupResponse)
async def get_group(
    group_id: int,
    user: dict = Depends(require_role("admin")),
    db: sqlite3.Connection = Depends(get_db),
    evaluate: Callable = Depends(get_evaluate_policy),
):
    """
    Get a specific group by ID.

    Requires admin role.
    """
    cursor = await asyncio.to_thread(
        db.execute,
        """
        SELECT
            g.id,
            g.org_id,
            g.name,
            g.description,
            g.created_at,
            o.name as organization_name
        FROM groups g
        JOIN organizations o ON g.org_id = o.id
        WHERE g.id = ?
        """,
        (group_id,),
    )
    row = await asyncio.to_thread(cursor.fetchone)

    if not row:
        raise HTTPException(status_code=404, detail="Group not found")

    if not await evaluate(user, "group", group_id, "read"):
        raise HTTPException(status_code=403, detail="No access to this group")

    return GroupResponse(
        id=row[0],
        org_id=row[1],
        name=row[2],
        description=row[3] or "",
        created_at=row[4],
        organization_name=row[5],
    )


@router.put("/{group_id}", response_model=GroupResponse)
async def update_group(
    group_id: int,
    request: GroupUpdateRequest,
    user: dict = Depends(require_role("admin")),
    db: sqlite3.Connection = Depends(get_db),
    evaluate: Callable = Depends(get_evaluate_policy),
    _csrf_token: str = Depends(csrf_protect),
):
    """
    Update a group.

    Requires admin role. Only updates provided fields.
    User must be a member of the group's organization (or superadmin).
    """
    # Verify group exists and get its org_id
    cursor = await asyncio.to_thread(
        db.execute,
        "SELECT id, org_id FROM groups WHERE id = ?",
        (group_id,),
    )
    group_row = await asyncio.to_thread(cursor.fetchone)
    if not group_row:
        raise HTTPException(status_code=404, detail="Group not found")

    if not await evaluate(user, "group", group_id, "update"):
        raise HTTPException(status_code=403, detail="No access to update this group")

    group_org_id = group_row[1]

    # Verify user is superadmin or member of the group's organization
    user_role = user.get("role", "")
    user_id = user.get("id")
    if user_role != "superadmin":
        cursor = await asyncio.to_thread(
            db.execute,
            "SELECT 1 FROM org_members WHERE org_id = ? AND user_id = ?",
            (group_org_id, user_id),
        )
        membership = await asyncio.to_thread(cursor.fetchone)
        if not membership:
            raise HTTPException(
                status_code=403,
                detail="You must be a member of the organization to modify this group",
            )

    # Build dynamic update query
    updates = []
    params = []

    if request.name is not None:
        updates.append("name = ?")
        params.append(request.name)

    if request.description is not None:
        updates.append("description = ?")
        params.append(request.description)

    if not updates:
        # No updates requested, just return current group
        cursor = await asyncio.to_thread(
            db.execute,
            """
            SELECT
                g.id,
                g.org_id,
                g.name,
                g.description,
                g.created_at,
                o.name as organization_name
            FROM groups g
            JOIN organizations o ON g.org_id = o.id
            WHERE g.id = ?
            """,
            (group_id,),
        )
        row = await asyncio.to_thread(cursor.fetchone)

        if not row:
            raise HTTPException(status_code=404, detail="Group not found")

        return GroupResponse(
            id=row[0],
            org_id=row[1],
            name=row[2],
            description=row[3] or "",
            created_at=row[4],
            organization_name=row[5],
        )

    params.append(group_id)

    cursor = await asyncio.to_thread(
        db.execute,
        f"""
        UPDATE groups
        SET {", ".join(updates)}
        WHERE id = ?
        """,
        tuple(params),
    )

    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="Group not found")

    await asyncio.to_thread(db.commit)

    # Fetch updated group
    cursor = await asyncio.to_thread(
        db.execute,
        """
        SELECT
            g.id,
            g.org_id,
            g.name,
            g.description,
            g.created_at,
            o.name as organization_name
        FROM groups g
        JOIN organizations o ON g.org_id = o.id
        WHERE g.id = ?
        """,
        (group_id,),
    )
    row = await asyncio.to_thread(cursor.fetchone)

    if not row:
        raise HTTPException(status_code=404, detail="Group not found after update")

    return GroupResponse(
        id=row[0],
        org_id=row[1],
        name=row[2],
        description=row[3] or "",
        created_at=row[4],
        organization_name=row[5],
    )


@router.delete("/{group_id}", status_code=204)
async def delete_group(
    group_id: int,
    user: dict = Depends(require_role("admin")),
    db: sqlite3.Connection = Depends(get_db),
    evaluate: Callable = Depends(get_evaluate_policy),
    _csrf_token: str = Depends(csrf_protect),
):
    """
    Delete a group.

    Requires admin role. User must be a member of the group's organization (or superadmin).
    Cascades to delete group_members and vault_group_access entries.
    """
    # Verify group exists and get its org_id
    cursor = await asyncio.to_thread(
        db.execute,
        "SELECT id, org_id FROM groups WHERE id = ?",
        (group_id,),
    )
    group_row = await asyncio.to_thread(cursor.fetchone)
    if not group_row:
        raise HTTPException(status_code=404, detail="Group not found")

    if not await evaluate(user, "group", group_id, "delete"):
        raise HTTPException(status_code=403, detail="No access to delete this group")

    group_org_id = group_row[1]

    # Verify user is superadmin or member of the group's organization
    user_role = user.get("role", "")
    user_id = user.get("id")
    if user_role != "superadmin":
        cursor = await asyncio.to_thread(
            db.execute,
            "SELECT 1 FROM org_members WHERE org_id = ? AND user_id = ?",
            (group_org_id, user_id),
        )
        membership = await asyncio.to_thread(cursor.fetchone)
        if not membership:
            raise HTTPException(
                status_code=403,
                detail="You must be a member of the organization to delete this group",
            )

    # ON DELETE CASCADE handles group_members and vault_group_access cleanup
    cursor = await asyncio.to_thread(
        db.execute,
        "DELETE FROM groups WHERE id = ?",
        (group_id,),
    )

    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="Group not found")

    await asyncio.to_thread(db.commit)


@router.get("/{group_id}/members", response_model=List[GroupMemberResponse])
async def get_group_members(
    group_id: int,
    user: dict = Depends(require_role("admin")),
    db: sqlite3.Connection = Depends(get_db),
    evaluate: Callable = Depends(get_evaluate_policy),
):
    """
    Get all members of a group.

    Returns list of user objects with id, username, and full_name.
    Requires admin role.
    """
    # Verify group exists
    cursor = await asyncio.to_thread(
        db.execute,
        "SELECT id FROM groups WHERE id = ?",
        (group_id,),
    )
    group_row = await asyncio.to_thread(cursor.fetchone)
    if not group_row:
        raise HTTPException(status_code=404, detail="Group not found")

    if not await evaluate(user, "group", group_id, "read"):
        raise HTTPException(status_code=403, detail="No access to this group")

    cursor = await asyncio.to_thread(
        db.execute,
        """
        SELECT u.id, u.username, u.full_name
        FROM users u
        JOIN group_members gm ON u.id = gm.user_id
        WHERE gm.group_id = ?
        ORDER BY u.username
        """,
        (group_id,),
    )
    rows = await asyncio.to_thread(cursor.fetchall)

    return [
        GroupMemberResponse(
            id=row[0],
            username=row[1],
            full_name=row[2] or "",
        )
        for row in rows
    ]


@router.get("/{group_id}/eligible-members", response_model=List[GroupMemberResponse])
async def get_eligible_members(
    group_id: int,
    user: dict = Depends(require_role("admin")),
    db: sqlite3.Connection = Depends(get_db),
    evaluate: Callable = Depends(get_evaluate_policy),
):
    """Return active users who belong to the same org as this group.

    These are the only users who can be validly added as group members.
    """
    cursor = await asyncio.to_thread(
        db.execute, "SELECT id, org_id FROM groups WHERE id = ?", (group_id,)
    )
    group_row = await asyncio.to_thread(cursor.fetchone)
    if not group_row:
        raise HTTPException(status_code=404, detail="Group not found")

    if not await evaluate(user, "group", group_id, "read"):
        raise HTTPException(status_code=403, detail="No access to this group")

    org_id = group_row[1]

    cursor = await asyncio.to_thread(
        db.execute,
        """SELECT u.id, u.username, u.full_name
           FROM users u
           JOIN org_members om ON u.id = om.user_id
           WHERE om.org_id = ? AND u.is_active = 1
           ORDER BY u.username""",
        (org_id,),
    )
    rows = await asyncio.to_thread(cursor.fetchall)

    return [
        GroupMemberResponse(id=row[0], username=row[1], full_name=row[2] or "")
        for row in rows
    ]


@router.put("/{group_id}/members", response_model=List[GroupMemberResponse])
async def update_group_members(
    group_id: int,
    request: GroupMembersUpdateRequest,
    user: dict = Depends(require_role("admin")),
    db: sqlite3.Connection = Depends(get_db),
    evaluate: Callable = Depends(get_evaluate_policy),
    _csrf_token: str = Depends(csrf_protect),
):
    """
    Update a group's members (replaces all existing members).

    Body: {user_ids: number[]} - replaces all members
    Requires admin role.
    """
    # Verify group exists and get its org_id
    cursor = await asyncio.to_thread(
        db.execute,
        "SELECT id, org_id FROM groups WHERE id = ?",
        (group_id,),
    )
    group_row = await asyncio.to_thread(cursor.fetchone)
    if not group_row:
        raise HTTPException(status_code=404, detail="Group not found")

    if not await evaluate(user, "group", group_id, "update"):
        raise HTTPException(status_code=403, detail="No access to update this group")

    group_org_id = group_row[1]

    # Verify user is superadmin or member of the group's organization
    user_role = user.get("role", "")
    user_id = user.get("id")
    if user_role != "superadmin":
        cursor = await asyncio.to_thread(
            db.execute,
            "SELECT 1 FROM org_members WHERE org_id = ? AND user_id = ?",
            (group_org_id, user_id),
        )
        membership = await asyncio.to_thread(cursor.fetchone)
        if not membership:
            raise HTTPException(
                status_code=403,
                detail="You must be a member of the organization to modify this group",
            )

    # Verify all user_ids exist and are members of the group's organization
    if request.user_ids:
        placeholders = ",".join(["?"] * len(request.user_ids))
        cursor = await asyncio.to_thread(
            db.execute,
            f"SELECT id FROM users WHERE id IN ({placeholders})",
            tuple(request.user_ids),
        )
        found_users = await asyncio.to_thread(cursor.fetchall)
        found_ids = {row[0] for row in found_users}
        missing_ids = set(request.user_ids) - found_ids
        if missing_ids:
            raise HTTPException(
                status_code=400,
                detail=f"Users not found: {sorted(missing_ids)}",
            )
        # Check that all users are members of the group's organization
        cursor = await asyncio.to_thread(
            db.execute,
            f"SELECT user_id FROM org_members WHERE org_id = ? AND user_id IN ({placeholders})",
            (group_org_id,) + tuple(request.user_ids),
        )
        org_members = await asyncio.to_thread(cursor.fetchall)
        org_member_ids = {row[0] for row in org_members}
        non_member_ids = set(request.user_ids) - org_member_ids
        if non_member_ids:
            raise HTTPException(
                status_code=400,
                detail=f"Users are not members of this organization: {sorted(non_member_ids)}",
            )

    # Atomic update: delete existing and insert new in transaction
    def _update_members():
        with transaction_context(db):
            # Delete existing memberships
            db.execute(
                "DELETE FROM group_members WHERE group_id = ?",
                (group_id,),
            )
            # Insert new memberships (deduplicated)
            for user_id in set(request.user_ids):
                db.execute(
                    "INSERT INTO group_members (group_id, user_id) VALUES (?, ?)",
                    (group_id, user_id),
                )

    await asyncio.to_thread(_update_members)

    # Fetch and return updated members
    cursor = await asyncio.to_thread(
        db.execute,
        """
        SELECT u.id, u.username, u.full_name
        FROM users u
        JOIN group_members gm ON u.id = gm.user_id
        WHERE gm.group_id = ?
        ORDER BY u.username
        """,
        (group_id,),
    )
    rows = await asyncio.to_thread(cursor.fetchall)

    return [
        GroupMemberResponse(
            id=row[0],
            username=row[1],
            full_name=row[2] or "",
        )
        for row in rows
    ]


@router.get("/{group_id}/vaults", response_model=List[GroupVaultResponse])
async def get_group_vaults(
    group_id: int,
    user: dict = Depends(require_role("admin")),
    db: sqlite3.Connection = Depends(get_db),
    evaluate: Callable = Depends(get_evaluate_policy),
):
    """
    Get all vaults accessible by a group.

    Returns list of vault objects with id and name.
    Requires admin role.
    """
    # Verify group exists
    cursor = await asyncio.to_thread(
        db.execute,
        "SELECT id FROM groups WHERE id = ?",
        (group_id,),
    )
    group_row = await asyncio.to_thread(cursor.fetchone)
    if not group_row:
        raise HTTPException(status_code=404, detail="Group not found")

    if not await evaluate(user, "group", group_id, "read"):
        raise HTTPException(status_code=403, detail="No access to this group")

    cursor = await asyncio.to_thread(
        db.execute,
        """
        SELECT v.id, v.name, v.org_id, COALESCE(vga.permission, 'read')
        FROM vaults v
        JOIN vault_group_access vga ON v.id = vga.vault_id
        WHERE vga.group_id = ?
        ORDER BY v.name
        """,
        (group_id,),
    )
    rows = await asyncio.to_thread(cursor.fetchall)

    return [
        GroupVaultResponse(
            id=row[0],
            name=row[1],
            org_id=row[2],
            permission=row[3],
        )
        for row in rows
    ]


@router.put("/{group_id}/vaults", response_model=List[GroupVaultResponse])
async def update_group_vaults(
    group_id: int,
    request: GroupVaultsUpdateRequest,
    user: dict = Depends(require_role("admin")),
    db: sqlite3.Connection = Depends(get_db),
    evaluate: Callable = Depends(get_evaluate_policy),
    _csrf_token: str = Depends(csrf_protect),
):
    """
    Update a group's vault access (replaces all existing access).

    Accepts ``{vault_access: [{vault_id, permission}]}`` (preferred) or the
    legacy ``{vault_ids: [number]}`` (maps to 'read' permission).
    Requires admin role.
    """
    # Verify group exists and get its org_id
    cursor = await asyncio.to_thread(
        db.execute,
        "SELECT id, org_id FROM groups WHERE id = ?",
        (group_id,),
    )
    group_row = await asyncio.to_thread(cursor.fetchone)
    if not group_row:
        raise HTTPException(status_code=404, detail="Group not found")

    if not await evaluate(user, "group", group_id, "update"):
        raise HTTPException(status_code=403, detail="No access to update this group")

    group_org_id = group_row[1]

    # Verify user is superadmin or member of the group's organization
    user_role = user.get("role", "")
    user_id = user.get("id")
    if user_role != "superadmin":
        cursor = await asyncio.to_thread(
            db.execute,
            "SELECT 1 FROM org_members WHERE org_id = ? AND user_id = ?",
            (group_org_id, user_id),
        )
        membership = await asyncio.to_thread(cursor.fetchone)
        if not membership:
            raise HTTPException(
                status_code=403,
                detail="You must be a member of the organization to modify this group",
            )

    # Resolve canonical access list (handles both vault_access and legacy vault_ids)
    access_list = request.resolved_access()

    # Verify all vaults exist and belong to the same organization as the group
    if access_list:
        vault_ids = [item.vault_id for item in access_list]
        placeholders = ",".join(["?"] * len(vault_ids))
        cursor = await asyncio.to_thread(
            db.execute,
            f"SELECT id, org_id FROM vaults WHERE id IN ({placeholders})",
            tuple(vault_ids),
        )
        found_vaults = await asyncio.to_thread(cursor.fetchall)
        found_ids = {row[0] for row in found_vaults}
        missing_ids = set(vault_ids) - found_ids
        if missing_ids:
            raise HTTPException(
                status_code=400,
                detail=f"Vaults not found: {sorted(missing_ids)}",
            )
        # A vault with org_id IS NULL is treated as global (unscoped) and allowed.
        # Only reject vaults that explicitly belong to a *different* organization.
        cross_org_vaults = [
            row[0]
            for row in found_vaults
            if row[1] is not None and row[1] != group_org_id
        ]
        if cross_org_vaults:
            raise HTTPException(
                status_code=400,
                detail=f"Vaults do not belong to this organization: {sorted(cross_org_vaults)}",
            )

    # Atomic update: delete existing and insert new in transaction
    def _update_vaults():
        with transaction_context(db):
            db.execute(
                "DELETE FROM vault_group_access WHERE group_id = ?",
                (group_id,),
            )
            seen: set[int] = set()
            for item in access_list:
                if item.vault_id not in seen:
                    seen.add(item.vault_id)
                    db.execute(
                        "INSERT INTO vault_group_access (vault_id, group_id, permission) VALUES (?, ?, ?)",
                        (item.vault_id, group_id, item.permission),
                    )

    await asyncio.to_thread(_update_vaults)

    # Fetch and return updated vaults with permissions
    cursor = await asyncio.to_thread(
        db.execute,
        """
        SELECT v.id, v.name, v.org_id, COALESCE(vga.permission, 'read')
        FROM vaults v
        JOIN vault_group_access vga ON v.id = vga.vault_id
        WHERE vga.group_id = ?
        ORDER BY v.name
        """,
        (group_id,),
    )
    rows = await asyncio.to_thread(cursor.fetchall)

    return [
        GroupVaultResponse(
            id=row[0],
            name=row[1],
            org_id=row[2],
            permission=row[3],
        )
        for row in rows
    ]
