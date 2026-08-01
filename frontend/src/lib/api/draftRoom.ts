import { apiClient, API_BASE_URL } from "./core";

// ============================================================================
// Draft Room — enums (const tuples + derived union types)
//
// Do not redefine these locally in components — import from here so status
// checks and filters stay in sync with the backend contract.
// ============================================================================

export const DRAFT_STATUSES = [
  "draft",
  "queued",
  "running",
  "needs_review",
  "ready",
  "failed",
  "cancelled",
  "archived",
] as const;
export type DraftStatus = (typeof DRAFT_STATUSES)[number];

export const DRAFT_JOB_STATUSES = ["pending", "running", "completed", "failed", "cancelled"] as const;
export type DraftJobStatus = (typeof DRAFT_JOB_STATUSES)[number];

export const DRAFT_INPUT_PARSE_STATUSES = ["pending", "parsing", "ready", "failed", "cancelled"] as const;
export type DraftInputParseStatus = (typeof DRAFT_INPUT_PARSE_STATUSES)[number];

export const DRAFT_MODES = ["rewrite", "compose"] as const;
export type DraftMode = (typeof DRAFT_MODES)[number];

export const DRAFT_TIERS = ["standard", "high_stakes", "sensitive"] as const;
export type DraftTier = (typeof DRAFT_TIERS)[number];

export const DRAFT_INPUT_ROLES = ["manuscript", "reference", "style", "background", "challenge"] as const;
export type DraftInputRole = (typeof DRAFT_INPUT_ROLES)[number];

export const DRAFT_INPUT_AUTHORITIES = [
  "primary",
  "official",
  "secondary",
  "user_asserted",
  "unknown",
] as const;
export type DraftInputAuthority = (typeof DRAFT_INPUT_AUTHORITIES)[number];

export const DRAFT_JOB_TYPES = ["parse_input", "compile"] as const;
export type DraftJobType = (typeof DRAFT_JOB_TYPES)[number];

export const DRAFT_REVISION_SOURCES = ["pipeline", "manual"] as const;
export type DraftRevisionSource = (typeof DRAFT_REVISION_SOURCES)[number];

export const DRAFT_STAGE_NAMES = [
  "intake",
  "research",
  "outline",
  "draft",
  "lint",
  "copy",
  "standards",
  "fact",
  "assemble",
] as const;
export type DraftStageName = (typeof DRAFT_STAGE_NAMES)[number];

export const DRAFT_STAGE_STATUSES = [
  "pending",
  "running",
  "completed",
  "failed",
  "skipped",
  "cancelled",
] as const;
export type DraftStageStatus = (typeof DRAFT_STAGE_STATUSES)[number];

export const DRAFT_EVIDENCE_SOURCE_KINDS = ["draft_input", "document", "wiki", "kms"] as const;
export type DraftEvidenceSourceKind = (typeof DRAFT_EVIDENCE_SOURCE_KINDS)[number];

export const DRAFT_CLAIM_TYPES = ["factual", "quote", "opinion"] as const;
export type DraftClaimType = (typeof DRAFT_CLAIM_TYPES)[number];

export const DRAFT_CLAIM_STATUSES = [
  "supported",
  "contradicted",
  "ambiguous",
  "stale",
  "unsupported",
  "opinion",
] as const;
export type DraftClaimStatus = (typeof DRAFT_CLAIM_STATUSES)[number];

export const DRAFT_CLAIM_SEVERITIES = ["info", "warning", "blocker"] as const;
export type DraftClaimSeverity = (typeof DRAFT_CLAIM_SEVERITIES)[number];

export const DRAFT_CLAIM_RESOLUTIONS = ["open", "resolved_by_revision", "accepted", "waived"] as const;
export type DraftClaimResolution = (typeof DRAFT_CLAIM_RESOLUTIONS)[number];

export const DRAFT_CLAIM_SOURCE_RELATIONSHIPS = ["supports", "contradicts", "context"] as const;
export type DraftClaimSourceRelationship = (typeof DRAFT_CLAIM_SOURCE_RELATIONSHIPS)[number];

export const DRAFT_FINDING_CATEGORIES = [
  "boilerplate",
  "style",
  "preservation",
  "factuality",
  "quote",
  "conflict",
  "security",
  "operational",
] as const;
export type DraftFindingCategory = (typeof DRAFT_FINDING_CATEGORIES)[number];

export const DRAFT_FINDING_SEVERITIES = ["info", "warning", "blocker"] as const;
export type DraftFindingSeverity = (typeof DRAFT_FINDING_SEVERITIES)[number];

export const DRAFT_FINDING_STATUSES = [
  "open",
  "applied",
  "dismissed",
  "waived",
  "resolved_by_revision",
] as const;
export type DraftFindingStatus = (typeof DRAFT_FINDING_STATUSES)[number];

export const DRAFT_VAULT_ACCESS_LEVELS = ["write", "read", "revoked"] as const;
export type DraftVaultAccess = (typeof DRAFT_VAULT_ACCESS_LEVELS)[number];

export const DRAFT_FACT_STATUSES = ["not_run", "running", "passed", "findings", "invalidated"] as const;
export type DraftFactStatus = (typeof DRAFT_FACT_STATUSES)[number];

export const DRAFT_COMPILE_START_STAGES = [
  "research",
  "outline",
  "draft",
  "lint",
  "copy",
  "standards",
  "fact",
] as const;
export type DraftCompileStartStage = (typeof DRAFT_COMPILE_START_STAGES)[number];

export const DRAFT_PROMOTE_SOURCE_TYPES = ["input", "revision"] as const;
export type DraftPromoteSourceType = (typeof DRAFT_PROMOTE_SOURCE_TYPES)[number];

/** Claim statuses that block a revision from being marked ready. */
export const BLOCKING_CLAIM_STATUSES = ["contradicted", "unsupported", "ambiguous", "stale"] as const;

/** Fact-check statuses that count as "current" (fact stage ran against this content). */
export const FACT_CURRENT_STATUSES = ["passed", "findings"] as const;

// ============================================================================
// Draft Room — DTOs
// ============================================================================

export interface DraftBrief {
  piece_type: string;
  audience: string;
  purpose: string;
  tone: string;
  target_words: number;
  transformation_strength: string;
  primary_input_id: number | null;
  must_include: string[];
  must_avoid: string[];
  preserve_quotes: boolean;
  preserve_numbers: boolean;
  preserve_uncertainty: boolean;
  drafting_priority: string;
  additional_instructions: string;
}

export interface DraftSummary {
  id: number;
  vault_id: number;
  vault_access: DraftVaultAccess;
  title: string;
  mode: DraftMode;
  status: DraftStatus;
  tier: DraftTier;
  lock_version: number;
  current_revision_id: number | null;
  active_job_id: number | null;
  input_count: number;
  open_blocker_count: number;
  created_at: string;
  updated_at: string;
  ready_at: string | null;
  /**
   * Optional-nullable rather than required: `GET /drafts/{id}` (the detail
   * endpoint) always populates both, but the list endpoint always leaves
   * `ready_by_username` `null` (resolving it per row would be an N+1 query
   * for a field the list doesn't show), and older fixtures across the
   * codebase predate these fields entirely. Never render a bare id as a
   * person's name — a `null` username with a present `ready_by` means either
   * "the list didn't resolve it" (list endpoint) or "that account no longer
   * exists" (detail endpoint); callers must know which endpoint they're
   * reading from to tell those apart.
   */
  ready_by?: number | null;
  ready_by_username?: string | null;
}

export interface DraftInput {
  id: number;
  role: DraftInputRole;
  authority: DraftInputAuthority;
  as_of_date: string | null;
  original_name: string;
  extension: string;
  media_type: string | null;
  size_bytes: number;
  content_sha256: string;
  parse_status: DraftInputParseStatus;
  parse_error: string | null;
  parsed_char_count: number | null;
  active_parse_job_id: number | null;
  last_parse_job_id: number | null;
  created_at: string;
}

export interface DraftInputContent {
  input_id: number;
  parse_status: DraftInputParseStatus;
  parsed_text: string | null;
}

export interface DraftJob {
  id: number;
  draft_id: number;
  job_type: DraftJobType;
  status: DraftJobStatus;
  start_stage: DraftStageName | null;
  active_stage: DraftStageName | null;
  progress_percent: number;
  model_call_count: number;
  max_model_calls: number;
  retry_count: number;
  parent_job_id: number | null;
  attempt_no: number;
  compile_input_sha256: string | null;
  prompt_bundle_version: string | null;
  timeout_seconds: number;
  cancel_requested_at: string | null;
  heartbeat_at: string | null;
  error_code: string | null;
  error_message: string | null;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
}

export interface DraftRevisionSummary {
  id: number;
  revision_no: number;
  parent_revision_id: number | null;
  job_id: number | null;
  source: DraftRevisionSource;
  content_sha256: string;
  fact_status: DraftFactStatus;
  is_current: boolean;
  created_by: number | null;
  created_at: string;
}

export interface DraftRevisionDetail {
  summary: DraftRevisionSummary;
  content_md: string;
  sections: unknown[];
  citations: unknown[];
  qa_summary: Record<string, unknown>;
}

export interface DraftDetail {
  summary: DraftSummary;
  brief: DraftBrief;
  inputs: DraftInput[];
  current_revision_summary: DraftRevisionSummary | null;
  active_compile_job: DraftJob | null;
  revision_count: number;
  evidence_count: number;
  claim_counts_by_status: Record<string, number>;
  finding_counts_by_severity: Record<string, number>;
}

export interface DraftInputUploadResponse {
  input: DraftInput;
  job: DraftJob;
}

export interface DraftStage {
  id: number;
  job_id: number;
  stage: DraftStageName;
  attempt: number;
  status: DraftStageStatus;
  input_sha256: string;
  artifact_sha256: string | null;
  candidate_sha256: string | null;
  semantic_changed: boolean;
  prompt_id: string | null;
  prompt_version: string | null;
  prompt_sha256: string | null;
  model_name: string | null;
  temperature: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
  error_code: string | null;
  error_message: string | null;
  started_at: string | null;
  completed_at: string | null;
  artifact: unknown;
  /** Only present when the request set `include_content=true`. */
  content_md: string | null;
}

export interface DraftEvidence {
  id: number;
  job_id: number;
  label: string;
  source_kind: DraftEvidenceSourceKind;
  title: string;
  passage: string;
  passage_sha256: string;
  source_content_sha256: string;
  draft_input_id: number | null;
  file_id: number | null;
  wiki_page_id: number | null;
  wiki_claim_id: number | null;
  kms_entry_id: number | null;
  chunk_uid: string | null;
  page_number: number | null;
  section: string | null;
  retrieval_score: number | null;
  authority: DraftInputAuthority;
  as_of_date: string | null;
  source_updated_at: string | null;
  source_deleted_at: string | null;
  source_deleted: boolean;
}

export interface DraftClaimSource {
  id: number;
  claim_id: number;
  evidence_id: number;
  relationship: DraftClaimSourceRelationship;
  exact_quote: string;
  passage_start: number | null;
  passage_end: number | null;
  /** The only allowed similarity score name on the wire — never confidence/support/entailment. */
  lexical_overlap_score: number | null;
}

export interface DraftClaim {
  id: number;
  revision_id: number;
  ordinal: number;
  claim_text: string;
  claim_sha256: string;
  span_start: number;
  span_end: number;
  claim_type: DraftClaimType;
  status: DraftClaimStatus;
  severity: DraftClaimSeverity;
  rationale: string;
  retrieval_audit: unknown;
  resolution: DraftClaimResolution;
  resolved_by: number | null;
  resolved_at: string | null;
  resolution_note: string | null;
  sources: DraftClaimSource[];
}

export interface DraftFinding {
  id: number;
  draft_id: number;
  revision_id: number | null;
  job_id: number | null;
  stage: DraftStageName;
  rule_id: string;
  rule_version: string;
  category: DraftFindingCategory;
  severity: DraftFindingSeverity;
  status: DraftFindingStatus;
  waivable: boolean;
  message: string;
  original_text: string | null;
  suggestion: string | null;
  span_start: number | null;
  span_end: number | null;
  span_text_sha256: string | null;
  resolved_by: number | null;
  resolved_at: string | null;
  resolution_note: string | null;
  waiver_rule_version: string | null;
  waiver_text_sha256: string | null;
  created_at: string;
  can_apply: boolean;
  can_dismiss: boolean;
  can_waive: boolean;
}

export interface FindingDispositionResponse {
  finding: DraftFinding;
  revision: DraftRevisionSummary | null;
}

export interface DraftRoomCapabilities {
  enabled: boolean;
  modes: DraftMode[];
  tiers: DraftTier[];
  piece_types: string[];
  transformation_strengths: string[];
  limits: Record<string, unknown>;
  export_formats: string[];
  logical_model_modes: string[];
  default_logical_mode: string;
  compile_start_stages: DraftCompileStartStage[];
  compile_stage_order: DraftStageName[];
  prompt_bundle_version: string;
  editorial_gates_installed: boolean;
  compile_available: boolean;
  findings_available: boolean;
  claims_available: boolean;
  evidence_available: boolean;
  ready_available: boolean;
  promote_available: boolean;
}

export interface DraftPaginated<T> {
  items: T[];
  total: number;
  page: number;
  per_page: number;
}

// ============================================================================
// Draft Room — request DTOs
// ============================================================================

export interface DraftCreateRequest {
  vault_id: number;
  title: string;
  mode: DraftMode;
  tier?: DraftTier;
  brief: DraftBrief;
}

export interface DraftUpdateRequest {
  lock_version: number;
  title?: string;
  brief?: DraftBrief;
  tier?: DraftTier;
}

export interface LockedSpan {
  start: number;
  end: number;
  sha256: string;
  reason: string;
}

export interface InputUpdateRequest {
  role?: DraftInputRole;
  authority?: DraftInputAuthority;
  as_of_date?: string;
  clear_as_of_date?: boolean;
  locked_spans?: LockedSpan[];
}

export interface RevisionCreateRequest {
  base_revision_id: number | null;
  lock_version: number;
  content_md: string;
}

export interface CompileRequest {
  base_revision_id: number | null;
  lock_version: number;
  start_stage?: DraftCompileStartStage;
}

export interface RetryJobRequest {
  start_stage?: DraftCompileStartStage;
}

export interface FindingDispositionRequest {
  action: "apply" | "dismiss" | "waive";
  base_revision_id: number | null;
  lock_version: number;
  note?: string;
}

export interface ReadyRequest {
  lock_version: number;
  acknowledge_source_only?: boolean;
}

export interface PromoteRequest {
  source_type: DraftPromoteSourceType;
  source_id: number;
  title: string;
  folder_id?: number | null;
  tag_ids?: number[];
}

export interface PromoteResponse {
  promotion_id: number;
  draft_id: number;
  vault_id: number;
  source_type: DraftPromoteSourceType;
  source_id: number;
  source_sha256: string;
  file_id: number;
  filename: string;
  created_at: string;
}

// ============================================================================
// Draft Room — error envelope helper
// ============================================================================

export interface DraftRoomErrorInfo {
  detail: string;
  code: string;
  context: Record<string, unknown>;
  status?: number;
}

interface NormalizedApiError {
  message?: string;
  status?: number;
  originalError?: {
    response?: {
      data?: {
        detail?: string;
        code?: string;
        context?: Record<string, unknown>;
      };
    };
  };
}

function isNormalizedApiError(error: unknown): error is NormalizedApiError {
  return typeof error === "object" && error !== null;
}

/**
 * Extracts the Draft Room error envelope (`{detail, code, context?}`) from an
 * error thrown by `apiClient`. `core.ts`'s response interceptor normalizes
 * axios errors into a plain `Error` carrying `.status` and `.originalError`
 * (the original axios error) — read the real envelope from there. Never
 * throws; falls back to a safe "unknown" shape for anything unrecognised.
 */
export function parseDraftRoomError(error: unknown): DraftRoomErrorInfo {
  const fallbackDetail =
    isNormalizedApiError(error) && typeof error.message === "string" && error.message
      ? error.message
      : "An unexpected error occurred";

  if (!isNormalizedApiError(error)) {
    return { detail: fallbackDetail, code: "unknown", context: {} };
  }

  const data = error.originalError?.response?.data;
  const detail = typeof data?.detail === "string" && data.detail ? data.detail : fallbackDetail;
  const code = typeof data?.code === "string" && data.code ? data.code : "unknown";
  const context = data?.context && typeof data.context === "object" ? data.context : {};

  return { detail, code, context, status: error.status };
}

/** Predicate for checking a caught error against a known Draft Room error code. */
export function isDraftRoomErrorCode(error: unknown, code: string): boolean {
  return parseDraftRoomError(error).code === code;
}

// ============================================================================
// Draft Room — React Query key factory
// ============================================================================

export const draftRoomKeys = {
  all: ["draft-room"] as const,
  capabilities: () => [...draftRoomKeys.all, "capabilities"] as const,
  lists: () => [...draftRoomKeys.all, "drafts"] as const,
  list: (params: Record<string, unknown>) => [...draftRoomKeys.lists(), params] as const,
  detail: (id: number) => [...draftRoomKeys.all, "draft", id] as const,
  inputs: (id: number) => [...draftRoomKeys.detail(id), "inputs"] as const,
  jobs: (id: number, params?: Record<string, unknown>) =>
    [...draftRoomKeys.detail(id), "jobs", params ?? {}] as const,
  job: (id: number, jobId: number) => [...draftRoomKeys.detail(id), "job", jobId] as const,
  stages: (id: number, jobId: number, includeContent?: boolean) =>
    [...draftRoomKeys.detail(id), "job", jobId, "stages", includeContent ?? false] as const,
  revisions: (id: number, params?: Record<string, unknown>) =>
    [...draftRoomKeys.detail(id), "revisions", params ?? {}] as const,
  revision: (id: number, revisionId: number) =>
    [...draftRoomKeys.detail(id), "revision", revisionId] as const,
  evidence: (id: number, params?: Record<string, unknown>) =>
    [...draftRoomKeys.detail(id), "evidence", params ?? {}] as const,
  claims: (id: number, params?: Record<string, unknown>) =>
    [...draftRoomKeys.detail(id), "claims", params ?? {}] as const,
  findings: (id: number, params?: Record<string, unknown>) =>
    [...draftRoomKeys.detail(id), "findings", params ?? {}] as const,
  promotions: (id: number) => [...draftRoomKeys.detail(id), "promotions"] as const,
};

// ============================================================================
// Draft Room — API functions
// ============================================================================

const DRAFT_ROOM_BASE = "/draft-room";

export async function getDraftRoomCapabilities(): Promise<DraftRoomCapabilities> {
  const response = await apiClient.get<DraftRoomCapabilities>(`${DRAFT_ROOM_BASE}/capabilities`);
  return response.data;
}

export async function createDraft(data: DraftCreateRequest): Promise<DraftSummary> {
  const response = await apiClient.post<DraftSummary>(`${DRAFT_ROOM_BASE}/drafts`, data);
  return response.data;
}

export async function listDrafts(params: {
  vault_id?: number;
  status?: DraftStatus;
  page?: number;
  per_page?: number;
} = {}): Promise<DraftPaginated<DraftSummary>> {
  const response = await apiClient.get<DraftPaginated<DraftSummary>>(`${DRAFT_ROOM_BASE}/drafts`, {
    params,
  });
  return response.data;
}

export async function getDraft(draftId: number): Promise<DraftDetail> {
  const response = await apiClient.get<DraftDetail>(`${DRAFT_ROOM_BASE}/drafts/${draftId}`);
  return response.data;
}

export async function updateDraft(draftId: number, data: DraftUpdateRequest): Promise<DraftSummary> {
  const response = await apiClient.patch<DraftSummary>(`${DRAFT_ROOM_BASE}/drafts/${draftId}`, data);
  return response.data;
}

export async function archiveDraft(draftId: number, lockVersion: number): Promise<DraftSummary> {
  const response = await apiClient.post<DraftSummary>(`${DRAFT_ROOM_BASE}/drafts/${draftId}/archive`, {
    lock_version: lockVersion,
  });
  return response.data;
}

export async function restoreDraft(draftId: number, lockVersion: number): Promise<DraftSummary> {
  const response = await apiClient.post<DraftSummary>(`${DRAFT_ROOM_BASE}/drafts/${draftId}/restore`, {
    lock_version: lockVersion,
  });
  return response.data;
}

export async function deleteDraft(draftId: number): Promise<void> {
  await apiClient.delete(`${DRAFT_ROOM_BASE}/drafts/${draftId}`);
}

export async function uploadDraftInput(
  draftId: number,
  params: {
    file: File;
    role: DraftInputRole;
    authority: DraftInputAuthority;
    as_of_date?: string;
  },
  onUploadProgress?: (progress: number) => void
): Promise<DraftInputUploadResponse> {
  const formData = new FormData();
  formData.append("file", params.file);
  formData.append("role", params.role);
  formData.append("authority", params.authority);
  if (params.as_of_date != null) {
    formData.append("as_of_date", params.as_of_date);
  }

  const response = await apiClient.post<DraftInputUploadResponse>(
    `${DRAFT_ROOM_BASE}/drafts/${draftId}/inputs`,
    formData,
    {
      timeout: 0, // disable timeout for file uploads — large files can take minutes
      headers: { "Content-Type": "" },
      onUploadProgress: (progressEvent) => {
        if (!onUploadProgress) return;
        if (progressEvent.total) {
          onUploadProgress(Math.round((progressEvent.loaded * 100) / progressEvent.total));
        } else {
          // Total unknown - report 0 for indeterminate progress
          onUploadProgress(0);
        }
      },
    }
  );
  return response.data;
}

export async function updateDraftInput(
  draftId: number,
  inputId: number,
  data: InputUpdateRequest
): Promise<DraftInput> {
  const response = await apiClient.patch<DraftInput>(
    `${DRAFT_ROOM_BASE}/drafts/${draftId}/inputs/${inputId}`,
    data
  );
  return response.data;
}

export async function getDraftInputContent(draftId: number, inputId: number): Promise<DraftInputContent> {
  const response = await apiClient.get<DraftInputContent>(
    `${DRAFT_ROOM_BASE}/drafts/${draftId}/inputs/${inputId}/content`
  );
  return response.data;
}

export async function deleteDraftInput(draftId: number, inputId: number): Promise<void> {
  await apiClient.delete(`${DRAFT_ROOM_BASE}/drafts/${draftId}/inputs/${inputId}`);
}

export async function listDraftJobs(
  draftId: number,
  params: { page?: number; per_page?: number } = {}
): Promise<DraftPaginated<DraftJob>> {
  const response = await apiClient.get<DraftPaginated<DraftJob>>(
    `${DRAFT_ROOM_BASE}/drafts/${draftId}/jobs`,
    { params }
  );
  return response.data;
}

export async function getDraftJob(draftId: number, jobId: number): Promise<DraftJob> {
  const response = await apiClient.get<DraftJob>(`${DRAFT_ROOM_BASE}/drafts/${draftId}/jobs/${jobId}`);
  return response.data;
}

export async function getDraftStages(
  draftId: number,
  jobId: number,
  params: { include_content?: boolean; page?: number; per_page?: number } = {}
): Promise<DraftPaginated<DraftStage>> {
  const response = await apiClient.get<DraftPaginated<DraftStage>>(
    `${DRAFT_ROOM_BASE}/drafts/${draftId}/jobs/${jobId}/stages`,
    { params }
  );
  return response.data;
}

export async function cancelDraftJob(draftId: number, jobId: number): Promise<DraftJob> {
  const response = await apiClient.post<DraftJob>(
    `${DRAFT_ROOM_BASE}/drafts/${draftId}/jobs/${jobId}/cancel`
  );
  return response.data;
}

export async function retryDraftJob(
  draftId: number,
  jobId: number,
  data: RetryJobRequest = {}
): Promise<DraftJob> {
  const response = await apiClient.post<DraftJob>(
    `${DRAFT_ROOM_BASE}/drafts/${draftId}/jobs/${jobId}/retry`,
    data
  );
  return response.data;
}

export async function compileDraft(
  draftId: number,
  data: CompileRequest,
  idempotencyKey?: string
): Promise<DraftJob> {
  const response = await apiClient.post<DraftJob>(
    `${DRAFT_ROOM_BASE}/drafts/${draftId}/compile`,
    data,
    idempotencyKey != null ? { headers: { "Idempotency-Key": idempotencyKey } } : undefined
  );
  return response.data;
}

/**
 * Absolute URL for the Draft Room SSE stream. Does NOT open the connection —
 * pass it to the streaming hook (fetch + ReadableStream, per
 * useWikiEventStream.ts; EventSource is unusable here since the JWT lives in
 * memory, not a cookie).
 */
export function getDraftEventsUrl(draftId: number): string {
  return `${API_BASE_URL}${DRAFT_ROOM_BASE}/drafts/${draftId}/events`;
}

export async function listDraftRevisions(
  draftId: number,
  params: { page?: number; per_page?: number } = {}
): Promise<DraftPaginated<DraftRevisionSummary>> {
  const response = await apiClient.get<DraftPaginated<DraftRevisionSummary>>(
    `${DRAFT_ROOM_BASE}/drafts/${draftId}/revisions`,
    { params }
  );
  return response.data;
}

export async function getDraftRevision(draftId: number, revisionId: number): Promise<DraftRevisionDetail> {
  const response = await apiClient.get<DraftRevisionDetail>(
    `${DRAFT_ROOM_BASE}/drafts/${draftId}/revisions/${revisionId}`
  );
  return response.data;
}

export async function createDraftRevision(
  draftId: number,
  data: RevisionCreateRequest
): Promise<DraftRevisionDetail> {
  const response = await apiClient.post<DraftRevisionDetail>(
    `${DRAFT_ROOM_BASE}/drafts/${draftId}/revisions`,
    data
  );
  return response.data;
}

export async function markDraftRevisionReady(
  draftId: number,
  revisionId: number,
  data: ReadyRequest
): Promise<DraftSummary> {
  const response = await apiClient.post<DraftSummary>(
    `${DRAFT_ROOM_BASE}/drafts/${draftId}/revisions/${revisionId}/ready`,
    data
  );
  return response.data;
}

export interface DraftExportResult {
  blob: Blob;
  filename: string;
  factStatus: string;
  approvalStatus: string;
  contentSha256: string;
}

const DEFAULT_EXPORT_FILENAME = "draft.md";

function parseContentDispositionFilename(headerValue: unknown): string {
  if (typeof headerValue !== "string" || !headerValue) return DEFAULT_EXPORT_FILENAME;
  // Matches filename="quoted value" or filename=unquoted-value (RFC 6266, no ext params).
  const match = /filename\s*=\s*(?:"([^"]*)"|([^;]+))/i.exec(headerValue);
  const rawName = match ? (match[1] ?? match[2])?.trim() : undefined;
  return rawName || DEFAULT_EXPORT_FILENAME;
}

function getHeaderCaseInsensitive(
  headers: Record<string, unknown> | undefined,
  name: string
): string {
  if (!headers) return "";
  const lowerName = name.toLowerCase();
  for (const key of Object.keys(headers)) {
    if (key.toLowerCase() === lowerName) {
      const value = headers[key];
      return typeof value === "string" ? value : "";
    }
  }
  return "";
}

/**
 * Exports a revision to markdown. The backend takes query params only (no
 * JSON body) and returns raw `text/markdown` bytes plus fact/approval status
 * headers. This function does NOT trigger the browser download — it returns
 * the blob and parsed metadata for the caller (UI) to act on.
 */
export async function exportDraftRevision(
  draftId: number,
  revisionId: number,
  params: { format?: string; acknowledge_not_fact_checked?: boolean } = {}
): Promise<DraftExportResult> {
  const response = await apiClient.post(
    `${DRAFT_ROOM_BASE}/drafts/${draftId}/revisions/${revisionId}/export`,
    undefined,
    { params, responseType: "blob" }
  );
  const headers = response.headers as Record<string, unknown> | undefined;
  return {
    blob: response.data as Blob,
    filename: parseContentDispositionFilename(getHeaderCaseInsensitive(headers, "content-disposition")),
    factStatus: getHeaderCaseInsensitive(headers, "x-draft-fact-status"),
    approvalStatus: getHeaderCaseInsensitive(headers, "x-draft-approval-status"),
    contentSha256: getHeaderCaseInsensitive(headers, "x-draft-content-sha256"),
  };
}

export async function listDraftEvidence(
  draftId: number,
  params: { job_id?: number; page?: number; per_page?: number } = {}
): Promise<DraftPaginated<DraftEvidence>> {
  const response = await apiClient.get<DraftPaginated<DraftEvidence>>(
    `${DRAFT_ROOM_BASE}/drafts/${draftId}/evidence`,
    { params }
  );
  return response.data;
}

export async function listDraftClaims(
  draftId: number,
  params: { revision_id?: number; status?: DraftClaimStatus; page?: number; per_page?: number } = {}
): Promise<DraftPaginated<DraftClaim>> {
  const response = await apiClient.get<DraftPaginated<DraftClaim>>(
    `${DRAFT_ROOM_BASE}/drafts/${draftId}/claims`,
    { params }
  );
  return response.data;
}

export async function listDraftFindings(
  draftId: number,
  params: {
    revision_id?: number;
    status?: DraftFindingStatus;
    severity?: DraftFindingSeverity;
    page?: number;
    per_page?: number;
  } = {}
): Promise<DraftPaginated<DraftFinding>> {
  const response = await apiClient.get<DraftPaginated<DraftFinding>>(
    `${DRAFT_ROOM_BASE}/drafts/${draftId}/findings`,
    { params }
  );
  return response.data;
}

export async function setDraftFindingDisposition(
  draftId: number,
  findingId: number,
  data: FindingDispositionRequest
): Promise<FindingDispositionResponse> {
  const response = await apiClient.post<FindingDispositionResponse>(
    `${DRAFT_ROOM_BASE}/drafts/${draftId}/findings/${findingId}/disposition`,
    data
  );
  return response.data;
}

export async function promoteDraftSource(draftId: number, data: PromoteRequest): Promise<PromoteResponse> {
  const response = await apiClient.post<PromoteResponse>(
    `${DRAFT_ROOM_BASE}/drafts/${draftId}/promote`,
    data
  );
  return response.data;
}
