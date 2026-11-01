"""Groups management routes (admin panel)."""

import asyncio
import sqlite3
from collections.abc import Callable
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.api.deps import (
    MultipleOrgError,
    get_db,
    get_evaluate_policy,
    get_user_primary_org,
    require_role,
    resolve_granted_by,
)
from app.models.database import transaction_context
from app.security import csrf_protect
from app.services.authz_policy import (
    direct_vault_admin_ids,
    group_derived_vault_admin_ids,
)

router = APIRouter(prefix="/groups", tags=["groups"])


def _covered_vault_ids(db: sqlite3.Connection, vault_ids: set) -> set:
    """Return the candidate vaults that still have an effective admin (#1028).

    Route-layer composition of the shared effective-admin inventory
    (``authz_policy.direct_vault_admin_ids`` /
    ``authz_policy.group_derived_vault_admin_ids``); the inventory functions
    themselves stay untouched per #1028's scope. Callers run this inside the
    same BEGIN IMMEDIATE transaction as the mutation (both the pre-state and
    the ACTUAL post-state read).
    """
    return {
        vault_id
        for vault_id in vault_ids
        if direct_vault_admin_ids(db, vault_id)
        | group_derived_vault_admin_ids(db, vault_id)
    }


def _group_admin_grant_vault_ids(db: sqlite3.Connection, group_id: int) -> set:
    """Vaults whose group-derived admin coverage runs through this group (#1028).

    Only admin ``vault_group_access`` rows can contribute effective admins, so
    read/write grants are not candidates.
    """
    return {
        row[0]
        for row in db.execute(
            "SELECT DISTINCT vault_id FROM vault_group_access"
            " WHERE group_id = ? AND permission = 'admin'",
            (group_id,),
        )
    }


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


def _validate_group_name(v: Optional[str]) -> Optional[str]:
    """Enforce the UI's exact name contract: trimmed length >= 2 AND raw
    length <= 100 (GroupFormDialog checks trim().length < 2 then
    .length > 100). Validation-only: the stored value is never rewritten
    (issue #726, R1-G5b-02 — API and UI reject and accept identical inputs
    for BMP text; lengths are Python code points vs the UI's UTF-16 units,
    so names containing astral characters (e.g. emoji) can diverge by up to
    1 UTF-16 unit per astral char).
    """
    if v is None:
        return v
    if len(v.strip()) < 2:
        raise ValueError("Group name must be at least 2 characters (after trimming whitespace)")
    if len(v) > 100:
        raise ValueError("Group name must be at most 100 characters")
    return v


def _validate_group_description(v: Optional[str]) -> Optional[str]:
    """UI parity: description capped at 500 raw characters."""
    if v is None:
        return v
    if len(v) > 500:
        raise ValueError("Group description must be at most 500 characters")
    return v


class GroupCreateRequest(BaseModel):
    """Request model for creating a group."""

    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = Field(default=None, max_length=2000)
    org_id: Optional[int] = None

    _name_check = field_validator("name")(_validate_group_name)
    _description_check = field_validator("description")(_validate_group_description)


class GroupUpdateRequest(BaseModel):
    """Request model for updating a group.

    None means "leave unchanged"; a provided name/description is validated
    with the same bounds as creation (an empty-after-trim rename is rejected).
    """

    name: Optional[str] = Field(default=None, max_length=255)
    description: Optional[str] = Field(default=None, max_length=2000)

    _name_check = field_validator("name")(_validate_group_name)
    _description_check = field_validator("description")(_validate_group_description)


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

    def any_field_set(self) -> bool:
        """True when at least one replace-set field was explicitly provided.

        Omitted/null fields are rejected upstream (422) so an empty-body PUT
        can never clear every vault grant for the group (issue #725 census,
        same family as the vault-groups and user-organizations routes).
        """
        return self.vault_access is not None or self.vault_ids is not None

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
    per_page: int = Query(100, ge=1),
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
    def _delete_group():
        # Issue #1028: one BEGIN IMMEDIATE over candidate discovery, the
        # pre-state read, the cascade DELETE, and the ACTUAL post-state
        # re-read. The FK cascade deletes the group's admin grants WITH the
        # row, so a vault whose only effective admin was group-derived through
        # this group would otherwise be silently orphaned. The refusal fires
        # only for vaults that HAD effective admins (pre-state-non-empty), so
        # an already-broken vault stays repairable (issue AC7).
        with transaction_context(db):
            candidate_vault_ids = _group_admin_grant_vault_ids(db, group_id)
            covered_before = _covered_vault_ids(db, candidate_vault_ids)
            delete_cursor = db.execute(
                "DELETE FROM groups WHERE id = ?",
                (group_id,),
            )
            if delete_cursor.rowcount == 0:
                raise HTTPException(status_code=404, detail="Group not found")
            # ACTUAL post-state re-read over the persisted rows, before commit.
            refused_vault_ids = sorted(
                covered_before - _covered_vault_ids(db, candidate_vault_ids)
            )
            if refused_vault_ids:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Deleting this group would leave vault(s)"
                        f" {refused_vault_ids} without an effective admin: each"
                        " vault must keep at least one effective admin"
                    ),
                )

    await asyncio.to_thread(_delete_group)


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

    # Atomic update: delete existing and insert new in transaction. The
    # user-existence and org-membership preconditions run INSIDE the
    # transaction so a concurrent org removal cannot commit between the
    # check and the INSERT and leave an orphaned group_members row
    # (issue #721 claim 7); with the removal-side cleanups (organizations.py
    # remove_org_member, users.py update_user_organizations) SQLite's
    # one-writer serialization makes every interleaving converge.
    def _update_members():
        with transaction_context(db):
            if request.user_ids:
                placeholders = ",".join(["?"] * len(request.user_ids))
                found_users = db.execute(
                    f"SELECT id FROM users WHERE id IN ({placeholders})",
                    tuple(request.user_ids),
                ).fetchall()
                found_ids = {row[0] for row in found_users}
                missing_ids = set(request.user_ids) - found_ids
                if missing_ids:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Users not found: {sorted(missing_ids)}",
                    )
                # All users must be members of the group's organization
                org_members = db.execute(
                    f"SELECT user_id FROM org_members WHERE org_id = ? AND user_id IN ({placeholders})",
                    (group_org_id,) + tuple(request.user_ids),
                ).fetchall()
                org_member_ids = {row[0] for row in org_members}
                non_member_ids = set(request.user_ids) - org_member_ids
                if non_member_ids:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Users are not members of this organization: {sorted(non_member_ids)}",
                    )
            # Issue #1028: candidate discovery and the pre-state read inside
            # the same BEGIN IMMEDIATE as the replacement — removing a member
            # deletes the rows that realize this group's admin grants.
            candidate_vault_ids = _group_admin_grant_vault_ids(db, group_id)
            covered_before = _covered_vault_ids(db, candidate_vault_ids)
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
            # ACTUAL post-state re-read before commit; refusal only for vaults
            # that HAD effective admins (AC7 keeps pre-broken vaults
            # repairable), and any refused vault rolls back the whole
            # replacement.
            refused_vault_ids = sorted(
                covered_before - _covered_vault_ids(db, candidate_vault_ids)
            )
            if refused_vault_ids:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Replacing this group's members would leave vault(s)"
                        f" {refused_vault_ids} without an effective admin: each"
                        " vault must keep at least one effective admin"
                    ),
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

    # Omitted (or null) list fields are NOT an explicit clear: reject with 422
    # before any DELETE or audit event runs (issue #725 census — same family
    # as the vault-groups and user-organizations replace routes).
    if not request.any_field_set():
        raise HTTPException(
            status_code=422,
            detail=(
                "Provide at least one of 'vault_access' or 'vault_ids' "
                "(an explicit empty list clears all vault access for this group)"
            ),
        )

    # Resolve canonical access list (handles both vault_access and legacy vault_ids)
    access_list = request.resolved_access()

    # granted_by is FK-safe: the single-admin sentinel (id 0, no users row)
    # records NULL provenance instead of tripping the granted_by FK (issue
    # #1025, PRR-001; at base commit this INSERT loop answered 500 in
    # single-admin mode).
    granted_by = resolve_granted_by(user)

    # Atomic update: the existing transaction wrapper, expanded to cover
    # validation and the per-vault effective-admin guard
    def _update_vaults():
        # One BEGIN IMMEDIATE over validation, the per-vault pre-state reads,
        # DELETE, INSERTs, and the per-vault ACTUAL post-state re-reads. The
        # vault-org validation must not race a vault adoption (#1003), and
        # the last-effective-admin guard must observe persisted rows for
        # EVERY vault in the old-union-new grant set — one vault's preserved
        # admin must never mask another vault's zeroing, so ANY refused vault
        # rolls the whole replacement back (issue #1025, PRR-010).
        with transaction_context(db):
            if access_list:
                vault_ids = [item.vault_id for item in access_list]
                placeholders = ",".join(["?"] * len(vault_ids))
                found_vaults = db.execute(
                    f"SELECT id, org_id FROM vaults WHERE id IN ({placeholders})",
                    tuple(vault_ids),
                ).fetchall()
                found_ids = {row[0] for row in found_vaults}
                missing_ids = set(vault_ids) - found_ids
                if missing_ids:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Vaults not found: {sorted(missing_ids)}",
                    )
                # A vault with org_id IS NULL is treated as global (unscoped)
                # and allowed. Only reject vaults that explicitly belong to a
                # *different* organization.
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

            # Every vault whose group-derived coverage this replacement can
            # change: the old grant set union the incoming one.
            affected_vault_ids = {
                row[0]
                for row in db.execute(
                    "SELECT vault_id FROM vault_group_access WHERE group_id = ?",
                    (group_id,),
                )
            } | {item.vault_id for item in access_list}
            pre_nonempty_vault_ids = {
                vid
                for vid in affected_vault_ids
                if direct_vault_admin_ids(db, vid)
                | group_derived_vault_admin_ids(db, vid)
            }

            db.execute(
                "DELETE FROM vault_group_access WHERE group_id = ?",
                (group_id,),
            )
            seen: set[int] = set()
            # Insert new memberships (deduplicated). granted_by records the
            # acting caller, identical to the vault-side grant writers
            # (vaults.py, vault_members.py) — issue #721 AC6.
            for item in access_list:
                if item.vault_id not in seen:
                    seen.add(item.vault_id)
                    db.execute(
                        "INSERT INTO vault_group_access (vault_id, group_id, permission, granted_by) VALUES (?, ?, ?, ?)",
                        (item.vault_id, group_id, item.permission, granted_by),
                    )

            # ACTUAL per-vault post-state re-read over the persisted rows,
            # before commit. Refusal only fires for vaults that HAD effective
            # admins, so an already-broken vault stays repairable.
            refused_vault_ids = sorted(
                vid
                for vid in pre_nonempty_vault_ids
                if not (
                    direct_vault_admin_ids(db, vid)
                    | group_derived_vault_admin_ids(db, vid)
                )
            )
            if refused_vault_ids:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Replacing vault access would leave vault(s)"
                        f" {refused_vault_ids} without an effective admin: each"
                        " vault must keep at least one effective admin"
                    ),
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
