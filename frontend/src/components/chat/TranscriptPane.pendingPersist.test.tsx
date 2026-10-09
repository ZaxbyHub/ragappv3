// frontend/src/components/chat/TranscriptPane.pendingPersist.test.tsx
// Issue #684 (C5): a revision must wait for EVERY in-flight turn save. The
// store's `pendingTurnPersist` is a single slot — a second turn's save
// OVERWRITES the first's in-flight promise, so awaitPendingPersist misses the
// older save and the truncate can race it (deleting rows the older save then
// resurrects, or trimming before the rows land). The contract: with BOTH
// saves in flight and only the newer one resolved, Retry must still be
// waiting for the older save — no truncate may have been issued.
//
// Harness: the REAL useSendMessage hook into the REAL chat store (the
// TranscriptPane.liveSeqRevision.test.tsx pattern). addChatMessagesBatch
// returns manually-resolved DEFERRED promises P1 (turn 1) and P2 (turn 2);
// the test resolves ONLY P2, clicks Retry, flushes microtasks, and asserts
// the truncate was never issued.

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

describe("TranscriptPane revision vs in-flight saves (issue #684)", () => {
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
    truncateChatSessionMock.mockResolvedValue({ remaining_count: 0, tail_seq: 0 });

    // Successful stream shape (liveSeqRevision pattern); any later re-send
    // falls back to a well-formed batch result so the flow settles.
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
    addChatMessagesBatchMock.mockResolvedValue([
      { id: 900, created_at: "2026-05-12T00:00:06Z", seq: 5 },
      { id: 901, created_at: "2026-05-12T00:00:07Z", seq: 6 },
    ]);

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

  it("revision waits for every in-flight turn save", async () => {
    // Two deferred durable saves: P1 (turn 1) and P2 (turn 2), both in flight.
    let resolveP1!: (value: Array<{ id: number; created_at: string; seq: number }>) => void;
    let resolveP2!: (value: Array<{ id: number; created_at: string; seq: number }>) => void;
    const P1 = new Promise<Array<{ id: number; created_at: string; seq: number }>>(
      (resolve) => {
        resolveP1 = resolve;
      },
    );
    const P2 = new Promise<Array<{ id: number; created_at: string; seq: number }>>(
      (resolve) => {
        resolveP2 = resolve;
      },
    );
    addChatMessagesBatchMock
      .mockReturnValueOnce(P1)
      .mockReturnValueOnce(P2)
      // Any later re-send settles against a resolved save.
      .mockResolvedValue([
        { id: 900, created_at: "2026-05-12T00:00:06Z", seq: 5 },
        { id: 901, created_at: "2026-05-12T00:00:07Z", seq: 6 },
      ]);

    const { result } = renderHook(() => useSendMessage(7, mockRefreshHistory));

    // Turn 1: the stream completes, its durable save (P1) is in flight.
    useChatStore.setState({ input: "first question" });
    await act(async () => {
      await result.current.handleSend();
    });
    await waitFor(() => expect(addChatMessagesBatchMock).toHaveBeenCalledTimes(1));

    // Real-clock gap so the second turn's Date.now()-derived local message
    // ids cannot collide with the first turn's (ids are Date.now()-based).
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 5));
    });

    // Turn 2: its durable save (P2) is ALSO in flight — P1 still pending.
    useChatStore.setState({ input: "second question" });
    await act(async () => {
      await result.current.handleSend();
    });
    await waitFor(() => expect(addChatMessagesBatchMock).toHaveBeenCalledTimes(2));

    // Resolve ONLY P2: the single-slot pendingTurnPersist clears (the newer
    // save's finally sees its own promise), while P1 is still in flight.
    await act(async () => {
      resolveP2([
        { id: 102, created_at: "2026-05-12T00:00:02Z", seq: 3 },
        { id: 103, created_at: "2026-05-12T00:00:03Z", seq: 4 },
      ]);
      await Promise.resolve();
    });
    await waitFor(() => {
      expect(useChatStore.getState().pendingTurnPersist).toBeNull();
    });

    render(<TranscriptPane />);

    // Retry the second turn (its assistant row migrated to server id 103).
    const ids = useChatStore.getState().messageIds;
    expect(ids).toHaveLength(4);
    await act(async () => {
      await userEvent.click(screen.getByLabelText(`Retry ${ids[3]}`));
    });
    await act(async () => {
      await Promise.resolve();
    });

    // The revision must STILL be waiting for turn 1's save (P1): no truncate
    // may have been issued. The single-slot store misses P1 and lets the
    // truncate fire at the pre-fix tree.
    expect(truncateChatSessionMock.mock.calls.length).toBe(0);
  });
});
