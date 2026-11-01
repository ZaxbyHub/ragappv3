// frontend/src/components/chat/TranscriptPane.forkZeroAnchor.test.tsx
// Issue #684 (D5 zero-anchor guard, plan-critic round 3): when the transcript
// up to the clicked message holds ZERO durable rows (nothing has a server
// seq), Fork must refuse LOCALLY — the existing warning toast, no server call
// — and the in-flight guard must reset so a later activation on a durable
// transcript still proceeds. Pins both the local refusal and the
// guard-lifecycle fix (a stranded isForkingRef would silently disable fork).

import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { toast } from "sonner";
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
Element.prototype.scrollTo = vi.fn();

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
  MAX_INPUT_LENGTH: 100_000,
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

import { forkChatSession } from "@/lib/api";

function setMockMessages(messages: Array<{ id: string; role: string; content: string; [key: string]: any }>) {
  mockChatState.messageIds = messages.map((m) => m.id);
  mockChatState.messagesById = Object.fromEntries(messages.map((m) => [m.id, m]));
}

const forkResponse = {
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
};

describe("TranscriptPane fork zero-anchor guard (issue #684)", () => {
  beforeEach(() => {
    vi.clearAllMocks();

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
    (useChatStore as unknown as { getState: () => typeof mockChatState }).getState = () => mockChatState;

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

  it("refuses with a warning and no server call when nothing is durable, then proceeds after recovery", async () => {
    mockChatState.activeChatId = "77";
    // Zero durable rows: neither message carries a server seq (fresh chat or
    // failed saves). Fork at the assistant message must refuse locally.
    setMockMessages([
      { id: "m1", role: "user", content: "Question" },
      { id: "m2", role: "assistant", content: "Answer" },
    ]);
    vi.mocked(forkChatSession).mockResolvedValue(forkResponse);

    render(<TranscriptPane />);

    await act(async () => {
      await userEvent.click(screen.getByLabelText("Fork m2"));
    });

    await waitFor(() => {
      expect(toast.warning).toHaveBeenCalledWith(
        "Fork returned no messages — staying on the current session."
      );
    });
    expect(forkChatSession).not.toHaveBeenCalled();

    // Recovery: the transcript becomes durable (a save landed); the guard was
    // reset by the refusal, so a second activation issues exactly one call
    // anchored on the durable seq.
    setMockMessages([
      { id: "m1", role: "user", content: "Question", seq: 1 },
      { id: "m2", role: "assistant", content: "Answer", seq: 2 },
    ]);

    await act(async () => {
      await userEvent.click(screen.getByLabelText("Fork m2"));
    });

    await waitFor(() => expect(forkChatSession).toHaveBeenCalledTimes(1));
    expect(forkChatSession).toHaveBeenCalledWith(77, { through_seq: 2 });
  });
});
