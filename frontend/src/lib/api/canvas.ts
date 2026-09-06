import { apiClient } from "./core";

// ============================================================================
// Canvas — versioned code/document artifacts (issue #509)
//
// Wire contract: POST/GET /chat/sessions/{id}/artifacts (create/list) and the
// /canvas/* artifact endpoints. All canvas client code lives here so the
// backend contract has a single frontend owner.
// ============================================================================

export const CANVAS_KINDS = ["code", "document"] as const;
export type CanvasKind = (typeof CANVAS_KINDS)[number];

export const CANVAS_VERSION_ORIGINS = [
  "created",
  "user_edit",
  "model_edit",
  "restore",
] as const;
export type CanvasVersionOrigin = (typeof CANVAS_VERSION_ORIGINS)[number];

export interface CanvasCapabilities {
  enabled: boolean;
}

/** Snapshot of a cited source captured at artifact-create time. */
export interface SourceRef {
  source_id: string | null;
  title: string | null;
}

export interface CanvasArtifact {
  artifact_uid: string;
  session_id: number;
  message_id: number | null;
  turn_id: string | null;
  kind: CanvasKind;
  name: string;
  language: string | null;
  current_version_no: number;
  source_refs: SourceRef[];
  created_at: string;
  updated_at: string;
}

export interface ModelEditInfo {
  start_line: number;
  end_line: number;
  instruction: string;
  base_version_no: number;
}

export interface CanvasVersionSummary {
  version_no: number;
  name: string | null;
  origin: CanvasVersionOrigin;
  model_edit: ModelEditInfo | null;
  content_sha256: string;
  created_at: string;
}

export type CanvasVersion = CanvasVersionSummary & {
  content: string;
};

export interface CanvasManifest {
  artifact_uid: string;
  kind: CanvasKind;
  name: string;
  language: string | null;
  version_no: number;
  version_name: string | null;
  content: string;
  content_sha256: string;
  source_refs: SourceRef[];
  session_id: number;
  turn_id: string | null;
  message_id: number | null;
  exported_at: string;
}

// ============================================================================
// Canvas — request DTOs
// ============================================================================

export interface CreateCanvasArtifactRequest {
  kind: CanvasKind;
  name: string;
  language?: string | null;
  content: string;
  message_id?: number | null;
  turn_id?: string | null;
  source_refs?: SourceRef[];
}

export interface SaveCanvasVersionRequest {
  content: string;
  name?: string | null;
  base_version_no: number;
  force?: boolean;
}

export interface RestoreCanvasVersionRequest {
  version_no: number;
  base_version_no: number;
}

export interface EditCanvasRangeRequest {
  start_line: number;
  end_line: number;
  instruction: string;
  base_version_no: number;
}

export interface CanvasArtifactResponse {
  artifact: CanvasArtifact;
  version: CanvasVersion;
}

/**
 * Raw detail/create payload as the backend serializes it: the artifact row is
 * a superset of `CanvasArtifact` (extra `vault_id`, `created_by`, …) and the
 * full version is embedded as `current_version` (contract delta #4); a
 * top-level `version` key is also accepted for tolerance. Normalized by
 * `normalizeCanvasDetail` before it reaches the UI.
 */
export interface CanvasDetailPayload {
  artifact: CanvasArtifact;
  current_version?: CanvasVersion;
}

/** Normalizes a raw detail/create payload into `{artifact, version}`. */
export function normalizeCanvasDetail(payload: CanvasDetailPayload): CanvasArtifactResponse {
  const version = payload.current_version;
  if (!version || !payload.artifact) {
    throw new Error("Canvas detail response is missing its version payload");
  }
  return { artifact: payload.artifact, version };
}

/**
 * Maps a fenced-code-block language to the backend download-extension key
 * (contract delta #2: keys are FILE EXTENSIONS — "py", not "python").
 * Unknown/long languages pass through as a lowercased short form, or null
 * when nothing sensible can be derived.
 */
const FENCE_LANGUAGE_TO_EXTENSION: Record<string, string> = {
  python: "py",
  typescript: "ts",
  javascript: "js",
  jsx: "jsx",
  tsx: "tsx",
  shell: "sh",
  bash: "sh",
  sh: "sh",
  zsh: "sh",
  "c++": "cpp",
  cpp: "cpp",
  "c#": "cs",
  csharp: "cs",
  c: "c",
  ruby: "rb",
  golang: "go",
  go: "go",
  rust: "rs",
  kotlin: "kt",
  swift: "swift",
  php: "php",
  java: "java",
  sql: "sql",
  html: "html",
  css: "css",
  json: "json",
  yaml: "yaml",
  yml: "yaml",
  xml: "xml",
  toml: "toml",
  markdown: "md",
  md: "md",
};

export function mapFenceLanguageToExtension(language: string): string | null {
  const normalized = language.trim().toLowerCase();
  if (!normalized) return null;
  const mapped = FENCE_LANGUAGE_TO_EXTENSION[normalized];
  if (mapped) return mapped;
  // Unknown language: pass through only a short alphanumeric form, else omit.
  return /^[a-z0-9+#]{1,8}$/.test(normalized) ? normalized : null;
}

// ============================================================================
// Canvas — error helpers
//
// `core.ts`'s response interceptor normalizes axios errors into a plain
// `Error` carrying `.status` and `.originalError` (the original axios error).
// These helpers read the backend's lowercase `detail` code from there, with
// the same defensive fallbacks as `parseDraftRoomError`.
// ============================================================================

interface NormalizedApiError {
  message?: string;
  status?: number;
  originalError?: {
    response?: {
      status?: number;
      data?: {
        detail?: string;
      };
    };
  };
}

function isNormalizedApiError(error: unknown): error is NormalizedApiError {
  return typeof error === "object" && error !== null;
}

export function getCanvasErrorStatus(error: unknown): number | undefined {
  if (!isNormalizedApiError(error)) return undefined;
  return error.status ?? error.originalError?.response?.status;
}

export function getCanvasErrorDetail(error: unknown): string {
  if (!isNormalizedApiError(error)) return "An unexpected error occurred";
  const detail = error.originalError?.response?.data?.detail;
  if (typeof detail === "string" && detail) return detail;
  return typeof error.message === "string" && error.message
    ? error.message
    : "An unexpected error occurred";
}

/**
 * True when the error is the save-conflict signal: HTTP 409 with the
 * `canvas_version_conflict` detail (a 409 with any other detail is treated as
 * a conflict too — the backend only emits 409 for this case, but failing
 * closed on status keeps the banner honest if that ever changes).
 */
export function isCanvasVersionConflict(error: unknown): boolean {
  return getCanvasErrorStatus(error) === 409;
}

// ============================================================================
// Canvas — React Query key factory
// ============================================================================

export const canvasKeys = {
  all: ["canvas"] as const,
  capabilities: () => [...canvasKeys.all, "capabilities"] as const,
  sessionArtifacts: (sessionId: number) =>
    [...canvasKeys.all, "session", sessionId, "artifacts"] as const,
  artifact: (artifactUid: string) => [...canvasKeys.all, "artifact", artifactUid] as const,
  versions: (artifactUid: string) =>
    [...canvasKeys.artifact(artifactUid), "versions"] as const,
  version: (artifactUid: string, versionNo: number) =>
    [...canvasKeys.versions(artifactUid), versionNo] as const,
};

// ============================================================================
// Canvas — API functions
// ============================================================================

export async function getCanvasCapabilities(): Promise<CanvasCapabilities> {
  const response = await apiClient.get<CanvasCapabilities>("/canvas/capabilities");
  return response.data;
}

export async function createCanvasArtifact(
  sessionId: number,
  payload: CreateCanvasArtifactRequest
): Promise<CanvasArtifactResponse> {
  const response = await apiClient.post<CanvasDetailPayload>(
    `/chat/sessions/${sessionId}/artifacts`,
    payload
  );
  return normalizeCanvasDetail(response.data);
}

export async function listSessionArtifacts(
  sessionId: number
): Promise<{ artifacts: CanvasArtifact[] }> {
  const response = await apiClient.get<{ artifacts: CanvasArtifact[] }>(
    `/chat/sessions/${sessionId}/artifacts`
  );
  return response.data;
}

export async function getCanvasArtifact(artifactUid: string): Promise<CanvasArtifactResponse> {
  const response = await apiClient.get<CanvasDetailPayload>(
    `/canvas/artifacts/${encodeURIComponent(artifactUid)}`
  );
  return normalizeCanvasDetail(response.data);
}

export async function listCanvasVersions(
  artifactUid: string
): Promise<{ versions: CanvasVersionSummary[] }> {
  const response = await apiClient.get<{ versions: CanvasVersionSummary[] }>(
    `/canvas/artifacts/${encodeURIComponent(artifactUid)}/versions`
  );
  return response.data;
}

export async function getCanvasVersion(
  artifactUid: string,
  versionNo: number
): Promise<CanvasArtifactResponse> {
  const response = await apiClient.get<CanvasDetailPayload>(
    `/canvas/artifacts/${encodeURIComponent(artifactUid)}/versions/${versionNo}`
  );
  return normalizeCanvasDetail(response.data);
}

/**
 * User save. Returns the NEW VERSION OBJECT directly (contract delta #3 —
 * the response root IS the version, not an `{artifact, version}` envelope).
 * Rejects with a 409 `canvas_version_conflict` when `base_version_no` is
 * stale unless `force` is true.
 */
export async function saveCanvasVersion(
  artifactUid: string,
  payload: SaveCanvasVersionRequest
): Promise<CanvasVersion> {
  const response = await apiClient.post<CanvasVersion>(
    `/canvas/artifacts/${encodeURIComponent(artifactUid)}/versions`,
    payload
  );
  return response.data;
}

/** Restore. Returns the appended version directly; 409 on a stale base has
 * NO force escape hatch — the UI must offer "reload latest" instead. */
export async function restoreCanvasVersion(
  artifactUid: string,
  payload: RestoreCanvasVersionRequest
): Promise<CanvasVersion> {
  const response = await apiClient.post<CanvasVersion>(
    `/canvas/artifacts/${encodeURIComponent(artifactUid)}/restore`,
    payload
  );
  return response.data;
}

/** Targeted model edit of a line range. Returns the appended version
 * directly; 422 `canvas_invalid_range` / 502 `canvas_model_unavailable` on
 * failure. */
export async function editCanvasRange(
  artifactUid: string,
  payload: EditCanvasRangeRequest,
  signal?: AbortSignal
): Promise<CanvasVersion> {
  const response = await apiClient.post<CanvasVersion>(
    `/canvas/artifacts/${encodeURIComponent(artifactUid)}/edit-range`,
    payload,
    { signal }
  );
  return response.data;
}

const DEFAULT_DOWNLOAD_FILENAME = "canvas.txt";

function parseContentDispositionFilename(headerValue: unknown): string {
  if (typeof headerValue !== "string" || !headerValue) return DEFAULT_DOWNLOAD_FILENAME;
  // Matches filename="quoted value" or filename=unquoted-value (RFC 6266, no ext params).
  const match = /filename\s*=\s*(?:"([^"]*)"|([^;]+))/i.exec(headerValue);
  const rawName = match ? (match[1] ?? match[2])?.trim() : undefined;
  return rawName || DEFAULT_DOWNLOAD_FILENAME;
}

export interface CanvasDownloadResult {
  blob: Blob;
  filename: string;
}

/**
 * Fetches a single version's exact bytes (utf-8, no normalization). Blob error
 * bodies have no `.detail` after the interceptor normalizes them — when the
 * response body is a Blob, read its text so the caller still receives the
 * backend's lowercase detail code (mirrors core.ts's blob-body CSRF handling).
 * Does NOT trigger the browser download — returns the blob for the caller.
 */
export async function downloadCanvasVersion(
  artifactUid: string,
  versionNo: number
): Promise<CanvasDownloadResult> {
  try {
    const response = await apiClient.get<Blob>(
      `/canvas/artifacts/${encodeURIComponent(artifactUid)}/versions/${versionNo}/download`,
      { responseType: "blob" }
    );
    const headers = response.headers as Record<string, unknown> | undefined;
    let disposition = "";
    for (const key of Object.keys(headers ?? {})) {
      if (key.toLowerCase() === "content-disposition") {
        disposition = typeof headers?.[key] === "string" ? (headers[key] as string) : "";
      }
    }
    return {
      blob: response.data,
      filename: parseContentDispositionFilename(disposition),
    };
  } catch (error) {
    const data = isNormalizedApiError(error)
      ? (error.originalError?.response?.data as unknown)
      : undefined;
    if (typeof Blob !== "undefined" && data instanceof Blob) {
      let detail = "";
      try {
        detail = JSON.parse(await data.text())?.detail ?? "";
      } catch {
        // non-JSON body — keep detail empty and fall through to the original error
      }
      if (typeof detail === "string" && detail) {
        const enriched = new Error(detail);
        enriched.name = "APIError";
        (enriched as unknown as { status?: number }).status =
          getCanvasErrorStatus(error);
        (enriched as unknown as { originalError?: unknown }).originalError = error;
        throw enriched;
      }
    }
    throw error;
  }
}

export async function exportCanvasManifest(
  artifactUid: string,
  versionNo: number
): Promise<CanvasManifest> {
  const response = await apiClient.get<CanvasManifest>(
    `/canvas/artifacts/${encodeURIComponent(artifactUid)}/export`,
    { params: { version_no: versionNo } }
  );
  return response.data;
}
