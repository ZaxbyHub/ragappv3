import { apiClient } from "./core";

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

export interface UpdateMemoryRequest {
  content?: string;
  category?: string;
  tags?: string;
  source?: string;
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
