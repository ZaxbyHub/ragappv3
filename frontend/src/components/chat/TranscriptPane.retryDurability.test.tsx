// frontend/src/components/chat/TranscriptPane.retryDurability.test.tsx
// Issue #684 (C3): Retry must not destroy the durable original Q&A when the
// replacement stream is rejected before any content. Today handleRetry awaits
// the server truncate BEFORE resending (destructive-first): the truncate
// deletes the original turn's rows, then the replacement stream dies at the
// admission gate (SSE `error` event with code ADMISSION_REJECTED followed by
// `done`) — the server writes no durable rows for such a turn and the empty
// assistant turn persists nothing — so the original Q&A is gone everywhere.
// The contract: the original question+answer rows must still be durable on
// the server afterwards (row count unchanged).
//
// Harness: the REAL useSendMessage hook into the REAL chat store (the
// TranscriptPane.liveSeqRevision.test.tsx pattern) with a STATEFUL in-test
// fake of the sessions API — an internal `rows` array models the server's
// ordered rows, truncateChatSession deletes `seq > keepSeq`, and
// addChatMessagesBatch APPENDS the batch (assigning the next seqs).
//
// Stream mock (review round 1 amendment): the chatStream mock feeds the REAL
// parseSSEStream the server's terminal SSE error frame
// ({"type":"error","code":"ADMISSION_REJECTED"}), so the check pins the
// production path including the parser's error.code attach. The trailing
// `done` frame is inert after the terminal error frame (the real parser
// returns), so no onComplete fires.

import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, render, renderHook, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { TranscriptPane } from "./TranscriptPane";
import { useSendMessage } from "@/hooks/useSendMessage";
import { useChatStore } from "@/stores/useChatStore";
import { useChatModeStore } from "@/stores/useChatModeStore";
import { useChatShellStore } from "@/stores/useChatShellStore";
import { useLlmHealthStore } from "@/stores/useLlmHealthStore";
import { mapSessionMessage } from "@/lib/chatMessageMapper";
import { parseSSEStream } from "@/lib/api/sessions";
import type { AddMessageRequest, ChatSessionMessage } from "@/lib/api";

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

const apiMocks = vi.hoisted(() => ({
  createChatSession: vi.fn(),
  chatStream: vi.fn(),
}));
const truncateChatSessionMock = vi.hoisted(() => vi.fn());
const addChatMessagesBatchMock = vi.hoisted(() => vi.fn());
const forkChatSessionMock = vi.hoisted(() => vi.fn());
const mockRefreshHistory = vi.hoisted(() => vi.fn());
const mockNavigate = vi.hoisted(() => vi.fn());
const mockGetActiveVault = vi.hoisted(() => vi.fn());

vi.mock("@/lib/api", () => ({
  createChatSession: (...args: unknown[]) => apiMocks.createChatSession(...args),
  addChatMessagesBatch: (...args: unknown[]) => addChatMessagesBatchMock(...args),
  addChatMessagesBatchKeepalive: vi.fn(),
  chatStream: (...args: unknown[]) => apiMocks.chatStream(...args),
  truncateChatSession: truncateChatSessionMock,
  forkChatSession: forkChatSessionMock,
}));

vi.mock("@/stores/useVaultStore");
vi.mock("@/stores/useAuthStore", () => ({
  useAuthStore: vi.fn(() => ({ user: null })),
}));
vi.mock("@/hooks/useChatHistory", () => ({
  useChatHistory: () => ({
    refreshHistory: mockRefreshHistory,
    chatHistory: [],
    isChatLoading: false,
    chatHistoryError: null,
  }),
}));
vi.mock("react-router-dom", () => ({
  useNavigate: () => mockNavigate,
  useInRouterContext: () => false,
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

import { useVaultStore } from "@/stores/useVaultStore";

/** Server-ordered row shape the in-test fake stores. */
type ServerRow = {
  id: number;
  role: "user" | "assistant";
  content: string;
  created_at: string;
  seq: number;
  sources: null;
};

const seedRow = (
  id: number,
  role: "user" | "assistant",
  content: string,
  seq: number,
): ServerRow => ({
  id,
  role,
  content,
  created_at: `2026-05-12T00:00:0${seq}:00Z`,
  seq,
  sources: null,
});

/** The STATEFUL fake of the server's session rows (reset per test). */
const server = vi.hoisted(() => ({
  rows: [] as Array<{
    id: number;
    role: "user" | "assistant";
    content: string;
    created_at: string;
    seq: number;
    sources: null;
  }>,
  nextId: 100,
}));

describe("TranscriptPane retry durability (issue #684)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockRefreshHistory.mockResolvedValue(undefined);

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
    useChatShellStore.setState({ sessionListRefreshToken: 0 });
    useChatModeStore.setState({ scopeDocumentIds: null });

    apiMocks.createChatSession.mockResolvedValue({ id: 42 });
    forkChatSessionMock.mockResolvedValue({ id: 20, messages: [] });

    // STATEFUL sessions-API fake: truncate deletes `seq > keepSeq`...
    truncateChatSessionMock.mockImplementation(
      (_sessionId: number, keepSeq: number) => {
        server.rows = server.rows.filter((r) => r.seq <= keepSeq);
        const tail = server.rows.length > 0 ? server.rows[server.rows.length - 1].seq : 0;
        return Promise.resolve({ remaining_count: server.rows.length, tail_seq: tail });
      },
    );
    // ...and a successful batch save APPENDS the batch rows (assigning the
    // next seqs), echoing contract-accurate saved rows back.
    addChatMessagesBatchMock.mockImplementation(
      (_sessionId: number, messages: AddMessageRequest[]) => {
        const saved: ChatSessionMessage[] = [];
        for (const message of messages) {
          const seq = (server.rows[server.rows.length - 1]?.seq ?? 0) + 1;
          const row = seedRow(server.nextId++, message.role as "user" | "assistant", message.content, seq);
          server.rows.push(row);
          saved.push({ ...row });
        }
        return Promise.resolve(saved);
      },
    );

    // Mock useVaultStore with selector support (mirrors the revision suite).
    (useVaultStore as unknown as ReturnType<typeof vi.fn>).mockImplementation((selector) => {
      const state = {
        vaults: [{ id: 1, name: "Test Vault", file_count: 5 }],
        activeVaultId: 1,
        getActiveVault: mockGetActiveVault,
      };
      return selector ? selector(state) : state;
    });
    mockGetActiveVault.mockReturnValue({ id: 1, name: "Test Vault", file_count: 5 });
  });

  it("rejected retry keeps the original QA durable", async () => {
    // Two durable turns on the server (seq 1..4), loaded into the store
    // through the real mapper so store rows carry those seqs.
    server.rows = [
      seedRow(1, "user", "question-1", 1),
      seedRow(2, "assistant", "answer-1", 2),
      seedRow(3, "user", "question-2", 3),
      seedRow(4, "assistant", "answer-2", 4),
    ];
    useChatStore.getState().loadChat("42", server.rows.map(mapSessionMessage));

    // The replacement stream is rejected at the admission gate before any
    // content: the terminal SSE error frame carries ADMISSION_REJECTED. The
    // REAL parseSSEStream consumes the frame (not a fabricated Error), so the
    // parser's error.code attach is part of what this check pins.
    apiMocks.chatStream.mockImplementation(
      async (
        _messages: unknown,
        handlers: Parameters<typeof parseSSEStream>[1],
      ) => {
        const frame =
          "data: " +
          JSON.stringify({
            type: "error",
            message: "Chat capacity is saturated; retry shortly",
            code: "ADMISSION_REJECTED",
          }) +
          "\n\n";
        const reader = new ReadableStream<Uint8Array>({
          start(controller) {
            controller.enqueue(new TextEncoder().encode(frame));
            controller.close();
          },
        }).getReader();
        await parseSSEStream(reader, handlers);
        return vi.fn();
      },
    );

    renderHook(() => useSendMessage(7, mockRefreshHistory));
    render(<TranscriptPane />);

    // Retry turn 2 (the last assistant row is server id 4).
    await act(async () => {
      await userEvent.click(screen.getByLabelText("Retry 4"));
    });

    // The send lifecycle finished: the replacement stream was attempted and
    // the pane is back to a settled, non-streaming state.
    await waitFor(() => {
      expect(apiMocks.chatStream).toHaveBeenCalledTimes(1);
    });
    await waitFor(() => {
      expect(useChatStore.getState().isStreaming).toBe(false);
      expect(useChatStore.getState().streamingMessageId).toBeNull();
    });

    // The server's durable row count is unchanged: the rejected replacement
    // wrote nothing, so the ORIGINAL question+answer rows must still be
    // durable. At the pre-fix tree the truncate already deleted rows 3..4.
    expect(server.rows.length).toBe(4);
  });
});
