import { apiClient, Folder } from "./core";

// ---------------------------------------------------------------------------
// Folders (document hierarchy)
// ---------------------------------------------------------------------------

export async function listFolders(vaultId: number): Promise<Folder[]> {
  const response = await apiClient.get<{ folders: Folder[] }>("/folders", {
    params: { vault_id: vaultId },
  });
  return response.data.folders;
}

export async function createFolder(
  vaultId: number,
  name: string,
  parentFolderId: number | null = null,
  description = ""
): Promise<Folder> {
  const response = await apiClient.post<Folder>("/folders", {
    vault_id: vaultId,
    name,
    description,
    parent_folder_id: parentFolderId,
  });
  return response.data;
}

export async function updateFolder(
  folderId: number,
  data: { name?: string; description?: string; parent_folder_id?: number | null }
): Promise<Folder> {
  const response = await apiClient.put<Folder>(`/folders/${folderId}`, data);
  return response.data;
}

export async function deleteFolder(folderId: number): Promise<void> {
  await apiClient.delete(`/folders/${folderId}`);
}

export async function moveDocumentsToFolder(
  vaultId: number,
  fileIds: number[],
  folderId: number | null
): Promise<{ moved: number }> {
  const response = await apiClient.post<{ moved: number }>("/folders/move", {
    vault_id: vaultId,
    file_ids: fileIds,
    folder_id: folderId,
  });
  return response.data;
}
