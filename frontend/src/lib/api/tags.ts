import type { Tag } from "./core";
import { apiClient } from "./core";

// Tags (document organization)
// ---------------------------------------------------------------------------

export async function listTags(vaultId: number): Promise<Tag[]> {
  const response = await apiClient.get<{ tags: Tag[] }>("/tags", { params: { vault_id: vaultId } });
  return response.data.tags;
}

export async function createTag(vaultId: number, name: string, color = ""): Promise<Tag> {
  const response = await apiClient.post<Tag>("/tags", { vault_id: vaultId, name, color });
  return response.data;
}

export async function updateTag(
  tagId: number,
  data: { name?: string; color?: string }
): Promise<Tag> {
  const response = await apiClient.put<Tag>(`/tags/${tagId}`, data);
  return response.data;
}

export async function deleteTag(tagId: number): Promise<void> {
  await apiClient.delete(`/tags/${tagId}`);
}

export async function assignTags(
  vaultId: number,
  fileIds: number[],
  tagIds: number[]
): Promise<{ assigned: number }> {
  const response = await apiClient.post<{ assigned: number }>("/tags/assign", {
    vault_id: vaultId,
    file_ids: fileIds,
    tag_ids: tagIds,
  });
  return response.data;
}

export async function getDocumentTags(fileId: number, vaultId: number): Promise<Tag[]> {
  const response = await apiClient.get<{ tags: Tag[] }>(`/tags/documents/${fileId}`, {
    params: { vault_id: vaultId },
  });
  return response.data.tags;
}

export async function setDocumentTags(
  fileId: number,
  vaultId: number,
  tagIds: number[]
): Promise<Tag[]> {
  const response = await apiClient.put<{ tags: Tag[] }>(`/tags/documents/${fileId}`, {
    vault_id: vaultId,
    tag_ids: tagIds,
  });
  return response.data.tags;
}

export async function unassignTag(
  tagId: number,
  fileId: number,
  vaultId: number
): Promise<void> {
  await apiClient.delete(`/tags/${tagId}/documents/${fileId}`, {
    params: { vault_id: vaultId },
  });
}
