import type { SettingsResponse, HealthResponse, LlmModeHealth, ConnectionTestResult } from "@/lib/api";
import type { HealthStatus } from "@/types/health";

export const mockSettings: SettingsResponse = import.meta.env.DEV ? ({
  port: 8000,
  data_dir: "/var/lib/ragapp/data",
  ollama_embedding_url: "http://localhost:11434",
  ollama_chat_url: "http://localhost:11434",
  embedding_model: "nomic-embed-text",
  chat_model: "llama3:70b",
  instant_chat_url: "http://localhost:1234/v1",
  instant_chat_model: "local-model",
  default_chat_mode: "thinking",
  instant_initial_retrieval_top_k: 20,
  instant_reranker_top_n: 5,
  instant_memory_context_top_k: 5,
  instant_max_tokens: 4096,
  chunk_size_chars: 1000,
  chunk_overlap_chars: 200,
  retrieval_top_k: 8,
  max_distance_threshold: 0.75,
  retrieval_window: 3,
  vector_metric: "cosine",
  embedding_doc_prefix: "search_document: ",
  embedding_query_prefix: "search_query: ",
  maintenance_mode: false,
  auto_scan_enabled: true,
  auto_scan_interval_minutes: 60,
  enable_model_validation: true,
  embedding_batch_size: 32,
  reranking_enabled: true,
  reranker_url: "http://localhost:11434",
  reranker_model: "cross-encoder",
  initial_retrieval_top_k: 20,
  reranker_top_n: 5,
  hybrid_search_enabled: true,
  hybrid_alpha: 0.7,
  wiki_enabled: true,
  wiki_compile_on_ingest: true,
  wiki_compile_on_query: false,
  wiki_compile_after_indexing: true,
  wiki_lint_enabled: true,
  wiki_llm_curator_enabled: false,
  wiki_llm_curator_url: "",
  wiki_llm_curator_model: "",
  wiki_llm_curator_temperature: 0.3,
  wiki_llm_curator_max_input_chars: 8000,
  wiki_llm_curator_max_output_tokens: 1024,
  wiki_llm_curator_timeout_sec: 60,
  wiki_llm_curator_concurrency: 2,
  wiki_llm_curator_mode: "draft",
  wiki_llm_curator_require_quote_match: true,
  wiki_llm_curator_require_chunk_id: true,
  wiki_llm_curator_run_on_ingest: false,
  wiki_llm_curator_run_on_query: false,
  wiki_llm_curator_run_on_manual: true,
  effective_sources: {
    port: "default",
    embedding_model: "env",
    chat_model: "kv",
    chunk_size_chars: "default",
  },
  max_file_size_mb: 50,
  allowed_extensions: [".pdf", ".docx", ".md", ".txt", ".html", ".csv", ".json", ".sql"],
  backend_cors_origins: ["http://localhost:5173", "http://localhost:3000"],
}) : ({} as SettingsResponse);

export const mockHealthStatus: HealthStatus = import.meta.env.DEV ? ({
  backend: true,
  embeddings: true,
  chat: true,
  loading: false,
  lastChecked: new Date("2024-05-07T10:00:00Z"),
}) : ({} as HealthStatus);

export const mockHealthResponse: HealthResponse = import.meta.env.DEV ? ({
  status: "healthy",
  version: "0.9.2",
  timestamp: "2024-05-07T10:00:00Z",
  services: {
    backend: true,
    embeddings: true,
    chat: true,
  },
}) : ({} as HealthResponse);

export const mockLlmModeHealth: LlmModeHealth = import.meta.env.DEV ? ({
  thinking: true,
  instant: false,
}) : ({} as LlmModeHealth);

export const mockConnectionResult: ConnectionTestResult = import.meta.env.DEV ? ({
  embeddings: { url: "http://localhost:11434", status: 200, ok: true },
  chat: { url: "http://localhost:11434", status: 200, ok: true },
}) : ({} as ConnectionTestResult);
