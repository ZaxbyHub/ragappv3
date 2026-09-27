// frontend/src/components/chat/TranscriptPane.staleRevision.test.tsx
// Issue #684 (C2): when the server REFUSES a retry's truncate with HTTP 409
// (stale view — the session tail moved past what this tab last saw), the
// client must not just toast and give up: it must RELOAD the session from the
// server (getChatSession + store loadChat) so the rows it never saw become
// visible and the tab converges. ChatShell never refetches an already-loaded
// session, so only this explicit reload converges the tab.
//
// Harness: ChatShell rendered at a real router route (the UI-001 pattern from
// ChatShell.test.tsx) with the REAL chat/shell/vault stores and the REAL
// TranscriptPane; only the sessions API module (@/lib/api/sessions — what the
// @/lib/api barrel re-exports) is mocked. MessageBubble/AssistantMessage are
// mocked to expose accessible Retry controls by message id, mirroring the
// TranscriptPane revision suites.

import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { BrowserRouter, Routes, Route } from "react-router-dom";
import ChatShell from "@/pages/ChatShell";
import { useChatStore } from "@/stores/useChatStore";

// Mock ResizeObserver for Radix UI ScrollArea
class MockResizeObserver {
  observe = vi.fn();
  unobserve = vi.fn();
  disconnect = vi.fn();
}
global.ResizeObserver = MockResizeObserver as unknown as typeof ResizeObserver;
Element.prototype.scrollIntoView = vi.fn();
// JSDOM does not implement scrollTo
Element.prototype.scrollTo = vi.fn();

// The sessions API module is mocked wholesale (the @/lib/api barrel re-exports
// it, so every consumer — ChatShell's load, TranscriptPane's truncate, the
// send path, the history rail — sees these fns). Defaults keep every other
// consumer inert; the test overrides getChatSession/truncateChatSession.
const sessionsApiMocks = vi.hoisted(() => ({
  getChatSession: vi.fn(),
  truncateChatSession: vi.fn(),
  forkChatSession: vi.fn(),
  chatStream: vi.fn(),
  createChatSession: vi.fn(),
  addChatMessagesBatch: vi.fn(),
  addChatMessagesBatchKeepalive: vi.fn(),
  listChatSessions: vi.fn(),
}));

vi.mock("@/lib/api/sessions", () => ({
  parseSSEStream: vi.fn(),
  chatStream: (...args: unknown[]) => sessionsApiMocks.chatStream(...args),
  getChatHistory: vi.fn(() => []),
  saveChatHistory: vi.fn(),
  listChatSessions: (...args: unknown[]) => sessionsApiMocks.listChatSessions(...args),
  getChatSession: (...args: unknown[]) => sessionsApiMocks.getChatSession(...args),
  createChatSession: (...args: unknown[]) => sessionsApiMocks.createChatSession(...args),
  addChatMessage: vi.fn(),
  addChatMessagesBatch: (...args: unknown[]) =>
    sessionsApiMocks.addChatMessagesBatch(...args),
  addChatMessagesBatchKeepalive: (...args: unknown[]) =>
    sessionsApiMocks.addChatMessagesBatchKeepalive(...args),
  truncateChatSession: (...args: unknown[]) =>
    sessionsApiMocks.truncateChatSession(...args),
  updateMessageFeedback: vi.fn(),
  updateChatSession: vi.fn(),
  deleteChatSession: vi.fn(),
  forkChatSession: (...args: unknown[]) => sessionsApiMocks.forkChatSession(...args),
}));

vi.mock("sonner", () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
    warning: vi.fn(),
    info: vi.fn(),
  },
}));
vi.mock("./MessageBubble", () => ({
  MessageBubble: ({
    message,
    onEdit,
  }: {
    message: { id: string; role: string; content: string };
    onEdit?: (messageId: string, content: string) => void;
  }) => (
    <div data-testid="message-bubble" data-message-id={message.id}>
      {message.content}
      {onEdit && (
        <button
          type="button"
          aria-label={`Edit ${message.id}`}
          onClick={() => onEdit(message.id, message.content)}
        >
          Edit
        </button>
      )}
    </div>
  ),
}));
vi.mock("./AssistantMessage", () => ({
  AssistantMessage: ({
    message,
    onRetry,
    onFork,
  }: {
    message: { id: string; role: string; content: string };
    onRetry?: () => void;
    onFork?: () => void;
  }) => (
    <div data-testid="message-bubble" data-message-id={message.id}>
      {message.content}
      {onRetry && (
        <button type="button" aria-label={`Retry ${message.id}`} onClick={onRetry}>
          Retry
        </button>
      )}
      {onFork && (
        <button type="button" aria-label={`Fork ${message.id}`} onClick={onFork}>
          Fork
        </button>
      )}
    </div>
  ),
}));
vi.mock("framer-motion", () => ({
  motion: {
    div: ({ children, ...props }: { children: React.ReactNode }) => (
      <div data-testid="motion-div" {...props}>
        {children}
      </div>
    ),
  },
  AnimatePresence: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  useReducedMotion: () => false,
}));

describe("ChatShell stale-view revision reload (issue #684)", () => {
  beforeEach(() => {
    vi.clearAllMocks();

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

    sessionsApiMocks.listChatSessions.mockResolvedValue({ sessions: [] });
    sessionsApiMocks.chatStream.mockReturnValue(vi.fn());
    sessionsApiMocks.createChatSession.mockResolvedValue({ id: 99 });
    sessionsApiMocks.addChatMessagesBatch.mockResolvedValue([]);
    sessionsApiMocks.addChatMessagesBatchKeepalive.mockResolvedValue(undefined);
    sessionsApiMocks.forkChatSession.mockResolvedValue({ id: 20, messages: [] });
  });

  it("stale refusal reloads the session from the server", async () => {
    // Call 1 (initial ChatShell load): this tab's view was built from a
    // 2-message session. Call 2 (the post-409 reload): the server now holds a
    // 4-message session — rows 3/4 were saved by another tab this one never
    // saw, and the 4th row is the assistant answer carrying the marker text.
    sessionsApiMocks.getChatSession
      .mockResolvedValueOnce({
        id: 42,
        vault_id: 1,
        title: "Stale view",
        created_at: "2026-05-12T00:00:00Z",
        updated_at: "2026-05-12T00:00:00Z",
        messages: [
          { id: 1, role: "user", content: "first question", created_at: "2026-05-12T00:00:01Z", seq: 1 },
          { id: 2, role: "assistant", content: "first answer", created_at: "2026-05-12T00:00:02Z", seq: 2, status: "complete" },
        ],
      })
      .mockResolvedValueOnce({
        id: 42,
        vault_id: 1,
        title: "Stale view",
        created_at: "2026-05-12T00:00:00Z",
        updated_at: "2026-05-12T00:00:04Z",
        messages: [
          { id: 1, role: "user", content: "first question", created_at: "2026-05-12T00:00:01Z", seq: 1 },
          { id: 2, role: "assistant", content: "first answer", created_at: "2026-05-12T00:00:02Z", seq: 2, status: "complete" },
          { id: 3, role: "user", content: "unseen question", created_at: "2026-05-12T00:00:03Z", seq: 3 },
          { id: 4, role: "assistant", content: "unseen-answer-4", created_at: "2026-05-12T00:00:04Z", seq: 4, status: "complete" },
        ],
      });
    // The server refuses the truncate with a 409 stale-view conflict
    // (axios-shaped error).
    sessionsApiMocks.truncateChatSession.mockRejectedValue({ response: { status: 409 } });

    // Real router at /chat/42 (the ChatShell.test.tsx UI-001 harness shape).
    window.history.pushState({}, "", "/chat/42");
    render(
      <BrowserRouter>
        <Routes>
          <Route path="/chat/:sessionId" element={<ChatShell />} />
        </Routes>
      </BrowserRouter>
    );

    // The initial load settled: this tab shows its stale 2-message view.
    await waitFor(() => {
      expect(sessionsApiMocks.getChatSession).toHaveBeenCalledTimes(1);
    });
    await waitFor(() => {
      expect(useChatStore.getState().messageIds).toEqual(["1", "2"]);
    });

    // Retry from the last assistant message of the (stale) view.
    await act(async () => {
      await userEvent.click(screen.getByLabelText("Retry 2"));
    });

    // The truncate was attempted — and the server refused it...
    await waitFor(() => {
      expect(sessionsApiMocks.truncateChatSession).toHaveBeenCalledTimes(1);
    });

    // ...so the tab must converge by reloading the session: the unseen 4th
    // row (the assistant marker message) becomes visible exactly once.
    expect(screen.queryAllByText(/unseen-answer-4/).length).toBe(1);

    // Restore the URL for any test that follows.
    window.history.pushState({}, "", "/");
  });
});
