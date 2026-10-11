import { apiClient, UserListItem } from "./core";

// #780 (L09): GET /users/{id}/groups returns the route's four-field group
// summary ({id, name, description, org_id} — see backend get_user_groups);
// it never sends the full Group shape (created_at/organization_name) that
// POST/PUT /groups return and the shared `Group` interface types.
export interface UserGroupSummary {
  id: number;
  name: string;
  description: string;
  org_id: number;
}

export async function listAllUsers(): Promise<UserListItem[]> {
  const response = await apiClient.get<{ users: UserListItem[] }>("/users");
  return response.data.users;
}

export async function getUserGroups(userId: number): Promise<{ groups: UserGroupSummary[] }> {
  const response = await apiClient.get<{ groups: UserGroupSummary[] }>(`/users/${userId}/groups`);
  return response.data;
}

export async function updateUserGroups(userId: number, groupIds: number[]): Promise<void> {
  await apiClient.put(`/users/${userId}/groups`, { group_ids: groupIds });
}
