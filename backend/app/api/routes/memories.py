"""
Memory API routes for CRUD operations on memories.

Provides endpoints for listing, creating, updating, deleting, and searching memories.
"""

import asyncio
import inspect
import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import List, Optional, Union

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.api.deps import (
    get_current_active_user,
    get_db,
    get_evaluate_policy,
    get_memory_store,
)
from app.config import settings
from app.limiter import limiter
from app.security import csrf_protect
from app.services.memory_store import MemoryRecord, MemoryStore, MemoryStoreError

logger = logging.getLogger(__name__)


router = APIRouter()


def _normalize_tags(tags: Optional[str]) -> Optional[str]:
    """Normalize tags to a valid JSON string."""
    if tags is None:
        return None
    tags = tags.strip()
    if not tags:
        return None
    # If it looks like a JSON array, validate it
    if tags.startswith("["):
        try:
            parsed = json.loads(tags)
            if not isinstance(parsed, list):
                raise ValueError("Tags must be a JSON array")
            return json.dumps(parsed)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON array for tags: {e}")
    # Otherwise, treat as comma-separated and convert to JSON array
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    return json.dumps(tag_list) if tag_list else None


def _normalize_tags_input(tags: Optional[Union[str, List[str]]]) -> Optional[str]:
    """Normalize tags from strings or lists into a JSON array string."""
    if tags is None:
        return None
    if isinstance(tags, list):
        cleaned = [
            str(tag).strip() for tag in tags if isinstance(tag, str) and tag.strip()
        ]
        return json.dumps(cleaned) if cleaned else None
    return _normalize_tags(tags)


class MemoryCreateRequest(BaseModel):
    """Request model for creating a new memory."""

    content: str = Field(
        ..., min_length=1, max_length=10000, description="Memory content"
    )
    category: Optional[str] = Field(
        None, max_length=255, description="Optional category"
    )
    tags: Optional[str] = Field(
        None,
        max_length=1000,
        description="Optional tags (JSON array or comma-separated)",
    )
    source: Optional[str] = Field(
        None, max_length=500, description="Optional source reference"
    )
    vault_id: Optional[int] = Field(
        None, description="Optional vault ID to scope this memory"
    )
    importance: float = Field(
        0.5, ge=0.0, le=1.0, description="Retention importance from 0 to 1"
    )
    expires_at: Optional[str] = Field(
        None, description="Optional ISO timestamp after which the memory expires"
    )

    @field_validator("tags", mode="before")
    @classmethod
    def validate_tags(cls, v):
        return _normalize_tags_input(v)

    @field_validator("expires_at")
    @classmethod
    def validate_expires_at(cls, v):
        if not v:
            return None
        try:
            parsed = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("expires_at must be an ISO timestamp") from exc
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed.isoformat()


class MemoryUpdateRequest(BaseModel):
    """Request model for updating an existing memory."""

    content: Optional[str] = Field(
        None, min_length=1, max_length=10000, description="Memory content"
    )
    category: Optional[str] = Field(
        None, max_length=255, description="Optional category"
    )
    tags: Optional[str] = Field(None, max_length=1000, description="Optional tags")
    source: Optional[str] = Field(
        None, max_length=500, description="Optional source reference"
    )
    importance: Optional[float] = Field(
        None, ge=0.0, le=1.0, description="Retention importance from 0 to 1"
    )
    expires_at: Optional[str] = Field(
        None, description="Optional ISO timestamp after which the memory expires"
    )

    @field_validator("tags", mode="before")
    @classmethod
    def validate_tags(cls, v):
        return _normalize_tags_input(v)

    @field_validator("expires_at")
    @classmethod
    def validate_expires_at(cls, v):
        if not v:
            return None
        try:
            parsed = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("expires_at must be an ISO timestamp") from exc
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed.isoformat()


class MemoryMetadata(BaseModel):
    """Metadata object for memory responses (frontend compatibility)."""

    category: Optional[str] = None
    tags: Optional[List[str]] = None
    source: Optional[str] = None


class MemoryResponse(BaseModel):
    """Response model for a memory record (frontend compatible)."""

    id: str
    content: str
    metadata: Optional[MemoryMetadata] = None
    score: Optional[float] = None
    importance: float = 0.5
    expires_at: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class MemoryListResponse(BaseModel):
    """Response model for listing memories."""

    memories: List[MemoryResponse]


class MemorySearchResponse(BaseModel):
    """Response model for memory search results (frontend compatible)."""

    results: List[MemoryResponse]
    total: int


class MemorySearchRequest(BaseModel):
    query: Optional[str] = Field(default="", description="Search query string")
    limit: int = Field(default=5, ge=1, le=100, description="Maximum number of results")
    vault_id: Optional[int] = Field(
        None, description="Optional vault ID to filter search"
    )


def _parse_tags_to_list(tags: Optional[str]) -> Optional[List[str]]:
    """Parse tags JSON string to list."""
    if not tags:
        return None
    try:
        parsed = json.loads(tags)
        if isinstance(parsed, list):
            return parsed
        # If JSON parsed but is not a list, fallback to string split
        return [t.strip() for t in str(parsed).split(",") if t.strip()]
    except json.JSONDecodeError:
        # Try comma-separated fallback
        return [t.strip() for t in tags.split(",") if t.strip()]
    return None


def _memory_record_to_response(
    record: MemoryRecord, score: Optional[float] = None
) -> MemoryResponse:
    """Convert a MemoryRecord to a MemoryResponse (frontend compatible format)."""
    metadata = (
        MemoryMetadata(
            category=record.category,
            tags=_parse_tags_to_list(record.tags),
            source=record.source,
        )
        if any([record.category, record.tags, record.source])
        else None
    )

    return MemoryResponse(
        id=str(record.id),
        content=record.content,
        metadata=metadata,
        score=score,
        importance=record.importance,
        expires_at=record.expires_at,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


async def _perform_memory_search(
    memory_store: MemoryStore, query: str, limit: int, vault_id: Optional[int] = None
) -> List[MemoryResponse]:
    try:
        records = await asyncio.to_thread(
            memory_store.search_memories, query=query, limit=limit, vault_id=vault_id
        )
    except MemoryStoreError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return [
        _memory_record_to_response(record, getattr(record, "score", None))
        for record in records
    ]


@router.get("/memories/", response_model=MemoryListResponse, include_in_schema=False)
@router.get("/memories", response_model=MemoryListResponse)
async def list_memories(
    vault_id: Optional[int] = Query(None, description="Filter by vault ID"),
    conn: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
):
    """
    List all memories.

    Returns a list of all memories with their id, content, category, tags, source,
    created_at, and updated_at fields.

    Authorization:
    - vault_id=N: requires read access to vault N (returns vault-scoped + global memories).
    - vault_id=None: admin/superadmin only (returns all memories across all vaults).
      Non-admin callers should specify vault_id explicitly.
    """
    if vault_id is not None:
        evaluate = get_evaluate_policy(conn)
        result = evaluate(user, "vault", vault_id, "read")
        if inspect.iscoroutine(result):
            result = await result
        if not result:
            raise HTTPException(status_code=403, detail="No read access to this vault")
        # Include global memories (vault_id IS NULL) alongside vault-scoped memories
        cursor = await asyncio.to_thread(
            conn.execute,
            """
            SELECT id, content, category, tags, source, importance, expires_at, created_at, updated_at
            FROM memories
            WHERE (vault_id = ? OR vault_id IS NULL)
              AND (expires_at IS NULL OR datetime(expires_at) > datetime('now'))
            ORDER BY created_at DESC
            """,
            (vault_id,),
        )
    else:
        # Listing across all vaults — restrict to admin/superadmin to prevent
        # cross-vault leakage. Non-admin users must specify a vault_id.
        if user.get("role") not in ("superadmin", "admin"):
            raise HTTPException(
                status_code=403,
                detail="Listing memories across all vaults requires admin access. Please specify a vault_id.",
            )
        cursor = await asyncio.to_thread(
            conn.execute,
            """
            SELECT id, content, category, tags, source, importance, expires_at, created_at, updated_at
            FROM memories
            WHERE expires_at IS NULL OR datetime(expires_at) > datetime('now')
            ORDER BY created_at DESC
            """,
        )
    rows = await asyncio.to_thread(cursor.fetchall)

    memories = []
    for row in rows:
        metadata = (
            MemoryMetadata(
                category=row[2],
                tags=_parse_tags_to_list(row[3]),
                source=row[4],
            )
            if any([row[2], row[3], row[4]])
            else None
        )
        memories.append(
            MemoryResponse(
                id=str(row[0]),
                content=row[1],
                metadata=metadata,
                importance=float(row[5] or 0.5),
                expires_at=row[6],
                created_at=row[7],
                updated_at=row[8],
            )
        )

    return MemoryListResponse(memories=memories)


@router.post("/memories/", response_model=MemoryResponse, include_in_schema=False)
@router.post("/memories", response_model=MemoryResponse)
@limiter.limit(settings.memory_mutation_rate_limit)
async def create_memory(
    request: Request,
    body: MemoryCreateRequest,
    memory_store: MemoryStore = Depends(get_memory_store),
    user: dict = Depends(get_current_active_user),
    evaluate=Depends(get_evaluate_policy),
    _csrf_token: str = Depends(csrf_protect),
):
    """
    Create a new memory.

    Uses MemoryStore.add_memory to add a new memory to the database.
    """
    if body.vault_id is not None:
        result = evaluate(user, "vault", body.vault_id, "write")
        if inspect.iscoroutine(result):
            result = await result
        if not result:
            raise HTTPException(status_code=403, detail="No write access to this vault")
    try:
        record = await asyncio.to_thread(
            memory_store.add_memory,
            content=body.content,
            category=body.category,
            tags=body.tags,
            source=body.source,
            vault_id=body.vault_id,
            importance=body.importance,
            expires_at=body.expires_at,
        )
    except MemoryStoreError as e:
        logger.exception(
            "MemoryStoreError in create_memory (content length: %d)",
            len(body.content),
        )
        raise HTTPException(status_code=400, detail=str(e))
    except sqlite3.Error as e:
        logger.exception(
            "Database error in create_memory (content length: %d)", len(body.content)
        )
        raise HTTPException(status_code=500, detail=f"Database error: {e}")
    except (ValueError, TypeError, RuntimeError) as e:
        logger.exception(
            "Unexpected error in create_memory (content length: %d)",
            len(body.content),
        )
        raise HTTPException(status_code=500, detail=f"Server error: {e}")

    return _memory_record_to_response(record)


@router.put("/memories/{memory_id}", response_model=MemoryResponse)
@limiter.limit(settings.memory_mutation_rate_limit)
async def update_memory(
    request: Request,
    memory_id: int,
    body: MemoryUpdateRequest,
    conn: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
    memory_store: MemoryStore = Depends(get_memory_store),
    _csrf_token: str = Depends(csrf_protect),
):
    """
    Update an existing memory.

    Updates content, category, tags, and/or source fields in the database.
    Returns 404 if the memory is not found.
    """
    try:
        # Check if memory exists and get vault_id
        cursor = await asyncio.to_thread(
            conn.execute, "SELECT id, vault_id FROM memories WHERE id = ?", (memory_id,)
        )
        row = await asyncio.to_thread(cursor.fetchone)
        if row is None:
            raise HTTPException(
                status_code=404, detail=f"Memory with id {memory_id} not found"
            )

        # Check vault write permission
        memory_vault_id = row[1]
        if memory_vault_id is not None:
            evaluate = get_evaluate_policy(conn)
            result = evaluate(user, "vault", memory_vault_id, "write")
            if inspect.iscoroutine(result):
                result = await result
            if not result:
                raise HTTPException(
                    status_code=403, detail="No write access to this vault"
                )

        # Build update query dynamically based on provided fields
        update_fields = []
        params = []

        if body.content is not None:
            update_fields.append("content = ?")
            params.append(body.content)
            # Clear stale embedding atomically with the content change so
            # semantic search never returns results based on the old content.
            # embed_and_store below recomputes best-effort after commit.
            if memory_store._has_embedding_columns(conn):
                update_fields.append("embedding = NULL")
                update_fields.append("embedding_model = NULL")
        if body.category is not None:
            update_fields.append("category = ?")
            params.append(body.category)
        if body.tags is not None:
            update_fields.append("tags = ?")
            params.append(body.tags)
        if body.source is not None:
            update_fields.append("source = ?")
            params.append(body.source)
        if body.importance is not None:
            update_fields.append("importance = ?")
            params.append(body.importance)
        if body.expires_at is not None:
            update_fields.append("expires_at = ?")
            params.append(body.expires_at)

        if not update_fields:
            # No fields to update, just fetch and return current record
            cursor = await asyncio.to_thread(
                conn.execute,
                """
                SELECT id, content, category, tags, source, importance, expires_at, created_at, updated_at
                FROM memories WHERE id = ?
                """,
                (memory_id,),
            )
            row = await asyncio.to_thread(cursor.fetchone)
            if row is None:
                raise HTTPException(
                    status_code=404, detail=f"Memory with id {memory_id} not found"
                )
            metadata = (
                MemoryMetadata(
                    category=row[2],
                    tags=_parse_tags_to_list(row[3]),
                    source=row[4],
                )
                if any([row[2], row[3], row[4]])
                else None
            )
            return MemoryResponse(
                id=str(row[0]),
                content=row[1],
                metadata=metadata,
                importance=float(row[5] or 0.5),
                expires_at=row[6],
                created_at=row[7],
                updated_at=row[8],
            )

        # Add memory_id to params
        params.append(memory_id)

        # Execute update
        sql = f"""
            UPDATE memories
            SET {", ".join(update_fields)}, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """
        await asyncio.to_thread(conn.execute, sql, params)
        await asyncio.to_thread(conn.commit)

        # If content changed, recompute embedding so semantic search stays fresh.
        # embed_and_store is best-effort: if the embedding service is down, FTS
        # fallback remains intact and the old embedding (now stale) is NULLed first
        # inside the method so searches won't use misleading vectors.
        if body.content is not None:
            try:
                await memory_store.embed_and_store(memory_id, body.content)
            except Exception:
                logger.warning(
                    "Could not recompute embedding for memory %d after content update",
                    memory_id,
                )
            # Mark wiki claims stale since the source memory content changed
            if memory_vault_id is not None:
                try:
                    from app.services.wiki_store import WikiStore as _WikiStore

                    await asyncio.to_thread(
                        lambda: _WikiStore(conn).mark_claims_stale_by_memory(memory_id, memory_vault_id)
                    )
                    # The mark_claims_stale_by_memory helper no longer commits
                    # (DD-C009 / #108) so the caller controls the transaction
                    # boundary. Commit here so the stale markings persist on
                    # update. Only commit when the helper succeeds to avoid
                    # persisting partial stale state on mid-loop exceptions.
                    await asyncio.to_thread(conn.commit)
                except Exception as _wiki_exc:
                    logger.warning("mark_claims_stale_by_memory(%d) failed: %s", memory_id, _wiki_exc)

        # Fetch updated record
        cursor = await asyncio.to_thread(
            conn.execute,
            """
            SELECT id, content, category, tags, source, importance, expires_at, created_at, updated_at
            FROM memories WHERE id = ?
            """,
            (memory_id,),
        )
        row = await asyncio.to_thread(cursor.fetchone)

        # Race condition fix: check if row is None after fetch
        if row is None:
            raise HTTPException(
                status_code=404, detail=f"Memory with id {memory_id} not found"
            )

        metadata = (
            MemoryMetadata(
                category=row[2],
                tags=_parse_tags_to_list(row[3]),
                source=row[4],
            )
            if any([row[2], row[3], row[4]])
            else None
        )
        return MemoryResponse(
            id=str(row[0]),
            content=row[1],
            metadata=metadata,
            importance=float(row[5] or 0.5),
            expires_at=row[6],
            created_at=row[7],
            updated_at=row[8],
        )
    except (sqlite3.Error, OSError) as e:
        logger.error(f"Database error during memory update: {e}")
        await asyncio.to_thread(lambda: conn.rollback())
        raise HTTPException(status_code=500, detail=f"Database error: {e}")


@router.delete("/memories/{memory_id}")
@limiter.limit(settings.memory_mutation_rate_limit)
async def delete_memory(
    request: Request,
    memory_id: int,
    conn: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
    _csrf_token: str = Depends(csrf_protect),
):
    """
    Delete a memory.

    Deletes the memory with the given id from the database.
    Returns 404 if the memory is not found.
    """
    # Check if memory exists and get vault_id
    cursor = await asyncio.to_thread(
        conn.execute, "SELECT id, vault_id FROM memories WHERE id = ?", (memory_id,)
    )
    row = await asyncio.to_thread(cursor.fetchone)
    if row is None:
        raise HTTPException(
            status_code=404, detail=f"Memory with id {memory_id} not found"
        )

    # Check vault admin permission
    memory_vault_id = row[1]
    if memory_vault_id is not None:
        evaluate = get_evaluate_policy(conn)
        result = evaluate(user, "vault", memory_vault_id, "admin")
        if inspect.iscoroutine(result):
            result = await result
        if not result:
            raise HTTPException(status_code=403, detail="No admin access to this vault")

    # Mark wiki claims stale before removing the memory record.
    # Use a savepoint so any partial stale state from a mid-loop exception
    # is rolled back before the DELETE commits — preventing orphan
    # lint findings on rows that still exist (DD-C009 / #108).
    if memory_vault_id is not None:
        await asyncio.to_thread(conn.execute, "SAVEPOINT stale_marking")
        try:
            from app.services.wiki_store import WikiStore as _WikiStore
            await asyncio.to_thread(
                lambda: _WikiStore(conn).mark_claims_stale_by_memory(memory_id, memory_vault_id)
            )
            await asyncio.to_thread(conn.execute, "RELEASE SAVEPOINT stale_marking")
        except Exception as _wiki_exc:
            await asyncio.to_thread(conn.execute, "ROLLBACK TO SAVEPOINT stale_marking")
            logger.warning("mark_claims_stale_by_memory(%d) failed: %s", memory_id, _wiki_exc)

    # Delete the memory
    await asyncio.to_thread(
        conn.execute, "DELETE FROM memories WHERE id = ?", (memory_id,)
    )
    await asyncio.to_thread(conn.commit)

    return {"message": f"Memory {memory_id} deleted successfully", "forgotten": True}


async def _authorize_memory_search(user: dict, vault_id: Optional[int], evaluate) -> None:
    """Enforce vault read access for memory search/list operations.

    - vault_id=N → require read access on vault N.
    - vault_id=None → admin/superadmin only (broad search across all vaults).
    """
    if vault_id is not None:
        result = evaluate(user, "vault", vault_id, "read")
        if inspect.iscoroutine(result):
            result = await result
        if not result:
            raise HTTPException(status_code=403, detail="No read access to this vault")
    else:
        if user.get("role") not in ("superadmin", "admin"):
            raise HTTPException(
                status_code=403,
                detail="Searching memories across all vaults requires admin access. Please specify a vault_id.",
            )


@router.get("/memories/search", response_model=MemorySearchResponse)
async def search_memories(
    query: str = Query(..., min_length=1, description="Search query string"),
    limit: int = Query(5, ge=1, le=100, description="Maximum number of results"),
    vault_id: Optional[int] = Query(None, description="Filter by vault ID"),
    memory_store: MemoryStore = Depends(get_memory_store),
    user: dict = Depends(get_current_active_user),
    evaluate=Depends(get_evaluate_policy),
):
    """
    Search memories using full-text search.

    Uses MemoryStore.search_memories to search memories via FTS5.
    Returns matching memories ordered by relevance.

    Authorization: requires vault read access when vault_id is provided;
    admin/superadmin only when vault_id is omitted (cross-vault search).
    """
    await _authorize_memory_search(user, vault_id, evaluate)
    results = await _perform_memory_search(memory_store, query, limit, vault_id)
    return MemorySearchResponse(results=results, total=len(results))


@router.post("/memories/search", response_model=MemorySearchResponse)
async def search_memories_post(
    request: MemorySearchRequest,
    memory_store: MemoryStore = Depends(get_memory_store),
    user: dict = Depends(get_current_active_user),
    evaluate=Depends(get_evaluate_policy),
    _csrf_token: str = Depends(csrf_protect),
):
    """Search memories via POST (request body).

    Authorization: requires vault read access when vault_id is provided;
    admin/superadmin only when vault_id is omitted (cross-vault search).
    """
    await _authorize_memory_search(user, request.vault_id, evaluate)
    # Handle empty or whitespace-only queries gracefully
    if not request.query or not request.query.strip():
        return MemorySearchResponse(results=[], total=0)
    results = await _perform_memory_search(
        memory_store, request.query, request.limit, request.vault_id
    )
    return MemorySearchResponse(results=results, total=len(results))


@router.post("/memories/backfill-embeddings")
async def backfill_memory_embeddings(
    memory_store: MemoryStore = Depends(get_memory_store),
    user: dict = Depends(get_current_active_user),
    _csrf_token: str = Depends(csrf_protect),
):
    """Trigger embedding backfill for memories missing embeddings or with stale models.

    Superadmin only. Runs synchronously and returns a progress summary.
    """
    if user.get("role") != "superadmin":
        raise HTTPException(status_code=403, detail="Superadmin access required")
    summary = await memory_store.backfill_missing_embeddings()
    return {"status": "complete", "summary": summary}
