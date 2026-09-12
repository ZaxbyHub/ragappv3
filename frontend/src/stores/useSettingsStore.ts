import { create } from "zustand";
import type { SettingsResponse } from "@/lib/api";

/**
 * Settings store — phase-aware (PR B).
 *
 * Form fields are read/written by the new SettingsPage tabs. Numeric inputs
 * use draft-string state in the UI: an empty string is a transient
 * "user is editing" state, not the value zero. The store only sees the
 * coerced value once the user blurs / saves, so blanks never accidentally
 * become zero in the persisted payload.
 *
 * Dirty tracking compares the live ``formData`` against a snapshot taken
 * at load time (``loadedFormData``). ``discard()`` restores the snapshot.
 * ``dirtyByTab`` powers the SaveDiscardFooter dot indicators.
 */
export type SettingsTab =
  | "overview"
  | "models"
  | "documents"
  | "retrieval"
  | "wiki"
  | "maintenance";

export interface SettingsFormData {
  // Document processing
  chunk_size_chars: number;
  chunk_overlap_chars: number;
  retrieval_top_k: number;
  auto_scan_enabled: boolean;
  auto_scan_interval_minutes: number;
  // Retrieval
  max_distance_threshold: number;
  retrieval_window: number;
  vector_metric: string;
  embedding_doc_prefix: string;
  embedding_query_prefix: string;
  embedding_batch_size: number;
  reranking_enabled: boolean;
  reranker_url: string;
  reranker_model: string;
  initial_retrieval_top_k: number;
  reranker_top_n: number;
  hybrid_search_enabled: boolean;
  hybrid_alpha: number;
  // Models tab
  ollama_embedding_url: string;
  ollama_chat_url: string;
  embedding_model: string;
  chat_model: string;
  // Instant mode (LM Studio on local GPU)
  instant_chat_url: string;
  instant_chat_model: string;
  default_chat_mode: 'instant' | 'thinking';
  ingestion_llm_mode: 'instant' | 'thinking' | 'disabled';
  instant_initial_retrieval_top_k: number;
  instant_reranker_top_n: number;
  instant_memory_context_top_k: number;
  instant_max_tokens: number;
  thinking_max_tokens: number;
  // Wiki & curator (PR B + PR C)
  wiki_enabled: boolean;
  wiki_compile_on_ingest: boolean;
  wiki_compile_on_query: boolean;
  wiki_compile_after_indexing: boolean;
  wiki_lint_enabled: boolean;
  wiki_llm_curator_enabled: boolean;
  wiki_llm_curator_url: string;
  wiki_llm_curator_model: string;
  wiki_llm_curator_temperature: number;
  wiki_llm_curator_max_input_chars: number;
  wiki_llm_curator_max_output_tokens: number;
  wiki_llm_curator_timeout_sec: number;
  wiki_llm_curator_concurrency: number;
  wiki_llm_curator_mode: string;
  wiki_llm_curator_require_quote_match: boolean;
  wiki_llm_curator_require_chunk_id: boolean;
  wiki_llm_curator_run_on_ingest: boolean;
  wiki_llm_curator_run_on_query: boolean;
  wiki_llm_curator_run_on_manual: boolean;
  // KMS / Knowledge Management config
  kms_enabled: boolean;
  kms_compile_on_ingest: boolean;
  // Draft Room
  draft_room_enabled: boolean;
  // Multimodal artifact enrichment (issue #461)
  multimodal_enrichment_enabled: boolean;
  multimodal_query_vision_enabled: boolean;
  multimodal_allowed_model_origins: string[];
  multimodal_chat_url: string;
  multimodal_model: string;
  multimodal_mode: "thinking" | "instant";
  multimodal_timeout_seconds: number;
  multimodal_concurrency: number;
  multimodal_max_assets_per_batch: number;
  multimodal_max_asset_bytes: number;
  multimodal_max_total_payload_bytes: number;
  multimodal_max_pixels: number;
  multimodal_max_attempts: number;
  multimodal_prompt_version: string;
  multimodal_schema_version: string;
  multimodal_impl_version: string;
}

export type SettingsErrors = Partial<Record<keyof SettingsFormData, string>>;

/**
 * Mapping of each form field to its tab. Used by ``dirtyByTab`` so the
 * SaveDiscardFooter can show a dot per tab with unsaved changes.
 */
export const FIELD_TAB: Record<keyof SettingsFormData, SettingsTab> = {
  chunk_size_chars: "documents",
  chunk_overlap_chars: "documents",
  auto_scan_enabled: "documents",
  auto_scan_interval_minutes: "documents",
  embedding_doc_prefix: "documents",
  embedding_query_prefix: "documents",
  embedding_batch_size: "documents",
  retrieval_top_k: "retrieval",
  max_distance_threshold: "retrieval",
  retrieval_window: "retrieval",
  vector_metric: "retrieval",
  reranking_enabled: "retrieval",
  reranker_url: "retrieval",
  reranker_model: "retrieval",
  initial_retrieval_top_k: "retrieval",
  reranker_top_n: "retrieval",
  hybrid_search_enabled: "retrieval",
  hybrid_alpha: "retrieval",
  ollama_embedding_url: "models",
  ollama_chat_url: "models",
  embedding_model: "models",
  chat_model: "models",
  instant_chat_url: "models",
  instant_chat_model: "models",
  default_chat_mode: "models",
  ingestion_llm_mode: "models",
  instant_initial_retrieval_top_k: "models",
  instant_reranker_top_n: "models",
  instant_memory_context_top_k: "models",
  instant_max_tokens: "models",
  thinking_max_tokens: "models",
  wiki_enabled: "wiki",
  wiki_compile_on_ingest: "wiki",
  wiki_compile_on_query: "wiki",
  wiki_compile_after_indexing: "wiki",
  wiki_lint_enabled: "wiki",
  wiki_llm_curator_enabled: "wiki",
  wiki_llm_curator_url: "wiki",
  wiki_llm_curator_model: "wiki",
  wiki_llm_curator_temperature: "wiki",
  wiki_llm_curator_max_input_chars: "wiki",
  wiki_llm_curator_max_output_tokens: "wiki",
  wiki_llm_curator_timeout_sec: "wiki",
  wiki_llm_curator_concurrency: "wiki",
  wiki_llm_curator_mode: "wiki",
  wiki_llm_curator_require_quote_match: "wiki",
  wiki_llm_curator_require_chunk_id: "wiki",
  wiki_llm_curator_run_on_ingest: "wiki",
  wiki_llm_curator_run_on_query: "wiki",
  wiki_llm_curator_run_on_manual: "wiki",
  kms_enabled: "maintenance",
  kms_compile_on_ingest: "maintenance",
  draft_room_enabled: "maintenance",
  multimodal_enrichment_enabled: "models",
  multimodal_query_vision_enabled: "models",
  multimodal_allowed_model_origins: "models",
  multimodal_chat_url: "models",
  multimodal_model: "models",
  multimodal_mode: "models",
  multimodal_timeout_seconds: "models",
  multimodal_concurrency: "models",
  multimodal_max_assets_per_batch: "models",
  multimodal_max_asset_bytes: "models",
  multimodal_max_total_payload_bytes: "models",
  multimodal_max_pixels: "models",
  multimodal_max_attempts: "models",
  multimodal_prompt_version: "models",
  multimodal_schema_version: "models",
  multimodal_impl_version: "models",
};

// Fields that invalidate existing embeddings when changed — reindex required.
export const REINDEX_REQUIRED_FIELDS = new Set<keyof SettingsFormData>([
  "embedding_model",
  "vector_metric",
  "chunk_size_chars",
  "chunk_overlap_chars",
  "embedding_doc_prefix",
  "embedding_query_prefix",
]);

export interface SettingsState {
  settings: SettingsResponse | null;
  formData: SettingsFormData;
  /** Snapshot taken at load time; restored by ``discard()``. */
  loadedFormData: SettingsFormData;

  loading: boolean;
  saving: boolean;
  error: string | null;
  errors: SettingsErrors;
  saveStatus: "idle" | "success" | "error";

  reindexRequired: boolean;
  setReindexRequired: (value: boolean) => void;
  checkReindexRequired: () => boolean;

  setSettings: (settings: SettingsResponse | null) => void;
  setFormData: (
    formData: SettingsFormData | ((prev: SettingsFormData) => SettingsFormData)
  ) => void;
  updateFormField: <K extends keyof SettingsFormData>(
    field: K,
    value: SettingsFormData[K]
  ) => void;
  setLoading: (loading: boolean) => void;
  setSaving: (saving: boolean) => void;
  setError: (error: string | null) => void;
  /** Failed initial load: records the error AND clears ``loading`` so the
   * skeleton does not render forever (UI-029). */
  setLoadError: (error: string) => void;
  setErrors: (errors: SettingsErrors) => void;
  setSaveStatus: (status: "idle" | "success" | "error") => void;

  initializeForm: (settings: SettingsResponse) => void;
  /** Post-save re-initialization that preserves edits made while the save
   * request was in flight (UI-031): fields whose live value moved on from
   * ``submitSnapshot`` are re-applied over the server snapshot so they
   * stay in ``formData`` and remain dirty. */
  initializeFormAfterSave: (
    settings: SettingsResponse,
    submitSnapshot: SettingsFormData,
  ) => void;

  validateForm: () => boolean;

  hasChanges: () => boolean;
  /** Set of currently-dirty field names. */
  dirtyFields: () => Set<keyof SettingsFormData>;
  /** Per-tab dirty count, used by SaveDiscardFooter dots. */
  dirtyByTab: () => Record<SettingsTab, number>;
  /** Restore form to the last-loaded snapshot. */
  discard: () => void;

  resetState: () => void;
}

const defaultFormData: SettingsFormData = {
  chunk_size_chars: 2000,
  chunk_overlap_chars: 200,
  retrieval_top_k: 5,
  auto_scan_enabled: false,
  auto_scan_interval_minutes: 60,
  max_distance_threshold: 0.7,
  retrieval_window: 1,
  vector_metric: "cosine",
  embedding_doc_prefix: "Passage: ",
  embedding_query_prefix: "Query: ",
  embedding_batch_size: 64,
  reranking_enabled: false,
  reranker_url: "",
  reranker_model: "",
  initial_retrieval_top_k: 20,
  reranker_top_n: 5,
  hybrid_search_enabled: false,
  hybrid_alpha: 0.5,
  ollama_embedding_url: "",
  ollama_chat_url: "",
  embedding_model: "",
  chat_model: "",
  instant_chat_url: "",
  instant_chat_model: "",
  default_chat_mode: "thinking",
  ingestion_llm_mode: "instant",
  instant_initial_retrieval_top_k: 10,
  instant_reranker_top_n: 4,
  instant_memory_context_top_k: 2,
  instant_max_tokens: 4096,
  thinking_max_tokens: 32768,
  wiki_enabled: true,
  wiki_compile_on_ingest: true,
  wiki_compile_on_query: true,
  wiki_compile_after_indexing: true,
  wiki_lint_enabled: true,
  wiki_llm_curator_enabled: false,
  wiki_llm_curator_url: "",
  wiki_llm_curator_model: "",
  wiki_llm_curator_temperature: 0.0,
  wiki_llm_curator_max_input_chars: 6000,
  wiki_llm_curator_max_output_tokens: 2048,
  wiki_llm_curator_timeout_sec: 120,
  wiki_llm_curator_concurrency: 1,
  wiki_llm_curator_mode: "draft",
  wiki_llm_curator_require_quote_match: true,
  wiki_llm_curator_require_chunk_id: true,
  wiki_llm_curator_run_on_ingest: true,
  wiki_llm_curator_run_on_query: false,
  wiki_llm_curator_run_on_manual: true,
  kms_enabled: true,
  kms_compile_on_ingest: true,
  draft_room_enabled: false,
  multimodal_enrichment_enabled: false,
  multimodal_query_vision_enabled: false,
  multimodal_allowed_model_origins: [],
  multimodal_chat_url: "",
  multimodal_model: "",
  multimodal_mode: "thinking",
  multimodal_timeout_seconds: 60,
  multimodal_concurrency: 2,
  multimodal_max_assets_per_batch: 4,
  multimodal_max_asset_bytes: 10 * 1024 * 1024,
  multimodal_max_total_payload_bytes: 40 * 1024 * 1024,
  multimodal_max_pixels: 4_000_000,
  multimodal_max_attempts: 3,
  multimodal_prompt_version: "v1",
  multimodal_schema_version: "v1",
  multimodal_impl_version: "1",
};

// Unwrap legacy json.dumps-encoded strings ('"x"' -> 'x').
function decodeStr(v: string | null | undefined, fallback: string): string {
  if (v == null) return fallback;
  if (v.length >= 2 && v.startsWith('"') && v.endsWith('"')) {
    try {
      const parsed = JSON.parse(v);
      if (typeof parsed === "string") return parsed;
    } catch {
      /* not JSON */
    }
  }
  return v;
}

function fromSettings(settings: SettingsResponse): SettingsFormData {
  const validMetrics = ["cosine", "euclidean", "dot_product"];
  const rawMetric = decodeStr(settings.vector_metric, "cosine");
  const validModes = ["draft", "active_if_verified"];
  const curatorMode = settings.wiki_llm_curator_mode ?? "draft";
  return {
    chunk_size_chars: settings.chunk_size_chars ?? 2000,
    chunk_overlap_chars: settings.chunk_overlap_chars ?? 200,
    retrieval_top_k: settings.retrieval_top_k ?? 5,
    auto_scan_enabled: settings.auto_scan_enabled ?? false,
    auto_scan_interval_minutes: settings.auto_scan_interval_minutes ?? 60,
    max_distance_threshold: settings.max_distance_threshold ?? 0.7,
    retrieval_window: settings.retrieval_window ?? 1,
    vector_metric: validMetrics.includes(rawMetric) ? rawMetric : "cosine",
    embedding_doc_prefix: decodeStr(settings.embedding_doc_prefix, ""),
    embedding_query_prefix: decodeStr(settings.embedding_query_prefix, ""),
    embedding_batch_size: settings.embedding_batch_size ?? 64,
    reranking_enabled: settings.reranking_enabled ?? false,
    reranker_url: decodeStr(settings.reranker_url, ""),
    reranker_model: decodeStr(settings.reranker_model, ""),
    initial_retrieval_top_k: settings.initial_retrieval_top_k ?? 20,
    reranker_top_n: settings.reranker_top_n ?? 5,
    hybrid_search_enabled: settings.hybrid_search_enabled ?? false,
    hybrid_alpha: settings.hybrid_alpha ?? 0.5,
    ollama_embedding_url: decodeStr(settings.ollama_embedding_url, ""),
    ollama_chat_url: decodeStr(settings.ollama_chat_url, ""),
    embedding_model: decodeStr(settings.embedding_model, ""),
    chat_model: decodeStr(settings.chat_model, ""),
    instant_chat_url: decodeStr(settings.instant_chat_url ?? "", ""),
    instant_chat_model: decodeStr(settings.instant_chat_model ?? "", ""),
    default_chat_mode:
      settings.default_chat_mode === "instant" ? "instant" : "thinking",
    ingestion_llm_mode:
      settings.ingestion_llm_mode === "thinking" ||
      settings.ingestion_llm_mode === "disabled"
        ? settings.ingestion_llm_mode
        : "instant",
    instant_initial_retrieval_top_k:
      settings.instant_initial_retrieval_top_k ?? 10,
    instant_reranker_top_n: settings.instant_reranker_top_n ?? 4,
    instant_memory_context_top_k: settings.instant_memory_context_top_k ?? 2,
    instant_max_tokens: settings.instant_max_tokens ?? 4096,
    thinking_max_tokens: settings.thinking_max_tokens ?? 32768,
    wiki_enabled: settings.wiki_enabled ?? true,
    wiki_compile_on_ingest: settings.wiki_compile_on_ingest ?? true,
    wiki_compile_on_query: settings.wiki_compile_on_query ?? true,
    wiki_compile_after_indexing: settings.wiki_compile_after_indexing ?? true,
    wiki_lint_enabled: settings.wiki_lint_enabled ?? true,
    wiki_llm_curator_enabled: settings.wiki_llm_curator_enabled ?? false,
    wiki_llm_curator_url: decodeStr(settings.wiki_llm_curator_url ?? "", ""),
    wiki_llm_curator_model: decodeStr(settings.wiki_llm_curator_model ?? "", ""),
    wiki_llm_curator_temperature: settings.wiki_llm_curator_temperature ?? 0.0,
    wiki_llm_curator_max_input_chars:
      settings.wiki_llm_curator_max_input_chars ?? 6000,
    wiki_llm_curator_max_output_tokens:
      settings.wiki_llm_curator_max_output_tokens ?? 2048,
    wiki_llm_curator_timeout_sec: settings.wiki_llm_curator_timeout_sec ?? 120,
    wiki_llm_curator_concurrency: settings.wiki_llm_curator_concurrency ?? 1,
    wiki_llm_curator_mode: validModes.includes(curatorMode) ? curatorMode : "draft",
    wiki_llm_curator_require_quote_match:
      settings.wiki_llm_curator_require_quote_match ?? true,
    wiki_llm_curator_require_chunk_id:
      settings.wiki_llm_curator_require_chunk_id ?? true,
    wiki_llm_curator_run_on_ingest:
      settings.wiki_llm_curator_run_on_ingest ?? true,
    wiki_llm_curator_run_on_query: settings.wiki_llm_curator_run_on_query ?? false,
    wiki_llm_curator_run_on_manual:
      settings.wiki_llm_curator_run_on_manual ?? true,
    kms_enabled: settings.kms_enabled ?? true,
    kms_compile_on_ingest: settings.kms_compile_on_ingest ?? true,
    draft_room_enabled: settings.draft_room_enabled ?? false,
    multimodal_enrichment_enabled: settings.multimodal_enrichment_enabled ?? false,
    multimodal_query_vision_enabled:
      settings.multimodal_query_vision_enabled ?? false,
    multimodal_allowed_model_origins: Array.isArray(
      settings.multimodal_allowed_model_origins,
    )
      ? settings.multimodal_allowed_model_origins
      : [],
    multimodal_chat_url: decodeStr(settings.multimodal_chat_url ?? "", ""),
    multimodal_model: decodeStr(settings.multimodal_model ?? "", ""),
    multimodal_mode:
      settings.multimodal_mode === "instant" ? "instant" : "thinking",
    multimodal_timeout_seconds: settings.multimodal_timeout_seconds ?? 60,
    multimodal_concurrency: settings.multimodal_concurrency ?? 2,
    multimodal_max_assets_per_batch:
      settings.multimodal_max_assets_per_batch ?? 4,
    multimodal_max_asset_bytes: settings.multimodal_max_asset_bytes ?? 10 * 1024 * 1024,
    multimodal_max_total_payload_bytes:
      settings.multimodal_max_total_payload_bytes ?? 40 * 1024 * 1024,
    multimodal_max_pixels: settings.multimodal_max_pixels ?? 4_000_000,
    multimodal_max_attempts: settings.multimodal_max_attempts ?? 3,
    multimodal_prompt_version: settings.multimodal_prompt_version ?? "v1",
    multimodal_schema_version: settings.multimodal_schema_version ?? "v1",
    multimodal_impl_version: settings.multimodal_impl_version ?? "1",
  };
}

/**
 * Per-field validators shared by ``validateForm`` (submit time) and
 * ``updateFormField``'s error re-sync (change time), so change-time and
 * submit-time validation cannot diverge (UI-030). A validator returns its
 * error message, or undefined when the field (and every rule that writes
 * to it) passes. Cross-field rules live in the validator of the field the
 * error is reported on (e.g. chunk_overlap_chars reads chunk_size_chars).
 */
const FIELD_VALIDATORS: Partial<
  Record<keyof SettingsFormData, (data: SettingsFormData) => string | undefined>
> = {
  chunk_size_chars: (d) =>
    d.chunk_size_chars <= 0
      ? "Chunk size must be a positive integer"
      : undefined,
  chunk_overlap_chars: (d) => {
    if (d.chunk_overlap_chars < 0)
      return "Chunk overlap must be a non-negative integer";
    if (d.chunk_overlap_chars >= d.chunk_size_chars)
      return "Chunk overlap must be less than chunk size";
    return undefined;
  },
  retrieval_top_k: (d) =>
    d.retrieval_top_k <= 0
      ? "Retrieval top-k must be a positive integer"
      : undefined,
  auto_scan_interval_minutes: (d) =>
    d.auto_scan_interval_minutes <= 0
      ? "Scan interval must be a positive integer"
      : undefined,
  embedding_batch_size: (d) =>
    d.embedding_batch_size < 1 || d.embedding_batch_size > 128
      ? "Embedding batch size must be between 1 and 128"
      : undefined,
  max_distance_threshold: (d) =>
    d.max_distance_threshold < 0 || d.max_distance_threshold > 1
      ? "Distance threshold must be between 0 and 1"
      : undefined,
  retrieval_window: (d) =>
    d.retrieval_window < 0 || d.retrieval_window > 3
      ? "Retrieval window must be between 0 and 3"
      : undefined,
  vector_metric: (d) =>
    ["cosine", "euclidean", "dot_product"].includes(d.vector_metric)
      ? undefined
      : "Vector metric must be cosine, euclidean, or dot_product",
  initial_retrieval_top_k: (d) =>
    d.initial_retrieval_top_k !== undefined &&
    (d.initial_retrieval_top_k < 5 || d.initial_retrieval_top_k > 100)
      ? "Initial retrieval top-k must be between 5 and 100"
      : undefined,
  reranker_top_n: (d) =>
    d.reranker_top_n !== undefined &&
    (d.reranker_top_n < 1 || d.reranker_top_n > 20)
      ? "Reranker top-n must be between 1 and 20"
      : undefined,
  hybrid_alpha: (d) =>
    d.hybrid_alpha !== undefined &&
    (d.hybrid_alpha < 0 || d.hybrid_alpha > 1)
      ? "Hybrid alpha must be between 0 and 1"
      : undefined,
  ollama_embedding_url: (d) =>
    d.ollama_embedding_url && !/^https?:\/\//.test(d.ollama_embedding_url)
      ? "URL must start with http:// or https://"
      : undefined,
  ollama_chat_url: (d) =>
    d.ollama_chat_url && !/^https?:\/\//.test(d.ollama_chat_url)
      ? "URL must start with http:// or https://"
      : undefined,
  instant_chat_url: (d) =>
    d.instant_chat_url && !/^https?:\/\//.test(d.instant_chat_url)
      ? "URL must start with http:// or https://"
      : undefined,
  instant_chat_model: (d) =>
    d.default_chat_mode === "instant" && !d.instant_chat_model.trim()
      ? "Instant chat model is required"
      : undefined,
  default_chat_mode: (d) =>
    ["instant", "thinking"].includes(d.default_chat_mode)
      ? undefined
      : "Default chat mode must be instant or thinking",
  ingestion_llm_mode: (d) =>
    ["instant", "thinking", "disabled"].includes(d.ingestion_llm_mode)
      ? undefined
      : "Ingestion LLM mode must be instant, thinking, or disabled",
  instant_initial_retrieval_top_k: (d) =>
    d.instant_initial_retrieval_top_k <= 0 ||
    !Number.isInteger(d.instant_initial_retrieval_top_k)
      ? "Instant initial retrieval top-k must be a positive integer"
      : undefined,
  instant_reranker_top_n: (d) =>
    d.instant_reranker_top_n <= 0 ||
    !Number.isInteger(d.instant_reranker_top_n)
      ? "Instant reranker top-n must be a positive integer"
      : undefined,
  instant_memory_context_top_k: (d) =>
    d.instant_memory_context_top_k <= 0 ||
    !Number.isInteger(d.instant_memory_context_top_k)
      ? "Instant memory context top-k must be a positive integer"
      : undefined,
  instant_max_tokens: (d) =>
    d.instant_max_tokens <= 0 || !Number.isInteger(d.instant_max_tokens)
      ? "Instant max tokens must be a positive integer"
      : undefined,
  thinking_max_tokens: (d) =>
    d.thinking_max_tokens <= 0 || !Number.isInteger(d.thinking_max_tokens)
      ? "Thinking max tokens must be a positive integer"
      : undefined,
  wiki_llm_curator_url: (d) => {
    if (!d.wiki_llm_curator_enabled) return undefined;
    if (!d.wiki_llm_curator_url.trim())
      return "Curator URL is required when curator is enabled";
    if (!/^https?:\/\//.test(d.wiki_llm_curator_url))
      return "Curator URL must start with http:// or https://";
    return undefined;
  },
  wiki_llm_curator_model: (d) =>
    d.wiki_llm_curator_enabled && !d.wiki_llm_curator_model.trim()
      ? "Curator model is required when curator is enabled"
      : undefined,
  wiki_llm_curator_temperature: (d) =>
    d.wiki_llm_curator_temperature < 0 ||
    d.wiki_llm_curator_temperature > 1
      ? "Temperature must be between 0.0 and 1.0"
      : undefined,
  wiki_llm_curator_max_input_chars: (d) =>
    d.wiki_llm_curator_max_input_chars < 1000 ||
    d.wiki_llm_curator_max_input_chars > 24000
      ? "Max input chars must be between 1000 and 24000"
      : undefined,
  wiki_llm_curator_timeout_sec: (d) =>
    d.wiki_llm_curator_timeout_sec < 10 || d.wiki_llm_curator_timeout_sec > 600
      ? "Timeout must be between 10 and 600 seconds"
      : undefined,
  wiki_llm_curator_concurrency: (d) =>
    d.wiki_llm_curator_concurrency < 1 || d.wiki_llm_curator_concurrency > 4
      ? "Concurrency must be between 1 and 4"
      : undefined,
  wiki_llm_curator_mode: (d) =>
    ["draft", "active_if_verified"].includes(d.wiki_llm_curator_mode)
      ? undefined
      : "Mode must be 'draft' or 'active_if_verified'",
};

/**
 * Cross-field validation dependencies: a changed form field mapped to the
 * OTHER fields whose validators read it. Editing either side of a
 * cross-field rule re-syncs the rule's error entry (UI-030).
 */
const VALIDATION_DEPENDENTS: Partial<
  Record<keyof SettingsFormData, ReadonlyArray<keyof SettingsFormData>>
> = {
  chunk_size_chars: ["chunk_overlap_chars"],
  default_chat_mode: ["instant_chat_model"],
  wiki_llm_curator_enabled: ["wiki_llm_curator_url", "wiki_llm_curator_model"],
};

/**
 * Re-syncs validation errors after ``field`` changed. Only fields that
 * already carry an error entry (the changed field itself plus its
 * cross-field dependents) are recomputed: errors are planted by a failed
 * submit (``validateForm``) and must clear or refresh as the user edits,
 * but a fresh edit does not error until the next submit attempt — stale
 * errors can no longer keep Save disabled (UI-030).
 */
function resyncFieldErrors(
  errors: SettingsErrors,
  formData: SettingsFormData,
  field: keyof SettingsFormData,
): SettingsErrors {
  const targets: ReadonlyArray<keyof SettingsFormData> = [
    field,
    ...(VALIDATION_DEPENDENTS[field] ?? []),
  ];
  if (!targets.some((t) => t in errors)) return errors;
  const next = { ...errors };
  targets.forEach((t) => {
    if (!(t in next)) return;
    const message = FIELD_VALIDATORS[t]?.(formData);
    if (message) next[t] = message;
    else delete next[t];
  });
  return next;
}

export const useSettingsStore = create<SettingsState>((set, get) => ({
  settings: null,
  formData: { ...defaultFormData },
  loadedFormData: { ...defaultFormData },
  loading: true,
  saving: false,
  error: null,
  errors: {},
  saveStatus: "idle",
  reindexRequired: false,

  setReindexRequired: (value) => set({ reindexRequired: value }),

  checkReindexRequired: () => {
    const { settings, formData } = get();
    if (!settings) return false;
    for (const field of REINDEX_REQUIRED_FIELDS) {
      const current = formData[field];
      let saved: unknown;
      if (field === "embedding_model")
        saved = decodeStr(settings.embedding_model, "");
      else if (field === "vector_metric")
        saved = decodeStr(settings.vector_metric, "cosine");
      else if (field === "embedding_doc_prefix")
        saved = decodeStr(settings.embedding_doc_prefix, "");
      else if (field === "embedding_query_prefix")
        saved = decodeStr(settings.embedding_query_prefix, "");
      else saved = (settings as unknown as Record<string, unknown>)[field];
      if (current !== saved) return true;
    }
    return false;
  },

  setSettings: (settings) => set({ settings }),

  setFormData: (formData) => {
    if (typeof formData === "function") {
      set((state) => ({ formData: formData(state.formData) }));
    } else {
      set({ formData });
    }
  },

  updateFormField: (field, value) => {
    set((state) => {
      const formData = { ...state.formData, [field]: value };
      return {
        formData,
        // Re-sync live validation for the changed field (and its cross-field
        // dependents) so a failed submit's errors clear/refresh on edit
        // instead of sticking until Discard (UI-030).
        errors: resyncFieldErrors(state.errors, formData, field),
        saveStatus: "idle",
      };
    });
  },

  setLoading: (loading) => set({ loading }),
  setSaving: (saving) => set({ saving }),
  setError: (error) => set({ error }),
  setLoadError: (error) => set({ error, loading: false }),
  setErrors: (errors) => set({ errors }),
  setSaveStatus: (saveStatus) => set({ saveStatus }),

  initializeForm: (settings) => {
    const next = fromSettings(settings);
    set({
      formData: next,
      loadedFormData: next,
      loading: false,
      error: null,
    });
  },

  initializeFormAfterSave: (settings, submitSnapshot) => {
    const serverSnapshot = fromSettings(settings);
    const live = get().formData;
    const next = { ...serverSnapshot };
    (Object.keys(live) as Array<keyof SettingsFormData>).forEach((k) => {
      // The live value moved on from what was submitted — an edit made
      // while the save was in flight. Re-apply it over the server
      // snapshot so it survives and stays dirty (UI-031). (Same indexed
      // write cast as SettingsPage's pickDirtyPayload — all fields are
      // scalar, so the value type is preserved.)
      if (live[k] !== submitSnapshot[k]) {
        (next as Record<string, unknown>)[k] = live[k];
      }
    });
    set({
      formData: next,
      loadedFormData: serverSnapshot,
      loading: false,
      error: null,
    });
  },

  validateForm: () => {
    const { formData } = get();
    const newErrors: SettingsErrors = {};
    (Object.keys(FIELD_VALIDATORS) as Array<keyof SettingsFormData>).forEach(
      (field) => {
        const message = FIELD_VALIDATORS[field]?.(formData);
        if (message) newErrors[field] = message;
      },
    );
    set({ errors: newErrors });
    return Object.keys(newErrors).length === 0;
  },

  hasChanges: () => {
    const { dirtyFields } = get();
    return dirtyFields().size > 0;
  },

  dirtyFields: () => {
    const { formData, loadedFormData } = get();
    const dirty = new Set<keyof SettingsFormData>();
    (Object.keys(formData) as Array<keyof SettingsFormData>).forEach((k) => {
      // Direct equality is fine; all fields are scalar.
      if (formData[k] !== loadedFormData[k]) {
        dirty.add(k);
      }
    });
    return dirty;
  },

  dirtyByTab: () => {
    const dirty = get().dirtyFields();
    const out: Record<SettingsTab, number> = {
      overview: 0,
      models: 0,
      documents: 0,
      retrieval: 0,
      wiki: 0,
      maintenance: 0,
    };
    dirty.forEach((field) => {
      const tab = FIELD_TAB[field];
      if (tab) out[tab] += 1;
    });
    return out;
  },

  discard: () => {
    set((state) => ({
      formData: { ...state.loadedFormData },
      errors: {},
      saveStatus: "idle",
    }));
  },

  resetState: () => {
    set({
      settings: null,
      formData: { ...defaultFormData },
      loadedFormData: { ...defaultFormData },
      loading: true,
      saving: false,
      error: null,
      errors: {},
      saveStatus: "idle",
      reindexRequired: false,
    });
  },
}));
