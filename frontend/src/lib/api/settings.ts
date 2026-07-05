import { apiClient } from "./core";

export interface SettingsResponse {
  // Server config
  port: number;
  data_dir: string;

  // Ollama config
  ollama_embedding_url: string;
  ollama_chat_url: string;

  // Model config
  embedding_model: string;
  chat_model: string;

  // Instant mode (LM Studio)
  instant_chat_url?: string;
  instant_chat_model?: string;
  default_chat_mode?: 'instant' | 'thinking';
  ingestion_llm_mode?: 'instant' | 'thinking' | 'disabled';
  instant_initial_retrieval_top_k?: number;
  instant_reranker_top_n?: number;
  instant_memory_context_top_k?: number;
  instant_max_tokens?: number;

  // Document processing (character-based)
  chunk_size_chars: number;
  chunk_overlap_chars: number;
  retrieval_top_k: number;

  // RAG config
  max_distance_threshold: number;
  retrieval_window: number;
  vector_metric: string;

  // Embedding prefixes
  embedding_doc_prefix: string;
  embedding_query_prefix: string;

  // Feature flags
  maintenance_mode: boolean;
  auto_scan_enabled: boolean;
  auto_scan_interval_minutes: number;
  enable_model_validation: boolean;

  // Embedding batch size
  embedding_batch_size: number;

  // Retrieval settings
  reranking_enabled?: boolean;
  reranker_url?: string;
  reranker_model?: string;
  initial_retrieval_top_k?: number;
  reranker_top_n?: number;
  hybrid_search_enabled?: boolean;
  hybrid_alpha?: number;

  // Wiki / Knowledge Compiler config (PR B)
  wiki_enabled?: boolean;
  wiki_compile_on_ingest?: boolean;
  wiki_compile_on_query?: boolean;
  wiki_compile_after_indexing?: boolean;
  wiki_lint_enabled?: boolean;

  // KMS / Knowledge Management config
  kms_enabled?: boolean;
  kms_compile_on_ingest?: boolean;

  // Optional LLM Wiki Curator config (PR B persists, PR C wires)
  wiki_llm_curator_enabled?: boolean;
  wiki_llm_curator_url?: string;
  wiki_llm_curator_model?: string;
  wiki_llm_curator_temperature?: number;
  wiki_llm_curator_max_input_chars?: number;
  wiki_llm_curator_max_output_tokens?: number;
  wiki_llm_curator_timeout_sec?: number;
  wiki_llm_curator_concurrency?: number;
  wiki_llm_curator_mode?: string;
  wiki_llm_curator_require_quote_match?: boolean;
  wiki_llm_curator_require_chunk_id?: boolean;
  wiki_llm_curator_run_on_ingest?: boolean;
  wiki_llm_curator_run_on_query?: boolean;
  wiki_llm_curator_run_on_manual?: boolean;

  /**
   * Per-field source map: which precedence level produced the
   * effective runtime value. Reflects actual lifespan order
   * (kv > env > default). Models tab uses this to label inputs
   * without disabling them on env presence.
   */
  effective_sources?: Record<string, "kv" | "env" | "default">;

  // Limits
  max_file_size_mb: number;
  allowed_extensions: string[];

  // CORS
  backend_cors_origins: string[];
}

export interface UpdateSettingsRequest {
  chunk_size_chars?: number;
  chunk_overlap_chars?: number;
  retrieval_top_k?: number;
  auto_scan_enabled?: boolean;
  auto_scan_interval_minutes?: number;
  max_distance_threshold?: number;
  retrieval_window?: number;
  vector_metric?: string;
  embedding_doc_prefix?: string;
  embedding_query_prefix?: string;
  embedding_batch_size?: number;
  // Retrieval settings
  reranking_enabled?: boolean;
  reranker_url?: string;
  reranker_model?: string;
  initial_retrieval_top_k?: number;
  reranker_top_n?: number;
  hybrid_search_enabled?: boolean;
  hybrid_alpha?: number;
  // Model connection settings
  ollama_embedding_url?: string;
  ollama_chat_url?: string;
  embedding_model?: string;
  chat_model?: string;
  // Instant mode (LM Studio)
  instant_chat_url?: string;
  instant_chat_model?: string;
  default_chat_mode?: 'instant' | 'thinking';
  ingestion_llm_mode?: 'instant' | 'thinking' | 'disabled';
  instant_initial_retrieval_top_k?: number;
  instant_reranker_top_n?: number;
  instant_memory_context_top_k?: number;
  instant_max_tokens?: number;
  // Wiki / Knowledge Compiler config
  wiki_enabled?: boolean;
  wiki_compile_on_ingest?: boolean;
  wiki_compile_on_query?: boolean;
  wiki_compile_after_indexing?: boolean;
  wiki_lint_enabled?: boolean;
  // KMS / Knowledge Management config
  kms_enabled?: boolean;
  kms_compile_on_ingest?: boolean;
  // Optional LLM Wiki Curator config
  wiki_llm_curator_enabled?: boolean;
  wiki_llm_curator_url?: string;
  wiki_llm_curator_model?: string;
  wiki_llm_curator_temperature?: number;
  wiki_llm_curator_max_input_chars?: number;
  wiki_llm_curator_max_output_tokens?: number;
  wiki_llm_curator_timeout_sec?: number;
  wiki_llm_curator_concurrency?: number;
  wiki_llm_curator_mode?: string;
  wiki_llm_curator_require_quote_match?: boolean;
  wiki_llm_curator_require_chunk_id?: boolean;
  wiki_llm_curator_run_on_ingest?: boolean;
  wiki_llm_curator_run_on_query?: boolean;
  wiki_llm_curator_run_on_manual?: boolean;
}

export interface CuratorTestResult {
  ok: boolean;
  model: string;
  latency_ms: number | null;
  error: string | null;
}

export async function getSettings(): Promise<SettingsResponse> {
  const response = await apiClient.get<SettingsResponse>("/settings");
  return response.data;
}

export async function testCuratorConnection(
  url?: string,
  model?: string,
): Promise<CuratorTestResult> {
  const body: Record<string, string> = {};
  if (url !== undefined) body.url = url;
  if (model !== undefined) body.model = model;
  const response = await apiClient.post<CuratorTestResult>(
    "/settings/curator/test",
    body,
  );
  return response.data;
}

export async function updateSettings(
  request: UpdateSettingsRequest
): Promise<SettingsResponse> {
  const response = await apiClient.put<SettingsResponse>("/settings", request);
  return response.data;
}
