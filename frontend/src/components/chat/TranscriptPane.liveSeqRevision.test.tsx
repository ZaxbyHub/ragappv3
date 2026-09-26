// frontend/src/components/chat/TranscriptPane.liveSeqRevision.test.tsx
// Issue #683 end-to-end: two turns saved through the REAL live-send path
// (useSendMessage + addChatMessagesBatch returning contract-accurate rows
// with seq), then Retry/Edit on the real TranscriptPane. The truncate anchor
// (durableKeepSeq) must be the highest durable seq among the KEPT rows —
// turn 1's rows carry server seq 1/2, so keep_seq must be 2. When the
// live-save migration drops seq, the anchor collapses to 0 and the server
// deletes EVERY row (DELETE ... WHERE seq > 0) — turn 1 is permanently lost.
//
// Harness mix: the send-path mocks mirror useSendMessage.test.ts (hoisted
// apiMocks with wrapper factories); the pane-side mocks mirror
// TranscriptPane.revision.test.tsx (plain vi.fn() exports for the api fns
// the pane touches, mocked MessageBubble/AssistantMessage exposing
// accessible Edit/Retry controls by message id). The chat store, the send
// hook, and the shell stores are the REAL modules — this is the live path,
// not a seeded store.

import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, render, renderHook, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { TranscriptPane } from "./TranscriptPane";
import { useSendMessage } from "@/hooks/useSendMessage";
import { useChatStore } from "@/stores/useChatStore";
import { useChatModeStore } from "@/stores/useChatModeStore";
import { useChatShellStore } from "@/stores/useChatShellStore";
import { useLlmHealthStore } from "@/stores/useLlmHealthStore";

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

// Send-path mocks (mirror useSendMessage.test.ts) plus the pane's revision
// endpoints (exposed as plain vi.fn()s so vi.mocked(...) assertions work,
// mirroring TranscriptPane.revision.test.tsx).
const apiMocks = vi.hoisted(() => ({
  createChatSession: vi.fn(),
  addChatMessagesBatch: vi.fn(),
  addChatMessagesBatchKeepalive: vi.fn(),
  chatStream: vi.fn(),
}));
const truncateChatSessionMock = vi.hoisted(() => vi.fn());
const forkChatSessionMock = vi.hoisted(() => vi.fn());
const mockRefreshHistory = vi.hoisted(() => vi.fn());
const mockNavigate = vi.hoisted(() => vi.fn());
const mockGetActiveVault = vi.hoisted(() => vi.fn());

vi.mock("@/lib/api", () => ({
  createChatSession: (...args: unknown[]) => apiMocks.createChatSession(...args),
  addChatMessagesBatch: (...args: unknown[]) => apiMocks.addChatMessagesBatch(...args),
  addChatMessagesBatchKeepalive: (...args: unknown[]) =>
    apiMocks.addChatMessagesBatchKeepalive(...args),
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

import { truncateChatSession } from "@/lib/api";
import { useVaultStore } from "@/stores/useVaultStore";

/**
 * Drive TWO complete live sends through the real useSendMessage hook into the
 * real chat store. The batch mock returns contract-accurate rows with a
 * DIFFERENT id/seq pair per send:
 *   send 1 → user 100 (seq 1), assistant 101 (seq 2)
 *   send 2 → user 102 (seq 3), assistant 103 (seq 4)
 * Any later re-send (Retry's sendDirect) falls back to a well-formed result
 * so the flow settles without dangling promises.
 */
async function runTwoLiveSends(): Promise<void> {
  apiMocks.addChatMessagesBatch
    .mockResolvedValueOnce([
      { id: 100, created_at: "2026-05-12T00:00:00Z", seq: 1 },
      { id: 101, created_at: "2026-05-12T00:00:01Z", seq: 2 },
    ])
    .mockResolvedValueOnce([
      { id: 102, created_at: "2026-05-12T00:00:02Z", seq: 3 },
      { id: 103, created_at: "2026-05-12T00:00:03Z", seq: 4 },
    ])
    .mockResolvedValue([
      { id: 200, created_at: "2026-05-12T00:00:04Z", seq: 5 },
      { id: 201, created_at: "2026-05-12T00:00:05Z", seq: 6 },
    ]);

  const { result } = renderHook(() => useSendMessage(7, mockRefreshHistory));

  useChatStore.setState({ input: "first question" });
  await act(async () => {
    await result.current.handleSend();
  });
  await waitFor(() => expect(mockRefreshHistory).toHaveBeenCalledTimes(1));

  useChatStore.setState({ input: "second question" });
  await act(async () => {
    await result.current.handleSend();
  });
  await waitFor(() => expect(mockRefreshHistory).toHaveBeenCalledTimes(2));

  // Fully settled: the background durable save promise is cleared and all
  // four server-issued ids are migrated into the store.
  await waitFor(() => {
    expect(useChatStore.getState().pendingTurnPersist).toBeNull();
  });
  const state = useChatStore.getState();
  expect(state.activeChatId).toBe("42");
  expect(state.messageIds).toEqual(["100", "101", "102", "103"]);
  for (const id of ["100", "101", "102", "103"]) {
    expect(state.messagesById[id]).toBeDefined();
  }
}

describe("TranscriptPane revision after live-saved turns (issue #683)", () => {
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
    apiMocks.chatStream.mockImplementation(
      (
        _messages: unknown,
        handlers: {
          onMessage: (chunk: string) => void;
          onComplete: () => Promise<void>;
        },
      ) => {
        handlers.onMessage("hello");
        void handlers.onComplete();
        return vi.fn();
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

  it("retry after two live-saved turns keeps turn 1 on the server", async () => {
    await runTwoLiveSends();

    // Hold the truncate pending so the anchor argument is observable before
    // the flow continues; resolve it afterwards so nothing dangles.
    let resolveTruncate!: (value: Awaited<ReturnType<typeof truncateChatSession>>) => void;
    const truncatePromise = new Promise<Awaited<ReturnType<typeof truncateChatSession>>>(
      (resolve) => {
        resolveTruncate = resolve;
      }
    );
    vi.mocked(truncateChatSession).mockReturnValue(truncatePromise);

    render(<TranscriptPane />);

    // Retry from the turn-2 assistant row (server id 103). The last user
    // message is turn 2's user row (102), so the KEPT rows are turn 1's
    // (100, 101) whose highest durable seq is 2.
    await userEvent.click(screen.getByLabelText("Retry 103"));

    const calls = vi.mocked(truncateChatSession).mock.calls;
    expect(calls.length).toBeGreaterThanOrEqual(1);
    expect(calls[calls.length - 1][1]).toBe(2); // keep_seq = turn-1 assistant seq

    // Settle: resolve the truncate, then wait for the turn-2 re-send (the
    // third durable batch save) so no in-flight promise leaks.
    resolveTruncate({ remaining_count: 2, tail_seq: 2 });
    await waitFor(() => expect(apiMocks.addChatMessagesBatch).toHaveBeenCalledTimes(3));
  });

  it("edit of turn 2 after two live-saved turns keeps turn 1 on the server", async () => {
    await runTwoLiveSends();

    let resolveTruncate!: (value: Awaited<ReturnType<typeof truncateChatSession>>) => void;
    const truncatePromise = new Promise<Awaited<ReturnType<typeof truncateChatSession>>>(
      (resolve) => {
        resolveTruncate = resolve;
      }
    );
    vi.mocked(truncateChatSession).mockReturnValue(truncatePromise);

    render(<TranscriptPane />);

    // Edit the turn-2 user row (server id 102). Everything from that row on
    // is truncated, so the KEPT rows are again turn 1's (100, 101) → 2.
    await userEvent.click(screen.getByLabelText("Edit 102"));

    const calls = vi.mocked(truncateChatSession).mock.calls;
    expect(calls.length).toBeGreaterThanOrEqual(1);
    expect(calls[calls.length - 1][1]).toBe(2); // keep_seq = turn-1 assistant seq

    // Settle: resolve the truncate, then wait for the local trim + composer
    // restore. Edit never re-sends — the two live sends remain the only
    // stream calls.
    resolveTruncate({ remaining_count: 2, tail_seq: 2 });
    await waitFor(() =>
      expect(useChatStore.getState().messageIds).toEqual(["100", "101"])
    );
    expect(useChatStore.getState().input).toBe("second question");
    expect(apiMocks.chatStream).toHaveBeenCalledTimes(2);
  });
});
