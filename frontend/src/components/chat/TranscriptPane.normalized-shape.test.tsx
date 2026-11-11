// frontend/src/components/chat/TranscriptPane.normalized-shape.test.tsx
// Issue #1078 AC1: TranscriptPane's two stale-view 409 arms (the retry
// truncate catch and the edit truncate catch) must fire for the error shape
// the api client ACTUALLY rejects with. core.ts's response interceptor
// returns a normalized Error whose status is at the TOP level and which has
// NO `.response` property (`originalError` nests the raw axios error). Both
// arms read only `err.response.status`, so a real 409 fell through to the
// generic "Couldn't update conversation history" toast and the tab never
// reloaded — the convergence the 409 exists to provide (#684) was dead in
// production.
//
// Contract asserted at BOTH ends (the ActivityTray.normalized-403 pattern):
// the normalized legs (top-level `status`, and the re-wrapped
// `originalError.response.status`-only tier) are RED at base and GREEN
// after the fix; the raw-shape control leg is green at base and must stay
// green (AC2 — the widening is a superset).
//
// Harness: the TranscriptPane.staleRevision.test.tsx shape — ChatShell at a
// real router route with the REAL chat/shell/vault stores and the REAL
// TranscriptPane; only the sessions API module is mocked, so the readers
// under test run against the production shared helper from core.ts.

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
// send path, the history rail — sees these fns). "@/lib/api/core" is NOT
// mocked: the status readers under test run the real shared helper.
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

vi.mock("@/lib/api/settings", () => ({
  getSettings: async () => ({ chat_message_max_chars: 20000 }),
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

/**
 * Replica of core.ts's interceptor normalizer tail (the shape every
 * apiClient rejection carries): top-level `status`, raw axios error under
 * `originalError`, and NO `response` property on the error itself.
 */
function normalizedReject(status: number): Error {
  const error = new Error(`Request failed with status code ${status}`);
  error.name = "APIError";
  (error as unknown as { status?: number }).status = status;
  (error as unknown as { originalError?: unknown }).originalError = {
    response: { status },
  };
  return error;
}

/** The re-wrapped tier: status only under `originalError.response.status`. */
function originalTierReject(status: number): Error {
  const error = new Error(`Request failed with status code ${status}`);
  error.name = "APIError";
  (error as unknown as { originalError?: unknown }).originalError = {
    response: { status },
  };
  return error;
}

// This tab's stale view: 2 rows. The post-409 reload shows the server's
// 4-row truth — rows 3/4 were saved by another tab this one never saw, and
// row 4 carries the convergence marker.
const STALE_VIEW = {
  id: 42,
  vault_id: 1,
  title: "Stale view",
  created_at: "2026-05-12T00:00:00Z",
  updated_at: "2026-05-12T00:00:00Z",
  messages: [
    { id: 1, role: "user", content: "first question", created_at: "2026-05-12T00:00:01Z", seq: 1 },
    { id: 2, role: "assistant", content: "first answer", created_at: "2026-05-12T00:00:02Z", seq: 2, status: "complete" },
  ],
};

const CONVERGED_VIEW = {
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
};

describe("TranscriptPane 409 arms read the api client's normalized error (issue #1078)", () => {
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
      messageEditVersions: {},
      activeEditVersion: {},
    });

    sessionsApiMocks.getChatSession
      .mockResolvedValueOnce(STALE_VIEW)
      .mockResolvedValueOnce(CONVERGED_VIEW);
    sessionsApiMocks.listChatSessions.mockResolvedValue({ sessions: [] });
    sessionsApiMocks.chatStream.mockReturnValue(vi.fn());
    sessionsApiMocks.createChatSession.mockResolvedValue({ id: 99 });
    sessionsApiMocks.addChatMessagesBatch.mockResolvedValue([]);
    sessionsApiMocks.addChatMessagesBatchKeepalive.mockResolvedValue(undefined);
    sessionsApiMocks.forkChatSession.mockResolvedValue({ id: 20, messages: [] });
  });

  async function renderStaleView(): Promise<void> {
    window.history.pushState({}, "", "/chat/42");
    render(
      <BrowserRouter>
        <Routes>
          <Route path="/chat/:sessionId" element={<ChatShell />} />
        </Routes>
      </BrowserRouter>
    );

    await waitFor(() => {
      expect(sessionsApiMocks.getChatSession).toHaveBeenCalledTimes(1);
    });
    await waitFor(() => {
      expect(useChatStore.getState().messageIds).toEqual(["1", "2"]);
    });
  }

  async function assertConvergedFromServer(): Promise<void> {
    await waitFor(() => {
      expect(sessionsApiMocks.truncateChatSession).toHaveBeenCalledTimes(1);
    });
    // The 409 arm reloaded the session: the unseen 4th row (the assistant
    // marker message) becomes visible exactly once.
    expect(screen.queryAllByText(/unseen-answer-4/).length).toBe(1);
    expect(sessionsApiMocks.getChatSession).toHaveBeenCalledTimes(2);

    window.history.pushState({}, "", "/");
  }

  it("retry arm: a NORMALIZED 409 (top-level status) reloads the session from the server", async () => {
    sessionsApiMocks.truncateChatSession.mockRejectedValue(normalizedReject(409));

    await renderStaleView();

    await act(async () => {
      await userEvent.click(screen.getByLabelText("Retry 2"));
    });

    await assertConvergedFromServer();
  });

  it("retry arm: the 409 reloads even when its status only survives under originalError.response.status (re-wrapped tier)", async () => {
    sessionsApiMocks.truncateChatSession.mockRejectedValue(originalTierReject(409));

    await renderStaleView();

    await act(async () => {
      await userEvent.click(screen.getByLabelText("Retry 2"));
    });

    await assertConvergedFromServer();
  });

  it("edit arm: a NORMALIZED 409 (top-level status) reloads the session from the server", async () => {
    sessionsApiMocks.truncateChatSession.mockRejectedValue(normalizedReject(409));

    await renderStaleView();

    await act(async () => {
      await userEvent.click(screen.getByLabelText("Edit 1"));
    });

    await assertConvergedFromServer();
  });

  it("both arms: the RAW axios shape { response: { status: 409 } } still reloads (control leg — superset)", async () => {
    sessionsApiMocks.truncateChatSession.mockRejectedValue({ response: { status: 409 } });

    await renderStaleView();

    await act(async () => {
      await userEvent.click(screen.getByLabelText("Retry 2"));
    });

    await assertConvergedFromServer();
  });
});
