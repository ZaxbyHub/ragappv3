import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useSendMessage } from "./useSendMessage";
import { useChatStore } from "@/stores/useChatStore";
import { useChatModeStore } from "@/stores/useChatModeStore";
import { useChatShellStore } from "@/stores/useChatShellStore";
import { useLlmHealthStore } from "@/stores/useLlmHealthStore";

const api = vi.hoisted(() => ({
  createChatSession: vi.fn(), addChatMessagesBatch: vi.fn(), addChatMessagesBatchKeepalive: vi.fn(), chatStream: vi.fn(),
  getLlmModeHealth: vi.fn(),
}));
vi.mock("@/lib/api", () => ({
  createChatSession: (...a: unknown[]) => api.createChatSession(...a),
  addChatMessagesBatch: (...a: unknown[]) => api.addChatMessagesBatch(...a),
  addChatMessagesBatchKeepalive: (...a: unknown[]) => api.addChatMessagesBatchKeepalive(...a),
  chatStream: (...a: unknown[]) => api.chatStream(...a),
  getLlmModeHealth: (...a: unknown[]) => api.getLlmModeHealth(...a),
}));

type RetirableHandlers = {
  onMessage?: (chunk: string) => void;
  onComplete?: () => void | Promise<void>;
  onRetired?: () => void;
};
const refreshHistory = vi.fn().mockResolvedValue(undefined);

describe("useSendMessage LOW03 auth-retirement ownership", () => {
  const handlers: RetirableHandlers[] = [];
  beforeEach(() => {
    vi.clearAllMocks(); handlers.length = 0;
    useChatStore.setState({ messageIds: [], messagesById: {}, streamingMessageId: null,
      input: "", isStreaming: false, abortFn: null, inputError: null, activeChatId: null,
      pendingTurnPersist: null });
    useLlmHealthStore.setState({ thinking: true, instant: true });
    useChatShellStore.setState({ sessionListRefreshToken: 0 });
    useChatModeStore.setState({ scopeDocumentIds: null });
    api.createChatSession.mockResolvedValue({ id: 42 });
    api.addChatMessagesBatch.mockResolvedValue([{ id: 1, created_at: "2026-01-01", seq: 1 }, { id: 2, created_at: "2026-01-01", seq: 2 }]);
    api.chatStream.mockImplementation((_messages: unknown, next: RetirableHandlers) => { handlers.push(next); return vi.fn(); });
  });

  async function begin(result: { current: ReturnType<typeof useSendMessage> }, expectedHandlers: number) {
    await act(async () => { await result.current.sendDirect("question", []); });
    await waitFor(() => expect(handlers).toHaveLength(expectedHandlers));
  }

  it("clears real chat-store streaming state after same-session remount retirement", async () => {
    const mounted = renderHook(() => useSendMessage(7, refreshHistory));
    await begin(mounted.result, 1);
    const activeSession = useChatStore.getState().activeChatId;
    expect(useChatStore.getState().isStreaming).toBe(true);
    mounted.unmount();
    const remounted = renderHook(() => useSendMessage(7, refreshHistory));
    expect(useChatStore.getState().activeChatId).toBe(activeSession);
    act(() => { handlers[0].onRetired?.(); });
    expect(useChatStore.getState()).toMatchObject({ isStreaming: false, abortFn: null, streamingMessageId: null });
    expect(remounted.result.current.currentStage).toBeNull();
  });

  it("does not let a normally completed predecessor re-retire a successor with the same local generation", async () => {
    const first = renderHook(() => useSendMessage(7, refreshHistory));
    await begin(first.result, 1);
    await act(async () => {
      handlers[0].onMessage?.("completed predecessor");
      await handlers[0].onComplete?.();
    });
    await waitFor(() => expect(useChatStore.getState().isStreaming).toBe(false));
    first.unmount();

    const second = renderHook(() => useSendMessage(7, refreshHistory));
    await begin(second.result, 2);
    const successorAbort = useChatStore.getState().abortFn;
    const successorMessageId = useChatStore.getState().streamingMessageId;
    expect(successorMessageId).not.toBeNull();
    act(() => { handlers[0].onRetired?.(); });
    expect(useChatStore.getState()).toMatchObject({ isStreaming: true, abortFn: successorAbort, streamingMessageId: successorMessageId });
    act(() => { handlers[1].onRetired?.(); });
    expect(useChatStore.getState().isStreaming).toBe(false);
  });
});
