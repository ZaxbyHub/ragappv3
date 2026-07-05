import { apiClient } from "./core";

export interface HealthResponse {
  status: string;
  version?: string;
  timestamp?: string;
  services?: {
    backend: boolean;
    embeddings: boolean;
    chat: boolean;
  };
}

export interface ConnectionCheck {
  url: string;
  status: number | null;
  ok: boolean;
  error?: string;
}

export interface ConnectionTestResult {
  embeddings: ConnectionCheck;
  chat: ConnectionCheck;
}

export interface LlmModeHealth {
  thinking: boolean;
  instant: boolean;
}

export async function getHealth(): Promise<HealthResponse> {
  const response = await apiClient.get<HealthResponse>("/health");
  return response.data;
}

export async function getLlmModeHealth(): Promise<LlmModeHealth> {
  const response = await apiClient.get<LlmModeHealth>("/llm-health/modes");
  return response.data;
}

export async function testConnections(): Promise<ConnectionTestResult> {
  const response = await apiClient.get<ConnectionTestResult>("/settings/connection");
  return response.data;
}
