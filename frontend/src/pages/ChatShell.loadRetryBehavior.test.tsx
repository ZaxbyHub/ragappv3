// frontend/src/pages/ChatShell.loadRetryBehavior.test.tsx
// Issue #685 — behavior beyond the frozen checks: the Retry control on a
// failed session load actually re-fetches and loads the transcript, and a
// SUPERSEDED REJECTION (an older selection's fetch failing after a newer
// selection took over) is discarded instead of clobbering the newer
// selection's state with an error banner.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { BrowserRouter, Routes, Route } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import ChatShell from "./ChatShell";
import { useChatStore } from "@/stores/useChatStore";
import { useChatShellStore } from "@/stores/useChatShellStore";
import { useVaultStore } from "@/stores/useVaultStore";
import { useLlmHealthStore } from "@/stores/useLlmHealthStore";
import { useChatModeStore } from "@/stores/useChatModeStore";
import type { Vault } from "@/lib/api";

class MockResizeObserver {
  observe = vi.fn();
  unobserve = vi.fn();
  disconnect = vi.fn();
}
global.ResizeObserver = MockResizeObserver as unknown as typeof ResizeObserver;
Element.prototype.scrollIntoView = vi.fn();

const getChatSessionMock = vi.hoisted(() => vi.fn(async () => ({ messages: [] })));
const createChatSessionMock = vi.hoisted(() => vi.fn(async () => ({ id: 8 })));
const addChatMessagesBatchMock = vi.hoisted(() => vi.fn());
const chatStreamMock = vi.hoisted(() => vi.fn());

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    getChatSession: getChatSessionMock,
    chatStream: (...args: unknown[]) => chatStreamMock(...args),
    addChatMessagesBatch: (...args: unknown[]) => addChatMessagesBatchMock(...args),
    createChatSession: (...args: unknown[]) => createChatSessionMock(...args),
    truncateChatSession: vi.fn(async () => ({ remaining_count: 0, tail_seq: 0 })),
    forkChatSession: vi.fn(async () => ({ id: 99, messages: [] })),
    listChatSessions: vi.fn(async () => ({ sessions: [] })),
    listAccessibleVaults: vi.fn(async () => ({ vaults: [] })),
    getLlmModeHealth: vi.fn(async () => ({ thinking: true, instant: true })),
  };
});

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
    // QueryClientProvider: once a turn's rows migrate to numeric server ids,
    // AssistantMessage's CanvasEntryPoints bridge mounts a react-query hook —
    // without a client the ErrorBoundary replaces the whole chat area.
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      <BrowserRouter>
        <Routes>
          <Route path="/chat" element={<ChatShell />} />
          <Route path="/chat/:sessionId" element={<ChatShell />} />
        </Routes>
      </BrowserRouter>
    </QueryClientProvider>
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

describe("ChatShell session load retry behavior (issue #685)", () => {
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
    vi.restoreAllMocks();
    window.history.pushState({}, "", "/");
  });

  it("retry click re-fetches and loads the session", async () => {
    const loadedDetail = {
      messages: [
        {
          id: 71,
          session_id: 7,
          role: "user",
          content: "stored question",
          created_at: "2026-05-12T00:00:00Z",
          seq: 1,
        },
      ],
    };
    getChatSessionMock
      .mockClear()
      .mockRejectedValueOnce(new Error("transient load failure"))
      .mockResolvedValue(loadedDetail);

    window.history.pushState({}, "", "/chat/7");
    renderChatShellRoutes();

    await waitFor(() => expect(getChatSessionMock).toHaveBeenCalledTimes(1));
    await flushMacroTasks();

    const retryButtons = screen.queryAllByRole("button", { name: /retry/i });
    expect(retryButtons.length).toBe(1);

    await act(async () => {
      fireEvent.click(retryButtons[0]);
    });

    await waitFor(() => expect(getChatSessionMock).toHaveBeenCalledTimes(2));
    await waitFor(() => {
      expect(useChatStore.getState().activeChatId).toBe("7");
    });
    expect(useChatStore.getState().messageIds.length).toBe(1);
    // A successful retry clears the failure surface.
    expect(screen.queryAllByRole("button", { name: /retry/i }).length).toBe(0);
  });

  it("superseded rejection does not clobber the newer selection", async () => {
    // The store already holds session 1 (the "newer selection" the user
    // returned to); session 2's fetch will REJECT after that return.
    useChatStore.setState({
      activeChatId: "1",
      messageIds: ["1u"],
      messagesById: {
        "1u": {
          id: "1u",
          role: "user",
          content: "session one question",
          created_at: "2026-05-12T00:00:00Z",
        },
      },
    });

    let rejectFetch2!: (reason: Error) => void;
    getChatSessionMock.mockClear().mockImplementationOnce(
      () =>
        new Promise((_resolve, reject) => {
          rejectFetch2 = reject;
        })
    );

    window.history.pushState({}, "", "/chat/1");
    renderChatShellRoutes();
    await flushMacroTasks();
    expect(getChatSessionMock).toHaveBeenCalledTimes(0); // session 1 already active

    navigate("/chat/2");
    await waitFor(() => expect(getChatSessionMock).toHaveBeenCalledTimes(1));

    navigate("/chat/1"); // back to the store-held session while 2 is pending
    await flushMacroTasks();

    await act(async () => {
      rejectFetch2(new Error("session 2 fetch failed"));
    });
    await flushMacroTasks();

    // The stale failure is discarded: no error banner, no state clobber.
    const state = useChatStore.getState();
    expect(state.activeChatId).toBe("1");
    expect(state.messageIds).toEqual(["1u"]);
    expect(screen.queryAllByRole("button", { name: /retry/i }).length).toBe(0);
  });

  it("first send replaces the URL with the new session route", async () => {
    // Reviewer revision (issue-tracer 4.5): the navigate(replace) half of the
    // #685 first-send binding is pinned here — C2 asserts only the shell
    // store, and no other test observes the URL after a first send.
    addChatMessagesBatchMock.mockResolvedValue([
      { id: 100, created_at: "2026-05-12T00:00:00Z", seq: 1 },
      { id: 101, created_at: "2026-05-12T00:00:01Z", seq: 2 },
    ]);
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

    window.history.pushState({}, "", "/chat");
    renderChatShellRoutes();

    const composer = screen.getByLabelText("Message input");
    fireEvent.change(composer, { target: { value: "first question" } });
    await act(async () => {
      fireEvent.keyDown(composer, { key: "Enter" });
    });

    await waitFor(() => expect(createChatSessionMock).toHaveBeenCalledTimes(1));
    await waitFor(() => {
      expect(window.location.pathname).toBe("/chat/8");
    });
  });

  it("navigating to New chat clears the held session from the chat store", async () => {
    // Reviewer revision (issue-tracer 4.5): the newChat() on the !sessionId
    // branch is pinned here — C4's store starts empty, so the token fix
    // alone satisfies it; this test holds a session and drops it by URL.
    useChatStore.setState({
      activeChatId: "5",
      messageIds: ["5u"],
      messagesById: {
        "5u": {
          id: "5u",
          role: "user",
          content: "held question",
          created_at: "2026-05-12T00:00:00Z",
        },
      },
    });

    window.history.pushState({}, "", "/chat/5");
    renderChatShellRoutes();
    await flushMacroTasks();
    // Session 5 is already the store-held session: no fetch, still active.
    expect(getChatSessionMock).toHaveBeenCalledTimes(0);
    expect(useChatStore.getState().activeChatId).toBe("5");

    navigate("/chat");
    await flushMacroTasks();

    const state = useChatStore.getState();
    expect(state.activeChatId).toBeNull();
    expect(state.messageIds).toEqual([]);
  });

  it("stop during a live stream shows the interrupted banner (PRR-004 DOM path)", async () => {
    // Review finding PRR-004: no unit test clicked Stop and asserted the
    // banner. chatStream stays pending (the e2e "SLOW" shape) so the turn is
    // live when Stop is clicked; persistStop must stamp the assistant row
    // interrupted and TranscriptPane must render its status banner.
    addChatMessagesBatchMock.mockResolvedValue([
      { id: 100, created_at: "2026-05-12T00:00:00Z", seq: 1 },
      { id: 101, created_at: "2026-05-12T00:00:01Z", seq: 2 },
    ]);
    // One chunk then silence: the e2e "SLOW" shape (a content-less assistant
    // row is never persisted — LIVE-01).
    chatStreamMock.mockImplementation(
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

    window.history.pushState({}, "", "/chat");
    renderChatShellRoutes();

    const composer = screen.getByLabelText("Message input");
    fireEvent.change(composer, { target: { value: "SLOW: tell me more" } });
    await act(async () => {
      fireEvent.keyDown(composer, { key: "Enter" });
    });

    await waitFor(() => {
      expect(useChatStore.getState().isStreaming).toBe(true);
      expect(useChatStore.getState().messageIds.length).toBe(2);
    });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Stop generating" }));
    });

    await waitFor(() => {
      expect(
        document.querySelector('[data-interrupted-status="interrupted"]')
      ).not.toBeNull();
    });
    // The durable path ran for the interrupted turn.
    await waitFor(() => {
      expect(addChatMessagesBatchMock).toHaveBeenCalledWith(
        8,
        expect.arrayContaining([expect.objectContaining({ status: "interrupted" })])
      );
    });
  });
});
