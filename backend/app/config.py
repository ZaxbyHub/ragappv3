"""
Application configuration using Pydantic Settings.
"""

import logging
import warnings
from pathlib import Path
from typing import Annotated, Mapping, Optional

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from app.services.document_artifacts import RASTER_IMAGE_EXTENSIONS
from app.utils.paths import normalize_root_path

logger = logging.getLogger(__name__)

# Legacy → new settings-field migration table (issue #494 CONFIG-004).
#
# Each row is (legacy field, replacement field, conversion factor). The
# factors are pinned by issue #494 acceptance check C15:
#   chunk_size       → chunk_size_chars   (x4: 4 chars per token)
#   chunk_overlap    → chunk_overlap_chars (x4: 4 chars per token)
#   vector_top_k     → retrieval_top_k     (x1: same unit — chunk count)
LEGACY_SETTINGS_CONVERSIONS: tuple[tuple[str, str, int], ...] = (
    ("chunk_size", "chunk_size_chars", 4),
    ("chunk_overlap", "chunk_overlap_chars", 4),
    ("vector_top_k", "retrieval_top_k", 1),
)


def apply_legacy_settings_conversion(data: Mapping[str, object]) -> dict:
    """Apply deprecated legacy settings values onto their replacement fields.

    THE single implementation of the legacy→new precedence, shared by Settings
    construction (``_convert_legacy_fields_at_construction`` below) and the
    live settings API (``app/api/routes/settings.py``, issue #494 CONFIG-004):

      - an explicit replacement-field value (not None) is NEVER overridden;
      - a legacy value present while its replacement is absent produces the
        converted replacement value (factors in ``LEGACY_SETTINGS_CONVERSIONS``);
      - neither present → untouched (field defaults apply).

    Returns a new dict; the input mapping is not mutated.
    """
    converted = dict(data)
    for legacy_field, new_field, factor in LEGACY_SETTINGS_CONVERSIONS:
        legacy_value = converted.get(legacy_field)
        if legacy_value is None:
            continue
        # pydantic-settings delivers env/.env values as STRINGS into the
        # mode="before" validator; multiply numerically, never lexically
        # ("512" * 4 == "512512512512" — PR #576 review F1).
        if isinstance(legacy_value, str):
            try:
                legacy_value = int(legacy_value.strip())
            except ValueError:
                logger.warning(
                    "Deprecated: '%s' value %r is not an integer; ignoring.",
                    legacy_field,
                    legacy_value,
                )
                continue
        if converted.get(new_field) is None:
            converted[new_field] = legacy_value * factor
            logger.warning(
                "Deprecated: '%s' is deprecated. Use '%s' instead. "
                "Auto-converting %s=%s to %s=%s.",
                legacy_field,
                new_field,
                legacy_field,
                legacy_value,
                new_field,
                legacy_value * factor,
            )
    return converted


class Settings(BaseSettings):
    """Application settings with environment variable support."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # docker-compose forwards documented keys as `- KEY=${KEY:-}`, which
        # injects an EMPTY STRING for every key the operator's .env omits.
        # Without this flag pydantic treats "" as a provided value and the
        # int/float/bool fields fail coercion at startup (PR #576 review F2):
        # an empty value must behave exactly like an unset one.
        env_ignore_empty=True,
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
    editorial_chat_url: str = Field(default="", alias="DRAFT_EDITORIAL_CHAT_URL")
    editorial_chat_model: str = Field(default="", alias="DRAFT_EDITORIAL_CHAT_MODEL")
    instant_chat_url: str = "http://host.docker.internal:1234"
    instant_chat_model: str = "nvidia/nemotron-3-nano-4b"
    default_chat_mode: str = "thinking"  # "instant" | "thinking"

    # Per-mode retrieval overrides (Instant uses smaller budget)
    instant_initial_retrieval_top_k: int = 10
    instant_reranker_top_n: int = 4
    instant_memory_context_top_k: int = 2
    instant_max_tokens: int = 4096
    thinking_max_tokens: int = 32768
    """Maximum output tokens for the thinking (high-quality) chat mode. Mirrors
    ``instant_max_tokens`` for the thinking branch of RAGEngine.query; default
    32768 preserves the prior hardcoded budget exactly. Configurable so operators
    can shrink the thinking-mode token budget without editing source (issue #395
    DD-rag-005). Must be >= 1 (see validate_per_mode_positive_ints)."""
    instant_enable_thinking: bool = False
    """Whether Instant-mode chat requests should leave the model's chat-template
    thinking mode enabled. False (default) sends ``enable_thinking: False`` via
    ``chat_template_kwargs`` — the behavior Instant traffic has always had and
    the correct setting for Gemma-4-style deployments whose templates default to
    thinking. True omits the kwarg entirely so the provider/model template
    default governs (for future Instant models that legitimately want thinking).
    Documented call-role setting per issue #494 FU-005; per-provider capability
    tables belong to model qualification (F2), not this switch."""
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
    """Window size for retrieval context expansion. 0 disables neighbor-chunk
    expansion entirely (document_retrieval skips expand_window when the value
    is not > 0); positive values expand each retrieved chunk by that many
    neighboring chunks. Documented UI range 0-3."""
    embedding_batch_size: int = 64
    """Number of texts to send per embedding API request. Capped at 128 for TEI compatibility."""
    embedding_batch_max_retries: int = 3
    """Maximum number of retries for adaptive batching when token overflow occurs."""
    embedding_batch_min_sub_size: int = 1
    """Minimum sub-batch size for adaptive batching fallback."""
    embedding_batch_max_chars: int = 131072
    """Maximum total characters per embedding API batch (issue #513 W23).
    ``embed_batch`` closes a batch BEFORE adding a text that would exceed this
    budget; an oversized single text ships alone (per-text validation still
    applies). Bounds the token cost of each request alongside
    ``embedding_batch_size``. Range 4096-1048576."""
    embedding_cache_max_entries: int = 50000
    """Capacity bound for the persistent embedding cache (issue #513 W24),
    stored in its own sqlite file under ``data_dir`` by
    ``app.services.embedding_cache``. Oldest entries are pruned beyond the cap.
    Range 1000-1000000."""
    near_dup_threshold: float = 0.96
    """Cosine threshold for advisory near-duplicate grouping (issue #513 W26).
    Files whose chunk-embedding centroids are at least this similar (cosine)
    share an advisory ``group_id``; grouping never blocks, deletes, or rejects
    documents. Range 0.5-0.999999."""
    orphan_rescan_interval_seconds: float = 3600.0
    """Interval in seconds for the periodic stranded-ingestion rescan loop
    (issue #513 W25). Each tick re-enqueues stranded pending/processing rows
    (with an active-job lease guard); the startup sweep is separate and runs
    unconditionally. Range 60.0-86400.0."""

    # ── Ingestion performance configuration ──────────────────────────────────
    ingestion_queue_max_size: int = 1000
    """Max size for the ingestion and enrichment asyncio.Queue (backpressure bound).
    1000 follows the Python/asyncio best-practice range (100-1000) for I/O-bound RAG workers.
    """
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
    vector_search_concurrency: int = 32
    """Maximum concurrent LanceDB search operations (1-64). Semaphore size for parallel vector searches."""
    search_semaphore_timeout_seconds: float = 30.0
    """Timeout in seconds for search semaphore acquisitions. Prevents indefinite blocking if search concurrency is saturated."""
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
    memory_store_pool_size: int = 10
    """Dedicated SQLiteConnectionPool max_size for MemoryStore concurrent
    retrieval operations. Default 10 matches the application pool default
    and removes the historical bottleneck of 2."""
    db_pool_max_size: int = 10
    """Maximum number of connections in the main application SQLiteConnectionPool
    (the singleton seeded at startup and shared by the get_pool() cache). Default
    10. Must be >= 1. This is the pool backing auth/permission checks, route DB
    access, and (briefly) the streaming-chat pre-stream auth boundary. Separate
    from memory_store_pool_size, which governs the MemoryStore pool."""
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
    memory_eviction_interval_seconds: int = 300
    """Interval, in seconds, between periodic background sweeps that delete expired
    memories (rows with a past `expires_at`). Runs off the search hot path (issue #263);
    see `MemoryStore.periodic_eviction_loop` in memory_store.py."""
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

    retrieval_consolidated_rerank: bool = True
    """Sub-query orchestration (issue #511): fuse + dedupe evidence from all sub-queries BEFORE one
    final rerank call instead of reranking every sub-query separately. Set False to restore the
    legacy per-sub-query rerank behavior (rollback switch)."""

    retrieval_kms_overlap: bool = True
    """Start KMS retrieval concurrently with the document pipeline (issue #511) instead of awaiting
    wiki+KMS before document retrieval; wiki evidence still gates raw RAG (precedence preserved).
    Set False to restore the legacy serialized wiki/KMS gather (rollback switch)."""

    query_transform_temperature: float = 0.0
    """Temperature for LLM calls during query transformation (step-back). Set to 0.0 for deterministic results."""

    hyde_temperature: float = 0.0
    """Temperature for LLM calls during HyDE hypothetical document generation. Set to 0.0 for deterministic results."""

    query_transform_cache_ttl_sec: int = 86400
    """Time-to-live in seconds for cached query transformation results. Default 24 hours."""

    active_user_cache_ttl_seconds: int = 30
    """Time-to-live in seconds for cached active-user lookups in get_current_active_user.
    0 disables the in-memory cache entirely. Positive values must be between 5 and 300
    inclusive.
    """

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

    prompt_budget_enabled: bool = False
    """Optional TOTAL model-aware prompt budget (issue #511). When False (default) prompts are built
    exactly as before. When True the assembled prompt (system, history, evidence, memories, wiki/KMS,
    visual observations) plus reserved answer tokens is kept within model_context_tokens."""

    model_context_tokens: int = 8192
    """Total context window of the selected chat model, in tokens. Operators MUST set this to match
    the deployed model; 8192 is a deliberately conservative default. Only used when
    prompt_budget_enabled is True."""

    prompt_reserve_output_tokens: int = 2048
    """Answer tokens reserved out of model_context_tokens before the prompt budget is computed.
    Only used when prompt_budget_enabled is True."""

    context_distiller_max_sentences: int = 600
    """Hard cap on sentences admitted to context-distiller dedup (issue #511). The greedy
    similarity loop is O(n^2); inputs above the cap are truncated with a logged note."""

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

    # ── Agentic RAG configuration ────────────────────────────────────────
    agentic_rag_enabled: bool = False
    """Enable agentic multi-step RAG (iterative retrieval + LLM synthesis via tool registry)."""

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

    # ── Draft Room configuration (issue #435, SPEC.md section 15) ────────
    # PR 1 subset. Compile/model-routing limits below this block (issue #436)
    # complete the SPEC.md §15 settings table.
    draft_room_enabled: bool = False
    """Master switch for Draft Room. Default False: create/edit/upload return 503 draft_room_disabled while capability discovery and owner cleanup stay available."""
    draft_max_inputs: int = 10
    """Maximum number of uploaded inputs per Draft Room project."""
    draft_max_total_input_mb: int = 250
    """Maximum total raw input bytes per project, in MB. The global per-file max_file_size_mb limit still applies to each upload."""
    draft_max_total_parsed_chars: int = 500000
    """Aggregate cap on normalized parsed characters across a project's ready inputs. Bounds decompression and parser amplification."""
    draft_parse_timeout_seconds: int = 300
    """Wall-clock limit for a single input extraction job."""
    draft_upload_rate_limit: str = "20/minute"
    """Rate limit for Draft Room input uploads."""
    draft_poll_interval_seconds: float = 2.0
    """Poll interval for the durable Draft Room job processor."""

    # ── Canvas configuration (issue #509) ─────────────────────────────────
    # Versioned code/document canvas. Purely additive: no chat endpoint,
    # schema object, or prompt is altered. Every entry point (frontend buttons
    # and backend routes alike) fails closed, so default-on changes nothing for
    # users who never open a canvas. Operators wanting the pre-#509 surface
    # set CANVAS_ENABLED=false (all canvas routes 503, UI hidden).
    canvas_enabled: bool = True
    """Master switch for the versioned canvas. When False every canvas route
    returns 503 canvas_disabled and the frontend entry points stay hidden."""
    canvas_max_artifact_kb: int = 512
    """Maximum artifact content size in KB enforced at create and save (413
    canvas_artifact_too_large beyond it)."""
    canvas_max_versions: int = 500
    """Per-artifact version cap enforced on every append path (save, restore,
    model edit). 413 canvas_version_limit_reached beyond it; raise or add a
    retention policy before lowering."""

    # ── Draft Room pipeline (issue #436, SPEC.md section 15) ──────────────
    draft_allowed_model_origins: Annotated[list[str], NoDecode] = []
    """Comma-separated exact normalized origins (scheme://host:port) allowed to
    receive ordinary-tier Draft Room model content. Empty blocks compile
    (fail closed). Enforced before enqueue and before every model call."""
    draft_sensitive_allowed_model_origins: Annotated[list[str], NoDecode] = []
    """Stricter exact-origin allowlist required for the `sensitive` tier, in
    addition to draft_allowed_model_origins. Empty blocks sensitive-tier
    compile (fail closed)."""

    # ── Multimodal artifact enrichment configuration (issue #461) ──────────
    # Default OFF + fail-closed: no external multimodal provider is contacted until
    # global enablement AND a per-vault opt-in AND exact-origin allowlisting AND
    # SSRF safety all hold. Raw evidence is never rewritten; only derived
    # description/search proxies are produced for typed artifacts (image/chart/
    # table/equation) whose typed atoms/assets exist (issue #460).
    multimodal_enrichment_enabled: bool = False
    """Global master switch for multimodal artifact enrichment. Default OFF. This
    enables the pipeline (overall feature is also gated per-vault and by exact-origin
    allowlist + SSRF), it is never sufficient on its own to transmit data."""
    multimodal_allowed_model_origins: Annotated[list[str], NoDecode] = []
    """Comma-separated exact normalized origins (scheme://host:port) allowed to
    receive vault artifact content for multimodal enrichment. Empty (default)
    fails closed -- zero outbound calls."""
    multimodal_chat_url: str = ""
    """OpenAI-compatible multimodal /chat/completions base URL. Empty when disabled.
    SSRF + exact-origin policy guarded before every call."""
    multimodal_model: str = ""
    """Multimodal model name passed to the enrichment endpoint. Empty when disabled."""
    multimodal_mode: str = "thinking"
    """Logical mode label reported in safe provider snapshots/audit (thinking/instant)."""
    multimodal_timeout_seconds: float = 60.0
    """Per-call timeout for a multimodal enrichment request."""
    multimodal_concurrency: int = 2
    """Maximum concurrent multimodal enrichment calls."""
    multimodal_max_assets_per_batch: int = 4
    """Maximum typed assets enriched together in one request batch."""
    multimodal_max_asset_bytes: int = 10485760
    """Per-asset byte cap loaded for enrichment (10 MiB default)."""
    multimodal_max_total_payload_bytes: int = 41943040
    """Total per-request payload byte cap (40 MiB default)."""
    multimodal_max_pixels: int = 4000000
    """Decoded image pixel cap per asset (width*height) for enrichment."""
    multimodal_max_attempts: int = 3
    """Maximum enrichment attempts per artifact before permanent failure (retryable
    timeout/rate/temporary-provider failures back off; policy/schema failures never retry)."""
    multimodal_prompt_version: str = "v1"
    """Version identifier of the enrichment prompt template (part of the fingerprint)."""
    multimodal_schema_version: str = "v1"
    """Version identifier of the derived response schema (part of the fingerprint)."""
    multimodal_impl_version: str = "1"
    """Enrichment implementation/config version (part of the fingerprint)."""
    multimodal_query_vision_enabled: bool = False
    """Query-time vision master switch (issue #462). Default OFF. When ON, selected
    authorized artifact winners MAY be sent to the multimodal provider for
    query-conditioned observations AFTER retrieval/rerank/distill/pack. Always
    additionally gated by the per-vault multimodal provider opt-in, the exact-origin
    allowlist, and SSRF — never sufficient alone to transmit data."""

    draft_max_sections: int = 12
    """Maximum outline sections a compile job may produce."""
    draft_qa_retry_limit: int = 2
    """Number of revisions allowed for each bounded editorial (lint/copy/standards) loop."""
    draft_job_timeout_seconds: int = 1800
    """Total wall-clock budget, in seconds, for one compile job."""
    draft_job_max_model_calls: int = 40
    """Hard per-job cap on model calls across every compile stage."""
    draft_compile_rate_limit: str = "5/minute"
    """Rate limit for Draft Room compile and retry requests, per user."""
    draft_research_retrieval_limit: int = 8
    """Maximum sources retrieved per research facet via rag_engine.retrieve_sources."""
    draft_transient_retry_limit: int = 2
    """Maximum automatic retries for a transient provider/retrieval error inside
    a single compile job, with bounded backoff between attempts."""
    draft_boilerplate_rule_version: str = "1"
    """Default curated-boilerplate rule version applied by the deterministic
    lint stage and recorded on lint findings."""
    draft_lint_rewrite_limit: int = 2
    """Maximum automatic rewrite attempts the lint stage may apply before
    surfacing remaining boilerplate findings unresolved."""
    draft_default_logical_mode: str = "thinking"
    """Default logical model mode ("instant" or "thinking") for Draft Room
    compile stages that do not pin a specific mode."""

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
    embedding_cache_ttl_seconds: int = 604800
    """Time-to-live in seconds for cached embeddings in Redis. Default 7 days."""

    redis_io_timeout_seconds: float = 1.0
    """Bounded timeout for OPTIONAL Redis cache reads/writes on request paths (issue #511): the sync
    client call runs in a worker thread and is abandoned past this timeout, degrading to cache-miss.
    Does not apply to auth-critical Redis use (CSRF)."""
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
    trust_proxy_headers: bool = False
    """When True, trust X-Forwarded-For for client IP (use behind trusted reverse proxy). Default False for security."""

    # Host-header validation (defense-in-depth for reverse-proxy deployments).
    # When non-empty, TrustedHostMiddleware rejects requests whose Host header
    # is not in this list. Leave empty to disable (the default — useful for
    # local dev and root deployments accessed by bare IP).
    allowed_hosts: list[str] = []

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
    """Enable the evaluation endpoints (/eval/heuristic and /eval/live). Disabled by default for production safety."""

    # Admin security
    admin_secret_token: str = (
        ""  # Must be set via environment variable - no default for security
    )

    # Server-side token→scopes mapping for require_scope dependency.
    # Keys are admin tokens; values are lists of scopes authorized for that token.
    # The X-Scopes HTTP header is IGNORED — scopes derive only from this mapping.
    admin_token_scopes: dict[str, list[str]] = {}

    # User authentication
    users_enabled: bool = True
    """Enable multi-user JWT authentication. When False, only admin_secret_token auth is used."""

    jwt_secret_key: str = "change-me-to-a-random-64-char-string"
    """Secret key for JWT signing. MUST be changed in production. Generate with: python -c \"import secrets; print(secrets.token_urlsafe(48))\""""

    jwt_algorithm: str = "HS256"
    """JWT signing algorithm."""

    audit_hmac_key_version: str = "v1"

    # Security settings
    max_file_size_mb: int = 100
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
    } | set(RASTER_IMAGE_EXTENSIONS)

    # Multimodal artifact storage bounds (issue #460). Binary assets are
    # confined per-vault and hardened against decompression bombs / oversize
    # generations. These default to permissive-but-bounded values; tune via env.
    max_asset_pixels: int = 50_000_000
    """Maximum decoded pixel area accepted for a standalone image asset."""
    max_asset_frames: int = 64
    """Maximum frames accepted for animated/multi-frame image assets."""
    max_assets_per_generation: int = 256
    """Maximum number of binary assets persisted per source generation."""
    max_asset_bytes_per_generation: int = 512 * 1024 * 1024
    """Maximum total asset bytes persisted per source generation (512 MB)."""

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

    @field_validator(
        "draft_allowed_model_origins",
        "draft_sensitive_allowed_model_origins",
        "multimodal_allowed_model_origins",
        mode="before",
    )
    @classmethod
    def parse_allowed_model_origins(cls, v):
        """Support JSON lists and comma-separated env values for provider-origin
        allowlists (Draft Room and multimodal), same convention as backend_cors_origins."""
        if isinstance(v, str):
            value = v.strip()
            if not value:
                return []
            if value.startswith("["):
                import json

                parsed = json.loads(value)
                if not isinstance(parsed, list):
                    raise ValueError("origin allowlist JSON value must be a list")
                return parsed
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return v

    @field_validator("allowed_hosts", mode="before")
    @classmethod
    def parse_allowed_hosts(cls, v):
        """Support JSON lists and comma-separated env values for allowed hosts."""
        if isinstance(v, str):
            value = v.strip()
            if not value:
                return []
            if value.startswith("["):
                import json

                parsed = json.loads(value)
                if not isinstance(parsed, list):
                    raise ValueError("allowed_hosts JSON value must be a list")
                return [
                    host.strip()
                    for host in parsed
                    if isinstance(host, str) and host.strip()
                ]
            return [host.strip() for host in value.split(",") if host.strip()]
        return v

    # Migration validators for backward compatibility
    @model_validator(mode="before")
    @classmethod
    def _convert_legacy_fields_at_construction(cls, data):
        """Apply legacy→new conversion BEFORE field validation (issue #494 CONFIG-004).

        The per-field migration validators below (migrate_chunk_size_chars
        etc.) run in field-declaration order, and the legacy fields
        (chunk_size etc.) are declared AFTER the new fields in this model —
        so ``values.data`` never contains the legacy value when those
        validators run, and ``Settings(chunk_size=512)`` used to silently
        yield the DEFAULT chunk_size_chars (2000) instead of the converted
        2048. Running the shared converter (``apply_legacy_settings_conversion``,
        the same implementation the live settings API uses) over the complete
        input dict first makes the conversion work at construction time. The
        pinned per-field validators keep their exact behavior for direct
        invocations and for explicit-value passthrough.
        """
        if isinstance(data, dict):
            return apply_legacy_settings_conversion(data)
        return data

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
    @field_validator("vector_search_concurrency", mode="after")
    @classmethod
    def validate_vector_search_concurrency(cls, v: int) -> int:
        """Validate vector_search_concurrency is in range 1..64."""
        return cls._validate_int_range(v, 1, 64, "vector_search_concurrency")

    @field_validator("search_semaphore_timeout_seconds", mode="after")
    @classmethod
    def validate_search_semaphore_timeout_seconds(cls, v: float) -> float:
        """Validate search semaphore timeout is in range 1.0..300.0."""
        return cls._validate_float_range(v, 1.0, 300.0, "search_semaphore_timeout_seconds")

    @field_validator("write_lock_timeout_seconds", mode="after")
    @classmethod
    def validate_write_lock_timeout_seconds(cls, v: float) -> float:
        """Validate write lock timeout is in range 1.0..300.0."""
        return cls._validate_float_range(v, 1.0, 300.0, "write_lock_timeout_seconds")

    @field_validator("active_user_cache_ttl_seconds", mode="after")
    @classmethod
    def validate_active_user_cache_ttl_seconds(cls, v: int) -> int:
        """Validate active-user cache TTL is within the allowed range 0..300.

        0 disables the in-memory cache entirely; positive values must be
        between 5 and 300 seconds.
        """
        if v == 0:
            return v
        return cls._validate_int_range(v, 5, 300, "active_user_cache_ttl_seconds")

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

    @field_validator("ingestion_queue_max_size", mode="after")
    @classmethod
    def validate_ingestion_queue_max_size(cls, v: int) -> int:
        """Ensure the ingestion queue maxsize is at least 1 (0/negative would be unbounded).

        asyncio.Queue(maxsize=0) creates an unbounded queue per asyncio docs, silently
        negating FR-6 DoS mitigation. This validator enforces v >= 1.
        """
        if v < 1:
            raise ValueError(
                f"ingestion_queue_max_size must be >= 1 (got {v}); "
                "0 or negative values create an unbounded asyncio.Queue"
            )
        return v

    @field_validator("memory_store_pool_size", mode="after")
    @classmethod
    def validate_memory_store_pool_size(cls, v: int) -> int:
        """Ensure the MemoryStore pool size is at least 5.

        Values below 5 bottleneck concurrent memory retrieval. This validator
        enforces v >= 5 per FR-004/FR-008.
        """
        if v < 5:
            raise ValueError(
                f"memory_store_pool_size must be >= 5 (got {v}); "
                "lower values bottleneck concurrent MemoryStore retrieval"
            )
        return v

    @field_validator("db_pool_max_size", mode="after")
    @classmethod
    def validate_db_pool_max_size(cls, v: int) -> int:
        """Ensure the main application DB pool size is at least 1.

        A pool of 0 would make every get_connection() block to exhaustion and
        raise RuntimeError, breaking all DB-backed requests. There is no upper
        bound here (SQLite serializes writers regardless); operators tune this
        against expected concurrent request count.
        """
        if v < 1:
            raise ValueError(
                f"db_pool_max_size must be >= 1 (got {v}); "
                "a zero/negative pool size would exhaust on every checkout"
            )
        return v

    @field_validator("memory_eviction_interval_seconds", mode="after")
    @classmethod
    def validate_memory_eviction_interval_seconds(cls, v: int) -> int:
        """Validate memory_eviction_interval_seconds is >= 1.

        0 or negative would make the periodic eviction loop a tight/instant busy loop.
        """
        return cls._validate_int_range(v, 1, None, "memory_eviction_interval_seconds")

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

    @field_validator("embedding_batch_max_chars", mode="after")
    @classmethod
    def validate_embedding_batch_max_chars(cls, v: int) -> int:
        """Validate embedding batch character budget is in range 4096-1048576."""
        return cls._validate_int_range(v, 4096, 1048576, "embedding_batch_max_chars")

    @field_validator("embedding_cache_max_entries", mode="after")
    @classmethod
    def validate_embedding_cache_max_entries(cls, v: int) -> int:
        """Validate embedding cache capacity is in range 1000-1000000."""
        return cls._validate_int_range(v, 1000, 1000000, "embedding_cache_max_entries")

    @field_validator("near_dup_threshold", mode="after")
    @classmethod
    def validate_near_dup_threshold(cls, v: float) -> float:
        """Validate near-duplicate cosine threshold is in range 0.5-0.999999."""
        return cls._validate_float_range(v, 0.5, 0.999999, "near_dup_threshold")

    @field_validator("orphan_rescan_interval_seconds", mode="after")
    @classmethod
    def validate_orphan_rescan_interval_seconds(cls, v: float) -> float:
        """Validate orphan rescan interval is in range 60.0-86400.0 seconds."""
        return cls._validate_float_range(v, 60.0, 86400.0, "orphan_rescan_interval_seconds")

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

    @field_validator(
        "model_context_tokens",
        "context_distiller_max_sentences",
        mode="after",
    )
    @classmethod
    def validate_issue511_positive_ints(cls, v: int) -> int:
        """Positive-int guards for issue #511 settings. context_distiller_max_sentences<=0
        would disable the O(n^2) dedup input cap, and a non-positive model context
        window is meaningless — fail fast at startup/env-parse instead."""
        if v <= 0:
            raise ValueError("issue #511 int settings must be > 0")
        return v

    @field_validator("prompt_reserve_output_tokens", mode="after")
    @classmethod
    def validate_prompt_reserve_output_tokens(cls, v: int) -> int:
        if v < 0:
            raise ValueError("prompt_reserve_output_tokens must be >= 0")
        return v

    @field_validator("redis_io_timeout_seconds", mode="after")
    @classmethod
    def validate_redis_io_timeout_seconds(cls, v: float) -> float:
        """A value <= 0 makes asyncio.wait_for time out every optional Redis
        cache call instantly, silently degrading embeddings/query-transform
        caching to always-miss — fail fast at startup instead."""
        if v <= 0:
            raise ValueError("redis_io_timeout_seconds must be > 0")
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

    @field_validator("draft_default_logical_mode", mode="after")
    @classmethod
    def validate_draft_default_logical_mode(cls, v: str) -> str:
        """Validate draft_default_logical_mode is 'instant' or 'thinking'."""
        return cls._validate_enum(v, {"instant", "thinking"}, "draft_default_logical_mode")

    @field_validator("multimodal_mode", mode="after")
    @classmethod
    def validate_multimodal_mode(cls, v: str) -> str:
        """Validate multimodal_mode is 'instant' or 'thinking'.

        The mode is persisted into provider_snapshot_json, so an arbitrary operator
        string would otherwise flow unnormalized into the audit/snapshot record.
        """
        return cls._validate_enum(v, {"instant", "thinking"}, "multimodal_mode")

    @field_validator(
        "draft_max_sections",
        "draft_job_timeout_seconds",
        "draft_job_max_model_calls",
        "draft_research_retrieval_limit",
        mode="after",
    )
    @classmethod
    def validate_draft_positive_ints(cls, v: int) -> int:
        """Validate Draft Room pipeline budget/limit settings are >= 1."""
        return cls._validate_int_range(v, 1, None, "draft pipeline setting")

    @field_validator(
        "draft_qa_retry_limit",
        "draft_transient_retry_limit",
        "draft_lint_rewrite_limit",
        mode="after",
    )
    @classmethod
    def validate_draft_nonnegative_ints(cls, v: int) -> int:
        """Validate Draft Room retry/loop cap settings are >= 0."""
        return cls._validate_int_range(v, 0, None, "draft pipeline setting")

    @field_validator(
        "instant_initial_retrieval_top_k",
        "instant_reranker_top_n",
        "instant_memory_context_top_k",
        "instant_max_tokens",
        "thinking_max_tokens",
        mode="after",
    )
    @classmethod
    def validate_per_mode_positive_ints(cls, v: int) -> int:
        """Validate per-chat-mode budgets (instant + thinking) from env/defaults
        before startup."""
        if v < 1:
            raise ValueError("per-mode numeric settings must be >= 1")
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
    def _build_admin_token_scopes_default(self) -> "Settings":
        """Seed admin_token_scopes from admin_secret_token when the field is empty.

        This provides a working default for deployments that set ADMIN_SECRET_TOKEN
        but don't explicitly configure admin_token_scopes. The key is the actual
        configured token value, which is available after env loading.
        """
        if not self.admin_token_scopes and self.admin_secret_token:
            self.admin_token_scopes = {self.admin_secret_token: ["admin:config"]}
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

    def vault_artifacts_dir(self, vault_id: int) -> Path:
        """Confined per-vault artifact root for extracted binary assets.

        Assets are stored beneath this root and represented only by opaque IDs
        outside the storage layer; their validated relative paths are resolved
        against this root on every read/delete (issue #460).
        """
        path = self.vault_dir(vault_id) / "artifacts"
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
