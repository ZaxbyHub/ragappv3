// frontend/src/components/chat/TranscriptPane.revision.test.tsx
// Revision flows on a PERSISTED session (issue #507): retry, edit, fork.
//
// Retry/edit must trim the SERVER history (truncateChatSession) with the
// right keepCount BEFORE touching the local store, and roll everything back
// to a toast when the truncate rejects. Fork must flow through the shared
// mapSessionMessage so kms_refs/mode survive the branch (UI-039).

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
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
  MAX_INPUT_LENGTH: 2000,
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
        <button type="button" aria-label={`Edit ${message.id}`} onClick={() => onEdit(message.id, message.content)}>
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
      <div data-testid="motion-div" {...props}>{children}</div>
    ),
  },
  AnimatePresence: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  useReducedMotion: () => false,
}));

import { truncateChatSession, forkChatSession } from "@/lib/api";
import { toast } from "sonner";

// Helper to set messages in both normalized fields
function setMockMessages(messages: Array<{ id: string; role: string; content: string; [key: string]: any }>) {
  mockChatState.messageIds = messages.map((m) => m.id);
  mockChatState.messagesById = Object.fromEntries(messages.map((m) => [m.id, m]));
}

describe("TranscriptPane revision flows (persisted session, issue #507)", () => {
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

    // removeMessagesFrom MUTATES the mock store so tests can assert the trim
    // actually happened (and WHEN it happened relative to the truncate call).
    mockChatState.removeMessagesFrom = vi.fn((index: number) => {
      mockChatState.messageIds = mockChatState.messageIds.slice(0, index);
      const next: Record<string, any> = {};
      for (const id of mockChatState.messageIds) next[id] = mockChatState.messagesById[id];
      mockChatState.messagesById = next;
    });
    // loadChat ADOPTS the mapped fork messages so tests can assert the store
    // content (kmsRefs/mode survive the mapper, UI-039).
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
  });

  it("retry truncates the persisted history at the LAST user message before trimming the store, then sends directly", async () => {
    mockChatState.activeChatId = "77";
    setMockMessages([
      { id: "m1", role: "user", content: "first question" },
      { id: "m2", role: "assistant", content: "first answer" },
      { id: "m3", role: "user", content: "second question" },
      { id: "m4", role: "assistant", content: "second answer" },
    ]);

    // Hold the truncate pending so the ordering is observable.
    let resolveTruncate!: (value: Awaited<ReturnType<typeof truncateChatSession>>) => void;
    const truncatePromise = new Promise<Awaited<ReturnType<typeof truncateChatSession>>>((resolve) => {
      resolveTruncate = resolve;
    });
    vi.mocked(truncateChatSession).mockReturnValue(truncatePromise);

    render(<TranscriptPane />);

    await userEvent.click(screen.getByLabelText("Retry m4"));

    // Server-side trim FIRST, with keepCount = index of the last user message.
    expect(truncateChatSession).toHaveBeenCalledTimes(1);
    expect(truncateChatSession).toHaveBeenCalledWith(77, 2);

    // The local transcript is untouched while the truncate is in flight.
    expect(mockChatState.messageIds).toHaveLength(4);

    resolveTruncate({ remaining_count: 2, tail_seq: 2 });
    await waitFor(() => expect(mockSendDirect).toHaveBeenCalledTimes(1));

    // After the truncate resolves: local store trimmed to the retained history...
    expect(mockChatState.messageIds).toEqual(["m1", "m2"]);
    // ...and the trimmed exchange is re-sent through the direct-send path.
    const [content, history] = mockSendDirect.mock.calls[0] as [
      string,
      Array<{ id: string; content: string }>,
    ];
    expect(content).toBe("second question");
    expect(history).toEqual([
      expect.objectContaining({ id: "m1", content: "first question" }),
      expect.objectContaining({ id: "m2", content: "first answer" }),
    ]);
  });

  it("edit truncates the persisted history at the edited message's own index before trimming the store", async () => {
    mockChatState.activeChatId = "77";
    const mockSetInput = vi.fn();
    mockChatState.setInput = mockSetInput;
    setMockMessages([
      { id: "m1", role: "user", content: "first question" },
      { id: "m2", role: "assistant", content: "first answer" },
    ]);

    let resolveTruncate!: (value: Awaited<ReturnType<typeof truncateChatSession>>) => void;
    const truncatePromise = new Promise<Awaited<ReturnType<typeof truncateChatSession>>>((resolve) => {
      resolveTruncate = resolve;
    });
    vi.mocked(truncateChatSession).mockReturnValue(truncatePromise);

    render(<TranscriptPane />);

    await userEvent.click(screen.getByLabelText("Edit m1"));

    // keepCount is the edited message's own index (0), not the last user index.
    expect(truncateChatSession).toHaveBeenCalledTimes(1);
    expect(truncateChatSession).toHaveBeenCalledWith(77, 0);

    // Store untouched until the truncate resolves.
    expect(mockChatState.messageIds).toEqual(["m1", "m2"]);

    resolveTruncate({ remaining_count: 0, tail_seq: null });
    await waitFor(() => expect(mockSetInput).toHaveBeenCalledWith("first question"));

    expect(mockChatState.messageIds).toEqual([]);
    // Edit restores content to the composer — it does NOT re-send.
    expect(mockSendDirect).not.toHaveBeenCalled();
  });

  it("keeps the local transcript intact and toasts when truncateChatSession rejects", async () => {
    mockChatState.activeChatId = "77";
    const mockSetInput = vi.fn();
    mockChatState.setInput = mockSetInput;
    setMockMessages([
      { id: "m1", role: "user", content: "first question" },
      { id: "m2", role: "assistant", content: "first answer" },
      { id: "m3", role: "user", content: "second question" },
      { id: "m4", role: "assistant", content: "second answer" },
    ]);

    vi.mocked(truncateChatSession).mockRejectedValue(new Error("network down"));

    render(<TranscriptPane />);

    await userEvent.click(screen.getByLabelText("Retry m4"));

    await waitFor(() => {
      expect(toast.error).toHaveBeenCalledWith("Couldn't update conversation history");
    });
    // Nothing local was trimmed and nothing was re-sent.
    expect(mockChatState.messageIds).toHaveLength(4);
    expect(mockSendDirect).not.toHaveBeenCalled();
    expect(mockChatState.removeMessagesFrom).not.toHaveBeenCalled();
  });

  it("fork maps the response through mapSessionMessage so kmsRefs and mode reach the store (UI-039)", async () => {
    mockChatState.activeChatId = "77";
    setMockMessages([
      { id: "m1", role: "user", content: "Question" },
      { id: "m2", role: "assistant", content: "Answer" },
    ]);

    vi.mocked(forkChatSession).mockResolvedValue({
      id: 20,
      vault_id: 1,
      title: "Branch of Original",
      created_at: "2026-05-12T00:00:00Z",
      updated_at: "2026-05-12T00:00:00Z",
      forked_from_session_id: 77,
      fork_message_index: 1,
      messages: [
        {
          id: 101,
          role: "user",
          content: "Question",
          sources: null,
          created_at: "2026-05-12T00:00:00Z",
        },
        {
          id: 102,
          role: "assistant",
          content: "Answer",
          sources: null,
          kms_refs: [{ kms_label: "K1", title: "Runbook" }],
          mode: "thinking",
          status: "interrupted",
          created_at: "2026-05-12T00:00:00Z",
        },
      ],
    });

    render(<TranscriptPane />);

    await userEvent.click(screen.getByLabelText("Fork m2"));

    await waitFor(() => expect(mockChatState.loadChat).toHaveBeenCalledTimes(1));
    expect(mockChatState.loadChat).toHaveBeenCalledWith(
      "20",
      expect.arrayContaining([
        expect.objectContaining({ id: "101", role: "user" }),
        expect.objectContaining({ id: "102" }),
      ])
    );

    // The store adopted the MAPPED messages — snake_case api fields must not
    // survive, and the camelCase fields the UI renders must be present.
    const forkedAssistant = mockChatState.messagesById["102"];
    expect(forkedAssistant.kmsRefs).toEqual([{ kms_label: "K1", title: "Runbook" }]);
    expect(forkedAssistant.mode).toBe("thinking");
    expect(forkedAssistant.status).toBe("interrupted");
    expect(mockRefreshHistory).toHaveBeenCalledTimes(1);
    expect(mockNavigate).toHaveBeenCalledWith("/chat/20");
  });
});

describe("TranscriptPane persisted terminal status banner (issue #507)", () => {
  // The banner from FIX 1 — rendered by TranscriptPane's MessageRow for
  // restored assistant rows whose persisted status is interrupted/partial.
  beforeEach(() => {
    vi.clearAllMocks();
    mockChatState.messageIds = [];
    mockChatState.messagesById = {};
    mockChatState.input = "";
    mockChatState.isStreaming = false;
    mockChatState.streamingMessageId = null;
    mockChatState.inputError = null;
    mockChatState.activeChatId = null;
    mockChatState.removeMessagesFrom = vi.fn();
    mockChatState.loadChat = vi.fn();
    (useChatStore as unknown as { getState: () => typeof mockChatState }).getState = () => mockChatState;
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

  it("renders the interrupted banner with a Retry button on the last message", () => {
    setMockMessages([
      { id: "m1", role: "user", content: "question" },
      { id: "m2", role: "assistant", content: "half an answer", status: "interrupted" },
    ]);

    render(<TranscriptPane />);

    expect(screen.getByText("Response interrupted — you can retry.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
  });

  it("renders the partial banner text and omits Retry when the message is not the last one", () => {
    setMockMessages([
      { id: "m1", role: "user", content: "question" },
      { id: "m2", role: "assistant", content: "half an answer", status: "partial" },
      { id: "m3", role: "user", content: "follow-up" },
    ]);

    render(<TranscriptPane />);

    expect(screen.getByText("Response is incomplete — you can retry.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();
  });

  it("does not render the banner while a stream is active", () => {
    mockChatState.isStreaming = true;
    setMockMessages([
      { id: "m1", role: "user", content: "question" },
      { id: "m2", role: "assistant", content: "half an answer", status: "interrupted" },
    ]);

    render(<TranscriptPane />);

    expect(screen.queryByText(/you can retry/)).not.toBeInTheDocument();
  });
});
