import { apiClient, UserListItem, Group } from "./core";

export async function listAllUsers(): Promise<UserListItem[]> {
  const response = await apiClient.get<{ users: UserListItem[] }>("/users");
  return response.data.users;
}

export async function getUserGroups(userId: number): Promise<{ groups: Group[] }> {
  const response = await apiClient.get<{ groups: Group[] }>(`/users/${userId}/groups`);
  return response.data;
}

export async function updateUserGroups(userId: number, groupIds: number[]): Promise<void> {
  await apiClient.put(`/users/${userId}/groups`, { group_ids: groupIds });
}
