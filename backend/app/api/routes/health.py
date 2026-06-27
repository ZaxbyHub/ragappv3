"""
Health check API route.

Provides a health endpoint to check backend status.
Expensive model checks are opt-in via ?deep=true query parameter.
"""

import logging
import sqlite3

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from app.api.deps import get_db, get_llm_health_checker, get_model_checker
from app.services.llm_health import LLMHealthChecker
from app.services.model_checker import ModelChecker

logger = logging.getLogger(__name__)

router = APIRouter()


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

    By default (deep=false), returns immediately with backend status only.
    This is fast and suitable for frequent polling.

    With deep=true, also checks LLM service health, model availability,
    and vector store connectivity.
    """
    result = {
        "status": "ok",
        "services": {"backend": True, "embeddings": None, "chat": None, "vector_store": None},
    }

    if deep:
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
            vector_store = getattr(request.app.state, "vector_store", None)
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

        result["llm"] = llm_status
        result["llm_modes"] = llm_mode_status
        result["models"] = models_status
        result["vector_store"] = vector_status
        result["services"] = {
            "backend": True,
            "embeddings": llm_status.get("embeddings", {}).get("ok", False),
            "chat": llm_status.get("chat", {}).get("ok", False),
            "vector_store": vector_status.get("ok", False),
        }

    return result


@router.get("/health/vector-reconciliation")
async def vector_reconciliation_health(
    conn: sqlite3.Connection = Depends(get_db),
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
