import { apiClient, Group, GroupListResponse, GroupMember, GroupVault, VaultAccessItem } from "./core";

// ---------------------------------------------------------------------------
// Groups
// ---------------------------------------------------------------------------

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
  const request = { name, description, org_id: orgId };
  const response = await apiClient.post<Group>("/groups", request);
  return response.data;
}

export async function updateGroup(
  groupId: number,
  name: string,
  description: string | null
): Promise<Group> {
  const request = { name, description };
  const response = await apiClient.put<Group>(`/groups/${groupId}`, request);
  return response.data;
}

export async function deleteGroup(groupId: number): Promise<void> {
  await apiClient.delete(`/groups/${groupId}`);
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
