import axios, { AxiosRequestHeaders } from "axios";
import { setChatHistory as storageSetChatHistory, getChatHistory as storageGetChatHistory } from "./storage";
import { appPath } from "./paths";

export const API_BASE_URL = import.meta.env.VITE_API_URL || appPath("/api");
console.info("[KnowledgeVault] API_BASE_URL:", API_BASE_URL);

// Module-level JWT token holder - persisted via useAuthStore persist middleware
let _jwtAccessToken: string | null = null;

export function setJwtAccessToken(token: string | null): void {
  _jwtAccessToken = token;
}

export function getJwtAccessToken(): string | null {
  return _jwtAccessToken;
}

// Read CSRF token from the non-httpOnly cookie set by the server
function getCsrfCookie(): string | null {
  const match = document.cookie
    .split('; ')
    .find(row => row.startsWith('X-CSRF-Token='));
  // Use split with limit=2 so token values containing '=' (base64 padding) are preserved
  return match ? decodeURIComponent(match.split('=', 2)[1]) : null;
}

// CSRF token cache and deduplication — single source of truth
let _csrfToken: string | null = null;
let _csrfFetchPromise: Promise<string> | null = null;

export function resetCsrfToken(): void {
  _csrfToken = null;
  _csrfFetchPromise = null;
}

/**
 * Get the cached CSRF token.
 * @internal Internal use only - prefer ensureCsrfToken() for actual usage
 */
export function getCsrfToken(): string | null {
  return _csrfToken;
}

export async function ensureCsrfToken(): Promise<string> {
  if (_csrfToken) return _csrfToken;

  // Check cookie first
  const cookieToken = getCsrfCookie();
  if (cookieToken) {
    _csrfToken = cookieToken;
    return cookieToken;
  }

  if (!_csrfFetchPromise) {
    const newPromise: Promise<string> = fetch(`${API_BASE_URL}/csrf-token`, { credentials: "include" })
      .then(async (resp) => {
        if (!resp.ok) throw new Error("Failed to fetch CSRF token");
        const data = await resp.json();
        if (!data.csrf_token || typeof data.csrf_token !== "string") {
          throw new Error("CSRF token missing from response");
        }
        const token: string = data.csrf_token;
        _csrfToken = token;
        return token;
      });
    _csrfFetchPromise = newPromise;
    newPromise
      .catch(() => {
        // Mark rejection as handled to prevent unhandled rejection warnings in test environments
        // Callers will handle the actual error when they await the promise
      })
      .finally(() => {
        _csrfFetchPromise = null;
      });
  }
  return _csrfFetchPromise as Promise<string>;
}

export function attachCsrfInterceptor(instance: ReturnType<typeof axios.create>): void {
  // Request interceptor: attach CSRF to mutating requests
  instance.interceptors.request.use(async (config) => {
    if (config.method && ["post", "put", "patch", "delete"].includes(config.method.toLowerCase())) {
      const token = await ensureCsrfToken();
      if (token) {
        if (!config.headers) {
          config.headers = {} as AxiosRequestHeaders;
        }
        config.headers["X-CSRF-Token"] = token;
      }
    }
    return config;
  });

  // Response interceptor: on CSRF-specific 403, clear cached token and retry once
  instance.interceptors.response.use(
    (resp) => resp,
    async (error) => {
      const config = error.config;
      const detail = error.response?.data?.detail || "";
      const isCsrfError = error.response?.status === 403 && (
        error.response?.headers?.["x-csrf-error"] === "true" ||
        (typeof detail === "string" && detail.toLowerCase().includes("csrf"))
      );
      if (isCsrfError && config && !config._csrfRetry) {
        resetCsrfToken(); // force refresh on next request
        config._csrfRetry = true;
        const newToken = await ensureCsrfToken();
        if (!config.headers) {
          config.headers = {};
        }
        config.headers["X-CSRF-Token"] = newToken;
        return instance(config);
      }
      return Promise.reject(error);
    }
  );
}

export function loginRedirectPath(): string {
  return appPath("/login");
}

export function redirectToLogin(): void {
  const loginPath = loginRedirectPath();
  if (window.location.pathname !== loginPath) {
    window.location.href = loginPath;
  }
}

// Singleton refresh promise — ensures only one /auth/refresh call is in flight
// at a time. Concurrent 401s share the same promise so the refresh cookie is
// not rotated twice (which would invalidate the second caller's session).
let _refreshInFlight: Promise<string | null> | null = null;

// Standalone refresh function to avoid circular dependencies
export async function refreshAccessToken(): Promise<string | null> {
  if (_refreshInFlight) {
    return _refreshInFlight;
  }
  _refreshInFlight = _doRefresh().finally(() => {
    _refreshInFlight = null;
  });
  return _refreshInFlight;
}

async function _doRefresh(): Promise<string | null> {
  try {
    // The /auth/refresh endpoint requires the CSRF token.
    // Read it from the non-httpOnly cookie; if missing, fetch a fresh one.
    let csrfToken = getCsrfCookie();
    if (!csrfToken) {
      try {
        const csrfResp = await fetch(`${API_BASE_URL}/csrf-token`, { credentials: "include" });
        if (csrfResp.ok) {
          const csrfData = await csrfResp.json();
          csrfToken = csrfData.csrf_token ?? null;
        }
      } catch {
        // proceed without CSRF — server will reject if required
      }
    }

    const headers: Record<string, string> = {};
    if (csrfToken) {
      headers["X-CSRF-Token"] = csrfToken;
    }

    const response = await fetch(`${API_BASE_URL}/auth/refresh`, {
      method: "POST",
      credentials: "include", // Send httpOnly cookie with refresh token
      headers,
    });
    if (!response.ok) return null;
    const data = await response.json();
    _jwtAccessToken = data.access_token;
    return data.access_token;
  } catch {
    return null;
  }
}

const apiClient = axios.create({
  baseURL: API_BASE_URL,
  timeout: 30000,
  headers: {
    "Content-Type": "application/json",
  },
});

// Attach JWT authentication token to all apiClient requests
apiClient.interceptors.request.use((config) => {
  if (_jwtAccessToken) {
    config.headers.Authorization = `Bearer ${_jwtAccessToken}`;
  }
  return config;
});

// Attach CSRF protection for all mutating requests on apiClient
attachCsrfInterceptor(apiClient);

// Parse JWT token to extract expiry timestamp (exp claim)
function getTokenExpiry(token: string): number | null {
  try {
    const parts = token.split('.');
    if (parts.length !== 3) return null;
    const payload = JSON.parse(atob(parts[1]));
    return payload.exp ? payload.exp * 1000 : null; // Convert to milliseconds
  } catch {
    return null;
  }
}

// Check if token is expired or close to expiring (within 1 minute)
function isTokenNearExpiry(token: string, bufferMs: number = 60000): boolean {
  const expiry = getTokenExpiry(token);
  if (!expiry) return false;
  return Date.now() + bufferMs >= expiry;
}

// Normalize error responses
apiClient.interceptors.response.use(
  (response) => response,
  async (error) => {
    // Preserve AbortError for cancellation handling
    if (error.name === "AbortError" || error.code === "ERR_CANCELED") {
      return Promise.reject(error);
    }

    // Handle 401 Unauthorized — attempt silent token refresh for expired JWTs
    if (error.response?.status === 401) {
      const detail = error.response?.data?.detail;
      const isTokenInvalid = typeof detail === "string" && (
        detail.includes("token_invalid") || detail.includes("user_inactive")
      );

      if (_jwtAccessToken && !isTokenInvalid) {
        // Token may be refreshable — retry with exponential backoff
        const retryCount = (error.config._retryCount || 0) as number;
        const maxRetries = 2;
        const delays = [1000, 2000]; // 1s, 2s

        if (retryCount < maxRetries) {
          error.config._retryCount = retryCount + 1;

          try {
            // Wait before retrying (exponential backoff)
            await new Promise((resolve) => setTimeout(resolve, delays[retryCount] || 2000));

            const newToken = await refreshAccessToken();
            if (newToken) {
              error.config.headers.Authorization = `Bearer ${newToken}`;
              return apiClient(error.config);
            }
          } catch {
            // Refresh failed — fall through to logout
          }
        }
      }

      // Clear auth state and redirect to login
      _jwtAccessToken = null;
      redirectToLogin();
    }

    // Extract the most useful error message
    let message = "An unexpected error occurred";
    
    if (error.response) {
      // Server responded with an error status
      const data = error.response.data;
      message = data?.detail || data?.message || data?.error || error.response.statusText || message;
    } else if (error.request) {
      // Request was made but no response received
      message = "Unable to reach the server. Please check your connection.";
    } else {
      // Something else happened
      message = error.message || message;
    }

    // Create a normalized error with the extracted message
    const normalizedError = new Error(message);
    normalizedError.name = error.name || "APIError";
    // Preserve the original response for status code checking
    (normalizedError as any).status = error.response?.status;
    (normalizedError as any).originalError = error;
    
    return Promise.reject(normalizedError);
  }
);

export interface HealthResponse {
  status: string;
  version?: string;
  timestamp?: string;
  services?: {
    backend: boolean;
    embeddings: boolean;
    chat: boolean;
  };
}

export interface ConnectionCheck {
  url: string;
  status: number | null;
  ok: boolean;
  error?: string;
}

export interface ConnectionTestResult {
  embeddings: ConnectionCheck;
  chat: ConnectionCheck;
}

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

export interface SearchMemoriesRequest {
  query: string;
  limit?: number;
  filter?: Record<string, unknown>;
}

export interface MemoryResult {
  id: string;
  content: string;
  metadata?: Record<string, unknown>;
  score?: number;
}

export interface AddMemoryRequest {
  content: string;
  category?: string;
  tags?: string[];
  source?: string;
}

export interface AddMemoryResponse {
  id: string;
  status: string;
}

export interface SearchMemoriesResponse {
  results: MemoryResult[];
  total: number;
}

export interface Tag {
  id: number;
  vault_id: number;
  name: string;
  color: string;
  created_at: string;
  updated_at: string;
  document_count: number;
}

export interface Document {
  id: string;
  filename: string;
  vault_id?: number | null;
  content_type?: string;
  size?: number;
  created_at?: string;
  processed_at?: string | null;
  error_message?: string | null;
  phase?: string | null;
  phase_message?: string | null;
  progress_percent?: number | null;
  processed_units?: number | null;
  total_units?: number | null;
  unit_label?: string | null;
  phase_started_at?: string | null;
  processing_started_at?: string | null;
  enrichment_status?: "pending" | "processing" | "complete" | "error" | string | null;
  enrichment_error?: string | null;
  metadata?: Record<string, unknown>;
  tags?: Tag[];
  folder_id?: number | null;
}

export interface Folder {
  id: number;
  vault_id: number;
  parent_folder_id: number | null;
  name: string;
  description: string;
  created_at: string;
  updated_at: string;
  document_count: number;
}

export type DocumentSortBy = "created_at" | "file_name" | "file_size" | "status";
export type SortOrder = "asc" | "desc";

export interface ListDocumentsOptions {
  vaultId?: number;
  search?: string;
  status?: string;
  page?: number;
  perPage?: number;
  sortBy?: DocumentSortBy;
  sortOrder?: SortOrder;
  tagId?: number;
  folderId?: number;
}

export interface ListDocumentsResponse {
  documents: Document[];
  total: number;
}

export interface UploadDocumentResponse {
  id: string;
  filename: string;
  status: string;
}

/**
 * Phase-aware status payload returned by GET /documents/{id}/status.
 *
 * `status` stays in the canonical 4-value enum
 * ("pending" | "processing" | "indexed" | "error"). Async lifecycle detail
 * (queued / parsing / extracting_text / chunking / embedding / writing_index)
 * lives in `phase`. `wiki_status` is derived server-side from the latest
 * wiki_compile_jobs row for this file (or "pending" when the processor has
 * signalled intent but the job row hasn't appeared yet).
 */
export interface DocumentStatusResponse {
  id: number;
  filename: string;
  status: string;
  chunk_count: number;
  error_message?: string | null;
  processed_at?: string | null;
  /** Granular pipeline phase. May be null for very old rows / fresh installs. */
  phase?: string | null;
  phase_message?: string | null;
  progress_percent?: number | null;
  processed_units?: number | null;
  total_units?: number | null;
  unit_label?: string | null;
  phase_started_at?: string | null;
  processing_started_at?: string | null;
  /** Server-computed seconds since processing_started_at; null when not started. */
  elapsed_seconds?: number | null;
  /** "pending" | "running" | "completed" | "failed" | "cancelled" | null */
  wiki_status?: string | null;
  wiki_phase?: string | null;
  wiki_job_id?: number | null;
  enrichment_status?: "pending" | "processing" | "complete" | "error" | string | null;
  enrichment_error?: string | null;
}

export interface DocumentStatsResponse {
  total_documents: number;
  total_chunks: number;
  total_size_bytes: number;
  documents_by_status: Record<string, number>;
}

export interface ScanDocumentsResponse {
  scanned: number;
  added: number;
  errors: string[];
}

export interface ChatMessage {
  role: "user" | "assistant" | "system";
  content: string;
}

export interface Source {
  id: string;
  file_id?: string;
  filename: string;
  section?: string;
  source_label?: string;
  evidence_type?: "primary" | "supporting";
  page_number?: number | null;
  snippet?: string;
  score?: number;
  score_type?: "distance" | "rerank" | "rrf";
  metadata?: Record<string, unknown>;
}

export interface ChunkContextResponse {
  id: string;
  file_id: string;
  filename: string;
  chunk_index: number | string;
  chunk_text: string;
  context_text: string;
  context_source: "parent_window" | "raw_text" | "chunk" | string;
}

/**
 * A memory the assistant referenced when generating a response.
 * Distinct from document sources: memories use the [M#] label space and
 * represent durable user context (preferences, prior facts) rather than
 * retrieved documents.
 */
export interface UsedMemory {
  id: string;
  /** Stable label like "M1", "M2" — matches the [M#] cited in answer text. */
  memory_label: string;
  content: string;
  category?: string | null;
  tags?: string | null;
  source?: string | null;
  vault_id?: number | null;
  score?: number | null;
  score_type?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface CitationValidationDebug {
  valid: string[];
  invalid: string[];
  uncited_factual_warning: boolean;
  has_evidence: boolean;
}

/**
 * A wiki knowledge entry cited as [W#] in an assistant response.
 * Mirrors WikiEvidence.to_dict() from the backend.
 */
export interface WikiReference {
  /** Stable label like "W1", "W2" — matches the [W#] cited in answer text. */
  wiki_label: string;
  page_id: number | null;
  claim_id: number | null;
  title: string;
  slug: string | null;
  page_type: string | null;
  claim_text: string | null;
  excerpt: string | null;
  confidence: number;
  /** Combined status: claim_status takes precedence over page_status. */
  status: string | null;
  page_status: string | null;
  claim_status: string | null;
  score: number;
  score_type: string | null;
  source_count: number;
  provenance_summary: string;
}

/**
 * A user-curated knowledge base entry cited as [K#] in an assistant response.
 * Mirrors KMSEvidence.to_dict() from the backend.
 */
export interface KMSReference {
  /** Stable label like "K1", "K2" — matches the [K#] cited in answer text. */
  kms_label: string;
  entry_id: number;
  slug: string | null;
  title: string;
  summary: string | null;
  excerpt: string | null;
  tags: string[];
  status: string | null;
  source_type: string | null;
  file_id: number | null;
  score: number;
  score_type: string | null;
}

export interface ChatStreamCallbacks {
  onMessage: (chunk: string) => void;
  onSources?: (sources: Source[]) => void;
  onMemories?: (memories: UsedMemory[]) => void;
  onWiki?: (wikiRefs: WikiReference[]) => void;
  onKMS?: (kmsRefs: KMSReference[]) => void;
  onCitationValidation?: (validation: CitationValidationDebug) => void;
  /**
   * Canonical, citation-repaired content sent on the `done` event when the
   * backend stripped invalid citations. Fires before onComplete so the message
   * content can be reconciled before it is persisted.
   */
  onFinalContent?: (content: string) => void;
  /** Resolved chat mode reported by the backend at the start of the stream. */
  onMode?: (mode: "instant" | "thinking") => void;
  onError?: (error: Error) => void;
  onComplete?: () => void;
}

export interface ChatHistoryItem {
  id: string;
  title: string;
  lastActive: string;
  messageCount: number;
  messages: Array<{ id: string; role: string; content: string; sources?: Source[] }>;
}

export interface ChatSession {
  id: number;
  vault_id: number;
  title: string | null;
  created_at: string;
  updated_at: string;
  message_count?: number;
  forked_from_session_id?: number | null;
  fork_message_index?: number | null;
}

export interface ChatSessionMessage {
  id: number;
  role: string;
  content: string;
  sources: Source[] | null;
  /** Memories used to generate this assistant message. May be null on legacy rows. */
  memories?: UsedMemory[] | null;
  /** Wiki evidence cited as [W#] in this assistant message. Null on legacy rows. */
  wiki_refs?: WikiReference[] | null;
  /** KMS evidence cited as [K#] in this assistant message. Null on legacy rows. */
  kms_refs?: KMSReference[] | null;
  created_at: string;
  feedback?: "up" | "down" | null;
  /** Chat mode used to generate this assistant message. Null on user rows / legacy data. */
  mode?: "instant" | "thinking" | null;
}

export interface ChatSessionDetail extends ChatSession {
  messages: ChatSessionMessage[];
}

export interface CreateSessionRequest {
  title?: string;
  vault_id: number;
}

export interface AddMessageRequest {
  role: string;
  content: string;
  sources?: Source[];
  memories?: UsedMemory[];
  wiki_refs?: WikiReference[];
  kms_refs?: KMSReference[];
  mode?: "instant" | "thinking";
}

export interface Organization {
  id: number;
  name: string;
  description: string;
  slug?: string;
  member_count?: number;
  vault_count?: number;
  group_count?: number;
  created_at?: string;
}

export async function listOrganizations(): Promise<Organization[]> {
  const response = await apiClient.get<{ organizations: Organization[] } | Organization[]>("/organizations/");
  const data = response.data;
  return Array.isArray(data) ? data : (data.organizations ?? []);
}

export interface Vault {
  id: number;
  name: string;
  description: string;
  created_at: string;
  updated_at: string;
  file_count: number;
  memory_count: number;
  session_count: number;
  org_id: number | null;
  current_user_permission?: "read" | "write" | "admin" | null;
}

export interface VaultListResponse {
  vaults: Vault[];
}

export interface VaultCreateRequest {
  name: string;
  description?: string;
  org_id?: number | null;
}

export interface VaultUpdateRequest {
  name?: string;
  description?: string;
}

export async function getHealth(): Promise<HealthResponse> {
  const response = await apiClient.get<HealthResponse>("/health");
  return response.data;
}

export interface LlmModeHealth {
  thinking: boolean;
  instant: boolean;
}

export async function getLlmModeHealth(): Promise<LlmModeHealth> {
  const response = await apiClient.get<LlmModeHealth>("/llm-health/modes");
  return response.data;
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

export async function testConnections(): Promise<ConnectionTestResult> {
  const response = await apiClient.get<ConnectionTestResult>("/settings/connection");
  return response.data;
}

export async function listVaults(): Promise<VaultListResponse> {
  const response = await apiClient.get<VaultListResponse>("/vaults");
  return response.data;
}

export async function listAccessibleVaults(): Promise<VaultListResponse> {
  const response = await apiClient.get<VaultListResponse>("/vaults/accessible");
  return response.data;
}

export async function getVault(id: number): Promise<Vault> {
  const response = await apiClient.get<Vault>(`/vaults/${id}`);
  return response.data;
}

export async function createVault(request: VaultCreateRequest): Promise<Vault> {
  const response = await apiClient.post<Vault>("/vaults", request);
  return response.data;
}

export async function updateVault(id: number, request: VaultUpdateRequest): Promise<Vault> {
  const response = await apiClient.put<Vault>(`/vaults/${id}`, request);
  return response.data;
}

export async function deleteVault(id: number): Promise<void> {
  await apiClient.delete(`/vaults/${id}`);
}

export async function searchMemories(
  request: SearchMemoriesRequest,
  signal?: AbortSignal,
  vaultId?: number
): Promise<SearchMemoriesResponse> {
  const body = { ...request, ...(vaultId != null && { vault_id: vaultId }) };
  const response = await apiClient.post<SearchMemoriesResponse>(
    "/memories/search",
    body,
    { signal }
  );
  return response.data;
}

export async function addMemory(
  request: AddMemoryRequest,
  vaultId?: number
): Promise<AddMemoryResponse> {
  // Ensure tags is always an array, never undefined
  const payload = {
    ...request,
    tags: request.tags ?? [],
    ...(vaultId != null && { vault_id: vaultId }),
  };
  const response = await apiClient.post<AddMemoryResponse>("/memories", payload);
  return response.data;
}

export async function deleteMemory(id: string): Promise<void> {
  await apiClient.delete(`/memories/${id}`);
}

export interface UpdateMemoryRequest {
  content?: string;
  category?: string;
  tags?: string;
  source?: string;
}

export async function updateMemory(id: string, request: UpdateMemoryRequest): Promise<MemoryResult> {
  const response = await apiClient.put<MemoryResult>(`/memories/${id}`, request);
  return response.data;
}

export async function listMemories(vaultId?: number): Promise<{ memories: MemoryResult[] }> {
  const response = await apiClient.get<{ memories: MemoryResult[] }>(
    "/memories",
    vaultId != null ? { params: { vault_id: vaultId } } : undefined
  );
  return response.data;
}

export async function listDocuments(options: ListDocumentsOptions = {}): Promise<ListDocumentsResponse> {
  const { vaultId, search, status, page, perPage, sortBy, sortOrder, tagId, folderId } = options;
  const params: Record<string, unknown> = {};
  if (vaultId != null) params.vault_id = vaultId;
  if (search && search.trim()) params.search = search.trim();
  if (status && status.trim()) params.status = status.trim();
  if (page != null) params.page = page;
  if (perPage != null) params.per_page = perPage;
  if (sortBy) params.sort_by = sortBy;
  if (sortOrder) params.sort_order = sortOrder;
  if (tagId != null) params.tag_id = tagId;
  if (folderId != null) params.folder_id = folderId;
  const response = await apiClient.get<ListDocumentsResponse>("/documents", { params });
  return response.data;
}

export async function getDocument(fileId: string | number): Promise<Document> {
  const response = await apiClient.get<Document>(`/documents/${fileId}`);
  return response.data;
}

// ---------------------------------------------------------------------------
// Tags (document organization)
// ---------------------------------------------------------------------------

export async function listTags(vaultId: number): Promise<Tag[]> {
  const response = await apiClient.get<{ tags: Tag[] }>("/tags", { params: { vault_id: vaultId } });
  return response.data.tags;
}

export async function createTag(vaultId: number, name: string, color = ""): Promise<Tag> {
  const response = await apiClient.post<Tag>("/tags", { vault_id: vaultId, name, color });
  return response.data;
}

export async function updateTag(
  tagId: number,
  data: { name?: string; color?: string }
): Promise<Tag> {
  const response = await apiClient.put<Tag>(`/tags/${tagId}`, data);
  return response.data;
}

export async function deleteTag(tagId: number): Promise<void> {
  await apiClient.delete(`/tags/${tagId}`);
}

export async function assignTags(
  vaultId: number,
  fileIds: number[],
  tagIds: number[]
): Promise<{ assigned: number }> {
  const response = await apiClient.post<{ assigned: number }>("/tags/assign", {
    vault_id: vaultId,
    file_ids: fileIds,
    tag_ids: tagIds,
  });
  return response.data;
}

export async function getDocumentTags(fileId: number, vaultId: number): Promise<Tag[]> {
  const response = await apiClient.get<{ tags: Tag[] }>(`/tags/documents/${fileId}`, {
    params: { vault_id: vaultId },
  });
  return response.data.tags;
}

export async function setDocumentTags(
  fileId: number,
  vaultId: number,
  tagIds: number[]
): Promise<Tag[]> {
  const response = await apiClient.put<{ tags: Tag[] }>(`/tags/documents/${fileId}`, {
    vault_id: vaultId,
    tag_ids: tagIds,
  });
  return response.data.tags;
}

export async function unassignTag(
  tagId: number,
  fileId: number,
  vaultId: number
): Promise<void> {
  await apiClient.delete(`/tags/${tagId}/documents/${fileId}`, {
    params: { vault_id: vaultId },
  });
}

// ---------------------------------------------------------------------------
// Folders (document hierarchy)
// ---------------------------------------------------------------------------

export async function listFolders(vaultId: number): Promise<Folder[]> {
  const response = await apiClient.get<{ folders: Folder[] }>("/folders", {
    params: { vault_id: vaultId },
  });
  return response.data.folders;
}

export async function createFolder(
  vaultId: number,
  name: string,
  parentFolderId: number | null = null,
  description = ""
): Promise<Folder> {
  const response = await apiClient.post<Folder>("/folders", {
    vault_id: vaultId,
    name,
    description,
    parent_folder_id: parentFolderId,
  });
  return response.data;
}

export async function updateFolder(
  folderId: number,
  data: { name?: string; description?: string; parent_folder_id?: number | null }
): Promise<Folder> {
  const response = await apiClient.put<Folder>(`/folders/${folderId}`, data);
  return response.data;
}

export async function deleteFolder(folderId: number): Promise<void> {
  await apiClient.delete(`/folders/${folderId}`);
}

export async function moveDocumentsToFolder(
  vaultId: number,
  fileIds: number[],
  folderId: number | null
): Promise<{ moved: number }> {
  const response = await apiClient.post<{ moved: number }>("/folders/move", {
    vault_id: vaultId,
    file_ids: fileIds,
    folder_id: folderId,
  });
  return response.data;
}

export async function uploadDocument(
  file: File,
  onProgress?: (progress: number) => void,
  vaultId?: number
): Promise<UploadDocumentResponse> {
  const formData = new FormData();
  formData.append("file", file);

  const response = await apiClient.post<UploadDocumentResponse>(
    "/documents",
    formData,
    {
      timeout: 0, // disable timeout for file uploads — large files can take minutes
      headers: { "Content-Type": "" },
      ...(vaultId != null && { params: { vault_id: vaultId } }),
      onUploadProgress: (progressEvent) => {
        if (onProgress) {
          if (progressEvent.total) {
            const progress = Math.round(
              (progressEvent.loaded * 100) / progressEvent.total
            );
            onProgress(progress);
          } else {
            // Total unknown - report 0 for indeterminate progress
            onProgress(0);
          }
        }
      },
    }
  );
  return response.data;
}

export async function scanDocuments(vaultId?: number): Promise<ScanDocumentsResponse> {
  const response = await apiClient.post<ScanDocumentsResponse>(
    "/documents/scan",
    undefined,
    vaultId != null ? { params: { vault_id: vaultId } } : undefined
  );
  return response.data;
}

export async function getDocumentStatus(
  fileId: string | number
): Promise<DocumentStatusResponse> {
  const response = await apiClient.get<DocumentStatusResponse>(
    `/documents/${fileId}/status`
  );
  return response.data;
}

export async function getDocumentRawBlob(
  fileId: string | number,
  signal?: AbortSignal
): Promise<Blob> {
  const response = await apiClient.get<Blob>(
    `/documents/${fileId}/raw`,
    {
      responseType: "blob",
      signal,
    }
  );
  return response.data;
}

export async function deleteDocument(fileId: string): Promise<void> {
  await apiClient.delete(`/documents/${fileId}`);
}

export async function deleteDocuments(fileIds: string[]): Promise<{ deleted_count: number, failed_ids: string[] }> {
  const response = await apiClient.post<{ deleted_count: number, failed_ids: string[] }>("/documents/batch", { file_ids: fileIds });
  return response.data;
}

export async function deleteAllDocumentsInVault(vaultId: number): Promise<{ deleted_count: number, vault_id: number }> {
  const response = await apiClient.delete<{ deleted_count: number, vault_id: number }>(`/documents/vault/${vaultId}/all`);
  return response.data;
}

export async function getDocumentStats(vaultId?: number): Promise<DocumentStatsResponse> {
  const response = await apiClient.get<DocumentStatsResponse>("/documents/stats", vaultId != null ? { params: { vault_id: vaultId } } : undefined);
  return response.data;
}

export async function getChunkContext(chunkId: string): Promise<ChunkContextResponse> {
  const response = await apiClient.get<ChunkContextResponse>(
    `/search/chunks/${encodeURIComponent(chunkId)}/context`
  );
  return response.data;
}

/**
 * Parse an SSE stream from a ReadableStream, invoking callbacks for each event.
 * Shared between the initial fetch and the 401 retry path to avoid duplication.
 *
 * Reasoning/thinking fields (``reasoning``, ``reasoning_content``, ``thinking``,
 * ``thinking_content``) are explicitly ignored as defense in depth — the backend
 * already strips these from streamed content, but if a misbehaving server
 * forwards them anyway they must never reach the message store.
 *
 * Exported for tests; not part of the public API.
 */
export async function parseSSEStream(
  reader: ReadableStreamDefaultReader<Uint8Array>,
  callbacks: ChatStreamCallbacks,
): Promise<void> {
  const decoder = new TextDecoder();
  let buffer = "";
  let completed = false;

  const completeOnce = () => {
    if (completed) return;
    completed = true;
    callbacks.onComplete?.();
  };

  // Field/event names we explicitly drop on receipt. Lowercase comparison.
  const REASONING_TYPES = new Set([
    "reasoning",
    "reasoning_content",
    "thinking",
    "thinking_content",
  ]);

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop() || "";

    for (const line of lines) {
      const trimmed = line.trim();
      if (trimmed.startsWith("data: ")) {
        const data = trimmed.slice(6);
        if (data === "[DONE]") {
          completeOnce();
          return;
        }
        try {
          const parsed = JSON.parse(data);
          if (parsed.type === 'error') {
            callbacks.onError?.(new Error(parsed.message || 'Chat stream error'));
            return;
          }
          if (parsed.type === 'mode' && (parsed.mode === 'instant' || parsed.mode === 'thinking')) {
            callbacks.onMode?.(parsed.mode);
            continue;
          }
          // Defense in depth: drop any reasoning/thinking event regardless of
          // whether it appears as ``type`` or as a content field.
          const eventType = typeof parsed.type === "string" ? parsed.type.toLowerCase() : "";
          if (REASONING_TYPES.has(eventType)) {
            continue;
          }
          // Only forward content from explicit "content" events to avoid
          // accidentally streaming a reasoning blob that happened to contain
          // a ``content`` field.
          if (parsed.content && (eventType === "content" || eventType === "" || eventType === "fallback")) {
            // Strip any reasoning-named keys before forwarding (paranoid).
            callbacks.onMessage(parsed.content);
          }
          if (Array.isArray(parsed.sources) && parsed.sources.length > 0) {
            const scoreType = ((parsed as { score_type?: Source["score_type"] }).score_type
              ?? "distance") as Source["score_type"];
            const enrichedSources = parsed.sources.map((s: Source) => ({
              ...s,
              score_type: scoreType,
            }));
            callbacks.onSources?.(enrichedSources);
          }
          if (Array.isArray(parsed.memories_used) && parsed.memories_used.length > 0) {
            // Backend may emit either bare strings (legacy) or structured
            // UsedMemory dicts. Normalize to structured shape; if a string is
            // received, synthesize a minimal record so the UI still renders.
            const normalized: UsedMemory[] = parsed.memories_used.map(
              (m: unknown, idx: number): UsedMemory => {
                if (typeof m === "string") {
                  return {
                    id: `M${idx + 1}`,
                    memory_label: `M${idx + 1}`,
                    content: m,
                  };
                }
                const obj = m as Partial<UsedMemory> & { id?: unknown };
                const fallbackLabel = `M${idx + 1}`;
                return {
                  id: String(obj.id ?? fallbackLabel),
                  memory_label: obj.memory_label ?? fallbackLabel,
                  content: typeof obj.content === "string" ? obj.content : "",
                  category: obj.category ?? null,
                  tags: obj.tags ?? null,
                  source: obj.source ?? null,
                  vault_id: obj.vault_id ?? null,
                  score: obj.score ?? null,
                  score_type: obj.score_type ?? null,
                  created_at: obj.created_at ?? null,
                  updated_at: obj.updated_at ?? null,
                };
              }
            );
            callbacks.onMemories?.(normalized);
          }
          if (Array.isArray(parsed.wiki_used) && parsed.wiki_used.length > 0) {
            callbacks.onWiki?.(parsed.wiki_used as WikiReference[]);
          }
          if (Array.isArray(parsed.kms_used) && parsed.kms_used.length > 0) {
            callbacks.onKMS?.(parsed.kms_used as KMSReference[]);
          }
          if (parsed.citation_validation && typeof parsed.citation_validation === "object") {
            callbacks.onCitationValidation?.(parsed.citation_validation as CitationValidationDebug);
          }
          if (typeof parsed.repaired_content === "string") {
            // Reconcile the message to the citation-clean content before
            // onComplete persists it (fired only when citations were stripped).
            callbacks.onFinalContent?.(parsed.repaired_content);
          }
          if (eventType === "done") {
            completeOnce();
            return;
          }
        } catch {
          // JSON.parse failed — the server sent a malformed SSE chunk.
          // Do NOT forward raw data to onMessage: it could contain thinking
          // content (reasoning_content, <think>, _lhs) that must never be
          // shown to the user.  Drop the chunk and continue streaming.
        }
      }
    }
  }
}

export function chatStream(
  messages: ChatMessage[],
  callbacks: ChatStreamCallbacks,
  vaultId?: number,
  mode?: 'instant' | 'thinking',
): () => void {
  const abortController = new AbortController();
  // Build the request body once and reuse for both the initial POST and
  // the 401 token-refresh retry path. Keeps payload shape consistent.
  const requestBody = JSON.stringify({
    messages,
    ...(vaultId != null && { vault_id: vaultId }),
    ...(mode != null && { mode }),
  });

  const startStream = async () => {
    try {
      // Pre-stream token refresh check: if JWT is close to expiring, refresh it first
      if (_jwtAccessToken && isTokenNearExpiry(_jwtAccessToken)) {
        const refreshedToken = await refreshAccessToken();
        if (!refreshedToken) {
          // Refresh failed - abort
          callbacks.onError?.(new Error("Session expired. Please log in again."));
          return;
        }
      }

      // Get CSRF token for the POST request
      let csrfToken: string;
      try {
        csrfToken = await ensureCsrfToken();
      } catch {
        callbacks.onError?.(new Error("Failed to get CSRF token"));
        return;
      }

      const headers: Record<string, string> = {
        "Content-Type": "application/json",
        "X-CSRF-Token": csrfToken,
      };
      if (_jwtAccessToken) {
        headers["Authorization"] = `Bearer ${_jwtAccessToken}`;
      }

      const response = await fetch(`${API_BASE_URL}/chat/stream`, {
        method: "POST",
        headers,
        body: requestBody,
        signal: abortController.signal,
      });

      if (!response.ok) {
        if (response.status === 401 && _jwtAccessToken) {
          // Check error detail — only retry on token_expired, skip token_invalid/user_inactive
          const errorBody = await response.json().catch(() => null);
          const detail = errorBody?.detail;
          const isTokenExpired = typeof detail === "string" && detail.includes("token_expired");
          const isTokenInvalid = typeof detail === "string" && (
            detail.includes("token_invalid") || detail.includes("user_inactive")
          );

          if (isTokenExpired && !isTokenInvalid) {
            try {
              // Backoff delay before retry (1 second, matching interceptor pattern)
              await new Promise((resolve) => setTimeout(resolve, 1000));

              const newToken = await refreshAccessToken();
              if (newToken) {
                headers["Authorization"] = `Bearer ${newToken}`;
                const retryResponse = await fetch(`${API_BASE_URL}/chat/stream`, {
                  method: "POST",
                  headers,
                  body: requestBody,
                  signal: abortController.signal,
                });
                if (!retryResponse.ok) {
                  throw new Error(`HTTP error! status: ${retryResponse.status}`);
                }
                const retryReader = retryResponse.body?.getReader();
                if (!retryReader) {
                  throw new Error("Response body is not readable");
                }
                await parseSSEStream(retryReader, callbacks);
                return;
              }
            } catch {
              // Refresh or retry failed — fall through to error
            }
          }
        }
        throw new Error(`HTTP error! status: ${response.status}`);
      }

      const reader = response.body?.getReader();
      if (!reader) {
        throw new Error("Response body is not readable");
      }

      await parseSSEStream(reader, callbacks);
    } catch (error) {
      if (error instanceof Error && error.name === "AbortError") {
        return;
      }
      callbacks.onError?.(
        error instanceof Error ? error : new Error(String(error))
      );
    }
  };

  startStream();

  return () => {
    abortController.abort();
  };
}

export function getChatHistory(): ChatHistoryItem[] {
  return storageGetChatHistory();
}

export async function saveChatHistory(history: ChatHistoryItem[]): Promise<void> {
  try {
    const success = await storageSetChatHistory(history);
    if (!success) {
      console.warn("Failed to save chat history: quota exceeded even after trimming");
    }
  } catch (err) {
    console.error("Failed to save chat history:", err);
  }
}

export async function listChatSessions(vaultId?: number): Promise<{ sessions: ChatSession[] }> {
  const response = await apiClient.get<{ sessions: ChatSession[] }>(
    "/chat/sessions",
    vaultId != null ? { params: { vault_id: vaultId } } : undefined
  );
  return response.data;
}

export async function getChatSession(sessionId: number): Promise<ChatSessionDetail> {
  const response = await apiClient.get<ChatSessionDetail>(`/chat/sessions/${sessionId}`);
  return response.data;
}

export async function createChatSession(request: CreateSessionRequest): Promise<ChatSession> {
  const response = await apiClient.post<ChatSession>("/chat/sessions", request);
  return response.data;
}

export async function addChatMessage(sessionId: number, request: AddMessageRequest): Promise<ChatSessionMessage> {
  const response = await apiClient.post<ChatSessionMessage>(`/chat/sessions/${sessionId}/messages`, request);
  return response.data;
}

export async function updateMessageFeedback(
  sessionId: number,
  messageId: number,
  rating: "up" | "down" | null
): Promise<ChatSessionMessage> {
  const response = await apiClient.patch(
    `/chat/sessions/${sessionId}/messages/${messageId}/feedback`,
    { rating }
  );
  return response.data;
}

export async function updateChatSession(sessionId: number, title: string): Promise<ChatSession> {
  const response = await apiClient.put<ChatSession>(`/chat/sessions/${sessionId}`, { title });
  return response.data;
}

export async function deleteChatSession(sessionId: number): Promise<void> {
  await apiClient.delete(`/chat/sessions/${sessionId}`);
}

export interface ForkSessionResponse extends ChatSessionDetail {
  forked_from_session_id: number;
  fork_message_index: number;
}

export async function forkChatSession(sessionId: number, messageIndex: number): Promise<ForkSessionResponse> {
  const response = await apiClient.post<ForkSessionResponse>(
    `/chat/sessions/${sessionId}/fork`,
    { message_index: messageIndex }
  );
  return response.data;
}

// ============================================================================
// Session Interfaces and Functions
// ============================================================================

export interface Session {
  id: string;
  user_id: number;
  user_agent: string | null;
  ip_address: string | null;
  created_at: string;
  expires_at: string;
  is_current: boolean;
}

export interface SessionListResponse {
  sessions: Session[];
}

export interface ChangePasswordRequest {
  current_password: string;
  new_password: string;
}

export async function changePassword(currentPassword: string, newPassword: string): Promise<void> {
  const request: ChangePasswordRequest = {
    current_password: currentPassword,
    new_password: newPassword,
  };
  await apiClient.post("/auth/change-password", request);
}

export async function listSessions(): Promise<SessionListResponse> {
  const response = await apiClient.get<SessionListResponse>("/auth/sessions");
  return response.data;
}

export async function revokeSession(sessionId: number): Promise<void> {
  await apiClient.delete(`/auth/sessions/${sessionId}`);
}

export async function revokeAllSessions(): Promise<{ access_token: string; token_type: string; expires_in: number }> {
  const response = await apiClient.delete<{ access_token: string; token_type: string; expires_in: number }>("/auth/sessions");
  return response.data;
}

// ============================================================================
// Group Interfaces and Functions
// ============================================================================

export interface Group {
  id: number;
  name: string;
  description: string | null;
  created_at: string;
  org_id: number;
  organization_name: string;
}

export interface GroupCreateRequest {
  name: string;
  description: string | null;
  org_id?: number | null;
}

export interface GroupUpdateRequest {
  name: string;
  description: string | null;
}

export interface GroupListResponse {
  groups: Group[];
  total: number;
  page: number;
  per_page: number;
}

export async function listGroups(
  page?: number,
  perPage?: number,
  search?: string
): Promise<GroupListResponse> {
  const params: Record<string, string | number> = {};
  if (page !== undefined) params.page = page;
  if (perPage !== undefined) params.per_page = perPage;
  if (search !== undefined) params.search = search;

  const response = await apiClient.get<GroupListResponse>("/groups", { params });
  return response.data;
}

export async function createGroup(name: string, description: string | null, orgId?: number | null): Promise<Group> {
  const request: GroupCreateRequest = { name, description, org_id: orgId };
  const response = await apiClient.post<Group>("/groups", request);
  return response.data;
}

export async function updateGroup(
  groupId: number,
  name: string,
  description: string | null
): Promise<Group> {
  const request: GroupUpdateRequest = { name, description };
  const response = await apiClient.put<Group>(`/groups/${groupId}`, request);
  return response.data;
}

export async function deleteGroup(groupId: number): Promise<void> {
  await apiClient.delete(`/groups/${groupId}`);
}

export interface GroupMember {
  id: number;
  username: string;
  full_name: string | null;
}

export async function getGroupMembers(groupId: number): Promise<GroupMember[]> {
  const response = await apiClient.get<GroupMember[]>(`/groups/${groupId}/members`);
  return response.data;
}

export async function updateGroupMembers(groupId: number, userIds: number[]): Promise<void> {
  await apiClient.put(`/groups/${groupId}/members`, { user_ids: userIds });
}

export async function getEligibleGroupMembers(groupId: number): Promise<GroupMember[]> {
  const response = await apiClient.get<GroupMember[]>(`/groups/${groupId}/eligible-members`);
  return response.data;
}

export async function getGroupVaults(groupId: number): Promise<GroupVault[]> {
  const response = await apiClient.get<GroupVault[]>(`/groups/${groupId}/vaults`);
  return response.data;
}

export async function updateGroupVaults(
  groupId: number,
  vaultAccess: VaultAccessItem[]
): Promise<void> {
  await apiClient.put(`/groups/${groupId}/vaults`, { vault_access: vaultAccess });
}

// ============================================================================
// User Interfaces and Functions
// ============================================================================

export interface User {
  id: number;
  email: string;
  full_name: string | null;
  is_active: boolean;
  is_superuser: boolean;
  created_at: string;
  updated_at: string;
}

export interface UserListItem {
  id: number;
  username: string;
  full_name: string | null;
  role: string;
  is_active: boolean;
}

export async function listAllUsers(): Promise<UserListItem[]> {
  const response = await apiClient.get<{ users: UserListItem[] }>("/users");
  return response.data.users;
}

export async function getUserGroups(userId: number): Promise<{ groups: Group[] }> {
  const response = await apiClient.get<{ groups: Group[] }>(`/users/${userId}/groups`);
  return response.data;
}

export async function updateUserGroups(userId: number, groupIds: number[]): Promise<void> {
  await apiClient.put(`/users/${userId}/groups`, { group_ids: groupIds });
}

// ============================================================================
// Vault-Group Interfaces and Functions
// ============================================================================

export interface GroupVault {
  id: number;
  name: string;
  org_id: number | null;
  permission: string;
}

export interface VaultAccessItem {
  vault_id: number;
  permission: string;
}

export interface VaultGroupAccess {
  group_id: number;
  permission: string;
}

export async function getVaultGroups(vaultId: number): Promise<{ groups: Array<{ id: number; name: string }> }> {
  const response = await apiClient.get<{ groups: Array<{ id: number; name: string }> }>(`/vaults/${vaultId}/groups`);
  return response.data;
}

export async function updateVaultGroups(
  vaultId: number,
  groupAccess: { groupId: number; permission: string }[]
): Promise<void> {
  await apiClient.put(`/vaults/${vaultId}/groups`, {
    vault_access: groupAccess.map(ga => ({ group_id: ga.groupId, permission: ga.permission })),
  });
}

// ============================================================================
// Chat Message Functions (Not yet implemented in backend)
// ============================================================================

// ============================================================================
// Wiki / Knowledge Compiler Types and Functions
// ============================================================================

export interface WikiClaimSource {
  id: number;
  claim_id: number;
  source_kind: "document" | "memory" | "chat_message" | "manual";
  file_id: number | null;
  chunk_id: string | null;
  memory_id: number | null;
  chat_message_id: number | null;
  source_label: string | null;
  quote: string | null;
  char_start: number | null;
  char_end: number | null;
  page_number: number | null;
  confidence: number;
  created_at: string;
}

export interface WikiClaim {
  id: number;
  vault_id: number;
  page_id: number | null;
  claim_text: string;
  claim_type: string;
  subject: string | null;
  predicate: string | null;
  object: string | null;
  source_type: "document" | "memory" | "chat_synthesis" | "manual" | "mixed";
  // 'needs_review' added by PR C — curator-authored claims default to
  // this status in draft mode.
  status:
    | "active"
    | "contradicted"
    | "superseded"
    | "unverified"
    | "archived"
    | "needs_review";
  confidence: number;
  created_by: number | null;
  /** PR C: provenance — null for legacy / deterministic rows. */
  created_by_kind?: "deterministic" | "llm_curator" | null;
  created_at: string;
  updated_at: string;
  sources: WikiClaimSource[];
}

export interface WikiEntity {
  id: number;
  vault_id: number;
  canonical_name: string;
  entity_type: string;
  aliases_json: string;
  description: string;
  page_id: number | null;
  created_at: string;
  updated_at: string;
}

export interface WikiPage {
  id: number;
  vault_id: number;
  slug: string;
  title: string;
  page_type: "entity" | "procedure" | "system" | "acronym" | "qa" | "contradiction" | "open_question" | "overview" | "manual";
  markdown: string;
  summary: string;
  status: "draft" | "verified" | "stale" | "needs_review" | "archived";
  confidence: number;
  created_by: number | null;
  created_at: string;
  updated_at: string;
  last_compiled_at: string | null;
  claims: WikiClaim[];
  entities: WikiEntity[];
  lint_findings: WikiLintFinding[];
}

export interface WikiRelation {
  id: number;
  vault_id: number;
  subject_entity_id: number | null;
  predicate: string;
  object_entity_id: number | null;
  object_text: string | null;
  claim_id: number | null;
  confidence: number;
  created_at: string;
}

export interface WikiCompileJob {
  id: number;
  vault_id: number;
  trigger_type: "ingest" | "query" | "memory" | "manual" | "settings_reindex";
  trigger_id: string | null;
  status: "pending" | "running" | "completed" | "failed" | "cancelled";
  error: string | null;
  result_json: string;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  retry_count: number;
}

export interface DocumentWikiStatus {
  file_id: number;
  wiki_status: "not_compiled" | "compiling" | "compiled" | "failed" | "skipped";
  pages_count: number;
  claims_count: number;
  active_claims: number;
  lint_count: number;
  pages: Array<{ id: number; slug: string; title: string; page_type: string; status: string }>;
  latest_job: WikiCompileJob | null;
  job_count: number;
}

export interface MemoryWikiStatus {
  memory_id: number;
  wiki_status: "not_promoted" | "promoted" | "stale" | "promoting";
  claims_count: number;
  active_claims: number;
  stale_claims: number;
  linked_pages: Array<{ id: number; slug: string; title: string; page_type: string; status: string }>;
  latest_job: WikiCompileJob | null;
  job_count: number;
}

export interface WikiLintFinding {
  id: number;
  vault_id: number;
  finding_type: "contradiction" | "stale" | "orphan" | "missing_page" | "unsupported_claim" | "duplicate_entity" | "weak_provenance";
  severity: "low" | "medium" | "high" | "critical";
  title: string;
  details: string;
  related_page_ids_json: string;
  related_claim_ids_json: string;
  status: "open" | "acknowledged" | "resolved" | "dismissed";
  created_at: string;
  updated_at: string;
}

export interface WikiSearchResponse {
  query: string;
  pages: WikiPage[];
  claims: WikiClaim[];
  entities: WikiEntity[];
}

export interface PromoteMemoryRequest {
  memory_id: number;
  vault_id: number;
  page_type?: string;
  target_page_id?: number;
  status?: string;
}

export interface PromoteMemoryResponse {
  page: WikiPage;
  claims: WikiClaim[];
  entities: WikiEntity[];
  relations: WikiRelation[];
}

export async function listWikiPages(params: {
  vault_id: number;
  page_type?: string;
  status?: string;
  search?: string;
  page?: number;
  per_page?: number;
}): Promise<{ pages: WikiPage[]; page: number; per_page: number }> {
  const response = await apiClient.get<{ pages: WikiPage[]; page: number; per_page: number }>(
    "/wiki/pages",
    { params }
  );
  return response.data;
}

export async function getWikiPage(pageId: number): Promise<WikiPage> {
  const response = await apiClient.get<WikiPage>(`/wiki/pages/${pageId}`);
  return response.data;
}

export async function createWikiPage(data: {
  vault_id: number;
  title: string;
  page_type: string;
  slug?: string;
  markdown?: string;
  summary?: string;
  status?: string;
  confidence?: number;
}): Promise<WikiPage> {
  const response = await apiClient.post<WikiPage>("/wiki/pages", data);
  return response.data;
}

export async function updateWikiPage(pageId: number, data: {
  title?: string;
  page_type?: string;
  slug?: string;
  markdown?: string;
  summary?: string;
  status?: string;
  confidence?: number;
}): Promise<WikiPage> {
  const response = await apiClient.put<WikiPage>(`/wiki/pages/${pageId}`, data);
  return response.data;
}

export async function deleteWikiPage(pageId: number): Promise<void> {
  await apiClient.delete(`/wiki/pages/${pageId}`);
}

export async function listWikiEntities(params: {
  vault_id: number;
  search?: string;
}): Promise<{ entities: WikiEntity[] }> {
  const response = await apiClient.get<{ entities: WikiEntity[] }>("/wiki/entities", { params });
  return response.data;
}

export async function listWikiClaims(params: {
  vault_id: number;
  page_id?: number;
  entity?: string;
  search?: string;
  status?: string;
}): Promise<{ claims: WikiClaim[] }> {
  const response = await apiClient.get<{ claims: WikiClaim[] }>("/wiki/claims", { params });
  return response.data;
}

export async function listWikiLintFindings(params: {
  vault_id: number;
  status?: string;
  severity?: string;
}): Promise<{ findings: WikiLintFinding[] }> {
  const response = await apiClient.get<{ findings: WikiLintFinding[] }>("/wiki/lint", { params });
  return response.data;
}

export async function runWikiLint(vaultId: number): Promise<{ findings: WikiLintFinding[]; count: number }> {
  const response = await apiClient.post<{ findings: WikiLintFinding[]; count: number }>(
    "/wiki/lint/run",
    { vault_id: vaultId }
  );
  return response.data;
}

export async function searchWiki(params: {
  vault_id: number;
  q: string;
}): Promise<WikiSearchResponse> {
  const response = await apiClient.get<WikiSearchResponse>("/wiki/search", { params });
  return response.data;
}

export async function promoteMemoryToWiki(request: PromoteMemoryRequest): Promise<PromoteMemoryResponse> {
  const response = await apiClient.post<PromoteMemoryResponse>("/wiki/promote-memory", request);
  return response.data;
}

export async function listWikiJobs(params: {
  vault_id: number;
  status?: string;
}): Promise<{ jobs: WikiCompileJob[] }> {
  const response = await apiClient.get<{ jobs: WikiCompileJob[] }>("/wiki/jobs", { params });
  return response.data;
}

export async function getWikiJob(jobId: number, vaultId: number): Promise<WikiCompileJob> {
  const response = await apiClient.get<WikiCompileJob>(`/wiki/jobs/${jobId}`, {
    params: { vault_id: vaultId },
  });
  return response.data;
}

export async function retryWikiJob(jobId: number, vaultId: number): Promise<WikiCompileJob> {
  const response = await apiClient.post<WikiCompileJob>(`/wiki/jobs/${jobId}/retry`, null, {
    params: { vault_id: vaultId },
  });
  return response.data;
}

export async function cancelWikiJob(jobId: number, vaultId: number): Promise<{ job_id: number; status: string }> {
  const response = await apiClient.post<{ job_id: number; status: string }>(
    `/wiki/jobs/${jobId}/cancel`,
    null,
    { params: { vault_id: vaultId } }
  );
  return response.data;
}

export async function recompileVaultWiki(vaultId: number): Promise<{ job_id: number; status: string }> {
  const response = await apiClient.post<{ job_id: number; status: string }>(
    "/wiki/recompile",
    null,
    { params: { vault_id: vaultId } }
  );
  return response.data;
}

export async function getDocumentWikiStatus(fileId: number, vaultId: number): Promise<DocumentWikiStatus> {
  const response = await apiClient.get<DocumentWikiStatus>(
    `/wiki/documents/${fileId}/status`,
    { params: { vault_id: vaultId } }
  );
  return response.data;
}

export async function compileDocumentWiki(fileId: number, vaultId: number): Promise<{ job_id: number; status: string }> {
  const response = await apiClient.post<{ job_id: number; status: string }>(
    `/wiki/documents/${fileId}/compile`,
    null,
    { params: { vault_id: vaultId } }
  );
  return response.data;
}

export async function getMemoryWikiStatus(memoryId: number, vaultId: number): Promise<MemoryWikiStatus> {
  const response = await apiClient.get<MemoryWikiStatus>(
    `/wiki/memories/${memoryId}/status`,
    { params: { vault_id: vaultId } }
  );
  return response.data;
}

// Version history
export async function getWikiPageVersions(pageId: number, vaultId: number) {
  const response = await apiClient.get(`/wiki/pages/${pageId}/versions`, { params: { vault_id: vaultId } });
  return response.data;
}

// File attachments
export async function getWikiPageFiles(pageId: number, vaultId: number) {
  const response = await apiClient.get(`/wiki/pages/${pageId}/files`, { params: { vault_id: vaultId } });
  return response.data;
}

export async function attachWikiPageFile(pageId: number, vaultId: number, fileId: number) {
  const response = await apiClient.post(`/wiki/pages/${pageId}/files`, { vault_id: vaultId, file_id: fileId });
  return response.data;
}

export async function detachWikiPageFile(pageId: number, fileId: number, vaultId: number) {
  const response = await apiClient.delete(`/wiki/pages/${pageId}/files/${fileId}`, { params: { vault_id: vaultId } });
  return response.data;
}

// Backlinks
export async function getWikiPageBacklinks(pageId: number, vaultId: number) {
  const response = await apiClient.get(`/wiki/pages/${pageId}/backlinks`, { params: { vault_id: vaultId } });
  return response.data;
}

// Activity feed
export async function getWikiActivityFeed(vaultId: number, limit: number = 50) {
  const response = await apiClient.get("/wiki/activity", { params: { vault_id: vaultId, limit } });
  return response.data;
}

// Bulk operations
export async function bulkWikiPageAction(vaultId: number, pageIds: number[], action: "delete" | "update", updates?: Record<string, unknown>) {
  const response = await apiClient.post("/wiki/pages/bulk", { vault_id: vaultId, page_ids: pageIds, action, updates });
  return response.data;
}

export async function resolveWikiLintFinding(
  findingId: number,
  vaultId: number,
  status: "resolved" | "dismissed" | "acknowledged"
): Promise<WikiLintFinding> {
  const response = await apiClient.post<WikiLintFinding>(
    `/wiki/lint/${findingId}/resolve`,
    { status },
    { params: { vault_id: vaultId } }
  );
  return response.data;
}

// ---------------------------------------------------------------------------
// KMS / Knowledge Management
// ---------------------------------------------------------------------------

export interface KMSEntry {
  id: number;
  vault_id: number;
  file_id: number | null;
  slug: string;
  title: string;
  body: string;
  summary: string;
  tags_json: string;
  tags: string[];
  source_type: "manual" | "document" | "import";
  status: "draft" | "published" | "archived";
  created_by: number | null;
  created_at: string;
  updated_at: string;
  last_compiled_at: string | null;
}

export interface KMSEntryListResponse {
  entries: KMSEntry[];
  total: number;
  page: number;
  per_page: number;
}

export interface KMSCompileJob {
  id: number;
  vault_id: number;
  trigger_type: "ingest" | "manual" | "settings_reindex";
  trigger_id: string | null;
  status: "pending" | "running" | "completed" | "failed" | "cancelled";
  error: string | null;
  result_json: string;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  input_json: string | null;
  retry_count: number;
}

export async function listKMSEntries(params: {
  vault_id: number;
  status?: string;
  tag?: string;
  search?: string;
  page?: number;
  per_page?: number;
}): Promise<KMSEntryListResponse> {
  const response = await apiClient.get<KMSEntryListResponse>("/kms/entries", {
    params,
  });
  return response.data;
}

export async function getKMSEntry(entryId: number): Promise<KMSEntry> {
  const response = await apiClient.get<KMSEntry>(`/kms/entries/${entryId}`);
  return response.data;
}

export async function createKMSEntry(data: {
  vault_id: number;
  title: string;
  body?: string;
  summary?: string;
  tags?: string[];
  slug?: string;
  status?: string;
}): Promise<KMSEntry> {
  const response = await apiClient.post<KMSEntry>("/kms/entries", data);
  return response.data;
}

export async function updateKMSEntry(
  entryId: number,
  data: {
    title?: string;
    body?: string;
    summary?: string;
    tags?: string[];
    slug?: string;
    status?: string;
  }
): Promise<KMSEntry> {
  const response = await apiClient.put<KMSEntry>(`/kms/entries/${entryId}`, data);
  return response.data;
}

export async function deleteKMSEntry(entryId: number): Promise<void> {
  await apiClient.delete(`/kms/entries/${entryId}`);
}

export async function searchKMS(params: {
  vault_id: number;
  q: string;
  page?: number;
  per_page?: number;
}): Promise<{ query: string } & KMSEntryListResponse> {
  const response = await apiClient.get<{ query: string } & KMSEntryListResponse>(
    "/kms/search",
    { params }
  );
  return response.data;
}

export async function compileDocumentKMS(
  fileId: number,
  vaultId: number
): Promise<{ job_id: number; status: string }> {
  const response = await apiClient.post<{ job_id: number; status: string }>(
    `/kms/documents/${fileId}/compile`,
    null,
    { params: { vault_id: vaultId } }
  );
  return response.data;
}

export async function recompileVaultKMS(
  vaultId: number
): Promise<{ job_id: number; status: string }> {
  const response = await apiClient.post<{ job_id: number; status: string }>(
    "/kms/recompile",
    null,
    { params: { vault_id: vaultId } }
  );
  return response.data;
}

export async function listKMSJobs(
  vaultId: number,
  status?: string
): Promise<{ jobs: KMSCompileJob[] }> {
  const response = await apiClient.get<{ jobs: KMSCompileJob[] }>("/kms/jobs", {
    params: { vault_id: vaultId, status },
  });
  return response.data;
}

/** Trigger a browser download of a document's original bytes (DD-C009). */
export async function downloadDocument(
  fileId: string | number,
  fileName: string
): Promise<void> {
  const blob = await getDocumentRawBlob(fileId);
  const url = window.URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = fileName || `document-${fileId}`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  window.URL.revokeObjectURL(url);
}

export default apiClient;
