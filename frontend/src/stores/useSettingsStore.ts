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
  instant_initial_retrieval_top_k: number;
  instant_reranker_top_n: number;
  instant_memory_context_top_k: number;
  instant_max_tokens: number;
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
  instant_initial_retrieval_top_k: "models",
  instant_reranker_top_n: "models",
  instant_memory_context_top_k: "models",
  instant_max_tokens: "models",
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
  setErrors: (errors: SettingsErrors) => void;
  setSaveStatus: (status: "idle" | "success" | "error") => void;

  initializeForm: (settings: SettingsResponse) => void;

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
  instant_initial_retrieval_top_k: 10,
  instant_reranker_top_n: 4,
  instant_memory_context_top_k: 2,
  instant_max_tokens: 4096,
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
    instant_initial_retrieval_top_k:
      settings.instant_initial_retrieval_top_k ?? 10,
    instant_reranker_top_n: settings.instant_reranker_top_n ?? 4,
    instant_memory_context_top_k: settings.instant_memory_context_top_k ?? 2,
    instant_max_tokens: settings.instant_max_tokens ?? 4096,
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
  };
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
    set((state) => ({
      formData: { ...state.formData, [field]: value },
      saveStatus: "idle",
    }));
  },

  setLoading: (loading) => set({ loading }),
  setSaving: (saving) => set({ saving }),
  setError: (error) => set({ error }),
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

  validateForm: () => {
    const { formData } = get();
    const newErrors: SettingsErrors = {};

    if (formData.chunk_size_chars <= 0) {
      newErrors.chunk_size_chars = "Chunk size must be a positive integer";
    }
    if (formData.chunk_overlap_chars < 0) {
      newErrors.chunk_overlap_chars = "Chunk overlap must be a non-negative integer";
    }
    if (formData.retrieval_top_k <= 0) {
      newErrors.retrieval_top_k = "Retrieval top-k must be a positive integer";
    }
    if (formData.auto_scan_interval_minutes <= 0) {
      newErrors.auto_scan_interval_minutes =
        "Scan interval must be a positive integer";
    }
    if (
      formData.embedding_batch_size < 1 ||
      formData.embedding_batch_size > 128
    ) {
      newErrors.embedding_batch_size =
        "Embedding batch size must be between 1 and 128";
    }
    if (formData.chunk_overlap_chars >= formData.chunk_size_chars) {
      newErrors.chunk_overlap_chars =
        "Chunk overlap must be less than chunk size";
    }
    if (
      formData.max_distance_threshold < 0 ||
      formData.max_distance_threshold > 1
    ) {
      newErrors.max_distance_threshold =
        "Distance threshold must be between 0 and 1";
    }
    if (formData.retrieval_window < 0 || formData.retrieval_window > 3) {
      newErrors.retrieval_window = "Retrieval window must be between 0 and 3";
    }
    const validMetrics = ["cosine", "euclidean", "dot_product"];
    if (!validMetrics.includes(formData.vector_metric)) {
      newErrors.vector_metric =
        "Vector metric must be cosine, euclidean, or dot_product";
    }
    if (
      formData.initial_retrieval_top_k !== undefined &&
      (formData.initial_retrieval_top_k < 5 ||
        formData.initial_retrieval_top_k > 100)
    ) {
      newErrors.initial_retrieval_top_k =
        "Initial retrieval top-k must be between 5 and 100";
    }
    if (
      formData.reranker_top_n !== undefined &&
      (formData.reranker_top_n < 1 || formData.reranker_top_n > 20)
    ) {
      newErrors.reranker_top_n = "Reranker top-n must be between 1 and 20";
    }
    if (
      formData.hybrid_alpha !== undefined &&
      (formData.hybrid_alpha < 0 || formData.hybrid_alpha > 1)
    ) {
      newErrors.hybrid_alpha = "Hybrid alpha must be between 0 and 1";
    }
    if (
      formData.ollama_embedding_url &&
      !/^https?:\/\//.test(formData.ollama_embedding_url)
    ) {
      newErrors.ollama_embedding_url = "URL must start with http:// or https://";
    }
    if (
      formData.ollama_chat_url &&
      !/^https?:\/\//.test(formData.ollama_chat_url)
    ) {
      newErrors.ollama_chat_url = "URL must start with http:// or https://";
    }
    if (
      formData.instant_chat_url &&
      !/^https?:\/\//.test(formData.instant_chat_url)
    ) {
      newErrors.instant_chat_url = "URL must start with http:// or https://";
    }
    if (
      formData.default_chat_mode === "instant" &&
      !formData.instant_chat_model.trim()
    ) {
      newErrors.instant_chat_model = "Instant chat model is required";
    }
    if (!["instant", "thinking"].includes(formData.default_chat_mode)) {
      newErrors.default_chat_mode =
        "Default chat mode must be instant or thinking";
    }
    if (
      formData.instant_initial_retrieval_top_k <= 0 ||
      !Number.isInteger(formData.instant_initial_retrieval_top_k)
    ) {
      newErrors.instant_initial_retrieval_top_k =
        "Instant initial retrieval top-k must be a positive integer";
    }
    if (
      formData.instant_reranker_top_n <= 0 ||
      !Number.isInteger(formData.instant_reranker_top_n)
    ) {
      newErrors.instant_reranker_top_n =
        "Instant reranker top-n must be a positive integer";
    }
    if (
      formData.instant_memory_context_top_k <= 0 ||
      !Number.isInteger(formData.instant_memory_context_top_k)
    ) {
      newErrors.instant_memory_context_top_k =
        "Instant memory context top-k must be a positive integer";
    }
    if (
      formData.instant_max_tokens <= 0 ||
      !Number.isInteger(formData.instant_max_tokens)
    ) {
      newErrors.instant_max_tokens =
        "Instant max tokens must be a positive integer";
    }

    // Curator: required-when-enabled (frontend mirror of backend invariant).
    if (formData.wiki_llm_curator_enabled) {
      if (!formData.wiki_llm_curator_url.trim()) {
        newErrors.wiki_llm_curator_url =
          "Curator URL is required when curator is enabled";
      } else if (!/^https?:\/\//.test(formData.wiki_llm_curator_url)) {
        newErrors.wiki_llm_curator_url =
          "Curator URL must start with http:// or https://";
      }
      if (!formData.wiki_llm_curator_model.trim()) {
        newErrors.wiki_llm_curator_model =
          "Curator model is required when curator is enabled";
      }
    }
    if (
      formData.wiki_llm_curator_temperature < 0 ||
      formData.wiki_llm_curator_temperature > 1
    ) {
      newErrors.wiki_llm_curator_temperature =
        "Temperature must be between 0.0 and 1.0";
    }
    if (
      formData.wiki_llm_curator_max_input_chars < 1000 ||
      formData.wiki_llm_curator_max_input_chars > 24000
    ) {
      newErrors.wiki_llm_curator_max_input_chars =
        "Max input chars must be between 1000 and 24000";
    }
    if (
      formData.wiki_llm_curator_timeout_sec < 10 ||
      formData.wiki_llm_curator_timeout_sec > 600
    ) {
      newErrors.wiki_llm_curator_timeout_sec =
        "Timeout must be between 10 and 600 seconds";
    }
    if (
      formData.wiki_llm_curator_concurrency < 1 ||
      formData.wiki_llm_curator_concurrency > 4
    ) {
      newErrors.wiki_llm_curator_concurrency =
        "Concurrency must be between 1 and 4";
    }
    if (
      !["draft", "active_if_verified"].includes(formData.wiki_llm_curator_mode)
    ) {
      newErrors.wiki_llm_curator_mode =
        "Mode must be 'draft' or 'active_if_verified'";
    }

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
