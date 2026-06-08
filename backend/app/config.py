"""
Application configuration using Pydantic Settings.
"""

import logging
import warnings
from pathlib import Path
from typing import Annotated, Optional

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from app.utils.paths import normalize_root_path

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    """Application settings with environment variable support."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Server configuration
    port: int = 9090
    app_root_path: str = ""
    """External app root path when deployed behind a prefix-stripping proxy, e.g. /knowledgevault."""

    # Base data directory - use relative path for cross-platform compatibility
    data_dir: Path = Path("./data")

    # Ollama configuration
    ollama_embedding_url: str = "http://harrier-embed:8080/v1/embeddings"
    ollama_chat_url: str = "http://host.docker.internal:11434"

    # Model configuration
    embedding_model: str = "microsoft/harrier-oss-v1-0.6b"
    chat_model: str = "gemma-4-26b-a4b-it-apex"

    # LLM HTTP client pool configuration
    llm_max_connections: int = 100
    """Maximum HTTP connections in the LLM client pool (httpx.AsyncClient)."""
    llm_max_keepalive_connections: int = 50
    """Maximum keep-alive connections in the LLM client pool."""

    # Instant mode (LM Studio on local GPU)
    instant_chat_url: str = "http://host.docker.internal:1234"
    instant_chat_model: str = "nvidia/nemotron-3-nano-4b"
    default_chat_mode: str = "thinking"  # "instant" | "thinking"

    # Per-mode retrieval overrides (Instant uses smaller budget)
    instant_initial_retrieval_top_k: int = 10
    instant_reranker_top_n: int = 4
    instant_memory_context_top_k: int = 2
    instant_max_tokens: int = 4096

    # Library vault mapping for file watcher
    library_vault_id: Optional[int] = None

    # Embedding dimension (auto-detected from model, but can be overridden)
    embedding_dim: int = 1024

    # Document processing configuration (character-based - NEW)
    chunk_size_chars: int | None = None
    """Character-based chunk size for document processing. Default 1200 chars (~300 tokens) leaves room for instruction prefix."""
    chunk_overlap_chars: int | None = None
    """Character-based overlap between chunks. Default 120 chars (~30 tokens)."""
    document_parsing_strategy: str = "auto"
    """Document parsing strategy for unstructured.io: 'fast' (fastest), 'hi_res' (best quality), 'auto' (automatic selection)."""
    document_parse_timeout: float = 300.0
    """Timeout in seconds for document parsing. Prevents worker threads from being blocked indefinitely by complex documents."""
    retrieval_top_k: int | None = None
    """Number of top chunks to retrieve (unifies max_context_chunks and vector_top_k)."""
    vector_metric: str = "cosine"
    """Distance metric for vector similarity search."""
    max_distance_threshold: float = 0.5
    """Maximum distance threshold for relevance filtering (replaces rag_relevance_threshold).

    For cosine distance: 0=identical, 1=orthogonal, 2=opposite.
    1.0 allows moderately similar results through. Lower values (e.g. 0.5) are
    more precise but risk filtering out all results for shorter or ambiguous queries.
    Can be overridden via MAX_DISTANCE_THRESHOLD env var.
    """
    embedding_doc_prefix: str = ""
    """Prefix to prepend to documents during embedding."""
    embedding_query_prefix: str = ""
    """Prefix to prepend to queries during embedding."""
    retrieval_window: int = 1
    """Window size for retrieval context expansion."""
    embedding_batch_size: int = 64
    """Number of texts to send per embedding API request. Capped at 128 for TEI compatibility."""
    embedding_batch_max_retries: int = 3
    """Maximum number of retries for adaptive batching when token overflow occurs."""
    embedding_batch_min_sub_size: int = 1
    """Minimum sub-batch size for adaptive batching fallback."""

    # ── Ingestion performance configuration ──────────────────────────────────
    ingestion_worker_count: int = 2
    """Number of concurrent document ingestion workers (1-16)."""
    optimize_mode: str = "periodic"
    """LanceDB table compaction mode: 'after_every_write' (current), 'periodic' (every N chunks), 'manual' (never during ingestion)."""
    optimize_interval_chunks: int = 5000
    """Number of chunks between optimize() calls when optimize_mode is 'periodic'."""
    embedding_concurrent_batches: int = 4
    """Maximum number of embedding batches to process concurrently (1-16). Set to 1 for sequential behavior."""
    embedding_global_concurrent_batches: int = 4
    """Global cap for concurrent embedding batch API calls across all simultaneous documents (1-16)."""
    optimize_on_shutdown: bool = True
    """When True, BackgroundProcessor.stop() calls VectorStore.flush_optimize() during graceful shutdown."""
    ingestion_llm_mode: str = "instant"
    """LLM client used for optional ingestion-time LLM work: 'instant', 'thinking', or 'disabled'."""
    vector_search_concurrency: int = 16
    """Maximum concurrent LanceDB search operations (1-64). Semaphore size for parallel vector searches."""
    write_lock_timeout_seconds: float = 30.0
    """Timeout in seconds for write lock acquisitions. Prevents indefinite deadlock if a write operation hangs."""

    # ── Embedding model validation configuration ───────────────────────────────────
    strict_embedding_model_check: bool = True
    """Enable strict validation that the live TEI model matches EMBEDDING_MODEL at startup."""

    # ── Reranker configuration ────────────────────────────────────────────────
    reranker_url: str = ""
    """TEI-compatible reranker endpoint URL. Empty = use sentence-transformers locally."""
    reranker_model: str = "BAAI/bge-reranker-v2-m3"
    """HuggingFace model ID for local reranking, or model name sent to TEI endpoint."""
    reranking_enabled: bool = True
    """Enable cross-encoder reranking after vector retrieval."""
    reranker_top_n: int = 7
    """Number of chunks to keep after reranking."""
    initial_retrieval_top_k: int = 20
    """Chunks fetched from vector store BEFORE reranking."""
    reranker_timeout_seconds: float = 12.0
    """HTTP timeout (seconds) for the TEI reranker endpoint. On timeout or failure the
    query degrades to un-reranked vector order, so keep this comfortably above the
    reranker's p99 latency to avoid quality regressions and circuit-breaker trips
    (reranking_cb fail_max=3 → a 30s outage for all callers). Lower it only to bound
    worst-case pre-generation latency. Read once when the HTTP client is first created."""

    # ── Hybrid search configuration ─────────────────────────────────────────
    hybrid_search_enabled: bool = True
    """Combine BM25 keyword search with dense vector search using RRF fusion."""
    hybrid_alpha: float = 0.6
    """Weight for dense vs BM25 scores in RRF. 0.0 = pure BM25, 1.0 = pure dense."""

    # ── RRF fusion tuning ───────────────────────────────────────────────────
    hybrid_rrf_k: int = 60
    """RRF k parameter for per-arm dense/FTS fusion within a single scale. Lower = sharper top-list preference."""
    multi_query_rrf_k: int = 60
    """RRF k parameter for cross-variant fusion (original + stepback + HyDE). Lower = sharper top-list preference. Operators may lower to 20 for recall-heavy workloads."""
    multi_scale_rrf_k: int = 60
    """RRF k parameter for multi-scale fusion across chunk sizes."""
    memory_rrf_k: int = 60
    """RRF k parameter for memory hybrid retrieval (FTS + dense fusion)."""
    memory_retrieval_enabled: bool = True
    """Enable memory retrieval as part of RAG queries. When False, the
    chat pipeline skips the memory_store search step entirely."""
    memory_retrieval_top_k: int = 5
    """Maximum memories returned by hybrid memory search per query."""
    memory_relevance_filter_enabled: bool = True
    """When True, apply similarity thresholds to filter out weakly related memories
    before injecting them into the prompt. Prevents unrelated memories from polluting
    document-grounded answers."""
    memory_dense_min_similarity: float = 0.30
    """Minimum cosine similarity for a memory to pass dense-path filtering.
    Memories with sim <= 0.0 are always excluded. Raise this to tighten relevance."""
    memory_rrf_min_score: float = 0.005
    """Minimum RRF fused score for a memory to pass RRF-path filtering."""
    memory_context_top_k: int = 3
    """Maximum memories actually injected into the prompt after relevance filtering.
    Applied after threshold filtering; further limits prompt context pollution."""
    memory_dense_max_candidates: int = 1000
    """Maximum memories scanned by the dense (cosine) memory search on the full,
    unfiltered path, ordered by recency before similarity ranking. Dense is now the
    sole path to purely-semantic recall (it is no longer pre-filtered to FTS hits), so
    this bounds the Python-side cosine work while giving semantic recall a wide pool.
    For vaults larger than this, the oldest memories fall outside the dense scan; raise
    it (at O(n) CPU cost) if a vault needs deeper recency reach."""
    rag_trace_in_response: bool = False
    """When True, RAG queries emit a ``trace`` field in the streaming
    done event with detailed retrieval/generation observability. Default
    False keeps the trace out of normal user-visible metadata; flip on
    for eval runs or when the existing debug panel is active."""
    rrf_weight_original: float = 1.0
    """Weight for original query arm in cross-variant RRF fusion. Applied directly (not normalized). Defaults sum to 2.0."""
    rrf_weight_stepback: float = 0.5
    """Weight for step-back variant arm in cross-variant RRF fusion. Applied directly (not normalized). Set to 0.0 to exclude."""
    rrf_weight_hyde: float = 0.5
    """Weight for HyDE variant arm in cross-variant RRF fusion. Applied directly (not normalized). Set to 0.0 to exclude."""
    exact_match_promote: bool = True
    """Promote the top-1 dense result from the original query into the top-5 of fused results if missing. Belt-and-suspenders safeguard against fusion math demoting exact matches."""
    rrf_legacy_mode: bool = False
    """When True, forces k=60 uniform weights and disables exact-match promotion. Fast rollback to pre-change behavior."""

    # ── Contextual chunking configuration ─────────────────────────────────────
    contextual_chunking_enabled: bool = False
    """Enable LLM-based contextual chunking (prepends document context to each chunk)."""
    contextual_chunking_concurrency: int = 5
    """Maximum concurrent LLM calls for contextual chunking."""

    # ── Multi-scale chunk indexing configuration ──────────────────────────────
    multi_scale_indexing_enabled: bool = True
    """Enable multi-scale chunk indexing (index chunks at multiple sizes for varied recall)."""
    multi_scale_chunk_sizes: str = "768,1536"
    """Comma-separated list of chunk sizes (in characters) for multi-scale indexing."""
    multi_scale_overlap_ratio: float = 0.1
    """Overlap ratio between adjacent chunks at each scale (0.0-1.0)."""

    # ── Query transformation configuration ────────────────────────────────────
    query_transformation_enabled: bool = True
    """Enable query transformation using step-back prompting for broader retrieval."""

    stepback_enabled: bool = True
    """Enable step-back prompting: generate a broader, more general version of the query to improve recall."""

    query_transform_temperature: float = 0.0
    """Temperature for LLM calls during query transformation (step-back). Set to 0.0 for deterministic results."""

    hyde_temperature: float = 0.0
    """Temperature for LLM calls during HyDE hypothetical document generation. Set to 0.0 for deterministic results."""

    query_transform_cache_ttl_sec: int = 86400
    """Time-to-live in seconds for cached query transformation results. Default 24 hours."""

    # ── Retrieval evaluation configuration ────────────────────────────────────
    retrieval_evaluation_enabled: bool = True
    """Enable CRAG-style retrieval evaluation (CONFIDENT/AMBIGUOUS/NO_MATCH classification)."""

    # ── Context distillation configuration ────────────────────────────
    context_distillation_enabled: bool = True
    """Enable context distillation: deduplicate sentences and optionally synthesize context."""

    context_distillation_dedup_threshold: float = 0.92
    """Cosine similarity threshold for sentence deduplication in context distillation (0.0-1.0)."""

    context_distillation_synthesis_enabled: bool = True
    """Enable LLM-based context synthesis when retrieval evaluation returns NO_MATCH only."""

    # ── Instant-mode latency overrides ────────────────────────────────────
    # Instant mode trades retrieval quality for speed. These flags skip the
    # expensive pre-generation LLM aux calls in Instant mode only; Thinking
    # mode is unaffected and keeps the full quality pipeline.
    instant_skip_query_transformation: bool = True
    """In Instant mode, skip step-back query transformation (saves one LLM call)."""

    instant_skip_retrieval_evaluation: bool = True
    """In Instant mode, skip CRAG retrieval evaluation (saves one LLM call)."""

    instant_skip_distillation_synthesis: bool = True
    """In Instant mode, skip context-distillation LLM synthesis. Defense-in-depth:
    synthesis only runs on a NO_MATCH verdict, which Instant no longer produces when
    instant_skip_retrieval_evaluation is True, so this is a belt-and-suspenders guard."""

    instant_skip_followup_rewrite: bool = False
    """In Instant mode, skip the follow-up rewrite LLM call. Default False: the rewrite
    runs on the fast Instant client and improves multi-turn retrieval, so it is kept on
    unless an operator explicitly trades it away for latency."""

    # ── Token budget configuration ────────────────────────────────────────
    context_max_tokens: int = 6000
    """Maximum approximate tokens for packed context before prompt building."""

    primary_evidence_count: int = 0
    """Override for primary evidence chunk count in prompt builder. 0 = use formula (min(max(n-2, 3), min(n, 5)))."""

    anchor_best_chunk: bool = True
    """Anchor the top-ranked chunk at both the start and end of the context region.
    Standard mitigation for lost-in-the-middle. Skipped if the top chunk exceeds 50% of context_max_tokens."""

    token_pack_strategy: str = "reserved_best_fit"
    """Token packing strategy: 'reserved_best_fit' reserves top-3 (never skipped) and uses best-fit for
    remaining chunks (no early break). 'greedy' is the legacy first-fit with early break."""

    # ── Chunking strategy ──────────────────────────────────────────────
    semantic_chunking_strategy: str = "title"
    """Chunking strategy: 'title' for fixed-size, 'embedding' for cosine-similarity breakpoints."""

    # ── HyDE (Hypothetical Document Embeddings) configuration ──
    hyde_enabled: bool = False
    """Enable HyDE: generate a hypothetical answer passage and embed it as additional query vector. Default False."""

    # ── Sparse search configuration ──────────────────────────────────
    sparse_search_max_candidates: int = 1000
    """Deprecated: Sparse search removed. Field retained for config compatibility."""

    sparse_embedding_timeout: float = 2.0
    """Deprecated: Sparse embedding removed. Field retained for config compatibility."""

    # ── Retrieval recency configuration ──────────────────────────
    retrieval_recency_weight: float = 0.1
    """Weight for recency score blending in RRF fusion (0.0 = disabled, 1.0 = fully recency-based)."""

    recency_decay_lambda: float = 0.001
    """Exponential decay rate (lambda) for recency scoring. Higher values decay faster."""

    # ── Parent-document retrieval configuration (Issue #12) ──────────────────
    parent_retrieval_enabled: bool = True
    """Enable parent-window expansion at prompt time: retrieve on small chunks, deliver the
    surrounding parent window to the LLM for broader context.

    When True, the prompt builder reads chunk.parent_window_text and renders
    the broader window with [[MATCH: …]] markers around the original small
    chunk. Chunks without a stored parent window (legacy ingest, spreadsheets,
    schema files) gracefully degrade to small-chunk-only rendering — the
    feature is safe to enable on un-migrated databases.

    Operators who explicitly want the legacy behavior can set this to False
    via env. The startup validation in lifespan.py emits a log warning when
    the chunks table does not yet have parent-window columns populated."""

    new_dedup_policy: bool = True
    """Enable group-aware dedup: preserve up to PER_DOC_CHUNK_CAP chunks per document and
    cap breadth at UNIQUE_DOCS_IN_TOP_K distinct documents. Replaces UID-strip dedup that
    collapsed the best document's multiple strong chunks to one. Default True (safe improvement)."""

    per_doc_chunk_cap: int = 5
    """Maximum chunks kept per document after group-aware dedup. Higher values give more
    context from each document at the cost of less diversity across sources."""

    unique_docs_in_top_k: int = 5
    """Maximum distinct documents allowed in the final dedup output when new_dedup_policy is
    enabled. Balances breadth vs. depth in source diversity."""

    parent_window_chars: int = 6000
    """Size of the parent window (in characters) delivered to the prompt when
    parent_retrieval_enabled=True. The matched small chunk is bracketed with [[MATCH:…]]
    markers inside this window for the LLM to orient itself."""

    # ── Ingestion integrity configuration (Issue #13) ────────────────────────
    index_rebuild_delta: float = 0.2
    """Fraction of row churn (deletes / previous row count) that triggers an IVF_PQ index
    rebuild. Default 0.2 = 20% row loss triggers rebuild. Keeps ANN search accurate after
    bulk deletes without rebuilding on every small delete."""

    reupload_safe_order: bool = True
    """Use insert-then-delete ordering for re-uploads with a changed file hash. When True,
    new chunks are inserted and the file pointer is flipped BEFORE old chunks are deleted,
    ensuring the corpus is never in a zero-chunk state for a live document."""

    # ── Tri-vector embedding configuration (deprecated) ───────────────────────
    tri_vector_search_enabled: bool = False
    """Deprecated: BGE-M3 replaced by Harrier. Field retained for config compatibility."""
    flag_embedding_url: str = ""
    """Deprecated: FlagEmbedding server removed. Field retained for config compatibility."""

    # ── Chunk enrichment / curator configuration ───────────────────────────
    chunk_enrichment_enabled: bool = False
    """Enable curator-style chunk enrichment (generates auxiliary metadata for retrieval)."""
    chunk_enrichment_concurrency: int = 5
    """Maximum concurrent LLM calls for chunk enrichment."""
    chunk_enrichment_fields: str = "summary,questions,entities"
    """Comma-separated list of enrichment fields to generate: summary, questions, entities, aliases."""

    # ── Wiki / Knowledge Compiler configuration ──────────────────────────
    wiki_enabled: bool = True
    """Master switch for the Knowledge Compiler / wiki subsystem."""
    wiki_fts_page_search_max_candidates: int = 5
    """Upper bound on the candidate-pool size that triggers the FTS page-search
    fallback (phase 4 of wiki retrieval). Page search only runs when the
    candidates dict from phases 1–3 contains fewer than this many entries.
    Lower values (e.g. 0) make page search more aggressive; higher values
    make it rarer. Default 5 preserves the historical hardcoded threshold;
    issue #101 reported a default of 3 — operators that want to recover the
    original behavior can set WIKI_FTS_PAGE_SEARCH_MAX_CANDIDATES=3."""
    wiki_compile_on_ingest: bool = True
    """Run wiki compile when a document finishes indexing."""
    wiki_compile_on_query: bool = True
    """Run wiki compile when a chat query touches the vault."""
    wiki_compile_after_indexing: bool = True
    """Backwards-compatible alias for wiki_compile_on_ingest."""
    wiki_lint_enabled: bool = True
    """Run wiki lint sweeps when triggered from the UI."""

    # ── Optional LLM Wiki Curator (PR C wires this through) ───────────────
    wiki_llm_curator_enabled: bool = False
    """Master switch for the LLM-assisted wiki curator. Default OFF."""
    wiki_llm_curator_url: str = ""
    """OpenAI-compatible /v1/chat/completions base URL for the curator model.
    Empty when the curator is disabled. SSRF-guarded at the route boundary."""
    wiki_llm_curator_model: str = ""
    """Model name passed to the curator endpoint. Empty when disabled."""
    wiki_llm_curator_temperature: float = 0.0
    """Temperature for curator calls. Default 0.0 for determinism."""
    wiki_llm_curator_max_input_chars: int = 6000
    """Maximum source text passed to the curator per call (1000-24000)."""
    wiki_llm_curator_max_output_tokens: int = 2048
    """max_tokens passed to the curator per call."""
    wiki_llm_curator_timeout_sec: float = 120.0
    """Per-call timeout in seconds (10-600)."""
    wiki_llm_curator_concurrency: int = 1
    """Maximum concurrent curator calls inside one compile job (1-4)."""
    wiki_llm_curator_mode: str = "draft"
    """draft (always status='needs_review') or active_if_verified
    (status='active' iff source_quote + chunk_id verify)."""
    wiki_llm_curator_require_quote_match: bool = True
    """Reject candidates whose source_quote does not match the source text."""
    wiki_llm_curator_require_chunk_id: bool = True
    """Reject candidates whose chunk_id is not in the provided chunk set."""
    wiki_llm_curator_run_on_ingest: bool = True
    """Run the curator in ingest compile jobs when curator is enabled."""
    wiki_llm_curator_run_on_query: bool = False
    """Run the curator in query compile jobs. Default OFF to protect chat latency."""
    wiki_llm_curator_run_on_manual: bool = True
    """Run the curator in manual / recompile jobs when curator is enabled."""

    # ── KMS / Knowledge Management configuration ─────────────────────────
    kms_enabled: bool = True
    """Master switch for the KMS (user-curated knowledge management) subsystem."""
    kms_compile_on_ingest: bool = True
    """Create/refresh a KMS document entry when a document finishes indexing."""

    # ── Retrieval profile configuration ──────────────────────────────────
    retrieval_profile: str = "advanced"
    """Retrieval profile: 'baseline' (dense + hybrid + rerank), 'advanced' (adds enrichment)."""

    # Document processing configuration (legacy - DEPRECATED)
    chunk_size: int | None = None
    """[DEPRECATED] Token-based chunk size. Use chunk_size_chars instead."""
    chunk_overlap: int | None = None
    """[DEPRECATED] Token-based chunk overlap. Use chunk_overlap_chars instead."""
    max_context_chunks: int = 10
    """[DEPRECATED] Number of context chunks. Use retrieval_top_k instead."""

    # RAG configuration (legacy - DEPRECATED)
    rag_relevance_threshold: float | None = None
    """[DEPRECATED] Relevance threshold. Use max_distance_threshold instead."""
    vector_top_k: int | None = None
    """[DEPRECATED] Vector top K. Use retrieval_top_k instead."""
    maintenance_mode: bool = False
    redis_url: str = "redis://localhost:6379/0"
    csrf_token_ttl: int = 900
    admin_rate_limit: str = "10/minute"

    # Rate limiting
    chat_rate_limit: str = "30/minute"
    """Rate limit for chat endpoints."""
    search_rate_limit: str = "30/minute"
    """Rate limit for search endpoints."""
    vault_create_rate_limit: str = "30/minute"
    """Rate limit for vault creation endpoints."""
    memory_mutation_rate_limit: str = "30/minute"
    """Rate limit for memory mutation endpoints (create, update, delete)."""

    health_check_api_key: str = ""
    csrf_cookie_secure: bool = False
    """Set Secure flag on CSRF cookie. Default False for local development. Set to True in production with HTTPS."""

    # Auto-scan configuration
    auto_scan_enabled: bool = True
    auto_scan_interval_minutes: int = 60

    # Logging configuration
    log_level: str = "INFO"

    # Feature flags
    enable_model_validation: bool = False
    eval_enabled: bool = False
    """Enable RAGAS evaluation endpoint. Disabled by default for production safety."""

    # Admin security
    admin_secret_token: str = (
        ""  # Must be set via environment variable - no default for security
    )

    # User authentication
    users_enabled: bool = True
    """Enable multi-user JWT authentication. When False, only admin_secret_token auth is used."""

    jwt_secret_key: str = "change-me-to-a-random-64-char-string"
    """Secret key for JWT signing. MUST be changed in production. Generate with: python -c \"import secrets; print(secrets.token_urlsafe(48))\""""

    jwt_algorithm: str = "HS256"
    """JWT signing algorithm."""

    audit_hmac_key_version: str = "v1"

    # Security settings
    max_file_size_mb: int = 50
    allowed_extensions: set[str] = {
        ".txt",
        ".md",
        ".pdf",
        ".docx",
        ".pptx",
        ".csv",
        ".xls",
        ".xlsx",
        ".json",
        ".sql",
        ".py",
        ".js",
        ".ts",
        ".html",
        ".css",
        ".xml",
        ".yaml",
        ".yml",
        ".log",
    }

    # IMAP Email Ingestion configuration
    imap_enabled: bool = False
    imap_host: str = ""
    imap_port: int = 993
    imap_username: str = ""
    imap_password: SecretStr = SecretStr("")
    imap_use_ssl: bool = True
    imap_mailbox: str = "INBOX"
    imap_poll_interval: int = 60  # seconds
    imap_max_attachment_size: int = 10 * 1024 * 1024  # 10MB
    imap_allowed_mime_types: set[str] = {
        "application/pdf",
        "text/plain",
        "text/markdown",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "text/csv",
        "application/json",
        "application/sql",
        "text/x-python",
        "application/javascript",
        "text/html",
        "text/css",
        "application/xml",
        "application/x-yaml",
        "text/x-log",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    }

    # CORS settings
    backend_cors_origins: Annotated[list[str], NoDecode] = [
        "http://localhost:5173",
        "http://localhost:3000",
    ]

    # Helper validation functions (consolidated validators)
    @staticmethod
    def _validate_int_range(
        v: int, min_val: int | None, max_val: int | None, field_name: str
    ) -> int:
        """Validate an integer is within a specified range."""
        if min_val is not None and v < min_val:
            raise ValueError(f"{field_name} must be >= {min_val}")
        if max_val is not None and v > max_val:
            raise ValueError(f"{field_name} must be <= {max_val}")
        return v

    @staticmethod
    def _validate_float_range(
        v: float, min_val: float | None, max_val: float | None, field_name: str
    ) -> float:
        """Validate a float is within a specified range."""
        if min_val is not None and v < min_val:
            raise ValueError(f"{field_name} must be >= {min_val}")
        if max_val is not None and v > max_val:
            raise ValueError(f"{field_name} must be <= {max_val}")
        return v

    @staticmethod
    def _validate_enum(v: str, allowed: set[str], field_name: str) -> str:
        """Validate a string is one of the allowed values."""
        if v not in allowed:
            raise ValueError(
                f"{field_name} must be one of: {', '.join(sorted(allowed))}"
            )
        return v

    @field_validator("app_root_path", mode="after")
    @classmethod
    def validate_app_root_path(cls, v: str) -> str:
        """Normalize and validate the browser-visible app root path."""
        return normalize_root_path(v)

    @field_validator("backend_cors_origins", mode="before")
    @classmethod
    def parse_backend_cors_origins(cls, v):
        """Support JSON lists and comma-separated env values for CORS origins."""
        if isinstance(v, str):
            value = v.strip()
            if not value:
                return []
            if value.startswith("["):
                import json

                parsed = json.loads(value)
                if not isinstance(parsed, list):
                    raise ValueError("backend_cors_origins JSON value must be a list")
                return parsed
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return v

    # Migration validators for backward compatibility
    @field_validator("chunk_size_chars", mode="before")
    @classmethod
    def migrate_chunk_size_chars(cls, v: int | None, values) -> int:
        """Auto-convert from legacy chunk_size if chunk_size_chars not provided."""
        if v is not None:
            return v
        legacy_chunk_size = values.data.get("chunk_size")
        if legacy_chunk_size is not None:
            logger.warning(
                "Deprecated: 'chunk_size' is deprecated. Use 'chunk_size_chars' instead. "
                f"Auto-converting chunk_size={legacy_chunk_size} to chunk_size_chars={legacy_chunk_size * 4}."
            )
            return legacy_chunk_size * 4
        return 2000  # ~500 tokens with llama.cpp -ub 8192 batch size

    @field_validator("chunk_overlap_chars", mode="before")
    @classmethod
    def migrate_chunk_overlap_chars(cls, v: int | None, values) -> int:
        """Auto-convert from legacy chunk_overlap if chunk_overlap_chars not provided."""
        if v is not None:
            return v
        legacy_chunk_overlap = values.data.get("chunk_overlap")
        if legacy_chunk_overlap is not None:
            logger.warning(
                "Deprecated: 'chunk_overlap' is deprecated. Use 'chunk_overlap_chars' instead. "
                f"Auto-converting chunk_overlap={legacy_chunk_overlap} to chunk_overlap_chars={legacy_chunk_overlap * 4}."
            )
            return legacy_chunk_overlap * 4
        return 200  # ~50 tokens overlap

    @field_validator("retrieval_top_k", mode="before")
    @classmethod
    def migrate_retrieval_top_k(cls, v: int | None, values) -> int:
        """Auto-convert from legacy vector_top_k if retrieval_top_k not provided."""
        if v is not None:
            return v
        legacy_vector_top_k = values.data.get("vector_top_k")
        if legacy_vector_top_k is not None:
            logger.warning(
                "Deprecated: 'vector_top_k' is deprecated. Use 'retrieval_top_k' instead. "
                f"Auto-copying vector_top_k={legacy_vector_top_k} to retrieval_top_k={legacy_vector_top_k}."
            )
            return legacy_vector_top_k
        return 12

    @field_validator("max_context_chunks", mode="after")
    @classmethod
    def deprecate_max_context_chunks(cls, v: int) -> int:
        """Emit deprecation warning if max_context_chunks is set to non-default value."""
        if v != 10:
            warnings.warn(
                "MAX_CONTEXT_CHUNKS is deprecated. Use RETRIEVAL_TOP_K instead. "
                "This setting will be removed in a future version.",
                DeprecationWarning,
                stacklevel=2,
            )
        return v

    # Consolidated range validators using helper functions
    @field_validator("embedding_batch_max_retries", mode="after")
    @classmethod
    def validate_embedding_batch_max_retries(cls, v: int) -> int:
        """Validate embedding batch max retries is in range 0..10."""
        return cls._validate_int_range(v, 0, 10, "embedding_batch_max_retries")

    @field_validator("embedding_batch_min_sub_size", mode="after")
    @classmethod
    def validate_embedding_batch_min_sub_size(cls, v: int) -> int:
        """Validate embedding batch minimum sub-size is >= 1."""
        return cls._validate_int_range(v, 1, None, "embedding_batch_min_sub_size")

    @field_validator("ingestion_worker_count", mode="after")
    @classmethod
    def validate_ingestion_worker_count(cls, v: int) -> int:
        """Validate ingestion worker count is in range 1..16."""
        return cls._validate_int_range(v, 1, 16, "ingestion_worker_count")

    @field_validator("optimize_mode", mode="after")
    @classmethod
    def validate_optimize_mode(cls, v: str) -> str:
        """Validate optimize_mode is one of the allowed values."""
        return cls._validate_enum(
            v, {"after_every_write", "periodic", "manual"}, "optimize_mode"
        )

    @field_validator("ingestion_llm_mode", mode="after")
    @classmethod
    def validate_ingestion_llm_mode(cls, v: str) -> str:
        """Validate ingestion LLM routing mode."""
        return cls._validate_enum(
            v, {"instant", "thinking", "disabled"}, "ingestion_llm_mode"
        )

    @field_validator("optimize_interval_chunks", mode="after")
    @classmethod
    def validate_optimize_interval_chunks(cls, v: int) -> int:
        """Validate optimize_interval_chunks is >= 1."""
        return cls._validate_int_range(v, 1, None, "optimize_interval_chunks")

    @field_validator("embedding_concurrent_batches", mode="after")
    @classmethod
    def validate_embedding_concurrent_batches(cls, v: int) -> int:
        """Validate embedding concurrent batches is in range 1..16."""
        return cls._validate_int_range(v, 1, 16, "embedding_concurrent_batches")

    @field_validator("embedding_global_concurrent_batches", mode="after")
    @classmethod
    def validate_embedding_global_concurrent_batches(cls, v: int) -> int:
        """Validate global embedding concurrent batches is in range 1..16."""
        return cls._validate_int_range(v, 1, 16, "embedding_global_concurrent_batches")

    @field_validator("embedding_batch_size", mode="after")
    @classmethod
    def validate_embedding_batch_size(cls, v: int) -> int:
        """Validate embedding batch size is within safe TEI limits (1-128)."""
        return cls._validate_int_range(v, 1, 128, "embedding_batch_size")

    @field_validator("document_parsing_strategy", mode="after")
    @classmethod
    def validate_document_parsing_strategy(cls, v: str) -> str:
        """Validate document parsing strategy is one of: fast, hi_res, auto."""
        return cls._validate_enum(
            v, {"fast", "hi_res", "auto"}, "document_parsing_strategy"
        )

    @field_validator("token_pack_strategy", mode="after")
    @classmethod
    def validate_token_pack_strategy(cls, v: str) -> str:
        """Validate token_pack_strategy is one of: reserved_best_fit, greedy."""
        return cls._validate_enum(v, {"reserved_best_fit", "greedy"}, "token_pack_strategy")

    @field_validator("multi_scale_chunk_sizes", mode="after")
    @classmethod
    def validate_multi_scale_chunk_sizes(cls, v: str) -> str:
        """Validate multi_scale_chunk_sizes is a comma-separated list of unique positive integers."""
        sizes = [int(x.strip()) for x in v.split(",") if x.strip()]
        if not sizes:
            raise ValueError("multi_scale_chunk_sizes cannot be empty")
        unique_sizes = sorted(set(sizes))
        if len(unique_sizes) != len(sizes):
            raise ValueError("multi_scale_chunk_sizes must contain unique values")
        for size in unique_sizes:
            if size <= 0:
                raise ValueError(
                    "multi_scale_chunk_sizes must contain only positive integers"
                )
        return ",".join(str(x) for x in unique_sizes)

    @field_validator("multi_scale_overlap_ratio", mode="after")
    @classmethod
    def validate_multi_scale_overlap_ratio(cls, v: float) -> float:
        """Validate multi_scale_overlap_ratio is in range 0.0-1.0."""
        return cls._validate_float_range(v, 0.0, 1.0, "multi_scale_overlap_ratio")

    @field_validator("index_rebuild_delta", mode="after")
    @classmethod
    def validate_index_rebuild_delta(cls, v: float) -> float:
        """Validate index_rebuild_delta is in range 0.0-1.0."""
        return cls._validate_float_range(v, 0.0, 1.0, "index_rebuild_delta")

    @field_validator("per_doc_chunk_cap", mode="after")
    @classmethod
    def validate_per_doc_chunk_cap(cls, v: int) -> int:
        """Validate per_doc_chunk_cap is >= 1."""
        return cls._validate_int_range(v, 1, None, "per_doc_chunk_cap")

    @field_validator("unique_docs_in_top_k", mode="after")
    @classmethod
    def validate_unique_docs_in_top_k(cls, v: int) -> int:
        """Validate unique_docs_in_top_k is >= 1."""
        return cls._validate_int_range(v, 1, None, "unique_docs_in_top_k")

    @field_validator("hybrid_alpha", mode="after")
    @classmethod
    def validate_hybrid_alpha(cls, v: float) -> float:
        """Validate hybrid_alpha is in range 0.0-1.0."""
        return cls._validate_float_range(v, 0.0, 1.0, "hybrid_alpha")

    @field_validator("rrf_weight_original", "rrf_weight_stepback", "rrf_weight_hyde", mode="after")
    @classmethod
    def validate_rrf_weights(cls, v: float) -> float:
        """Validate RRF weights are non-negative."""
        if v < 0.0:
            raise ValueError("RRF weights must be >= 0.0")
        return v

    @field_validator("hybrid_rrf_k", "multi_query_rrf_k", "multi_scale_rrf_k", "memory_rrf_k", mode="after")
    @classmethod
    def validate_rrf_k(cls, v: int) -> int:
        """Validate RRF k parameters are >= 1 (prevents ZeroDivisionError in 1/(k+rank))."""
        if v < 1:
            raise ValueError("RRF k must be >= 1")
        return v

    @field_validator("reranker_timeout_seconds", mode="after")
    @classmethod
    def validate_reranker_timeout_seconds(cls, v: float) -> float:
        """Validate the reranker HTTP timeout is positive. A value <= 0 makes
        httpx time out on every reranking request, silently degrading every
        query to un-reranked vector order — so we fail fast at startup instead."""
        if v <= 0:
            raise ValueError("reranker_timeout_seconds must be > 0")
        return v

    @field_validator("default_chat_mode", mode="after")
    @classmethod
    def validate_default_chat_mode(cls, v: str) -> str:
        """Validate default chat mode from env/defaults before startup."""
        if v not in ("instant", "thinking"):
            raise ValueError("default_chat_mode must be 'instant' or 'thinking'")
        return v

    @field_validator(
        "instant_initial_retrieval_top_k",
        "instant_reranker_top_n",
        "instant_memory_context_top_k",
        "instant_max_tokens",
        mode="after",
    )
    @classmethod
    def validate_instant_positive_ints(cls, v: int) -> int:
        """Validate instant-mode budgets from env/defaults before startup."""
        if v < 1:
            raise ValueError("instant-mode numeric settings must be >= 1")
        return v

    @field_validator("library_vault_id", mode="after")
    @classmethod
    def validate_library_vault_id(cls, v: Optional[int]) -> Optional[int]:
        """Validate library_vault_id is positive when set."""
        if v is not None and v <= 0:
            raise ValueError("library_vault_id must be > 0 when set")
        return v

    @model_validator(mode="after")
    def validate_rrf_weight_sanity(self) -> "Settings":
        """Validate at least one RRF arm weight is > 0.0 to prevent silent retrieval outage."""
        if (
            self.rrf_weight_original == 0.0
            and self.rrf_weight_stepback == 0.0
            and self.rrf_weight_hyde == 0.0
        ):
            raise ValueError("At least one RRF arm weight must be > 0.0")
        return self

    @model_validator(mode="after")
    def validate_batch_config_consistency(self) -> "Settings":
        """Validate embedding batch configuration consistency."""
        if self.embedding_batch_min_sub_size > self.embedding_batch_size:
            raise ValueError(
                "embedding_batch_min_sub_size must be <= embedding_batch_size"
            )
        return self

    @model_validator(mode="after")
    def reject_insecure_defaults(self) -> "Settings":
        """Refuse startup if security-critical secrets use default values."""
        if self.users_enabled and not self.admin_secret_token:
            raise ValueError(
                "ADMIN_SECRET_TOKEN must be set when USERS_ENABLED=True. "
                'Generate one with: python -c "import secrets; print(secrets.token_urlsafe(48))"'
            )
        if self.users_enabled and (not self.jwt_secret_key.strip() or self.jwt_secret_key == "change-me-to-a-random-64-char-string"):
            raise ValueError(
                "JWT_SECRET_KEY must be changed from the default when USERS_ENABLED=True. "
                'Generate one with: python -c "import secrets; print(secrets.token_urlsafe(48))"'
            )
        # Single-admin mode: admin_secret_token is the ONLY authentication mechanism.
        # Running without it means the application has no authentication at all.
        if not self.users_enabled and not self.admin_secret_token.strip():
            raise ValueError(
                "ADMIN_SECRET_TOKEN must be set when USERS_ENABLED=False (single-admin mode). "
                "In single-admin mode this token is the sole authentication mechanism. "
                'Generate one with: python -c "import secrets; print(secrets.token_urlsafe(48))"'
            )
        return self

    @model_validator(mode="after")
    def validate_hyde_config(self) -> "Settings":
        """Warn when HyDE is enabled without query transformation."""
        if self.hyde_enabled and not self.query_transformation_enabled:
            warnings.warn(
                "HyDE is enabled but query_transformation_enabled is False. "
                "HyDE works best when query transformation is also enabled.",
                UserWarning,
                stacklevel=2,
            )
        return self

    @property
    def documents_dir(self) -> Path:
        return self.data_dir / "documents"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def vaults_dir(self) -> Path:
        path = self.data_dir / "vaults"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def vault_dir(self, vault_id: int) -> Path:
        """Canonical per-vault storage directory, keyed by integer ID."""
        path = self.data_dir / "vaults" / str(vault_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def vault_uploads_dir(self, vault_id: int) -> Path:
        """Canonical per-vault uploads directory."""
        path = self.vault_dir(vault_id) / "uploads"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def vault_documents_dir(self, vault_id: int) -> Path:
        """Canonical per-vault documents directory."""
        path = self.vault_dir(vault_id) / "documents"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def library_dir(self) -> Path:
        return self.data_dir / "library"

    @property
    def lancedb_path(self) -> Path:
        return self.data_dir / "lancedb"

    @property
    def sqlite_path(self) -> Path:
        return self.data_dir / "app.db"


# Global settings instance
settings = Settings()
