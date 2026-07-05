import { apiClient } from "./core";

export interface Session {
  id: string;
  user_id: number;
  user_agent: string | null;
  ip_address: string | null;
  created_at: string;
  expires_at: string;
  is_current: boolean;
}

export interface SessionListResponse {
  sessions: Session[];
}

export interface ChangePasswordRequest {
  current_password: string;
  new_password: string;
}

export async function changePassword(currentPassword: string, newPassword: string): Promise<void> {
  const request: ChangePasswordRequest = {
    current_password: currentPassword,
    new_password: newPassword,
  };
  await apiClient.post("/auth/change-password", request);
}

export async function listSessions(): Promise<SessionListResponse> {
  const response = await apiClient.get<SessionListResponse>("/auth/sessions");
  return response.data;
}

export async function revokeSession(sessionId: number): Promise<void> {
  await apiClient.delete(`/auth/sessions/${sessionId}`);
}

export async function revokeAllSessions(): Promise<{ access_token: string; token_type: string; expires_in: number }> {
  const response = await apiClient.delete<{ access_token: string; token_type: string; expires_in: number }>("/auth/sessions");
  return response.data;
}
