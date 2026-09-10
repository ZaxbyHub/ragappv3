"""
Search API routes.

Semantic (vector) search for document chunks, chunk context expansion, and
the unified cross-entity discovery endpoint (Issue #515 / PRODUCT-ENH-11).
"""

import asyncio
import json
import logging
import sqlite3
from collections.abc import Callable
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.api.deps import (
    get_current_active_user,
    get_current_user_or_service_account,
    get_db,
    get_embedding_service,
    get_evaluate_policy,
    get_user_accessible_vault_ids,
    get_vector_store,
    require_model_ready,
)
from app.api.routes.documents import _build_files_fts_query, search_documents
from app.config import settings
from app.limiter import limiter
from app.services.document_retrieval import (
    _strip_reupload_hash,
    sanitize_wire_filename,
    whitelist_metadata_for_wire,
)
from app.services.embeddings import EmbeddingError, EmbeddingService
from app.services.kms_store import KMSStore
from app.services.vector_store import (
    SearchSemaphoreTimeoutError,
    VectorStore,
    VectorStoreError,
)
from app.services.wiki_store import WikiStore

router = APIRouter()
logger = logging.getLogger(__name__)


class SearchRequest(BaseModel):
    """Request model for semantic search endpoint."""

    query: str = Field(..., min_length=1, max_length=1000)
    limit: int = Field(10, ge=1, le=100)
    vault_id: Optional[int] = None


class SearchResult(BaseModel):
    """Model for a single search result."""

    id: str
    text: str
    file_id: str
    chunk_index: int
    metadata: Dict[str, Any]
    score: float


class SearchResponse(BaseModel):
    """Response model for search endpoint.

    ``search_type`` is always ``"diagnostic"`` — this endpoint performs simple
    vector similarity search and does NOT use the full RAG retrieval pipeline
    (no reranking, no hybrid fusion, no citation generation). Use ``/chat``
    for production RAG answers.
    """

    results: List[SearchResult]
    search_type: str = "diagnostic"


class ChunkContextResponse(BaseModel):
    """Expanded context for a retrieved chunk.

    Backward-compatible: ``context_text``/``context_source`` remain the opaque
    window used by existing clients. New structured fields (Issue #396):
    ``matched_text`` is the chunk itself; ``before``/``after`` are ordered lists
    of neighbor chunk texts selected by the ``context_before``/``context_after``
    query params (empty when the params are 0, the default).
    """

    id: str
    file_id: str
    filename: str
    chunk_index: int | str
    chunk_text: str
    context_text: str
    context_source: str
    matched_text: str = ""
    before: List[str] = []
    after: List[str] = []


def _record_get(record: Any, key: str, default: Any = None) -> Any:
    if isinstance(record, dict):
        return record.get(key, default)
    return getattr(record, key, default)


def _parse_metadata(record: Any) -> Dict[str, Any]:
    metadata = _record_get(record, "metadata", {})
    if isinstance(metadata, dict):
        return metadata
    if isinstance(metadata, str):
        try:
            parsed = json.loads(metadata)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, TypeError):
            return {}
    return {}


def _coerce_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@router.post("/search", response_model=SearchResponse)
@limiter.limit(settings.search_rate_limit)
async def search(
    request: Request,
    body: SearchRequest,
    user: dict = Depends(get_current_user_or_service_account),
    db=Depends(get_db),
    embedding_service: EmbeddingService = Depends(get_embedding_service),
    vector_store: VectorStore = Depends(get_vector_store),
    evaluate: Callable = Depends(get_evaluate_policy),
    _=Depends(require_model_ready),
):
    """
    Semantic search endpoint for document chunks.

    Embeds the query text and searches the vector store for similar chunks.

    Args:
        request: SearchRequest containing query text and optional limit

    Returns:
        SearchResponse with list of matching chunks and similarity scores

    Raises:
        HTTPException: 500 if embedding or search operation fails
    """
    # Early check for whitespace-only query
    if not body.query or not body.query.strip():
        raise HTTPException(
            status_code=400, detail="Query cannot be empty or whitespace only"
        )

    # Vault permission scoping
    if body.vault_id is not None:
        # Specific vault requested — check read access
        if not await evaluate(user, "vault", body.vault_id, "read"):
            raise HTTPException(status_code=403, detail="No read access to this vault")
    else:
        # No vault specified — non-admins must specify a vault
        if user.get("role") not in ("superadmin", "admin"):
            raise HTTPException(
                status_code=400, detail="vault_id is required for non-admin users"
            )

    try:
        # Generate embedding for the query
        query_embedding = await embedding_service.embed_single(body.query)

        # Initialize vector store table
        embedding_dim = len(query_embedding)
        await vector_store.init_table(embedding_dim)

        # Perform semantic search
        vault_id_str = str(body.vault_id) if body.vault_id is not None else None
        raw_results = await vector_store.search(
            embedding=query_embedding, limit=body.limit, vault_id=vault_id_str
        )

        # Transform results to response model
        results = []
        for record in raw_results:
            # Parse metadata JSON string if present
            metadata = _parse_metadata(record)

            # Get similarity score (_distance is returned by LanceDB)
            score = (
                record.get("_distance", 0.0)
                if isinstance(record, dict)
                else getattr(record, "_distance", 0.0)
            )

            # Safe extraction from records with defaults when None
            results.append(
                SearchResult(
                    id=record.get("id", "")
                    if isinstance(record, dict)
                    else getattr(record, "id", ""),
                    text=record.get("text", "")
                    if isinstance(record, dict)
                    else getattr(record, "text", ""),
                    file_id=record.get("file_id", "")
                    if isinstance(record, dict)
                    else getattr(record, "file_id", ""),
                    chunk_index=record.get("chunk_index", 0)
                    if isinstance(record, dict)
                    else getattr(record, "chunk_index", 0),
                    # Issue #480 (A1) / PR #481 (PRR-001): apply the same wire
                    # metadata whitelist as to_source_metadata so server-absolute
                    # paths (file_path/source_file) and internal ids do not leak
                    # via the /search results either.
                    metadata=whitelist_metadata_for_wire(metadata),
                    score=score,
                )
            )

        return SearchResponse(results=results)

    except SearchSemaphoreTimeoutError:
        raise HTTPException(status_code=503, detail="Search temporarily unavailable")
    except EmbeddingError as e:
        # Log the underlying error server-side; return a generic message so
        # internal exception text is not echoed to the caller.
        logger.error("Search embedding error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Embedding service error")
    except VectorStoreError as e:
        logger.error("Search vector store error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Vector store error")
    except Exception as e:
        # Catch-all: never echo raw internal exception text into the response.
        logger.error("Search operation failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal error during search")


@router.get("/search/chunks/{chunk_id}/context", response_model=ChunkContextResponse)
async def get_chunk_context(
    chunk_id: str,
    context_before: int = Query(0, ge=0, le=10, description="Neighbor chunks before the match"),
    context_after: int = Query(0, ge=0, le=10, description="Neighbor chunks after the match"),
    user: dict = Depends(get_current_user_or_service_account),
    db: sqlite3.Connection = Depends(get_db),
    vector_store: VectorStore = Depends(get_vector_store),
    evaluate: Callable = Depends(get_evaluate_policy),
    _=Depends(require_model_ready),
):
    """
    Fetch a retrieved chunk plus stored parent-window context for lazy previews.

    The endpoint is intentionally point-look-up only: it does not run semantic
    search or scan the corpus, so expanding a source preview does not change
    query performance characteristics.
    """
    try:
        # Exact-match lookup first (preserves precedence for current ids).
        chunks = await vector_store.get_chunks_by_uid([chunk_id])
        if not chunks:
            # PRR-010: a caller (e.g. a persisted chat source captured before
            # a reprocess) may hold an id whose hash segment no longer
            # matches the stored uid. Retry once with the reupload hash
            # segment stripped so those legacy-format counterparts resolve
            # instead of 404ing. Ids without a hash segment are unchanged by
            # the strip, so the retry is skipped entirely for them.
            normalized_id = _strip_reupload_hash(chunk_id)
            if normalized_id != chunk_id:
                chunks = await vector_store.get_chunks_by_uid([normalized_id])
    except VectorStoreError as e:
        raise HTTPException(status_code=500, detail=f"Vector store error: {str(e)}")

    if not chunks:
        raise HTTPException(status_code=404, detail="Chunk not found")

    chunk = chunks[0]
    metadata = _parse_metadata(chunk)
    file_id = str(_record_get(chunk, "file_id", metadata.get("file_id", "")) or "")
    vault_id = _coerce_int(_record_get(chunk, "vault_id", metadata.get("vault_id")))
    filename = (
        metadata.get("source_file")
        or metadata.get("filename")
        or metadata.get("section_title")
        or "Unknown document"
    )

    file_id_int = _coerce_int(file_id)
    if file_id_int is None:
        raise HTTPException(status_code=404, detail="Chunk not found")

    cursor = await asyncio.to_thread(
        db.execute,
        "SELECT file_name, vault_id FROM files WHERE id = ?",
        (file_id_int,),
    )
    row = await asyncio.to_thread(cursor.fetchone)
    if not row:
        raise HTTPException(status_code=404, detail="Chunk not found")

    # PR #481 (PRR-001): prefer the DB file_name; if it's NULL/empty, fall back to
    # the metadata-derived name but SANITIZE it so a stored server-absolute
    # ``source_file`` never reaches the wire filename field.
    filename = row["file_name"] or sanitize_wire_filename(filename)
    vault_id = _coerce_int(row["vault_id"])
    if vault_id is None:
        raise HTTPException(status_code=404, detail="Chunk not found")

    if not await evaluate(user, "vault", vault_id, "read"):
        raise HTTPException(status_code=404, detail="Chunk not found")

    chunk_text = str(_record_get(chunk, "text", "") or "")
    parent_window_text = metadata.get("parent_window_text")
    raw_text = metadata.get("raw_text")
    if isinstance(parent_window_text, str) and parent_window_text.strip():
        context_text = parent_window_text
        context_source = "parent_window"
    elif isinstance(raw_text, str) and raw_text.strip():
        context_text = raw_text
        context_source = "raw_text"
    else:
        context_text = chunk_text
        context_source = "chunk"

    # Configurable neighbor context (Issue #396). Fetch chunks before/after by
    # file_id + chunk_index range and split into ordered before/after lists.
    before_texts: List[str] = []
    after_texts: List[str] = []
    if context_before > 0 or context_after > 0:
        center_idx = _record_get(chunk, "chunk_index", metadata.get("chunk_index", 0))
        try:
            center_idx_int = int(center_idx)
        except (TypeError, ValueError):
            center_idx_int = 0
        get_range = getattr(vector_store, "get_chunks_by_file_range", None)
        if get_range is not None:
            try:
                neighbors = await get_range(
                    file_id, center_idx_int, context_before, context_after
                )
            except (OSError, RuntimeError, ValueError):
                neighbors = []
            for nbr in neighbors:
                nbr_idx = _record_get(nbr, "chunk_index", None)
                nbr_text = str(_record_get(nbr, "text", "") or "")
                try:
                    nbr_idx_int = int(nbr_idx) if nbr_idx is not None else None
                except (TypeError, ValueError):
                    continue
                if nbr_idx_int is None or nbr_idx_int == center_idx_int:
                    continue
                if nbr_idx_int < center_idx_int:
                    before_texts.append(nbr_text)
                else:
                    after_texts.append(nbr_text)

    return ChunkContextResponse(
        id=str(_record_get(chunk, "id", chunk_id) or chunk_id),
        file_id=file_id,
        filename=str(filename),
        chunk_index=_record_get(chunk, "chunk_index", metadata.get("chunk_index", 0)),
        chunk_text=chunk_text,
        context_text=context_text,
        context_source=context_source,
        matched_text=chunk_text,
        before=before_texts,
        after=after_texts,
    )


# ---------------------------------------------------------------------------
# Unified cross-entity discovery search (Issue #515 / PRODUCT-ENH-11)
# ---------------------------------------------------------------------------

class UnifiedSearchResult(BaseModel):
    """One cross-entity discovery hit (Issue #515).

    ``type`` discriminates the entity kind so clients can pick a renderer;
    ``url_hint`` is the frontend path that opens the entity; ``score`` is
    bm25-derived for documents and a deterministic title > summary > body
    tier proxy for the FTS-backed stores whose public search APIs do not
    expose a rank.
    """

    type: str
    id: int
    title: str
    snippet: str
    vault_id: int
    url_hint: str
    score: float


class UnifiedSearchResponse(BaseModel):
    """Aggregated response for GET /search/unified (Issue #515)."""

    results: List[UnifiedSearchResult]


_UNIFIED_TYPES = ("document", "wiki", "kms", "chat")
_UNIFIED_TYPE_ORDER = {name: idx for idx, name in enumerate(_UNIFIED_TYPES)}


def _parse_unified_types(raw: Optional[str]) -> List[str]:
    """Parse the ``types`` query parameter into a validated entity-type list.

    Unknown values are rejected with 422: the parameter is a machine contract
    (frontend type-filter checkboxes), not free-text search input. An absent
    or blank parameter means "search every entity type".
    """
    if raw is None or not raw.strip():
        return list(_UNIFIED_TYPES)
    requested = [part.strip().lower() for part in raw.split(",") if part.strip()]
    invalid = sorted({part for part in requested if part not in _UNIFIED_TYPE_ORDER})
    if not requested or invalid:
        raise HTTPException(
            status_code=422,
            detail=(
                "types must be a comma-separated subset of "
                f"{list(_UNIFIED_TYPES)}; invalid: {invalid or ['<empty>']}"
            ),
        )
    deduped: List[str] = []
    for entity_type in requested:
        if entity_type not in deduped:
            deduped.append(entity_type)
    return deduped


def _unified_snippet(primary: Optional[str], fallback: Optional[str]) -> str:
    """Prefer the entity's summary field, fall back to its body, clipped."""
    text = (primary or "").strip() or (fallback or "").strip()
    return text[:200]


def _tiered_score(
    query_lower: str, title: Optional[str], summary: Optional[str]
) -> float:
    """Deterministic relevance proxy for stores without a bm25 rank:
    title containment scores 3.0, summary containment 2.0, body-only FTS
    match 1.0. ``query_lower`` is non-empty at every call site."""
    if query_lower in (title or "").lower():
        return 3.0
    if query_lower in (summary or "").lower():
        return 2.0
    return 1.0


@router.get("/search/unified", response_model=UnifiedSearchResponse)
async def unified_search(
    q: str = Query(..., min_length=1, max_length=200, description="Search query"),
    vault_id: Optional[int] = Query(None, description="Restrict to a vault"),
    types: Optional[str] = Query(
        None,
        description="Comma-separated subset of document,wiki,kms,chat (default: all)",
    ),
    limit: int = Query(20, ge=1, le=50, description="Maximum results per entity type"),
    db: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
    evaluate: Callable = Depends(get_evaluate_policy),
):
    """Unified discovery search across documents, wiki pages, KMS entries and
    chat session titles (Issue #515, PRODUCT-ENH-11).

    SQL/FTS-only — no vector store involvement. Each arm reuses the existing
    per-entity search logic rather than a new retrieval stack: documents call
    the ranked ``GET /documents/search`` handler (bm25 + highlighted
    excerpts + per-document dedup), wiki pages go through ``WikiStore.search``,
    KMS entries through ``KMSStore.list_entries`` and chat sessions are
    matched on title the way ``GET /chat/sessions`` scopes them. Every arm
    applies its own ``LIMIT``. Authz mirrors the sibling per-entity routes:
    vault read check when ``vault_id`` is given, accessible-vault scoping for
    non-admins without one.
    """
    query_text = q.strip()
    if not query_text:
        raise HTTPException(status_code=400, detail="Query must not be empty")
    requested_types = _parse_unified_types(types)
    query_lower = query_text.lower()

    # Vault scoping (mirrors GET /documents/search). WikiStore.search and
    # KMSStore.list_entries take a single vault_id, so a multi-vault scope
    # iterates the vault list with the same per-entity limit.
    if vault_id is not None:
        if not await evaluate(user, "vault", vault_id, "read"):
            raise HTTPException(status_code=403, detail="Access denied to vault")
        scope_vaults: List[int] = [vault_id]
    elif user.get("role") in ("admin", "superadmin"):
        cursor = await asyncio.to_thread(db.execute, "SELECT id FROM vaults ORDER BY id")
        scope_vaults = [int(row[0]) for row in await asyncio.to_thread(cursor.fetchall)]
    else:
        scope_vaults = await get_user_accessible_vault_ids(user, db)
        if not scope_vaults:
            return UnifiedSearchResponse(results=[])

    # Sanitize once into a safe FTS5 prefix query: raw user input containing
    # hyphens/punctuation would otherwise be interpreted as FTS5 MATCH syntax
    # by the wiki store's pass-through MATCH.
    fts_query = _build_files_fts_query(query_text)

    results: List[UnifiedSearchResult] = []

    if "document" in requested_types and fts_query:
        # Direct reuse of the ranked documents search — it re-applies the same
        # vault scoping rules (evaluate check above already ran for the
        # vault_id case) and returns deduplicated, excerpt-bearing hits.
        doc_response = await search_documents(
            q=q,
            vault_id=vault_id,
            limit=limit,
            offset=0,
            conn=db,
            user=user,
            evaluate=evaluate,
        )
        for hit in doc_response.results:
            results.append(
                UnifiedSearchResult(
                    type="document",
                    id=hit.id,
                    title=hit.file_name,
                    snippet=hit.excerpt,
                    vault_id=hit.vault_id,
                    url_hint=f"/documents/{hit.id}",
                    score=float(hit.score),
                )
            )

    if "wiki" in requested_types and fts_query and scope_vaults:
        wiki_store = WikiStore(db)
        for scope_vault in scope_vaults:
            wiki_hits = wiki_store.search(scope_vault, fts_query, limit=limit)
            for page in wiki_hits["pages"]:
                results.append(
                    UnifiedSearchResult(
                        type="wiki",
                        id=page.id,
                        title=page.title or page.slug,
                        snippet=_unified_snippet(page.summary, page.markdown),
                        vault_id=page.vault_id,
                        url_hint=f"/wiki?page={page.id}",
                        score=_tiered_score(query_lower, page.title, page.summary),
                    )
                )

    if "kms" in requested_types and fts_query and scope_vaults:
        kms_store = KMSStore(db)
        for scope_vault in scope_vaults:
            for entry in kms_store.list_entries(
                scope_vault, search=fts_query, page=1, per_page=limit
            ):
                results.append(
                    UnifiedSearchResult(
                        type="kms",
                        id=entry.id,
                        title=entry.title,
                        snippet=_unified_snippet(entry.summary, entry.body),
                        vault_id=entry.vault_id,
                        url_hint=f"/kms/{entry.id}",
                        score=_tiered_score(query_lower, entry.title, entry.summary),
                    )
                )

    if "chat" in requested_types:
        # Title substring match with the same visibility rules as
        # GET /chat/sessions: admins see the vault's sessions, non-admins
        # only their own (plus unowned) ones.
        chat_sql = "SELECT id, vault_id, title FROM chat_sessions WHERE LOWER(title) LIKE ?"
        chat_params: List[Any] = [f"%{query_lower}%"]
        if scope_vaults:
            placeholders = ",".join("?" * len(scope_vaults))
            chat_sql += f" AND vault_id IN ({placeholders})"
            chat_params.extend(scope_vaults)
        if user.get("role") not in ("admin", "superadmin"):
            chat_sql += " AND (user_id = ? OR user_id IS NULL)"
            chat_params.append(user.get("id"))
        chat_sql += " ORDER BY updated_at DESC, id DESC LIMIT ?"
        chat_params.append(limit)
        cursor = await asyncio.to_thread(db.execute, chat_sql, chat_params)
        for row in await asyncio.to_thread(cursor.fetchall):
            # A NULL title can never satisfy the LIKE, so matched rows
            # always carry a non-empty title.
            results.append(
                UnifiedSearchResult(
                    type="chat",
                    id=int(row["id"]),
                    title=str(row["title"]),
                    snippet=str(row["title"]),
                    vault_id=int(row["vault_id"]),
                    url_hint=f"/chat/{row['id']}",
                    score=_tiered_score(query_lower, row["title"], None),
                )
            )

    # Deterministic ordering: entity type, then score descending, then id.
    results.sort(key=lambda r: (_UNIFIED_TYPE_ORDER[r.type], -r.score, r.id))
    return UnifiedSearchResponse(results=results)
