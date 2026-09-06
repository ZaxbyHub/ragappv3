import { useState, useEffect, useCallback } from "react";
import { listChatSessions, getChatSession, type ChatSession } from "@/lib/api";
import { mapSessionMessage } from "@/lib/chatMessageMapper";
import { useChatStore, type Message } from "@/stores/useChatStore";

interface CacheEntry {
  data: ChatSession[];
  timestamp: number;
}

const cache = new Map<string, CacheEntry>();
const CACHE_TTL = 30000; // 30 seconds

export interface UseChatHistoryReturn {
  chatHistory: ChatSession[];
  isChatLoading: boolean;
  chatHistoryError: string | null;
  handleLoadChat: (session: ChatSession) => Promise<void>;
  refreshHistory: (force?: boolean) => Promise<void>;
}

/** Manages chat session history — fetches session list and loads individual sessions. */
export function useChatHistory(activeVaultId: number | null): UseChatHistoryReturn {
  const [chatHistory, setChatHistory] = useState<ChatSession[]>([]);
  const [isChatLoading, setIsChatLoading] = useState(true);
  const [chatHistoryError, setChatHistoryError] = useState<string | null>(null);

  const refreshHistory = useCallback(async (force = false) => {
    const cacheKey = activeVaultId?.toString() ?? 'default';
    const cached = cache.get(cacheKey);
    
    // Use cache if valid and not forced
    if (!force && cached && Date.now() - cached.timestamp < CACHE_TTL) {
      setChatHistory(cached.data);
      setIsChatLoading(false);
      return;
    }

    setIsChatLoading(true);
    setChatHistoryError(null);
    try {
      const data = await listChatSessions(activeVaultId ?? undefined);
      cache.set(cacheKey, { data: data.sessions, timestamp: Date.now() });
      setChatHistory(data.sessions);
    } catch (err) {
      const errorMessage = err instanceof Error ? err.message : "Failed to load chat history";
      setChatHistoryError(errorMessage);
    } finally {
      setIsChatLoading(false);
    }
  }, [activeVaultId]);

  useEffect(() => {
    refreshHistory();
  }, [refreshHistory]);

  const handleLoadChat = useCallback(async (session: ChatSession) => {
    const { isStreaming } = useChatStore.getState();
    if (isStreaming) return;
    try {
      const detail = await getChatSession(session.id);
      // Canonical mapper (PRR-019): the previous inline hand-mapping dropped
      // every issue-#507 field (kmsRefs/mode/turnId/status/assessment), so any
      // future consumer of this hook would silently lose them on replay.
      const loadedMessages: Message[] = (detail.messages ?? []).map(mapSessionMessage);
      useChatStore.getState().loadChat(session.id.toString(), loadedMessages);
    } catch (err) {
      console.error("Failed to load chat session:", err);
    }
  }, []);

  return {
    chatHistory,
    isChatLoading,
    chatHistoryError,
    handleLoadChat,
    refreshHistory,
  };
}
