"""
Semantic search API routes for document chunks.

Provides endpoints for searching document chunks using vector similarity.
"""

import asyncio
import json
import sqlite3
from collections.abc import Callable
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.api.deps import (
    get_current_active_user,
    get_db,
    get_embedding_service,
    get_evaluate_policy,
    get_vector_store,
)
from app.config import settings
from app.limiter import limiter
from app.services.embeddings import EmbeddingError, EmbeddingService
from app.services.vector_store import VectorStore, VectorStoreError

router = APIRouter()


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
    """Expanded context for a retrieved chunk."""

    id: str
    file_id: str
    filename: str
    chunk_index: int | str
    chunk_text: str
    context_text: str
    context_source: str


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
    user: dict = Depends(get_current_active_user),
    db=Depends(get_db),
    embedding_service: EmbeddingService = Depends(get_embedding_service),
    vector_store: VectorStore = Depends(get_vector_store),
    evaluate: Callable = Depends(get_evaluate_policy),
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
                    metadata=metadata,
                    score=score,
                )
            )

        return SearchResponse(results=results)

    except EmbeddingError as e:
        raise HTTPException(
            status_code=500, detail=f"Embedding service error: {str(e)}"
        )
    except VectorStoreError as e:
        raise HTTPException(status_code=500, detail=f"Vector store error: {str(e)}")
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Search operation failed: {str(e)}"
        )


@router.get("/search/chunks/{chunk_id}/context", response_model=ChunkContextResponse)
async def get_chunk_context(
    chunk_id: str,
    user: dict = Depends(get_current_active_user),
    db: sqlite3.Connection = Depends(get_db),
    vector_store: VectorStore = Depends(get_vector_store),
    evaluate: Callable = Depends(get_evaluate_policy),
):
    """
    Fetch a retrieved chunk plus stored parent-window context for lazy previews.

    The endpoint is intentionally point-look-up only: it does not run semantic
    search or scan the corpus, so expanding a source preview does not change
    query performance characteristics.
    """
    try:
        chunks = await vector_store.get_chunks_by_uid([chunk_id])
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

    filename = row["file_name"] or filename
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

    return ChunkContextResponse(
        id=str(_record_get(chunk, "id", chunk_id) or chunk_id),
        file_id=file_id,
        filename=str(filename),
        chunk_index=_record_get(chunk, "chunk_index", metadata.get("chunk_index", 0)),
        chunk_text=chunk_text,
        context_text=context_text,
        context_source=context_source,
    )
