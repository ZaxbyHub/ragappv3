// frontend/src/pages/ChatShell.a03.loadRace.test.tsx
// Issue #685 — supersession must cover the transitions that issue NO new
// fetch. ChatShell's load guard (loadSeqRef) only discards a stale fetch when
// a NEWER fetch bumped the sequence. Returning to the store-held session, or
// leaving for a New chat, issues no fetch — so at the pre-fix tree the late
// fetch for the abandoned selection still lands and loadChat() clobbers the
// transcript the user is looking at.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, render, waitFor } from "@testing-library/react";
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

const getChatSessionMock = vi.hoisted(() => vi.fn(async () => ({ messages: [] })));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    chatStream: vi.fn(() => vi.fn()),
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

interface SessionDetailShape {
  messages: Array<{
    id: number;
    role: "user" | "assistant";
    content: string;
    created_at: string;
  }>;
}

const sessionTwoDetail: SessionDetailShape = {
  messages: [
    {
      id: 21,
      role: "user",
      content: "session two",
      created_at: "2026-05-12T00:00:00Z",
    },
  ],
};

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

function navigate(path: string): void {
  act(() => {
    window.history.pushState({}, "", path);
    window.dispatchEvent(new PopStateEvent("popstate"));
  });
}

async function flushMacroTasks(): Promise<void> {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

describe("ChatShell session load races (issue #685)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    getChatSessionMock.mockClear().mockResolvedValue({ messages: [] });

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
  });

  afterEach(() => {
    window.history.pushState({}, "", "/");
  });

  it("superseded fetch after returning to the store-held session is discarded", async () => {
    // Session 2's transcript fetch is held pending until the test resolves it.
    let resolveFetchTwo!: (detail: SessionDetailShape) => void;
    const fetchTwo = new Promise<SessionDetailShape>((resolve) => {
      resolveFetchTwo = resolve;
    });
    getChatSessionMock.mockImplementation(async (sessionId: number) =>
      sessionId === 2 ? fetchTwo : { messages: [] }
    );

    // The store already holds session 1.
    useChatStore.setState({
      activeChatId: "1",
      messageIds: ["11"],
      messagesById: {
        "11": {
          id: "11",
          role: "user",
          content: "session one",
          created_at: "2026-05-11T00:00:00Z",
        },
      },
    });

    window.history.pushState({}, "", "/chat/1");
    renderChatShellRoutes();
    await flushMacroTasks();
    // Session 1 is store-held: no fetch is issued for it.
    expect(getChatSessionMock).not.toHaveBeenCalled();

    // Switch to session 2 (its fetch stays in flight)...
    navigate("/chat/2");
    await waitFor(() => expect(getChatSessionMock).toHaveBeenCalledTimes(1));

    // ...then return to the store-held session 1 — no new fetch is needed.
    navigate("/chat/1");
    await flushMacroTasks();
    expect(getChatSessionMock).toHaveBeenCalledTimes(1);

    // The superseded fetch resolves late: it must be discarded, not loaded.
    await act(async () => {
      resolveFetchTwo(sessionTwoDetail);
    });
    await flushMacroTasks();

    expect(useChatStore.getState().activeChatId).toBe("1");
  });

  it("fetch resolving after New chat is discarded", async () => {
    // Session 2's transcript fetch is held pending until the test resolves it.
    let resolveFetchTwo!: (detail: SessionDetailShape) => void;
    const fetchTwo = new Promise<SessionDetailShape>((resolve) => {
      resolveFetchTwo = resolve;
    });
    getChatSessionMock.mockImplementation(async () => fetchTwo);

    window.history.pushState({}, "", "/chat/2");
    renderChatShellRoutes();
    await waitFor(() => expect(getChatSessionMock).toHaveBeenCalledTimes(1));

    // Leave the session route (New chat) while the fetch is pending.
    navigate("/chat");
    await flushMacroTasks();

    // The abandoned fetch resolves late: it must be discarded, not loaded.
    await act(async () => {
      resolveFetchTwo(sessionTwoDetail);
    });
    await flushMacroTasks();

    expect(useChatStore.getState().activeChatId).toBe(null);
  });
});
