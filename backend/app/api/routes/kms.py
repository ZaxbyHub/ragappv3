"""
KMS / Knowledge Management API routes.

User-curated documentation entries, scoped per vault. Entries may be authored
manually or compiled from ingested documents (source_type='document'). All
endpoints follow vault access permissions, mirroring the wiki routes.
"""

import logging
import sqlite3
from dataclasses import asdict
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.api.deps import evaluate_policy, get_current_active_user, get_db
from app.config import settings
from app.security import csrf_protect
from app.services.kms_store import KMSStore

logger = logging.getLogger(__name__)


async def require_kms_enabled() -> None:
    """Master switch (config.kms_enabled). When off, the entire KMS subsystem —
    reads, writes, and job creation — is unavailable, not just auto-ingest.

    Returns 503 (Service Unavailable) since the subsystem is intentionally
    turned off, not an authorization failure."""
    if not settings.kms_enabled:
        raise HTTPException(status_code=503, detail="KMS subsystem is disabled")


# Apply the master switch to every KMS route. CSRF is added per-route on the
# mutating endpoints below.
router = APIRouter(dependencies=[Depends(require_kms_enabled)])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _entry_dict(entry) -> dict:
    d = asdict(entry)
    d["tags"] = entry.tags
    return d


async def _require_vault_read(user: dict, vault_id: int) -> None:
    if not await evaluate_policy(user, "vault", vault_id, "read"):
        raise HTTPException(status_code=403, detail="No read access to this vault")


async def _require_vault_write(user: dict, vault_id: int) -> None:
    if not await evaluate_policy(user, "vault", vault_id, "write"):
        raise HTTPException(status_code=403, detail="No write access to this vault")


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class KMSEntryCreateRequest(BaseModel):
    vault_id: int
    title: str = Field(..., min_length=1, max_length=500)
    body: str = ""
    summary: str = Field("", max_length=2000)
    tags: list[str] = Field(default_factory=list)
    slug: Optional[str] = Field(None, min_length=1, max_length=500)
    status: str = "draft"


class KMSEntryUpdateRequest(BaseModel):
    title: Optional[str] = Field(None, min_length=1, max_length=500)
    body: Optional[str] = None
    summary: Optional[str] = Field(None, max_length=2000)
    tags: Optional[list[str]] = None
    slug: Optional[str] = Field(None, min_length=1, max_length=500)
    status: Optional[str] = None


_VALID_STATUSES = {"draft", "published", "archived"}


def _validate_status(status: Optional[str]) -> None:
    if status is not None and status not in _VALID_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid status {status!r}. Allowed: {sorted(_VALID_STATUSES)}",
        )


# ---------------------------------------------------------------------------
# Entry endpoints
# ---------------------------------------------------------------------------


@router.get("/kms/entries")
async def list_kms_entries(
    vault_id: int = Query(..., description="Vault ID"),
    status: Optional[str] = Query(None),
    tag: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
    _: None = Depends(require_kms_enabled),
):
    await _require_vault_read(user, vault_id)
    store = KMSStore(db)
    entries = store.list_entries(
        vault_id, status=status, tag=tag, search=search, page=page, per_page=per_page
    )
    total = store.count_entries(vault_id, status=status, tag=tag, search=search)
    return {
        "entries": [_entry_dict(e) for e in entries],
        "total": total,
        "page": page,
        "per_page": per_page,
    }


@router.post("/kms/entries", status_code=201)
async def create_kms_entry(
    request: KMSEntryCreateRequest,
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
    _: None = Depends(require_kms_enabled),
    _csrf_token: str = Depends(csrf_protect),
):
    await _require_vault_write(user, request.vault_id)
    _validate_status(request.status)
    store = KMSStore(db)
    try:
        entry = store.create_entry(
            vault_id=request.vault_id,
            title=request.title,
            body=request.body,
            summary=request.summary,
            tags=request.tags,
            slug=request.slug,
            source_type="manual",
            status=request.status,
            created_by=user.get("id"),
        )
    except Exception as e:
        logger.exception("Error creating KMS entry")
        raise HTTPException(status_code=400, detail=str(e))
    return _entry_dict(entry)


@router.get("/kms/entries/{entry_id}")
async def get_kms_entry(
    entry_id: int,
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
    _: None = Depends(require_kms_enabled),
):
    store = KMSStore(db)
    entry = store.get_entry(entry_id)
    if not entry:
        raise HTTPException(status_code=404, detail="KMS entry not found")
    await _require_vault_read(user, entry.vault_id)
    return _entry_dict(entry)


@router.put("/kms/entries/{entry_id}")
async def update_kms_entry(
    entry_id: int,
    request: KMSEntryUpdateRequest,
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
    _: None = Depends(require_kms_enabled),
    _csrf_token: str = Depends(csrf_protect),
):
    store = KMSStore(db)
    entry = store.get_entry(entry_id)
    if not entry:
        raise HTTPException(status_code=404, detail="KMS entry not found")
    await _require_vault_write(user, entry.vault_id)
    _validate_status(request.status)
    updates = request.model_dump(exclude_none=True)
    updated = store.update_entry(entry_id, entry.vault_id, **updates)
    if not updated:
        raise HTTPException(status_code=404, detail="KMS entry not found")
    return _entry_dict(updated)


@router.delete("/kms/entries/{entry_id}", status_code=204)
async def delete_kms_entry(
    entry_id: int,
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
    _: None = Depends(require_kms_enabled),
    _csrf_token: str = Depends(csrf_protect),
):
    store = KMSStore(db)
    entry = store.get_entry(entry_id)
    if not entry:
        raise HTTPException(status_code=404, detail="KMS entry not found")
    await _require_vault_write(user, entry.vault_id)
    store.delete_entry(entry_id, entry.vault_id)


# ---------------------------------------------------------------------------
# Search endpoint
# ---------------------------------------------------------------------------


@router.get("/kms/search")
async def search_kms(
    vault_id: int = Query(...),
    q: str = Query(..., min_length=1),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
    _: None = Depends(require_kms_enabled),
):
    await _require_vault_read(user, vault_id)
    store = KMSStore(db)
    entries = store.list_entries(vault_id, search=q, page=page, per_page=per_page)
    total = store.count_entries(vault_id, search=q)
    return {
        "query": q,
        "entries": [_entry_dict(e) for e in entries],
        "total": total,
        "page": page,
        "per_page": per_page,
    }


# ---------------------------------------------------------------------------
# Compile endpoints
# ---------------------------------------------------------------------------


@router.post("/kms/documents/{file_id}/compile", status_code=202)
async def compile_document_kms(
    file_id: int,
    vault_id: int = Query(...),
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
    _: None = Depends(require_kms_enabled),
    _csrf_token: str = Depends(csrf_protect),
):
    """Enqueue a KMS ingest job for an already-indexed document."""
    await _require_vault_write(user, vault_id)
    db.row_factory = sqlite3.Row
    file_row = db.execute(
        "SELECT id FROM files WHERE id = ? AND vault_id = ?",
        (file_id, vault_id),
    ).fetchone()
    if not file_row:
        raise HTTPException(
            status_code=404, detail=f"File {file_id} not found in vault {vault_id}"
        )
    store = KMSStore(db)
    job = store.create_job(
        vault_id=vault_id,
        trigger_type="ingest",
        trigger_id=f"file:{file_id}",
        input_json={"file_id": file_id, "vault_id": vault_id},
    )
    return {"job_id": job.id, "status": job.status}


@router.post("/kms/recompile", status_code=202)
async def recompile_vault_kms(
    vault_id: int = Query(...),
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
    _: None = Depends(require_kms_enabled),
    _csrf_token: str = Depends(csrf_protect),
):
    """Enqueue a settings_reindex job to recompile all document entries in a vault."""
    await _require_vault_write(user, vault_id)
    store = KMSStore(db)
    job = store.create_job(
        vault_id=vault_id,
        trigger_type="settings_reindex",
        trigger_id=f"vault:{vault_id}",
        input_json={"vault_id": vault_id},
    )
    return {"job_id": job.id, "status": job.status}


# ---------------------------------------------------------------------------
# Job status endpoints
# ---------------------------------------------------------------------------


@router.get("/kms/jobs")
async def list_kms_jobs(
    vault_id: int = Query(...),
    status: Optional[str] = Query(None),
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
    _: None = Depends(require_kms_enabled),
):
    await _require_vault_read(user, vault_id)
    store = KMSStore(db)
    jobs = store.list_jobs(vault_id, status=status)
    return {"jobs": [asdict(j) for j in jobs]}


@router.get("/kms/jobs/{job_id}")
async def get_kms_job(
    job_id: int,
    vault_id: int = Query(...),
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
    _: None = Depends(require_kms_enabled),
):
    await _require_vault_read(user, vault_id)
    store = KMSStore(db)
    job = store.get_job(job_id, vault_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    return asdict(job)
