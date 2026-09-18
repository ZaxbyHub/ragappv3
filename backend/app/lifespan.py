"""
Lifespan context manager for FastAPI application startup and shutdown.
"""

import asyncio
import json
import logging
import sqlite3
from contextlib import asynccontextmanager
from typing import Union, get_args, get_origin

from fastapi import FastAPI

from app.api.routes.settings import PERSISTED_FUNCTIONAL_FIELDS
from app.config import Settings, settings
from app.middleware.logging import SensitiveFieldFilter
from app.models.database import SQLiteConnectionPool, get_pool, run_migrations
from app.security import CSRFManager
from app.services.background_tasks import get_background_processor
from app.services.document_extraction import DocumentExtractionService
from app.services.draft_input_storage import DraftInputStorage
from app.services.draft_job_processor import DraftJobProcessor
from app.services.email_service import EmailIngestionService
from app.services.embeddings import EmbeddingService
from app.services.file_watcher import FileWatcher
from app.services.kms_compile_processor import KMSCompileProcessor
from app.services.kms_retrieval import KMSRetrievalService
from app.services.llm_client import (
    LLMClient,
    ModelNotConfiguredError,
    create_instant_client,
    create_thinking_client,
)
from app.services.llm_health import LLMHealthChecker
from app.services.maintenance import MaintenanceService
from app.services.memory_store import MemoryStore
from app.services.model_checker import ModelChecker
from app.services.rag_engine import RAGEngine
from app.services.reranking import RerankingService
from app.services.secret_manager import SecretManager
from app.services.ssrf import URLBlocked, assert_url_safe
from app.services.ssrf_transport import SSRFSafeTransport
from app.services.toggle_manager import ToggleManager
from app.services.vector_store import VectorStore, VectorStoreError, has_index
from app.services.wiki_compile_processor import WikiCompileProcessor
from app.services.wiki_retrieval import WikiRetrievalService
from app.utils.request_context import JsonFormatter, RequestIdFilter

logger = logging.getLogger(__name__)  # noqa: E402


# ── Persisted-settings replay decoding (issue #494 CONFIG-003) ──────────────
# Typed converters for ``settings_kv`` values, derived from the Settings field
# annotations so each persisted field decodes to its real runtime type. This
# covers the 21 previously-drifted fields (ingestion_llm_mode str,
# instant_skip_* bool, wiki_lint_enabled bool, wiki_llm_curator_* str/int/
# float/bool, instant_enable_thinking bool) and fixes the Optional-annotated
# fields (vector_top_k etc.), whose None default previously made the
# runtime-type inference setattr the raw JSON STRING onto the singleton.

def _decode_persisted_bool(raw: str) -> bool:
    return str(raw).strip().lower() in ("true", "1", "yes", "on")


def _decode_persisted_int(raw: str) -> int:
    return int(raw)


def _decode_persisted_float(raw: str) -> float:
    return float(raw)


def _decode_persisted_str(raw: str) -> str:
    # Values are written with json.dumps (json-quoted); fall back to the raw
    # text for hand-edited rows instead of failing the restore.
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError, TypeError):
        return raw


def _decode_persisted_json(raw: str):
    # Lists/dicts (e.g. multimodal_allowed_model_origins). A non-JSON row is
    # returned as-is; the per-field validation gate below rejects mismatches.
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError, TypeError):
        return raw


_PERSISTED_SCALAR_DECODERS = {
    bool: _decode_persisted_bool,
    int: _decode_persisted_int,
    float: _decode_persisted_float,
    str: _decode_persisted_str,
}


def _build_persisted_field_decoders() -> dict:
    """Map every PERSISTED_FUNCTIONAL_FIELDS entry to its typed decoder."""
    converters: dict = {}
    for field_name in PERSISTED_FUNCTIONAL_FIELDS:
        field_info = Settings.model_fields.get(field_name)
        annotation = field_info.annotation if field_info is not None else None
        if annotation is None:
            continue
        # Unwrap Optional[X] → X so None-defaulted fields decode as their
        # concrete scalar type instead of hitting a NoneType branch.
        if get_origin(annotation) is Union:
            non_none = [arg for arg in get_args(annotation) if arg is not type(None)]
            annotation = non_none[0] if non_none else str
        converters[field_name] = _PERSISTED_SCALAR_DECODERS.get(
            annotation, _decode_persisted_json
        )
    return converters


_PERSISTED_FIELD_DECODERS = _build_persisted_field_decoders()


def select_ingestion_llm_client(app: FastAPI, mode: str):
    """Select the LLM client used by optional ingestion LLM work."""
    if mode == "instant":
        return app.state.instant_llm_client
    if mode == "thinking":
        return app.state.thinking_llm_client
    return None



def _create_llm_clients() -> "tuple[LLMClient | None, LLMClient | None]":
    """Build the dual LLM clients, or (None, None) when unconfigured.

    The system ships no chat-model defaults (issue #570): when either
    endpoint pair is incomplete the factory raises ModelNotConfiguredError,
    which here means "not configured yet" — an info line, never a boot
    failure. The app runs degraded (chat requests 409 with setup guidance)
    until an operator configures endpoints; Settings saves then activate
    the missing clients live via _hot_rebind_llm_clients.
    """
    thinking: LLMClient | None = None
    instant: LLMClient | None = None
    try:
        thinking = create_thinking_client()
    except ModelNotConfiguredError:
        logger.info(
            "Thinking chat model not configured; chat is disabled until an "
            "endpoint is set (Settings -> Models or OLLAMA_CHAT_URL/CHAT_MODEL)"
        )
    try:
        instant = create_instant_client()
    except ModelNotConfiguredError:
        logger.info(
            "Instant chat model not configured; instant mode is disabled "
            "until an endpoint is set (Settings -> Models or "
            "INSTANT_CHAT_URL/INSTANT_CHAT_MODEL)"
        )
    return thinking, instant
def _validate_setting_value(key: str, value) -> bool:
    """Validate a single setting value through Pydantic field validation.

    Returns True if the value is valid, False otherwise.
    """
    try:
        current = settings.model_dump()
        current[key] = value
        type(settings).model_validate(current)
        return True
    except Exception as e:
        logger.warning("Persisted setting %s=%r failed validation: %s", key, value, e)
        return False


def _load_persisted_settings(sqlite_path: str) -> None:
    """Load user-configurable settings from DB if they were previously saved."""
    conn = sqlite3.connect(sqlite_path)
    conn.row_factory = sqlite3.Row
    try:
        cursor = conn.execute("SELECT key, value FROM settings_kv")
        # Build persisted dict from all rows
        persisted = {row["key"]: row["value"] for row in cursor.fetchall()}

        # Legacy keys — require JSON parsing and type conversion
        legacy_keys = {
            "chunk_size": int,
            "chunk_overlap": int,
            "max_context_chunks": int,
            "auto_scan_interval_minutes": int,
            "auto_scan_enabled": bool,
            "rag_relevance_threshold": float,
        }
        for key, expected_type in legacy_keys.items():
            if key in persisted:
                try:
                    if expected_type is bool:
                        converted = bool(json.loads(persisted[key]))
                    elif expected_type is int:
                        converted = int(json.loads(persisted[key]))
                    elif expected_type is float:
                        converted = float(json.loads(persisted[key]))
                    else:
                        converted = persisted[key]
                    if _validate_setting_value(key, converted):
                        setattr(settings, key, converted)
                except Exception as e:
                    logger.warning(f"Failed to restore persisted setting {key}: {e}")

        # Every remaining persisted functional field replays through the typed
        # converter map (issue #494 CONFIG-003).
        #
        # NEW_DIRECT_KEYS is the literal mirror of PERSISTED_FUNCTIONAL_FIELDS
        # — the list exported by app/api/routes/settings.py as the single
        # source of truth shared with the save path (ALLOWED_FIELDS minus the
        # documented exclusions; empty today). The literal form is load-bearing:
        # the replay-contract checks verify the startup replay set by statically
        # reading this list (backend/tests/test_issue494_settings_replay_drift.py
        # C12a extracts it with ast; test_lifespan_wiki_settings_reload.py
        # greps it). The equality guard right below makes any drift between
        # the two lists a loud failure the first time persisted settings are
        # loaded — the hand-maintained-list drift that caused CONFIG-003 (21
        # saveable fields silently reverting to defaults on restart) cannot
        # recur silently.
        NEW_DIRECT_KEYS = [
            "chunk_size_chars",
            "chunk_overlap_chars",
            "retrieval_top_k",
            "retrieval_window",
            "max_distance_threshold",
            "vector_metric",
            "embedding_doc_prefix",
            "embedding_query_prefix",
            "embedding_batch_size",
            # Embedding batching / cache / near-dup / orphan rescan (issue #513)
            "embedding_batch_max_chars",
            "embedding_cache_max_entries",
            "near_dup_threshold",
            "orphan_rescan_interval_seconds",
            # Job-lease knobs (issue #559)
            "jobs_heartbeat_interval_seconds",
            "jobs_lease_reclaim_timeout_seconds",
            "jobs_max_attempts",
            "reranking_enabled",
            "reranker_top_n",
            "initial_retrieval_top_k",
            "hybrid_search_enabled",
            "hybrid_alpha",
            "ollama_embedding_url",
            "ollama_chat_url",
            "reranker_url",
            "reranker_model",
            "embedding_model",
            "chat_model",
            "instant_chat_url",
            "instant_chat_model",
            "default_chat_mode",
            "ingestion_llm_mode",
            "instant_initial_retrieval_top_k",
            "instant_reranker_top_n",
            "instant_memory_context_top_k",
            "instant_max_tokens",
            "thinking_max_tokens",
            "instant_enable_thinking",
            # Instant-mode latency skips
            "instant_skip_query_transformation",
            "instant_skip_retrieval_evaluation",
            "instant_skip_distillation_synthesis",
            "instant_skip_followup_rewrite",
            "retrieval_consolidated_rerank",
            "retrieval_kms_overlap",
            "prompt_budget_enabled",
            "model_context_tokens",
            "prompt_reserve_output_tokens",
            "redis_io_timeout_seconds",
            "context_distiller_max_sentences",
            "vector_top_k",
            "kms_enabled",
            "kms_compile_on_ingest",
            "wiki_enabled",
            "wiki_compile_on_ingest",
            "wiki_compile_on_query",
            "wiki_compile_after_indexing",
            "wiki_lint_enabled",
            # Optional LLM Wiki Curator
            "wiki_llm_curator_enabled",
            "wiki_llm_curator_url",
            "wiki_llm_curator_model",
            "wiki_llm_curator_temperature",
            "wiki_llm_curator_max_input_chars",
            "wiki_llm_curator_max_output_tokens",
            "wiki_llm_curator_timeout_sec",
            "wiki_llm_curator_concurrency",
            "wiki_llm_curator_mode",
            "wiki_llm_curator_require_quote_match",
            "wiki_llm_curator_require_chunk_id",
            "wiki_llm_curator_run_on_ingest",
            "wiki_llm_curator_run_on_query",
            "wiki_llm_curator_run_on_manual",
            "draft_room_enabled",
            # Multimodal artifact enrichment (issue #461)
            "multimodal_enrichment_enabled",
            "multimodal_allowed_model_origins",
            "multimodal_chat_url",
            "multimodal_model",
            "multimodal_mode",
            "multimodal_timeout_seconds",
            "multimodal_concurrency",
            "multimodal_max_assets_per_batch",
            "multimodal_max_asset_bytes",
            "multimodal_max_total_payload_bytes",
            "multimodal_max_pixels",
            "multimodal_max_attempts",
            "multimodal_prompt_version",
            "multimodal_schema_version",
            "multimodal_impl_version",
            # Query-time retrieval-first VLM master switch (issue #462)
            "multimodal_query_vision_enabled",
        ]
        if sorted(NEW_DIRECT_KEYS) != sorted(
            k for k in PERSISTED_FUNCTIONAL_FIELDS if k not in legacy_keys
        ):
            raise RuntimeError(
                "app/lifespan.py NEW_DIRECT_KEYS drifted from "
                "app/api/routes/settings.py PERSISTED_FUNCTIONAL_FIELDS — "
                "update the literal to mirror the exported list "
                "(issue #494 CONFIG-003 drift guard)"
            )
        # Iterate the exported single source of truth; the guard above
        # guarantees the literal mirror (plus the legacy-loop keys above)
        # covers exactly this set.
        for key in PERSISTED_FUNCTIONAL_FIELDS:
            if key in persisted:
                try:
                    if not hasattr(settings, key):
                        logger.warning(f"Unknown persisted setting {key}, skipping")
                        continue
                    decoder = _PERSISTED_FIELD_DECODERS.get(key)
                    if decoder is not None:
                        converted = decoder(persisted[key])
                    else:
                        # Fallback: infer from the current value's runtime type.
                        expected_type = type(getattr(settings, key))
                        raw = persisted[key]
                        if expected_type is type(None):  # NoneType - just set as string
                            converted = raw
                        elif expected_type is bool:
                            converted = str(raw).lower() in ("true", "1", "yes", "on")
                        elif expected_type is int:
                            converted = int(raw)
                        elif expected_type is float:
                            converted = float(raw)
                        else:
                            try:
                                converted = json.loads(raw)
                            except (json.JSONDecodeError, ValueError):
                                converted = raw
                    if _validate_setting_value(key, converted):
                        setattr(settings, key, converted)
                except Exception as e:
                    logger.warning(f"Failed to restore persisted setting {key}: {e}")
    except sqlite3.OperationalError:
        logger.debug(
            "Settings table not yet created; skipping persisted settings load (expected on first startup)"
        )
    finally:
        conn.close()


async def _safe_await(coro, name, timeout=10):
    """Await a coroutine with a timeout, logging warnings on failure."""
    try:
        await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning(f"{name} timed out after {timeout}s (continuing)")
    except Exception as e:
        logger.warning(f"{name} failed: {e} (continuing)")


async def _validate_tei_embedding_model(embedding_service) -> None:
    """Validate that the live TEI model matches the configured EMBEDDING_MODEL.

    Probes the embedding server's ``/info`` endpoint (served at the host root,
    not under the embeddings route) and raises ``RuntimeError`` on a genuine
    model-id mismatch so startup fails fast on an embedding-space mismatch.
    Network failures, SSRF blocks, and non-200 responses are treated as
    "skip validation" (logged, non-fatal) so startup is not blocked when the
    endpoint simply does not expose ``/info``.
    """
    try:
        import httpx

        # follow_redirects=False so a 30x from the embedding host cannot bypass
        # the SSRF guard by redirecting to a private/internal address.
        # SSRFSafeTransport re-validates the resolved IP at request time to close
        # the DNS-rebinding TOCTOU gap the startup-only guard leaves open.
        async with httpx.AsyncClient(
            timeout=10.0,
            follow_redirects=False,
            transport=SSRFSafeTransport(),
        ) as client:
            # TEI exposes /info at the server root, not under the embeddings
            # route. Strip the resolved embeddings endpoint suffix (native
            # /embed, OpenAI /v1/embeddings, or Ollama /api/embeddings) so the
            # probe reaches <host>/info for native TEI as well as the
            # OpenAI-compatible variant.
            _emb_url = embedding_service.embeddings_url.rstrip("/")
            for _suffix in ("/embed", "/v1/embeddings", "/api/embeddings"):
                if _emb_url.endswith(_suffix):
                    _emb_url = _emb_url[: -len(_suffix)]
                    break
            info_url = _emb_url.rstrip("/") + "/info"
            assert_url_safe(info_url)
            response = await client.get(info_url)
            if response.status_code == 200:
                info_data = response.json()
                live_model_id = info_data.get("model_id", "").split("/")[-1]
                configured_model = settings.embedding_model.split("/")[-1]
                if live_model_id and live_model_id != configured_model:
                    error_msg = (
                        f"EMBEDDING_MODEL mismatch! Configured: '{configured_model}', "
                        f"Live TEI: '{live_model_id}'. "
                        f"Embedding space mismatch will cause incorrect retrieval. "
                        f"Set STRICT_EMBEDDING_MODEL_CHECK=false to disable this check."
                    )
                    logger.error("=" * 60)
                    logger.error("STARTUP VALIDATION FAILED: %s", error_msg)
                    logger.error("=" * 60)
                    raise RuntimeError(error_msg)
                else:
                    logger.info("TEI model validation passed: %s", live_model_id)
            else:
                logger.warning(
                    "TEI /info endpoint returned %d, skipping model validation",
                    response.status_code,
                )
    except httpx.TimeoutException:
        logger.warning("TEI /info endpoint timed out, skipping model validation")
    except URLBlocked as e:
        logger.warning(
            "TEI /info endpoint blocked by SSRF guard: %s (continuing)", e
        )
    except Exception as e:
        if isinstance(e, RuntimeError):
            raise  # Re-raise our own error
        logger.warning("TEI model validation failed (continuing): %s", e)


async def validate_fts_index(table) -> bool:
    """Validate that the vector table carries the full-text-search index.

    ``table`` is the vector store's table object (anything exposing an async
    ``list_indices()`` whose results have ``.columns`` and ``.index_type``
    attributes). Detection is by COLUMN and TYPE (``has_index``): the engine
    auto-derives index names, so a name-based check can never match a table
    this application creates (issue #557). Returns True iff an FTS index on
    the ``text`` column exists. Returns False — after logging — when the
    index is missing or when ``list_indices()`` itself raises; a validation
    failure is observable via the return value and never propagated, so
    startup always continues.

    Note: no production caller currently branches on the return value (the
    lifespan call site ignores it); the bool surface exists so the check is
    observable to tests and future callers.
    """
    try:
        fts_index_exists = await has_index(table, "text", "FTS")
        if not fts_index_exists:
            logger.error(
                "Hybrid search is enabled but the FTS index is missing on the 'text' column. "
                "FTS search will not function. Create the index with "
                "VectorStore._ensure_fts_index() or rebuild the table."
            )
        return fts_index_exists
    except Exception as e:
        logger.error(
            f"Failed to check FTS index status (hybrid search may not work): {e}"
        )
        return False


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager for startup and shutdown events."""
    # Apply the configured log level to the root logger so application
    # ``logger.info(...)`` calls (e.g. the ``[query]`` retrieval-phase traces)
    # actually emit. Historically ``settings.log_level`` / ``LOG_LEVEL`` was
    # never applied to the root logger, so under uvicorn the root stayed at the
    # default WARNING and all app INFO logs were silently dropped. We set the
    # level here and only add a handler if the root has none, so uvicorn's own
    # access/error handlers are left intact.
    _root_logger = logging.getLogger()
    _root_logger.setLevel(
        getattr(logging, settings.log_level.upper(), logging.INFO)
    )
    if not _root_logger.handlers:
        _handler = logging.StreamHandler()
        # Structured JSON output so the extra={} fields supplied by the HTTP
        # middleware (request_id, method, path, status_code, duration_ms, …)
        # and the request_id stamped by RequestIdFilter are actually emitted,
        # instead of being dropped by a plain-text formatter.
        _handler.setFormatter(JsonFormatter())
        # Stamp request_id on every record (from the contextvar) and redact
        # sensitive record attributes by name (user_input/api_key/…).
        _handler.addFilter(RequestIdFilter())
        _handler.addFilter(SensitiveFieldFilter())
        _root_logger.addHandler(_handler)
    else:
        # A handler already exists (e.g. uvicorn installed its own, or a
        # custom log config was supplied). Attach the request_id and
        # sensitive-field filters so those concerns apply uniformly across
        # whatever formatter the existing handler uses. We deliberately do
        # NOT replace the existing formatter here — clobbering uvicorn's (or
        # a deployment's) formatter would silently change its log shape.
        # Operators who want JSON output everywhere can configure a JSON
        # formatter in their logging config directly.
        for _existing in _root_logger.handlers:
            if RequestIdFilter not in {type(f) for f in _existing.filters}:
                _existing.addFilter(RequestIdFilter())
            if SensitiveFieldFilter not in {type(f) for f in _existing.filters}:
                _existing.addFilter(SensitiveFieldFilter())

    # Startup: Initialize database and services. The outcome is recorded on
    # app.state so the readiness probe (/api/healthz) can report a failed
    # startup migration without a per-request pooled DB read (issues #550,
    # #549 C02): in-memory flag, written where the migration already runs.
    app.state.migrations_ok = True
    try:
        run_migrations(str(settings.sqlite_path))
    except Exception as e:
        app.state.migrations_ok = False
        logger.error(
            "Database migration failed: %s — app will start with degraded database state",
            e,
        )
    # Operator visibility (issue #512 recovery journal): one summary line
    # with the latest migration/recovery outcomes so a prior failed or
    # recovered migration is visible without querying the journal table.
    try:
        from app.models.migration_journal import latest_outcomes

        _recent = latest_outcomes(str(settings.sqlite_path), limit=3)
        if _recent:
            logger.info(
                "Migration journal (latest %d): %s",
                len(_recent),
                "; ".join(
                    f"{row['migration_name']}[{row['phase']}:{row['outcome']}]"
                    for row in _recent
                ),
            )
    except Exception as e:  # pragma: no cover - journal is best-effort
        logger.debug("Could not read migration journal at startup: %s", e)
    _load_persisted_settings(str(settings.sqlite_path))

    # Seed the application DB pool BEFORE any other get_pool() caller can run.
    # get_pool() is a first-call-wins singleton keyed on the sqlite_path, so a
    # caller that requests a smaller (or default) max_size earlier in startup
    # would permanently cache that size for the process. migrate_uploads() below
    # is exactly such a caller (_lookup_vault_id used to seed at the default 5
    # before this line sized the pool at 10) — sizing first closes issue #302.
    #
    # Wrapped in try/except so a seeding failure does not hard-crash startup
    # with an opaque traceback: app.state.db_pool is referenced directly by the
    # streaming auth boundaries (get_stream_auth, wiki_events_stream) and by
    # MemoryStore init below, so it must always be set. On failure we fall back
    # to a fresh pool constructed directly (bypassing the singleton cache) at a
    # conservative default size — degraded capacity, but the app still boots.
    try:
        app.state.db_pool = get_pool(
            str(settings.sqlite_path), max_size=settings.db_pool_max_size
        )
    except Exception as e:
        logger.error(
            "DB pool seeding failed (sqlite_path=%s, max_size=%d): %s — "
            "falling back to a default-size pool (degraded capacity)",
            settings.sqlite_path,
            settings.db_pool_max_size,
            e,
        )
        app.state.db_pool = SQLiteConnectionPool(
            str(settings.sqlite_path), max_size=5
        )

    # Migrate uploads to per-vault directories (run before accepting requests)
    try:
        from app.services.upload_path import migrate_uploads

        logger.info("Checking for upload migration...")
        await asyncio.wait_for(asyncio.to_thread(migrate_uploads, False), timeout=15)
    except Exception as e:
        logger.warning(f"Upload migration failed (continuing anyway): {e}")

    # Dual LLM clients, both operator-configured (no shipped defaults,
    # issue #570): Thinking (any OpenAI-compatible endpoint via
    # ollama_chat_url/chat_model) and Instant (via instant_chat_url/
    # instant_chat_model). Unconfigured pairs boot as None — chat returns
    # a 409 pointing at Settings -> Models until an endpoint is configured.
    app.state.thinking_llm_client, app.state.instant_llm_client = _create_llm_clients()
    if app.state.thinking_llm_client is not None:
        await _safe_await(
            app.state.thinking_llm_client.start(),
            "Thinking LLM client start",
            timeout=10,
        )
    if app.state.instant_llm_client is not None:
        await _safe_await(
            app.state.instant_llm_client.start(),
            "Instant LLM client start",
            timeout=10,
        )
    # Back-compat alias — every existing consumer (LLMHealthChecker,
    # background_processor, keepalive, RAGEngine) reads ``llm_client``.
    app.state.llm_client = app.state.thinking_llm_client
    app.state.embedding_service = EmbeddingService()

    # Validate that live TEI model matches EMBEDDING_MODEL config
    if settings.strict_embedding_model_check:
        await _validate_tei_embedding_model(app.state.embedding_service)

    app.state.vector_store = VectorStore()
    # Critical: fail fast if the vector store cannot connect or initialize its table.
    # Without these two, no search or ingestion is possible.
    await asyncio.wait_for(app.state.vector_store.connect(), timeout=15)
    await _safe_await(
        app.state.vector_store.migrate_add_vault_id(),
        "Vector store migrate vault_id",
        timeout=10,
    )
    await _safe_await(
        app.state.vector_store.migrate_add_chunk_scale(),
        "Vector store migrate chunk_scale",
        timeout=10,
    )
    await _safe_await(
        app.state.vector_store.migrate_add_sparse_embedding(),
        "Vector store migrate sparse",
        timeout=10,
    )
    await _safe_await(
        app.state.vector_store.migrate_add_parent_window(),
        "Vector store migrate parent_window",
        timeout=10,
    )

    # Initialize vector store table before FTS validation
    await asyncio.wait_for(
        app.state.vector_store.init_table(settings.embedding_dim),
        timeout=10,
    )

    # Validate FTS index exists if hybrid search is enabled.
    # The return value is intentionally ignored: validation logs on failure
    # and startup continues either way (behavior identical to the former
    # inline block).
    if settings.hybrid_search_enabled:
        await validate_fts_index(app.state.vector_store.table)

    # Initialize RerankingService — no explicit args so URL/model/top_n are
    # read live from settings on each call (admin can change via Settings UI).
    app.state.reranking_service = RerankingService()

    # Validate schema at startup
    try:
        embedding_model_id = settings.embedding_model
        embedding_dim = settings.embedding_dim
        validation_result = await app.state.vector_store.validate_schema(
            embedding_model_id, embedding_dim
        )
        logger.info(f"Vector store schema validation completed: {validation_result}")
        if not app.state.vector_store._ready:
            logger.warning("=" * 60)
            logger.warning("VECTOR STORE MODEL MISMATCH")
            logger.warning("=" * 60)
            logger.warning("The configured embedding model does not match the identity stored for the existing index.")
            logger.warning("Vector-query endpoints will return HTTP 503 until a reindex is completed.")
            if validation_result and isinstance(validation_result, dict):
                logger.warning("Validation result: %s", validation_result)
            logger.warning("=" * 60)
    except VectorStoreError as e:
        logger.error("=" * 60)
        logger.error("VECTOR STORE SCHEMA VALIDATION FAILED")
        logger.error("=" * 60)
        logger.error("Error: %s", e)
        logger.error("Vector store validation encountered an unexpected error.")
        logger.error("=" * 60)
        # Continue startup; the vector store will remain unavailable for vector queries.
    # Parent-window retrieval startup check: if the operator has enabled
    # parent_retrieval but the on-disk chunks were ingested before the
    # parent_window_text was being persisted, the feature degrades to
    # legacy chunk-only rendering. We emit a log diagnostic so operators
    # can decide whether to reindex; the runtime path itself remains
    # safe (prompt_builder handles missing parent_window_text gracefully).
    if settings.parent_retrieval_enabled:
        try:
            sample_present = await app.state.vector_store.has_parent_window_text_sample()
            if sample_present:
                logger.info(
                    "Parent-window retrieval: ENABLED and at least one indexed chunk "
                    "has a stored parent window."
                )
            else:
                logger.warning(
                    "Parent-window retrieval is enabled but no indexed chunks have a "
                    "stored parent window text. Queries will degrade to legacy "
                    "small-chunk rendering until documents are reindexed. To backfill, "
                    "delete and re-add the affected files."
                )
        except Exception as exc:
            logger.warning(
                "Parent-window startup check failed (continuing): %s", exc
            )

    # Inject the embedding service so memory hybrid retrieval can use dense
    # search. MemoryStore degrades gracefully to FTS-only when the embedding
    # service is unavailable.
    app.state.memory_store = MemoryStore(
        app.state.db_pool, embedding_service=app.state.embedding_service
    )
    app.state.secret_manager = SecretManager()
    app.state.toggle_manager = ToggleManager(app.state.db_pool)
    try:
        app.state.csrf_manager = CSRFManager(
            settings.redis_url, settings.csrf_token_ttl,
            db_path=settings.sqlite_path,
        )
    except Exception as e:
        logger.warning(f"CSRF manager init failed (continuing): {e}")
        app.state.csrf_manager = None
    app.state.maintenance_service = MaintenanceService(app.state.db_pool)
    app.state.llm_health_checker = LLMHealthChecker(
        embedding_service=app.state.embedding_service,
        llm_client=app.state.llm_client,
        thinking_client=app.state.thinking_llm_client,
        instant_client=app.state.instant_llm_client,
    )
    app.state.model_checker = ModelChecker()
    app.state.model_validation = (
        settings.enable_model_validation
        or app.state.toggle_manager.get_toggle(
            "model_validation", settings.enable_model_validation
        )
    )
    ingestion_llm_client = select_ingestion_llm_client(
        app, settings.ingestion_llm_mode
    )

    # Initialize background processor as singleton (runs continuously)
    try:
        multimodal_service = None
        try:
            from app.services.multimodal_enrichment import (
                ArtifactEnrichmentService,
                MultimodalProviderClient,
            )

            multimodal_client = MultimodalProviderClient()
            app.state.multimodal_client = multimodal_client
            multimodal_service = ArtifactEnrichmentService(
                pool=app.state.db_pool, client=multimodal_client
            )
        except Exception as exc:  # noqa: BLE001 - multimodal enrichment is optional
            logger.warning("Multimodal enrichment service not available: %s", exc)
            app.state.multimodal_client = None

        app.state.background_processor = get_background_processor(
            max_retries=3,
            retry_delay=1.0,
            # is-not-None guards: an operator-set 0 is a meaningful value
            # (zero overlap must stay zero), not "unset" — same contract as
            # DocumentProcessor._get_chunker (issue #513 defect class).
            chunk_size_chars=(
                settings.chunk_size_chars
                if settings.chunk_size_chars is not None
                else 2000
            ),
            chunk_overlap_chars=(
                settings.chunk_overlap_chars
                if settings.chunk_overlap_chars is not None
                else 200
            ),
            vector_store=app.state.vector_store,
            embedding_service=app.state.embedding_service,
            maintenance_service=app.state.maintenance_service,
            pool=app.state.db_pool,
            llm_client=ingestion_llm_client,
            multimodal_service=multimodal_service,
        )
        await _safe_await(
            app.state.background_processor.start(),
            "Background processor start",
            timeout=10,
        )
    except Exception as e:
        logger.warning(f"Background processor start failed (continuing): {e}")
        app.state.background_processor = None

    # Initialize email ingestion service if enabled
    try:
        app.state.email_service = EmailIngestionService(
            settings=settings,
            pool=app.state.db_pool,
            background_processor=app.state.background_processor,
        )
        await _safe_await(
            app.state.email_service.start_polling(), "Email service start", timeout=10
        )
    except Exception as e:
        logger.warning(f"Email service start failed (continuing): {e}")
        app.state.email_service = None

    # Start FileWatcher for auto-scanning directories
    try:
        app.state.file_watcher = FileWatcher(
            app.state.background_processor, pool=app.state.db_pool
        )
        await _safe_await(
            app.state.file_watcher.start(), "FileWatcher start", timeout=10
        )
    except Exception as e:
        logger.warning(f"FileWatcher start failed (continuing): {e}")
        app.state.file_watcher = None

    # Initialize WikiRetrievalService using the app's DB pool
    app.state.wiki_retrieval = WikiRetrievalService(pool=app.state.db_pool)
    logger.info("WikiRetrievalService initialized")

    # Initialize KMSRetrievalService using the app's DB pool
    app.state.kms_retrieval = KMSRetrievalService(pool=app.state.db_pool)
    logger.info("KMSRetrievalService initialized")

    # Draft Room Ready-evidence reconciler (SPEC section 12.6). Runs BEFORE any
    # job processor starts and before HTTP traffic is served, so an out-of-band
    # or database-only source deletion cannot leave a draft sitting in Ready on
    # stale evidence. Bounded and paginated; never fatal to startup.
    try:
        from app.services.draft_evidence_freshness import reconcile_ready_evidence

        await _safe_await(
            reconcile_ready_evidence(app.state.db_pool),
            "Draft Room Ready-evidence reconcile",
            timeout=30,
        )
    except Exception as e:
        logger.warning(
            "Draft Room Ready-evidence reconcile failed (continuing): %s", e
        )

    # Start WikiCompileProcessor (background wiki job worker)
    try:
        app.state.wiki_compile_processor = WikiCompileProcessor(pool=app.state.db_pool)
        await _safe_await(
            app.state.wiki_compile_processor.start(),
            "WikiCompileProcessor start",
            timeout=10,
        )
    except Exception as e:
        logger.warning("WikiCompileProcessor start failed (continuing): %s", e)
        app.state.wiki_compile_processor = None

    # Start DraftJobProcessor (background Draft Room parse-job worker).
    # Not gated on settings.draft_room_enabled: SPEC section 9.2 requires owner
    # cleanup and durable orphan/job recovery to keep working even while the
    # feature is disabled for new work. The DISABLED gate belongs on the
    # mutating HTTP routes, not on this background worker.
    try:
        app.state.draft_job_processor = DraftJobProcessor(
            pool=app.state.db_pool,
            storage=DraftInputStorage(settings.data_dir / "draft-room"),
            extraction=DocumentExtractionService(),
        )
        await _safe_await(
            app.state.draft_job_processor.start(),
            "DraftJobProcessor start",
            timeout=10,
        )
    except Exception as e:
        logger.warning("DraftJobProcessor start failed (continuing): %s", e)
        app.state.draft_job_processor = None

    # Start KMSCompileProcessor (background KMS job worker) only if KMS is enabled
    if settings.kms_enabled:
        try:
            app.state.kms_compile_processor = KMSCompileProcessor(pool=app.state.db_pool)
            await _safe_await(
                app.state.kms_compile_processor.start(),
                "KMSCompileProcessor start",
                timeout=10,
            )
        except Exception as e:
            logger.warning("KMSCompileProcessor start failed (continuing): %s", e)
            app.state.kms_compile_processor = None
    else:
        logger.info("KMS subsystem disabled; skipping KMSCompileProcessor startup")
        app.state.kms_compile_processor = None

    # Initialize RAGEngine singleton with cached services
    app.state.rag_engine = RAGEngine(
        embedding_service=app.state.embedding_service,
        vector_store=app.state.vector_store,
        memory_store=app.state.memory_store,
        llm_client=app.state.llm_client,
        reranking_service=app.state.reranking_service,
        wiki_retrieval=app.state.wiki_retrieval,
        kms_retrieval=app.state.kms_retrieval,
        thinking_client=app.state.thinking_llm_client,
        instant_client=app.state.instant_llm_client,
    )
    logger.info("RAGEngine singleton initialized with wiki retrieval")

    # Hand the engine to the Draft Room worker. DraftJobProcessor is started
    # above, before RAGEngine exists, so compile retrieval can only be wired
    # here. Without this call the worker keeps its fail-closed retrieval stub
    # and every compile job would end in retrieval_unavailable.
    if getattr(app.state, "draft_job_processor", None):
        app.state.draft_job_processor.set_rag_engine(app.state.rag_engine)
        logger.info("DraftJobProcessor wired to RAGEngine for compile retrieval")

    # Start memory embedding backfill as a non-blocking background task.
    # Memories created before the embedding column existed (or with a stale model)
    # will be embedded so hybrid/semantic retrieval can use them.
    async def _run_memory_backfill() -> None:
        try:
            summary = await app.state.memory_store.backfill_missing_embeddings()
            if summary["total"] > 0:
                logger.info("Memory embedding backfill summary: %s", summary)
        except Exception as exc:
            logger.warning("Memory embedding backfill startup task failed: %s", exc)

    # Hold a reference and cancel on shutdown so the backfill task is not
    # orphaned at process exit (mirrors memory_eviction_task below).
    memory_backfill_task = asyncio.create_task(_run_memory_backfill())


    # Start periodic memory eviction as a background task so
    # evict_expired_memories no longer runs on every search_memories call
    # (issue #263 — moves a DELETE + COMMIT off the RAG query hot path).
    memory_eviction_task = asyncio.create_task(
        app.state.memory_store.periodic_eviction_loop(
            interval=settings.memory_eviction_interval_seconds
        )
    )

    # Provider residency priming (issue #571): one native model-load call per
    # long-lived backend replaces the retired 30-second max_tokens=1 ping
    # loop — Ollama pins keep_alive/num_ctx via /api/generate, LM Studio
    # requests context_length via its native load API (per-request ttl rides
    # every chat payload). Best-effort: a failed prime logs and continues;
    # the model then loads on demand with provider defaults.
    async def _prime_residency(name: str, client) -> None:
        try:
            if await client.prime_residency():
                logger.info("%s model residency primed", name)
            else:
                logger.debug("%s client needs no residency priming", name)
        except Exception as e:  # noqa: BLE001 — priming must never fail startup
            logger.warning("%s residency priming failed (continuing): %s", name, e)

    residency_tasks = []
    if getattr(app.state, "thinking_llm_client", None):
        residency_tasks.append(
            asyncio.create_task(
                _prime_residency("Thinking", app.state.thinking_llm_client)
            )
        )
    if getattr(app.state, "instant_llm_client", None):
        residency_tasks.append(
            asyncio.create_task(
                _prime_residency("Instant", app.state.instant_llm_client)
            )
        )
    # An explicitly configured editorial endpoint gets its own prime; when
    # unset its URL+model equal the thinking client's, which the prime above
    # already pins on the same server. The throwaway editorial client is
    # closed after priming (it is not a long-lived app.state client).
    if settings.editorial_chat_url or settings.editorial_chat_model:
        from app.services.llm_client import create_editorial_client

        async def _prime_editorial() -> None:
            editorial_client = create_editorial_client()
            try:
                await _prime_residency("Editorial", editorial_client)
            finally:
                await editorial_client.close()

        residency_tasks.append(asyncio.create_task(_prime_editorial()))

    yield

    # Shutdown: stop services, cancel background tasks
    # Stop email ingestion service
    if app.state.email_service:
        await app.state.email_service.stop_polling()
    for rt in residency_tasks:
        rt.cancel()
        try:
            await rt
        except asyncio.CancelledError:
            pass
    # Cancel the periodic memory eviction background task.
    if memory_eviction_task:
        memory_eviction_task.cancel()
        try:
            await memory_eviction_task
        except asyncio.CancelledError:
            pass
    # Cancel the one-shot memory embedding backfill task if still running.
    if memory_backfill_task and not memory_backfill_task.done():
        memory_backfill_task.cancel()
        try:
            await memory_backfill_task
        except asyncio.CancelledError:
            pass
    if app.state.file_watcher:
        await app.state.file_watcher.stop()
    if app.state.background_processor:
        await app.state.background_processor.stop()
    if getattr(app.state, "wiki_compile_processor", None):
        await app.state.wiki_compile_processor.stop()
    if getattr(app.state, "draft_job_processor", None):
        await app.state.draft_job_processor.stop()
    if getattr(app.state, "kms_compile_processor", None):
        await app.state.kms_compile_processor.stop()
    # E3 admission shutdown (issue #518, swarm review F-006): after all
    # background workers have drained, release their admission slots,
    # reject further admits and close a Redis shared store's connection
    # pool. Must run AFTER the processor stop() calls above — those drains
    # hold background leases until they finish.
    try:
        from app.services.admission import get_admission_controller

        controller = get_admission_controller()
        await controller.shutdown()
        close = getattr(controller.store, "close", None)
        if close is not None:
            await close()
    except Exception:
        logger.exception("Admission shutdown failed")
    # Close both underlying LLM clients. The ``llm_client`` attr is an
    # alias of ``thinking_llm_client`` so closing it separately is
    # unnecessary; ``LLMClient.close()`` is also idempotent.
    try:
        await app.state.thinking_llm_client.close()
    except Exception:
        pass
    try:
        await app.state.instant_llm_client.close()
    except Exception:
        pass
    try:
        await app.state.embedding_service.close()
    except Exception:
        pass
    try:
        await app.state.reranking_service.close()
    except Exception:
        pass
    try:
        app.state.vector_store.close()
    except Exception:
        pass
    try:
        app.state.db_pool.close_all()
    except Exception:
        pass
    try:
        from app.services.auth_service import _auth_executor
        _auth_executor.shutdown(wait=False)
    except Exception:
        pass
