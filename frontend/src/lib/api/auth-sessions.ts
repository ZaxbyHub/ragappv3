import { apiClient } from "./core";
import { captureAuthOwner, captureAuthPrincipalGeneration, enqueueAuthTransport, type AuthTransportContext } from "./auth-lifecycle";

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

export interface SessionCredentials {
  access_token: string;
  token_type: string;
  expires_in: number;
}

/** Preserve the initiating session and principal through dispatch and completion. */
function runSessionMutation<T>(work: (context: AuthTransportContext) => Promise<T>): Promise<T> {
  const owner = captureAuthOwner();
  const principalGeneration = captureAuthPrincipalGeneration();
  return enqueueAuthTransport(owner, async (context) => {
    context.assertCurrent();
    const result = await work(context);
    context.assertCurrent();
    return result;
  }, undefined, () => captureAuthPrincipalGeneration() === principalGeneration);
}

export function changePassword(currentPassword: string, newPassword: string): Promise<SessionCredentials> {
  const request: ChangePasswordRequest = {
    current_password: currentPassword,
    new_password: newPassword,
  };
  return runSessionMutation(async (context) => {
    const response = await apiClient.post<SessionCredentials>("/auth/change-password", request, { signal: context.signal });
    return response.data;
  });
}

export async function listSessions(): Promise<SessionListResponse> {
  const response = await apiClient.get<SessionListResponse>("/auth/sessions");
  return response.data;
}

export function revokeSession(sessionId: number): Promise<void> {
  return runSessionMutation(async (context) => {
    await apiClient.delete(`/auth/sessions/${sessionId}`, { signal: context.signal });
  });
}

export function revokeAllSessions(): Promise<SessionCredentials> {
  return runSessionMutation(async (context) => {
    const response = await apiClient.delete<SessionCredentials>("/auth/sessions", { signal: context.signal });
    return response.data;
  });
}
