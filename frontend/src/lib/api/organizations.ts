import { apiClient } from "./core";

export interface Organization {
  id: number;
  name: string;
  description: string;
  slug?: string;
  member_count?: number;
  vault_count?: number;
  group_count?: number;
  created_at?: string;
}

export async function listOrganizations(): Promise<Organization[]> {
  const response = await apiClient.get<{ organizations: Organization[] } | Organization[]>("/organizations/");
  const data = response.data;
  return Array.isArray(data) ? data : (data.organizations ?? []);
}
