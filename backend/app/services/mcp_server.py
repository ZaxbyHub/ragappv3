"""Read-only MCP-style tool adapter for service-account principals (#805).

Exposes the two allowlisted read-only tools (``retrieval`` and
``wiki_read``) over a small HTTP surface:

- ``GET /api/mcp/tools`` lists the allowlisted tools with JSON-schema
  input descriptions.
- ``POST /api/mcp/tools/call`` invokes one tool with a validated argument
  envelope.

Every route authenticates through
:func:`app.api.deps.get_current_user_or_service_account` (human JWTs
first, then scoped ``sak_`` service-account keys). Every tool invocation
passes through the vault-read policy BEFORE any retrieval or page read
executes. The surface is read-only: no route writes to the database, and
the tool allowlist deliberately excludes the generative synthesis tool.

The adapter neither reads nor gates on ``settings.agentic_rag_enabled``
(it is independent of the agentic RAG feature flag).
"""

import inspect
import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.api.deps import (
    get_current_user_or_service_account,
    get_db,
    get_embedding_service,
    get_evaluate_policy,
    get_vector_store,
)
from app.config import settings
from app.limiter import limiter
from app.services.agentic_tools import ToolResult, WikiReadTool

logger = logging.getLogger(__name__)

TOOL_ALLOWLIST = frozenset({"retrieval", "wiki_read"})

_RETRIEVAL_ARGUMENT_KEYS = frozenset({"query", "vault_id"})
_WIKI_READ_ARGUMENT_KEYS = frozenset({"page_id"})

# Strict wire whitelist for fallback-path source dicts. Anything a search
# row carries beyond these keys (metadata blobs, internal ``_``-prefixed
# fields) is dropped at this boundary.
_FALLBACK_SOURCE_KEYS = (
    "source_label",
    "id",
    "file_id",
    "filename",
    "section",
    "page_number",
    "score",
    "score_type",
    "vault_id",
)

_RETRIEVAL_INPUT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "Natural-language search query (1-1000 chars).",
        },
        "vault_id": {
            "type": "integer",
            "description": "Vault to search (required; the adapter never "
            "retrieves unscoped).",
        },
    },
    "required": ["query", "vault_id"],
}

_WIKI_READ_INPUT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "page_id": {
            "type": "integer",
            "description": "Id of the wiki page to read.",
        },
    },
    "required": ["page_id"],
}

_TOOL_DESCRIPTIONS = {
    "retrieval": (
        "Semantic document retrieval scoped to one vault. Arguments: "
        "query (str, required), vault_id (int, required). Returns "
        "S-labelled sources. Read-only."
    ),
    "wiki_read": (
        "Read one wiki page by id. Arguments: page_id (int, required). "
        "Returns the page markdown and a W-labelled source entry. "
        "Read-only."
    ),
}


class ToolCallRequest(BaseModel):
    """Envelope for POST /api/mcp/tools/call."""

    name: str = Field(..., min_length=1)
    arguments: Dict[str, Any] = Field(default_factory=dict)


router = APIRouter()


def register_mcp_routes(app) -> None:
    """Mount the adapter surface on the application under /api.

    The module-level ``router`` documents the surface (two routes, no
    prefix) for introspection; this registrar attaches the SAME endpoint
    functions directly to the app with the /api prefix. Direct
    registration (the register_metrics_route pattern from
    app/services/telemetry.py) keeps the routes materialized in
    ``app.routes`` at import time — FastAPI 0.139 defers
    ``include_router`` mounts behind lazy ``_IncludedRouter`` wrappers,
    which would leave the endpoints invisible to route introspection
    until the first request materializes them.
    """
    app.add_api_route(
        "/api/mcp/tools",
        endpoint=list_mcp_tools,
        methods=["GET"],
        name="list_mcp_tools",
    )
    app.add_api_route(
        "/api/mcp/tools/call",
        endpoint=call_mcp_tool,
        methods=["POST"],
        name="call_mcp_tool",
    )



def build_result_payload(tool_name: str, tool_result: ToolResult) -> Dict[str, Any]:
    """Serialize a ToolResult into the public ``{"result": ...}`` body.

    Private ``_``-prefixed keys are stripped from every source, and each
    surviving ``source_label`` is embedded in the output string as
    ``[S#]``/``[W#]`` so the labels round-trip through
    :func:`app.services.citation_validator.parse_citations` and
    :func:`app.services.citation_validator.parse_wiki_citations`.
    """
    sources = []
    labels = []
    for src in tool_result.sources or []:
        clean = {k: v for k, v in src.items() if not str(k).startswith("_")}
        label = clean.get("source_label") or ""
        if label:
            labels.append(f"[{label}]")
        sources.append(clean)
    output = tool_result.output or ""
    if labels:
        output = f"{output} {' '.join(labels)}".strip()
    error = tool_result.error if not tool_result.success else None
    return {
        "output": output,
        "sources": sources,
        "success": bool(tool_result.success),
        "error": error,
    }


async def _resolve_embedding_service(request: Request):
    """Resolve the embedding service honoring DI overrides first.

    The adapter deliberately does not declare
    ``Depends(get_embedding_service)`` on its routes (the dependency reads
    ``app.state.embedding_service``, which only exists after lifespan
    startup); instead this seam checks the FastAPI dependency-override
    table first (awaiting an async override if necessary), then falls back
    to optional app state, and finally returns None.
    """
    override = request.app.dependency_overrides.get(get_embedding_service)
    if override is not None:
        resolved = override()
        if inspect.isawaitable(resolved):
            resolved = await resolved
        return resolved
    return getattr(request.app.state, "embedding_service", None)


def _validate_retrieval_arguments(arguments: Dict[str, Any]) -> tuple[str, int]:
    """Strict retrieval argument validation (exact key set, typed)."""
    keys = set(arguments)
    if keys != _RETRIEVAL_ARGUMENT_KEYS:
        missing = sorted(_RETRIEVAL_ARGUMENT_KEYS - keys)
        unknown = sorted(keys - _RETRIEVAL_ARGUMENT_KEYS)
        raise HTTPException(
            status_code=400,
            detail=(
                "invalid arguments for tool retrieval: "
                f"missing={missing} unexpected={unknown}; "
                "expected exactly ['query', 'vault_id']"
            ),
        )
    query = arguments["query"]
    if not isinstance(query, str):
        raise HTTPException(
            status_code=400, detail="invalid arguments for tool retrieval: query must be a string"
        )
    query = query.strip()
    if not query or len(query) > 1000:
        raise HTTPException(
            status_code=400,
            detail="invalid arguments for tool retrieval: query must be 1-1000 non-blank characters",
        )
    vault_id = arguments["vault_id"]
    if isinstance(vault_id, bool) or not isinstance(vault_id, int) or vault_id <= 0:
        raise HTTPException(
            status_code=400,
            detail="invalid arguments for tool retrieval: vault_id must be a positive integer",
        )
    return query, vault_id


def _validate_wiki_read_arguments(arguments: Dict[str, Any]) -> int:
    """Strict wiki_read argument validation (exact key set, typed)."""
    keys = set(arguments)
    if keys != _WIKI_READ_ARGUMENT_KEYS:
        missing = sorted(_WIKI_READ_ARGUMENT_KEYS - keys)
        unknown = sorted(keys - _WIKI_READ_ARGUMENT_KEYS)
        raise HTTPException(
            status_code=400,
            detail=(
                "invalid arguments for tool wiki_read: "
                f"missing={missing} unexpected={unknown}; "
                "expected exactly ['page_id']"
            ),
        )
    page_id = arguments["page_id"]
    if isinstance(page_id, bool) or not isinstance(page_id, int) or page_id <= 0:
        raise HTTPException(
            status_code=400,
            detail="invalid arguments for tool wiki_read: page_id must be a positive integer",
        )
    return page_id


def _fallback_source_dict(row: Dict[str, Any], index: int) -> Dict[str, Any]:
    """Strictly whitelisted source dict for a fallback-path search row."""
    source = {key: row.get(key) for key in _FALLBACK_SOURCE_KEYS}
    source["source_label"] = f"S{index}"
    return source


async def _run_retrieval(
    request: Request,
    vector_store,
    query: str,
    vault_id: int,
) -> ToolResult:
    """Execute retrieval through the engine when present, else minimally.

    Engine-present path (production): mirrors RetrievalTool.execute
    without ever writing engine attributes — the engine's
    operator-configured ``retrieval_top_k`` governs, rerank status is
    propagated into ``filter_relevant`` exactly as the agentic path does,
    and no private ``_chunk_text`` channel is attached (this is a wire
    boundary).

    No-engine path (pre-lifespan or DI-override fixtures): embed via the
    DI-honoring embedding seam, then a typed ``vector_store.search`` with
    the configured limit, string vault scope, and raw query text. When no
    embedding service is resolvable either, the tool honestly reports
    typed unavailability.
    """
    engine = getattr(request.app.state, "rag_engine", None)
    try:
        if engine is not None:
            embedding_service = getattr(engine, "embedding_service", None)
            if embedding_service is None:
                return ToolResult(
                    output="", sources=[], success=False, error="retrieval unavailable"
                )
            embedding = await embedding_service.embed_single(query)
            (
                vector_results,
                _relevance_hint,
                _eval_result,
                rerank_success,
                _score_type,
                _hybrid_status,
                _fts_exceptions,
                _rerank_status,
                _variants_dropped,
                _exact_match_promoted,
                _token_pack_stats,
            ) = await engine._execute_retrieval(
                [("original", embedding)],
                query,
                vault_id,
            )
            engine._sync_document_retrieval_settings()
            relevant_chunks = await engine.document_retrieval.filter_relevant(
                vector_results,
                reranked=rerank_success if rerank_success is not None else False,
                indexed_file_ids=None,
            )
            sources = []
            for idx, chunk in enumerate(relevant_chunks):
                sources.append(
                    engine.document_retrieval.to_source_metadata(
                        chunk, source_index=1 + idx
                    )
                )
            return ToolResult(
                output=f"Retrieved {len(sources)} sources for query: {query}",
                sources=sources,
                success=True,
            )

        embedding_service = await _resolve_embedding_service(request)
        if embedding_service is None:
            return ToolResult(
                output="", sources=[], success=False, error="retrieval unavailable"
            )
        embedding = await embedding_service.embed_single(query)
        results = await vector_store.search(
            embedding,
            limit=settings.retrieval_top_k,
            vault_id=str(vault_id),
            query_text=query,
        )
        if not isinstance(results, list):
            results = []
        sources = [
            _fallback_source_dict(row, 1 + idx) for idx, row in enumerate(results)
        ]
        return ToolResult(
            output=f"Retrieved {len(sources)} sources for query: {query}",
            sources=sources,
            success=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[mcp_server] retrieval failed: %s", exc, exc_info=True)
        return ToolResult(
            output="", sources=[], success=False, error="retrieval unavailable"
        )


@router.get("/mcp/tools")
async def list_mcp_tools(
    user: dict = Depends(get_current_user_or_service_account),
):
    """List the allowlisted read-only tools with JSON-schema inputs.

    Deliberately independent of ``settings.agentic_rag_enabled``: the
    adapter surface exists whether or not agentic RAG is enabled.
    """
    return {
        "tools": [
            {
                "name": name,
                "description": _TOOL_DESCRIPTIONS[name],
                "input_schema": (
                    _RETRIEVAL_INPUT_SCHEMA
                    if name == "retrieval"
                    else _WIKI_READ_INPUT_SCHEMA
                ),
            }
            for name in sorted(TOOL_ALLOWLIST)
        ]
    }


@router.post("/mcp/tools/call")
@limiter.limit(settings.search_rate_limit)
async def call_mcp_tool(
    request: Request,
    body: ToolCallRequest,
    user: dict = Depends(get_current_user_or_service_account),
    db=Depends(get_db),
    evaluate=Depends(get_evaluate_policy),
    vector_store=Depends(get_vector_store),
):
    """Invoke one allowlisted read-only tool with authorization first.

    Response matrix: 422 malformed envelope (FastAPI); 404 unknown tool;
    400 invalid per-tool arguments; 403 vault-read denial on the
    adapter-owned retrieval gate (nothing executed); 200 with the
    ``{"result": ...}`` payload otherwise (tool-level failures surface as
    ``success=False`` inside the envelope with empty output/sources).
    """
    if body.name not in TOOL_ALLOWLIST:
        raise HTTPException(status_code=404, detail=f"Unknown tool: {body.name}")

    if body.name == "retrieval":
        query, vault_id = _validate_retrieval_arguments(body.arguments)
        allowed = await evaluate(user, "vault", vault_id, "read")
        if not allowed:
            raise HTTPException(status_code=403, detail="No read access to vault")
        tool_result = await _run_retrieval(request, vector_store, query, vault_id)
    else:
        page_id = _validate_wiki_read_arguments(body.arguments)
        tool = WikiReadTool(db, evaluate, user)
        tool_result = await tool.execute(page_id=page_id)

    return {"result": build_result_payload(body.name, tool_result)}
