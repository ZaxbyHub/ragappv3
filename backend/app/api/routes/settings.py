import json
import sqlite3
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, field_validator, model_validator

from app.api.deps import get_csrf_manager, get_current_active_user, get_db, require_role
from app.config import settings
from app.security import CSRFManager, issue_csrf_token
from app.services.ssrf import URLBlocked, assert_url_safe

router = APIRouter()


class SettingsUpdate(BaseModel):
    # Legacy fields (deprecated, still supported for backward compatibility)
    chunk_size: Optional[int] = None
    chunk_overlap: Optional[int] = None
    max_context_chunks: Optional[int] = None
    rag_relevance_threshold: Optional[float] = None
    vector_top_k: Optional[int] = None

    # New character-based fields
    chunk_size_chars: Optional[int] = None
    chunk_overlap_chars: Optional[int] = None
    retrieval_top_k: Optional[int] = None
    vector_metric: Optional[str] = None
    max_distance_threshold: Optional[float] = None
    embedding_doc_prefix: Optional[str] = None
    embedding_query_prefix: Optional[str] = None
    retrieval_window: Optional[int] = None
    embedding_batch_size: Optional[int] = None

    # Reranker config
    reranker_url: Optional[str] = None
    reranker_model: Optional[str] = None
    reranking_enabled: Optional[bool] = None
    reranker_top_n: Optional[int] = None
    initial_retrieval_top_k: Optional[int] = None

    # Hybrid search config
    hybrid_search_enabled: Optional[bool] = None
    hybrid_alpha: Optional[float] = None

    # Ollama and model config
    ollama_embedding_url: Optional[str] = None
    ollama_chat_url: Optional[str] = None
    embedding_model: Optional[str] = None
    chat_model: Optional[str] = None

    # Instant mode (LM Studio on local GPU)
    instant_chat_url: Optional[str] = None
    instant_chat_model: Optional[str] = None
    default_chat_mode: Optional[str] = None
    ingestion_llm_mode: Optional[str] = None

    # Per-mode retrieval overrides
    instant_initial_retrieval_top_k: Optional[int] = None
    instant_reranker_top_n: Optional[int] = None
    instant_memory_context_top_k: Optional[int] = None
    instant_max_tokens: Optional[int] = None

    # Feature flags (still supported)
    auto_scan_enabled: Optional[bool] = None
    auto_scan_interval_minutes: Optional[int] = None
    maintenance_mode: Optional[bool] = None
    enable_model_validation: Optional[bool] = None

    # Wiki / Knowledge Compiler config
    wiki_enabled: Optional[bool] = None
    wiki_compile_on_ingest: Optional[bool] = None
    wiki_compile_on_query: Optional[bool] = None
    wiki_compile_after_indexing: Optional[bool] = None
    wiki_lint_enabled: Optional[bool] = None

    # Optional LLM Wiki Curator config
    wiki_llm_curator_enabled: Optional[bool] = None
    wiki_llm_curator_url: Optional[str] = None
    wiki_llm_curator_model: Optional[str] = None
    wiki_llm_curator_temperature: Optional[float] = None
    wiki_llm_curator_max_input_chars: Optional[int] = None
    wiki_llm_curator_max_output_tokens: Optional[int] = None
    wiki_llm_curator_timeout_sec: Optional[float] = None
    wiki_llm_curator_concurrency: Optional[int] = None
    wiki_llm_curator_mode: Optional[str] = None
    wiki_llm_curator_require_quote_match: Optional[bool] = None
    wiki_llm_curator_require_chunk_id: Optional[bool] = None
    wiki_llm_curator_run_on_ingest: Optional[bool] = None
    wiki_llm_curator_run_on_query: Optional[bool] = None
    wiki_llm_curator_run_on_manual: Optional[bool] = None

    @field_validator("chunk_size")
    @classmethod
    def validate_chunk_size(cls, v):
        if v is not None and v <= 0:
            raise ValueError("chunk_size must be a positive integer")
        return v

    @field_validator("chunk_overlap")
    @classmethod
    def validate_chunk_overlap(cls, v):
        if v is not None and v < 0:
            raise ValueError("chunk_overlap must be a non-negative integer")
        return v

    @field_validator("max_context_chunks")
    @classmethod
    def validate_max_context_chunks(cls, v):
        if v is not None and v <= 0:
            raise ValueError("max_context_chunks must be a positive integer")
        return v

    @field_validator("rag_relevance_threshold")
    @classmethod
    def validate_rag_relevance_threshold(cls, v):
        if v is not None and (v < 0 or v > 1):
            raise ValueError("rag_relevance_threshold must be between 0 and 1")
        return v

    @field_validator("vector_top_k")
    @classmethod
    def validate_vector_top_k(cls, v):
        if v is not None and v <= 0:
            raise ValueError("vector_top_k must be a positive integer")
        return v

    @field_validator("default_chat_mode")
    @classmethod
    def validate_default_chat_mode(cls, v):
        if v is not None and v not in ("instant", "thinking"):
            raise ValueError("default_chat_mode must be 'instant' or 'thinking'")
        return v

    @field_validator("ingestion_llm_mode")
    @classmethod
    def validate_ingestion_llm_mode(cls, v):
        if v is not None and v not in ("instant", "thinking", "disabled"):
            raise ValueError(
                "ingestion_llm_mode must be 'instant', 'thinking', or 'disabled'"
            )
        return v

    @field_validator(
        "instant_initial_retrieval_top_k",
        "instant_reranker_top_n",
        "instant_memory_context_top_k",
        "instant_max_tokens",
    )
    @classmethod
    def validate_instant_positive_ints(cls, v):
        if v is not None and v <= 0:
            raise ValueError("must be a positive integer")
        return v

    @field_validator("chunk_size_chars")
    @classmethod
    def validate_chunk_size_chars(cls, v):
        if v is not None and v <= 0:
            raise ValueError("chunk_size_chars must be a positive integer")
        return v

    @field_validator("chunk_overlap_chars")
    @classmethod
    def validate_chunk_overlap_chars(cls, v):
        if v is not None and v < 0:
            raise ValueError("chunk_overlap_chars must be a non-negative integer")
        return v

    @field_validator("retrieval_top_k")
    @classmethod
    def validate_retrieval_top_k(cls, v):
        if v is not None and v <= 0:
            raise ValueError("retrieval_top_k must be a positive integer")
        return v

    @field_validator("max_distance_threshold")
    @classmethod
    def validate_max_distance_threshold(cls, v):
        if v is not None and v < 0:
            raise ValueError("max_distance_threshold must be a non-negative number")
        return v

    @field_validator("retrieval_window")
    @classmethod
    def validate_retrieval_window(cls, v):
        if v is not None and v <= 0:
            raise ValueError("retrieval_window must be a positive integer")
        return v

    @field_validator("auto_scan_interval_minutes")
    @classmethod
    def validate_auto_scan_interval_minutes(cls, v):
        if v is not None and v <= 0:
            raise ValueError("auto_scan_interval_minutes must be a positive integer")
        return v

    @field_validator("embedding_batch_size")
    @classmethod
    def validate_embedding_batch_size(cls, v):
        if v is not None and (v < 1 or v > 128):
            raise ValueError("embedding_batch_size must be between 1 and 128 (TEI limit)")
        return v

    @field_validator("reranker_top_n")
    @classmethod
    def validate_reranker_top_n(cls, v):
        if v is not None and v <= 0:
            raise ValueError("reranker_top_n must be a positive integer")
        return v

    @field_validator("initial_retrieval_top_k")
    @classmethod
    def validate_initial_retrieval_top_k(cls, v):
        if v is not None and v <= 0:
            raise ValueError("initial_retrieval_top_k must be a positive integer")
        return v

    @field_validator("hybrid_alpha")
    @classmethod
    def validate_hybrid_alpha(cls, v):
        if v is not None and (v < 0 or v > 1):
            raise ValueError("hybrid_alpha must be between 0 and 1")
        return v

    @field_validator("ollama_embedding_url", "ollama_chat_url", "instant_chat_url", mode="before")
    @classmethod
    def validate_ollama_url(cls, v):
        if v is None:
            return v
        if not isinstance(v, str) or len(v) > 2048:
            raise ValueError("URL must be a string up to 2048 characters")
        v = v.strip()
        if v and not v.startswith(("http://", "https://")):
            raise ValueError("URL must start with http:// or https://")
        if "@" in v:
            raise ValueError("URL must not contain credentials (@)")
        return v

    @field_validator("embedding_model", "chat_model", "instant_chat_model", mode="before")
    @classmethod
    def validate_model_name(cls, v):
        if v is None:
            return v
        if not isinstance(v, str) or len(v) > 256:
            raise ValueError("Model name must be a string up to 256 characters")
        v = v.strip()
        if not v:
            raise ValueError("Model name must not be empty")
        # Check for control characters (ASCII 0-31 and 127)
        if any(ord(c) < 32 or ord(c) == 127 for c in v):
            raise ValueError("Model name must not contain control characters")
        return v

    @model_validator(mode="after")
    def validate_chunk_overlap_less_than_size(self):
        chunk_overlap = self.chunk_overlap
        chunk_size = self.chunk_size
        if (
            chunk_overlap is not None
            and chunk_size is not None
            and chunk_overlap >= chunk_size
        ):
            raise ValueError("chunk_overlap must be less than chunk_size")
        return self

    # ── Curator validators ────────────────────────────────────────────────
    @field_validator("wiki_llm_curator_temperature")
    @classmethod
    def validate_curator_temperature(cls, v):
        if v is not None and not (0.0 <= float(v) <= 1.0):
            raise ValueError("wiki_llm_curator_temperature must be between 0.0 and 1.0")
        return v

    @field_validator("wiki_llm_curator_max_input_chars")
    @classmethod
    def validate_curator_max_input_chars(cls, v):
        if v is not None and not (1000 <= int(v) <= 24000):
            raise ValueError(
                "wiki_llm_curator_max_input_chars must be between 1000 and 24000"
            )
        return v

    @field_validator("wiki_llm_curator_max_output_tokens")
    @classmethod
    def validate_curator_max_output_tokens(cls, v):
        if v is not None and not (1 <= int(v) <= 16384):
            raise ValueError(
                "wiki_llm_curator_max_output_tokens must be between 1 and 16384"
            )
        return v

    @field_validator("wiki_llm_curator_timeout_sec")
    @classmethod
    def validate_curator_timeout_sec(cls, v):
        if v is not None and not (10 <= float(v) <= 600):
            raise ValueError(
                "wiki_llm_curator_timeout_sec must be between 10 and 600"
            )
        return v

    @field_validator("wiki_llm_curator_concurrency")
    @classmethod
    def validate_curator_concurrency(cls, v):
        if v is not None and not (1 <= int(v) <= 4):
            raise ValueError("wiki_llm_curator_concurrency must be between 1 and 4")
        return v

    @field_validator("wiki_llm_curator_mode")
    @classmethod
    def validate_curator_mode(cls, v):
        if v is not None and v not in ("draft", "active_if_verified"):
            raise ValueError(
                "wiki_llm_curator_mode must be 'draft' or 'active_if_verified'"
            )
        return v

    @field_validator("wiki_llm_curator_url", mode="before")
    @classmethod
    def validate_curator_url(cls, v):
        if v is None:
            return v
        if not isinstance(v, str) or len(v) > 2048:
            raise ValueError("wiki_llm_curator_url must be a string up to 2048 chars")
        v = v.strip()
        if v and not v.startswith(("http://", "https://")):
            raise ValueError("wiki_llm_curator_url must start with http:// or https://")
        if "@" in v:
            raise ValueError("wiki_llm_curator_url must not contain credentials (@)")
        return v

    @field_validator("wiki_llm_curator_model", mode="before")
    @classmethod
    def validate_curator_model(cls, v):
        if v is None:
            return v
        if not isinstance(v, str) or len(v) > 256:
            raise ValueError("wiki_llm_curator_model must be a string up to 256 chars")
        v = v.strip()
        if any(ord(c) < 32 or ord(c) == 127 for c in v):
            raise ValueError("wiki_llm_curator_model must not contain control characters")
        return v


ALLOWED_FIELDS = [
    "chunk_size",
    "chunk_overlap",
    "max_context_chunks",
    "auto_scan_enabled",
    "auto_scan_interval_minutes",
    "rag_relevance_threshold",
    "vector_top_k",
    "chunk_size_chars",
    "chunk_overlap_chars",
    "retrieval_top_k",
    "vector_metric",
    "max_distance_threshold",
    "embedding_doc_prefix",
    "embedding_query_prefix",
    "retrieval_window",
    "embedding_batch_size",
    "reranker_url",
    "reranker_model",
    "reranking_enabled",
    "reranker_top_n",
    "initial_retrieval_top_k",
    "hybrid_search_enabled",
    "hybrid_alpha",
    "ollama_embedding_url",
    "ollama_chat_url",
    "embedding_model",
    "chat_model",
    # Instant mode (LM Studio)
    "instant_chat_url",
    "instant_chat_model",
    "default_chat_mode",
    "ingestion_llm_mode",
    "instant_initial_retrieval_top_k",
    "instant_reranker_top_n",
    "instant_memory_context_top_k",
    "instant_max_tokens",
    # Wiki / Knowledge Compiler
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
]


# Curator fields that must be non-empty when wiki_llm_curator_enabled is true.
# Enforced at PUT-time so the backend never silently accepts a half-configured
# curator (which would then surface as a runtime error during compile).
_CURATOR_REQUIRED_WHEN_ENABLED = ("wiki_llm_curator_url", "wiki_llm_curator_model")


def _persist_settings(conn: sqlite3.Connection, update: SettingsUpdate) -> None:
    """Save changed settings to the settings_kv table."""
    for field in ALLOWED_FIELDS:
        value = getattr(update, field)
        if value is not None:
            conn.execute(
                "INSERT OR REPLACE INTO settings_kv (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
                (field, json.dumps(value)),
            )
    conn.commit()


def _enforce_curator_required_when_enabled(update: SettingsUpdate) -> None:
    """Reject PUT bodies that enable the curator without URL+model.

    Reads the *effective post-update state*: if the body sets
    ``wiki_llm_curator_enabled=True`` but does not also supply (or already
    has) a non-empty url + model, raise 422. This prevents silent half-
    configured curators that fail at runtime instead of at save time.
    """
    # Effective enabled = body value if provided, else current setting.
    if update.wiki_llm_curator_enabled is not None:
        will_be_enabled = bool(update.wiki_llm_curator_enabled)
    else:
        will_be_enabled = bool(getattr(settings, "wiki_llm_curator_enabled", False))
    if not will_be_enabled:
        return

    missing: list[str] = []
    for field in _CURATOR_REQUIRED_WHEN_ENABLED:
        # Effective post-update value: body wins, else current setting.
        body_val = getattr(update, field)
        if body_val is not None:
            effective = body_val
        else:
            effective = getattr(settings, field, "")
        if not effective or (isinstance(effective, str) and not effective.strip()):
            missing.append(field)
    if missing:
        raise HTTPException(
            status_code=422,
            detail=(
                "Curator is enabled but required fields are missing: "
                f"{', '.join(missing)}. Provide URL and model, or disable the curator."
            ),
        )


class SettingsResponse(BaseModel):
    """Public settings response - excludes secrets."""

    # Server config (safe to expose)
    port: int
    data_dir: str

    # Ollama config
    ollama_embedding_url: str
    ollama_chat_url: str

    # Model config
    embedding_model: str
    chat_model: str

    # Instant mode (LM Studio)
    instant_chat_url: str = "http://host.docker.internal:1234"
    instant_chat_model: str = "nvidia/nemotron-3-nano-4b"
    default_chat_mode: str = "thinking"
    ingestion_llm_mode: str = "instant"
    instant_initial_retrieval_top_k: int = 10
    instant_reranker_top_n: int = 4
    instant_memory_context_top_k: int = 2
    instant_max_tokens: int = 4096

    # Document processing (user-configurable)
    chunk_size: Optional[int] = None  # Legacy, deprecated
    chunk_overlap: Optional[int] = None  # Legacy, deprecated
    max_context_chunks: int

    # RAG config (user-configurable)
    rag_relevance_threshold: Optional[float] = None  # Legacy, deprecated
    vector_top_k: Optional[int] = None  # Legacy, deprecated

    # New character-based fields
    chunk_size_chars: int
    chunk_overlap_chars: int
    retrieval_top_k: int
    vector_metric: str
    max_distance_threshold: Optional[float] = None
    embedding_doc_prefix: str
    embedding_query_prefix: str
    retrieval_window: int

    # Feature flags
    maintenance_mode: bool
    auto_scan_enabled: bool
    auto_scan_interval_minutes: int
    enable_model_validation: bool

    # Embedding config
    embedding_batch_size: int

    # Reranker config
    reranker_url: str = ""
    reranker_model: str = "BAAI/bge-reranker-v2-m3"
    reranking_enabled: bool = False
    reranker_top_n: int = 5
    initial_retrieval_top_k: int = 20

    # Hybrid search config
    hybrid_search_enabled: bool = True
    hybrid_alpha: float = 0.5

    # Wiki / Knowledge Compiler config
    wiki_enabled: bool = True
    wiki_compile_on_ingest: bool = True
    wiki_compile_on_query: bool = True
    wiki_compile_after_indexing: bool = True
    wiki_lint_enabled: bool = True

    # Optional LLM Wiki Curator config
    wiki_llm_curator_enabled: bool = False
    wiki_llm_curator_url: str = ""
    wiki_llm_curator_model: str = ""
    wiki_llm_curator_temperature: float = 0.0
    wiki_llm_curator_max_input_chars: int = 6000
    wiki_llm_curator_max_output_tokens: int = 2048
    wiki_llm_curator_timeout_sec: float = 120.0
    wiki_llm_curator_concurrency: int = 1
    wiki_llm_curator_mode: str = "draft"
    wiki_llm_curator_require_quote_match: bool = True
    wiki_llm_curator_require_chunk_id: bool = True
    wiki_llm_curator_run_on_ingest: bool = True
    wiki_llm_curator_run_on_query: bool = False
    wiki_llm_curator_run_on_manual: bool = True

    # Limits (safe to expose)
    max_file_size_mb: int
    allowed_extensions: list[str]

    # CORS (safe to expose)
    backend_cors_origins: list[str]

    # Source map: per field, where the effective runtime value came from.
    # Values: "kv" (settings_kv override), "env" (matches an env variable),
    # "default" (Pydantic default). The Models tab uses this to label each
    # field instead of disabling inputs based on env presence (kv > env at
    # runtime in the actual lifespan order, so disabling on env presence
    # would lie). May be omitted on legacy clients that do not expect it.
    effective_sources: dict[str, str] = {}

    @field_validator("data_dir", mode="before")
    @classmethod
    def convert_path_to_str(cls, v):
        return str(v)


def _build_settings_dict() -> dict:
    """Build the public settings dict from the live ``settings`` singleton.

    Used by both GET /settings and the post-update response so the wire
    shape is identical regardless of code path.
    """
    base = {
        "port": settings.port,
        "data_dir": str(settings.data_dir),
        "ollama_embedding_url": settings.ollama_embedding_url,
        "ollama_chat_url": settings.ollama_chat_url,
        "embedding_model": settings.embedding_model,
        "chat_model": settings.chat_model,
        # Instant mode (LM Studio)
        "instant_chat_url": settings.instant_chat_url,
        "instant_chat_model": settings.instant_chat_model,
        "default_chat_mode": settings.default_chat_mode,
        "ingestion_llm_mode": settings.ingestion_llm_mode,
        "instant_initial_retrieval_top_k": settings.instant_initial_retrieval_top_k,
        "instant_reranker_top_n": settings.instant_reranker_top_n,
        "instant_memory_context_top_k": settings.instant_memory_context_top_k,
        "instant_max_tokens": settings.instant_max_tokens,
        "chunk_size": settings.chunk_size,
        "chunk_overlap": settings.chunk_overlap,
        "max_context_chunks": settings.max_context_chunks,
        "rag_relevance_threshold": settings.rag_relevance_threshold,
        "vector_top_k": settings.vector_top_k,
        "chunk_size_chars": settings.chunk_size_chars,
        "chunk_overlap_chars": settings.chunk_overlap_chars,
        "retrieval_top_k": settings.retrieval_top_k,
        "vector_metric": settings.vector_metric,
        "max_distance_threshold": settings.max_distance_threshold,
        "embedding_doc_prefix": settings.embedding_doc_prefix,
        "embedding_query_prefix": settings.embedding_query_prefix,
        "retrieval_window": settings.retrieval_window,
        "embedding_batch_size": settings.embedding_batch_size,
        "maintenance_mode": settings.maintenance_mode,
        "auto_scan_enabled": settings.auto_scan_enabled,
        "auto_scan_interval_minutes": settings.auto_scan_interval_minutes,
        "enable_model_validation": settings.enable_model_validation,
        "max_file_size_mb": settings.max_file_size_mb,
        "allowed_extensions": settings.allowed_extensions,
        "backend_cors_origins": settings.backend_cors_origins,
        "reranker_url": settings.reranker_url,
        "reranker_model": settings.reranker_model,
        "reranking_enabled": settings.reranking_enabled,
        "reranker_top_n": settings.reranker_top_n,
        "initial_retrieval_top_k": settings.initial_retrieval_top_k,
        "hybrid_search_enabled": settings.hybrid_search_enabled,
        "hybrid_alpha": settings.hybrid_alpha,
        # Wiki / Knowledge Compiler
        "wiki_enabled": settings.wiki_enabled,
        "wiki_compile_on_ingest": settings.wiki_compile_on_ingest,
        "wiki_compile_on_query": settings.wiki_compile_on_query,
        "wiki_compile_after_indexing": settings.wiki_compile_after_indexing,
        "wiki_lint_enabled": settings.wiki_lint_enabled,
        # Optional LLM Wiki Curator
        "wiki_llm_curator_enabled": settings.wiki_llm_curator_enabled,
        "wiki_llm_curator_url": settings.wiki_llm_curator_url,
        "wiki_llm_curator_model": settings.wiki_llm_curator_model,
        "wiki_llm_curator_temperature": settings.wiki_llm_curator_temperature,
        "wiki_llm_curator_max_input_chars": settings.wiki_llm_curator_max_input_chars,
        "wiki_llm_curator_max_output_tokens": settings.wiki_llm_curator_max_output_tokens,
        "wiki_llm_curator_timeout_sec": settings.wiki_llm_curator_timeout_sec,
        "wiki_llm_curator_concurrency": settings.wiki_llm_curator_concurrency,
        "wiki_llm_curator_mode": settings.wiki_llm_curator_mode,
        "wiki_llm_curator_require_quote_match": settings.wiki_llm_curator_require_quote_match,
        "wiki_llm_curator_require_chunk_id": settings.wiki_llm_curator_require_chunk_id,
        "wiki_llm_curator_run_on_ingest": settings.wiki_llm_curator_run_on_ingest,
        "wiki_llm_curator_run_on_query": settings.wiki_llm_curator_run_on_query,
        "wiki_llm_curator_run_on_manual": settings.wiki_llm_curator_run_on_manual,
    }
    # NOTE: callers MUST set base["effective_sources"] explicitly with a
    # real DB connection. _compute_effective_sources(None) would silently
    # return an all-"env"/"default" map (no kv visibility), which would
    # mislead the Models tab badges. Forcing the assignment at the call
    # site keeps the contract honest.
    return base


def _compute_effective_sources(conn: Optional[sqlite3.Connection]) -> dict[str, str]:
    """Map each ALLOWED_FIELD to the source of its current effective value.

    Precedence reflects actual runtime behaviour from
    ``backend/app/lifespan.py``: settings_kv is loaded at startup and
    ``setattr(settings, key, value)`` overwrites env-derived values, so
    persistence wins after the first save. Values:
      - "kv":      a row exists in ``settings_kv`` for this field.
      - "env":     no kv row, and an env variable with the same name
                   (uppercased) is set.
      - "default": neither.

    The Models tab uses this to label inputs honestly without disabling
    them on env presence.
    """
    kv_keys: set[str] = set()
    if conn is not None:
        try:
            cursor = conn.execute("SELECT key FROM settings_kv")
            kv_keys = {row[0] for row in cursor.fetchall()}
        except sqlite3.Error:
            kv_keys = set()
    out: dict[str, str] = {}
    import os as _os

    for field in ALLOWED_FIELDS:
        if field in kv_keys:
            out[field] = "kv"
        else:
            # Treat ``X=""`` as "not set". Pydantic typically falls back
            # to its default for empty strings, so labelling the source
            # as "env" would mislead the Models tab badges.
            env_val = _os.environ.get(field.upper(), "")
            if env_val != "":
                out[field] = "env"
            else:
                out[field] = "default"
    return out


def _hot_rebind_llm_clients(app, update: SettingsUpdate) -> None:
    """Apply live URL/model updates to the running LLMClient instances.

    The ``settings`` singleton has already been mutated by
    ``_apply_settings_update`` before this is called, so we read the new
    values from settings directly. We do NOT recreate the httpx pools —
    ``LLMClient.reconfigure`` mutates ``base_url``/``model`` in place,
    preserving every external reference (LLMHealthChecker,
    background_processor, keepalive task, RAGEngine).
    """
    thinking_client = getattr(app.state, "thinking_llm_client", None)
    instant_client = getattr(app.state, "instant_llm_client", None)
    if thinking_client is not None and (
        update.ollama_chat_url is not None or update.chat_model is not None
    ):
        thinking_client.reconfigure(
            base_url=settings.ollama_chat_url,
            model=settings.chat_model,
        )
    if instant_client is not None and (
        update.instant_chat_url is not None or update.instant_chat_model is not None
    ):
        instant_client.reconfigure(
            base_url=settings.instant_chat_url,
            model=settings.instant_chat_model,
        )
    if update.ingestion_llm_mode is not None:
        background_processor = getattr(app.state, "background_processor", None)
        if background_processor is not None:
            if settings.ingestion_llm_mode == "instant":
                ingestion_client = instant_client
            elif settings.ingestion_llm_mode == "thinking":
                ingestion_client = thinking_client
            else:
                ingestion_client = None
            background_processor.set_llm_client(ingestion_client)


def _apply_settings_update(update: SettingsUpdate) -> SettingsResponse:
    """Shared logic to apply settings update and return updated settings."""
    updated = False
    for field in ALLOWED_FIELDS:
        value = getattr(update, field)
        if value is not None:
            setattr(settings, field, value)
            updated = True
    if not updated:
        raise HTTPException(
            status_code=400, detail="No valid fields provided for update"
        )
    return SettingsResponse.model_validate(_build_settings_dict())


@router.get("/settings", response_model=SettingsResponse)
def get_settings(
    user: dict = Depends(get_current_active_user),
    conn: sqlite3.Connection = Depends(get_db),
):
    """Return current public settings dict (including embedding_batch_size)."""
    settings_dict = _build_settings_dict()
    settings_dict["effective_sources"] = _compute_effective_sources(conn)
    return SettingsResponse.model_validate(settings_dict)


@router.post("/settings")
def post_settings(
    update: SettingsUpdate,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    _role: dict = Depends(require_role("admin")),
):
    """Apply settings update and persist to database."""
    _enforce_curator_required_when_enabled(update)
    result = _apply_settings_update(update)
    _persist_settings(conn, update)
    _hot_rebind_llm_clients(request.app, update)
    # Re-derive effective_sources now that we've persisted.
    result = SettingsResponse.model_validate(
        {**_build_settings_dict(), "effective_sources": _compute_effective_sources(conn)}
    )
    return result


@router.put("/settings")
def put_settings(
    update: SettingsUpdate,
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
    _role: dict = Depends(require_role("admin")),
):
    """Update settings (upserts into settings_kv)."""
    _enforce_curator_required_when_enabled(update)
    result = _apply_settings_update(update)
    _persist_settings(conn, update)
    _hot_rebind_llm_clients(request.app, update)
    result = SettingsResponse.model_validate(
        {**_build_settings_dict(), "effective_sources": _compute_effective_sources(conn)}
    )
    return result


@router.get("/csrf-token")
def get_csrf_token(
    response: Response,
    csrf_manager: CSRFManager = Depends(get_csrf_manager),
):
    token = issue_csrf_token(response, csrf_manager)
    return {"csrf_token": token}


@router.get("/settings/connection")
async def test_connection(user: dict = Depends(get_current_active_user)):
    """Test connectivity to Ollama endpoints and reranker."""
    targets = {
        "embeddings": settings.ollama_embedding_url,
        "chat": settings.ollama_chat_url,
    }
    if settings.reranker_url:
        targets["reranker"] = settings.reranker_url

    async with httpx.AsyncClient(timeout=5.0, follow_redirects=False) as client:
        results = {}
        for name, url in targets.items():
            try:
                try:
                    assert_url_safe(url)
                except URLBlocked as exc:
                    results[name] = {
                        "url": url,
                        "status": None,
                        "ok": False,
                        "error": f"SSRF blocked: {exc}",
                    }
                    continue
                response = await client.get(url)
                results[name] = {
                    "url": url,
                    "status": response.status_code,
                    "ok": response.status_code < 300,
                }
            except Exception as exc:
                results[name] = {
                    "url": url,
                    "status": None,
                    "ok": False,
                    "error": str(exc),
                }

        # Only add local mode result if reranker wasn't tested (i.e., reranker_url was not set)
        if "reranker" not in results:
            results["reranker"] = {
                "url": "local (sentence-transformers)",
                "ok": True,
                "status": "local",
                "model": settings.reranker_model,
            }
    return results


class _CuratorTestBody(BaseModel):
    """Optional inline override for the curator test endpoint.

    When the body is empty, the test uses the persisted curator settings
    (typical case after Save). The frontend sends an inline override so
    the operator can validate a URL/model BEFORE saving. Validation rules
    mirror SettingsUpdate.validate_curator_url / validate_curator_model.
    """

    url: Optional[str] = None
    model: Optional[str] = None


@router.post("/settings/curator/test")
async def test_curator_connection(
    body: Optional[_CuratorTestBody] = None,
    _role: dict = Depends(require_role("admin")),
):
    """Validate the curator endpoint by issuing a tiny JSON-only ping.

    SSRF-guarded: rejects RFC1918 / loopback / link-local destinations
    unless ``ALLOW_LOCAL_CURATOR=1`` is set. The ping uses
    ``max_tokens=16`` and ``temperature=0`` so it is cheap even when
    pointed at a real model. We never follow redirects.

    Returns ``{ok, model, latency_ms, error?}`` shape — never throws to
    the client; errors are surfaced in the body so the UI can render
    them inline.
    """
    from app.services.curator_ssrf import CuratorURLBlocked, assert_curator_url_safe

    url = (body.url if body and body.url is not None else settings.wiki_llm_curator_url) or ""
    model = (
        body.model if body and body.model is not None else settings.wiki_llm_curator_model
    ) or ""

    if not url.strip() or not model.strip():
        return {
            "ok": False,
            "model": model,
            "latency_ms": None,
            "error": "Curator URL and model are required.",
        }

    # SSRF guard.
    try:
        assert_curator_url_safe(url)
    except CuratorURLBlocked as e:
        return {
            "ok": False,
            "model": model,
            "latency_ms": None,
            "error": str(e),
        }

    import time as _time

    base = url.rstrip("/")
    endpoint = base if base.endswith("/v1/chat/completions") else (
        base + "/v1/chat/completions"
    )
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a JSON-only echo. Reply with the literal JSON "
                    'object {"ok": true} and nothing else.'
                ),
            },
            {"role": "user", "content": '{"ping": true}'},
        ],
        "temperature": 0.0,
        "max_tokens": 16,
        "stream": False,
    }
    timeout = float(settings.wiki_llm_curator_timeout_sec or 10.0)
    timeout = min(max(timeout, 1.0), 30.0)  # cap test calls at 30s

    started = _time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            resp = await client.post(endpoint, json=payload)
        latency_ms = int((_time.monotonic() - started) * 1000)
        if resp.status_code >= 300:
            return {
                "ok": False,
                "model": model,
                "latency_ms": latency_ms,
                "error": f"Curator returned HTTP {resp.status_code}: {resp.text[:200]}",
            }
        # Parsing the response is best-effort — if the model returns plain
        # text instead of JSON, the test still passes (the curator client
        # in PR C does robust JSON extraction during real runs).
        return {
            "ok": True,
            "model": model,
            "latency_ms": latency_ms,
            "error": None,
        }
    except httpx.TimeoutException:
        latency_ms = int((_time.monotonic() - started) * 1000)
        return {
            "ok": False,
            "model": model,
            "latency_ms": latency_ms,
            "error": f"Curator endpoint timed out after {timeout}s.",
        }
    except Exception as e:  # broad: surface any transport error to the UI
        latency_ms = int((_time.monotonic() - started) * 1000)
        return {
            "ok": False,
            "model": model,
            "latency_ms": latency_ms,
            "error": str(e)[:300],
        }
