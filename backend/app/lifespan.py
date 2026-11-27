"""
Lifespan context manager for FastAPI application startup and shutdown.
"""

import asyncio
import json
import logging
import os
import sqlite3
import stat
import time
from contextlib import asynccontextmanager
from contextlib import suppress as contextlib_suppress
from typing import Union, get_args, get_origin

from fastapi import FastAPI

from app.api.routes.settings import PERSISTED_FUNCTIONAL_FIELDS
from app.config import Settings, canonicalize_vector_metric, settings
from app.middleware.logging import SensitiveFieldFilter
from app.models.database import SQLiteConnectionPool, get_pool, run_migrations
from app.models.migration_journal import MIGRATION_CONNECT_TIMEOUT_SECONDS
from app.security import CSRFManager
from app.services.audit_redaction import run_startup_audit_redaction
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

# Aggregate process-shutdown budget (#799/#854, plan rev 7-10): one deadline
# covering every lifespan shutdown stage (email, residency, watcher, the
# four processor stops, admission, store/pool closes, the upload-commit and
# turn-finalizer gates).
#
# The process envelope's ONE derivation, quoted VERBATIM by
# docker-compose.yml, both .ps1 scripts, the release fragment and the PR
# body. The three ENFORCED terms are bounded by the code that implements
# them; the two ALLOWANCE terms (the executor-join tail and the
# interpreter teardown) are bounded by NO code:
#   enforced: 20 s (uvicorn request drain) + 65 s (lifespan aggregate) + 15 s (turn-finalizer drain) = 100 s; allowance: 30 s (SQLite-family executor-join tail) + 5 s (teardown) = 35 s; total 135 s
# (the 65 s is this 60 s budget plus the 5 s outer cap on the processor
# stop; the 15 s is the turn-finalizer drain's 5 s producer retirement plus
# its 10 s drain.) The 135 s composed envelope sits below the 140 s native
# stop wait and the 150 s compose grace, so a shutdown that stays inside
# the enforced 100 s is never force-killed mid-flight.
# scripts/check_p03_shutdown_envelope.py owns that arithmetic: it reads
# every enforced term from the code that enforces it and fails when a
# surface stops quoting this line.
_SHUTDOWN_BUDGET_SECONDS = 60.0

# The extra cap the lifespan puts on the background processor's stop: the
# processor's own aggregate drain is given the REMAINING budget plus this
# slack, so the composed process envelope stays inside one derivable
# worst case. Named (not inlined) so the envelope contract gate
# (scripts/check_p03_shutdown_envelope.py) reads this number from the code
# that enforces it instead of restating it.
_PROCESSOR_STOP_CAP_SECONDS = 5.0


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
    except URLBlocked as e:
        # Environment-shaped failure (#799 AC3): the constructor runs the
        # live-DNS assert_url_safe, so a blocked/unresolvable endpoint must
        # degrade the client instead of failing startup — same degraded
        # state as "not configured" until an operator fixes the endpoint.
        logger.warning(
            "Thinking chat endpoint rejected by the SSRF guard; chat is "
            "disabled until the endpoint is fixed: %s",
            e,
        )
    try:
        instant = create_instant_client()
    except ModelNotConfiguredError:
        logger.info(
            "Instant chat model not configured; instant mode is disabled "
            "until an endpoint is set (Settings -> Models or "
            "INSTANT_CHAT_URL/INSTANT_CHAT_MODEL)"
        )
    except URLBlocked as e:
        logger.warning(
            "Instant chat endpoint rejected by the SSRF guard; instant mode "
            "is disabled until the endpoint is fixed: %s",
            e,
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
    except Exception:
        # Persisted values may contain credentials or other sensitive settings.
        # Log the field name only; callers retain the validation boundary.
        logger.warning("Persisted setting %s failed validation", key)
        return False


def _batch_validate_persisted(converted_pairs: dict) -> bool:
    """Validate ALL persisted values as one merged candidate state.

    Cross-field model validators (chunk overlap vs size, jobs-lease
    coherence) reject a persisted pair when each key is validated alone
    against the not-yet-updated singleton, silently reverting one field of
    an operator-saved pair on restart (issue #698 feedback). Validating the
    merged candidate once makes a jointly-valid pair replay as a unit;
    callers fall back to per-key replay when this returns False.
    """
    try:
        candidate = settings.model_dump()
        candidate.update(converted_pairs)
        type(settings).model_validate(candidate)
        return True
    except Exception:
        logger.warning("Persisted settings rejected as a set; falling back to per-key replay")
        return False


def _load_persisted_settings(sqlite_path: str) -> None:
    """Load user-configurable settings from DB if they were previously saved."""
    conn = sqlite3.connect(sqlite_path, timeout=MIGRATION_CONNECT_TIMEOUT_SECONDS)
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
                except Exception:
                    logger.warning("Failed to restore persisted setting %s", key)

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
            # Reindex stall watchdog budget (issue #1010)
            "jobs_stall_timeout_seconds",
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
            # Operator API keys (issue #622): settings-kv replay so a saved
            # key survives restart (mirror of PERSISTED_FUNCTIONAL_FIELDS).
            "chat_api_key",
            "instant_api_key",
            "default_chat_mode",
            "ingestion_llm_mode",
            "instant_initial_retrieval_top_k",
            "instant_reranker_top_n",
            "instant_memory_context_top_k",
            "instant_max_tokens",
            "thinking_max_tokens",
            "assistant_message_max_chars",
            "instant_enable_thinking",
            # Per-mode HTTP request timeouts (issue #652)
            "thinking_request_timeout_seconds",
            "editorial_request_timeout_seconds",
            "instant_request_timeout_seconds",
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
        #
        # Two-pass replay (issue #698 feedback): convert every persisted key
        # first, then batch-validate the merged candidate state ONCE before
        # applying. Per-key validation alone rejects a jointly-valid pair
        # (e.g. chunk_size_chars=150 + chunk_overlap_chars=100 replayed over
        # boot defaults {2000,200}) because the first key is checked against
        # the not-yet-updated sibling — silently reverting one field of an
        # operator-saved pair on restart. When the merged set is rejected,
        # fall back to the historical per-key behavior (warn + skip each
        # offender individually).
        converted_pairs: dict = {}
        for key in PERSISTED_FUNCTIONAL_FIELDS:
            if key not in persisted:
                continue
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
                if key == "vector_metric":
                    converted = canonicalize_vector_metric(converted)
                converted_pairs[key] = converted
            except Exception:
                logger.warning("Failed to restore persisted setting %s", key)
        if converted_pairs and _batch_validate_persisted(converted_pairs):
            for key, converted in converted_pairs.items():
                setattr(settings, key, converted)
        else:
            for key in PERSISTED_FUNCTIONAL_FIELDS:
                if key not in converted_pairs:
                    continue
                converted = converted_pairs[key]
                try:
                    if _validate_setting_value(key, converted):
                        setattr(settings, key, converted)
                except Exception:
                    logger.warning("Failed to restore persisted setting %s", key)
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


async def _await_migration(coro, name):
    """Await a migration WITHOUT a cancel-on-timeout deadline (issue #706).

    A wall-clock deadline can cancel a staging swap mid-drop — CancelledError
    is a BaseException and bypasses the swap's except-Exception critical
    handling, leaving a half-swapped table whose next-boot reconcile could
    silently discard one generation. Migrations run unbounded; genuine
    failures are still caught-and-logged (fail-soft startup).

    A shutdown cancellation (SIGTERM/restart mid-boot) is the remaining
    cancel source: it is logged CRITICAL before propagating, so the operator
    gets the same "swap was interrupted, staging holds the recoverable
    copy" evidence the removed deadline used to suppress.
    """
    try:
        await coro
    except Exception as e:
        logger.warning(f"{name} failed: {e} (continuing)")
    except BaseException:
        logger.critical(
            f"{name} was interrupted by cancellation mid-swap; the staging "
            f"table holds the recoverable copy and a restart reconciles it "
            f"before the app serves."
        )
        raise


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
                "FTS search will not function. Trigger a full reindex from the "
                "admin UI (a full reindex recreates the FTS index) or disable "
                "hybrid search (ENABLE_HYBRID_SEARCH=false)."
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
    #
    # Same-process restart hygiene (#799/#854): a previous lifespan entry in
    # this interpreter may have set the curator shutdown flag / shut its
    # executor; clear the flag and let the recreate-on-demand accessors
    # replace any dead executors before startup proceeds.
    with contextlib_suppress(Exception):
        from app.services.wiki_compiler import CURATOR_SHUTDOWN

        CURATOR_SHUTDOWN.clear()
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
    # Data-directory writability preflight (issue #749, TQ-differential-e-03):
    # a bind mount the Docker daemon created root-owned aborts startup further
    # down with bare sqlite/LanceDB tracebacks. Fail HERE instead with one
    # actionable message naming the directory, the current uid, and the owner
    # uid — the criterion's explicitly permitted "fail with a message naming
    # the directory/owner and uid" arm. W_OK|X_OK: creating children requires
    # the search bit too, so a write-but-not-searchable directory must fail
    # HERE with the named remedy rather than later as a generic migration
    # error. A NONEXISTENT directory passes: fresh starts rely on the DB layer
    # creating parents, and that flow is untouched. The message reports the
    # CONTAINER-side path as the observed condition and describes the host
    # remediation in prose — it deliberately does NOT print a runnable host
    # command for the container path (the host bind source is not knowable
    # from inside the container), and the path is never interpolated into
    # shell syntax. The existence gate is an explicit stat(), not
    # Path.exists(): exists() raises a bare PermissionError on 3.11 and
    # returns False on 3.12+ when the PARENT is unsearchable, either of which
    # skips the named message (batch-5 review). Only FileNotFoundError means
    # "fresh start"; any other stat failure is itself the unusable-directory
    # evidence.
    _data_dir = settings.data_dir
    _owner_uid = None
    _dd_unusable = False
    try:
        _dd_stat = _data_dir.stat()
    except FileNotFoundError:
        pass
    except OSError:
        _dd_unusable = True
    else:
        _owner_uid = getattr(_dd_stat, "st_uid", None)
        # A regular FILE with W/X bits would pass os.access and fail later
        # with a bare NotADirectoryError under it (batch-7 review).
        _dd_unusable = not stat.S_ISDIR(_dd_stat.st_mode) or not os.access(
            _data_dir, os.W_OK | os.X_OK
        )
    if _dd_unusable:
        _current_uid = getattr(os, "getuid", lambda: None)()
        raise RuntimeError(
            f"Data directory is not a writable/searchable directory: {_data_dir}\n"
            f"  current uid : {_current_uid}\n"
            f"  owner uid   : {_owner_uid}\n"
            "The application stores its database and vector index under this\n"
            "directory and cannot start without write and search permission.\n"
            "This path is as seen INSIDE the container; the directory to fix\n"
            "is the bind-mount SOURCE on the host that maps to it (compose\n"
            "maps ${HOST_DATA_DIR:-./data} to this location). On the HOST:\n"
            "create that directory (mkdir -p) and hand it to uid 1000\n"
            "(sudo chown -R 1000:1000, quoting the host path if it contains\n"
            "spaces) — see the README first-run steps — then re-run."
        )
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
    # Issue #699: clean boots no longer write routine journal rows, and an
    # UNRESOLVED failure/recovery (no later succeeded row for the same
    # migration — on upgraded databases it is typically buried under
    # pre-existing noise rows) is surfaced as one extra signal line; resolved
    # failures are never re-warned.
    try:
        from app.models.migration_journal import latest_outcomes_with_signal

        _recent, _signal = latest_outcomes_with_signal(
            str(settings.sqlite_path), limit=3
        )
        if _recent:
            logger.info(
                "Migration journal (latest %d): %s",
                len(_recent),
                "; ".join(
                    f"{row['migration_name']}[{row['phase']}:{row['outcome']}]"
                    for row in _recent
                ),
            )
        if _signal is not None:
            logger.warning(
                "Migration journal (unresolved failure/recovery, %s, outside "
                "latest %d): %s[%s:%s] %s",
                _signal.get("created_at") or "unknown time",
                len(_recent),
                _signal["migration_name"],
                _signal["phase"],
                _signal["outcome"],
                _signal.get("detail") or "",
            )
    except Exception as e:  # pragma: no cover - journal is best-effort
        logger.debug("Could not read migration journal at startup: %s", e)
    # Guarded like the migration step above (issue #749, TQ-differential-e-03):
    # an unopenable settings database (e.g. a root-owned bind mount) must not
    # abort startup with a bare sqlite traceback — it routes into the same
    # degraded-start signal so /api/healthz reports 503 (issues #550, #749).
    try:
        _load_persisted_settings(str(settings.sqlite_path))
    except RuntimeError:
        # The persisted-schema drift guard raises RuntimeError by contract
        # (issue #494 CONFIG-003): a drift must ABORT startup, not boot
        # degraded with a silently reverted settings surface (batch-2 review
        # F-06). Only unexpected load failures degrade.
        raise
    except Exception as e:
        app.state.migrations_ok = False
        logger.error(
            "Persisted settings load failed: %s — app will start with degraded database state",
            e,
        )

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
    # [issue #692 / P03-SK2-06] No asyncio.wait_for here: it cancels only the
    # awaiting coroutine, never the to_thread worker, so a timeout would let
    # the migration keep copying/renaming after startup completes — during
    # which row-based document reads could observe a half-copied destination.
    # The await below genuinely blocks startup until the migration completes,
    # which is what the comment above promises. A failure still must not
    # brick boot (try/except below, unchanged).
    try:
        from app.services.upload_path import migrate_uploads

        logger.info("Checking for upload migration...")
        _migration_started = time.monotonic()
        await asyncio.to_thread(migrate_uploads, False)
        # Record the outcome for readiness (#799 AC6): healthz reads this
        # attribute and reports degraded (503) when a migration FAILED while
        # the app deliberately continued ("continuing anyway" must stay
        # observable to the health probe that exists to surface exactly
        # this degradation).
        app.state.upload_migration_ok = True
        logger.info(
            "Upload migration finished in %.1fs",
            time.monotonic() - _migration_started,
        )
    except Exception as e:
        app.state.upload_migration_ok = False
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
    # Zero-downtime switchover wiring (issue #696): while a staged rebuild is
    # open on the store, the embedding service pins embed_single/embed_passage
    # to the SERVING generation's identity (the draining snapshot captured at
    # the identity-changing settings save) so queries keep landing in the old
    # generation's embedding space until cutover. embed_batch (which builds
    # the new generation) is deliberately unpinned.
    app.state.embedding_service.set_serving_identity_provider(
        app.state.vector_store.serving_embedding_identity
    )
    # Critical: fail fast if the vector store cannot connect or initialize its table.
    # Without these two, no search or ingestion is possible.
    await asyncio.wait_for(app.state.vector_store.connect(), timeout=15)
    # [issue #706] Migrations run WITHOUT a cancel-on-timeout deadline: a
    # deadline cancelling a mid-swap migration can silently drop a corpus
    # generation (see _await_migration).
    await _await_migration(
        app.state.vector_store.migrate_add_vault_id(),
        "Vector store migrate vault_id",
    )
    await _await_migration(
        app.state.vector_store.migrate_add_chunk_scale(),
        "Vector store migrate chunk_scale",
    )
    await _await_migration(
        app.state.vector_store.migrate_add_sparse_embedding(),
        "Vector store migrate sparse",
    )
    await _await_migration(
        app.state.vector_store.migrate_add_parent_window(),
        "Vector store migrate parent_window",
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
    # Issue #894: scrub raw admin-token material from historical
    # audit_toggle_log rows now that the HMAC key exists; fail-soft with an
    # app-state flag (audit_redaction_ok), mirroring migrations_ok above.
    await asyncio.to_thread(run_startup_audit_redaction, app)
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
    # will be embedded so hybrid/semantic retrieval can use them. The run
    # holds the store-level backfill lock (issue #937) so the startup pass,
    # the periodic embed-heal loop, and the superadmin backfill route are
    # single-flight within this process (per-worker in multi-worker
    # deployments; duplicate work there is idempotent).
    async def _run_memory_backfill() -> None:
        try:
            async with app.state.memory_store.get_backfill_lock():
                summary = await (
                    app.state.memory_store.backfill_missing_embeddings()
                )
                if summary["total"] > 0:
                    logger.info(
                        "Memory embedding backfill summary: %s", summary
                    )
        except Exception as exc:
            logger.warning("Memory embedding backfill startup task failed: %s", exc)

    # Hold a reference and cancel on shutdown so the backfill task is not
    # orphaned at process exit (mirrors memory_eviction_task below).
    memory_backfill_task = asyncio.create_task(_run_memory_backfill())

    # Periodic embed-heal (issue #937): the third healer for NULL-embedding
    # rows. Bounded by the configured interval; NULL-only selection and
    # per-row EMBEDDING admission keep it inside the #939 embedding budget.
    memory_embed_heal_task = asyncio.create_task(
        app.state.memory_store.periodic_embed_heal_loop(
            interval=settings.memory_embed_heal_interval_seconds
        )
    )

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
            # Construction lives INSIDE the try (#799 AC1): a misconfigured
            # editorial pair must be contained in this task (logged, startup
            # continues) instead of escaping the task — an escaped exception
            # is re-raised by the shutdown loop's `await rt` and would skip
            # every later shutdown stage (the AC2 hazard).
            editorial_client = None
            try:
                editorial_client = create_editorial_client()
                await _prime_residency("Editorial", editorial_client)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — priming must never fail startup
                logger.warning(
                    "Editorial residency priming failed (continuing): %s", e
                )
            finally:
                if editorial_client is not None:
                    await editorial_client.close()

        residency_tasks.append(asyncio.create_task(_prime_editorial()))

    yield

    # Shutdown: stop services, cancel background tasks — under ONE aggregate
    # process-shutdown budget (#799/#854). Every stage below runs through
    # _stop_stage with the REMAINING budget; an exhausted budget skips the
    # stage with a warning instead of an unbounded await. Only
    # BackgroundProcessor.stop() takes a timeout argument (the sibling
    # processors' stop() methods take none); everything else is wrapped.
    _shutdown_deadline = time.monotonic() + _SHUTDOWN_BUDGET_SECONDS

    def _remaining() -> float:
        return max(0.0, _shutdown_deadline - time.monotonic())

    # Abandoned shutdown stages are RETAINED until they finish: an
    # unreferenced task can be garbage-collected while it is still running,
    # so a strong reference keeps it alive until its done callback drops
    # it. The set lives in this lifespan's scope, so it dies with it.
    _detached_stages: set[asyncio.Task] = set()

    async def _stop_stage(name: str, factory, timeout: float | None = None):
        """Await one shutdown stage under the remaining process budget.

        ``factory`` is a zero-arg callable returning the awaitable, so the
        coroutine is only created when the stage actually runs. ``timeout``
        overrides the budget (the background processor's stop gets the
        remaining budget plus its own documented cap).

        The bound does NOT depend on the stage's cooperation. The stage
        runs as a task and is waited for with ``asyncio.wait``, which
        returns the still-pending set instead of forwarding the wait;
        ``asyncio.wait_for`` forwards the timeout's cancellation INTO the
        stage and then WAITS for that cancellation to land, so a stage
        that suppresses or delays its cancellation would hold the whole
        shutdown past the aggregate budget (the same defect class the
        residency settle had). A stage that overruns is CANCELLED — asked
        to stop — and left detached: the shutdown continues without it,
        and its late outcome is logged rather than awaited.
        """
        budget = _remaining() if timeout is None else timeout
        if budget <= 0:
            logger.warning(
                "Shutdown stage '%s' skipped: aggregate budget exhausted",
                name,
            )
            return None
        try:
            task = asyncio.ensure_future(factory())
        except Exception:  # noqa: BLE001 — a stage that cannot even start never skips the rest
            logger.warning("Shutdown stage '%s' failed", name, exc_info=True)
            return None

        try:
            done, pending = await asyncio.wait({task}, timeout=budget)
        except asyncio.CancelledError:
            # THIS task was cancelled (an external cancel, not the
            # budget expiring): the stage is asked to stop and the
            # cancellation propagates.
            task.cancel()
            raise
        if pending:
            # The stage overran: ask it to stop, keep a strong reference
            # until it finishes, and report a late failure instead of
            # letting it surface as an unretrieved-exception warning.
            task.cancel()
            _detached_stages.add(task)
            task.add_done_callback(_detached_stages.discard)
            task.add_done_callback(
                lambda finished, stage_name=name: _log_detached_failure(
                    stage_name, finished
                )
            )
            logger.warning(
                "Shutdown stage '%s' exceeded the aggregate shutdown budget; "
                "continuing shutdown (it was asked to stop and is detached)",
                name,
            )
            return None
        try:
            return task.result()
        except asyncio.CancelledError:
            # The stage itself was cancelled (and honoured it): that is
            # not THIS task's cancellation, so it is absorbed — unless the
            # enclosing scope is being cancelled, which must propagate.
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
            return None
        except Exception:  # noqa: BLE001 — one stage's failure never skips the rest
            logger.warning("Shutdown stage '%s' failed", name, exc_info=True)
        return None

    # Stop email ingestion service
    if app.state.email_service:
        await _stop_stage("email stop_polling", app.state.email_service.stop_polling)
    async def _settle_residency_tasks() -> None:
        # One aggregate-bounded settle (#799 AC2 + #854): the whole loop runs
        # under ONE budget (the remaining process budget plus its documented
        # 5 s slack), and each task's outcome is absorbed — a task that DIED
        # with a natural exception is contained (the processor stops,
        # admission release, store/pool closes below stay reachable) instead
        # of skipping the whole shutdown chain.
        #
        # The bound must NOT depend on the residency tasks' COOPERATION.
        # Awaiting a task DIRECTLY makes the current task's cancellation
        # FORWARD into it: when the enclosing budget expires,
        # task.cancel() cancels the residency task, and a task that
        # suppresses that cancellation never completes — the await never
        # returns, the enclosing scope never regains control, and the whole
        # shutdown hangs until the outer force boundary. asyncio.wait()
        # waits for the set with a timeout and returns the still-pending
        # set instead, so a task that refuses to die costs the settle its
        # budget and nothing more.
        pending_settle: set[asyncio.Task] = set(residency_tasks)
        for rt in pending_settle:
            rt.cancel()
        if not pending_settle:
            return
        settle_budget = _remaining() + 5.0
        # An external cancellation of THIS task propagates out of the wait
        # (it is not our own budget expiring): swallowing it would strand
        # the shutdown.
        settled, pending_settle = await asyncio.wait(
            pending_settle, timeout=settle_budget
        )
        for rt in settled:
            # Retrieve each settled task's outcome: a task that DIED with a
            # natural exception must be absorbed here (AC2) so the rest of
            # the shutdown chain stays reachable.
            try:
                await rt
            except asyncio.CancelledError:
                # The task ended CANCELLED (we asked for that). This is NOT
                # the current task's own cancellation, so it is absorbed —
                # unless the enclosing scope is being cancelled, which must
                # propagate so the shutdown can continue. asyncio counts the
                # cancellations requested on the current task, so
                # cancelling() tells the two apart.
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
            except Exception as e:  # noqa: BLE001 — AC2 absorb
                logger.warning(
                    "Residency task ended with exception at shutdown "
                    "(continuing): %s",
                    e,
                )
        if pending_settle:
            # PROCESS-LEVEL INVARIANT (disclosed, #799/#854): the shutdown
            # SEQUENCE stays bounded — this settle ended on its own budget
            # and the stages below keep running. The PROCESS EXIT is
            # bounded by two mechanisms, and this warning names the one
            # that applies:
            #   * State A — the bounded tail exit fires (the registry is
            #     empty, the finalizers drained, the pool closed) and
            #     os._exit ends the process, so a detached task never
            #     delays the exit;
            #   * State B — the tail exit declines (a critical workflow is
            #     still in flight, so the pool is deliberately left open),
            #     the event loop's teardown then waits for this task, and
            #     the PROCESS-LEVEL FORCE BOUNDARY ends the process: the
            #     compose stop grace (150 s) or the native stop wait
            #     (140 s), both above the composed envelope.
            # A task that ignores cancellation can therefore delay the
            # exit in State B up to that force boundary — it cannot make
            # the shutdown sequence itself unbounded, and it cannot make
            # the process outlive the force limits in the shipped launch
            # configurations.
            logger.warning(
                "%d residency task(s) did not settle within the aggregate "
                "shutdown budget; continuing shutdown (they were asked to "
                "cancel and are detached; the process-level force boundary "
                "ends the exit if the bounded tail exit declines)",
                len(pending_settle),
            )

    await _settle_residency_tasks()
    # Cancel the periodic memory eviction background task.
    if memory_eviction_task:
        memory_eviction_task.cancel()
        await _stop_stage(
            "memory eviction cancel",
            lambda: asyncio.gather(memory_eviction_task, return_exceptions=True),
        )
    # Cancel the one-shot memory embedding backfill task if still running.
    if memory_backfill_task and not memory_backfill_task.done():
        memory_backfill_task.cancel()
        await _stop_stage(
            "memory backfill cancel",
            lambda: asyncio.gather(memory_backfill_task, return_exceptions=True),
        )
    # Cancel the periodic memory embed-heal task (issue #937).
    if memory_embed_heal_task and not memory_embed_heal_task.done():
        memory_embed_heal_task.cancel()
        await _stop_stage(
            "memory embed-heal cancel",
            lambda: asyncio.gather(memory_embed_heal_task, return_exceptions=True),
        )
    if app.state.file_watcher:
        await _stop_stage("file watcher stop", app.state.file_watcher.stop)
    if app.state.background_processor:
        # The only stop() that takes a timeout: stop() itself aggregates its
        # whole composed drain sequence inside the slice it is given (#854).
        remaining_budget = _remaining()
        if remaining_budget > 0:
            await _stop_stage(
                "background processor stop",
                lambda: app.state.background_processor.stop(
                    timeout=remaining_budget
                ),
                timeout=remaining_budget + _PROCESSOR_STOP_CAP_SECONDS,
            )
        else:
            logger.warning(
                "Shutdown stage 'background processor stop' skipped: aggregate "
                "budget exhausted"
            )
    if getattr(app.state, "wiki_compile_processor", None):
        await _stop_stage(
            "wiki compile processor stop",
            app.state.wiki_compile_processor.stop,
        )
    # Curator lifecycle ownership (#799/#854): set the cooperative flag so an
    # in-flight curate() orchestration aborts at its next call boundary, and
    # shut the offload executor down so QUEUED orchestration never extends
    # interpreter exit. The recreate-on-demand accessor keeps a NEXT lifespan
    # entry in the same interpreter working (wiki-lint convention, PR #1113).
    with contextlib_suppress(Exception):
        from app.services.wiki_compiler import (
            CURATOR_SHUTDOWN,
            _get_curator_offload_executor,
        )

        CURATOR_SHUTDOWN.set()
        _get_curator_offload_executor().shutdown(wait=False, cancel_futures=True)
    if getattr(app.state, "draft_job_processor", None):
        await _stop_stage(
            "draft job processor stop",
            app.state.draft_job_processor.stop,
        )
    if getattr(app.state, "kms_compile_processor", None):
        await _stop_stage(
            "kms compile processor stop",
            app.state.kms_compile_processor.stop,
        )
    # Parse executor lifecycle ownership (#799/#854): cancel queued parses so
    # they never extend interpreter exit; in-flight parses keep running per
    # the documented abandoned-thread contract (issue #703) and are skipped
    # by the bounded tail exit below.
    with contextlib_suppress(Exception):
        from app.services.document_processor import _get_parse_executor

        _get_parse_executor().shutdown(wait=False, cancel_futures=True)
    # E3 admission shutdown (issue #518, swarm review F-006): after all
    # background workers have drained, release their admission slots,
    # reject further admits and close a Redis shared store's connection
    # pool. Must run AFTER the processor stop() calls above — those drains
    # hold background leases until they finish.
    async def _admission_shutdown() -> None:
        from app.services.admission import get_admission_controller

        controller = get_admission_controller()
        await controller.shutdown()
        close = getattr(controller.store, "close", None)
        if close is not None:
            await close()

    await _stop_stage("admission shutdown", _admission_shutdown)
    # Close both underlying LLM clients. The ``llm_client`` attr is an
    # alias of ``thinking_llm_client`` so closing it separately is
    # unnecessary; ``LLMClient.close()`` is also idempotent. Unconfigured
    # boots carry None clients — the getattr guard keeps that inert.
    for _client_attr in ("thinking_llm_client", "instant_llm_client"):
        _client = getattr(app.state, _client_attr, None)
        if _client is not None:
            await _stop_stage(f"{_client_attr} close", _client.close)
    for _service_attr in ("embedding_service", "reranking_service"):
        _service = getattr(app.state, _service_attr, None)
        if _service is not None:
            await _stop_stage(f"{_service_attr} close", _service.close)
    # Synchronous closes run on a worker under the remaining budget (never
    # wait_for(None) — both are plain functions): handle-close and
    # lock-scoped operations, fast by construction per the census.
    await _stop_stage(
        "vector store close", lambda: asyncio.to_thread(app.state.vector_store.close)
    )
    # Review PRR-020: let detached upload commit tasks settle (bounded)
    # before the pool closes, so every commit/compensation decides its
    # outcome instead of racing pool closure.
    async def _upload_commit_drain() -> None:
        from app.api.routes.draft_room import drain_inflight_upload_commits

        await drain_inflight_upload_commits()

    await _stop_stage("upload commit drain", _upload_commit_drain)
    turn_finalizers_drained, pool_closed = await _finalize_turns_and_close_pool(
        app, remaining=_remaining
    )
    try:
        from app.services.auth_service import _auth_executor
        _auth_executor.shutdown(wait=False)
    except Exception:
        pass
    with contextlib_suppress(Exception):
        # PR #1113 (swarm-pr-review N-05a): the wiki lint scan executor is
        # created at routes import time; shut it down with the same
        # convention as the auth/curator executors so a queued scan does
        # not extend interpreter exit.
        from app.api.routes.wiki import _WIKI_LINT_EXECUTOR
        _WIKI_LINT_EXECUTOR.shutdown(wait=False)
    with contextlib_suppress(Exception):
        from app.services import redis_io
        redis_io.shutdown_executor()

    # Bounded tail exit (#799/#854, plan-critic rounds 6-10): when the
    # production launch surface opted in, every recovery-critical workflow
    # has completed (deny-by-default registry empty, turn finalizers
    # drained, pool closed, upload registry empty), and logging is flushed,
    # exit NOW instead of joining whatever non-critical worker threads are
    # alive. The decision lives in _bounded_tail_exit so tests drive the
    # REAL gate (os._exit patched) instead of a re-implementation.
    _bounded_tail_exit(turn_finalizers_drained, pool_closed)



def _log_detached_failure(stage_name: str, finished: asyncio.Future) -> None:
    """Report a detached shutdown stage's late failure.

    Registered only for a stage that was ABANDONED on timeout: its
    exception would otherwise surface as an "exception was never
    retrieved" warning at collection time, with no stage name.
    """
    if finished.cancelled():
        return
    exc = finished.exception()
    if exc is not None:
        logger.warning(
            "Shutdown stage '%s' failed after the budget expired",
            stage_name,
            exc_info=exc,
        )

def _bounded_tail_exit(turn_finalizers_drained: bool, pool_closed: bool) -> bool:
    """Run the bounded tail-exit gate; returns True iff os._exit fired.

    Fires only when RAGAPP_BOUNDED_EXIT=1 AND the deny-by-default critical
    registry is empty AND turn finalizers drained AND the upload-commit
    registry is empty (read at gate time). Unreadable state counts as
    pending (never exit on unknown). os._exit skips executor joins and
    interpreter teardown; there are no app-level atexit registrations.
    Tests/embedded lifespans never set the env (conftest pops it) and take
    the normal path.

    ``pool_closed`` is NOT a firing precondition (plan rev 11): it is a
    RESOURCE-CLEANUP outcome, not a durability condition. The durability
    conditions are the three registries/states above -- the turn
    finalizers (the #1202 contract: a live turn's finalizer must be able to
    finish, which is why the pool is deliberately left open when it did
    not drain), the upload-commit registry, and the deny-by-default
    critical-work registry. Exiting with the pool open is safe: the
    process exit reclaims the connections, SQLite abandons uncommitted
    work, and committed WAL transactions survive a process crash and are
    recovered on reopen. Requiring the close would have made the
    EXHAUSTED-BUDGET path (the #854 stall scenario) fall through to the
    unbounded executor joins -- and Compose's force kill would then end
    the process, which frozen AC12 forbids ("in either case the process
    exits before Compose force-terminates it").

    The pool state is still LOGGED on both the firing and the non-firing
    paths, so a cleanup that did not complete is visible.
    """
    if os.environ.get("RAGAPP_BOUNDED_EXIT") != "1":
        return False
    try:
        from app.api.routes.draft_room import _INFLIGHT_UPLOAD_COMMITS
        from app.services.shutdown_gate import critical_work_active

        uploads_pending = bool(_INFLIGHT_UPLOAD_COMMITS)
    except Exception:  # noqa: BLE001 — unreadable state = do not exit
        uploads_pending = True
    critical_complete = (
        turn_finalizers_drained
        and not uploads_pending
        and not critical_work_active()
    )
    for handler in logging.getLogger().handlers:
        with contextlib_suppress(Exception):
            handler.flush()
    if critical_complete:
        logging.getLogger().info(
            "ragapp: bounded tail exit — critical shutdown complete "
            "(pool_closed=%s); skipping non-critical executor joins",
            pool_closed,
        )
        for handler in logging.getLogger().handlers:
            with contextlib_suppress(Exception):
                handler.flush()
        os._exit(0)
        return True  # pragma: no cover — os._exit does not return
    logging.getLogger().info(
        "ragapp: critical work incomplete at the tail (finalizers=%s "
        "pool_closed=%s uploads_pending=%s) — normal exit; recovery "
        "contracts govern",
        turn_finalizers_drained,
        pool_closed,
        uploads_pending,
    )
    for handler in logging.getLogger().handlers:
        with contextlib_suppress(Exception):
            handler.flush()
    return False


async def _finalize_turns_and_close_pool(app, remaining=None) -> tuple[bool, bool]:
    """Drain durable turn finalizers, then close the pool only when safe.

    Keeping this decision in one runtime-testable function makes an
    unconditional close (or a close that races a live finalizer) observable
    instead of relying on source-order inspection alone. Returns
    ``(turn_finalizers_drained, pool_closed)`` so the bounded tail exit can
    consume the authoritative state (#799/#854) instead of inferring it.

    ``pool_closed`` is True ONLY when ``close_all()`` actually completed:
    the close is a synchronous callable that drains the pool under its lock
    (and shuts the checkout executor), so it can block briefly on an
    in-flight checkout. It runs on a worker under the remaining aggregate
    budget — an unbounded call here would be the one stage that can hold
    the shutdown past the envelope and keep the tail gate unreachable. A
    timed-out close leaves ``pool_closed`` False, so the gate never fires
    on a pool that was not actually closed.
    """
    turn_finalizers_drained = False
    pool_closed = False
    try:
        # Issue #1202: cancellation finalizes run on a worker; drain them before
        # the pool becomes unusable, or an interrupted turn can be lost.
        from app.api.routes.chat import (
            TURN_FINALIZE_DRAIN_TIMEOUT_SECONDS,
            TURN_PRODUCER_SHUTDOWN_TIMEOUT_SECONDS,
            _turn_registry,
            shutdown_turn_finalizers,
        )

        # Cancel live stream producers first so their cancellation backstops
        # submit before the gate closes. Retirement is bounded; the drain runs
        # in a worker so the event loop never blocks on future completion.
        producers = [
            getattr(entry, "task", None)
            for entry in list(_turn_registry().values())
        ]
        turn_finalizers_drained = await shutdown_turn_finalizers(
            producers,
            TURN_PRODUCER_SHUTDOWN_TIMEOUT_SECONDS,
            TURN_FINALIZE_DRAIN_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        logger.warning(
            "Durable turn finalizer shutdown check failed: %s", type(exc).__name__
        )
    if turn_finalizers_drained:
        close_budget = remaining() if remaining is not None else None
        if close_budget is None:
            # No budget supplied (a direct caller with no envelope): the
            # historical synchronous close.
            app.state.db_pool.close_all()
            pool_closed = True
        elif close_budget > 0:
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(app.state.db_pool.close_all),
                    timeout=close_budget,
                )
                pool_closed = True
            except asyncio.TimeoutError:
                # The close did not complete inside the aggregate budget; the
                # pool is NOT marked closed, so the bounded tail exit cannot
                # fire. close_all is idempotent-safe for a later retry (a
                # second close finds an empty pool).
                logger.warning(
                    "Pool close exceeded the aggregate shutdown budget; "
                    "continuing shutdown (pool not marked closed)"
                )
            except Exception:
                logger.warning("Pool close failed during shutdown", exc_info=True)
        else:
            # The aggregate budget is EXHAUSTED: close_all() takes the pool
            # lock and can block, so it must never run synchronously on the
            # event loop here (that would hold the shutdown past the
            # envelope and keep the tail gate unreachable). The pool is
            # left open and NOT marked closed — the gate declines to fire
            # rather than claim a close that did not happen, and the
            # process's own exit reclaims the connections.
            logger.warning(
                "Pool close skipped: the aggregate shutdown budget is "
                "exhausted; the pool is left open for the process exit "
                "(not marked closed)"
            )
    return turn_finalizers_drained, pool_closed
