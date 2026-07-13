"""
Tag API routes for document organization.

User-curated, vault-scoped tags and their assignment to documents. All
endpoints enforce vault read/write permissions, mirroring the KMS routes.
Mutating endpoints are CSRF-protected.
"""

import logging
import sqlite3
from dataclasses import asdict
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.api.deps import (
    get_current_active_user,
    get_current_user_or_service_account,
    get_db,
    get_evaluate_policy,
)
from app.security import csrf_protect
from app.services.tag_store import TagDuplicateError, TagStore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tags", tags=["tags"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _require_vault_read(db: sqlite3.Connection, user: dict, vault_id: int) -> None:
    evaluate = get_evaluate_policy(db)
    if not await evaluate(user, "vault", vault_id, "read"):
        raise HTTPException(status_code=403, detail="No read access to this vault")


async def _require_vault_write(db: sqlite3.Connection, user: dict, vault_id: int) -> None:
    evaluate = get_evaluate_policy(db)
    if not await evaluate(user, "vault", vault_id, "write"):
        raise HTTPException(status_code=403, detail="No write access to this vault")


def _tag_vault_id(db: sqlite3.Connection, tag_id: int) -> Optional[int]:
    db.row_factory = sqlite3.Row
    row = db.execute("SELECT vault_id FROM tags WHERE id = ?", (tag_id,)).fetchone()
    return row["vault_id"] if row else None


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class TagCreateRequest(BaseModel):
    vault_id: int
    name: str = Field(..., min_length=1, max_length=100)
    color: str = Field("", max_length=32)


class TagUpdateRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    color: Optional[str] = Field(None, max_length=32)


class TagAssignRequest(BaseModel):
    vault_id: int
    file_ids: list[int] = Field(..., min_length=1)
    tag_ids: list[int] = Field(..., min_length=1)


class DocumentTagsSetRequest(BaseModel):
    vault_id: int
    tag_ids: list[int] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Tag CRUD
# ---------------------------------------------------------------------------


@router.get("")
async def list_tags(
    vault_id: int = Query(..., description="Vault ID"),
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
):
    await _require_vault_read(db, user, vault_id)
    store = TagStore(db)
    return {"tags": [asdict(t) for t in store.list_tags(vault_id)]}


@router.post("", status_code=201)
async def create_tag(
    request: TagCreateRequest,
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
    _csrf_token: str = Depends(csrf_protect),
):
    await _require_vault_write(db, user, request.vault_id)
    store = TagStore(db)
    try:
        tag = store.create_tag(request.vault_id, request.name, request.color)
    except TagDuplicateError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return asdict(tag)


@router.put("/{tag_id}")
async def update_tag(
    tag_id: int,
    request: TagUpdateRequest,
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
    _csrf_token: str = Depends(csrf_protect),
):
    vault_id = _tag_vault_id(db, tag_id)
    if vault_id is None:
        raise HTTPException(status_code=404, detail="Tag not found")
    await _require_vault_write(db, user, vault_id)
    store = TagStore(db)
    try:
        updated = store.update_tag(
            tag_id, vault_id, name=request.name, color=request.color
        )
    except TagDuplicateError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    if not updated:
        raise HTTPException(status_code=404, detail="Tag not found")
    return asdict(updated)


@router.delete("/{tag_id}", status_code=204)
async def delete_tag(
    tag_id: int,
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
    _csrf_token: str = Depends(csrf_protect),
):
    vault_id = _tag_vault_id(db, tag_id)
    if vault_id is None:
        raise HTTPException(status_code=404, detail="Tag not found")
    await _require_vault_write(db, user, vault_id)
    store = TagStore(db)
    store.delete_tag(tag_id, vault_id)


# ---------------------------------------------------------------------------
# Assignment
# ---------------------------------------------------------------------------


@router.post("/assign")
async def assign_tags(
    request: TagAssignRequest,
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
    _csrf_token: str = Depends(csrf_protect),
):
    """Bulk-assign one or more tags to one or more documents in a vault."""
    await _require_vault_write(db, user, request.vault_id)
    store = TagStore(db)
    created = store.assign_tags(request.vault_id, request.file_ids, request.tag_ids)
    return {"assigned": created}


@router.get("/documents/{file_id}")
async def list_document_tags(
    file_id: int,
    vault_id: int = Query(...),
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_user_or_service_account),
):
    await _require_vault_read(db, user, vault_id)
    db.row_factory = sqlite3.Row
    row = db.execute(
        "SELECT id FROM files WHERE id = ? AND vault_id = ?", (file_id, vault_id)
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Document not found in vault")
    store = TagStore(db)
    return {"tags": [asdict(t) for t in store.get_tags_for_document(file_id)]}


@router.put("/documents/{file_id}")
async def set_document_tags(
    file_id: int,
    request: DocumentTagsSetRequest,
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
    _csrf_token: str = Depends(csrf_protect),
):
    """Replace the full tag set for a single document."""
    await _require_vault_write(db, user, request.vault_id)
    db.row_factory = sqlite3.Row
    row = db.execute(
        "SELECT id FROM files WHERE id = ? AND vault_id = ?",
        (file_id, request.vault_id),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Document not found in vault")
    store = TagStore(db)
    tags = store.set_document_tags(request.vault_id, file_id, request.tag_ids)
    return {"tags": [asdict(t) for t in tags]}


@router.delete("/{tag_id}/documents/{file_id}", status_code=204)
async def unassign_tag(
    tag_id: int,
    file_id: int,
    vault_id: int = Query(...),
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
    _csrf_token: str = Depends(csrf_protect),
):
    await _require_vault_write(db, user, vault_id)
    store = TagStore(db)
    store.unassign_tag(vault_id, file_id, tag_id)
