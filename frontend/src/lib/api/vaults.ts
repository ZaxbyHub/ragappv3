import { apiClient } from "./core";

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
  enrichment_enabled?: boolean | null;
  effective_enrichment_enabled: boolean;
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

export interface VaultEnrichmentToggleRequest {
  enabled: boolean | null;
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

export async function toggleVaultEnrichment(
  vaultId: number,
  request: VaultEnrichmentToggleRequest,
): Promise<Vault> {
  const response = await apiClient.put<Vault>(
    `/vaults/${vaultId}/enrichment-toggle`,
    request,
  );
  return response.data;
}
