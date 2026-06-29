"""Service-account API key management (FR-014).

Provides issuance, rotation, and revocation of scoped service-account API keys.
Service accounts are independent of human users and are authenticated via a
Bearer key that is hashed (sha256) and stored as key_hash in the service_accounts
table. The raw key is returned exactly once at issuance and is never stored.

Management endpoints (/api/service-accounts/*) require admin:config scope.
"""

import asyncio
import hashlib
import logging
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.config import settings
from app.models.database import get_pool
from app.security import require_scope

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/service-accounts", tags=["service-accounts"])


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class ServiceAccountCreate(BaseModel):
    name: str
    scopes: list[str]


class ServiceAccountIssueResponse(BaseModel):
    id: int
    name: str
    scopes: list[str]
    key: str  # Raw key — returned EXACTLY once at issuance
    created_at: str


class ServiceAccountMetadata(BaseModel):
    id: int
    name: str
    scopes: list[str]
    created_at: str
    last_rotated_at: str | None = None
    revoked_at: str | None = None


class ServiceAccountRotateResponse(BaseModel):
    id: int
    name: str
    scopes: list[str]
    key: str  # Raw key — returned EXACTLY once at rotation
    last_rotated_at: str


class ServiceAccountRevokeResponse(BaseModel):
    id: int
    revoked_at: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SERVICE_KEY_PREFIX = "sak_"


def _generate_service_key() -> tuple[str, str]:
    """Generate a raw service-account key and its sha256 hash.

    Returns (raw_key, key_hash). The raw key uses a recognizable prefix
    followed by 32 bytes of cryptographically secure random data.
    """
    raw_key = f"{SERVICE_KEY_PREFIX}{secrets.token_urlsafe(32)}"
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    return raw_key, key_hash


# ---------------------------------------------------------------------------
# Routes — all require admin:config scope
# ---------------------------------------------------------------------------


@router.post("", response_model=ServiceAccountIssueResponse, status_code=201)
async def create_service_account(
    payload: ServiceAccountCreate,
    _auth: dict = Depends(require_scope("admin:config")),
):
    """Issue a new service-account API key.

    The raw key is returned exactly once and is never stored — only its
    sha256 hash. Store the raw key securely; it cannot be retrieved again.
    """
    if not payload.name.strip():
        raise HTTPException(status_code=400, detail="name cannot be empty")
    if not payload.scopes:
        raise HTTPException(status_code=400, detail="at least one scope is required")
    if any(not s.strip() for s in payload.scopes):
        raise HTTPException(status_code=400, detail="scope strings cannot be empty")

    raw_key, key_hash = _generate_service_key()
    scopes_str = ",".join(payload.scopes)
    now = datetime.now(timezone.utc).isoformat()

    pool = get_pool(str(settings.sqlite_path))
    conn = pool.get_connection()
    try:
        cursor = await asyncio.to_thread(
            conn.execute,
            """
            INSERT INTO service_accounts
                (name, key_hash, scopes, created_at, created_by)
            VALUES (?, ?, ?, ?, ?)
            """,
            (payload.name.strip(), key_hash, scopes_str, now,
             # created_by is a non-secret fingerprint of the creating admin's SCOPES (not the token) —
             # the admin-token auth model exposes no per-admin identity besides the secret token,
             # so scope-set is the available non-secret distinguisher.
             hashlib.sha256(",".join(sorted(
                 settings.admin_token_scopes.get(_auth.get("user_id", ""), [])
             )).encode()).hexdigest()[:16]),
        )
        await asyncio.to_thread(conn.commit)
        sa_id = cursor.lastrowid
    except Exception as exc:
        await asyncio.to_thread(conn.rollback)
        if "UNIQUE constraint failed" in str(exc):
            raise HTTPException(
                status_code=409,
                detail="A service account with this key hash already exists (collision)",
            )
        raise
    finally:
        pool.release_connection(conn)

    return ServiceAccountIssueResponse(
        id=sa_id,
        name=payload.name.strip(),
        scopes=payload.scopes,
        key=raw_key,
        created_at=now,
    )


@router.get("", response_model=list[ServiceAccountMetadata])
async def list_service_accounts(
    _auth: dict = Depends(require_scope("admin:config")),
):
    """List all service accounts (metadata only — raw keys are never returned)."""
    pool = get_pool(str(settings.sqlite_path))
    conn = pool.get_connection()
    try:
        cursor = await asyncio.to_thread(
            conn.execute,
            """
            SELECT id, name, scopes, created_at, last_rotated_at, revoked_at
            FROM service_accounts
            ORDER BY id ASC
            """,
        )
        rows = await asyncio.to_thread(cursor.fetchall)
    finally:
        pool.release_connection(conn)

    return [
        ServiceAccountMetadata(
            id=row[0],
            name=row[1],
            scopes=[s.strip() for s in row[2].split(",") if s.strip()],
            created_at=row[3],
            last_rotated_at=row[4],
            revoked_at=row[5],
        )
        for row in rows
    ]


@router.post("/{sa_id}/rotate", response_model=ServiceAccountRotateResponse)
async def rotate_service_account_key(
    sa_id: int,
    _auth: dict = Depends(require_scope("admin:config")),
):
    """Rotate a service account's API key.

    Supersedes the current key with a new one. The old key stops working
    immediately; the new raw key is returned once.
    """
    now = datetime.now(timezone.utc).isoformat()

    pool = get_pool(str(settings.sqlite_path))
    conn = pool.get_connection()
    try:
        # Generate new key BEFORE the atomic conditional UPDATE to avoid
        # allocating entropy on a row that may already be revoked.
        new_raw, new_hash = _generate_service_key()

        # Atomic conditional UPDATE: only succeeds if the SA is not revoked.
        # Uses rowcount to detect the already-revoked / not-found case.
        cursor = await asyncio.to_thread(
            conn.execute,
            """
            UPDATE service_accounts
            SET key_hash = ?, last_rotated_at = ?
            WHERE id = ? AND revoked_at IS NULL
            """,
            (new_hash, now, sa_id),
        )
        await asyncio.to_thread(conn.commit)

        if cursor.rowcount == 0:
            # Determine whether it was "not found" or "already revoked" by
            # doing a lightweight existence check (not worth a full SELECT).
            check = await asyncio.to_thread(
                conn.execute,
                "SELECT revoked_at IS NOT NULL FROM service_accounts WHERE id = ?",
                (sa_id,),
            )
            row = await asyncio.to_thread(check.fetchone)
            if row is None:
                raise HTTPException(status_code=404, detail="Service account not found")
            raise HTTPException(status_code=400, detail="Cannot rotate a revoked service account")

        # Fetch metadata for response
        cursor = await asyncio.to_thread(
            conn.execute,
            "SELECT id, name, scopes, last_rotated_at FROM service_accounts WHERE id = ?",
            (sa_id,),
        )
        meta_row = await asyncio.to_thread(cursor.fetchone)
    finally:
        pool.release_connection(conn)

    return ServiceAccountRotateResponse(
        id=meta_row[0],
        name=meta_row[1],
        scopes=[s.strip() for s in meta_row[2].split(",") if s.strip()],
        key=new_raw,
        last_rotated_at=meta_row[3],
    )


@router.post("/{sa_id}/revoke", response_model=ServiceAccountRevokeResponse)
async def revoke_service_account(
    sa_id: int,
    _auth: dict = Depends(require_scope("admin:config")),
):
    """Revoke a service account immediately.

    Sets revoked_at; the associated key stops working immediately.
    """
    now = datetime.now(timezone.utc).isoformat()

    pool = get_pool(str(settings.sqlite_path))
    conn = pool.get_connection()
    try:
        cursor = await asyncio.to_thread(
            conn.execute,
            "SELECT id, revoked_at FROM service_accounts WHERE id = ?",
            (sa_id,),
        )
        row = await asyncio.to_thread(cursor.fetchone)

        if row is None:
            raise HTTPException(status_code=404, detail="Service account not found")
        if row[1] is not None:
            raise HTTPException(status_code=400, detail="Service account is already revoked")

        await asyncio.to_thread(
            conn.execute,
            "UPDATE service_accounts SET revoked_at = ? WHERE id = ?",
            (now, sa_id),
        )
        await asyncio.to_thread(conn.commit)
    finally:
        pool.release_connection(conn)

    return ServiceAccountRevokeResponse(id=sa_id, revoked_at=now)
