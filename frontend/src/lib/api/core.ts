import { APP_BASENAME } from "@/lib/paths";
import axios, { AxiosRequestHeaders } from "axios";
import { appPath } from "../paths";

export const API_BASE_URL = import.meta.env.VITE_API_URL || appPath("/api");
console.info("[KnowledgeVault] API_BASE_URL:", API_BASE_URL);

const IDEMPOTENT_METHODS = new Set(["get", "head", "options"]);
const TRANSIENT_STATUS_CODES = new Set([502, 503, 504]);
const TRANSIENT_RETRY_DELAYS_MS = [300, 900];
// Error Blobs larger than this are never buffered into a string for JSON decoding (PRR-021).
export const MAX_ERROR_BLOB_DECODE_BYTES = 64 * 1024;

export function isTransientRetryableRequest(method?: string, status?: number, hasResponse = true): boolean {
  if (!method || !IDEMPOTENT_METHODS.has(method.toLowerCase())) {
    return false;
  }
  return !hasResponse || (status !== undefined && TRANSIENT_STATUS_CODES.has(status));
}

export function transientRetryDelayMs(retryCount: number): number {
  return TRANSIENT_RETRY_DELAYS_MS[Math.min(retryCount, TRANSIENT_RETRY_DELAYS_MS.length - 1)];
}

function wait(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// Module-level JWT token holder - persisted via useAuthStore persist middleware
export let _jwtAccessToken: string | null = null;

export function setJwtAccessToken(token: string | null): void {
  _jwtAccessToken = token;
}

export function getJwtAccessToken(): string | null {
  return _jwtAccessToken;
}

// Read CSRF token from the non-httpOnly cookie set by the server.
// Shadow-cookie defense: a stale same-name cookie with a broader Path (e.g.
// a leftover Path=/ cookie from a pre-subpath deployment) can coexist with
// the current scoped one. document.cookie collapses duplicates last-wins,
// so the read may surface the stale value. When multiple candidates exist,
// purge the broader-path shadows and re-read so the scoped cookie wins.
export function getCsrfCookie(): string | null {
  const read = () => {
    const match = document.cookie
      .split('; ')
      .find(row => row.startsWith('X-CSRF-Token='));
    // Use split with limit=2 so token values containing '=' (base64 padding) are preserved
    return match ? decodeURIComponent(match.split('=', 2)[1]) : null;
  };
  let token = read();
  if (document.cookie.split('; ').filter(row => row.startsWith('X-CSRF-Token=')).length > 1) {
    // Duplicate same-name cookies: purge JS-addressable path-variants and
    // re-read (falls back to the pre-purge read if none survive). If the
    // purge removed the good cookie too, ensureCsrfToken's network fetch
    // re-establishes it on the next mutating request.
    purgeStaleCsrfCookies();
    token = read() ?? token;
  }
  return token;
}

// Shadow-cookie cleanup for the whole origin: removes stale X-CSRF-Token
// cookies at common paths so only the scoped one remains. Safe to call
// anytime; the next token fetch re-establishes the cookie if needed.
export function purgeStaleCsrfCookies(): void {
  // The server sets the CSRF cookie at the app-root path (APP_BASENAME —
  // `/meridian` for a subpath deployment, `/` for a root deployment). That
  // path is authoritative: the server issues it, so deleting it cannot heal
  // anything, and in a root deployment it IS the legitimate cookie. Only
  // strictly broader paths (shadows like a pre-subpath `Path=/` leftover)
  // and unrelated app-local paths are safe to purge.
  const appRoot = APP_BASENAME ? APP_BASENAME.replace(/\/+$/, '') : '';
  const base = new URL(API_BASE_URL, location.origin).pathname.replace(/\/+$/, '');
  const candidates = ['/', base + '/', location.pathname.replace(/\/[^/]*$/, '') || '/'];
  for (const p of new Set(candidates)) {
    if (appRoot !== '' && (p === appRoot || p === appRoot + '/')) continue;
    document.cookie = `X-CSRF-Token=; path=${p}; max-age=0`;
  }
}

// CSRF token cache and deduplication — single source of truth
let _csrfToken: string | null = null;
let _csrfFetchPromise: Promise<string> | null = null;

// Bounded CSRF fetch (issue #774, TQ-sweep-B05-02): every mutating request
// awaits the singleton below, so a hung /csrf-token wedges all writes. The
// deadline rejects with a plain Error — never an "AbortError" and never
// abort-worded — because AbortError is the user-cancel sentinel (chatStream's
// catch) and an abort-worded message matches the /aborted|abort/i user-cancel
// check in useSendMessage.
const CSRF_FETCH_TIMEOUT_MS = 10_000;

export function resetCsrfToken(): void {
  _csrfToken = null;
  _csrfFetchPromise = null;
}

/**
 * Get the cached CSRF token.
 * Lifecycle keepalive callers use this non-blocking cache read when a refresh
 * has cleared the in-memory token; awaited requests should prefer
 * ensureCsrfToken() so the cookie/network fallback can run.
 */
export function getCsrfToken(): string | null {
  return _csrfToken;
}

export async function ensureCsrfToken(force: boolean = false): Promise<string> {
  // `force` bypasses the in-memory cache as well as the cookie jar: callers
  // force exactly when the cached token is known-stale (post-403 retry,
  // post-refresh rotation), so serving the cache would defeat the point.
  if (_csrfToken && !force) return _csrfToken;

  // Check cookie first — unless forced: after a CSRF 403 the cookie may hold
  // the very token the server just rejected (rotated by login/refresh, or
  // expired server-side while Max-Age keeps it in the jar). A forced refresh
  // bypasses the jar and gets a server-issued token + fresh cookie pair.
  if (!force) {
    const cookieToken = getCsrfCookie();
    if (cookieToken) {
      _csrfToken = cookieToken;
      return cookieToken;
    }
  }

  if (!_csrfFetchPromise) {
    const controller = new AbortController();
    const deadline = setTimeout(() => controller.abort(), CSRF_FETCH_TIMEOUT_MS);
    const newPromise: Promise<string> = fetch(`${API_BASE_URL}/csrf-token`, {
        credentials: "include",
        signal: controller.signal,
      })
      .then(async (resp) => {
        if (!resp.ok) throw new Error("Failed to fetch CSRF token");
        const data = await resp.json();
        if (!data.csrf_token || typeof data.csrf_token !== "string") {
          throw new Error("CSRF token missing from response");
        }
        const token: string = data.csrf_token;
        _csrfToken = token;
        return token;
      })
      .catch((err: unknown) => {
        if (controller.signal.aborted) {
          // Our own deadline fired (not a caller cancel — this fetch has no
          // external signal): surface the timeout, which settles the promise
          // and lets the singleton clear below so a retry issues a new fetch.
          const timeout = new Error("csrf token fetch timed out");
          (timeout as Error & { cause?: unknown }).cause = err;
          throw timeout;
        }
        throw err;
      })
      .finally(() => clearTimeout(deadline));
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
      // Blob error bodies (responseType: "blob", e.g. the draft export
      // download) carry no .detail/.code — axios delivers the raw Blob. Decode
      // JSON blob bodies regardless of status and replace
      // error.response.data IN PLACE (mutating the shared axios error object)
      // so every downstream reader — the CSRF-403 detection here, this
      // module's message extraction, and Draft Room error parsing via
      // originalError.response.data — sees the real envelope
      // {detail, code, context} while the status is preserved. Non-JSON blobs
      // keep today's fallbacks (issue #516 API-003; the previous 403-only
      // decode is a subset of this). Blobs above MAX_ERROR_BLOB_DECODE_BYTES
      // are also left as-is: a huge error body must not be buffered into a
      // string wholesale, so its readers fall back to statusText / header-
      // based CSRF detection exactly as they did before the decode existed.
      if (
        typeof Blob !== "undefined" &&
        error.response?.data instanceof Blob &&
        error.response.data.size <= MAX_ERROR_BLOB_DECODE_BYTES
      ) {
        try {
          error.response.data = JSON.parse(await error.response.data.text());
        } catch {
          // non-JSON body — leave the Blob as-is
        }
      }
      const detail = error.response?.data?.detail || "";
      const isCsrfError = error.response?.status === 403 && (
        error.response?.headers?.["x-csrf-error"] === "true" ||
        (typeof detail === "string" && detail.toLowerCase().includes("csrf"))
      );
      if (isCsrfError && config && !config._csrfRetry) {
        resetCsrfToken(); // force refresh on next request
        purgeStaleCsrfCookies(); // drop shadow cookies before refetch
        config._csrfRetry = true;
        let newToken: string;
        try {
          newToken = await ensureCsrfToken(true);
        } catch {
          // Token fetch failed (incl. the #774 deadline) — keep the caller's
          // original CSRF error instead of surfacing the fetch failure.
          return Promise.reject(error);
        }
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

// Subpath deployment guardrail: this bundle bakes VITE_APP_BASENAME at build
// time while the refresh cookie's Path comes from the backend's APP_ROOT_PATH
// env. When the two diverge, the browser never returns the cookie and every
// refresh is rejected with one of two signatures — a 401 from /auth/refresh
// (cookie missing) or a CSRF-marked 403 (csrf_protect rejects before the
// handler when the CSRF cookie also misses the prefixed path; the raw fetch
// here bypasses the axios CSRF retry). Only those authentication-shaped
// rejections emit the diagnostic, once per failure burst, so unrelated
// outages (5xx, network errors) and ordinary session expiry noise stay
// appropriately phrased instead of asserting a misconfiguration.
let _refreshMismatchDiagnosed = false;

function isAuthShapedRefreshRejection(response: Response): boolean {
  if (response.status === 401) return true;
  return response.status === 403 && response.headers.get("x-csrf-error") === "true";
}

function diagnoseSubpathRefreshFailure(): void {
  if (_refreshMismatchDiagnosed || !APP_BASENAME) return;
  _refreshMismatchDiagnosed = true;
  console.error(
    `[Auth] Silent token refresh was rejected while the app is served under "${APP_BASENAME}" (VITE_APP_BASENAME). If this is unexpected for an active session, verify APP_ROOT_PATH and VITE_APP_BASENAME are identical in .env (or the Compose environment). VITE_APP_BASENAME is baked into the frontend image; rebuild and restart with \`docker compose build --no-cache && docker compose up -d\`.`
  );
}

// Session boundaries (login/register/logout) start a new diagnostic burst.
export function resetSubpathRefreshDiagnostic(): void {
  _refreshMismatchDiagnosed = false;
}

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

// Bounded refresh fetch (issue #774, TQ-sweep-B05-02): a hung /auth/refresh
// wedges all 401 recovery behind the singleton above. Same error-shape rules
// as the CSRF deadline: the timeout is a plain Error, never an AbortError and
// never abort-worded (user-cancel sentinels).
const AUTH_REFRESH_TIMEOUT_MS = 10_000;

async function _doRefresh(): Promise<string | null> {
  // The /auth/refresh endpoint requires the CSRF token.
  // Read it from the non-httpOnly cookie; if missing, fetch a fresh one.
  // Force a server-issued token: the jar's cookie may have expired in step
  // with its Redis TTL (both 900s) while the access token outlived it, which
  // is exactly when a silent refresh fires.
  let csrfToken: string | null = null;
  try {
    csrfToken = await ensureCsrfToken(true);
  } catch {
    // proceed without CSRF — server will reject if required
  }

  const headers: Record<string, string> = {};
  if (csrfToken) {
    headers["X-CSRF-Token"] = csrfToken;
  }

  const controller = new AbortController();
  const deadline = setTimeout(() => controller.abort(), AUTH_REFRESH_TIMEOUT_MS);
  const stopDeadline = () => clearTimeout(deadline);
  let response: Response;
  try {
    response = await fetch(`${API_BASE_URL}/auth/refresh`, {
      method: "POST",
      credentials: "include", // Send httpOnly cookie with refresh token
      headers,
      signal: controller.signal,
    });
  } catch (err) {
    stopDeadline();
    if (controller.signal.aborted) {
      const timeout = new Error("auth refresh timed out");
      (timeout as Error & { cause?: unknown }).cause = err;
      throw timeout;
    }
    // Network failure — transport-class by contract (#774): rethrow so the
    // auth store keeps the session instead of treating an outage as a
    // session rejection. Callers that cannot throw already catch this.
    throw err;
  }
  if (!response.ok) {
    stopDeadline();
    if (isAuthShapedRefreshRejection(response)) {
      diagnoseSubpathRefreshFailure();
      return null;
    }
    if (response.status >= 500) {
      // A 5xx is an outage, not a session verdict (#774) — transport-class.
      throw new Error(`auth refresh failed with status ${response.status}`);
    }
    return null;
  }
  // /auth/refresh rotates the CSRF cookie (issue_csrf_token in the handler).
  // Drop the cached token so the next mutating request re-reads the cookie
  // instead of sending the stale pre-refresh token (403 CSRF mismatch).
  resetCsrfToken();
  try {
    // The deadline covers the body read too (#774, reviewer question): a
    // server that sends headers and then stalls the body would otherwise
    // wedge the _refreshInFlight singleton exactly like a hung fetch.
    const data = await response.json();
    stopDeadline();
    _jwtAccessToken = data.access_token;
    _refreshMismatchDiagnosed = false;
    return data.access_token ?? null;
  } catch (err) {
    stopDeadline();
    if (controller.signal.aborted) {
      const timeout = new Error("auth refresh timed out");
      (timeout as Error & { cause?: unknown }).cause = err;
      throw timeout;
    }
    if (err instanceof SyntaxError) {
      // A 2xx whose body does not parse (e.g. a proxy's 200 + HTML) keeps
      // today's behavior: a failed refresh, not a transport error (#774).
      return null;
    }
    // Any other body-read failure is transport-class.
    throw err;
  }
}

export const apiClient = axios.create({
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
export function getTokenExpiry(token: string): number | null {
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
export function isTokenNearExpiry(token: string, bufferMs: number = 60000): boolean {
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
      // True when the refresh failed for transport reasons (#774): the
      // session is kept, no logout/redirect, and the error skips the logout
      // below so control reaches the shared normalizer tail — callers get
      // the standard .message/.status/.originalError shape instead of a raw
      // AxiosError (PRR-011 / OOB F-006). No retry loop is possible: 401 is
      // not a transient status and non-idempotent methods never
      // transient-retry.
      let refreshTransportFailure = false;

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
            // Refresh transport failure (#774): refreshAccessToken only
            // rejects for transport-class failures; an auth-shaped rejection
            // resolves null and takes the logout below.
            refreshTransportFailure = true;
          }
        }
      }

      if (!refreshTransportFailure) {
        // Clear auth state and redirect to login
        _jwtAccessToken = null;
        redirectToLogin();
      }
    }

    const retryConfig = error.config;
    const retryCount = (retryConfig?._transientRetryCount || 0) as number;
    if (
      retryConfig &&
      retryCount < TRANSIENT_RETRY_DELAYS_MS.length &&
      isTransientRetryableRequest(
        retryConfig.method,
        error.response?.status,
        Boolean(error.response)
      )
    ) {
      retryConfig._transientRetryCount = retryCount + 1;
      await wait(transientRetryDelayMs(retryCount));
      return apiClient(retryConfig);
    }

    // Extract the most useful error message
    let message = "An unexpected error occurred";

    if (error.response) {
      // Server responded with an error status
      const data = error.response.data;
      const detail = data?.detail;
      if (Array.isArray(detail)) {
        // FastAPI request-validation failures (422) send `detail` as an
        // array of per-field error objects ({loc, msg, type}). Format each
        // entry as "<field>: <msg>" (field = last loc segment) joined by
        // "; " so consumers see which fields failed and why — the raw
        // array would coerce to "[object Object],..." in Error.message.
        const formatted = detail
          .map((entry: unknown) => {
            if (entry !== null && typeof entry === "object") {
              const { loc, msg } = entry as { loc?: unknown; msg?: unknown };
              const segments = Array.isArray(loc) ? loc : [];
              const field = segments.length
                ? String(segments[segments.length - 1])
                : "";
              const text =
                typeof msg === "string" ? msg : JSON.stringify(msg) ?? "";
              return field ? `${field}: ${text}` : text;
            }
            return String(entry);
          })
          .filter((part: string) => part.length > 0)
          .join("; ");
        message = formatted || data?.message || data?.error || error.response.statusText || message;
      } else {
        message = detail || data?.message || data?.error || error.response.statusText || message;
      }
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

export interface Tag {
  id: number;
  vault_id: number;
  name: string;
  color: string;
  created_at: string;
  updated_at: string;
  document_count: number;
}

/**
 * Structured parse diagnostics produced by the extraction pipeline
 * (issue #514 / PRODUCT-ENH-06). Reveals extraction omissions (low-content
 * pages, dropped tables) even when embedding succeeded.
 */
export interface ExtractionDiagnostics {
  pages_total: number;
  pages_with_text: number;
  low_content_pages: number[];
  ocr_used: boolean;
  tables_detected: number;
  captions_detected?: number;
  extraction_version: string;
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
  /** Mirrors chunk_count/status; chunks_failed counts chunks dropped by embedding failures (Issue #221). */
  metadata?: Record<string, unknown> & { chunks_failed?: number };
  tags?: Tag[];
  folder_id?: number | null;
  /** Parse-quality diagnostics persisted on files.extraction_diagnostics (issue #514). Matches the backend wire field name. */
  extraction_diagnostics?: ExtractionDiagnostics | null;
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
 * `status` stays in the canonical files.status enum
 * ("pending" | "processing" | "indexed" | "partial" | "error" | "cancelled"
 * — "partial" = completed with failed chunks, issue #513; "cancelled" =
 * user-cancelled ingest, issue #783). Async lifecycle detail
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
  /** Parse-quality diagnostics persisted on files.extraction_diagnostics (issue #514). Matches the backend wire field name. */
  extraction_diagnostics?: ExtractionDiagnostics | null;
}

/**
 * One per-id entry of a batched status response. Carries its own `id` so the
 * client can key results and tolerate out-of-order entries. Fields beyond
 * `id` mirror `DocumentStatusResponse` and are optional so per-id error
 * entries (unknown ids) can omit them.
 */
export type DocumentStatusEntry = Partial<DocumentStatusResponse> & {
  id: number;
  /** Per-id failure text (e.g. the requested id is unknown to this vault). */
  error?: string | null;
};

/**
 * Batched status payload returned by GET /documents/status?ids=...
 * (issue #514 / FU-008). One entry per requested id; per-id failures arrive
 * as entries/errors, never as a whole-request failure.
 */
export interface DocumentStatusesResponse {
  results: DocumentStatusEntry[];
  /** Per-id failure entries — same shape as a `results` error entry. */
  errors?: DocumentStatusEntry[];
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
  // Verified producer contract (issue #36): the chat pipeline emits only
  // "distance" or "rerank" for sources. The memory channel carries its own
  // score_type vocabulary (rrf/fts/dense) under UsedMemory/WikiReference.
  score_type?: "distance" | "rerank";
  /**
   * Multi-modal artifact presentation (issue #462). These are first-class safe
   * fields: they NEVER carry paths, bytes, or base64 — artifact bytes are fetched
   * by opaque `artifact_id` via GET /api/documents/artifacts/{id}/raw.
   */
  modality?: string;
  artifact_id?: string;
  asset_id?: string;
  bbox?: { x0: number; y0: number; x1: number; y1: number } | null;
  vision_status?:
    | "used"
    | "proxy_only"
    | "policy_blocked"
    | "asset_missing"
    | "provider_unavailable"
    | "empty_response";
  description?: string;
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

/**
 * Retrieval metadata filter (issue #510 AC-16). Sent as ``metadata_filter``
 * on the chat stream request; only non-empty fields are serialized.
 */
export interface ChatMetadataFilter {
  /** Inclusive lower date bound, ISO yyyy-mm-dd. */
  date_from?: string;
  /** Inclusive upper date bound, ISO yyyy-mm-dd. */
  date_to?: string;
  tags?: string[];
  author?: string;
}

/**
 * Citation-mode enforcement outcome from the done event (issue #510 UI-004).
 * Emitted only when the request ran with citation_mode "required".
 */
export interface CitationEnforcement {
  mode: string;
  status: "satisfied" | "missing_citations";
  detail?: string;
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
  /** Citation confidence scores from the done event (FR-004). */
  onCitationConfidence?: (confidence: Record<string, number>) => void;
  /** Unverifiable claims flagged by the citation validator from the done event (FR-004). */
  onUnverifiableClaims?: (claims: string[]) => void;
  /** Currency/supersession warnings from the done event (issue #510 AC-17). */
  onCurrencyWarnings?: (warnings: string[]) => void;
  /** Citation-mode enforcement outcome from the done event (issue #510 UI-004). */
  onCitationEnforcement?: (enforcement: CitationEnforcement) => void;
  /** Resolved chat mode reported by the backend at the start of the stream. */
  onMode?: (mode: "instant" | "thinking") => void;
  /** Pipeline stage event (Searching / Reading / Drafting) before content streams. */
  onStage?: (stage: string) => void;
  /**
   * Retrieved-but-not-yet-cited evidence candidates from the versioned
   * "evidence" SSE event (issue #508). Fires mid-stream, before content.
   */
  onEvidenceCandidates?: (candidates: Source[]) => void;
  /**
   * Provider reasoning deltas from the additive "reasoning_delta" SSE event
   * (issue #554). Fires mid-stream, typically before the answer content.
   * Optional so older consumers treat the new event type as inert.
   */
  onReasoning?: (chunk: string) => void;
  /**
   * Reasoning accounting from the done event's llm_metrics (issue #554):
   * the provider-side reasoning span and token estimate, when the backend
   * reports them. Fires just before onComplete.
   */
  onReasoningMetrics?: (metrics: {
    durationMs?: number;
    tokensEstimate?: number;
  }) => void;
  /**
   * Reason the provider stopped generating, from the done event's
   * llm_metrics.finish_reason (issue #573 AC2): "length" marks a response
   * truncated at max_tokens and surfaces the Continue action. Fired only
   * when the backend reports a non-empty reason, just before onComplete.
   */
  onFinishReason?: (reason: string) => void;
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
  /** Durable per-session message order (issue #507). Null on rows saved before the column existed. */
  seq?: number | null;
  /** Client-generated UUID linking a turn's user+assistant rows. Null on legacy rows. */
  turn_id?: string | null;
  /** Assistant terminal state. Null on legacy rows renders as complete.
   * "pending" (issue #553) marks a turn the server pre-wrote but never
   * finalized (in-flight from another client, or a crash before stream end). */
  status?: "pending" | "complete" | "partial" | "interrupted" | "failed" | null;
  /** Citation confidence scores persisted with the answer (DEEP-D-01). */
  citation_confidence?: Record<string, number> | null;
  /** Unverifiable claims persisted with the answer (DEEP-D-01). */
  unverifiable_claims?: string[] | null;
  /** Supersession/currency advisories persisted with the answer (issue #510). */
  currency_warnings?: string[] | null;
  /** Required-citations enforcement status persisted with the answer (issue #510). */
  citation_enforcement?: CitationEnforcement | null;
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
  /** Durable turn linkage (issue #507): shared by a turn's user+assistant rows. */
  turn_id?: string;
  /** Assistant terminal state persisted with the row. */
  status?: "complete" | "partial" | "interrupted" | "failed";
  /** Citation confidence scores persisted with the answer (DEEP-D-01). */
  citation_confidence?: Record<string, number>;
  /** Unverifiable claims persisted with the answer (DEEP-D-01). */
  unverifiable_claims?: string[];
  /** Supersession/currency advisories persisted with the answer (issue #510). */
  currency_warnings?: string[];
  /** Required-citations enforcement status persisted with the answer (issue #510). */
  citation_enforcement?: CitationEnforcement;
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

/**
 * Cancel a running or queued document ingest (issue #783's endpoint,
 * `POST /documents/{file_id}/cancel`). The server returns 200 with the
 * terminal status; 409 when the ingest already finished (nothing to cancel).
 */
export async function cancelDocumentIngest(
  fileId: string | number
): Promise<{ file_id: number; status: string }> {
  const response = await apiClient.post<{ file_id: number; status: string }>(
    `/documents/${encodeURIComponent(fileId)}/cancel`,
    null
  );
  return response.data;
}

/** One embedding-reindex job row from `GET /documents/reindex/jobs` (issue #784). */
export interface ReindexJobSummary {
  id: number;
  vault_id?: number | null;
  trigger_type?: string | null;
  trigger_id?: string | null;
  status: string;
  error?: string | null;
  result_json?: string | null;
  input_json?: string | null;
  retry_count?: number;
  created_at?: string | null;
  started_at?: string | null;
  completed_at?: string | null;
}

/**
 * List recent embedding-reindex jobs (admin route; non-admin callers get 403
 * and callers treat that as "family hidden", per the Activity tray contract).
 */
export async function listReindexJobs(): Promise<{ jobs: ReindexJobSummary[] }> {
  const response = await apiClient.get<{ jobs: ReindexJobSummary[] }>(
    "/documents/reindex/jobs"
  );
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

/**
 * Server-side per-request bound for the batched status route
 * (BATCHED_STATUS_MAX_IDS in backend/app/api/routes/documents.py): requests
 * above 100 ids are rejected with 400, so the client pages larger id sets
 * through multiple requests and merges the envelopes.
 */
export const BATCHED_STATUS_MAX_IDS = 100;

/**
 * Batched document status (issue #514 / FU-008): one logical call for N ids
 * via GET /documents/status?ids=<id>,<id>,... Entries carry their own id so
 * out-of-order responses still converge per document. Ids are joined into a
 * comma list (the endpoint's documented wire form; axios would serialize an
 * array as repeated params otherwise).
 *
 * Id sets larger than the server's 100-id request cap are split into
 * multiple concurrent requests and their results/errors merged — a queue
 * bigger than one request (a >100-file bulk upload) must keep monitoring
 * instead of failing the whole poll with 400.
 */
export async function getDocumentStatuses(
  ids: Array<string | number>,
  vaultId?: number
): Promise<DocumentStatusesResponse> {
  const slices: Array<Array<string | number>> = [];
  for (let i = 0; i < ids.length; i += BATCHED_STATUS_MAX_IDS) {
    slices.push(ids.slice(i, i + BATCHED_STATUS_MAX_IDS));
  }
  const responses = await Promise.all(
    slices.map(async (slice) => {
      const response = await apiClient.get<DocumentStatusesResponse>(
        "/documents/status",
        {
          params: {
            ids: slice.map(String).join(","),
            ...(vaultId != null && { vault_id: vaultId }),
          },
        }
      );
      const data = response.data;
      // The batched route serializes the entries under `results`; `documents`
      // is the alternate envelope key the API may emit.
      const results =
        data.results ?? (data as { documents?: DocumentStatusEntry[] }).documents ?? [];
      return { ...data, results };
    })
  );
  return {
    results: responses.flatMap((r) => r.results),
    errors: responses.some((r) => r.errors?.length)
      ? responses.flatMap((r) => r.errors ?? [])
      : undefined,
  };
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

/** Fetch a safe raster artifact asset by opaque id (issue #462). */
export async function getArtifactRawBlob(
  artifactId: string,
  signal?: AbortSignal
): Promise<Blob> {
  const response = await apiClient.get<Blob>(
    `/documents/artifacts/${artifactId}/raw`,
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

export interface GroupMember {
  id: number;
  username: string;
  full_name: string | null;
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

export default apiClient;
