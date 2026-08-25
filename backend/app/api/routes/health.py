"""
Health check API route.

Provides a health endpoint to check backend status. Expensive model checks
run on ?deep=true and their results are cached so shallow polls can serve
last-known service status instead of nulls ("not checked") that clients
historically coerced into "down".
"""

import asyncio
import logging
import sqlite3
import time

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from app.api.deps import (
    get_current_active_user,
    get_db,
    get_llm_health_checker,
    get_model_checker,
)
from app.services.llm_health import LLMHealthChecker
from app.services.model_checker import ModelChecker

logger = logging.getLogger(__name__)

router = APIRouter()


# --- Last-known service status cache for shallow /health polls ---------------
#
# Shallow polls are the frontend's 30s heartbeat. They used to return null for
# embeddings/chat ("not checked this cycle"), which clients treated as "down".
# Instead, shallow responses now serve the last deep result (with its age) and
# lazily kick a background deep refresh once it goes stale — polls stay cheap
# but the reported booleans stay truthful for every client, not just the one
# that happened to send ?deep=true.
_SHALLOW_CACHE_TTL = 50.0  # seconds; refresh when older
_deep_cache: dict = {"services": None, "ts": 0.0}
# Plain flag (not asyncio.Lock): the check-and-set below runs without an await
# between the two steps, so the event loop's single-threadedness already makes
# it atomic — and a flag carries no event-loop affinity, which matters for
# test clients that run the app on fresh loops per request.
_deep_refresh_in_flight = False
_refresh_tasks: set = set()  # keep strong refs so tasks aren't GC'd mid-flight


def _cache_age() -> float:
    return time.monotonic() - _deep_cache["ts"]


def _update_deep_cache(deep_state: dict) -> None:
    _deep_cache["services"] = deep_state["services"]
    _deep_cache["ts"] = time.monotonic()


async def _refresh_deep_cache(app_state, llm_checker, model_checker) -> None:
    """Refresh the last-known cache if stale (single-flight per staleness window)."""
    global _deep_refresh_in_flight
    try:
        if _cache_age() < _SHALLOW_CACHE_TTL:
            return  # another request already refreshed it
        deep_state = await _collect_deep_state(app_state, llm_checker, model_checker)
        _update_deep_cache(deep_state)
    except Exception as exc:
        # Keep the previous (stale) cache rather than poisoning it with a
        # partial failure; the age field lets clients judge staleness.
        logger.debug("background deep-health refresh failed: %s", exc)
    finally:
        _deep_refresh_in_flight = False


def _spawn_refresh_task(request, llm_checker, model_checker) -> None:
    global _deep_refresh_in_flight
    if _deep_refresh_in_flight:
        return
    _deep_refresh_in_flight = True
    task = asyncio.create_task(
        _refresh_deep_cache(request.app.state, llm_checker, model_checker)
    )
    _refresh_tasks.add(task)
    task.add_done_callback(_refresh_tasks.discard)


async def _collect_deep_state(app_state, llm_checker, model_checker) -> dict:
    """Run all deep checks; returns the deep-only portion of the response."""
    llm_status = await llm_checker.check_all()
    try:
        llm_mode_status = await llm_checker.check_chat_modes()
    except Exception as exc:
        logger.debug("check_chat_modes unavailable: %s", exc)
        llm_mode_status = {"thinking": False, "instant": False}
    models_status = await model_checker.check_models()

    # Probe vector store connectivity and embedding dimension consistency
    vector_status = {"ok": False}
    try:
        vector_store = getattr(app_state, "vector_store", None)
        if vector_store and vector_store.table:
            row_count = await vector_store.table.count_rows()
            vector_status = {"ok": True, "rows": row_count}

            # Issue #2: Warn if stored embedding dimension mismatches configured dim.
            # A mismatch means documents were indexed with a different model and
            # searches will return empty or incorrect results until re-embedded.
            try:
                from app.config import settings as _settings
                stored_dim = await vector_store._get_expected_embedding_dim()
                configured_dim = _settings.embedding_dim
                if stored_dim and stored_dim != configured_dim:
                    vector_status["stale_embeddings"] = True
                    vector_status["stale_embeddings_detail"] = (
                        f"LanceDB index was built with {stored_dim}-dim embeddings but "
                        f"EMBEDDING_DIM is now {configured_dim}. "
                        f"Run scripts/migrate_embeddings.py to re-index."
                    )
                    logger.warning(
                        "Stale embedding dimensions detected: stored=%d, configured=%d. "
                        "Documents will not be searchable until re-embedded. "
                        "Run scripts/migrate_embeddings.py.",
                        stored_dim,
                        configured_dim,
                    )
            except Exception as _dim_exc:
                logger.debug("Embedding dimension check failed (non-fatal): %s", _dim_exc)

        elif vector_store:
            vector_status = {"ok": True, "rows": 0}
        else:
            vector_status = {"ok": False, "error": "not initialized"}
    except Exception as e:
        logger.debug("Vector store health probe failed: %s", e)
        vector_status = {"ok": False, "error": str(e)}

    services = {
        "backend": True,
        "embeddings": llm_status.get("embeddings", {}).get("ok", False),
        "chat": llm_status.get("chat", {}).get("ok", False),
        "vector_store": vector_status.get("ok", False),
    }

    return {
        "llm": llm_status,
        "llm_modes": llm_mode_status,
        "models": models_status,
        "vector_store": vector_status,
        "services": services,
    }


def _vector_reconciliation_status(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS pending_count,
            MIN(created_at) AS oldest_pending_created_at,
            MAX(attempts) AS max_attempts
        FROM vector_delete_pending
        """
    ).fetchone()
    pending_count = int(row[0] or 0) if row else 0
    max_attempts = int(row[2] or 0) if row and row[2] is not None else 0
    return {
        "ok": pending_count == 0,
        "pending_count": pending_count,
        "oldest_pending_created_at": row[1] if row else None,
        "max_attempts": max_attempts,
    }


@router.get("/health")
async def health_check(
    request: Request,
    deep: bool = Query(False, description="Run expensive model availability checks"),
    llm_checker: LLMHealthChecker = Depends(get_llm_health_checker),
    model_checker: ModelChecker = Depends(get_model_checker),
):
    """
    Health check endpoint.

    By default (deep=false), returns immediately with backend status plus the
    last known embeddings/chat/vector_store status (from the most recent deep
    check, with ``services_age_seconds`` indicating its age). A stale cache
    triggers a background deep refresh; the current response still returns
    instantly with the last-known values.

    With deep=true, runs the full LLM/service/model/vector checks inline and
    refreshes the last-known cache.
    """
    result = {
        "status": "ok",
        "services": {"backend": True, "embeddings": None, "chat": None, "vector_store": None},
    }

    # Expose the ingestion/enrichment backlog depth so operators can observe a
    # stuck BackgroundProcessor without tailing logs (the queue_size property
    # was previously defined but never consumed by any application code).
    bg_processor = getattr(request.app.state, "background_processor", None)
    if bg_processor is not None:
        try:
            result["ingestion_queue_size"] = bg_processor.queue_size
        except Exception as exc:
            logger.debug("ingestion queue_size probe failed: %s", exc)

    if deep:
        deep_state = await _collect_deep_state(request.app.state, llm_checker, model_checker)
        _update_deep_cache(deep_state)
        result.update(deep_state)
    else:
        cached = _deep_cache["services"]
        if cached is not None:
            result["services"] = dict(cached)
            result["services_cached"] = True
            result["services_age_seconds"] = round(_cache_age(), 1)
        if _cache_age() >= _SHALLOW_CACHE_TTL:
            _spawn_refresh_task(request, llm_checker, model_checker)

    return result


@router.get("/health/vector-reconciliation")
async def vector_reconciliation_health(
    conn: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
):
    """Return runtime status for pending SQLite-to-vector-store reconciliation."""
    try:
        return _vector_reconciliation_status(conn)
    except sqlite3.Error as exc:
        logger.warning("Vector reconciliation status probe failed: %s", exc)
        return JSONResponse(
            status_code=503,
            content={
                "ok": False,
                "pending_count": None,
                "oldest_pending_created_at": None,
                "max_attempts": None,
                "error": str(exc),
            },
        )


@router.get("/llm-health/modes")
async def llm_mode_health(
    llm_checker: LLMHealthChecker = Depends(get_llm_health_checker),
):
    """Probe both Thinking and Instant LLM endpoints.

    Returns ``{"thinking": bool, "instant": bool}``. Used by the chat
    composer to enable/disable the per-message mode toggle.
    """
    return await llm_checker.check_chat_modes()


@router.get("/healthz")
async def healthz(request: Request):
    """
    Lightweight readiness probe.

    Returns 200 when critical services (db, vector store, embedding) are initialized.
    Returns 503 with a list of issues otherwise.
    Suitable for Kubernetes liveness/readiness probes and load-balancer health checks.
    Does not run expensive model availability checks.
    """
    state = request.app.state
    issues = []

    if not getattr(state, "db_pool", None):
        issues.append("db_pool not initialized")
    vector_store = getattr(state, "vector_store", None)
    if not vector_store:
        issues.append("vector_store not initialized")
    elif not getattr(vector_store, "table", None):
        issues.append("vector_store not connected")
    if not getattr(state, "embedding_service", None):
        issues.append("embedding_service not initialized")

    if issues:
        return JSONResponse(
            status_code=503,
            content={"status": "degraded", "issues": issues},
        )
    return {"status": "ok"}
