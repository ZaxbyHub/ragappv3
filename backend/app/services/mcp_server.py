"""Read-only MCP-style tool adapter for service-account principals (#805).

Exposes the two allowlisted read-only tools (``retrieval`` and
``wiki_read``) over a small HTTP surface:

- ``GET /api/mcp/tools`` lists the allowlisted tools with JSON-schema
  input descriptions.
- ``POST /api/mcp/tools/call`` invokes one tool with a validated argument
  envelope.

Every route authenticates through
:func:`app.api.deps.get_current_user_or_service_account` (human JWTs
first, then scoped ``sak_`` service-account keys). The ``retrieval``
tool is gated on the vault-read policy by the adapter BEFORE anything
executes; ``wiki_read`` is gated inside the tool, after the (read-only,
off-loop) page lookup that yields the vault id, and reports denial
through the same failure envelope as a missing page. The surface is
read-only: no route writes to the database, and the tool allowlist
deliberately excludes the generative synthesis tool.

The adapter neither reads nor gates on ``settings.agentic_rag_enabled``
(it is independent of the agentic RAG feature flag). It DOES honor
``settings.wiki_enabled`` for ``wiki_read``, matching the wiki router's
master switch.
"""

import inspect
import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

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

# SQLite binds 64-bit signed integers; anything at or above 2**63 raises
# OverflowError at execute() time and would surface as a 500. Ids are
# validated against this ceiling so an oversized id is a 400 instead.
_MAX_SQLITE_INT = 2**63 - 1

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
            "minLength": 1,
            "maxLength": 1000,
            "description": "Natural-language search query (1-1000 chars; must be "
            "non-blank after trimming, which the schema cannot express).",
        },
        "vault_id": {
            "type": "integer",
            "minimum": 1,
            "maximum": 2**63 - 1,
            "description": "Vault to search (required; the adapter never "
            "retrieves unscoped). JSON integer without a fractional part - "
            "a decoded float such as 1.0 is rejected by the validator.",
        },
    },
    "required": ["query", "vault_id"],
    "additionalProperties": False,
}

_WIKI_READ_INPUT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "page_id": {
            "type": "integer",
            "minimum": 1,
            "maximum": 2**63 - 1,
            "description": "Id of the wiki page to read. JSON integer without a "
            "fractional part - a decoded float such as 1.0 is rejected by the "
            "validator.",
        },
    },
    "required": ["page_id"],
    "additionalProperties": False,
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

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1)
    arguments: Dict[str, Any] = Field(default_factory=dict)


router = APIRouter()


def register_mcp_routes(app) -> None:
    """Mount the adapter surface on the application under /api.

    The module-level ``router`` carries the ``@router`` decorators that the
    frozen AC10 introspection check reads (``app.services.mcp_server`` must
    expose an ``APIRouter`` named ``router``), while this registrar attaches
    the SAME endpoint functions directly to the app with the /api prefix.
    Direct registration (the register_metrics_route pattern from
    app/services/telemetry.py) keeps the routes materialized in
    ``app.routes`` at import time. ``include_router`` mounts can be
    deferred behind lazy ``_IncludedRouter`` wrappers — observed on the
    FastAPI 0.139.0 installed in the dev environment; the pinned lock
    (0.163.0) was not probed here, so direct registration is the
    version-independent choice.
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
    # Length is checked on the RAW string so the advertised schema
    # (minLength/maxLength) and this validator agree exactly; the blank
    # check then runs on the trimmed value (the only intentional
    # divergence: the schema admits whitespace-only, the validator does not).
    if not 1 <= len(query) <= 1000:
        raise HTTPException(
            status_code=400,
            detail="invalid arguments for tool retrieval: query must be 1-1000 non-blank characters",
        )
    query = query.strip()
    if not query:
        raise HTTPException(
            status_code=400,
            detail="invalid arguments for tool retrieval: query must be 1-1000 non-blank characters",
        )
    vault_id = arguments["vault_id"]
    if (
        isinstance(vault_id, bool)
        or not isinstance(vault_id, int)
        or not 0 < vault_id <= _MAX_SQLITE_INT
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "invalid arguments for tool retrieval: vault_id must be a "
                "positive integer within the SQLite integer range"
            ),
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
    if (
        isinstance(page_id, bool)
        or not isinstance(page_id, int)
        or not 0 < page_id <= _MAX_SQLITE_INT
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "invalid arguments for tool wiki_read: page_id must be a "
                "positive integer within the SQLite integer range"
            ),
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
    without persisting any engine state — the engine's
    operator-configured ``retrieval_top_k`` governs (with the pre-existing
    caveat that ``RetrievalTool`` itself can overwrite
    ``engine.retrieval_top_k`` on the shared engine, so a prior agentic
    call in the same process can change what "configured" means here),
    rerank status is propagated into ``filter_relevant`` exactly as the
    agentic path does, and no private ``_chunk_text`` channel is attached
    (this is a wire boundary). The one in-memory touch is
    ``engine._sync_document_retrieval_settings()``, which refreshes the
    engine's own retrieval sub-service settings from live config (the
    same call RetrievalTool makes); it writes no database or on-disk
    state.

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
@limiter.limit(settings.search_rate_limit)
async def list_mcp_tools(
    request: Request,
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
    """Invoke one allowlisted read-only tool, authorizing per tool.

    ``retrieval`` is gated by the adapter BEFORE anything executes;
    ``wiki_read`` is gated inside the tool, after the read-only page
    lookup that yields the vault id (no content leaves the tool before
    the policy call).

    Response matrix: 422 malformed envelope (FastAPI; unknown top-level
    keys are rejected too); 404 unknown tool; 400 invalid per-tool
    arguments (including ids outside the SQLite integer range); 503 when
    ``wiki_read`` is called while the wiki subsystem is disabled; 403
    vault-read denial on the adapter-owned retrieval gate (nothing
    executed); 200 with the ``{"result": ...}`` payload otherwise —
    tool-level failures, including a vault-policy evaluator fault, surface
    as ``success=False`` inside the envelope with empty output/sources.
    """
    if body.name not in TOOL_ALLOWLIST:
        raise HTTPException(status_code=404, detail=f"Unknown tool: {body.name}")

    if body.name == "retrieval":
        query, vault_id = _validate_retrieval_arguments(body.arguments)
        try:
            allowed = await evaluate(user, "vault", vault_id, "read")
        except Exception as exc:  # noqa: BLE001
            # A policy-evaluator fault is not a denial we can prove, so it
            # fails closed through the same envelope as any other tool
            # failure instead of escaping as an unhandled 500.
            logger.warning(
                "[mcp_server] vault policy check failed for vault_id=%s: %s",
                vault_id,
                exc,
                exc_info=True,
            )
            tool_result = ToolResult(
                output="", sources=[], success=False, error="retrieval unavailable"
            )
            return {"result": build_result_payload(body.name, tool_result)}
        if not allowed:
            raise HTTPException(status_code=403, detail="No read access to vault")
        tool_result = await _run_retrieval(request, vector_store, query, vault_id)
    else:
        page_id = _validate_wiki_read_arguments(body.arguments)
        if not settings.wiki_enabled:
            # Match require_wiki_enabled on the wiki router: with the wiki
            # subsystem disabled, no wiki content leaves the system.
            raise HTTPException(status_code=503, detail="Wiki subsystem is disabled")
        # The pooled connection is opened with check_same_thread=False, so the
        # tool's synchronous store read can run off the event loop.
        tool = WikiReadTool(db, evaluate, user, offload_store_read=True)
        tool_result = await tool.execute(page_id=page_id)

    return {"result": build_result_payload(body.name, tool_result)}
