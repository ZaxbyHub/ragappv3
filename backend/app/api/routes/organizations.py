"""Organization CRUD and member management routes."""

import asyncio
import hashlib
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from app.api.deps import UserRole, require_role
from app.config import settings
from app.models.database import get_pool
from app.security import csrf_protect

router = APIRouter(prefix="/organizations", tags=["organizations"])


class OrganizationCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = Field(default="", max_length=1000)


class OrganizationUpdateRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = Field(None, max_length=1000)


VALID_ORG_ROLES = ("owner", "admin", "member")


class OrgMemberRequest(BaseModel):
    user_id: int = Field(..., gt=0)
    role: str

    @field_validator("role")
    @classmethod
    def validate_role(cls, v):
        if v not in VALID_ORG_ROLES:
            raise ValueError(f"Role must be one of: {', '.join(VALID_ORG_ROLES)}")
        return v


class OrgMemberUpdateRequest(BaseModel):
    role: str

    @field_validator("role")
    @classmethod
    def validate_role(cls, v):
        if v not in VALID_ORG_ROLES:
            raise ValueError(f"Role must be one of: {', '.join(VALID_ORG_ROLES)}")
        return v


# Invite roles — owner cannot be invited (only added via add_org_member)
VALID_INVITE_ROLES = ("admin", "member")


class OrgInviteCreateRequest(BaseModel):
    email: str = Field(..., min_length=1, max_length=255)
    role: str
    expires_in_days: Optional[int] = Field(default=7, ge=1, le=30)

    @field_validator("email")
    @classmethod
    def validate_email(cls, v):
        # Accept either a plain username (3+ non-whitespace chars without @) or a
        # well-formed email address. This allows invites by identifier (e.g.
        # "alice", "superadmin") as well as by email address (e.g. "a@b.com").
        if not re.match(r"^([^\s@]{3,}|[^@\s]+@[^@\s]+\.[^@\s]+)$", v):
            raise ValueError("Invalid identifier format")
        return v.lower()

    @field_validator("role")
    @classmethod
    def validate_role(cls, v):
        if v not in VALID_INVITE_ROLES:
            raise ValueError(f"Role must be one of: {', '.join(VALID_INVITE_ROLES)}")
        return v


class OrgInviteAcceptRequest(BaseModel):
    token: str


def _generate_invite_token() -> tuple[str, str]:
    """Generate a raw invite token and its sha256 hash.

    Returns (raw_token, token_hash). The raw token uses an 'inv_' prefix
    followed by 32 bytes of cryptographically secure random data.
    """
    raw_token = f"inv_{secrets.token_urlsafe(32)}"
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    return raw_token, token_hash


def _generate_slug(name: str) -> str:
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    slug = re.sub(r"-+", "-", slug)
    return slug[:50]


def _is_org_admin_or_owner(conn: sqlite3.Connection, org_id: int, user_id: int) -> bool:
    """Check if user has admin or owner role in the organization."""
    cursor = conn.execute(
        "SELECT role FROM org_members WHERE org_id = ? AND user_id = ?",
        (org_id, user_id),
    )
    row = cursor.fetchone()
    if not row:
        return False
    return row[0] in ("owner", "admin")


@router.get("", include_in_schema=False)
@router.get("/")
async def list_organizations(user: dict = Depends(require_role("member"))):
    """List organizations. Superadmin/admin see all; others see their own."""
    pool = get_pool(str(settings.sqlite_path))
    conn = pool.get_connection()
    try:
        user_role = user.get("role", "")
        if user_role in ("superadmin", "admin"):
            cursor = await asyncio.to_thread(
                conn.execute,
                """SELECT o.id, o.name, o.description, o.created_at, o.updated_at,
                          COUNT(DISTINCT om.user_id) as member_count,
                          COUNT(DISTINCT v.id) as vault_count
                   FROM organizations o
                   LEFT JOIN org_members om ON o.id = om.org_id
                   LEFT JOIN vaults v ON v.org_id = o.id
                   GROUP BY o.id
                   ORDER BY o.name""",
            )
        else:
            cursor = await asyncio.to_thread(
                conn.execute,
                """SELECT o.id, o.name, o.description, o.created_at, o.updated_at,
                          COUNT(DISTINCT om2.user_id) as member_count,
                          COUNT(DISTINCT v.id) as vault_count
                   FROM organizations o
                   JOIN org_members om ON o.id = om.org_id
                   LEFT JOIN org_members om2 ON o.id = om2.org_id
                   LEFT JOIN vaults v ON v.org_id = o.id
                   WHERE om.user_id = ?
                   GROUP BY o.id
                   ORDER BY o.name""",
                (user["id"],),
            )
        rows = await asyncio.to_thread(cursor.fetchall)
        organizations = []
        for row in rows:
            organizations.append(
                {
                    "id": row[0],
                    "name": row[1],
                    "description": row[2] or "",
                    "created_at": row[3],
                    "updated_at": row[4],
                    "member_count": row[5] or 0,
                    "vault_count": row[6] or 0,
                }
            )
        return {"organizations": organizations, "total": len(organizations)}
    finally:
        pool.release_connection(conn)


@router.post("", include_in_schema=False)
@router.post("/")
async def create_organization(
    req: OrganizationCreateRequest,
    user: dict = Depends(require_role("admin")),
    _csrf_token: str = Depends(csrf_protect),
):
    """Create a new organization with the current user as owner."""
    pool = get_pool(str(settings.sqlite_path))
    conn = pool.get_connection()
    try:
        slug = _generate_slug(req.name)
        try:
            # Insert organization
            cursor = await asyncio.to_thread(
                conn.execute,
                """INSERT INTO organizations (name, description, slug, created_by)
                   VALUES (?, ?, ?, ?)""",
                (req.name, req.description or "", slug, user["id"]),
            )
            org_id = cursor.lastrowid

            # Add creator as owner
            await asyncio.to_thread(
                conn.execute,
                "INSERT INTO org_members (org_id, user_id, role) VALUES (?, ?, 'owner')",
                (org_id, user["id"]),
            )

            await asyncio.to_thread(conn.commit)
        except sqlite3.IntegrityError:
            await asyncio.to_thread(conn.rollback)
            raise HTTPException(
                status_code=409,
                detail="Conflict — could not create organization. Please choose a different name.",
            )

        # Fetch created organization
        cursor = await asyncio.to_thread(
            conn.execute,
            """SELECT o.id, o.name, o.description, o.slug, o.created_at, o.updated_at,
                      COUNT(DISTINCT om.user_id) as member_count,
                      COUNT(DISTINCT v.id) as vault_count
               FROM organizations o
               LEFT JOIN org_members om ON o.id = om.org_id
               LEFT JOIN vaults v ON v.org_id = o.id
               WHERE o.id = ?
               GROUP BY o.id""",
            (org_id,),
        )
        row = await asyncio.to_thread(cursor.fetchone)
        return {
            "id": row[0],
            "name": row[1],
            "description": row[2] or "",
            "slug": row[3],
            "created_at": row[4],
            "updated_at": row[5],
            "member_count": row[6] or 0,
            "vault_count": row[7] or 0,
        }
    finally:
        pool.release_connection(conn)


@router.get("/{org_id}")
async def get_organization(
    org_id: int,
    user: dict = Depends(require_role("member")),
):
    """Get organization details with members list."""
    pool = get_pool(str(settings.sqlite_path))
    conn = pool.get_connection()
    try:
        # Check organization exists first (before revealing membership info)
        cursor = await asyncio.to_thread(
            conn.execute,
            "SELECT id FROM organizations WHERE id = ?",
            (org_id,),
        )
        if not await asyncio.to_thread(cursor.fetchone):
            raise HTTPException(status_code=404, detail="Organization not found")

        # Check user is member
        cursor = await asyncio.to_thread(
            conn.execute,
            "SELECT 1 FROM org_members WHERE org_id = ? AND user_id = ?",
            (org_id, user["id"]),
        )
        if not await asyncio.to_thread(cursor.fetchone):
            raise HTTPException(
                status_code=403,
                detail="Access denied: not a member of this organization",
            )

        # Fetch organization details
        cursor = await asyncio.to_thread(
            conn.execute,
            """SELECT o.id, o.name, o.description, o.slug, o.created_at, o.updated_at,
                      COUNT(DISTINCT om.user_id) as member_count,
                      COUNT(DISTINCT v.id) as vault_count
               FROM organizations o
               LEFT JOIN org_members om ON o.id = om.org_id
               LEFT JOIN vaults v ON v.org_id = o.id
               WHERE o.id = ?
               GROUP BY o.id""",
            (org_id,),
        )
        row = await asyncio.to_thread(cursor.fetchone)

        org = {
            "id": row[0],
            "name": row[1],
            "description": row[2] or "",
            "slug": row[3],
            "created_at": row[4],
            "updated_at": row[5],
            "member_count": row[6] or 0,
            "vault_count": row[7] or 0,
        }

        # Fetch members
        cursor = await asyncio.to_thread(
            conn.execute,
            """SELECT u.id, u.username, u.full_name, om.role, om.joined_at
               FROM org_members om JOIN users u ON om.user_id = u.id
               WHERE om.org_id = ? ORDER BY om.role DESC, u.username""",
            (org_id,),
        )
        members = []
        for member_row in await asyncio.to_thread(cursor.fetchall):
            members.append(
                {
                    "id": member_row[0],
                    "username": member_row[1],
                    "full_name": member_row[2] or "",
                    "role": member_row[3],
                    "joined_at": member_row[4],
                }
            )

        org["members"] = members
        return org
    finally:
        pool.release_connection(conn)


@router.patch("/{org_id}")
async def update_organization(
    org_id: int,
    req: OrganizationUpdateRequest,
    user: dict = Depends(require_role("member")),
    _csrf_token: str = Depends(csrf_protect),
):
    """Update organization details (admin or owner only)."""
    pool = get_pool(str(settings.sqlite_path))
    conn = pool.get_connection()
    try:
        # Check organization exists
        cursor = await asyncio.to_thread(
            conn.execute,
            "SELECT id FROM organizations WHERE id = ?",
            (org_id,),
        )
        if not await asyncio.to_thread(cursor.fetchone):
            raise HTTPException(status_code=404, detail="Organization not found")

        # Check user is admin or owner
        cursor = await asyncio.to_thread(
            conn.execute,
            "SELECT role FROM org_members WHERE org_id = ? AND user_id = ?",
            (org_id, user["id"]),
        )
        row = await asyncio.to_thread(cursor.fetchone)
        if not row or row[0] not in ("owner", "admin"):
            raise HTTPException(
                status_code=403,
                detail="Insufficient privileges. Organization admin or owner required",
            )

        # Build partial update
        updates = []
        params = []
        if req.name is not None:
            updates.append("name = ?")
            params.append(req.name)
            # Update slug when name changes
            updates.append("slug = ?")
            params.append(_generate_slug(req.name))
        if req.description is not None:
            updates.append("description = ?")
            params.append(req.description)

        if not updates:
            raise HTTPException(status_code=400, detail="No fields to update")

        updates.append("updated_at = CURRENT_TIMESTAMP")
        params.append(org_id)

        try:
            await asyncio.to_thread(
                conn.execute,
                f"UPDATE organizations SET {', '.join(updates)} WHERE id = ?",
                params,
            )
            await asyncio.to_thread(conn.commit)
        except sqlite3.IntegrityError:
            await asyncio.to_thread(conn.rollback)
            raise HTTPException(
                status_code=409,
                detail="Conflict — could not update organization. Please choose a different name.",
            )

        # Check if organization still exists after update
        cursor = await asyncio.to_thread(
            conn.execute,
            """SELECT o.id, o.name, o.description, o.slug, o.created_at, o.updated_at,
                      COUNT(DISTINCT om.user_id) as member_count,
                      COUNT(DISTINCT v.id) as vault_count
               FROM organizations o
               LEFT JOIN org_members om ON o.id = om.org_id
               LEFT JOIN vaults v ON v.org_id = o.id
               WHERE o.id = ?
               GROUP BY o.id""",
            (org_id,),
        )
        row = await asyncio.to_thread(cursor.fetchone)
        if not row:
            raise HTTPException(status_code=404, detail="Organization not found")

        return {
            "id": row[0],
            "name": row[1],
            "description": row[2] or "",
            "slug": row[3],
            "created_at": row[4],
            "updated_at": row[5],
            "member_count": row[6] or 0,
            "vault_count": row[7] or 0,
        }
    finally:
        pool.release_connection(conn)


@router.get("/{org_id}/members")
async def list_org_members(
    org_id: int,
    user: dict = Depends(require_role("member")),
):
    """List members of an organization."""
    pool = get_pool(str(settings.sqlite_path))
    conn = pool.get_connection()
    try:
        # Check organization exists
        cursor = await asyncio.to_thread(
            conn.execute,
            "SELECT id FROM organizations WHERE id = ?",
            (org_id,),
        )
        if not await asyncio.to_thread(cursor.fetchone):
            raise HTTPException(status_code=404, detail="Organization not found")

        # Check user is member (or superadmin/admin who can see all)
        user_role = user.get("role", "")
        if user_role not in ("superadmin", "admin"):
            cursor = await asyncio.to_thread(
                conn.execute,
                "SELECT 1 FROM org_members WHERE org_id = ? AND user_id = ?",
                (org_id, user["id"]),
            )
            if not await asyncio.to_thread(cursor.fetchone):
                raise HTTPException(
                    status_code=403,
                    detail="Access denied: not a member of this organization",
                )

        # Fetch members
        cursor = await asyncio.to_thread(
            conn.execute,
            """SELECT u.id, u.username, u.full_name, om.role, om.joined_at
               FROM org_members om JOIN users u ON om.user_id = u.id
               WHERE om.org_id = ? ORDER BY om.role DESC, u.username""",
            (org_id,),
        )
        members = []
        for row in await asyncio.to_thread(cursor.fetchall):
            members.append(
                {
                    "user_id": row[0],
                    "username": row[1],
                    "full_name": row[2] or "",
                    "role": row[3],
                    "joined_at": row[4],
                }
            )
        return {"members": members}
    finally:
        pool.release_connection(conn)


@router.post("/{org_id}/members")
async def add_org_member(
    org_id: int,
    req: OrgMemberRequest,
    user: dict = Depends(require_role("member")),
    _csrf_token: str = Depends(csrf_protect),
):
    """Add a member to organization (admin or owner only)."""
    pool = get_pool(str(settings.sqlite_path))
    conn = pool.get_connection()
    try:
        # Check organization exists
        cursor = await asyncio.to_thread(
            conn.execute,
            "SELECT id FROM organizations WHERE id = ?",
            (org_id,),
        )
        if not await asyncio.to_thread(cursor.fetchone):
            raise HTTPException(status_code=404, detail="Organization not found")

        # Check caller is admin or owner
        if not await asyncio.to_thread(_is_org_admin_or_owner, conn, org_id, user["id"]):
            raise HTTPException(
                status_code=403,
                detail="Insufficient privileges. Organization admin or owner required",
            )

        # Check target user exists and is active
        cursor = await asyncio.to_thread(
            conn.execute,
            "SELECT id, username, full_name FROM users WHERE id = ? AND is_active = 1",
            (req.user_id,),
        )
        target_user = await asyncio.to_thread(cursor.fetchone)
        if not target_user:
            raise HTTPException(
                status_code=404,
                detail="User not found or inactive",
            )

        # Only org owners can add other owners
        if req.role == "owner":
            # Check if caller is actually an owner (not just admin)
            cursor = await asyncio.to_thread(
                conn.execute,
                "SELECT role FROM org_members WHERE org_id = ? AND user_id = ?",
                (org_id, user["id"]),
            )
            caller_role_row = await asyncio.to_thread(cursor.fetchone)
            if not caller_role_row or caller_role_row[0] != "owner":
                raise HTTPException(
                    status_code=403,
                    detail="Only organization owners can assign the owner role",
                )

        # Check not already member
        cursor = await asyncio.to_thread(
            conn.execute,
            "SELECT 1 FROM org_members WHERE org_id = ? AND user_id = ?",
            (org_id, req.user_id),
        )
        if await asyncio.to_thread(cursor.fetchone):
            raise HTTPException(
                status_code=409,
                detail="User is already a member of this organization",
            )

        # Insert member
        cursor = await asyncio.to_thread(
            conn.execute,
            "INSERT INTO org_members (org_id, user_id, role) VALUES (?, ?, ?)",
            (org_id, req.user_id, req.role),
        )
        await asyncio.to_thread(conn.commit)

        # Fetch member details
        cursor = await asyncio.to_thread(
            conn.execute,
            """SELECT u.id, u.username, u.full_name, om.role, om.joined_at
               FROM org_members om JOIN users u ON om.user_id = u.id
               WHERE om.org_id = ? AND om.user_id = ?""",
            (org_id, req.user_id),
        )
        row = await asyncio.to_thread(cursor.fetchone)
        return {
            "user_id": row[0],
            "username": row[1],
            "full_name": row[2] or "",
            "role": row[3],
            "joined_at": row[4],
        }
    finally:
        pool.release_connection(conn)


# ---------------------------------------------------------------------------
# Organization invite management
# ---------------------------------------------------------------------------


ORG_INVITE_COLUMNS = (
    "id",
    "org_id",
    "email",
    "token_hash",
    "role",
    "expires_at",
    "created_at",
    "created_by_user_id",
    "accepted_at",
    "accepted_by_user_id",
    "revoked_at",
)


def _org_invite_row(row: tuple[Any, ...]) -> dict[str, Any]:
    """Convert a selected org_invites row to named fields without mutating row_factory."""
    return dict(zip(ORG_INVITE_COLUMNS, row))


def _invite_status(invite_row: dict[str, Any]) -> str:
    """Determine invite status from a org_invites row."""
    if invite_row["accepted_at"]:
        return "accepted"
    if invite_row["revoked_at"]:
        return "revoked"
    expires_at = datetime.fromisoformat(invite_row["expires_at"])
    if expires_at < datetime.now(timezone.utc):
        return "expired"
    return "pending"


@router.post("/{org_id}/invites", status_code=201)
async def create_org_invite(
    org_id: int,
    req: OrgInviteCreateRequest,
    user: dict = Depends(require_role("member")),
    _csrf_token: str = Depends(csrf_protect),
):
    """Create an invite for a user to join an organization (admin/owner only)."""
    pool = get_pool(str(settings.sqlite_path))
    conn = pool.get_connection()
    try:
        # Check organization exists
        cursor = await asyncio.to_thread(
            conn.execute,
            "SELECT id FROM organizations WHERE id = ?",
            (org_id,),
        )
        if not await asyncio.to_thread(cursor.fetchone):
            raise HTTPException(status_code=404, detail="Organization not found")

        # Check caller is admin or owner
        if not await asyncio.to_thread(_is_org_admin_or_owner, conn, org_id, user["id"]):
            raise HTTPException(
                status_code=403,
                detail="Insufficient privileges. Organization admin or owner required",
            )

        # Fail closed: if the invitee exists with a global role below member,
        # they can never accept the invite (accept_org_invite requires
        # require_role("member")), so reject at creation time with a clear
        # message instead of creating a permanently dead-end invite.
        invitee_cursor = await asyncio.to_thread(
            conn.execute,
            "SELECT role FROM users WHERE username = ? COLLATE NOCASE",
            (req.email.lower(),),
        )
        invitee_row = await asyncio.to_thread(invitee_cursor.fetchone)
        if invitee_row:
            invitee_level = UserRole.level(invitee_row[0])
            if invitee_level < UserRole.MEMBER.value:
                raise HTTPException(
                    status_code=400,
                    detail="Cannot invite a user with a global role below member",
                )

        # Generate token
        raw_token, token_hash = _generate_invite_token()
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(days=req.expires_in_days)

        # Insert invite
        cursor = await asyncio.to_thread(
            conn.execute,
            """INSERT INTO org_invites
               (org_id, email, token_hash, role, expires_at, created_at, created_by_user_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                org_id,
                req.email.lower(),
                token_hash,
                req.role,
                expires_at.isoformat(),
                now.isoformat(),
                user["id"],
            ),
        )
        await asyncio.to_thread(conn.commit)
        invite_id = cursor.lastrowid

        return {
            "invite_id": invite_id,
            "email": req.email.lower(),
            "role": req.role,
            "expires_at": expires_at.isoformat(),
            "token": raw_token,
        }
    finally:
        pool.release_connection(conn)


@router.post("/{org_id}/invites/{invite_id}/resend", status_code=201)
async def resend_org_invite(
    org_id: int,
    invite_id: int,
    user: dict = Depends(require_role("member")),
    _csrf_token: str = Depends(csrf_protect),
):
    """Resend an invite with a new token (admin/owner only). Invalidates old token."""
    pool = get_pool(str(settings.sqlite_path))
    conn = pool.get_connection()
    try:
        # Check organization exists
        cursor = await asyncio.to_thread(
            conn.execute,
            "SELECT id FROM organizations WHERE id = ?",
            (org_id,),
        )
        if not await asyncio.to_thread(cursor.fetchone):
            raise HTTPException(status_code=404, detail="Organization not found")

        # Check caller is admin or owner
        if not await asyncio.to_thread(_is_org_admin_or_owner, conn, org_id, user["id"]):
            raise HTTPException(
                status_code=403,
                detail="Insufficient privileges. Organization admin or owner required",
            )

        # Fetch existing invite
        cursor = await asyncio.to_thread(
            conn.execute,
            """SELECT id, email, role, revoked_at, accepted_at
               FROM org_invites WHERE id = ? AND org_id = ?""",
            (invite_id, org_id),
        )
        invite = await asyncio.to_thread(cursor.fetchone)
        if not invite:
            raise HTTPException(status_code=404, detail="Invite not found")

        # Cannot resend accepted or revoked invites
        if invite[3]:  # revoked_at
            raise HTTPException(status_code=400, detail="Invite has been revoked")
        if invite[4]:  # accepted_at
            raise HTTPException(status_code=400, detail="Invite has already been accepted")

        # Generate new token
        raw_token, token_hash = _generate_invite_token()
        now = datetime.now(timezone.utc)
        # Default 7-day expiry on resend
        expires_at = now + timedelta(days=7)

        await asyncio.to_thread(
            conn.execute,
            """UPDATE org_invites
               SET token_hash = ?, expires_at = ?, created_at = ?, created_by_user_id = ?
               WHERE id = ?""",
            (token_hash, expires_at.isoformat(), now.isoformat(), user["id"], invite_id),
        )
        await asyncio.to_thread(conn.commit)

        return {
            "invite_id": invite_id,
            "email": invite[1],
            "role": invite[2],
            "expires_at": expires_at.isoformat(),
            "token": raw_token,
        }
    finally:
        pool.release_connection(conn)


@router.post("/{org_id}/invites/{invite_id}/revoke")
async def revoke_org_invite(
    org_id: int,
    invite_id: int,
    user: dict = Depends(require_role("member")),
    _csrf_token: str = Depends(csrf_protect),
):
    """Revoke an invite (admin/owner only). Idempotent-ish."""
    pool = get_pool(str(settings.sqlite_path))
    conn = pool.get_connection()
    try:
        # Check organization exists
        cursor = await asyncio.to_thread(
            conn.execute,
            "SELECT id FROM organizations WHERE id = ?",
            (org_id,),
        )
        if not await asyncio.to_thread(cursor.fetchone):
            raise HTTPException(status_code=404, detail="Organization not found")

        # Check caller is admin or owner
        if not await asyncio.to_thread(_is_org_admin_or_owner, conn, org_id, user["id"]):
            raise HTTPException(
                status_code=403,
                detail="Insufficient privileges. Organization admin or owner required",
            )

        # Fetch existing invite
        cursor = await asyncio.to_thread(
            conn.execute,
            "SELECT id, revoked_at FROM org_invites WHERE id = ? AND org_id = ?",
            (invite_id, org_id),
        )
        invite = await asyncio.to_thread(cursor.fetchone)
        if not invite:
            raise HTTPException(status_code=404, detail="Invite not found")

        if invite[1]:  # already revoked
            raise HTTPException(status_code=400, detail="Invite has already been revoked")

        now = datetime.now(timezone.utc).isoformat()
        await asyncio.to_thread(
            conn.execute,
            "UPDATE org_invites SET revoked_at = ? WHERE id = ?",
            (now, invite_id),
        )
        await asyncio.to_thread(conn.commit)

        return {"message": "Invite revoked", "invite_id": invite_id}
    finally:
        pool.release_connection(conn)


@router.get("/{org_id}/invites")
async def list_org_invites(
    org_id: int,
    user: dict = Depends(require_role("member")),
):
    """List invites for an organization (admin/owner only)."""
    pool = get_pool(str(settings.sqlite_path))
    conn = pool.get_connection()
    try:
        # Check organization exists
        cursor = await asyncio.to_thread(
            conn.execute,
            "SELECT id FROM organizations WHERE id = ?",
            (org_id,),
        )
        if not await asyncio.to_thread(cursor.fetchone):
            raise HTTPException(status_code=404, detail="Organization not found")

        # Check caller is admin or owner
        if not await asyncio.to_thread(_is_org_admin_or_owner, conn, org_id, user["id"]):
            raise HTTPException(
                status_code=403,
                detail="Insufficient privileges. Organization admin or owner required",
            )

        cursor = await asyncio.to_thread(
            conn.execute,
            """SELECT id, org_id, email, token_hash, role, expires_at, created_at,
                      created_by_user_id, accepted_at, accepted_by_user_id, revoked_at
               FROM org_invites WHERE org_id = ?
               ORDER BY created_at DESC""",
            (org_id,),
        )
        invites = []
        for row in await asyncio.to_thread(cursor.fetchall):
            invite = _org_invite_row(row)
            invites.append(
                {
                    "id": invite["id"],
                    "email": invite["email"],
                    "role": invite["role"],
                    "status": _invite_status(invite),
                    "expires_at": invite["expires_at"],
                    "created_at": invite["created_at"],
                }
            )
        return {"invites": invites}
    finally:
        pool.release_connection(conn)


@router.post("/invites/accept")
async def accept_org_invite(
    req: OrgInviteAcceptRequest,
    user: dict = Depends(require_role("member")),
    _csrf_token: str = Depends(csrf_protect),
):
    """Accept an organization invite using a token.

    The authenticated user must match the invite email (prevents token theft).
    On success, creates org_members row and marks invite as accepted.
    """
    token_hash = hashlib.sha256(req.token.encode()).hexdigest()

    pool = get_pool(str(settings.sqlite_path))
    conn = pool.get_connection()
    try:
        # Look up invite by token hash
        cursor = await asyncio.to_thread(
            conn.execute,
            """SELECT id, org_id, email, role, expires_at, revoked_at,
                      accepted_at, accepted_by_user_id
               FROM org_invites WHERE token_hash = ?""",
            (token_hash,),
        )
        invite = await asyncio.to_thread(cursor.fetchone)
        if not invite:
            raise HTTPException(status_code=400, detail="Invalid or expired invite")

        invite_id, org_id, invite_email, role, expires_at_str, revoked_at, accepted_at, accepted_by = invite

        # Check expiry, revoked, and already-accepted — all return the same
        # generic message to prevent token-state enumeration.
        expires_at = datetime.fromisoformat(expires_at_str)
        if expires_at < datetime.now(timezone.utc) or revoked_at or accepted_at:
            raise HTTPException(status_code=400, detail="Invalid or expired invite")

        # SECURITY: invite email must match authenticated user's username
        # (users table has no email column, so we match against username;
        # usernames are unique and case-insensitive, providing equivalent security)
        cursor = await asyncio.to_thread(
            conn.execute,
            "SELECT username FROM users WHERE id = ?",
            (user["id"],),
        )
        user_row = await asyncio.to_thread(cursor.fetchone)
        if not user_row or user_row[0].lower() != invite_email.lower():
            raise HTTPException(
                status_code=403,
                detail="Invite is not addressed to your account",
            )

        now = datetime.now(timezone.utc).isoformat()

        # Check if already a member (fast-path to avoid the atomic block for the
        # common case; the authoritative guard is the transactional INSERT below).
        cursor = await asyncio.to_thread(
            conn.execute,
            "SELECT 1 FROM org_members WHERE org_id = ? AND user_id = ?",
            (org_id, user["id"]),
        )
        if await asyncio.to_thread(cursor.fetchone):
            raise HTTPException(
                status_code=409,
                detail="You are already a member of this organization",
            )

        # Atomic: membership INSERT + invite accepted_at UPDATE must be committed
        # together so a concurrent double-accept cannot leave the invite unmarked.
        # BEGIN IMMEDIATE acquires an exclusive write lock before the pre-check.
        try:
            await asyncio.to_thread(conn.execute, "BEGIN IMMEDIATE")
            try:
                await asyncio.to_thread(
                    conn.execute,
                    "INSERT INTO org_members (org_id, user_id, role) VALUES (?, ?, ?)",
                    (org_id, user["id"], role),
                )
            except sqlite3.IntegrityError as e:
                if "UNIQUE constraint failed" in str(e):
                    # Roll back BEFORE raising so the transaction is closed before
                    # HTTPException propagates past the outer sqlite3.Error handler.
                    await asyncio.to_thread(conn.rollback)
                    raise HTTPException(
                        status_code=409,
                        detail="You are already a member of this organization",
                    )
                raise

            await asyncio.to_thread(
                conn.execute,
                """UPDATE org_invites
                   SET accepted_at = ?, accepted_by_user_id = ?
                   WHERE id = ?""",
                (now, user["id"], invite_id),
            )
            await asyncio.to_thread(conn.commit)
        except sqlite3.Error:
            await asyncio.to_thread(conn.rollback)
            raise

        # Fetch membership details
        cursor = await asyncio.to_thread(
            conn.execute,
            """SELECT u.id, u.username, u.full_name, om.role, om.joined_at
               FROM org_members om JOIN users u ON om.user_id = u.id
               WHERE om.org_id = ? AND om.user_id = ?""",
            (org_id, user["id"]),
        )
        row = await asyncio.to_thread(cursor.fetchone)
        return {
            "user_id": row[0],
            "username": row[1],
            "full_name": row[2] or "",
            "role": row[3],
            "joined_at": row[4],
        }
    finally:
        pool.release_connection(conn)


@router.patch("/{org_id}/members/{member_user_id}")
async def update_org_member_role(
    org_id: int,
    member_user_id: int,
    req: OrgMemberUpdateRequest,
    user: dict = Depends(require_role("member")),
    _csrf_token: str = Depends(csrf_protect),
):
    """Update a member's role (admin or owner only)."""
    pool = get_pool(str(settings.sqlite_path))
    conn = pool.get_connection()
    try:
        # Check organization exists
        cursor = await asyncio.to_thread(
            conn.execute,
            "SELECT id FROM organizations WHERE id = ?",
            (org_id,),
        )
        if not await asyncio.to_thread(cursor.fetchone):
            raise HTTPException(status_code=404, detail="Organization not found")

        # Check caller is admin or owner
        if not await asyncio.to_thread(_is_org_admin_or_owner, conn, org_id, user["id"]):
            raise HTTPException(
                status_code=403,
                detail="Insufficient privileges. Organization admin or owner required",
            )

        # Fetch target member's current role
        cursor = await asyncio.to_thread(
            conn.execute,
            "SELECT role FROM org_members WHERE org_id = ? AND user_id = ?",
            (org_id, member_user_id),
        )
        target_row = await asyncio.to_thread(cursor.fetchone)
        if not target_row:
            raise HTTPException(status_code=404, detail="Member not found")
        if target_row[0] == "owner":
            raise HTTPException(
                status_code=403,
                detail="Cannot change the role of the organization owner",
            )
        if req.role == "owner":
            raise HTTPException(
                status_code=403,
                detail="Use the ownership transfer endpoint to assign the owner role",
            )

        # Update role
        try:
            await asyncio.to_thread(
                conn.execute,
                "UPDATE org_members SET role = ? WHERE org_id = ? AND user_id = ?",
                (req.role, org_id, member_user_id),
            )
            await asyncio.to_thread(conn.commit)
        except sqlite3.Error:
            await asyncio.to_thread(conn.rollback)
            raise HTTPException(
                status_code=409,
                detail="Could not update member role. Please try again.",
            )

        # Fetch updated member details
        cursor = await asyncio.to_thread(
            conn.execute,
            """SELECT u.id, u.username, u.full_name, om.role, om.joined_at
               FROM org_members om JOIN users u ON om.user_id = u.id
               WHERE om.org_id = ? AND om.user_id = ?""",
            (org_id, member_user_id),
        )
        row = await asyncio.to_thread(cursor.fetchone)
        return {
            "user_id": row[0],
            "username": row[1],
            "full_name": row[2] or "",
            "role": row[3],
            "joined_at": row[4],
        }
    finally:
        pool.release_connection(conn)


@router.delete("/{org_id}/members/{member_user_id}")
async def remove_org_member(
    org_id: int,
    member_user_id: int,
    user: dict = Depends(require_role("member")),
    _csrf_token: str = Depends(csrf_protect),
):
    """Remove a member from organization (admin or owner only)."""
    pool = get_pool(str(settings.sqlite_path))
    conn = pool.get_connection()
    try:
        # Check organization exists
        cursor = await asyncio.to_thread(
            conn.execute,
            "SELECT id FROM organizations WHERE id = ?",
            (org_id,),
        )
        if not await asyncio.to_thread(cursor.fetchone):
            raise HTTPException(status_code=404, detail="Organization not found")

        # Check caller is admin or owner
        if not await asyncio.to_thread(_is_org_admin_or_owner, conn, org_id, user["id"]):
            raise HTTPException(
                status_code=403,
                detail="Insufficient privileges. Organization admin or owner required",
            )

        # Check target member exists
        cursor = await asyncio.to_thread(
            conn.execute,
            "SELECT role FROM org_members WHERE org_id = ? AND user_id = ?",
            (org_id, member_user_id),
        )
        target_row = await asyncio.to_thread(cursor.fetchone)
        if not target_row:
            raise HTTPException(
                status_code=404,
                detail="Member not found in organization",
            )
        if target_row[0] == "owner":
            raise HTTPException(
                status_code=403,
                detail="Cannot remove the organization owner. Transfer ownership first.",
            )

        # Check not removing self
        if member_user_id == user.get("id"):
            raise HTTPException(
                status_code=400, detail="Cannot remove yourself from an organization"
            )

        # Delete member
        try:
            await asyncio.to_thread(
                conn.execute,
                "DELETE FROM org_members WHERE org_id = ? AND user_id = ?",
                (org_id, member_user_id),
            )
            await asyncio.to_thread(conn.commit)
        except Exception:
            await asyncio.to_thread(conn.rollback)
            raise

        return {
            "message": "Member removed",
            "org_id": org_id,
            "user_id": member_user_id,
        }
    finally:
        pool.release_connection(conn)


class TransferOwnershipRequest(BaseModel):
    new_owner_user_id: int = Field(..., gt=0)


@router.post("/{org_id}/transfer-ownership")
async def transfer_ownership(
    org_id: int,
    req: TransferOwnershipRequest,
    user: dict = Depends(require_role("member")),
    _csrf_token: str = Depends(csrf_protect),
):
    """Transfer organization ownership to another member (current owner or superadmin only)."""
    pool = get_pool(str(settings.sqlite_path))
    conn = pool.get_connection()
    try:
        cursor = await asyncio.to_thread(
            conn.execute,
            "SELECT id FROM organizations WHERE id = ?",
            (org_id,),
        )
        if not await asyncio.to_thread(cursor.fetchone):
            raise HTTPException(status_code=404, detail="Organization not found")

        if user.get("role") != "superadmin":
            cursor = await asyncio.to_thread(
                conn.execute,
                "SELECT role FROM org_members WHERE org_id = ? AND user_id = ?",
                (org_id, user["id"]),
            )
            row = await asyncio.to_thread(cursor.fetchone)
            if not row or row[0] != "owner":
                raise HTTPException(
                    status_code=403,
                    detail="Only the organization owner or a superadmin can transfer ownership",
                )

        if req.new_owner_user_id == user.get("id"):
            raise HTTPException(status_code=400, detail="Cannot transfer ownership to yourself")

        cursor = await asyncio.to_thread(
            conn.execute,
            """SELECT u.id FROM users u
               JOIN org_members om ON u.id = om.user_id
               WHERE u.id = ? AND om.org_id = ? AND u.is_active = 1""",
            (req.new_owner_user_id, org_id),
        )
        if not await asyncio.to_thread(cursor.fetchone):
            raise HTTPException(
                status_code=400,
                detail="New owner must be an active member of the organization",
            )

        try:
            await asyncio.to_thread(conn.execute, "BEGIN IMMEDIATE")
            await asyncio.to_thread(
                conn.execute,
                "UPDATE org_members SET role = 'admin' WHERE org_id = ? AND role = 'owner'",
                (org_id,),
            )
            await asyncio.to_thread(
                conn.execute,
                "UPDATE org_members SET role = 'owner' WHERE org_id = ? AND user_id = ?",
                (org_id, req.new_owner_user_id),
            )
            await asyncio.to_thread(conn.commit)
        except sqlite3.Error:
            await asyncio.to_thread(conn.rollback)
            raise HTTPException(status_code=500, detail="Failed to transfer ownership")

        return {
            "message": "Ownership transferred successfully",
            "org_id": org_id,
            "new_owner_user_id": req.new_owner_user_id,
        }
    finally:
        pool.release_connection(conn)


@router.delete("/{org_id}")
async def delete_organization(
    org_id: int,
    user: dict = Depends(require_role("superadmin")),
    _csrf_token: str = Depends(csrf_protect),
):
    """Delete organization and all associated data (superadmin only)."""
    pool = get_pool(str(settings.sqlite_path))
    conn = pool.get_connection()
    try:
        # Check organization exists
        cursor = await asyncio.to_thread(
            conn.execute,
            "SELECT 1 FROM organizations WHERE id = ?",
            (org_id,),
        )
        if not await asyncio.to_thread(cursor.fetchone):
            raise HTTPException(status_code=404, detail="Organization not found")

        # Delete organization (FK cascades handle org_members, groups, group_members)
        await asyncio.to_thread(
            conn.execute,
            "DELETE FROM organizations WHERE id = ?",
            (org_id,),
        )
        await asyncio.to_thread(conn.commit)

        return {
            "message": "Organization deleted",
            "org_id": org_id,
        }
    finally:
        pool.release_connection(conn)


# ---------------------------------------------------------------------------
# Prompt override management (FR-007 part 2)
# ---------------------------------------------------------------------------


class PromptOverrideSetRequest(BaseModel):
    """Request body for setting an org's prompt version override."""

    version: str = Field(..., min_length=1, description="Prompt version to use for this org")


class PromptOverrideResponse(BaseModel):
    """Response containing the effective prompt version for an org."""

    version: str
    content: str
    is_override: bool  # True if org has an override; False if using global active
    org_id: int


@router.put("/{org_id}/prompt-override")
async def set_prompt_override(
    org_id: int,
    req: PromptOverrideSetRequest,
    user: dict = Depends(require_role("member")),
    _csrf_token: str = Depends(csrf_protect),
):
    """Set (or update) an organization's prompt version override.

    Only org admins and owners may set the override. The specified version
    must exist (404 if not). Returns the effective version (which may be
    the global active if no override is set).
    """
    pool = get_pool(str(settings.sqlite_path))
    conn = pool.get_connection()
    try:
        # Check org exists
        cursor = await asyncio.to_thread(
            conn.execute,
            "SELECT id FROM organizations WHERE id = ?",
            (org_id,),
        )
        if not await asyncio.to_thread(cursor.fetchone):
            raise HTTPException(status_code=404, detail="Organization not found")

        # Authz: caller must be org admin or owner (superadmin bypasses this check)
        if user.get("role") != "superadmin" and not await asyncio.to_thread(
            _is_org_admin_or_owner, conn, org_id, user["id"]
        ):
            raise HTTPException(
                status_code=403,
                detail="Insufficient privileges. Organization admin or owner required",
            )

        from app.services.prompt_store import PromptVersionStore

        store = PromptVersionStore(conn)
        try:
            await asyncio.to_thread(
                store.set_org_override, org_id, req.version, str(user["id"])
            )
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))

        # Return the effective version
        effective = await asyncio.to_thread(store.resolve_for_org, org_id)
        is_override = (await asyncio.to_thread(store.get_for_org, org_id)) is not None

        return PromptOverrideResponse(
            version=effective.version,
            content=effective.content,
            is_override=is_override,
            org_id=org_id,
        )
    finally:
        pool.release_connection(conn)


@router.delete("/{org_id}/prompt-override")
async def clear_prompt_override(
    org_id: int,
    user: dict = Depends(require_role("member")),
    _csrf_token: str = Depends(csrf_protect),
):
    """Clear an organization's prompt version override.

    Only org admins and owners may clear the override. After clearing,
    the org uses the global active version. Returns the global active version.
    """
    pool = get_pool(str(settings.sqlite_path))
    conn = pool.get_connection()
    try:
        # Check org exists
        cursor = await asyncio.to_thread(
            conn.execute,
            "SELECT id FROM organizations WHERE id = ?",
            (org_id,),
        )
        if not await asyncio.to_thread(cursor.fetchone):
            raise HTTPException(status_code=404, detail="Organization not found")

        # Authz: caller must be org admin or owner (superadmin bypasses this check)
        if user.get("role") != "superadmin" and not await asyncio.to_thread(
            _is_org_admin_or_owner, conn, org_id, user["id"]
        ):
            raise HTTPException(
                status_code=403,
                detail="Insufficient privileges. Organization admin or owner required",
            )

        from app.services.prompt_store import PromptVersionStore

        store = PromptVersionStore(conn)
        await asyncio.to_thread(store.clear_org_override, org_id)

        # Return the effective version (now the global active)
        effective = await asyncio.to_thread(store.resolve_for_org, org_id)
        if effective is None:
            raise HTTPException(
                status_code=404,
                detail="No prompt version is currently active globally",
            )
        return PromptOverrideResponse(
            version=effective.version,
            content=effective.content,
            is_override=False,
            org_id=org_id,
        )
    finally:
        pool.release_connection(conn)


@router.get("/{org_id}/prompt-override")
async def get_prompt_override(
    org_id: int,
    user: dict = Depends(require_role("member")),
):
    """Get the effective prompt version for an org (override or global active).

    Does not require org admin/owner — any org member can read the effective version.
    """
    pool = get_pool(str(settings.sqlite_path))
    conn = pool.get_connection()
    try:
        # Check org exists
        cursor = await asyncio.to_thread(
            conn.execute,
            "SELECT id FROM organizations WHERE id = ?",
            (org_id,),
        )
        if not await asyncio.to_thread(cursor.fetchone):
            raise HTTPException(status_code=404, detail="Organization not found")

        # Check user is member of org (or superadmin/admin who can see all)
        user_role = user.get("role", "")
        if user_role not in ("superadmin", "admin"):
            cursor = await asyncio.to_thread(
                conn.execute,
                "SELECT 1 FROM org_members WHERE org_id = ? AND user_id = ?",
                (org_id, user["id"]),
            )
            if not await asyncio.to_thread(cursor.fetchone):
                raise HTTPException(
                    status_code=403,
                    detail="Access denied: not a member of this organization",
                )

        from app.services.prompt_store import PromptVersionStore

        store = PromptVersionStore(conn)
        is_override = (await asyncio.to_thread(store.get_for_org, org_id)) is not None
        effective = await asyncio.to_thread(store.resolve_for_org, org_id)
        if effective is None:
            raise HTTPException(
                status_code=404,
                detail="No prompt version is currently active globally",
            )
        return PromptOverrideResponse(
            version=effective.version,
            content=effective.content,
            is_override=is_override,
            org_id=org_id,
        )
    finally:
        pool.release_connection(conn)
