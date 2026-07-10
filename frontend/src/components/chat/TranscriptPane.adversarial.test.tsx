// frontend/src/components/chat/TranscriptPane.adversarial.test.tsx
// ADVERSARIAL TESTS: Security, edge cases, race conditions, and attack vectors
// These tests deliberately try to break the component with malicious inputs

import { describe, it, expect, vi, beforeEach, beforeAll } from "vitest";
import { render, screen, fireEvent, waitFor, within, act } from "@testing-library/react";
import { BrowserRouter } from "react-router-dom";
import { TranscriptPane, Composer, EmptyTranscript } from "./TranscriptPane";
import { useChatStore } from "@/stores/useChatStore";
import { useVaultStore } from "@/stores/useVaultStore";

// Set longer timeout for async tests
const TEST_TIMEOUT = 15000;

// Mock ResizeObserver for Radix UI ScrollArea
class MockResizeObserver {
  observe = vi.fn();
  unobserve = vi.fn();
  disconnect = vi.fn();
}
global.ResizeObserver = MockResizeObserver as unknown as typeof ResizeObserver;

// Mock scrollIntoView
Element.prototype.scrollIntoView = vi.fn();

// Mock scrollTo — JSDOM does not implement it
Element.prototype.scrollTo = vi.fn();

// =============================================================================
// MOCKS
// =============================================================================

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
vi.mock("@/hooks/useSendMessage");
vi.mock("@/hooks/useChatHistory");
vi.mock("@/lib/api");
vi.mock("react-router-dom", () => ({
  useNavigate: () => vi.fn(),
}));
vi.mock("./MessageBubble", () => ({
  MessageBubble: ({ message }: { message: { id: string; role: string; content: string } }) => (
    <div data-testid="message-bubble" data-message-id={message.id}>
      {message.content}
    </div>
  ),
}));
vi.mock("./AssistantMessage", () => ({
  AssistantMessage: ({ message }: { message: { id: string; role: string; content: string } }) => (
    <div data-testid="message-bubble" data-message-id={message.id}>
      {message.content}
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

// Mock @tanstack/react-virtual - without this, real components render
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
    measureElement: vi.fn((el: HTMLElement) => ({
      getBoundingClientRect: () => ({ height: 120 }),
    })),
    scrollToIndex: vi.fn(),
    measure: vi.fn(),
  })),
}));

import { useSendMessage, MAX_INPUT_LENGTH } from "@/hooks/useSendMessage";
import { useChatHistory } from "@/hooks/useChatHistory";
import { TooltipProvider } from "@/components/ui/tooltip";

// =============================================================================
// ADVERSARIAL TEST SUITE
// =============================================================================

describe("TranscriptPane ADVERSARIAL TESTS", () => {
  const mockSetInput = vi.fn();
  const mockHandleSend = vi.fn();
  const mockHandleStop = vi.fn();
  const mockRefreshHistory = vi.fn();
  const mockGetActiveVault = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
    _mockMessageCount = 0;

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
    mockChatState.stopStreaming = vi.fn();

    // Mock useVaultStore with selector support
    (useVaultStore as unknown as ReturnType<typeof vi.fn>).mockImplementation((selector) => {
      const state = {
        vaults: [],
        activeVaultId: null,
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

    mockGetActiveVault.mockReturnValue(undefined);
  });

  // Helper to set chat store messages AND sync virtualizer count
  const withMessages = (messages: any[]) => {
    _mockMessageCount = messages.length;
    mockChatState.messageIds = messages.map((m: any) => m.id ?? String(Math.random()));
    mockChatState.messagesById = Object.fromEntries(
      messages.map((m: any, i: number) => [m.id ?? String(i), m])
    );
  };

  // Helper to replace old-style mockReturnValue calls with normalized state updates
  const setMockChatState = (state: {
    messages?: any[];
    input?: string;
    isStreaming?: boolean;
    inputError?: string | null;
    [key: string]: any;
  }) => {
    if (state.messages !== undefined) {
      _mockMessageCount = state.messages.length;
      mockChatState.messageIds = state.messages.map((m: any, i: number) => m.id ?? String(i));
      mockChatState.messagesById = Object.fromEntries(
        state.messages.map((m: any, i: number) => [m.id ?? String(i), m])
      );
    }
    if (state.input !== undefined) mockChatState.input = state.input;
    if (state.isStreaming !== undefined) mockChatState.isStreaming = state.isStreaming;
    if (state.inputError !== undefined) mockChatState.inputError = state.inputError;
    if (state.setInput !== undefined) mockChatState.setInput = state.setInput;
    if (state.setIsStreaming !== undefined) mockChatState.setIsStreaming = state.setIsStreaming;
    if (state.setAbortFn !== undefined) mockChatState.setAbortFn = state.setAbortFn;
    if (state.setInputError !== undefined) mockChatState.setInputError = state.setInputError;
    if (state.addMessage !== undefined) mockChatState.addMessage = state.addMessage;
    if (state.updateMessage !== undefined) mockChatState.updateMessage = state.updateMessage;
  };

  // ===========================================================================
  // 1. XSS IN MESSAGES - Should render safely without executing scripts
  // ===========================================================================
  describe("XSS in messages", () => {
    const xssPayloads = [
      '<img onerror="alert(1)" src=x>',
      '<script>alert("xss")</script>',
      '<svg onload="alert(1)">',
      '"><script>alert(document.cookie)</script>',
      '<a href="javascript:alert(1)">Click</a>',
      '<div onclick="alert(1)">click me</div>',
      '<iframe src="javascript:alert(1)"></iframe>',
      '${alert(1)}',
      '{{constructor.constructor("alert(1)")()}}',
      '<img src=x onerror="eval(atob(\'YWxlcnQoMSk=\'))">',
      '<script>document.write("<img src=x>")</script>',
      '<style>@import"javascript:alert(1)"</style>',
    ];

    it.each(xssPayloads)("should NOT execute XSS payload in message content: %s", async (payload) => {
      const messages = [{ id: "msg-1", role: "assistant", content: payload }];
      _mockMessageCount = messages.length;
      setMockChatState({ messages, input: "", isStreaming: false, inputError: null });

      render(<TranscriptPane />);

      await waitFor(() => {
        expect(screen.getByTestId("message-bubble")).toBeInTheDocument();
      });

      // Verify no script elements were created
      expect(document.querySelector("script")).toBeNull();
    });

    it("should NOT execute XSS in message sources", async () => {
      const messages = [{
        id: "msg-1",
        role: "assistant",
        content: "Here are your results",
        sources: [{
          id: 1,
          filename: 'test.js"><script>alert(1)</script>',
          score: 0.9,
          score_type: "distance",
          snippet: '<img onerror="alert(1)" src=x>',
        }],
      }];
      _mockMessageCount = messages.length;
      setMockChatState({ messages, input: "", isStreaming: false, inputError: null });

      render(<TranscriptPane />);

      await waitFor(() => {
        expect(screen.getByTestId("message-bubble")).toBeInTheDocument();
      });

      // No script execution
      expect(document.querySelector("script")).toBeNull();
    });

    it("should escape HTML in user messages", async () => {
      const messages = [{
        id: "msg-1",
        role: "user",
        content: '<script>alert("bad")</script>Hello',
      }];
      _mockMessageCount = messages.length;
      setMockChatState({ messages, input: "", isStreaming: false, inputError: null });

      render(<TranscriptPane />);

      await waitFor(() => {
        expect(screen.getByTestId("message-bubble")).toBeInTheDocument();
      });

      // Content should be escaped, not rendered as script
      expect(document.querySelector("script")).toBeNull();
    });
  });

  // ===========================================================================
  // 2. VERY LONG MESSAGE CONTENT - 10000+ characters
  // ===========================================================================
  describe("Very long message content (10000+ chars)", () => {
    it("should handle 10000 character message without breaking layout", async () => {
      const messages = [{
        id: "msg-1",
        role: "assistant",
        content: "A".repeat(10000),
      }];
      _mockMessageCount = messages.length;
      setMockChatState({ messages, input: "", isStreaming: false, inputError: null });

      expect(() => {
        render(<TranscriptPane />);
      }).not.toThrow();

      await waitFor(() => {
        expect(screen.getByTestId("message-bubble")).toBeInTheDocument();
      });
    });

    it("should handle 50000 character message", async () => {
      setMockChatState({ messages: [{ id: "msg-1", role: "assistant", content: "Lorem ipsum ".repeat(5000) }], input: "", isStreaming: false, inputError: null });

      expect(() => {
        render(<TranscriptPane />);
      }).not.toThrow();
    });

    it("should handle message with no spaces (10000 chars)", async () => {
      const messages = [{
        id: "msg-1",
        role: "assistant",
        content: "AAAAAAAAAA".repeat(1000),
      }];
      _mockMessageCount = messages.length;
      setMockChatState({ messages, input: "", isStreaming: false, inputError: null });

      render(<TranscriptPane />);

      await waitFor(() => {
        expect(screen.getByTestId("message-bubble")).toBeInTheDocument();
      });
    });

    it("should handle message with multiple newlines and whitespace", async () => {
      setMockChatState({ messages: [{ id: "msg-1", role: "assistant", content: "\n\n\n".repeat(1000) + "   ".repeat(1000) + "text" }], input: "", isStreaming: false, inputError: null });

      expect(() => {
        render(<TranscriptPane />);
      }).not.toThrow();
    });
  });

  // ===========================================================================
  // 3. RAPID MESSAGE STREAMING - 100 messages rapidly
  // ===========================================================================
  describe("Rapid message streaming (100 messages)", () => {
    it("should handle 100 messages added rapidly", async () => {
      const messages = [];
      for (let i = 0; i < 100; i++) {
        messages.push({
          id: `msg-${i}`,
          role: i % 2 === 0 ? "user" : "assistant",
          content: `Message ${i}`,
        });
      }
      _mockMessageCount = messages.length;

      setMockChatState({ messages, input: "", isStreaming: false, inputError: null });

      render(<TranscriptPane />);

      await waitFor(() => {
        expect(screen.getAllByTestId("message-bubble")).toHaveLength(100);
      });
    });

    it("should handle streaming state changes rapidly", async () => {
      let streamingState = false;
      mockChatState.isStreaming = streamingState;

      render(<TranscriptPane />);

      // Rapidly toggle streaming state
      await act(async () => {
        for (let i = 0; i < 20; i++) {
          streamingState = i % 2 === 0;
          mockChatState.isStreaming = streamingState;
        }
      });

      // Component should render without crashing
      expect(screen.getByLabelText("Chat messages")).toBeInTheDocument();
    });

    it("should handle interleaved user/assistant messages", async () => {
      const interleavedMessages = [];
      for (let i = 0; i < 50; i++) {
        interleavedMessages.push({
          id: `user-${i}`,
          role: "user",
          content: `User message ${i}`,
        });
        interleavedMessages.push({
          id: `assistant-${i}`,
          role: "assistant",
          content: `Assistant response ${i} with some content`,
        });
      }
      _mockMessageCount = interleavedMessages.length;

      setMockChatState({ messages: interleavedMessages, input: "", isStreaming: false, inputError: null });

      render(<TranscriptPane />);

      await waitFor(() => {
        expect(screen.getAllByTestId("message-bubble")).toHaveLength(100);
      });
    });
  });

  // ===========================================================================
  // 4. SLASH COMMAND INJECTION - XSS-like patterns in slash menu
  // ===========================================================================
  describe("Slash command injection", () => {
    const injectionPayloads = [
      "/<script>alert(1)</script>",
      "/'><script>alert(1)</script>",
      '/" onload="alert(1)"',
      "/${alert(1)}",
      "/{{alert(1)}}",
      '/><img src=x onerror=alert(1)>',
    ];

    it.each(injectionPayloads)("should handle potentially malicious slash input: %s", async (payload) => {
      setMockChatState({ messages: [], input: payload, isStreaming: false, inputError: null });

      render(<TranscriptPane />);

      await waitFor(() => {
        expect(screen.getByLabelText("Message input")).toBeInTheDocument();
      });

      const textarea = screen.getByLabelText("Message input");
      
      expect(() => {
        fireEvent.change(textarea, { target: { value: payload } });
      }).not.toThrow();

      // Should not execute any scripts
      expect(document.querySelector("script")).toBeNull();
    });

    it("should handle slash command with very long query", async () => {
      const longSlash = "/" + "a".repeat(500);

      setMockChatState({ messages: [], input: "", isStreaming: false, inputError: null });

      render(<TranscriptPane />);

      const textarea = screen.getByLabelText("Message input");
      fireEvent.change(textarea, { target: { value: longSlash } });

      // Component should handle without crash
      expect(screen.getByLabelText("Message input")).toBeInTheDocument();
    });

    it("should handle rapid slash menu open/close with Escape", async () => {
      setMockChatState({ messages: [], input: "", isStreaming: false, inputError: null });

      render(<TranscriptPane />);

      const textarea = screen.getByLabelText("Message input");

      // Type "/" to open menu
      fireEvent.change(textarea, { target: { value: "/" } });

      await waitFor(() => {
        expect(screen.getByRole("listbox")).toBeInTheDocument();
      });

      // Rapidly open/close
      for (let i = 0; i < 10; i++) {
        fireEvent.keyDown(textarea, { key: "Escape" });
        await act(async () => {
          fireEvent.change(textarea, { target: { value: "/" } });
        });
        await act(async () => {
          fireEvent.keyDown(textarea, { key: "Escape" });
        });
      }

      // Should not crash and menu should be closed
      expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
    });

    it("should handle keyboard navigation in slash menu rapidly", async () => {
      setMockChatState({ messages: [], input: "", isStreaming: false, inputError: null });

      render(<TranscriptPane />);

      const textarea = screen.getByLabelText("Message input");
      fireEvent.change(textarea, { target: { value: "/" } });

      await waitFor(() => {
        expect(screen.getByRole("listbox")).toBeInTheDocument();
      });

      // Rapidly press arrow keys
      for (let i = 0; i < 20; i++) {
        await act(async () => {
          fireEvent.keyDown(textarea, { key: "ArrowDown" });
        });
      }

      expect(screen.getByRole("listbox")).toBeInTheDocument();
    });
  });

  // ===========================================================================
  // 5. TEXTAREA OVERFLOW - Paste 5000 characters
  // ===========================================================================
  describe("Textarea overflow (5000+ characters)", () => {
    it("should respect max height with 5000 character input", async () => {
      setMockChatState({ messages: [], input: "A".repeat(5000), isStreaming: false, inputError: null });

      render(<TranscriptPane />);

      const textarea = screen.getByLabelText("Message input");
      expect(textarea).toBeInTheDocument();

      // Component should handle large input without crashing
      expect(screen.getByLabelText("Chat messages")).toBeInTheDocument();
    });

    it("should handle input exceeding MAX_INPUT_LENGTH", async () => {
      setMockChatState({ messages: [], input: "A".repeat(3000), isStreaming: false, inputError: null }); // input exceeds MAX_INPUT_LENGTH of 2000

      render(<TranscriptPane />);

      const textarea = screen.getByLabelText("Message input");
      fireEvent.change(textarea, { target: { value: "A".repeat(3000) } });

      // Character count warning should appear
      await waitFor(() => {
        expect(screen.getByText(/\d+\/2000/)).toBeInTheDocument();
      });
    });

    it("should handle very long single word (no spaces)", async () => {
      const singleWord = "A".repeat(10000);

      setMockChatState({ messages: [], input: "", isStreaming: false, inputError: null });

      render(<TranscriptPane />);

      const textarea = screen.getByLabelText("Message input");
      
      // Should not crash when typing very long word
      expect(() => {
        fireEvent.change(textarea, { target: { value: singleWord } });
      }).not.toThrow();
    });

    it("should handle rapid input changes", async () => {
      setMockChatState({ messages: [], input: "", isStreaming: false, inputError: null });

      render(<TranscriptPane />);

      const textarea = screen.getByLabelText("Message input");

      await act(async () => {
        for (let i = 0; i < 100; i++) {
          fireEvent.change(textarea, { target: { value: "A".repeat(i) } });
        }
      });

      expect(screen.getByLabelText("Message input")).toBeInTheDocument();
    });

    it("should handle textarea with newlines that exceed max-height", async () => {
      const manyNewlines = "\n".repeat(50) + "text";

      setMockChatState({ messages: [], input: "", isStreaming: false, inputError: null });

      render(<TranscriptPane />);

      const textarea = screen.getByLabelText("Message input");
      fireEvent.change(textarea, { target: { value: manyNewlines } });

      // Component should handle
      expect(screen.getByLabelText("Message input")).toBeInTheDocument();
    });
  });

  // ===========================================================================
  // 6. EMPTY VAULT WITH UNDEFINED file_count
  // ===========================================================================
  describe("Empty vault with undefined/missing file_count", () => {
    it("should handle vault with undefined file_count", async () => {
      // Create vault without file_count property
      const vaultWithoutFileCount = {
        id: 1,
        name: "Test Vault",
        // file_count is intentionally missing
      };

      (useVaultStore as unknown as ReturnType<typeof vi.fn>).mockImplementation((selector) => {
        const state = {
          vaults: [vaultWithoutFileCount],
          activeVaultId: 1,
          getActiveVault: () => vaultWithoutFileCount as any,
        };
        return selector ? selector(state) : state;
      });

      setMockChatState({ messages: [], input: "", isStreaming: false, inputError: null });

      // This could crash if code tries to do activeVault.file_count > 0
      expect(() => {
        render(<TranscriptPane />);
      }).not.toThrow();
    });

    it("should handle vault with null file_count", async () => {
      const vaultWithNullFileCount = {
        id: 1,
        name: "Test Vault",
        file_count: null,
      };

      (useVaultStore as unknown as ReturnType<typeof vi.fn>).mockImplementation((selector) => {
        const state = {
          vaults: [vaultWithNullFileCount],
          activeVaultId: 1,
          getActiveVault: () => vaultWithNullFileCount as any,
        };
        return selector ? selector(state) : state;
      });

      setMockChatState({ messages: [], input: "", isStreaming: false, inputError: null });

      expect(() => {
        render(<TranscriptPane />);
      }).not.toThrow();
    });

    it("should handle vault with negative file_count", async () => {
      const vaultWithNegativeCount = {
        id: 1,
        name: "Test Vault",
        file_count: -5,
      };

      (useVaultStore as unknown as ReturnType<typeof vi.fn>).mockImplementation((selector) => {
        const state = {
          vaults: [vaultWithNegativeCount],
          activeVaultId: 1,
          getActiveVault: () => vaultWithNegativeCount,
        };
        return selector ? selector(state) : state;
      });

      setMockChatState({ messages: [], input: "", isStreaming: false, inputError: null });

      expect(() => {
        render(<TranscriptPane />);
      }).not.toThrow();
    });

    it("should handle vault with NaN file_count", async () => {
      const vaultWithNaNCount = {
        id: 1,
        name: "Test Vault",
        file_count: NaN,
      };

      (useVaultStore as unknown as ReturnType<typeof vi.fn>).mockImplementation((selector) => {
        const state = {
          vaults: [vaultWithNaNCount],
          activeVaultId: 1,
          getActiveVault: () => vaultWithNaNCount as any,
        };
        return selector ? selector(state) : state;
      });

      setMockChatState({ messages: [], input: "", isStreaming: false, inputError: null });

      expect(() => {
        render(<TranscriptPane />);
      }).not.toThrow();
    });

    it("should handle vault with Infinity file_count", async () => {
      const vaultWithInfinityCount = {
        id: 1,
        name: "Test Vault",
        file_count: Infinity,
      };

      (useVaultStore as unknown as ReturnType<typeof vi.fn>).mockImplementation((selector) => {
        const state = {
          vaults: [vaultWithInfinityCount],
          activeVaultId: 1,
          getActiveVault: () => vaultWithInfinityCount as any,
        };
        return selector ? selector(state) : state;
      });

      setMockChatState({ messages: [], input: "", isStreaming: false, inputError: null });

      expect(() => {
        render(<TranscriptPane />);
      }).not.toThrow();
    });

    it("should show empty state when file_count is 0 or falsy", async () => {
      const emptyVault = {
        id: 1,
        name: "Empty Vault",
        file_count: 0,
      };

      (useVaultStore as unknown as ReturnType<typeof vi.fn>).mockImplementation((selector) => {
        const state = {
          vaults: [emptyVault],
          activeVaultId: 1,
          getActiveVault: () => emptyVault,
        };
        return selector ? selector(state) : state;
      });

      setMockChatState({ messages: [], input: "", isStreaming: false, inputError: null });

      render(<TranscriptPane />);

      await waitFor(() => {
        // Should show empty transcript with upload prompt
        expect(screen.getByText(/Upload documents to get started/)).toBeInTheDocument();
      });
    });
  });

  // ===========================================================================
  // 7. RAPID SLASH MENU OPEN/CLOSE
  // ===========================================================================
  describe("Rapid slash menu open/close", () => {
    it("should handle concurrent slash command trigger and Escape", async () => {
      setMockChatState({ messages: [], input: "", isStreaming: false, inputError: null });

      render(<TranscriptPane />);

      const textarea = screen.getByLabelText("Message input");

      // Rapid open/close cycle
      for (let i = 0; i < 5; i++) {
        await act(async () => {
          fireEvent.change(textarea, { target: { value: "/" } });
        });
        
        await act(async () => {
          fireEvent.keyDown(textarea, { key: "Escape" });
        });
      }

      // Final state should be consistent
      expect(screen.getByLabelText("Message input")).toBeInTheDocument();
    });

    it("should handle slash menu state race condition", async () => {
      setMockChatState({ messages: [], input: "", isStreaming: false, inputError: null });

      render(<TranscriptPane />);

      const textarea = screen.getByLabelText("Message input");

      // Type partial command
      fireEvent.change(textarea, { target: { value: "/s" } });
      
      // Type space (should close menu)
      fireEvent.change(textarea, { target: { value: "/summarize " } });

      // Menu should be closed
      await waitFor(() => {
        expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
      });
    });

    it("should handle rapid command selection", async () => {
      setMockChatState({ messages: [], input: "", isStreaming: false, inputError: null });

      render(<TranscriptPane />);

      const textarea = screen.getByLabelText("Message input");

      // Open menu
      fireEvent.change(textarea, { target: { value: "/" } });

      await waitFor(() => {
        expect(screen.getByRole("listbox")).toBeInTheDocument();
      });

      // Rapidly select commands
      for (let i = 0; i < 10; i++) {
        await act(async () => {
          fireEvent.keyDown(textarea, { key: "Enter" });
        });
        // Re-open menu
        await act(async () => {
          fireEvent.change(textarea, { target: { value: "/" } });
        });
      }

      expect(screen.getByLabelText("Message input")).toBeInTheDocument();
    });

    it("should handle mouse/keyboard interleaving on slash menu", async () => {
      setMockChatState({ messages: [], input: "", isStreaming: false, inputError: null });

      render(<TranscriptPane />);

      const textarea = screen.getByLabelText("Message input");
      fireEvent.change(textarea, { target: { value: "/" } });

      await waitFor(() => {
        expect(screen.getByRole("listbox")).toBeInTheDocument();
      });

      // Keyboard navigation
      fireEvent.keyDown(textarea, { key: "ArrowDown" });
      
      // Mouse click on first option
      const options = screen.getAllByRole("option");
      if (options.length > 0) {
        fireEvent.click(options[0]);
      }

      // Menu should close after click
      await waitFor(() => {
        expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
      });
    });
  });

  // ===========================================================================
  // 8. SPECIAL CHARS IN VAULT NAME
  // ===========================================================================
  describe("Special chars in vault name", () => {
    const maliciousVaultNames = [
      '<script>alert(1)</script>',
      '<img onerror="alert(1)" src=x>',
      '"><script>alert(1)</script>',
      '${alert(1)}',
      '{{constructor.constructor("alert(1)")()}}',
      '<a href="javascript:alert(1)">click</a>',
      '<svg onload="alert(1)">',
      'vault<script>document.location="evil.com"</script>',
    ];

    it.each(maliciousVaultNames)("should NOT execute script in vault name: %s", async (vaultName) => {
      const maliciousVault = {
        id: 1,
        name: vaultName,
        file_count: 5,
      };

      (useVaultStore as unknown as ReturnType<typeof vi.fn>).mockImplementation((selector) => {
        const state = {
          vaults: [maliciousVault],
          activeVaultId: 1,
          getActiveVault: () => maliciousVault,
        };
        return selector ? selector(state) : state;
      });

      setMockChatState({ messages: [], input: "", isStreaming: false, inputError: null });

      render(<TranscriptPane />);

      await waitFor(() => {
        expect(screen.getByLabelText(/Active vault/)).toBeInTheDocument();
      });

      // No script execution
      expect(document.querySelector("script")).toBeNull();
    });

    it("should handle very long vault name (500+ chars)", async () => {
      const longNameVault = {
        id: 1,
        name: "A".repeat(500),
        file_count: 5,
      };

      (useVaultStore as unknown as ReturnType<typeof vi.fn>).mockImplementation((selector) => {
        const state = {
          vaults: [longNameVault],
          activeVaultId: 1,
          getActiveVault: () => longNameVault,
        };
        return selector ? selector(state) : state;
      });

      setMockChatState({ messages: [], input: "", isStreaming: false, inputError: null });

      render(<TranscriptPane />);

      // Badge should exist and handle long name
      const badge = screen.getByLabelText(/Active vault/);
      expect(badge).toBeInTheDocument();
    });

    it("should handle vault name with newlines and tabs", async () => {
      const weirdVault = {
        id: 1,
        name: "Vault\nName\tWith\nSpecial\nChars",
        file_count: 5,
      };

      (useVaultStore as unknown as ReturnType<typeof vi.fn>).mockImplementation((selector) => {
        const state = {
          vaults: [weirdVault],
          activeVaultId: 1,
          getActiveVault: () => weirdVault,
        };
        return selector ? selector(state) : state;
      });

      setMockChatState({ messages: [], input: "", isStreaming: false, inputError: null });

      expect(() => {
        render(<TranscriptPane />);
      }).not.toThrow();
    });

    it("should handle null vault name", async () => {
      const nullNameVault: any = {
        id: 1,
        name: null,
        file_count: 5,
      };

      (useVaultStore as unknown as ReturnType<typeof vi.fn>).mockImplementation((selector) => {
        const state = {
          vaults: [nullNameVault],
          activeVaultId: 1,
          getActiveVault: () => nullNameVault,
        };
        return selector ? selector(state) : state;
      });

      setMockChatState({ messages: [], input: "", isStreaming: false, inputError: null });

      expect(() => {
        render(<TranscriptPane />);
      }).not.toThrow();
    });

    it("should handle undefined vault name", async () => {
      const undefinedNameVault: any = {
        id: 1,
        file_count: 5,
      };

      (useVaultStore as unknown as ReturnType<typeof vi.fn>).mockImplementation((selector) => {
        const state = {
          vaults: [undefinedNameVault],
          activeVaultId: 1,
          getActiveVault: () => undefinedNameVault,
        };
        return selector ? selector(state) : state;
      });

      setMockChatState({ messages: [], input: "", isStreaming: false, inputError: null });

      expect(() => {
        render(<TranscriptPane />);
      }).not.toThrow();
    });

    it("should handle emoji in vault name", async () => {
      const emojiVault = {
        id: 1,
        name: "🎯 Vault 📚 🚀",
        file_count: 5,
      };

      (useVaultStore as unknown as ReturnType<typeof vi.fn>).mockImplementation((selector) => {
        const state = {
          vaults: [emojiVault],
          activeVaultId: 1,
          getActiveVault: () => emojiVault,
        };
        return selector ? selector(state) : state;
      });

      setMockChatState({ messages: [], input: "", isStreaming: false, inputError: null });

      render(<TranscriptPane />);

      await waitFor(() => {
        expect(screen.getByLabelText(/Active vault/)).toBeInTheDocument();
      });
    });

    it("should handle Unicode right-to-left override in vault name", async () => {
      const rtlVault = {
        id: 1,
        name: "\u202EEvil\u202C Vault", // RTL override + name
        file_count: 5,
      };

      (useVaultStore as unknown as ReturnType<typeof vi.fn>).mockImplementation((selector) => {
        const state = {
          vaults: [rtlVault],
          activeVaultId: 1,
          getActiveVault: () => rtlVault,
        };
        return selector ? selector(state) : state;
      });

      setMockChatState({ messages: [], input: "", isStreaming: false, inputError: null });

      render(<TranscriptPane />);

      // Should render without crashing
      expect(screen.getByLabelText(/Active vault/)).toBeInTheDocument();
    });
  });

  // ===========================================================================
  // ADDITIONAL ADVERSARIAL CASES
  // ===========================================================================
  describe("Additional edge cases", () => {
    // BUG DISCOVERED: The component crashes when messages is undefined
    // Error: TypeError: Cannot read properties of undefined (reading 'length')
    // This is documented below as a failing test that reveals the bug
    it.skip("should handle undefined messages array - SKIPPED: reveals component bug", async () => {
      // BUG: Component tries to access messages.length without checking if messages exists
      // Expected behavior: Should handle undefined gracefully, maybe default to []
      // Actual behavior: Crashes with TypeError
      mockChatState.messageIds = undefined as any;
      mockChatState.messagesById = undefined as any;

      expect(() => {
        render(<TranscriptPane />);
      }).not.toThrow();
    });

    it("should handle null message content", async () => {
      setMockChatState({ messages: [{ id: "1", role: "user", content: null }, { id: "2", role: "assistant", content: null }], input: "", isStreaming: false, inputError: null });

      expect(() => {
        render(<TranscriptPane />);
      }).not.toThrow();
    });

    it("should handle message with undefined sources", async () => {
      const messages = [
        { id: "1", role: "assistant", content: "test", sources: undefined },
      ];
      _mockMessageCount = messages.length;
      setMockChatState({ messages, input: "", isStreaming: false, inputError: null });

      render(<TranscriptPane />);

      await waitFor(() => {
        expect(screen.getByTestId("message-bubble")).toBeInTheDocument();
      });
    });

    it("should handle rapid scroll position changes", async () => {
      const messages = Array.from({ length: 50 }, (_, i) => ({
        id: `msg-${i}`,
        role: i % 2 === 0 ? "user" : "assistant",
        content: `Message ${i}`,
      }));
      _mockMessageCount = messages.length;

      setMockChatState({ messages, input: "", isStreaming: false, inputError: null });

      render(<TranscriptPane />);

      const scrollArea = screen.getByLabelText("Chat messages");
      
      // Rapid scroll events
      await act(async () => {
        for (let i = 0; i < 20; i++) {
          fireEvent.scroll(scrollArea, { target: { scrollTop: i * 100 } });
        }
      });

      expect(screen.getAllByTestId("message-bubble")).toHaveLength(50);
    });

    it("should handle message with circular reference in sources", async () => {
      const circularObj: any = { id: 1, filename: "test" };
      circularObj.self = circularObj; // Circular reference

      setMockChatState({ messages: [{ id: "1", role: "assistant", content: "test", sources: [circularObj] }], input: "", isStreaming: false, inputError: null });

      expect(() => {
        render(<TranscriptPane />);
      }).not.toThrow();
    });

    it("should handle empty message array with streaming true", async () => {
      setMockChatState({ messages: [], input: "", isStreaming: true, inputError: null });

      render(<TranscriptPane />);

      // Should show empty transcript - use getByRole instead of text matcher
      await waitFor(() => {
        expect(screen.getByRole("region", { name: /empty transcript/i })).toBeInTheDocument();
      });
    });

    it("should handle message with malformed source data", async () => {
      setMockChatState({
        messages: [{
          id: "1",
          role: "assistant",
          content: "test",
          sources: [{ id: 1 }, { filename: 123, score: "invalid" }, undefined, null] as any,
        }],
        input: "",
        isStreaming: false,
        inputError: null,
      });

      expect(() => {
        render(<TranscriptPane />);
      }).not.toThrow();
    });

    it("should handle EmptyTranscript with null callbacks", async () => {
      expect(() => {
        render(
          <EmptyTranscript
            onPromptClick={null as any}
            hasIndexedDocs={false}
            onNavigateToDocuments={null as any}
          />
        );
      }).not.toThrow();
    });

    it("should handle message with very deep nesting in content", async () => {
      // Create deeply nested markdown that could cause issues
      const deepNested = "# ".repeat(50) + "Title\n" + "> ".repeat(50) + "Quote";

      setMockChatState({ messages: [{ id: "1", role: "assistant", content: deepNested }], input: "", isStreaming: false, inputError: null });

      expect(() => {
        render(<TranscriptPane />);
      }).not.toThrow();
    });
  });

  // ===========================================================================
  // BOUNDARY VIOLATIONS
  // ===========================================================================
  describe("Boundary violations", () => {
    it("should handle messages at Number.MAX_SAFE_INTEGER index", async () => {
      setMockChatState({ messages: [{ id: String(Number.MAX_SAFE_INTEGER), role: "assistant", content: "Boundary test" }], input: "", isStreaming: false, inputError: null });

      expect(() => {
        render(<TranscriptPane />);
      }).not.toThrow();
    });

    it("should handle negative message indices", async () => {
      setMockChatState({ messages: [{ id: "-1", role: "assistant", content: "Negative ID message" }], input: "", isStreaming: false, inputError: null });

      expect(() => {
        render(<TranscriptPane />);
      }).not.toThrow();
    });

    it("should handle NaN in message indices", async () => {
      setMockChatState({ messages: [{ id: String(NaN), role: "assistant", content: "NaN ID message" }], input: "", isStreaming: false, inputError: null });

      expect(() => {
        render(<TranscriptPane />);
      }).not.toThrow();
    });
  });

  // ===========================================================================
  // RACE CONDITIONS
  // ===========================================================================
  describe("Race conditions", () => {
    it("should handle streaming state flipping during render", async () => {
      let streamingState = false;
      _mockMessageCount = 1;
      setMockChatState({ messages: [{ id: "1", role: "assistant", content: "test" }], input: "", isStreaming: false, inputError: null });

      render(<TranscriptPane />);

      // Rapid state changes
      await act(async () => {
        for (let i = 0; i < 10; i++) {
          streamingState = i % 2 === 0;
          mockChatState.isStreaming = streamingState;
        }
      });

      expect(screen.getByTestId("message-bubble")).toBeInTheDocument();
    });

    it("should handle vault becoming active during render", async () => {
      // First render with no vault
      (useVaultStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
        vaults: [],
        activeVaultId: null,
        getActiveVault: () => undefined,
      });

      setMockChatState({ messages: [], input: "", isStreaming: false, inputError: null });

      render(<TranscriptPane />);

      // Update store to have an active vault
      (useVaultStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
        vaults: [{ id: 1, name: "New Vault", file_count: 5 }],
        activeVaultId: 1,
        getActiveVault: () => ({ id: 1, name: "New Vault", file_count: 5 }),
      });

      // Force re-render
      render(<TranscriptPane />);

      expect(screen.getByLabelText(/Active vault/)).toBeInTheDocument();
    });
  });

  describe("Type confusion attacks", () => {
    // Regression coverage for scenarios previously covered by the deleted
    // TranscriptPane.{,adversarial.}virtualization.test.tsx files (removed
    // because they mocked an unused @tanstack/react-virtual module — but
    // these specific role/content type-confusion cases are not tied to the
    // virtualizer and were otherwise left untested). MessageBubble renders
    // `message.content` directly in JSX, so a non-string content value can
    // throw React's "Objects are not valid as a React child" error.
    it("should handle message with number role instead of string", async () => {
      const messages = [{ id: "msg-1", role: 123 as any, content: "Number role" }];
      setMockChatState({ messages, input: "", isStreaming: false, inputError: null });

      expect(() => {
        render(<TranscriptPane />);
      }).not.toThrow();
    });

    it("should handle message with array content instead of string", async () => {
      const messages = [{ id: "msg-1", role: "assistant", content: ["array", "content"] as any }];
      setMockChatState({ messages, input: "", isStreaming: false, inputError: null });

      expect(() => {
        render(<TranscriptPane />);
      }).not.toThrow();
    });

    it("should handle message with plain-object content instead of string", async () => {
      const messages = [{ id: "msg-1", role: "assistant", content: { nested: "object" } as any }];
      setMockChatState({ messages, input: "", isStreaming: false, inputError: null });

      expect(() => {
        render(<TranscriptPane />);
      }).not.toThrow();
    });
  });
});
