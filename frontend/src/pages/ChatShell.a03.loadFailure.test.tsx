// frontend/src/pages/ChatShell.a03.loadFailure.test.tsx
// Issue #685 — a failed session load must be recoverable and must not leave
// the PREVIOUS session as the silent target of the next send. At the pre-fix
// tree the load effect swallows the rejection with console.error: no retry
// control is offered, and because useChatStore.activeChatId still holds the
// previous session, the composer's next turn is durably written into it.

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

function renderChatShellRoutes(): void {
  render(
    <BrowserRouter>
      <Routes>
        <Route path="/chat" element={<ChatShell />} />
        <Route path="/chat/:sessionId" element={<ChatShell />} />
      </Routes>
    </BrowserRouter>
  );
}

async function flushMacroTasks(): Promise<void> {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

describe("ChatShell session load failures (issue #685)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.spyOn(console, "error").mockImplementation(() => {});

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

    createChatSessionMock.mockResolvedValue({ id: 8 });
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
    vi.restoreAllMocks();
    window.history.pushState({}, "", "/");
  });

  it("failed session load surfaces a retry control", async () => {
    getChatSessionMock
      .mockClear()
      .mockRejectedValueOnce(new Error("session load failed"))
      .mockResolvedValue({ messages: [] });

    window.history.pushState({}, "", "/chat/7");
    renderChatShellRoutes();

    await waitFor(() => expect(getChatSessionMock).toHaveBeenCalledTimes(1));
    await flushMacroTasks();

    // Exactly one load-retry control: the panes themselves render cleanly,
    // so the only retry counted is the one for the failed transcript load.
    expect(screen.queryAllByRole("button", { name: /retry/i }).length).toBe(1);
  });

  it("send after failed load does not retarget the previous session", async () => {
    getChatSessionMock.mockClear().mockRejectedValue(new Error("session load failed"));

    // The store holds a previously loaded session (id 5, one exchange).
    useChatStore.setState({
      activeChatId: "5",
      messageIds: ["5u", "5a"],
      messagesById: {
        "5u": {
          id: "5u",
          role: "user",
          content: "previous question",
          created_at: "2026-05-12T00:00:00Z",
        },
        "5a": {
          id: "5a",
          role: "assistant",
          content: "previous answer",
          status: "complete",
          created_at: "2026-05-12T00:00:01Z",
        },
      },
    });

    window.history.pushState({}, "", "/chat/7");
    renderChatShellRoutes();

    await waitFor(() => expect(getChatSessionMock).toHaveBeenCalledTimes(1));
    await flushMacroTasks();

    const composer = screen.getByLabelText("Message input");
    fireEvent.change(composer, { target: { value: "next question" } });
    await act(async () => {
      fireEvent.keyDown(composer, { key: "Enter" });
    });

    await waitFor(() => expect(chatStreamMock).toHaveBeenCalledTimes(1));

    // chatStream(messages, callbacks, vaultId, mode, temperature,
    // retrievalMode, citationMode, metadataFilter, documentIds, durableTurn)
    // — the durable-turn option (index 9) carries the target session id.
    const sendsIntoPreviousSession = chatStreamMock.mock.calls.filter(
      (call) =>
        (call[9] as { sessionId?: number } | undefined)?.sessionId === 5
    ).length;
    expect(sendsIntoPreviousSession).toBe(0);
  });
});
