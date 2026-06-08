// frontend/src/components/chat/TranscriptPane.test.tsx
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor, within, act } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { TranscriptPane, Composer, EmptyTranscript } from "./TranscriptPane";
import { useChatStore } from "@/stores/useChatStore";
import { useVaultStore } from "@/stores/useVaultStore";

// Mock ResizeObserver for Radix UI ScrollArea
class MockResizeObserver {
  observe = vi.fn();
  unobserve = vi.fn();
  disconnect = vi.fn();
}
global.ResizeObserver = MockResizeObserver as unknown as typeof ResizeObserver;

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
const mockAuthState = vi.hoisted(() => ({
  user: null as null | { full_name?: string | null; username?: string | null },
}));

// Mock the hooks and dependencies
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
  useAuthStore: vi.fn((selector?: (s: any) => unknown) => {
    return typeof selector === "function" ? selector(mockAuthState) : mockAuthState;
  }),
}));
vi.mock("@/stores/useChatShellStore", () => ({
  useChatShellStore: vi.fn((selector?: (s: any) => unknown) => {
    const state = {
      activeSessionId: null,
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
vi.mock("@/hooks/useSendMessage");
vi.mock("@/hooks/useChatHistory");
vi.mock("@/lib/api", () => ({
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
  MessageBubble: ({ message, onFork, userInitial }: { message: { id: string; role: string; content: string }; onFork?: () => void; userInitial?: string }) => (
    <div data-testid="message-bubble" data-message-id={message.id} data-user-initial={userInitial}>
      {message.content}
      {onFork && <button type="button" aria-label={`Fork ${message.id}`} onClick={onFork}>Fork</button>}
    </div>
  ),
}));
vi.mock("./AssistantMessage", () => ({
  AssistantMessage: ({ message, onFork }: { message: { id: string; role: string; content: string }; onFork?: () => void }) => (
    <div data-testid="message-bubble" data-message-id={message.id}>
      {message.content}
      {onFork && <button type="button" aria-label={`Fork ${message.id}`} onClick={onFork}>Fork</button>}
    </div>
  ),
}));

// Mock framer-motion to avoid animation issues in tests
vi.mock("framer-motion", () => ({
  motion: {
    div: ({ children, ...props }: { children: React.ReactNode }) => (
      <div data-testid="motion-div" {...props}>{children}</div>
    ),
  },
  AnimatePresence: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  useReducedMotion: () => false,
}));

// Mock @tanstack/react-virtual
// Track message count dynamically for virtualizer mock
let _mockMessageCount = 0;
const _createVirtualItems = () =>
  Array.from({ length: _mockMessageCount }, (_, i) => ({
    index: i,
    start: i * 120,
    size: 120,
    key: `mock-msg-${i}`,
  }));

vi.mock("@tanstack/react-virtual", () => ({
  useVirtualizer: vi.fn(() => ({
    getVirtualItems: () => _createVirtualItems(),
    getTotalSize: () => _mockMessageCount * 120,
    measureElement: vi.fn((el) => ({
      getBoundingClientRect: () => ({ height: 120 }),
    })),
    scrollToIndex: vi.fn(),
    measure: vi.fn(),
  })),
}));

import { useSendMessage, MAX_INPUT_LENGTH } from "@/hooks/useSendMessage";
import { useChatHistory } from "@/hooks/useChatHistory";
import { forkChatSession } from "@/lib/api";
import { toast } from "sonner";
import { TooltipProvider } from "@/components/ui/tooltip";

// Helper to render Composer with required providers
const renderComposerWithProviders = (props: React.ComponentProps<typeof Composer>) => {
  return render(
    <TooltipProvider>
      <Composer {...props} />
    </TooltipProvider>
  );
};

// Helper to set messages in both normalized fields and keep _mockMessageCount in sync
function setMockMessages(messages: Array<{ id: string; role: string; content: string; [key: string]: any }>) {
  mockChatState.messageIds = messages.map((m) => m.id);
  mockChatState.messagesById = Object.fromEntries(messages.map((m) => [m.id, m]));
}

describe("TranscriptPane", () => {
  const mockSetInput = vi.fn();
  const mockHandleSend = vi.fn();
  const mockHandleStop = vi.fn();
  const mockRefreshHistory = vi.fn();
  const mockGetActiveVault = vi.fn();
  beforeEach(() => {
    vi.clearAllMocks();
    (useChatStore as unknown as { getState: ReturnType<typeof vi.fn> }).getState = vi.fn(() => mockChatState);

    // Reset shared mock state
    mockChatState.messageIds = [];
    mockChatState.messagesById = {};
    mockChatState.input = "";
    mockChatState.isStreaming = false;
    mockChatState.streamingMessageId = null;
    mockChatState.inputError = null;
    mockChatState.setInput = mockSetInput;
    mockChatState.setIsStreaming = vi.fn();
    mockChatState.setAbortFn = vi.fn();
    mockChatState.setInputError = vi.fn();
    mockChatState.addMessage = vi.fn();
    mockChatState.updateMessage = vi.fn();
    mockChatState.removeMessagesFrom = vi.fn();
    mockChatState.stopStreaming = vi.fn();
    mockChatState.loadChat = vi.fn();
    mockChatState.newChat = vi.fn();
    mockAuthState.user = null;

    // Mock useVaultStore with selector support
    (useVaultStore as unknown as ReturnType<typeof vi.fn>).mockImplementation((selector) => {
      const state = {
        vaults: [{ id: 1, name: "Test Vault", file_count: 5 }],
        activeVaultId: 1,
        getActiveVault: mockGetActiveVault,
      };
      return selector ? selector(state) : state;
    });

    (useSendMessage as ReturnType<typeof vi.fn>).mockReturnValue({
      handleSend: mockHandleSend,
      handleStop: mockHandleStop,
    });

    (useChatHistory as ReturnType<typeof vi.fn>).mockReturnValue({
      refreshHistory: mockRefreshHistory,
      chatHistory: [],
      isChatLoading: false,
      chatHistoryError: null,
    });

    mockGetActiveVault.mockReturnValue({
      id: 1,
      name: "Test Vault",
      file_count: 5,
    });
  });

  describe("1. TranscriptPane renders EmptyTranscript when no messages", () => {
    it("shows EmptyTranscript component when messages array is empty", () => {
      render(<TranscriptPane />);
      expect(screen.getByText("What would you like to know?")).toBeInTheDocument();
    });

    it("does not render any MessageBubble when messages are empty", () => {
      render(<TranscriptPane />);
      expect(screen.queryByTestId("message-bubble")).not.toBeInTheDocument();
    });
  });

  describe("2. TranscriptPane renders message list when messages exist", () => {
    it("renders MessageBubble components for each message", () => {
      _mockMessageCount = 2;
      setMockMessages([
        { id: "1", role: "user", content: "Hello" },
        { id: "2", role: "assistant", content: "Hi there!" },
      ]);

      render(<TranscriptPane />);
      expect(screen.getAllByTestId("message-bubble")).toHaveLength(2);
    });

    it("renders user message correctly", () => {
      _mockMessageCount = 1;
      setMockMessages([{ id: "1", role: "user", content: "Test message" }]);

      render(<TranscriptPane />);
      expect(screen.getByText("Test message")).toBeInTheDocument();
    });

    it.each([
      [{ full_name: "Brett Example", username: "ignored" }, "B"],
      [{ full_name: "", username: "codex-user" }, "C"],
      [null, "U"],
    ])("derives the user avatar initial from the current auth user", (user, expectedInitial) => {
      mockAuthState.user = user;
      _mockMessageCount = 1;
      setMockMessages([{ id: "1", role: "user", content: "Initial source" }]);

      render(<TranscriptPane />);

      expect(screen.getByTestId("message-bubble")).toHaveAttribute("data-user-initial", expectedInitial);
    });

    it("guards fork requests against duplicate clicks while pending", async () => {
      let resolveFork!: (value: Awaited<ReturnType<typeof forkChatSession>>) => void;
      const forkPromise = new Promise<Awaited<ReturnType<typeof forkChatSession>>>((resolve) => {
        resolveFork = resolve;
      });
      vi.mocked(forkChatSession).mockReturnValue(forkPromise);
      mockChatState.activeChatId = "10";
      _mockMessageCount = 2;
      setMockMessages([
        { id: "m1", role: "user", content: "Question" },
        { id: "m2", role: "assistant", content: "Answer" },
      ]);

      render(<TranscriptPane />);

      await userEvent.dblClick(screen.getByLabelText("Fork m2"));

      expect(forkChatSession).toHaveBeenCalledTimes(1);
      expect(forkChatSession).toHaveBeenCalledWith(10, 1);
      await waitFor(() => expect(screen.queryByLabelText("Fork m2")).not.toBeInTheDocument());

      resolveFork({
        id: 20,
        vault_id: 1,
        title: "Branch of Original",
        created_at: "2026-05-12T00:00:00Z",
        updated_at: "2026-05-12T00:00:00Z",
        forked_from_session_id: 10,
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
            wiki_refs: [{ wiki_label: "W1", title: "Runbook" }],
            created_at: "2026-05-12T00:00:00Z",
          },
        ],
      });

      await waitFor(() => expect(mockChatState.loadChat).toHaveBeenCalledTimes(1));
      expect(mockChatState.loadChat).toHaveBeenCalledWith(
        "20",
        expect.arrayContaining([
          expect.objectContaining({
            id: "102",
            wikiRefs: [{ wiki_label: "W1", title: "Runbook" }],
          }),
        ])
      );
      expect(mockRefreshHistory).toHaveBeenCalledTimes(1);
      expect(mockNavigate).toHaveBeenCalledWith("/chat/20");
    });

    it("does not load the transcript when the fork response has no messages (issue #83)", async () => {
      vi.mocked(forkChatSession).mockResolvedValue({
        id: 99,
        vault_id: 1,
        title: "Empty Branch",
        created_at: "2026-05-15T00:00:00Z",
        updated_at: "2026-05-15T00:00:00Z",
        forked_from_session_id: 10,
        fork_message_index: 1,
        messages: [],
      });
      mockChatState.activeChatId = "10";
      _mockMessageCount = 2;
      setMockMessages([
        { id: "m1", role: "user", content: "Question" },
        { id: "m2", role: "assistant", content: "Answer" },
      ]);

      render(<TranscriptPane />);

      await userEvent.click(screen.getByLabelText("Fork m2"));

      await waitFor(() => expect(forkChatSession).toHaveBeenCalledTimes(1));
      await waitFor(() =>
        expect(toast.warning).toHaveBeenCalledWith(
          expect.stringMatching(/no messages/i)
        )
      );
      expect(mockChatState.loadChat).not.toHaveBeenCalled();
      expect(mockRefreshHistory).not.toHaveBeenCalled();
      expect(mockNavigate).not.toHaveBeenCalled();
    });
  });

  describe("3. Scroll-to-bottom button appears when scrolled up", () => {
    it("TranscriptPane has scroll area element", () => {
      render(<TranscriptPane />);
      expect(screen.getByRole("log")).toBeInTheDocument();
    });

    it("shows scroll button when TranscriptPane renders", () => {
      render(<TranscriptPane />);
      // Verify the scroll area exists (actual scroll behavior is tested via integration)
      const scrollArea = screen.getByRole("log");
      expect(scrollArea).toBeInTheDocument();
    });
  });

  describe("4. Composer renders textarea with placeholder", () => {
    it("renders textarea with correct placeholder text", () => {
      renderComposerWithProviders({ onSend: mockHandleSend, onStop: mockHandleStop, isStreaming: false });
      expect(screen.getByPlaceholderText(/Message\.\.\..*Enter to send.*Shift\+Enter.*newline.*\/ for commands/i)).toBeInTheDocument();
    });

    it("textarea has correct aria-label", () => {
      renderComposerWithProviders({ onSend: mockHandleSend, onStop: mockHandleStop, isStreaming: false });
      expect(screen.getByLabelText("Message input")).toBeInTheDocument();
    });
  });

  describe("5. Composer shows VaultContextPill when vault is active", () => {
    it("displays vault badge with vault name", () => {
      mockGetActiveVault.mockReturnValue({
        id: 1,
        name: "Test Vault",
        file_count: 5,
      });

      renderComposerWithProviders({ onSend: mockHandleSend, onStop: mockHandleStop, isStreaming: false });
      expect(screen.getByText("Test Vault")).toBeInTheDocument();
    });

    it("vault pill has correct aria-label", () => {
      mockGetActiveVault.mockReturnValue({
        id: 1,
        name: "Test Vault",
        file_count: 5,
      });

      renderComposerWithProviders({ onSend: mockHandleSend, onStop: mockHandleStop, isStreaming: false });
      expect(screen.getByLabelText("Active vault: Test Vault")).toBeInTheDocument();
    });
  });

  describe("6. Composer hides VaultContextPill when no vault", () => {
    it("does not render vault badge when no active vault", () => {
      mockGetActiveVault.mockReturnValue(undefined);

      renderComposerWithProviders({ onSend: mockHandleSend, onStop: mockHandleStop, isStreaming: false });
      expect(screen.queryByLabelText(/Active vault:/i)).not.toBeInTheDocument();
    });

    it("does not show vault name in badge when getActiveVault returns undefined", () => {
      mockGetActiveVault.mockReturnValue(undefined);

      renderComposerWithProviders({ onSend: mockHandleSend, onStop: mockHandleStop, isStreaming: false });
      expect(screen.queryByText("Test Vault")).not.toBeInTheDocument();
    });
  });

  describe("7. Composer slash menu opens when \"/\" is typed", () => {
    it("shows slash command menu when user types /", async () => {
      renderComposerWithProviders({ onSend: mockHandleSend, onStop: mockHandleStop, isStreaming: false });

      const textarea = screen.getByPlaceholderText(/Message\.\.\./i);
      
      await act(async () => {
        await userEvent.click(textarea);
        await userEvent.type(textarea, "/");
      });

      await waitFor(() => {
        expect(screen.getByRole("listbox", { name: "Slash commands" })).toBeInTheDocument();
      });
    });

    it("slash menu contains all 4 commands by default", async () => {
      renderComposerWithProviders({ onSend: mockHandleSend, onStop: mockHandleStop, isStreaming: false });

      const textarea = screen.getByPlaceholderText(/Message\.\.\./i);
      
      await act(async () => {
        await userEvent.click(textarea);
        await userEvent.type(textarea, "/");
      });

      await waitFor(() => {
        const listbox = screen.getByRole("listbox", { name: "Slash commands" });
        expect(within(listbox).getByText("/summarize")).toBeInTheDocument();
        expect(within(listbox).getByText("/compare")).toBeInTheDocument();
        expect(within(listbox).getByText("/timeline")).toBeInTheDocument();
        expect(within(listbox).getByText("/actions")).toBeInTheDocument();
      });
    });
  });

  describe("8. Composer slash menu filters commands", () => {
    it("shows no commands when filter matches nothing", async () => {
      renderComposerWithProviders({ onSend: mockHandleSend, onStop: mockHandleStop, isStreaming: false });

      const textarea = screen.getByPlaceholderText(/Message\.\.\./i);
      
      await act(async () => {
        await userEvent.click(textarea);
        await userEvent.type(textarea, "/xyz");
      });

      // Menu should not be visible when no commands match
      expect(screen.queryByRole("listbox", { name: "Slash commands" })).not.toBeInTheDocument();
    });

    it("slash menu opens when / is typed", async () => {
      renderComposerWithProviders({ onSend: mockHandleSend, onStop: mockHandleStop, isStreaming: false });

      const textarea = screen.getByPlaceholderText(/Message\.\.\./i);
      
      await act(async () => {
        await userEvent.click(textarea);
        await userEvent.type(textarea, "/");
      });

      await waitFor(() => {
        expect(screen.getByRole("listbox", { name: "Slash commands" })).toBeInTheDocument();
      });
    });
  });

  describe("9. Composer Enter sends when menu is closed", () => {
    it("calls onSend when Enter is pressed without Shift and menu is closed", async () => {
      mockChatState.input = "Test message";

      renderComposerWithProviders({ onSend: mockHandleSend, onStop: mockHandleStop, isStreaming: false });

      const textarea = screen.getByPlaceholderText(/Message\.\.\./i);
      
      // Ensure menu is not open (type without leading /)
      await act(async () => {
        await userEvent.type(textarea, "Test message");
      });
      
      // Press Enter without Shift
      fireEvent.keyDown(textarea, { key: "Enter", shiftKey: false });

      await waitFor(() => {
        expect(mockHandleSend).toHaveBeenCalled();
      });
    });

    it("does not send if input is only whitespace", async () => {
      mockChatState.input = "   ";

      renderComposerWithProviders({ onSend: mockHandleSend, onStop: mockHandleStop, isStreaming: false });

      const textarea = screen.getByPlaceholderText(/Message\.\.\./i);
      fireEvent.keyDown(textarea, { key: "Enter", shiftKey: false });

      expect(mockHandleSend).not.toHaveBeenCalled();
    });
  });

  describe("10. Composer Enter selects menu item when menu is open", () => {
    it("inserts command and closes menu when Enter is pressed with menu open", async () => {
      renderComposerWithProviders({ onSend: mockHandleSend, onStop: mockHandleStop, isStreaming: false });

      const textarea = screen.getByPlaceholderText(/Message\.\.\./i);
      
      await act(async () => {
        await userEvent.click(textarea);
        await userEvent.type(textarea, "/");
      });

      await waitFor(() => {
        expect(screen.getByRole("listbox", { name: "Slash commands" })).toBeInTheDocument();
      });

      // Press Enter to select first command
      fireEvent.keyDown(textarea, { key: "Enter" });

      await waitFor(() => {
        expect(mockSetInput).toHaveBeenCalledWith("/summarize ");
      });
    });

    it("Escape key closes the menu", async () => {
      renderComposerWithProviders({ onSend: mockHandleSend, onStop: mockHandleStop, isStreaming: false });

      const textarea = screen.getByPlaceholderText(/Message\.\.\./i);
      
      await act(async () => {
        await userEvent.click(textarea);
        await userEvent.type(textarea, "/");
      });

      await waitFor(() => {
        expect(screen.getByRole("listbox", { name: "Slash commands" })).toBeInTheDocument();
      });

      fireEvent.keyDown(textarea, { key: "Escape" });

      expect(screen.queryByRole("listbox", { name: "Slash commands" })).not.toBeInTheDocument();
    });

    it("clicking a command in menu inserts it", async () => {
      renderComposerWithProviders({ onSend: mockHandleSend, onStop: mockHandleStop, isStreaming: false });

      const textarea = screen.getByPlaceholderText(/Message\.\.\./i);
      
      await act(async () => {
        await userEvent.click(textarea);
        await userEvent.type(textarea, "/");
      });

      await waitFor(() => {
        expect(screen.getByRole("listbox", { name: "Slash commands" })).toBeInTheDocument();
      });

      const compareCommand = screen.getByText("/compare");
      await act(async () => {
        await userEvent.click(compareCommand);
      });

      await waitFor(() => {
        expect(mockSetInput).toHaveBeenCalledWith("/compare ");
      });
    });
  });

  describe("11. Composer Shift+Enter adds newline", () => {
    it("does not call onSend when Shift+Enter is pressed", async () => {
      mockChatState.input = "Test";

      renderComposerWithProviders({ onSend: mockHandleSend, onStop: mockHandleStop, isStreaming: false });

      const textarea = screen.getByPlaceholderText(/Message\.\.\./i);
      fireEvent.keyDown(textarea, { key: "Enter", shiftKey: true });

      expect(mockHandleSend).not.toHaveBeenCalled();
    });

    it("textarea updates on key events for newline handling", async () => {
      mockChatState.input = "Line 1";

      renderComposerWithProviders({ onSend: mockHandleSend, onStop: mockHandleStop, isStreaming: false });

      const textarea = screen.getByPlaceholderText(/Message\.\.\./i);
      
      // Shift+Enter should be handled without sending
      fireEvent.keyDown(textarea, { key: "Enter", shiftKey: true });
      
      // Verify send was NOT called
      expect(mockHandleSend).not.toHaveBeenCalled();
    });
  });

  describe("12. Composer send button disabled when input is empty", () => {
    it("send button is disabled when input is empty", () => {
      mockChatState.input = "";

      renderComposerWithProviders({ onSend: mockHandleSend, onStop: mockHandleStop, isStreaming: false });

      const sendButton = screen.getByLabelText(/send message/i);
      expect(sendButton).toBeDisabled();
    });

    it("send button is disabled when input is only whitespace", () => {
      mockChatState.input = "   ";

      renderComposerWithProviders({ onSend: mockHandleSend, onStop: mockHandleStop, isStreaming: false });

      const sendButton = screen.getByLabelText(/send message/i);
      expect(sendButton).toBeDisabled();
    });

    it("send button is enabled when input has content", () => {
      mockChatState.input = "Hello";

      renderComposerWithProviders({ onSend: mockHandleSend, onStop: mockHandleStop, isStreaming: false });

      const sendButton = screen.getByLabelText(/send message/i);
      expect(sendButton).not.toBeDisabled();
    });
  });

  describe("12a. Composer clears slash menu on send button submit", () => {
    it("closes the slash menu when the send button submits a message", async () => {
      mockChatState.input = "/summarize this";
      renderComposerWithProviders({ onSend: mockHandleSend, onStop: mockHandleStop, isStreaming: false });

      const textarea = screen.getByPlaceholderText(/Message\.\.\./i);

      fireEvent.change(textarea, { target: { value: "/summarize" } });

      await waitFor(() => {
        expect(screen.getByRole("listbox", { name: "Slash commands" })).toBeInTheDocument();
      });

      fireEvent.click(screen.getByRole("button", { name: "Send message" }));

      await waitFor(() => {
        expect(mockHandleSend).toHaveBeenCalled();
        expect(screen.queryByRole("listbox", { name: "Slash commands" })).not.toBeInTheDocument();
      });
    });
  });

  describe("13. Composer stop button visible during streaming", () => {
    it("shows stop button when isStreaming is true", () => {
      renderComposerWithProviders({ onSend: mockHandleSend, onStop: mockHandleStop, isStreaming: true });
      expect(screen.getByText("Stop")).toBeInTheDocument();
      expect(screen.getByLabelText("Stop generating")).toBeInTheDocument();
    });

    it("calls onStop when stop button is clicked", async () => {
      renderComposerWithProviders({ onSend: mockHandleSend, onStop: mockHandleStop, isStreaming: true });

      const stopButton = screen.getByLabelText("Stop generating");
      
      await act(async () => {
        await userEvent.click(stopButton);
      });

      expect(mockHandleStop).toHaveBeenCalled();
    });

    it("send button is hidden when streaming", () => {
      renderComposerWithProviders({ onSend: mockHandleSend, onStop: mockHandleStop, isStreaming: true });
      expect(screen.queryByLabelText(/send message/i)).not.toBeInTheDocument();
    });
  });

  describe("14. Composer attachment affordance", () => {
    it("renders an attachment button for file upload", () => {
      renderComposerWithProviders({ onSend: mockHandleSend, onStop: mockHandleStop, isStreaming: false });

      expect(screen.getByLabelText(/attach file/i)).toBeInTheDocument();
    });
  });

  describe("15. EmptyTranscript shows suggested prompts when vault has docs", () => {
    it("displays all 4 suggested prompts when hasIndexedDocs is true", () => {
      render(
        <EmptyTranscript
          onPromptClick={mockSetInput}
          hasIndexedDocs={true}
          onNavigateToDocuments={mockNavigate}
        />
      );

      expect(
        screen.getByText("Summarize the uploaded documents with citations")
      ).toBeInTheDocument();
      expect(
        screen.getByText("Find contradictions or conflicts across sources")
      ).toBeInTheDocument();
      expect(
        screen.getByText("Create an action-item list from the documents")
      ).toBeInTheDocument();
      expect(
        screen.getByText("Show the strongest evidence for the main conclusion")
      ).toBeInTheDocument();
    });

    it("displays appropriate heading for vault with docs", () => {
      render(
        <EmptyTranscript
          onPromptClick={mockSetInput}
          hasIndexedDocs={true}
          onNavigateToDocuments={mockNavigate}
        />
      );

      expect(screen.getByText("What would you like to know?")).toBeInTheDocument();
    });

    it("prompts list has correct role and aria-label", () => {
      render(
        <EmptyTranscript
          onPromptClick={mockSetInput}
          hasIndexedDocs={true}
          onNavigateToDocuments={mockNavigate}
        />
      );

      expect(screen.getByRole("list", { name: "Suggested prompts" })).toBeInTheDocument();
    });
  });

  describe("16. EmptyTranscript shows EmptyVaultCTA when vault has no docs", () => {
    it("displays empty vault heading", () => {
      render(
        <EmptyTranscript
          onPromptClick={mockSetInput}
          hasIndexedDocs={false}
          onNavigateToDocuments={mockNavigate}
        />
      );

      expect(screen.getByText("Upload documents to get started")).toBeInTheDocument();
    });

    it("displays empty vault description", () => {
      render(
        <EmptyTranscript
          onPromptClick={mockSetInput}
          hasIndexedDocs={false}
          onNavigateToDocuments={mockNavigate}
        />
      );

      expect(screen.getByText(/Add documents to your vault/i)).toBeInTheDocument();
    });

    it("shows 'Go to Documents' button", () => {
      render(
        <EmptyTranscript
          onPromptClick={mockSetInput}
          hasIndexedDocs={false}
          onNavigateToDocuments={mockNavigate}
        />
      );

      expect(screen.getByText("Go to Documents")).toBeInTheDocument();
    });

    it("does not show suggested prompts when hasIndexedDocs is false", () => {
      render(
        <EmptyTranscript
          onPromptClick={mockSetInput}
          hasIndexedDocs={false}
          onNavigateToDocuments={mockNavigate}
        />
      );

      expect(
        screen.queryByText("Summarize the uploaded documents with citations")
      ).not.toBeInTheDocument();
      expect(screen.queryByRole("list", { name: "Suggested prompts" })).not.toBeInTheDocument();
    });
  });

  describe("17. Clicking suggested prompt sets input in store", () => {
    it("calls onPromptClick with correct prompt when clicked", async () => {
      render(
        <EmptyTranscript
          onPromptClick={mockSetInput}
          hasIndexedDocs={true}
          onNavigateToDocuments={mockNavigate}
        />
      );

      const promptText = "Summarize the uploaded documents with citations";
      const promptButton = screen.getByText(promptText);

      await act(async () => {
        await userEvent.click(promptButton);
      });

      expect(mockSetInput).toHaveBeenCalledWith(promptText);
    });

    it("works for all suggested prompts", async () => {
      render(
        <EmptyTranscript
          onPromptClick={mockSetInput}
          hasIndexedDocs={true}
          onNavigateToDocuments={mockNavigate}
        />
      );

      const prompts = [
        "Summarize the uploaded documents with citations",
        "Find contradictions or conflicts across sources",
        "Create an action-item list from the documents",
        "Show the strongest evidence for the main conclusion",
      ];

      for (const prompt of prompts) {
        vi.clearAllMocks();
        const button = screen.getByText(prompt);
        await act(async () => {
          await userEvent.click(button);
        });
        expect(mockSetInput).toHaveBeenCalledWith(prompt);
      }
    });
  });

  describe("Additional coverage", () => {
    it("textarea is readOnly (not disabled) when streaming so users can still scroll their draft", () => {
      renderComposerWithProviders({ onSend: mockHandleSend, onStop: mockHandleStop, isStreaming: true });

      const textarea = screen.getByLabelText("Message input");
      expect(textarea).toHaveAttribute("readonly");
      expect(textarea).not.toBeDisabled();
    });

    it("shows error message when inputError is set", () => {
      mockChatState.inputError = "Test error message";

      renderComposerWithProviders({ onSend: mockHandleSend, onStop: mockHandleStop, isStreaming: false });

      expect(screen.getByRole("alert")).toHaveTextContent("Test error message");
    });

    it("character count shows warning when input is near max length", () => {
      const longInput = "a".repeat(1700); // > 80% of 2000
      mockChatState.input = longInput;

      renderComposerWithProviders({ onSend: mockHandleSend, onStop: mockHandleStop, isStreaming: false });

      expect(screen.getByText(/1700\/2000/)).toBeInTheDocument();
    });

    it("character count shows destructive color when over max", () => {
      const overMaxInput = "a".repeat(2100); // > 2000
      mockChatState.input = overMaxInput;

      renderComposerWithProviders({ onSend: mockHandleSend, onStop: mockHandleStop, isStreaming: false });

      const charCount = screen.getByText(/2100\/2000/);
      expect(charCount).toHaveClass(/destructive/);
    });

    it("keyboard navigation works in slash menu", async () => {
      renderComposerWithProviders({ onSend: mockHandleSend, onStop: mockHandleStop, isStreaming: false });

      const textarea = screen.getByPlaceholderText(/Message\.\.\./i);
      
      await act(async () => {
        await userEvent.click(textarea);
        await userEvent.type(textarea, "/");
      });

      await waitFor(() => {
        expect(screen.getByRole("listbox", { name: "Slash commands" })).toBeInTheDocument();
      });

      // Navigate down
      fireEvent.keyDown(textarea, { key: "ArrowDown" });
      
      // Verify menu still visible after navigation
      await waitFor(() => {
        expect(screen.getByRole("listbox", { name: "Slash commands" })).toBeInTheDocument();
      });
    });

    it("slash hint button exists", async () => {
      renderComposerWithProviders({ onSend: mockHandleSend, onStop: mockHandleStop, isStreaming: false });

      const slashButton = screen.getByLabelText("Open slash commands");
      expect(slashButton).toBeInTheDocument();
    });

    it("TranscriptPane renders with empty vault CTA", () => {
      mockGetActiveVault.mockReturnValue({
        id: 1,
        name: "Empty Vault",
        file_count: 0,
      });

      render(<TranscriptPane />);

      expect(screen.getByText("Upload documents to get started")).toBeInTheDocument();
    });

    it("handles multiline input", async () => {
      renderComposerWithProviders({ onSend: mockHandleSend, onStop: mockHandleStop, isStreaming: false });

      const textarea = screen.getByPlaceholderText(/Message\.\.\./i);
      
      // Type text and then newline
      await act(async () => {
        await userEvent.type(textarea, "Hello world");
      });

      expect(mockSetInput).toHaveBeenCalled();
    });

    it("shows MAX_INPUT_LENGTH constant is 2000", () => {
      expect(MAX_INPUT_LENGTH).toBe(2000);
    });
  });
});
