import { apiClient } from "./core";

// ============================================================================
// Vault-Group Functions
// ============================================================================

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
