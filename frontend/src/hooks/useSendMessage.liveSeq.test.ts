// frontend/src/hooks/useSendMessage.liveSeq.test.ts
// Issue #683 (live-send seq propagation): the batch-save endpoint returns the
// server-issued durable order for every saved row (backend contract at
// backend/app/api/routes/chat.py — "seq": row[9]), so after migrateId the
// store rows must carry that seq. TranscriptPane's durableKeepSeq anchor
// (retry/edit truncate) reads exactly this field: a row saved without it
// contributes nothing to the anchor, collapsing the truncate boundary to 0
// and deleting every persisted turn on the server.

import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useSendMessage } from "./useSendMessage";
import { useChatStore } from "@/stores/useChatStore";
import { useChatModeStore } from "@/stores/useChatModeStore";
import { useChatShellStore } from "@/stores/useChatShellStore";
import { useLlmHealthStore } from "@/stores/useLlmHealthStore";

const apiMocks = vi.hoisted(() => ({
  createChatSession: vi.fn(),
  addChatMessage: vi.fn(),
  addChatMessagesBatch: vi.fn(),
  chatStream: vi.fn(),
  getLlmModeHealth: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  createChatSession: (...args: unknown[]) => apiMocks.createChatSession(...args),
  addChatMessage: (...args: unknown[]) => apiMocks.addChatMessage(...args),
  addChatMessagesBatch: (...args: unknown[]) => apiMocks.addChatMessagesBatch(...args),
  chatStream: (...args: unknown[]) => apiMocks.chatStream(...args),
  getLlmModeHealth: (...args: unknown[]) => apiMocks.getLlmModeHealth(...args),
}));

describe("useSendMessage live-saved durable seq (issue #683)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useChatStore.setState({
      messageIds: [],
      messagesById: {},
      streamingMessageId: null,
      input: "",
      isStreaming: false,
      abortFn: null,
      inputError: null,
      expandedSources: new Set(),
      activeChatId: null,
      pendingTurnPersist: null,
    });
    useLlmHealthStore.setState({ thinking: true, instant: true });
    useChatShellStore.setState({ sessionListRefreshToken: 0 });
    useChatModeStore.setState({ scopeDocumentIds: null });
    apiMocks.createChatSession.mockResolvedValue({ id: 42 });
    // Contract-accurate batch save: the server returns the durable order
    // (seq) for every row it persisted.
    apiMocks.addChatMessagesBatch.mockResolvedValue([
      { id: 100, created_at: "2026-05-12T00:00:00Z", seq: 1 },
      { id: 101, created_at: "2026-05-12T00:00:01Z", seq: 2 },
    ]);
    apiMocks.chatStream.mockImplementation(
      (
        _messages: unknown,
        handlers: {
          onMessage: (chunk: string) => void;
          onComplete: () => Promise<void>;
        },
      ) => {
        handlers.onMessage("hello");
        void handlers.onComplete();
        return vi.fn();
      },
    );
  });

  it("live-saved rows carry the server-issued seq", async () => {
    const refreshHistory = vi.fn().mockResolvedValue(undefined);
    useChatStore.setState({ input: "What changed?" });

    const { result } = renderHook(() => useSendMessage(7, refreshHistory));

    await act(async () => {
      await result.current.handleSend();
    });

    await waitFor(() => {
      expect(refreshHistory).toHaveBeenCalledWith(true);
    });

    // The migrated server rows must carry the durable order the backend
    // returned — dropping seq makes every later truncate anchor compute 0.
    expect(useChatStore.getState().messagesById["100"].seq).toBe(1);
    expect(useChatStore.getState().messagesById["101"].seq).toBe(2);
  });
});
