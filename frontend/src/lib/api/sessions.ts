import { apiClient, API_BASE_URL, _jwtAccessToken, ChatStreamCallbacks, ChatMessage, Source, UsedMemory, WikiReference, KMSReference, CitationValidationDebug, ChatSession, ChatSessionDetail, ChatSessionMessage, CreateSessionRequest, AddMessageRequest, ChatHistoryItem, ensureCsrfToken, refreshAccessToken, isTokenNearExpiry } from "./core";
import { setChatHistory as storageSetChatHistory, getChatHistory as storageGetChatHistory } from "../storage";

// ============================================================================
// SSE Streaming
// ============================================================================

export async function parseSSEStream(
  reader: ReadableStreamDefaultReader<Uint8Array>,
  callbacks: ChatStreamCallbacks,
): Promise<void> {
  const decoder = new TextDecoder();
  let buffer = "";
  let completed = false;

  const completeOnce = () => {
    if (completed) return;
    completed = true;
    callbacks.onComplete?.();
  };

  // Field/event names we explicitly drop on receipt. Lowercase comparison.
  const REASONING_TYPES = new Set([
    "reasoning",
    "reasoning_content",
    "thinking",
    "thinking_content",
  ]);

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop() || "";

    for (const line of lines) {
      const trimmed = line.trim();
      if (trimmed.startsWith("data: ")) {
        const data = trimmed.slice(6);
        if (data === "[DONE]") {
          completeOnce();
          return;
        }
        try {
          const parsed = JSON.parse(data);
          if (parsed.type === 'error') {
            callbacks.onError?.(new Error(parsed.message || 'Chat stream error'));
            return;
          }
          if (parsed.type === 'mode' && (parsed.mode === 'instant' || parsed.mode === 'thinking')) {
            callbacks.onMode?.(parsed.mode);
            continue;
          }
          if (parsed.type === 'stage' && typeof parsed.stage === 'string') {
            callbacks.onStage?.(parsed.stage);
            continue;
          }
          // Defense in depth: drop any reasoning/thinking event regardless of
          // whether it appears as ``type`` or as a content field.
          const eventType = typeof parsed.type === "string" ? parsed.type.toLowerCase() : "";
          if (REASONING_TYPES.has(eventType)) {
            continue;
          }
          // Only forward content from explicit "content" events to avoid
          // accidentally streaming a reasoning blob that happened to contain
          // a ``content`` field.
          if (parsed.content && (eventType === "content" || eventType === "" || eventType === "fallback")) {
            // Strip any reasoning-named keys before forwarding (paranoid).
            callbacks.onMessage(parsed.content);
          }
          if (Array.isArray(parsed.sources) && parsed.sources.length > 0) {
            const scoreType = ((parsed as { score_type?: Source["score_type"] }).score_type
              ?? "distance") as Source["score_type"];
            const enrichedSources = parsed.sources.map((s: Source) => ({
              ...s,
              score_type: scoreType,
            }));
            callbacks.onSources?.(enrichedSources);
          }
          if (Array.isArray(parsed.memories_used) && parsed.memories_used.length > 0) {
            // Backend may emit either bare strings (legacy) or structured
            // UsedMemory dicts. Normalize to structured shape; if a string is
            // received, synthesize a minimal record so the UI still renders.
            const normalized: UsedMemory[] = parsed.memories_used.map(
              (m: unknown, idx: number): UsedMemory => {
                if (typeof m === "string") {
                  return {
                    id: `M${idx + 1}`,
                    memory_label: `M${idx + 1}`,
                    content: m,
                  };
                }
                const obj = m as Partial<UsedMemory> & { id?: unknown };
                const fallbackLabel = `M${idx + 1}`;
                return {
                  id: String(obj.id ?? fallbackLabel),
                  memory_label: obj.memory_label ?? fallbackLabel,
                  content: typeof obj.content === "string" ? obj.content : "",
                  category: obj.category ?? null,
                  tags: obj.tags ?? null,
                  source: obj.source ?? null,
                  vault_id: obj.vault_id ?? null,
                  score: obj.score ?? null,
                  score_type: obj.score_type ?? null,
                  created_at: obj.created_at ?? null,
                  updated_at: obj.updated_at ?? null,
                };
              }
            );
            callbacks.onMemories?.(normalized);
          }
          if (Array.isArray(parsed.wiki_used) && parsed.wiki_used.length > 0) {
            callbacks.onWiki?.(parsed.wiki_used as WikiReference[]);
          }
          if (Array.isArray(parsed.kms_used) && parsed.kms_used.length > 0) {
            callbacks.onKMS?.(parsed.kms_used as KMSReference[]);
          }
          if (parsed.citation_validation && typeof parsed.citation_validation === "object") {
            callbacks.onCitationValidation?.(parsed.citation_validation as CitationValidationDebug);
          }
          if (typeof parsed.repaired_content === "string") {
            // Reconcile the message to the citation-clean content before
            // onComplete persists it (fired only when citations were stripped).
            callbacks.onFinalContent?.(parsed.repaired_content);
          }
          // FR-004: extract citation confidence and unverifiable claims from done payload.
          if (typeof parsed.citation_confidence === "object" && parsed.citation_confidence !== null) {
            callbacks.onCitationConfidence?.(parsed.citation_confidence as Record<string, number>);
          }
          if (Array.isArray(parsed.unverifiable_claims)) {
            callbacks.onUnverifiableClaims?.(parsed.unverifiable_claims as string[]);
          }
          if (eventType === "done") {
            completeOnce();
            return;
          }
        } catch {
          // JSON.parse failed — the server sent a malformed SSE chunk.
          // Do NOT forward raw data to onMessage: it could contain thinking
          // content (reasoning_content, <think>, _lhs) that must never be
          // shown to the user.  Drop the chunk and continue streaming.
        }
      }
    }
  }
}

export function chatStream(
  messages: ChatMessage[],
  callbacks: ChatStreamCallbacks,
  vaultId?: number,
  mode?: 'instant' | 'thinking',
  temperature?: number,
  retrievalMode?: string,
  citationMode?: string,
): () => void {
  const abortController = new AbortController();
  // Build the request body once and reuse for both the initial POST and
  // the 401 token-refresh retry path. Keeps payload shape consistent.
  const requestBody = JSON.stringify({
    messages,
    ...(vaultId != null && { vault_id: vaultId }),
    ...(mode != null && { mode }),
    ...(temperature != null && { temperature }),
    ...(retrievalMode != null && { retrieval_mode: retrievalMode }),
    ...(citationMode != null && { citation_mode: citationMode }),
  });

  const startStream = async () => {
    try {
      // Pre-stream token refresh check: if JWT is close to expiring, refresh it first
      if (_jwtAccessToken && isTokenNearExpiry(_jwtAccessToken)) {
        const refreshedToken = await refreshAccessToken();
        if (!refreshedToken) {
          // Refresh failed - abort
          callbacks.onError?.(new Error("Session expired. Please log in again."));
          return;
        }
      }

      // Get CSRF token for the POST request
      let csrfToken: string;
      try {
        csrfToken = await ensureCsrfToken();
      } catch {
        callbacks.onError?.(new Error("Failed to get CSRF token"));
        return;
      }

      const headers: Record<string, string> = {
        "Content-Type": "application/json",
        "X-CSRF-Token": csrfToken,
      };
      if (_jwtAccessToken) {
        headers["Authorization"] = `Bearer ${_jwtAccessToken}`;
      }

      const response = await fetch(`${API_BASE_URL}/chat/stream`, {
        method: "POST",
        headers,
        body: requestBody,
        signal: abortController.signal,
      });

      if (!response.ok) {
        if (response.status === 401 && _jwtAccessToken) {
          // Check error detail — only retry on token_expired, skip token_invalid/user_inactive
          const errorBody = await response.json().catch(() => null);
          const detail = errorBody?.detail;
          const isTokenExpired = typeof detail === "string" && detail.includes("token_expired");
          const isTokenInvalid = typeof detail === "string" && (
            detail.includes("token_invalid") || detail.includes("user_inactive")
          );

          if (isTokenExpired && !isTokenInvalid) {
            try {
              // Backoff delay before retry (1 second, matching interceptor pattern)
              await new Promise((resolve) => setTimeout(resolve, 1000));

              const newToken = await refreshAccessToken();
              if (newToken) {
                headers["Authorization"] = `Bearer ${newToken}`;
                const retryResponse = await fetch(`${API_BASE_URL}/chat/stream`, {
                  method: "POST",
                  headers,
                  body: requestBody,
                  signal: abortController.signal,
                });
                if (!retryResponse.ok) {
                  throw new Error(`HTTP error! status: ${retryResponse.status}`);
                }
                const retryReader = retryResponse.body?.getReader();
                if (!retryReader) {
                  throw new Error("Response body is not readable");
                }
                await parseSSEStream(retryReader, callbacks);
                return;
              }
            } catch {
              // Refresh or retry failed — fall through to error
            }
          }
        }
        throw new Error(`HTTP error! status: ${response.status}`);
      }

      const reader = response.body?.getReader();
      if (!reader) {
        throw new Error("Response body is not readable");
      }

      await parseSSEStream(reader, callbacks);
    } catch (error) {
      if (error instanceof Error && error.name === "AbortError") {
        return;
      }
      callbacks.onError?.(
        error instanceof Error ? error : new Error(String(error))
      );
    }
  };

  startStream();

  return () => {
    abortController.abort();
  };
}

// ============================================================================
// Chat History (local storage)
// ============================================================================

export function getChatHistory(): ChatHistoryItem[] {
  return storageGetChatHistory();
}

export async function saveChatHistory(history: ChatHistoryItem[]): Promise<void> {
  try {
    const success = await storageSetChatHistory(history);
    if (!success) {
      console.warn("Failed to save chat history: quota exceeded even after trimming");
    }
  } catch (err) {
    console.error("Failed to save chat history:", err);
  }
}

// ============================================================================
// Chat Sessions (API)
// ============================================================================

export async function listChatSessions(vaultId?: number): Promise<{ sessions: ChatSession[] }> {
  const response = await apiClient.get<{ sessions: ChatSession[] }>(
    "/chat/sessions",
    vaultId != null ? { params: { vault_id: vaultId } } : undefined
  );
  return response.data;
}

export async function getChatSession(sessionId: number): Promise<ChatSessionDetail> {
  const response = await apiClient.get<ChatSessionDetail>(`/chat/sessions/${sessionId}`);
  return response.data;
}

export async function createChatSession(request: CreateSessionRequest): Promise<ChatSession> {
  const response = await apiClient.post<ChatSession>("/chat/sessions", request);
  return response.data;
}

export async function addChatMessage(sessionId: number, request: AddMessageRequest): Promise<ChatSessionMessage> {
  const response = await apiClient.post<ChatSessionMessage>(`/chat/sessions/${sessionId}/messages`, request);
  return response.data;
}

export async function updateMessageFeedback(
  sessionId: number,
  messageId: number,
  rating: "up" | "down" | null
): Promise<ChatSessionMessage> {
  const response = await apiClient.patch(
    `/chat/sessions/${sessionId}/messages/${messageId}/feedback`,
    { rating }
  );
  return response.data;
}

export async function updateChatSession(sessionId: number, title: string): Promise<ChatSession> {
  const response = await apiClient.put<ChatSession>(`/chat/sessions/${sessionId}`, { title });
  return response.data;
}

export async function deleteChatSession(sessionId: number): Promise<void> {
  await apiClient.delete(`/chat/sessions/${sessionId}`);
}

export interface ForkSessionResponse extends ChatSessionDetail {
  forked_from_session_id: number;
  fork_message_index: number;
}

export async function forkChatSession(sessionId: number, messageIndex: number): Promise<ForkSessionResponse> {
  const response = await apiClient.post<ForkSessionResponse>(
    `/chat/sessions/${sessionId}/fork`,
    { message_index: messageIndex }
  );
  return response.data;
}
