import sqlite3
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from app.api.deps import (
    get_current_active_user,
    get_db,
    get_evaluate_policy,
    require_vault_permission,
    resolve_granted_by,
)
from app.models.database import set_migration_flag, transaction_context
from app.security import csrf_protect
from app.services.authz_policy import (
    direct_vault_admin_ids,
    group_derived_vault_admin_ids,
)

router = APIRouter(prefix="/vaults/{vault_id}/members", tags=["vault-members"])
group_access_router = APIRouter(
    prefix="/vaults/{vault_id}/group-access", tags=["vault-group-access"]
)

VALID_PERMISSIONS = ("read", "write", "admin")


def _effective_admin_post_state(
    conn: sqlite3.Connection,
    vault_id: int,
    *,
    exclude_direct_user_id: int | None = None,
    exclude_group_id: int | None = None,
) -> set[int]:
    """Compute the post-mutation effective-admin set for a vault (issue #1025).

    The shared inventory union (direct admins plus group-derived admins) with
    the mutation's exclusions applied: a demoted/removed member drops out of
    the direct layer only (their group grant survives), a demoted/revoked group
    grant drops out via ``exclude_group_id``.
    """
    return (
        direct_vault_admin_ids(conn, vault_id, exclude_user_id=exclude_direct_user_id)
        | group_derived_vault_admin_ids(
            conn, vault_id, exclude_group_id=exclude_group_id
        )
    )


def _refuse_if_last_effective_admin(
    conn: sqlite3.Connection,
    vault_id: int,
    post_admins: set[int],
    action: str,
) -> None:
    """Refuse a mutation whose post-state has zero effective admins.

    Only refused when the vault HAD effective admins before the mutation
    (pre-state non-empty): an already-broken vault stays repairable — every
    guard-raising mutation on it is one that does not further reduce the set.
    """
    if post_admins:
        return
    pre_admins = direct_vault_admin_ids(conn, vault_id) | group_derived_vault_admin_ids(
        conn, vault_id
    )
    if not pre_admins:
        return
    raise HTTPException(
        status_code=409,
        detail=(
            f"Cannot {action} the last effective admin: a vault must keep at"
            " least one effective admin"
        ),
    )


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


@router.get("", include_in_schema=False)
@router.get("/")
def list_vault_members(
    vault_id: int,
    conn: sqlite3.Connection = Depends(get_db),
    current_user: dict = Depends(require_vault_permission("read")),
):
    """List all members of a vault."""
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


@router.post("", include_in_schema=False)
@router.post("/")
def add_vault_member(
    vault_id: int,
    request: VaultMemberCreateRequest,
    conn: sqlite3.Connection = Depends(get_db),
    current_user: dict = Depends(require_vault_permission("admin")),
    _csrf_token: str = Depends(csrf_protect),
):
    """Add a member to a vault."""
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

    # Insert new member. granted_by is FK-safe: the single-admin sentinel (id
    # 0, no users row) records NULL provenance instead of tripping the FK and
    # surfacing as a misleading duplicate-member 409 (issue #1025, PRR-001).
    granted_by = resolve_granted_by(current_user)
    try:
        cursor.execute(
            "INSERT INTO vault_members (vault_id, user_id, permission, granted_by) VALUES (?, ?, ?, ?)",
            (vault_id, request.member_user_id, request.permission, granted_by),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        raise HTTPException(
            status_code=409, detail="User is already a member of this vault"
        )

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
    if row is None:
        # Post-commit re-read guard: a concurrent removal of the just-inserted
        # row must answer 404, never an untyped TypeError 500 (issue #726,
        # T1-01-S2-08).
        raise HTTPException(status_code=404, detail="Member not found")

    return {
        "user_id": row[0],
        "username": row[1],
        "full_name": row[2] if row[2] else "",
        "permission": row[3],
        "granted_at": row[4],
        "granted_by": row[5],
    }


@router.patch("/{member_user_id}")
def update_vault_member(
    vault_id: int,
    member_user_id: int,
    request: VaultMemberUpdateRequest,
    conn: sqlite3.Connection = Depends(get_db),
    current_user: dict = Depends(require_vault_permission("admin")),
    _csrf_token: str = Depends(csrf_protect),
):
    """Update a vault member's permission."""
    cursor = conn.cursor()

    # Verify vault exists
    cursor.execute("SELECT id FROM vaults WHERE id = ?", (vault_id,))
    if not cursor.fetchone():
        raise HTTPException(status_code=404, detail="Vault not found")

    granted_by = resolve_granted_by(current_user)
    # BEGIN IMMEDIATE from the existence read through the UPDATE: a deferred
    # read-then-write let two connections both observe the last admin before
    # either demoted them, leaving the vault with zero admins (issue #726,
    # T1-01-S-06; same atomicity the users.py last-superadmin guard uses).
    with transaction_context(conn):
        # Check member exists (and capture its permission for the guard)
        cursor.execute(
            "SELECT permission FROM vault_members WHERE vault_id = ? AND user_id = ?",
            (vault_id, member_user_id),
        )
        row = cursor.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Member not found")

        # Last-effective-admin guard: demoting this member's direct admin row
        # leaves the vault manageable when any OTHER effective admin remains —
        # another direct admin, or a group grant conferring admin on anyone
        # (issue #1025, PRR-010; the demoted member's own group grant also
        # keeps them admin, so a same-user dual-admin demote is allowed).
        if row[0] == "admin" and request.permission != "admin":
            post_admins = _effective_admin_post_state(
                conn, vault_id, exclude_direct_user_id=member_user_id
            )
            _refuse_if_last_effective_admin(conn, vault_id, post_admins, "demote")

        cursor.execute(
            "UPDATE vault_members SET permission = ?, granted_by = ? WHERE vault_id = ? AND user_id = ?",
            (request.permission, granted_by, vault_id, member_user_id),
        )
        if cursor.rowcount == 0:
            # The member vanished between the guarded read and the UPDATE
            # (concurrent removal): answer 404, never report success for an
            # unapplied mutation (issue #726, T1-01-S2-08).
            raise HTTPException(status_code=404, detail="Member not found")

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
    if row is None:
        raise HTTPException(status_code=404, detail="Member not found")

    return {
        "user_id": row[0],
        "username": row[1],
        "full_name": row[2] if row[2] else "",
        "permission": row[3],
        "granted_at": row[4],
        "granted_by": row[5],
    }


@router.delete("/{member_user_id}")
def remove_vault_member(
    vault_id: int,
    member_user_id: int,
    conn: sqlite3.Connection = Depends(get_db),
    current_user: dict = Depends(require_vault_permission("admin")),
    _csrf_token: str = Depends(csrf_protect),
):
    """Remove a member from a vault."""
    cursor = conn.cursor()

    # Verify vault exists
    cursor.execute("SELECT id FROM vaults WHERE id = ?", (vault_id,))
    if not cursor.fetchone():
        raise HTTPException(status_code=404, detail="Vault not found")

    # Guard against self-removal
    if member_user_id == current_user.get("id"):
        raise HTTPException(
            status_code=400, detail="Cannot remove yourself from a vault"
        )

    # BEGIN IMMEDIATE through the DELETE, mirroring update_vault_member: the
    # last-admin check and the removal must be one atomic step (issue #726,
    # T1-01-S-06 constraint — no combination of update/remove calls may leave
    # a vault with zero admin members).
    try:
        with transaction_context(conn):
            cursor.execute(
                "SELECT permission FROM vault_members WHERE vault_id = ? AND user_id = ?",
                (vault_id, member_user_id),
            )
            row = cursor.fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="Member not found")

            if row[0] == "admin":
                # Last-effective-admin guard over the shared inventory: the
                # removed member drops out of the DIRECT layer only — their
                # group-derived admin (if any) survives the removal, and any
                # other direct or group-derived admin keeps the vault
                # manageable (issue #1025, PRR-010).
                post_admins = _effective_admin_post_state(
                    conn, vault_id, exclude_direct_user_id=member_user_id
                )
                _refuse_if_last_effective_admin(conn, vault_id, post_admins, "remove")

            cursor.execute(
                "DELETE FROM vault_members WHERE vault_id = ? AND user_id = ?",
                (vault_id, member_user_id),
            )
            if cursor.rowcount == 0:
                # Racing removal already deleted the row: 404, not success.
                raise HTTPException(status_code=404, detail="Member not found")
            # Durable-removal marker (issue #690 / S03-SK-01): written in the same
            # transaction as the DELETE so the startup orphan backfill can never
            # re-grant a membership an admin explicitly removed. User-scoped: any
            # removed membership blocks the Default-vault backfill for that user.
            set_migration_flag(conn, f"vault_members.removed.user.{member_user_id}")
    except sqlite3.IntegrityError:
        conn.rollback()
        raise HTTPException(status_code=409, detail="Member is referenced elsewhere")

    return {
        "message": "Member removed",
        "vault_id": vault_id,
        "user_id": member_user_id,
    }


# =============================================================================
# Vault Group Access Routes
# =============================================================================


@group_access_router.get("", include_in_schema=False)
@group_access_router.get("/")
async def list_vault_group_access(
    vault_id: int,
    conn: sqlite3.Connection = Depends(get_db),
    current_user: dict = Depends(get_current_active_user),
    evaluate: Callable = Depends(get_evaluate_policy),
):
    """List all groups with access to a vault."""
    cursor = conn.cursor()

    # Verify vault exists
    cursor.execute("SELECT id FROM vaults WHERE id = ?", (vault_id,))
    if not cursor.fetchone():
        raise HTTPException(status_code=404, detail="Vault not found")

    if not await evaluate(current_user, "vault", vault_id, "admin"):
        raise HTTPException(
            status_code=403, detail="Insufficient vault permissions"
        )

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


@group_access_router.post("", include_in_schema=False)
@group_access_router.post("/")
def grant_vault_group_access(
    vault_id: int,
    request: VaultGroupAccessCreateRequest,
    conn: sqlite3.Connection = Depends(get_db),
    current_user: dict = Depends(require_vault_permission("admin")),
    _csrf_token: str = Depends(csrf_protect),
):
    """Grant a group access to a vault."""
    cursor = conn.cursor()

    # granted_by is FK-safe: the single-admin sentinel (id 0, no users row)
    # records NULL provenance instead of tripping the granted_by FK and
    # surfacing as a misleading duplicate-grant 409 (issue #1025, PRR-001).
    granted_by = resolve_granted_by(current_user)
    # One BEGIN IMMEDIATE over the whole read-validate-write: the vault-org
    # read, duplicate check, and INSERT are atomic against a concurrent
    # adoption of this vault into an organization (#1003) and against a racing
    # duplicate grant. HTTPExceptions roll back through transaction_context;
    # only the IntegrityError -> 409 translation stays outside.
    try:
        with transaction_context(conn):
            # Verify vault exists and get org_id
            cursor.execute("SELECT id, org_id FROM vaults WHERE id = ?", (vault_id,))
            vault_row = cursor.fetchone()
            if not vault_row:
                raise HTTPException(status_code=404, detail="Vault not found")
            vault_org_id = vault_row[1]

            # Verify target group exists and get org_id
            cursor.execute(
                "SELECT id, org_id FROM groups WHERE id = ?", (request.group_id,)
            )
            group_row = cursor.fetchone()
            if not group_row:
                raise HTTPException(status_code=404, detail="Group not found")
            group_org_id = group_row[1]

            # Check not already granted
            cursor.execute(
                "SELECT group_id FROM vault_group_access"
                " WHERE vault_id = ? AND group_id = ?",
                (vault_id, request.group_id),
            )
            if cursor.fetchone():
                raise HTTPException(
                    status_code=409, detail="Group already has access to this vault"
                )

            # Validate group and vault belong to the same org (NULL-safe
            # comparison): only reject if both have non-NULL org_ids that
            # differ — an org-less (global) vault accepts groups from any
            # organization (pinned policy, issue #721 AC9).
            if (
                group_org_id is not None
                and vault_org_id is not None
                and group_org_id != vault_org_id
            ):
                raise HTTPException(
                    status_code=400,
                    detail="Group belongs to a different organization than this vault",
                )

            cursor.execute(
                "INSERT INTO vault_group_access (vault_id, group_id, permission, granted_by) VALUES (?, ?, ?, ?)",
                (vault_id, request.group_id, request.permission, granted_by),
            )
    except sqlite3.IntegrityError:
        raise HTTPException(
            status_code=409, detail="Group already has access to this vault"
        )

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
    if row is None:
        # Post-commit re-read guard (issue #726, T1-01-S2-08): a racing revoke
        # of the just-granted row answers 404, never a TypeError 500.
        raise HTTPException(status_code=404, detail="Group access not found")

    return {
        "group_id": row[0],
        "group_name": row[1],
        "org_name": row[2],
        "permission": row[3],
        "granted_at": row[4],
        "granted_by": row[5],
    }


@group_access_router.patch("/{group_id}")
def update_vault_group_access(
    vault_id: int,
    group_id: int,
    request: VaultGroupAccessUpdateRequest,
    conn: sqlite3.Connection = Depends(get_db),
    current_user: dict = Depends(require_vault_permission("admin")),
    _csrf_token: str = Depends(csrf_protect),
):
    """Update a group's permission for a vault."""
    cursor = conn.cursor()

    # granted_by is FK-safe for the single-admin sentinel (issue #1025,
    # PRR-001). One BEGIN IMMEDIATE over the existence read, the guard, and
    # the UPDATE: this route previously had no transaction at all, so a
    # concurrent revoke (or member demote) could interleave between the read
    # and the write and strand the vault with zero effective admins.
    granted_by = resolve_granted_by(current_user)
    with transaction_context(conn):
        # Verify vault exists
        cursor.execute("SELECT id FROM vaults WHERE id = ?", (vault_id,))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="Vault not found")

        # Check group access exists (capture its permission for the guard)
        cursor.execute(
            "SELECT permission FROM vault_group_access"
            " WHERE vault_id = ? AND group_id = ?",
            (vault_id, group_id),
        )
        row = cursor.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Group access not found")

        # Last-effective-admin guard: demoting this grant's admin
        # permission leaves the vault manageable when any other effective
        # admin remains — a direct admin, or another group's admin grant
        # (issue #1025, PRR-010). This grant's contribution is excluded
        # from the post-state; refusal only fires when the pre-state was
        # non-empty, so an already-broken vault stays repairable.
        if row[0] == "admin" and request.permission != "admin":
            post_admins = _effective_admin_post_state(
                conn, vault_id, exclude_group_id=group_id
            )
            _refuse_if_last_effective_admin(conn, vault_id, post_admins, "demote")

        cursor.execute(
            "UPDATE vault_group_access SET permission = ?, granted_by = ?"
            " WHERE vault_id = ? AND group_id = ?",
            (request.permission, granted_by, vault_id, group_id),
        )
        if cursor.rowcount == 0:
            # The row vanished between the guarded read and the UPDATE
            # (concurrent revoke): 404, never success-for-nothing (issue
            # #726, T1-01-S2-08).
            raise HTTPException(status_code=404, detail="Group access not found")

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
    if row is None:
        # Post-commit re-read guard (issue #726, T1-01-S2-08).
        raise HTTPException(status_code=404, detail="Group access not found")

    return {
        "group_id": row[0],
        "group_name": row[1],
        "org_name": row[2],
        "permission": row[3],
        "granted_at": row[4],
        "granted_by": row[5],
    }


@group_access_router.delete("/{group_id}")
def revoke_vault_group_access(
    vault_id: int,
    group_id: int,
    conn: sqlite3.Connection = Depends(get_db),
    current_user: dict = Depends(require_vault_permission("admin")),
    _csrf_token: str = Depends(csrf_protect),
):
    """Remove a group's access from a vault."""
    cursor = conn.cursor()

    # One BEGIN IMMEDIATE over the existence read, the guard, and the DELETE:
    # like update_vault_group_access, this route previously had no
    # transaction, so the revocation could strand a vault with zero effective
    # admins when it removed the last one (issue #1025, PRR-010).
    with transaction_context(conn):
        # Verify vault exists
        cursor.execute("SELECT id FROM vaults WHERE id = ?", (vault_id,))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="Vault not found")

        # Check group access exists (capture its permission for the guard)
        cursor.execute(
            "SELECT permission FROM vault_group_access"
            " WHERE vault_id = ? AND group_id = ?",
            (vault_id, group_id),
        )
        row = cursor.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Group access not found")

        # Last-effective-admin guard: this grant's contribution is excluded
        # from the post-state entirely; refusal only fires when the vault had
        # effective admins (direct or via another group) beforehand.
        if row[0] == "admin":
            post_admins = _effective_admin_post_state(
                conn, vault_id, exclude_group_id=group_id
            )
            _refuse_if_last_effective_admin(conn, vault_id, post_admins, "revoke")

        cursor.execute(
            "DELETE FROM vault_group_access WHERE vault_id = ? AND group_id = ?",
            (vault_id, group_id),
        )
        if cursor.rowcount == 0:
            # Racing revoke already removed the row: 404, not success.
            raise HTTPException(status_code=404, detail="Group access not found")

    return {
        "message": "Group access revoked",
        "vault_id": vault_id,
        "group_id": group_id,
    }
