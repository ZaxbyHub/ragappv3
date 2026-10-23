"""
Vault API routes for vault management.

Provides endpoints for listing, creating, updating, and deleting vaults.
Vaults are containers for documents, memories, and chat sessions.
"""

import asyncio
import json
import logging
import shutil
import sqlite3
from datetime import datetime, timezone
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
from app.security import csrf_protect
from app.services.draft_deletion import DraftDeletionService
from app.services.draft_input_storage import DraftInputStorage, DraftInputStorageError
from app.services.draft_store import DraftStore
from app.services.security_audit import safe_record_security_event
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


class VaultEnrichmentToggleRequest(BaseModel):
    """Request model for toggling vault-level enrichment override.

    enabled: True  → vault-level ON (overrides global)
    enabled: False → vault-level OFF (overrides global)
    enabled: null  → clears override, falls back to global
    """

    enabled: Optional[bool] = Field(
        None,
        description="True=enable for vault, False=disable for vault, null=inherit global",
    )


class VaultMultimodalToggleRequest(BaseModel):
    """Request model for toggling vault-level multimodal provider opt-in (issue #461).

    enabled: True  → vault-level ON (overrides global)
    enabled: False → vault-level OFF (overrides global)
    enabled: null  → clears override, falls back to global
    """

    enabled: Optional[bool] = Field(
        None,
        description="True=opt in for vault, False=opt out, null=inherit global",
    )


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
    enrichment_enabled: Optional[bool] = None
    effective_enrichment_enabled: bool = False
    multimodal_provider_enabled: Optional[bool] = None
    effective_multimodal_enabled: bool = False

    model_config = ConfigDict(from_attributes=True)


class VaultListResponse(BaseModel):
    """Response model for listing vaults."""

    vaults: List[VaultResponse]


def _row_to_vault_response(row, current_user_permission: Optional[str] = None) -> VaultResponse:
    """Map a database row to VaultResponse."""
    vault_id = row[0]
    # Row structure: id, name, description, created_at, updated_at,
    #               file_count, memory_count, session_count,
    #               org_id, visibility, enrichment_enabled, multimodal_provider_enabled
    enrichment_enabled_raw = row[10] if len(row) > 10 else None
    multimodal_raw = row[11] if len(row) > 11 else None
    enrichment_override: Optional[bool] = None
    if enrichment_enabled_raw is not None:
        enrichment_override = bool(enrichment_enabled_raw)
    effective = (
        enrichment_override
        if enrichment_override is not None
        else settings.chunk_enrichment_enabled
    )
    multimodal_override: Optional[bool] = None
    if multimodal_raw is not None:
        multimodal_override = bool(multimodal_raw)
    # Multimodal enrichment transmits vault artifacts to an external provider,
    # so it requires an EXPLICIT per-vault opt-in (override is True) in addition
    # to the global switch. A NULL/inherit value is fail-closed and does NOT
    # authorize egress — unlike chunk enrichment (local processing) which may
    # inherit the global switch.
    effective_multimodal = bool(
        settings.multimodal_enrichment_enabled and multimodal_override is True
    )
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
        enrichment_enabled=enrichment_override,
        effective_enrichment_enabled=effective,
        multimodal_provider_enabled=multimodal_override,
        effective_multimodal_enabled=effective_multimodal,
        current_user_permission=current_user_permission,
    )


# Vault-scoped tables that carry vault_id with no FK to vaults(id) and no
# other cleanup path (issue #723 / T1-21-S-03): delete_vault's transaction
# removes their rows explicitly. artifact_delete_pending and
# vector_delete_pending are deliberately-durable cleanup queues and are NOT
# listed; memories are reassigned to global scope, not deleted;
# wiki_claim_sources / wiki_page_files / wiki_page_versions /
# wiki_page_links cascade from the wiki_claims / wiki_pages / files rows
# deleted here and in the files delete below. Statements are prebuilt
# literals (no formatting at the call site) so static analysis can vouch
# for them; the table names come only from this module.
_VAULT_SCOPED_CLEANUP_SQL = {
    "wiki_pages": "DELETE FROM wiki_pages WHERE vault_id = ?",
    "wiki_entities": "DELETE FROM wiki_entities WHERE vault_id = ?",
    "wiki_claims": "DELETE FROM wiki_claims WHERE vault_id = ?",
    "wiki_relations": "DELETE FROM wiki_relations WHERE vault_id = ?",
    "wiki_lint_findings": "DELETE FROM wiki_lint_findings WHERE vault_id = ?",
    "wiki_page_versions": "DELETE FROM wiki_page_versions WHERE vault_id = ?",
    "wiki_page_links": "DELETE FROM wiki_page_links WHERE vault_id = ?",
    "wiki_activity_log": "DELETE FROM wiki_activity_log WHERE vault_id = ?",
    "wiki_compile_jobs": "DELETE FROM wiki_compile_jobs WHERE vault_id = ?",
    "kms_entries": "DELETE FROM kms_entries WHERE vault_id = ?",
    "kms_compile_jobs": "DELETE FROM kms_compile_jobs WHERE vault_id = ?",
    "document_reindex_jobs": "DELETE FROM document_reindex_jobs WHERE vault_id = ?",
    "canvas_artifacts": "DELETE FROM canvas_artifacts WHERE vault_id = ?",
}
_VAULT_SCOPED_CLEANUP_TABLES = tuple(_VAULT_SCOPED_CLEANUP_SQL)

_VAULT_WITH_COUNTS_SQL = """
    SELECT v.id, v.name, v.description, v.created_at, v.updated_at,
           (SELECT COUNT(*) FROM files f WHERE f.vault_id = v.id) as file_count,
           (SELECT COUNT(*) FROM memories m WHERE m.vault_id = v.id) as memory_count,
           (SELECT COUNT(*) FROM chat_sessions cs WHERE cs.vault_id = v.id) as session_count,
           v.org_id,
           v.visibility,
           v.enrichment_enabled,
           v.multimodal_provider_enabled
    FROM vaults v
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
            JOIN groups g ON g.id = gm.group_id
            WHERE gm.user_id = ?
              AND EXISTS (
                  SELECT 1 FROM org_members om
                  WHERE om.org_id = g.org_id AND om.user_id = ?
              )
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
        lambda: conn.execute(union_query, (user_id, user_id, user_id, user_id, user_id)).fetchall()
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
    _csrf_token: str = Depends(csrf_protect),
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

    if vault_id is None or vault_id <= 0:
        try:
            await asyncio.to_thread(conn.rollback)
        except Exception:
            pass
        raise HTTPException(status_code=500, detail="Failed to create vault")

    try:
        await asyncio.to_thread(
            conn.execute,
            # granted_by records the creating caller, identical to the
            # grant route in vault_members.py — the vault-creation writer is
            # the same provenance class (issue #721 sweep, C14 sibling).
            "INSERT INTO vault_members (vault_id, user_id, permission, granted_by) VALUES (?, ?, ?, ?)",
            (vault_id, user["id"], "admin", user["id"]),
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
    http_request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(require_vault_permission("admin")),
    _csrf_token: str = Depends(csrf_protect),
):
    """
    Update vault name/description.

    Updates the name and/or description of the vault with the given id.
    Returns 404 if not found.
    Returns 409 if new name conflicts with an existing vault.
    """
    # Check if vault exists
    row = await asyncio.to_thread(
        lambda: conn.execute("SELECT id, name, visibility FROM vaults WHERE id = ?", (vault_id,)).fetchone()
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

    if request.visibility is not None:
        # Visibility-carrying updates are security-relevant events (issue
        # #722 AC1): visibility widens or narrows who can read the vault.
        # Recorded after the commit via the standard tamper-evident helper;
        # the metadata carries old and new values even when equal.
        await safe_record_security_event(
            conn,
            event_type="vault.visibility_updated",
            actor=user,
            request=http_request,
            metadata={
                "vault_id": vault_id,
                "old_visibility": row[2],
                "new_visibility": request.visibility,
            },
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


def _log_vault_dir_rmtree_error(func, path, exc_info):  # noqa: ANN001
    """Log, never raise, per-child failures of deleted-vault directory removal.

    Passing this as ``shutil.rmtree(onerror=...)`` keeps the cleanup
    best-effort (one locked file must not abort the sweep) while making every
    leftover visible in the log instead of silently swallowed.
    """
    logger.warning(
        "Failed to remove %s during deleted-vault directory cleanup: %s",
        path,
        exc_info[1],
    )


@router.delete("/vaults/{vault_id}")
async def delete_vault(
    vault_id: int,
    http_request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    vector_store: VectorStore = Depends(get_vector_store),
    user: dict = Depends(require_vault_permission("admin")),
    _csrf_token: str = Depends(csrf_protect),
):
    """
    Delete vault with cascade cleanup.

    Deletes the vault and all associated data:
    - All files in the vault (and their chunks in vector store)
    - All chat sessions and messages (via cascade)
    - All wiki and KMS content and job rows scoped to the vault (the
      vault-scoped tables without an FK to vaults; see
      _VAULT_SCOPED_CLEANUP_TABLES), and pending unified lease jobs
    - Vector chunks for this vault (with durable vector_delete_pending
      tombstones written in the same transaction, before COMMIT, so a
      post-commit crash or cancellation can always re-drive the cleanup)
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

    # Prepare the Draft Room purge before the row deletion below: drafts.vault_id
    # cascades on vault delete (ON DELETE CASCADE), losing the rows that name
    # the files to remove. `prepare_purge_for_vault` only tombstones input
    # files on disk — it touches no draft rows and opens no transaction of its
    # own, so it cannot nest inside (or conflict with) the BEGIN IMMEDIATE
    # below. The tombstones are only permanently discarded after that
    # transaction COMMITS (via commit_purge); if it rolls back instead, they
    # are restored (via restore_purge) so the vault's drafts keep their bytes.
    # Not gated on draft_room_enabled (SPEC 9.2) — cleanup must run even if
    # disabled.
    draft_deletion = DraftDeletionService(
        DraftInputStorage(settings.data_dir / "draft-room")
    )
    draft_purge_plan = None
    try:
        draft_store = DraftStore(conn)
        draft_purge_plan = await asyncio.to_thread(
            draft_deletion.prepare_purge_for_vault, draft_store, vault_id
        )
        logger.info(
            "draft_room_vault_purge_prepared vault_id=%s draft_count=%s",
            vault_id,
            len(draft_purge_plan.draft_owner_pairs),
        )
    except (sqlite3.Error, OSError, RuntimeError, DraftInputStorageError) as e:
        # Never block the vault delete, matching the vector-store precedent.
        # DraftInputStorageError is listed explicitly: it derives from Exception,
        # not OSError, so a tombstone/resolve failure would otherwise escape this
        # handler and 500 the delete -- the opposite of what this block promises.
        logger.warning(
            "draft_room_vault_purge_prepare_failed vault_id=%s reason=%s",
            vault_id,
            type(e).__name__,
        )

    # W21 (C19 / RC-17): the vault's file ids, collected inside the
    # transaction below so a post-commit vector-store failure can record
    # per-file `vector_delete_pending` tombstones for the sweep. The stored
    # paths are collected alongside them for the post-commit disk GC (issue
    # #692 / T1-21-S-04): after `DELETE FROM files` the paths are
    # unrecoverable, and the uploaded bytes they name must not outlive the
    # vault the way single-document delete already prevents.
    vault_file_ids: List[int] = []
    vault_file_paths: List[str] = []

    try:
        # Start transaction. BEGIN IMMEDIATE acquires the write lock up front
        # so a concurrent deletion/mutation of the same vault cannot interleave
        # between our reads and writes (avoids SQLITE_BUSY mid-cascade).
        await asyncio.to_thread(conn.execute, "BEGIN IMMEDIATE")

        # Collect the vault's file ids and stored paths BEFORE deleting their
        # rows: they are needed for the post-commit vector reconciliation and
        # disk GC below, and after `DELETE FROM files` they are unrecoverable.
        file_id_rows = await asyncio.to_thread(
            lambda: conn.execute(
                "SELECT id, file_path FROM files WHERE vault_id = ?", (vault_id,)
            ).fetchall()
        )
        vault_file_ids = [int(row[0]) for row in file_id_rows]
        vault_file_paths = [str(row[1]) for row in file_id_rows if row[1]]

        # Issue #723 (T1-21-S2-02): record one durable vector_delete_pending
        # tombstone per file INSIDE this transaction, before COMMIT — the
        # documents.py single-delete pattern extended to vault deletion. The
        # tombstones make the post-commit vector reconciliation re-driveable
        # across a crash or a BaseException (asyncio.CancelledError escapes
        # `except Exception`); they roll back with everything else if the
        # transaction fails, and are removed again once delete_by_vault
        # succeeds below.
        if vault_file_ids:
            await asyncio.to_thread(
                lambda: conn.executemany(
                    "INSERT INTO vector_delete_pending (file_id, vault_id) "
                    "VALUES (?, ?)",
                    [(fid, vault_id) for fid in vault_file_ids],
                )
            )

        # W21 (C19): vector chunks are NOT deleted here. The relational
        # deletion is committed first (durable intent) and the vector store
        # is reconciled AFTER the commit — a pre-commit SQL failure rolls
        # back with BOTH stores intact, and a vector failure after commit is
        # tombstoned for the hourly sweep + startup
        # retry_pending_vector_deletes instead of being swallowed.

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

        # Tombstone every binary asset path within the vault, in the same
        # transaction, BEFORE deleting the files rows. The collect query reads
        # `document_assets` joined to `files`; deleting files first would cascade
        # the asset rows away and the sweep would never learn about these bytes
        # (issue #460, final-critic finding 1).
        from app.services import artifact_store as _artifact_store

        vault_assets = await asyncio.to_thread(
            _artifact_store.devault_asset_rel_paths, conn, vault_id
        )
        if vault_assets:
            _artifact_store.enqueue_asset_cleanup(
                conn,
                file_id=0,
                vault_id=vault_id,
                rel_paths=vault_assets,
                generation_hash=None,
            )

        # Advisory near-duplicate groupings are vault-scoped; clear them with
        # the files rows so a reused files.id rowid cannot inherit a stale
        # group from a deleted file.
        await asyncio.to_thread(
            conn.execute,
            "DELETE FROM document_near_dups WHERE vault_id = ?",
            (vault_id,),
        )

        # Issue #723 (T1-21-S-03 / T1-21-S-07): every vault-scoped table with
        # no FK to vaults keeps rows past the vault delete unless they are
        # removed here, in the same transaction. The NULL-vault rows some of
        # them carry (e.g. global reindex jobs) are never matched by this
        # predicate and survive by design.
        for cleanup_sql in _VAULT_SCOPED_CLEANUP_SQL.values():
            await asyncio.to_thread(conn.execute, cleanup_sql, (vault_id,))

        # Issue #723 (T1-21-S-07): cancel the vault's pending/running jobs on
        # the shared unified lease table. The legacy job tables were fully
        # deleted above; the unified `jobs` table is shared across queues and
        # vaults, so its rows are cancelled instead — the same
        # status/completed_at pair and json_valid-guarded vault match the
        # KmsStore lease cancel uses. A fenced worker write on a cancelled
        # row then fails on status by design (issue #559). Skipped when the
        # lease table was never created (lease mode never used).
        has_jobs_table = await asyncio.to_thread(
            lambda: conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'jobs'"
            ).fetchone()
        )
        if has_jobs_table:
            await asyncio.to_thread(
                conn.execute,
                """
                UPDATE jobs SET status = 'cancelled', completed_at = ?
                WHERE queue IN ('wiki', 'kms', 'reindex')
                  AND CASE WHEN json_valid(payload_json)
                       THEN CAST(json_extract(payload_json, '$.vault_id') AS INTEGER)
                       ELSE NULL END = ?
                  AND status IN ('pending', 'running')
                """,
                (datetime.now(timezone.utc).isoformat(), vault_id),
            )

        # Delete files
        await asyncio.to_thread(
            conn.execute, "DELETE FROM files WHERE vault_id = ?", (vault_id,)
        )

        # Draft Room evidence freshness (SPEC section 12.6, issue #516
        # DRAFT-001): every purged document invalidates the evidence rows
        # citing it — the same hook, in this same transaction, that the
        # single-document delete path uses. Best effort: the hook is wrapped
        # so it cannot raise into the purge.
        from app.services.draft_evidence_freshness import on_document_changed

        for purged_file_id in vault_file_ids:
            await asyncio.to_thread(
                on_document_changed,
                conn,
                file_id=purged_file_id,
                new_content_sha256=None,
            )

        # Delete the vault itself. drafts.vault_id carries ON DELETE CASCADE
        # to vaults(id) (see app/models/database.py _DRAFT_ROOM_CORE_DDL), and
        # draft_inputs/draft_jobs/draft_revisions/draft_events all cascade
        # from drafts in turn — so this one statement also removes every
        # draft row purged above with no separate DELETE FROM drafts needed.
        # The rowcount gates the audit event below: a concurrent delete that
        # won the race must not double-record vault.deleted (review F-031a).
        def _delete_vault_row() -> int:
            return conn.execute(
                "DELETE FROM vaults WHERE id = ?", (vault_id,)
            ).rowcount

        deleted_vault_rows = await asyncio.to_thread(_delete_vault_row)

        # Commit transaction (durable deletion intent)
        await asyncio.to_thread(conn.commit)
    except HTTPException:
        await asyncio.to_thread(lambda: conn.rollback())
        if draft_purge_plan is not None:
            try:
                await asyncio.to_thread(draft_deletion.restore_purge, draft_purge_plan)
            except Exception as restore_exc:
                logger.error(
                    "draft_room_vault_purge_restore_failed vault_id=%s reason=%s",
                    vault_id,
                    type(restore_exc).__name__,
                )
        raise
    except (sqlite3.Error, OSError, RuntimeError):
        await asyncio.to_thread(lambda: conn.rollback())
        if draft_purge_plan is not None:
            try:
                await asyncio.to_thread(draft_deletion.restore_purge, draft_purge_plan)
            except Exception as restore_exc:
                logger.error(
                    "draft_room_vault_purge_restore_failed vault_id=%s reason=%s",
                    vault_id,
                    type(restore_exc).__name__,
                )
        logger.exception("Error deleting vault %d", vault_id)
        raise HTTPException(status_code=500, detail="Failed to delete vault")
    except BaseException:
        # Issue #723 (T1-21-S2-04): a BaseException (asyncio.CancelledError,
        # KeyboardInterrupt, an exception type outside the two handlers above)
        # must never strand the tombstoned Draft Room bytes while the rows
        # survive. Gating on conn.in_transaction is race-free: before COMMIT
        # the transaction is open (rollback + restore); after COMMIT it is
        # closed, and restoring bytes that belong to already-deleted drafts
        # would be wrong — those tombstones stay in .trash and are discarded
        # by the startup reconcile.
        if conn.in_transaction:
            await asyncio.to_thread(lambda: conn.rollback())
            if draft_purge_plan is not None:
                try:
                    await asyncio.to_thread(
                        draft_deletion.restore_purge, draft_purge_plan
                    )
                except Exception as restore_exc:
                    logger.error(
                        "draft_room_vault_purge_restore_failed vault_id=%s reason=%s",
                        vault_id,
                        type(restore_exc).__name__,
                    )
        raise

    # Security-relevant event recorded after the relational delete committed
    # (issue #722 AC1) — the same record-after-real-mutation ordering the
    # Draft Room deletions use, so the row only ever reflects a durable
    # deletion. Gated on the DELETE's own rowcount so a concurrent delete
    # that already removed the row records nothing (review F-031a).
    if deleted_vault_rows > 0:
        await safe_record_security_event(
            conn,
            event_type="vault.deleted",
            actor=user,
            request=http_request,
            metadata={"vault_id": vault_id, "name": vault_name},
        )

    # -- Post-commit vector reconciliation (W21 / C19). -------------------
    # The relational delete is durable now; destroy the vault's vectors. On
    # failure, record one `vector_delete_pending` tombstone per collected
    # file (the same insert shape the document-delete path uses) so the
    # hourly sweep and startup retry_pending_vector_deletes reconcile the
    # orphaned chunks. This can never roll back the committed rows.
    try:
        deleted_chunks = await vector_store.delete_by_vault(str(vault_id))
        logger.info(
            "Deleted %d chunks from vector store for vault_id %s",
            deleted_chunks,
            vault_id,
        )
        # Issue #723 (T1-21-S2-02): the intent recorded before COMMIT is now
        # fulfilled — drop those tombstone rows again so the pending table
        # only ever holds unfulfilled intents for the sweep. A crash between
        # the vector delete and this cleanup leaves a stale row the sweep
        # re-drives as a no-op.
        if vault_file_ids:

            def _clear_vector_delete_tombstones() -> None:
                conn.execute(
                    "DELETE FROM vector_delete_pending WHERE vault_id = ? "
                    "AND file_id IN (SELECT value FROM json_each(?))",
                    (vault_id, json.dumps(vault_file_ids)),
                )
                conn.commit()

            await asyncio.to_thread(_clear_vector_delete_tombstones)
    except Exception as e:  # noqa: BLE001 — reconcile, never fail the response
        # The in-transaction tombstones written before COMMIT stay in place:
        # the hourly sweep and startup retry_pending_vector_deletes reconcile
        # the orphaned chunks from them.
        logger.warning(
            "Error deleting chunks from vector store for vault %s; leaving "
            "%d in-transaction vector_delete_pending tombstone(s) for the "
            "sweep: %s",
            vault_id,
            len(vault_file_ids),
            e,
        )

    if draft_purge_plan is not None:
        try:
            await asyncio.to_thread(draft_deletion.commit_purge, draft_purge_plan)
        except Exception as e:
            # Bytes are already tombstoned in `.trash`; failing to finish
            # discarding them must not turn a successful vault delete
            # into an error response.
            logger.warning(
                "draft_room_vault_purge_commit_failed vault_id=%s reason=%s",
                vault_id,
                type(e).__name__,
            )

    # -- Post-commit disk GC (issue #692 / T1-21-S-04). ---------------------
    # Parity with single-document delete: the rows are durably gone, so the
    # uploaded originals they named are unreachable orphans now. Unlink each
    # one through the same root-containment helper the document-delete path
    # uses, THEN remove the vault's whole storage directory. Order matters:
    # _unlink_document_file resolves the vault's upload/document roots, whose
    # accessors recreate missing directories, so running the rmtree first
    # would let the unlinks rebuild data/vaults/{id}/ after its removal.
    try:
        from app.api.routes.documents import _unlink_document_file

        for stored_path in vault_file_paths:
            await asyncio.to_thread(_unlink_document_file, stored_path, vault_id)

        vault_dir = settings.data_dir / "vaults" / str(vault_id)
        if vault_dir.is_dir():
            await asyncio.to_thread(
                shutil.rmtree, vault_dir, False, _log_vault_dir_rmtree_error
            )
    except Exception as e:  # noqa: BLE001 — GC must not fail the response
        logger.warning(
            "Post-commit disk cleanup for deleted vault %s incomplete: %s",
            vault_id,
            e,
        )

    return {
        "message": f"Vault '{vault_name}' (id: {vault_id}) deleted successfully"
    }


@router.put("/vaults/{vault_id}/enrichment-toggle", response_model=VaultResponse)
async def toggle_vault_enrichment(
    vault_id: int,
    request: VaultEnrichmentToggleRequest,
    conn: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(require_vault_permission("admin")),
    _csrf_token: str = Depends(csrf_protect),
):
    """
    Set or clear the per-vault enrichment override.

    Requires admin permission on the vault.
    - enabled=true  → vault-level ON (overrides global)
    - enabled=false → vault-level OFF (overrides global)
    - enabled=null → clears override, falls back to global

    Returns the updated vault with effective_enrichment_enabled computed.
    """
    # Check vault exists
    row = await asyncio.to_thread(
        lambda: conn.execute(
            "SELECT id FROM vaults WHERE id = ?", (vault_id,)
        ).fetchone()
    )
    if row is None:
        raise HTTPException(
            status_code=404, detail=f"Vault with id {vault_id} not found"
        )

    # Apply the override (NULL to clear, 1 to enable, 0 to disable)
    if request.enabled is None:
        new_value: Optional[int] = None
    else:
        new_value = 1 if request.enabled else 0

    await asyncio.to_thread(
        conn.execute,
        "UPDATE vaults SET enrichment_enabled = ? WHERE id = ?",
        (new_value, vault_id),
    )
    await asyncio.to_thread(conn.commit)

    # Return updated vault
    vault = await _fetch_vault_with_counts(conn, vault_id, user)
    if vault is None:
        raise HTTPException(
            status_code=404, detail=f"Vault with id {vault_id} not found"
        )
    return vault


@router.put("/vaults/{vault_id}/multimodal-provider-toggle", response_model=VaultResponse)
async def toggle_vault_multimodal_provider(
    vault_id: int,
    request: VaultMultimodalToggleRequest,
    conn: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(require_vault_permission("admin")),
    _csrf_token: str = Depends(csrf_protect),
):
    """Set or clear the per-vault multimodal provider opt-in (issue #461).

    Requires admin permission on the vault. This is a *vault authorization*
    gate only; outbound calls additionally require the global
    multimodal_enrichment_enabled switch, exact-origin allowlisting, and SSRF
    safety (evaluated at enqueue and immediately before every provider call).
    - enabled=true  → vault-level opt-in (required for egress)
    - enabled=false → vault-level opt-out
    - enabled=null  → clears override; fail-closed for egress (not opted in)

    Returns the updated vault with effective_multimodal_enabled computed.
    """
    row = await asyncio.to_thread(
        lambda: conn.execute(
            "SELECT id FROM vaults WHERE id = ?", (vault_id,)
        ).fetchone()
    )
    if row is None:
        raise HTTPException(
            status_code=404, detail=f"Vault with id {vault_id} not found"
        )

    if request.enabled is None:
        new_value: Optional[int] = None
    else:
        new_value = 1 if request.enabled else 0

    await asyncio.to_thread(
        conn.execute,
        "UPDATE vaults SET multimodal_provider_enabled = ? WHERE id = ?",
        (new_value, vault_id),
    )
    await asyncio.to_thread(conn.commit)

    vault = await _fetch_vault_with_counts(conn, vault_id, user)
    if vault is None:
        raise HTTPException(
            status_code=404, detail=f"Vault with id {vault_id} not found"
        )
    return vault


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
    http_request: Request,
    user: dict = Depends(require_vault_permission("admin")),
    conn: sqlite3.Connection = Depends(get_db),
    _csrf_token: str = Depends(csrf_protect),
):
    """
    Update which groups have access to this vault.

    Replaces the current group access list with the provided group_ids.
    Validates all group_ids exist and, when the vault belongs to an
    organization, that each group belongs to the same organization. POLICY
    (issue #721 AC9): a vault whose org_id is NULL is org-less/global and
    accepts groups from ANY organization — the pinned, explicitly-decided
    behavior (test_allows_null_org_vault_to_accept_any_org_group_on_replace
    and the sibling POST route's test_allows_null_org_id_vault_accepts_any_
    org_group); a vault WITH an org rejects groups from a different one.
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
        # Clearing all group access is the most security-relevant form of
        # this mutation — it is audited exactly like a replace (issue #722
        # AC1, both return paths).
        await safe_record_security_event(
            conn,
            event_type="vault.group_access_replaced",
            actor=user,
            request=http_request,
            metadata={"vault_id": vault_id, "group_ids": []},
        )
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

    # Security-relevant event on the replace path, recorded after the commit
    # (issue #722 AC1 — the clear-all early return above records its own).
    await safe_record_security_event(
        conn,
        event_type="vault.group_access_replaced",
        actor=user,
        request=http_request,
        metadata={
            "vault_id": vault_id,
            "group_ids": [item.group_id for item in access_items],
        },
    )

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
