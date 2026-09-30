// frontend/src/pages/ChatShell.a03.firstSendBinding.test.tsx
// Issue #685 — the shell's session identity must bind on the FIRST send.
// When a brand-new chat creates session 42 server-side, the shell store's
// activeSessionId has to follow (the header/share/export surface reads it).
// At the pre-fix tree useSendMessage only sets useChatStore.activeChatId;
// the shell store keeps activeSessionId null until a URL navigation happens.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { BrowserRouter, Routes, Route } from "react-router-dom";
import ChatShell from "./ChatShell";
import { useChatStore } from "@/stores/useChatStore";
import { useChatShellStore } from "@/stores/useChatShellStore";
import { useVaultStore } from "@/stores/useVaultStore";
import { useLlmHealthStore } from "@/stores/useLlmHealthStore";
import { useChatModeStore } from "@/stores/useChatModeStore";
import type { Vault } from "@/lib/api";

// Mock ResizeObserver for Radix UI ScrollArea / react-virtual
class MockResizeObserver {
  observe = vi.fn();
  unobserve = vi.fn();
  disconnect = vi.fn();
}
global.ResizeObserver = MockResizeObserver as unknown as typeof ResizeObserver;
Element.prototype.scrollIntoView = vi.fn();

const chatStreamMock = vi.hoisted(() => vi.fn());
const addChatMessagesBatchMock = vi.hoisted(() => vi.fn());
const createChatSessionMock = vi.hoisted(() => vi.fn());
const getChatSessionMock = vi.hoisted(() => vi.fn(async () => ({ messages: [] })));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    chatStream: (...args: unknown[]) => chatStreamMock(...args),
    addChatMessagesBatch: (...args: unknown[]) => addChatMessagesBatchMock(...args),
    createChatSession: (...args: unknown[]) => createChatSessionMock(...args),
    getChatSession: getChatSessionMock,
    truncateChatSession: vi.fn(async () => ({ remaining_count: 0, tail_seq: 0 })),
    forkChatSession: vi.fn(async () => ({ id: 99, messages: [] })),
    listChatSessions: vi.fn(async () => ({ sessions: [] })),
    listAccessibleVaults: vi.fn(async () => ({ vaults: [] })),
    getLlmModeHealth: vi.fn(async () => ({ thinking: true, instant: true })),
  };
});

// AssistantMessage's canvas bridge imports the canvas API module DIRECTLY
// (not via the barrel); keep its capabilities query offline and fail-closed.
vi.mock("@/lib/api/canvas", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api/canvas")>(
    "@/lib/api/canvas"
  );
  return {
    ...actual,
    getCanvasCapabilities: vi.fn(async () => ({ enabled: false })),
  };
});

vi.mock("sonner", () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
    warning: vi.fn(),
    info: vi.fn(),
  },
}));

const TEST_VAULT: Vault = { id: 1, name: "Test Vault", file_count: 5 } as Vault;

describe("ChatShell first-send session binding (issue #685)", () => {
  beforeEach(() => {
    vi.clearAllMocks();

    // REAL shell store — reset the identity fields the check asserts on.
    useChatShellStore.setState({
      activeSessionId: null,
      activeSessionTitle: null,
      sessionListRefreshToken: 0,
      sessionRailOpen: true,
      rightPaneOpen: false,
      sessionSearchQuery: "",
      pinnedSessionIds: [],
      selectedEvidenceSource: null,
      selectedEvidenceMessageId: null,
      evidenceReturnFocusId: null,
      mobileSheetOpen: false,
    });

    useChatStore.setState({
      activeChatId: null,
      messageIds: [],
      messagesById: {},
      streamingMessageId: null,
      input: "",
      isStreaming: false,
      abortFn: null,
      inputError: null,
      expandedSources: new Set(),
      pendingTurnPersist: null,
      messageEditVersions: {},
      activeEditVersion: {},
    });
    useVaultStore.setState({
      vaults: [TEST_VAULT],
      activeVaultId: 1,
      loading: false,
      error: null,
    });
    useLlmHealthStore.setState({ thinking: true, instant: true });
    useChatModeStore.setState({ scopeDocumentIds: null });

    createChatSessionMock.mockResolvedValue({ id: 42 });
    addChatMessagesBatchMock.mockResolvedValue([
      { id: 100, created_at: "2026-05-12T00:00:00Z", seq: 1 },
      { id: 101, created_at: "2026-05-12T00:00:01Z", seq: 2 },
    ]);

    // Successful stream shape (TranscriptPane.pendingPersist pattern).
    chatStreamMock.mockImplementation(
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
    window.history.pushState({}, "", "/");
  });

  it("first send binds the shell session id to the new session", async () => {
    window.history.pushState({}, "", "/chat");
    render(
      <BrowserRouter>
        <Routes>
          <Route path="/chat" element={<ChatShell />} />
          <Route path="/chat/:sessionId" element={<ChatShell />} />
        </Routes>
      </BrowserRouter>
    );

    const composer = screen.getByLabelText("Message input");
    fireEvent.change(composer, { target: { value: "first question" } });
    await act(async () => {
      fireEvent.keyDown(composer, { key: "Enter" });
    });

    await waitFor(() => expect(createChatSessionMock).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(addChatMessagesBatchMock).toHaveBeenCalledTimes(1));
    // Let the durable save settle into a terminal state before asserting.
    await waitFor(() => {
      const state = useChatStore.getState();
      const assistant = state.messageIds
        .map((id) => state.messagesById[id])
        .find((m) => m?.role === "assistant");
      expect(assistant?.saveState).toBeDefined();
      expect(assistant?.saveState).not.toBe("saving");
    });

    expect(useChatShellStore.getState().activeSessionId).toBe("42");
  });
});
