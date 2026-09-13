// frontend/src/components/chat/TranscriptPane.pending-status.test.tsx
// Issue #553: an assistant row the server pre-wrote but never finalized
// (status "pending" — crash mid-generation) must render the same retryable
// interrupted banner as "interrupted", never as a successful answer.

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render } from "@testing-library/react";
import { TranscriptPane } from "./TranscriptPane";
import { useVaultStore } from "@/stores/useVaultStore";

class MockResizeObserver {
  observe = vi.fn();
  unobserve = vi.fn();
  disconnect = vi.fn();
}
global.ResizeObserver = MockResizeObserver as unknown as typeof ResizeObserver;
Element.prototype.scrollIntoView = vi.fn();
Element.prototype.scrollTo = vi.fn();

const mockChatState = vi.hoisted(() => ({
  messageIds: ["m1", "m2"],
  messagesById: {
    m1: { id: "m1", role: "user", content: "question" },
    m2: { id: "m2", role: "assistant", content: "partial answer", status: "pending" },
  } as Record<string, any>,
  input: "",
  isStreaming: false,
  streamingMessageId: null as string | null,
  inputError: null as string | null,
  expandedSources: new Set(),
  activeChatId: "77",
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
    sendDirect: vi.fn(),
    currentStage: null,
  }),
  MAX_INPUT_LENGTH: 2000,
}));
vi.mock("@/hooks/useChatHistory", () => ({
  useChatHistory: () => ({
    refreshHistory: vi.fn(),
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
  useNavigate: () => vi.fn(),
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
  MessageBubble: ({ message }: { message: { id: string; content: string } }) => (
    <div data-testid="message-bubble" data-message-id={message.id}>
      {message.content}
    </div>
  ),
}));

describe("TranscriptPane pending assistant status (issue #553)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    (useVaultStore as unknown as ReturnType<typeof vi.fn>).mockImplementation(
      (selector: (s: unknown) => unknown) => {
        const state = {
          activeVaultId: 1,
          getActiveVault: () => ({ id: 1, name: "Test Vault", file_count: 5 }),
        };
        return selector ? selector(state) : state;
      },
    );
  });

  it("renders the retryable interrupted banner for a pending assistant row", () => {
    render(<TranscriptPane />);
    const banner = document.querySelector('[data-interrupted-status="pending"]');
    expect(banner).not.toBeNull();
    expect(banner?.textContent).toContain("Response interrupted");
  });

  it("does not render the banner while a stream is active or for user rows", () => {
    // Sanity pin on the predicate's guards: not streaming is required, and
    // the pending user row itself never shows the assistant banner.
    mockChatState.isStreaming = true;
    const { unmount } = render(<TranscriptPane />);
    expect(document.querySelector('[data-interrupted-status="pending"]')).toBeNull();
    mockChatState.isStreaming = false;
    unmount();

    render(<TranscriptPane />);
    // Only the assistant row carries the banner (m2), never the user row.
    const banners = document.querySelectorAll("[data-interrupted-status]");
    expect(banners).toHaveLength(1);
  });
});
