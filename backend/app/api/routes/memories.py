"""
Memory API routes for CRUD operations on memories.

Provides endpoints for listing, creating, updating, deleting, and searching memories.
"""

import asyncio
import contextlib
import json
import logging
import sqlite3
import threading
import weakref
from datetime import datetime, timezone
from typing import Callable, List, Optional, Union

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.api.deps import (
    get_current_active_user,
    get_db,
    get_evaluate_policy,
    get_memory_store,
    require_model_ready,
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
            # Every element must be a non-empty string (issue #515, AC18):
            # a string-form array like "[1]" would otherwise persist and later
            # break typed List[str] response serialization with a 500.
            if not all(isinstance(tag, str) and tag.strip() for tag in parsed):
                raise ValueError("Tags must be a JSON array of non-empty strings")
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
    expected_updated_at: Optional[str] = Field(
        None,
        description=(
            "Optional concurrency token: the updated_at value the caller last "
            "read. When provided and no longer current, the update is "
            "rejected with 409 (issue #686, T1-02-S-05)."
        ),
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
            # Keep only string members so legacy pre-#515 rows with
            # non-string elements cannot 500 the typed List[str] response
            # (issue #686, T1-02-S-06). The write path already rejects
            # non-string arrays (#515 AC18); this guards rows written before.
            return [tag for tag in parsed if isinstance(tag, str)]
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


def _require_admin_for_global(user: dict, vault_id: Optional[int]) -> None:
    """Global memories (``vault_id IS NULL``) are admin/superadmin only.

    Enforced on every memory read/create/update/delete path so the global
    tier cannot be reached by a non-admin via any route. Vault-scoped
    operations (``vault_id is not None``) delegate to the existing
    per-vault ``evaluate()`` checks at the call site (issue #404,
    MEDIUM-11).
    """
    if vault_id is None and user.get("role") not in ("superadmin", "admin"):
        raise HTTPException(
            status_code=403,
            detail="Global memories are admin-only. Specify a vault_id for vault-scoped access.",
        )


def _is_admin(user: dict) -> bool:
    """True when the caller may see/write global (vault_id IS NULL) memories."""
    return user.get("role") in ("superadmin", "admin")


@contextlib.asynccontextmanager
async def _atomic_memory_write(conn: sqlite3.Connection):
    """Run memory content plus its derived-state writes in one transaction.

    Pool connections keep Python's default isolation level, where an
    outermost SAVEPOINT RELEASE commits immediately (issue #686, T1-02-S-01),
    so the route — the transaction owner since #253 — must open the
    transaction itself. Any leftover transaction on a reused pooled
    connection is rolled back first (the admin routes use the same pattern):
    the production pool rolls back on release, but defensive reset keeps a
    stale write from joining this transaction.
    """
    if conn.in_transaction:
        await asyncio.to_thread(conn.rollback)
    await asyncio.to_thread(conn.execute, "BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        try:
            await asyncio.to_thread(conn.rollback)
        except sqlite3.Error:
            logger.exception("Rollback failed after memory write error")
        raise
    else:
        await asyncio.to_thread(conn.commit)


_backfill_locks_guard = threading.Lock()
_backfill_locks: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock]" = (
    weakref.WeakKeyDictionary()
)


def _get_backfill_lock() -> asyncio.Lock:
    """Per-event-loop single-flight lock for the superadmin backfill.

    A module-level ``asyncio.Lock`` binds to the first loop that acquires it
    and raises on later loops (starlette's TestClient spins a fresh loop per
    request), so locks are keyed by the running loop; dead loops drop out of
    the weakref map. Scope is per-process, matching the in-request overlap
    the audit found (issue #686, T1-02-S2-07).
    """
    loop = asyncio.get_running_loop()
    with _backfill_locks_guard:
        lock = _backfill_locks.get(loop)
        if lock is None:
            lock = asyncio.Lock()
            _backfill_locks[loop] = lock
        return lock


async def _perform_memory_search(
    memory_store: MemoryStore,
    query: str,
    limit: int,
    vault_id: Optional[int] = None,
    include_global: bool = False,
) -> List[MemoryResponse]:
    try:
        records = await asyncio.to_thread(
            memory_store.search_memories,
            query=query,
            limit=limit,
            vault_id=vault_id,
            include_global=include_global,
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
    limit: int = Query(
        200, ge=1, le=500, description="Maximum number of memories to return"
    ),
    offset: int = Query(0, ge=0, description="Number of memories to skip"),
    conn: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
    evaluate: Callable = Depends(get_evaluate_policy),
):
    """
    List memories.

    Returns memories with their id, content, category, tags, source,
    created_at, and updated_at fields, newest first, bounded by ``limit``
    (default 200, max 500) with ``offset`` paging (issue #686, T1-02-S2-09 —
    the read must stay bounded; the default still covers what the Memory
    page shows today).

    Authorization:
    - vault_id=N: requires read access to vault N. Admin/superadmin callers
      also see global (vault_id IS NULL) memories alongside the vault's own;
      non-admin callers see ONLY the vault's own memories (issue #404,
      MEDIUM-11 — global memories are admin-only to prevent cross-tenant
      leakage).
    - vault_id=None: admin/superadmin only (returns all memories across all
      vaults). Non-admin callers must specify vault_id explicitly.
    """
    if vault_id is not None:
        if not await evaluate(user, "vault", vault_id, "read"):
            raise HTTPException(status_code=403, detail="No read access to this vault")
        # Global memories (vault_id IS NULL) are admin-only. Non-admins get
        # Two static SQL branches (no f-string interpolation) so bandit B608
        # is not triggered. Non-admins get vault-scoped rows exclusively;
        # admins get the union with global (vault_id IS NULL) memories.
        # Mirrors the include_global flag threaded through
        # MemoryStore.search_memories (issue #404, MEDIUM-11).
        if _is_admin(user):
            sql = """
            SELECT id, content, category, tags, source, importance, expires_at, created_at, updated_at
            FROM memories
            WHERE (vault_id = ? OR vault_id IS NULL)
              AND (expires_at IS NULL OR datetime(expires_at) > datetime('now'))
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?
            """
        else:
            sql = """
            SELECT id, content, category, tags, source, importance, expires_at, created_at, updated_at
            FROM memories
            WHERE vault_id = ?
              AND (expires_at IS NULL OR datetime(expires_at) > datetime('now'))
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?
            """
        cursor = await asyncio.to_thread(conn.execute, sql, (vault_id, limit, offset))
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
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
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
                # None-guard (not truthiness) so importance 0.0 round-trips
                # instead of being coerced to the 0.5 default (issue #686,
                # T1-02-K-03; same guard the store read paths already apply).
                importance=float(row[5]) if row[5] is not None else 0.5,
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
    evaluate: Callable = Depends(get_evaluate_policy),
    _csrf_token: str = Depends(csrf_protect),
    conn: sqlite3.Connection = Depends(get_db),
):
    """
    Create a new memory.

    Uses MemoryStore.add_memory to add a new memory to the database.

    Authorization: vault-scoped memories require write access to the vault;
    global memories (vault_id is None) require admin/superadmin (issue #404).
    """
    # Global-memory tier (vault_id IS NULL) is admin-only. Checked before the
    # per-vault write check so a non-admin POSTing {"vault_id": null} is
    # rejected rather than silently persisting an unscoped memory.
    _require_admin_for_global(user, body.vault_id)
    if body.vault_id is not None:
        if not await evaluate(user, "vault", body.vault_id, "write"):
            raise HTTPException(status_code=403, detail="No write access to this vault")
    # Whitespace-only content is not a usable memory (issue #515, DEEP-D-02):
    # reject before any persistence instead of storing a blank row.
    if not body.content.strip():
        raise HTTPException(status_code=422, detail="Content cannot be empty")
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
    except sqlite3.Error:
        logger.exception(
            "Database error in create_memory (content length: %d)", len(body.content)
        )
        raise HTTPException(status_code=500, detail="Database error")
    except (ValueError, TypeError, RuntimeError):
        logger.exception(
            "Unexpected error in create_memory (content length: %d)",
            len(body.content),
        )
        raise HTTPException(status_code=500, detail="Server error")

    return _memory_record_to_response(record)


@router.put("/memories/{memory_id}", response_model=MemoryResponse)
@limiter.limit(settings.memory_mutation_rate_limit)
async def update_memory(
    request: Request,
    memory_id: int,
    body: MemoryUpdateRequest,
    conn: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
    evaluate: Callable = Depends(get_evaluate_policy),
    memory_store: MemoryStore = Depends(get_memory_store),
    _csrf_token: str = Depends(csrf_protect),
):
    """
    Update an existing memory.

    Updates content, category, tags, and/or source fields in the database.
    Returns 404 if the memory is not found.
    """
    try:
        # Check if memory exists and get vault_id + current content and the
        # concurrency-token baseline (the content is the no-op-save baseline
        # for the claim invalidation below).
        cursor = await asyncio.to_thread(
            conn.execute,
            "SELECT id, vault_id, content, updated_at FROM memories WHERE id = ?",
            (memory_id,),
        )
        row = await asyncio.to_thread(cursor.fetchone)
        if row is None:
            raise HTTPException(
                status_code=404, detail=f"Memory with id {memory_id} not found"
            )

        # Check vault write permission
        memory_vault_id = row[1]
        # Global memories (vault_id IS NULL) are admin-only (issue #404).
        _require_admin_for_global(user, memory_vault_id)
        if memory_vault_id is not None:
            if not await evaluate(user, "vault", memory_vault_id, "write"):
                raise HTTPException(
                    status_code=403, detail="No write access to this vault"
                )

        # Whitespace-only content is not a usable memory (issue #515,
        # DEEP-D-02): reject before any write so the row is not blanked.
        if body.content is not None and not body.content.strip():
            raise HTTPException(status_code=422, detail="Content cannot be empty")

        # Optional optimistic-concurrency token (issue #686, T1-02-S-05): a
        # caller that echoes the updated_at it read gets a 409 when another
        # writer committed first; omitting the token keeps the update
        # unconditional (all pre-existing callers).
        if (
            body.expected_updated_at is not None
            and body.expected_updated_at != row[3]
        ):
            raise HTTPException(
                status_code=409,
                detail="Memory was modified by another session",
            )

        # Build update query dynamically based on provided fields. The
        # embedding-clearing entries and the no-op-save decision are added
        # inside the transaction below, against the freshest stored content
        # re-read under the write lock (a concurrent write between the
        # request-time read and the lock must not produce a stale baseline).
        update_fields = []
        params = []

        if body.content is not None:
            update_fields.append("content = ?")
            params.append(body.content)
        # Explicit-null vs omitted (issue #515, AC19, extended to
        # category/source by issue #686, T1-02-K-02): a JSON null CLEARS the
        # column (SET ... = NULL), while an omitted field preserves the
        # stored value. The distinction is made via pydantic v2
        # model_fields_set: the field counts as "provided" when it was
        # explicitly sent, even when its validated value is None.
        # Documented asymmetry: the tags/expires_at before-validators
        # normalize "" to None (so an empty-string tags CLEARS), while
        # category/source have no such validator (an empty string stores
        # ''). The Memory page always sends null for cleared fields, so the
        # asymmetry is intentional and unchanged.
        fields_set = body.model_fields_set
        if "category" in fields_set:
            update_fields.append("category = ?")
            params.append(body.category)
        if "tags" in fields_set:
            update_fields.append("tags = ?")
            params.append(body.tags)
        if "source" in fields_set:
            update_fields.append("source = ?")
            params.append(body.source)
        if body.importance is not None:
            update_fields.append("importance = ?")
            params.append(body.importance)
        if "expires_at" in fields_set:
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
                importance=float(row[5]) if row[5] is not None else 0.5,
                expires_at=row[6],
                created_at=row[7],
                updated_at=row[8],
            )

        # Add memory_id to params
        params.append(memory_id)

        # One transaction covers the content UPDATE and the claim
        # invalidation (issue #686, T1-02-S-02): the helper opens BEGIN
        # IMMEDIATE and rolls both back on any failure, so the previously
        # stored content survives an invalidation error.
        try:
            async with _atomic_memory_write(conn):
                # Authoritative re-read under the write lock: no other writer
                # can commit between this read and our UPDATE. The optional
                # token is compared here (string equality on
                # CURRENT_TIMESTAMP has 1-second resolution, so same-second
                # races remain possible by design — a monotonic version
                # column would close them; no schema change here), and the
                # no-op-save baseline is refreshed from this same read.
                recheck = await asyncio.to_thread(
                    conn.execute,
                    "SELECT updated_at, content FROM memories WHERE id = ?",
                    (memory_id,),
                )
                fresh = await asyncio.to_thread(recheck.fetchone)
                if fresh is None:
                    raise HTTPException(
                        status_code=404,
                        detail=f"Memory with id {memory_id} not found",
                    )
                if (
                    body.expected_updated_at is not None
                    and body.expected_updated_at != fresh[0]
                ):
                    raise HTTPException(
                        status_code=409,
                        detail="Memory was modified by another session",
                    )

                # No-op save detection (issue #515, AC27): the wiki-claim
                # invalidation below must only fire when the submitted
                # content actually differs from the stored content — an
                # identical-content save cannot have changed any derived
                # claim.
                content_changed = (
                    body.content is not None and body.content != fresh[1]
                )

                sql_fields = list(update_fields)
                if content_changed and memory_store._has_embedding_columns(conn):
                    # Clear the stale embedding only when the content
                    # actually changed (issue #686, T1-02-S-03): an
                    # identical-content save keeps the stored vector.
                    # embed_and_store below recomputes best-effort after
                    # commit.
                    sql_fields.append("embedding = NULL")
                    sql_fields.append("embedding_model = NULL")

                sql = f"""
                    UPDATE memories
                    SET {", ".join(sql_fields)}, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                """
                await asyncio.to_thread(conn.execute, sql, params)

                # Mark wiki claims stale since the source memory content
                # changed. No-op guard (issue #515, AC27): identical-content
                # saves keep sole-source claims active. The helper no longer
                # commits (DD-C009 / #108); our transaction commits it with
                # the content change or rolls both back.
                if content_changed and memory_vault_id is not None:
                    from app.services.wiki_store import WikiStore as _WikiStore

                    await asyncio.to_thread(
                        lambda: _WikiStore(conn).mark_claims_stale_by_memory(memory_id, memory_vault_id)
                    )
        except HTTPException:
            raise
        except Exception:
            logger.exception("Failed to update memory %d", memory_id)
            raise HTTPException(status_code=500, detail="Failed to update memory")

        # If content changed, recompute embedding so semantic search stays fresh.
        # embed_and_store is best-effort: if the embedding service is down, FTS
        # fallback remains intact and the old embedding (now stale) is NULLed first
        # inside the method so searches won't use misleading vectors.
        if content_changed and body.content is not None:
            try:
                await memory_store.embed_and_store(memory_id, body.content)
            except Exception:
                logger.warning(
                    "Could not recompute embedding for memory %d after content update",
                    memory_id,
                )

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
            importance=float(row[5]) if row[5] is not None else 0.5,
            expires_at=row[6],
            created_at=row[7],
            updated_at=row[8],
        )
    except HTTPException:
        raise
    except (sqlite3.Error, OSError):
        logger.exception("Database error during memory update")
        raise HTTPException(status_code=500, detail="Failed to update memory")


@router.delete("/memories/{memory_id}")
@limiter.limit(settings.memory_mutation_rate_limit)
async def delete_memory(
    request: Request,
    memory_id: int,
    conn: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
    evaluate: Callable = Depends(get_evaluate_policy),
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
    # Global memories (vault_id IS NULL) are admin-only (issue #404).
    _require_admin_for_global(user, memory_vault_id)
    if memory_vault_id is not None:
        if not await evaluate(user, "vault", memory_vault_id, "admin"):
            raise HTTPException(status_code=403, detail="No admin access to this vault")

    # Mark wiki claims stale and delete the memory in ONE transaction
    # (issue #686, T1-02-S-01 / T1-02-K-09): the helper opens BEGIN
    # IMMEDIATE and rolls everything back on any failure — an outermost
    # SAVEPOINT RELEASE on these connections would commit the marking before
    # the DELETE runs, and a marking failure must abort the delete rather
    # than fall through to it (DD-C009 / #108 contract, now actually held).
    try:
        async with _atomic_memory_write(conn):
            if memory_vault_id is not None:
                from app.services.wiki_store import WikiStore as _WikiStore

                await asyncio.to_thread(
                    lambda: _WikiStore(conn).mark_claims_stale_by_memory(memory_id, memory_vault_id)
                )
            await asyncio.to_thread(
                conn.execute, "DELETE FROM memories WHERE id = ?", (memory_id,)
            )
    except HTTPException:
        raise
    except Exception:
        logger.exception("Failed to delete memory %d", memory_id)
        raise HTTPException(status_code=500, detail="Failed to delete memory")

    return {"message": f"Memory {memory_id} deleted successfully", "forgotten": True}


async def _authorize_memory_search(
    evaluate: Callable, user: dict, vault_id: Optional[int]
) -> None:
    """Enforce vault read access for memory search/list operations.

    - vault_id=N → require read access on vault N.
    - vault_id=None → admin/superadmin only (broad search across all vaults).
    """
    if vault_id is not None:
        if not await evaluate(user, "vault", vault_id, "read"):
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
    conn: sqlite3.Connection = Depends(get_db),
    memory_store: MemoryStore = Depends(get_memory_store),
    user: dict = Depends(get_current_active_user),
    evaluate: Callable = Depends(get_evaluate_policy),
    _: None = Depends(require_model_ready),
):
    """
    Search memories using full-text search.

    Uses MemoryStore.search_memories to search memories via FTS5.
    Returns matching memories ordered by relevance.

    Authorization: requires vault read access when vault_id is provided;
    admin/superadmin only when vault_id is omitted (cross-vault search).
    Global memories (vault_id IS NULL) are only returned to admin/superadmin
    callers (issue #404, MEDIUM-11).
    """
    await _authorize_memory_search(evaluate, user, vault_id)
    results = await _perform_memory_search(
        memory_store, query, limit, vault_id, include_global=_is_admin(user)
    )
    return MemorySearchResponse(results=results, total=len(results))


@router.post("/memories/search", response_model=MemorySearchResponse)
async def search_memories_post(
    request: MemorySearchRequest,
    memory_store: MemoryStore = Depends(get_memory_store),
    user: dict = Depends(get_current_active_user),
    evaluate: Callable = Depends(get_evaluate_policy),
    _csrf_token: str = Depends(csrf_protect),
    _: None = Depends(require_model_ready),
    conn: sqlite3.Connection = Depends(get_db),
):
    """Search memories via POST (request body).

    Authorization: requires vault read access when vault_id is provided;
    admin/superadmin only when vault_id is omitted (cross-vault search).
    Global memories (vault_id IS NULL) are only returned to admin/superadmin
    callers (issue #404, MEDIUM-11).
    """
    await _authorize_memory_search(evaluate, user, request.vault_id)
    # Handle empty or whitespace-only queries gracefully
    if not request.query or not request.query.strip():
        return MemorySearchResponse(results=[], total=0)
    results = await _perform_memory_search(
        memory_store,
        request.query,
        request.limit,
        request.vault_id,
        include_global=_is_admin(user),
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
    Single-flight (issue #686, T1-02-S2-07): concurrent requests serialize
    on a per-loop lock so at most one backfill runs at a time per process.
    """
    if user.get("role") != "superadmin":
        raise HTTPException(status_code=403, detail="Superadmin access required")
    async with _get_backfill_lock():
        summary = await memory_store.backfill_missing_embeddings()
    return {"status": "complete", "summary": summary}
