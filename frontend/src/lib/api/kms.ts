import { apiClient, getDocumentRawBlob } from "./core";

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
