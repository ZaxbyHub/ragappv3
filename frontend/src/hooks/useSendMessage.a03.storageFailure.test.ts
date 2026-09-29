// frontend/src/hooks/useSendMessage.a03.storageFailure.test.ts
// Issue #685 — a durable save must not fail because of a storage exception
// in the id-migration path. migrateId() reads localStorage inside the persist
// try-block to carry a vote over to the migrated server id; when that read
// throws (quota / security policy / corrupted storage), the pre-fix catch
// stamps the whole turn saveState "failed" even though the batch save itself
// succeeded. The durable outcome must not depend on the feedback mirror.

import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useSendMessage } from "./useSendMessage";
import { useChatStore } from "@/stores/useChatStore";
import { useChatModeStore } from "@/stores/useChatModeStore";
import { useLlmHealthStore } from "@/stores/useLlmHealthStore";
import { useChatShellStore } from "@/stores/useChatShellStore";

// Mock ResizeObserver for Radix UI ScrollArea
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

describe("useSendMessage durable save vs storage exception (issue #685)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.spyOn(console, "error").mockImplementation(() => {});

    // Reset the REAL chat store between tests.
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

    apiMocks.createChatSession.mockResolvedValue({ id: 12 });
    apiMocks.addChatMessagesBatch.mockResolvedValue([
      { id: 100, created_at: "2026-05-12T00:00:00Z", seq: 1 },
      { id: 101, created_at: "2026-05-12T00:00:01Z", seq: 2 },
    ]);

    // Successful stream shape (TranscriptPane.pendingPersist pattern): the
    // turn completes immediately so the durable save runs.
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
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("storage exception does not fail a durable save", async () => {
    // setup.ts replaces localStorage with a silent no-op object whose methods
    // are plain vi.fn()s, so a Storage.prototype spy cannot intercept the
    // bare `localStorage.getItem` reads inside migrateId. Install a storage
    // whose chat_feedback_ reads THROW — the exact hostile condition the
    // migration path must survive.
    const backing = new Map<string, string>();
    Object.defineProperty(window, "localStorage", {
      value: {
        getItem: (key: string) => {
          if (key.startsWith("chat_feedback_")) {
            throw new Error("localStorage is unavailable");
          }
          return backing.get(key) ?? null;
        },
        setItem: (key: string, value: string) => {
          backing.set(key, value);
        },
        removeItem: (key: string) => {
          backing.delete(key);
        },
        clear: () => {
          backing.clear();
        },
        get length() {
          return backing.size;
        },
        key: () => null,
      },
      writable: true,
      configurable: true,
    });

    const { result } = renderHook(() => useSendMessage(7, refreshHistoryMock));

    useChatStore.setState({ input: "please answer" });
    await act(async () => {
      await result.current.handleSend();
    });

    await waitFor(() => {
      expect(apiMocks.addChatMessagesBatch).toHaveBeenCalledTimes(1);
    });

    // Wait until the persist promise has settled into a terminal saveState
    // ("saving" means it is still in flight).
    const readAssistantSaveState = () => {
      const state = useChatStore.getState();
      const assistant = state.messageIds
        .map((id) => state.messagesById[id])
        .find((m) => m?.role === "assistant");
      return assistant?.saveState;
    };
    await waitFor(() => {
      expect(readAssistantSaveState()).toBeDefined();
      expect(readAssistantSaveState()).not.toBe("saving");
    });

    expect(readAssistantSaveState()).toBe("saved");
  });
});
