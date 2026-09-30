// frontend/src/components/chat/TranscriptPane.doubleActivation.test.tsx
// Issue #684 (C4): two synchronous activations of Retry/Fork must issue at
// most ONE server call. Today handleRetry/handleEdit guard only on the
// render-time `isStreaming` (a state that has not changed between the two
// clicks), and handleFork reads `isForkingRef.current` BEFORE
// `await awaitPendingPersist()` but sets it only after — so a double-click
// issues TWO truncate/fork requests. The contract: at most one server call
// per activation pair.
//
// Harness: the TranscriptPane.revision.test.tsx mocked-store pattern (plain
// vi.fn() exports for the api fns the pane touches, mocked
// MessageBubble/AssistantMessage exposing accessible Edit/Retry/Fork controls
// by message id). The truncate/fork mocks return manually-resolved DEFERRED
// promises so the guard window is observable, and the two activations are
// fired with fireEvent.click (synchronous — neither activation reaches an
// await before the second click lands).

import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { TranscriptPane } from "./TranscriptPane";
import { useChatStore } from "@/stores/useChatStore";
import { useVaultStore } from "@/stores/useVaultStore";

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

// Shared mock state — must be hoisted so vi.mock factories can close over it
const mockChatState = vi.hoisted(() => ({
  messageIds: [] as string[],
  messagesById: {} as Record<string, any>,
  input: "",
  isStreaming: false,
  streamingMessageId: null as string | null,
  inputError: null as string | null,
  expandedSources: new Set<string>(),
  activeChatId: null as string | null,
  abortFn: null,
  pendingTurnPersist: null as Promise<void> | null,
  setInput: vi.fn(),
  setIsStreaming: vi.fn(),
  setAbortFn: vi.fn(),
  setInputError: vi.fn(),
  addMessage: vi.fn(),
  updateMessage: vi.fn(),
  appendToMessage: vi.fn(),
  removeMessagesFrom: vi.fn(),
  stopStreaming: vi.fn(),
  loadChat: vi.fn(),
  newChat: vi.fn(),
}));
const mockNavigate = vi.hoisted(() => vi.fn());
const mockSendDirect = vi.hoisted(() => vi.fn());
const mockRefreshHistory = vi.hoisted(() => vi.fn());
const mockGetActiveVault = vi.hoisted(() => vi.fn());

vi.mock("@/stores/useChatStore", () => ({
  useChatStore: vi.fn((selector?: (s: typeof mockChatState) => unknown) =>
    typeof selector === "function" ? selector(mockChatState) : mockChatState
  ),
  useMessageIds: vi.fn(() => mockChatState.messageIds),
  useMessage: vi.fn((id: string) => mockChatState.messagesById[id]),
  useChatMessages: vi.fn(() =>
    mockChatState.messageIds.map((id) => mockChatState.messagesById[id])
  ),
  useChatInput: vi.fn(() => mockChatState.input),
  useChatIsStreaming: vi.fn(() => mockChatState.isStreaming),
  useChatInputError: vi.fn(() => mockChatState.inputError),
  useChatActiveChatId: vi.fn(() => mockChatState.activeChatId),
  useChatStreamingId: vi.fn(() => mockChatState.streamingMessageId),
  useStreamingMessageContentLength: vi.fn(() => {
    const id = mockChatState.streamingMessageId;
    if (!id) return 0;
    return (mockChatState.messagesById[id]?.content ?? "").length;
  }),
}));
vi.mock("@/stores/useVaultStore");
vi.mock("@/stores/useAuthStore", () => ({
  useAuthStore: vi.fn(() => ({ user: null })),
}));
vi.mock("@/stores/useChatShellStore", () => ({
  useChatShellStore: vi.fn((selector?: (s: any) => unknown) => {
    const state = {
      activeSessionId: "77",
      activeSessionTitle: null,
      openRightPane: vi.fn(),
      closeRightPane: vi.fn(),
      setActiveRightTab: vi.fn(),
      activeRightTab: "evidence",
      selectedEvidenceSource: null,
      setSelectedEvidenceSource: vi.fn(),
    };
    return typeof selector === "function" ? selector(state) : state;
  }),
}));
vi.mock("@/hooks/useSendMessage", () => ({
  useSendMessage: () => ({
    handleSend: vi.fn(),
    handleStop: vi.fn(),
    sendDirect: mockSendDirect,
    currentStage: null,
  }),
  MAX_INPUT_LENGTH: 100_000, // mirrors the real constant (issue #616); hoisted mock factories cannot import it
}));
vi.mock("@/hooks/useChatHistory", () => ({
  useChatHistory: () => ({
    refreshHistory: mockRefreshHistory,
    chatHistory: [],
    isChatLoading: false,
    chatHistoryError: null,
  }),
}));
vi.mock("@/lib/api", () => ({
  truncateChatSession: vi.fn(),
  forkChatSession: vi.fn(),
  getChatSession: vi.fn(),
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
    onFork,
  }: {
    message: { id: string; role: string; content: string };
    onEdit?: (messageId: string, content: string) => void;
    onFork?: () => void;
  }) => (
    <div data-testid="message-bubble" data-message-id={message.id}>
      {message.content}
      {onEdit && (
        <button type="button" aria-label={`Edit ${message.id}`} onClick={() => onEdit(message.id, message.content)}>
          Edit
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
      <div data-testid="motion-div" {...props}>{children}</div>
    ),
  },
  AnimatePresence: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  useReducedMotion: () => false,
}));

import { truncateChatSession, forkChatSession } from "@/lib/api";

// Helper to set messages in both normalized fields
function setMockMessages(messages: Array<{ id: string; role: string; content: string; [key: string]: any }>) {
  mockChatState.messageIds = messages.map((m) => m.id);
  mockChatState.messagesById = Object.fromEntries(messages.map((m) => [m.id, m]));
}

describe("TranscriptPane double activation (issue #684)", () => {
  beforeEach(() => {
    vi.clearAllMocks();

    // Reset shared mock state
    mockChatState.messageIds = [];
    mockChatState.messagesById = {};
    mockChatState.input = "";
    mockChatState.isStreaming = false;
    mockChatState.streamingMessageId = null;
    mockChatState.inputError = null;
    mockChatState.activeChatId = null;
    mockChatState.pendingTurnPersist = null;
    mockChatState.setInput = vi.fn();
    mockChatState.setIsStreaming = vi.fn();
    mockChatState.setAbortFn = vi.fn();
    mockChatState.setInputError = vi.fn();
    mockChatState.addMessage = vi.fn();
    mockChatState.updateMessage = vi.fn();
    mockChatState.stopStreaming = vi.fn();
    mockChatState.newChat = vi.fn();
    // The revision handlers read fresh state through getState().
    (useChatStore as unknown as { getState: () => typeof mockChatState }).getState = () => mockChatState;

    // removeMessagesFrom MUTATES the mock store (mirrors the revision suite).
    mockChatState.removeMessagesFrom = vi.fn((index: number) => {
      mockChatState.messageIds = mockChatState.messageIds.slice(0, index);
      const next: Record<string, any> = {};
      for (const id of mockChatState.messageIds) next[id] = mockChatState.messagesById[id];
      mockChatState.messagesById = next;
    });
    mockChatState.loadChat = vi.fn((chatId: string, messages: any[]) => {
      mockChatState.activeChatId = chatId;
      mockChatState.messageIds = messages.map((m) => m.id);
      mockChatState.messagesById = Object.fromEntries(messages.map((m) => [m.id, m]));
    });

    // Mock useVaultStore with selector support
    (useVaultStore as unknown as ReturnType<typeof vi.fn>).mockImplementation((selector) => {
      const state = {
        vaults: [{ id: 1, name: "Test Vault", file_count: 5 }],
        activeVaultId: 1,
        getActiveVault: mockGetActiveVault,
      };
      return selector ? selector(state) : state;
    });
    mockGetActiveVault.mockReturnValue({ id: 1, name: "Test Vault", file_count: 5 });
    mockRefreshHistory.mockResolvedValue(undefined);
  });

  it("double Retry issues one truncate", async () => {
    mockChatState.activeChatId = "77";
    setMockMessages([
      { id: "m1", role: "user", content: "first question", seq: 1 },
      { id: "m2", role: "assistant", content: "first answer", seq: 2 },
      { id: "m3", role: "user", content: "second question", seq: 3 },
      { id: "m4", role: "assistant", content: "second answer", seq: 4 },
    ]);

    // Deferred server truncate: pending until the test resolves it.
    let resolveTruncate!: (value: Awaited<ReturnType<typeof truncateChatSession>>) => void;
    const truncatePromise = new Promise<Awaited<ReturnType<typeof truncateChatSession>>>((resolve) => {
      resolveTruncate = resolve;
    });
    vi.mocked(truncateChatSession).mockReturnValue(truncatePromise);

    render(<TranscriptPane />);

    // TWO SYNCHRONOUS activations — neither reaches an await before the
    // second click lands (fireEvent, not awaited userEvent).
    const retryButton = screen.getByLabelText("Retry m4");
    fireEvent.click(retryButton);
    fireEvent.click(retryButton);

    // Settle: resolve the deferred truncate and flush microtasks/timers so a
    // wrongly-admitted second activation would have issued its call.
    await act(async () => {
      resolveTruncate({ remaining_count: 2, tail_seq: 2 });
      await Promise.resolve();
    });
    await waitFor(() => expect(mockSendDirect).toHaveBeenCalled());

    // One activation pair, one server call.
    expect(truncateChatSession.mock.calls.length).toBe(1);
  });

  it("double Fork issues one fork request", async () => {
    mockChatState.activeChatId = "77";
    // seq-carrying rows: the fork anchor (issue #684, T1-13-K-02) is the
    // highest durable seq up to the clicked message; this test exercises the
    // double-activation guard on the normal durable path (the zero-durable
    // refusal is pinned by TranscriptPane.forkZeroAnchor.test.tsx).
    setMockMessages([
      { id: "m1", role: "user", content: "Question", seq: 1 },
      { id: "m2", role: "assistant", content: "Answer", seq: 2 },
    ]);

    // Deferred server fork: pending until the test resolves it.
    let resolveFork!: (value: Awaited<ReturnType<typeof forkChatSession>>) => void;
    const forkPromise = new Promise<Awaited<ReturnType<typeof forkChatSession>>>((resolve) => {
      resolveFork = resolve;
    });
    vi.mocked(forkChatSession).mockReturnValue(forkPromise);

    render(<TranscriptPane />);

    // TWO SYNCHRONOUS activations on the same message's Fork control.
    const forkButton = screen.getByLabelText("Fork m2");
    fireEvent.click(forkButton);
    fireEvent.click(forkButton);

    // Settle: resolve the deferred fork with a well-formed branched session.
    await act(async () => {
      resolveFork({
        id: 20,
        vault_id: 1,
        title: "Branch of Original",
        created_at: "2026-05-12T00:00:00Z",
        updated_at: "2026-05-12T00:00:00Z",
        forked_from_session_id: 77,
        fork_message_index: 1,
        messages: [
          { id: 101, role: "user", content: "Question", sources: null, created_at: "2026-05-12T00:00:00Z" },
        ],
      });
      await Promise.resolve();
    });
    await waitFor(() => expect(mockNavigate).toHaveBeenCalledWith("/chat/20"));

    // One activation pair, one server call.
    expect(forkChatSession.mock.calls.length).toBe(1);
  });
});
