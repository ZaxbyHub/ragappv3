import { apiClient } from "./core";

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
  // DD-C020 optimistic-locking version (issue #276 1X-1). The backend
  // returns this on every WikiPage response (wiki.py:255) and rejects
  // conflicting updates with HTTP 409 when the client sends a stale
  // expected_version.
  version: number;
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

// ============================================================================
// Wiki Functions
// ============================================================================

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
  // DD-C020 optimistic-locking guard (issue #276 1X-1). When set, the backend
  // (wiki.py:99/246) compares this to the stored version and rejects the
  // update with HTTP 409 if they differ. Omit to skip the conflict check.
  expected_version?: number;
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
