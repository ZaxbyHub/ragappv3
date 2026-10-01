// frontend/src/hooks/useSendMessage.a07.errorCause.test.ts
// A07 (AC5): when the chat stream's terminal error is an admission-rejection
// SSE error frame (delivered before any content, followed by done), the
// assistant message must keep that error's cause ("capacity"). On the
// current tree the empty-content guard in persistTurn overwrites the raw
// error with "The model returned an empty response. Try again." — the user
// loses the actionable reason. Harness mirrors useSendMessage.test.ts.

import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useSendMessage } from "./useSendMessage";
import { useChatStore } from "@/stores/useChatStore";
import { useChatModeStore } from "@/stores/useChatModeStore";
import { useChatShellStore } from "@/stores/useChatShellStore";
import { useLlmHealthStore } from "@/stores/useLlmHealthStore";

const apiMocks = vi.hoisted(() => ({
  createChatSession: vi.fn(),
  addChatMessagesBatch: vi.fn(),
  chatStream: vi.fn(),
  getLlmModeHealth: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  createChatSession: (...args: unknown[]) => apiMocks.createChatSession(...args),
  addChatMessagesBatch: (...args: unknown[]) => apiMocks.addChatMessagesBatch(...args),
  chatStream: (...args: unknown[]) => apiMocks.chatStream(...args),
  getLlmModeHealth: (...args: unknown[]) => apiMocks.getLlmModeHealth(...args),
}));

type StreamHandlers = {
  onError: (error: Error) => void;
  onComplete: () => Promise<void> | void;
};

describe("useSendMessage admission-rejection error cause (A07 AC5)", () => {
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
    apiMocks.addChatMessagesBatch.mockResolvedValue([
      { id: 100, created_at: "2026-05-12T00:00:00Z", seq: 1 },
      { id: 101, created_at: "2026-05-12T00:00:01Z", seq: 2 },
    ]);
  });

  it("admission rejection keeps its cause", async () => {
    // Capturing stream stub: the test fires the handlers when it chooses.
    const cell: { current: StreamHandlers | null } = { current: null };
    apiMocks.chatStream.mockImplementation((_messages: unknown, handlers: StreamHandlers) => {
      cell.current = handlers;
      return vi.fn(); // abort function
    });

    const refreshHistory = vi.fn().mockResolvedValue(undefined);
    useChatStore.setState({ activeChatId: "42", input: "Admission check question" });

    const { result } = renderHook(() => useSendMessage(7, refreshHistory));

    await act(async () => {
      await result.current.handleSend();
    });

    const streamingId = useChatStore.getState().streamingMessageId;
    expect(streamingId).toBeTruthy();

    await act(async () => {
      // SSE error frame before any content, then the stream's done event.
      const admission = new Error("Chat capacity is saturated; retry shortly");
      (admission as { code?: string }).code = "ADMISSION_REJECTED";
      cell.current!.onError(admission);
      await cell.current!.onComplete();
    });

    await waitFor(() => {
      expect(useChatStore.getState().isStreaming).toBe(false);
    });

    const assistant = useChatStore.getState().messagesById[streamingId!];
    expect(assistant?.status).toBe("failed");
    expect(assistant?.error).toMatch(/capacity/i);
  });
});
