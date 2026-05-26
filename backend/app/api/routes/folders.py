"""
Folder API routes for document organization.

User-curated, vault-scoped folder hierarchy (nested via parent_folder_id) and
moving documents between folders. All endpoints enforce vault read/write
permissions, mirroring the tag routes. Mutating endpoints are CSRF-protected.
"""

import logging
import sqlite3
from dataclasses import asdict
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.api.deps import evaluate_policy, get_current_active_user, get_db
from app.security import csrf_protect
from app.services.folder_store import (
    _UNSET,
    FolderCycleError,
    FolderDuplicateError,
    FolderNotFoundError,
    FolderStore,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/folders", tags=["folders"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _require_vault_read(user: dict, vault_id: int) -> None:
    if not await evaluate_policy(user, "vault", vault_id, "read"):
        raise HTTPException(status_code=403, detail="No read access to this vault")


async def _require_vault_write(user: dict, vault_id: int) -> None:
    if not await evaluate_policy(user, "vault", vault_id, "write"):
        raise HTTPException(status_code=403, detail="No write access to this vault")


def _folder_vault_id(db: sqlite3.Connection, folder_id: int) -> Optional[int]:
    db.row_factory = sqlite3.Row
    row = db.execute(
        "SELECT vault_id FROM folders WHERE id = ?", (folder_id,)
    ).fetchone()
    return row["vault_id"] if row else None


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class FolderCreateRequest(BaseModel):
    vault_id: int
    name: str = Field(..., min_length=1, max_length=200)
    description: str = Field("", max_length=2000)
    parent_folder_id: Optional[int] = None


class FolderUpdateRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=200)
    description: Optional[str] = Field(None, max_length=2000)
    # parent_folder_id is intentionally nullable: a client may set it to null to
    # move the folder to the root. We distinguish "field omitted" from
    # "explicit null" via model_fields_set in the handler.
    parent_folder_id: Optional[int] = None


class FolderMoveRequest(BaseModel):
    vault_id: int
    file_ids: list[int] = Field(..., min_length=1)
    # null => move the documents to the root (unfiled).
    folder_id: Optional[int] = None


# ---------------------------------------------------------------------------
# Folder CRUD
# ---------------------------------------------------------------------------


@router.get("")
async def list_folders(
    vault_id: int = Query(..., description="Vault ID"),
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
):
    await _require_vault_read(user, vault_id)
    store = FolderStore(db)
    return {"folders": [asdict(f) for f in store.list_folders(vault_id)]}


@router.post("", status_code=201)
async def create_folder(
    request: FolderCreateRequest,
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
    _csrf_token: str = Depends(csrf_protect),
):
    await _require_vault_write(user, request.vault_id)
    store = FolderStore(db)
    try:
        folder = store.create_folder(
            request.vault_id,
            request.name,
            request.description,
            request.parent_folder_id,
        )
    except FolderNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except FolderDuplicateError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return asdict(folder)


@router.put("/{folder_id}")
async def update_folder(
    folder_id: int,
    request: FolderUpdateRequest,
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
    _csrf_token: str = Depends(csrf_protect),
):
    vault_id = _folder_vault_id(db, folder_id)
    if vault_id is None:
        raise HTTPException(status_code=404, detail="Folder not found")
    await _require_vault_write(user, vault_id)
    store = FolderStore(db)
    # Only reparent when the caller actually included parent_folder_id.
    reparent = "parent_folder_id" in request.model_fields_set
    parent_arg = request.parent_folder_id if reparent else _UNSET
    try:
        updated = store.update_folder(
            folder_id,
            vault_id,
            name=request.name,
            description=request.description,
            parent_folder_id=parent_arg,
        )
    except FolderNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except FolderCycleError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except FolderDuplicateError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    if not updated:
        raise HTTPException(status_code=404, detail="Folder not found")
    return asdict(updated)


@router.delete("/{folder_id}", status_code=204)
async def delete_folder(
    folder_id: int,
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
    _csrf_token: str = Depends(csrf_protect),
):
    vault_id = _folder_vault_id(db, folder_id)
    if vault_id is None:
        raise HTTPException(status_code=404, detail="Folder not found")
    await _require_vault_write(user, vault_id)
    store = FolderStore(db)
    store.delete_folder(folder_id, vault_id)


# ---------------------------------------------------------------------------
# Document assignment
# ---------------------------------------------------------------------------


@router.post("/move")
async def move_documents(
    request: FolderMoveRequest,
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
    _csrf_token: str = Depends(csrf_protect),
):
    """Move one or more documents into a folder (or to root when folder_id is
    null). Both the target folder and the files are scoped to the vault."""
    await _require_vault_write(user, request.vault_id)
    store = FolderStore(db)
    try:
        moved = store.move_documents(
            request.vault_id, request.file_ids, request.folder_id
        )
    except FolderNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"moved": moved}
