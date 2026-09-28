// frontend/src/hooks/useSendMessage.turnPersistenceRemount.test.ts
// Issue #685 / review PRR-001 regression test: PageShell keys page content by
// location.pathname, so the first send's navigate(/chat/:id) REMOUNTS
// ChatShell and this hook. The remounted instance's currentTurnPersistenceRef
// is null; before the fix, its handleStop silently skipped persistStop, so
// the turn was never stamped "interrupted" and never durably saved. The fix
// keeps the in-flight turn's persistence handle at module scope, so a fresh
// instance's handleStop still persists the interrupted turn. This test
// simulates the remount by unmounting the first hook instance and invoking
// handleStop on a second, fresh instance.

import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useSendMessage } from "./useSendMessage";
import { useChatStore } from "@/stores/useChatStore";
import { useChatModeStore } from "@/stores/useChatModeStore";
import { useLlmHealthStore } from "@/stores/useLlmHealthStore";
import { useChatShellStore } from "@/stores/useChatShellStore";

class MockResizeObserver {
  observe = vi.fn();
  unobserve = vi.fn();
  disconnect = vi.fn();
}
global.ResizeObserver = MockResizeObserver as unknown as typeof ResizeObserver;

const apiMocks = vi.hoisted(() => ({
  createChatSession: vi.fn(),
  chatStream: vi.fn(),
  addChatMessagesBatch: vi.fn(),
}));
const refreshHistoryMock = vi.hoisted(() => vi.fn(async () => {}));
const onSessionCreatedMock = vi.hoisted(() => vi.fn());

vi.mock("@/lib/api", () => ({
  createChatSession: (...args: unknown[]) => apiMocks.createChatSession(...args),
  addChatMessagesBatch: (...args: unknown[]) => apiMocks.addChatMessagesBatch(...args),
  addChatMessagesBatchKeepalive: vi.fn(),
  chatStream: (...args: unknown[]) => apiMocks.chatStream(...args),
}));

vi.mock("sonner", () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
    warning: vi.fn(),
    info: vi.fn(),
  },
}));

function resetStores(): void {
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
    messageEditVersions: {},
    activeEditVersion: {},
  });
  useLlmHealthStore.setState({ thinking: true, instant: true });
  useChatModeStore.setState({ scopeDocumentIds: null });
  useChatShellStore.setState({ sessionListRefreshToken: 0 });
}

describe("useSendMessage turn persistence across a remount (issue #685 / PRR-001)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.spyOn(console, "error").mockImplementation(() => {});
    resetStores();

    apiMocks.createChatSession.mockResolvedValue({ id: 12 });
    apiMocks.addChatMessagesBatch.mockResolvedValue([
      { id: 100, created_at: "2026-05-12T00:00:00Z", seq: 1 },
      { id: 101, created_at: "2026-05-12T00:00:01Z", seq: 2 },
    ]);
    // A stream that delivered one chunk then went silent — the e2e "SLOW"
    // shape (LIVE-01 means a content-less assistant row would be dropped).
    apiMocks.chatStream.mockImplementation(
      (
        _messages: unknown,
        handlers: {
          onMessage: (chunk: string) => void;
        }
      ) => {
        handlers.onMessage("partial answer");
        return vi.fn();
      }
    );
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("handleStop on a remounted instance persists the interrupted turn", async () => {
    const first = renderHook(() =>
      useSendMessage(7, refreshHistoryMock, { onSessionCreated: onSessionCreatedMock })
    );

    useChatStore.setState({ input: "please answer" });
    await act(async () => {
      void first.result.current.handleSend();
    });
    await waitFor(() => {
      expect(useChatStore.getState().isStreaming).toBe(true);
      expect(useChatStore.getState().messageIds.length).toBe(2);
    });
    expect(onSessionCreatedMock).toHaveBeenCalledWith("12");

    // PageShell-keyed remount: the first instance unmounts mid-stream and a
    // fresh one mounts. The new instance's ref has never seen this turn.
    first.unmount();
    const second = renderHook(() => useSendMessage(7, refreshHistoryMock));

    await act(async () => {
      second.result.current.handleStop();
    });

    // persistStop ran: the assistant row is stamped interrupted and saved.
    await waitFor(() => {
      const state = useChatStore.getState();
      const assistant = state.messageIds
        .map((id) => state.messagesById[id])
        .find((m) => m?.role === "assistant");
      expect(assistant?.status).toBe("interrupted");
      expect(assistant?.saveState).toBe("saved");
    });
    expect(apiMocks.addChatMessagesBatch).toHaveBeenCalledWith(
      12,
      expect.arrayContaining([expect.objectContaining({ status: "interrupted" })])
    );
    second.unmount();
  });

  it("onSessionCreated is not invoked when the send uses an existing session (F-004)", async () => {
    useChatStore.setState({ activeChatId: "7" });
    // Complete immediately so the send settles.
    apiMocks.chatStream.mockImplementation(
      (
        _messages: unknown,
        handlers: {
          onMessage: (chunk: string) => void;
          onComplete: () => Promise<void>;
        }
      ) => {
        handlers.onMessage("hello");
        void handlers.onComplete();
        return vi.fn();
      }
    );

    const hook = renderHook(() =>
      useSendMessage(7, refreshHistoryMock, { onSessionCreated: onSessionCreatedMock })
    );
    useChatStore.setState({ input: "follow-up" });
    await act(async () => {
      void hook.result.current.handleSend();
    });
    await waitFor(() => {
      expect(apiMocks.addChatMessagesBatch).toHaveBeenCalledTimes(1);
    });

    expect(apiMocks.createChatSession).not.toHaveBeenCalled();
    expect(onSessionCreatedMock).not.toHaveBeenCalled();
    hook.unmount();
  });
});
