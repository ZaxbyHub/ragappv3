"""
Lifespan context manager for FastAPI application startup and shutdown.
"""

import asyncio
import logging
import sqlite3
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import settings
from app.models.database import get_pool, run_migrations
from app.security import CSRFManager
from app.services.background_tasks import get_background_processor
from app.services.email_service import EmailIngestionService
from app.services.embeddings import EmbeddingService
from app.services.file_watcher import FileWatcher
from app.services.kms_compile_processor import KMSCompileProcessor
from app.services.llm_client import (
    LLMClient,
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
from app.services.toggle_manager import ToggleManager
from app.services.vector_store import VectorStore, VectorStoreError
from app.services.wiki_compile_processor import WikiCompileProcessor
from app.services.wiki_retrieval import WikiRetrievalService

logger = logging.getLogger(__name__)


def select_ingestion_llm_client(app: FastAPI, mode: str):
    """Select the LLM client used by optional ingestion LLM work."""
    if mode == "instant":
        return app.state.instant_llm_client
    if mode == "thinking":
        return app.state.thinking_llm_client
    return None


async def _llm_keepalive_task(llm_client: LLMClient, interval: int = 30):
    """
    Background task to keep LLM model loaded in LM Studio.

    LM Studio unloads models when clients disconnect. This task periodically
    sends a ping request to keep the model in memory.

    Args:
        llm_client: The LLM client instance
        interval: Seconds between keep-alive pings (default: 30)
    """
    logger.info("Starting LLM keep-alive task (interval: %ds)", interval)

    while True:
        try:
            await asyncio.sleep(interval)
            # Send a simple completion to keep model loaded
            messages = [{"role": "user", "content": "ping"}]
            await llm_client.chat_completion(messages, max_tokens=1)
            logger.debug("LLM keep-alive ping sent successfully")
        except asyncio.CancelledError:
            logger.info("LLM keep-alive task cancelled")
            break
        except Exception as e:
            # Log but don't crash - model might just be unloaded
            logger.debug("LLM keep-alive ping failed (model may be unloaded): %s", e)


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
    import json

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

        # New fields — load directly without legacy conversion
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
            "instant_initial_retrieval_top_k",
            "instant_reranker_top_n",
            "instant_memory_context_top_k",
            "instant_max_tokens",
            "vector_top_k",
            "kms_enabled",
            "kms_compile_on_ingest",
        ]
        for key in NEW_DIRECT_KEYS:
            if key in persisted:
                try:
                    if not hasattr(settings, key):
                        logger.warning(f"Unknown persisted setting {key}, skipping")
                        continue
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager for startup and shutdown events."""
    # Startup: Initialize database and services
    try:
        run_migrations(str(settings.sqlite_path))
    except Exception as e:
        logger.error(
            "Database migration failed: %s — app will start with degraded database state",
            e,
        )
    _load_persisted_settings(str(settings.sqlite_path))

    # Migrate uploads to per-vault directories (run before accepting requests)
    try:
        from app.services.upload_path import migrate_uploads

        logger.info("Checking for upload migration...")
        await asyncio.wait_for(asyncio.to_thread(migrate_uploads, False), timeout=15)
    except Exception as e:
        logger.warning(f"Upload migration failed (continuing anyway): {e}")

    app.state.db_pool = get_pool(str(settings.sqlite_path), max_size=10)

    # Dual LLM clients: Thinking (gpt-oss-120b on DGX Spark via ollama_chat_url)
    # and Instant (Nemotron 3 Nano 4B on LM Studio via instant_chat_url).
    app.state.thinking_llm_client = create_thinking_client()
    await _safe_await(
        app.state.thinking_llm_client.start(),
        "Thinking LLM client start",
        timeout=10,
    )
    app.state.instant_llm_client = create_instant_client()
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
        try:
            import httpx

            async with httpx.AsyncClient(timeout=10.0) as client:
                info_url = (
                    app.state.embedding_service.embeddings_url.rstrip("/") + "/info"
                )
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

    # Validate FTS index exists if hybrid search is enabled
    if settings.hybrid_search_enabled:
        try:
            indices = await app.state.vector_store.table.list_indices()
            fts_index_exists = any(idx.name == "fts_text" for idx in indices)
            if not fts_index_exists:
                logger.error(
                    "Hybrid search is enabled but the FTS index is missing on the 'text' column. "
                    "FTS search will not function. Create the index with "
                    "VectorStore._ensure_fts_index() or rebuild the table."
                )
        except Exception as e:
            logger.error(
                f"Failed to check FTS index status (hybrid search may not work): {e}"
            )

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
    except VectorStoreError as e:
        logger.error("=" * 60)
        logger.error("VECTOR STORE SCHEMA VALIDATION FAILED")
        logger.error("=" * 60)
        logger.error(f"Error: {e}")
        logger.error("The embedding dimension has changed. A full reindex is required.")
        logger.error("Please run the reindex process or delete the LanceDB database.")
        logger.error("=" * 60)
        # Continue startup but warn that reindex is needed
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
            settings.redis_url, settings.csrf_token_ttl
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
        app.state.background_processor = get_background_processor(
            max_retries=3,
            retry_delay=1.0,
            chunk_size_chars=settings.chunk_size_chars or 2000,
            chunk_overlap_chars=settings.chunk_overlap_chars or 200,
            vector_store=app.state.vector_store,
            embedding_service=app.state.embedding_service,
            maintenance_service=app.state.maintenance_service,
            pool=app.state.db_pool,
            llm_client=ingestion_llm_client,
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
        thinking_client=app.state.thinking_llm_client,
        instant_client=app.state.instant_llm_client,
    )
    logger.info("RAGEngine singleton initialized with wiki retrieval")

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

    asyncio.create_task(_run_memory_backfill())

    # Start LLM keep-alive tasks for both backends to prevent unload on idle.
    keepalive_task_thinking = None
    keepalive_task_instant = None
    try:
        keepalive_task_thinking = asyncio.create_task(
            _llm_keepalive_task(app.state.thinking_llm_client)
        )
    except Exception as e:
        logger.warning(f"Thinking LLM keepalive task failed (continuing): {e}")
    try:
        keepalive_task_instant = asyncio.create_task(
            _llm_keepalive_task(app.state.instant_llm_client)
        )
    except Exception as e:
        logger.warning(f"Instant LLM keepalive task failed (continuing): {e}")

    yield

    # Shutdown: Cancel keepalive, stop file watcher, and close services
    # Stop email ingestion service
    if app.state.email_service:
        app.state.email_service.stop_polling()
    for kt in (keepalive_task_thinking, keepalive_task_instant):
        if kt:
            kt.cancel()
            try:
                await kt
            except asyncio.CancelledError:
                pass
    if app.state.file_watcher:
        await app.state.file_watcher.stop()
    if app.state.background_processor:
        await app.state.background_processor.stop()
    if getattr(app.state, "wiki_compile_processor", None):
        await app.state.wiki_compile_processor.stop()
    if getattr(app.state, "kms_compile_processor", None):
        await app.state.kms_compile_processor.stop()
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
