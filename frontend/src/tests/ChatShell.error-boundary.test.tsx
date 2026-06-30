// frontend/src/tests/ChatShell.error-boundary.test.tsx
/**
 * FR-017: Per-pane ErrorBoundary isolation tests
 *
 * Verifies that when one pane in ChatShell throws, its ErrorBoundary
 * catches the error (showing the fallback) without taking down the other panes.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, act } from "@testing-library/react";
import React from "react";
import { BrowserRouter } from "react-router-dom";
import ChatShell from "@/pages/ChatShell";

// =============================================================================
// MOCK RESIZE OBSERVER
// =============================================================================

class MockResizeObserver {
  observe = vi.fn();
  unobserve = vi.fn();
  disconnect = vi.fn();
}
global.ResizeObserver = MockResizeObserver as unknown as typeof ResizeObserver;

if (typeof Element !== "undefined" && !Element.prototype.scrollTo) {
  Element.prototype.scrollTo = vi.fn();
}

// =============================================================================
// MOCK STORES
// =============================================================================

vi.mock("@/stores/useChatShellStore", () => ({
  __esModule: true,
  default: vi.fn((selector?: (s: any) => any) => {
    const state = {
      sessionRailOpen: true,
      rightPaneOpen: true,
      rightPaneWidth: 360,
      sessionRailWidth: 260,
      activeSessionId: "session-1",
      activeSessionTitle: "Test Session",
      sessionListRefreshToken: 0,
      sessionSearchQuery: "",
      pinnedSessionIds: [] as number[],
      selectedEvidenceSource: null,
      activeRightTab: "evidence" as const,
      toggleSessionRail: vi.fn(),
      toggleRightPane: vi.fn(),
      setRightPaneWidth: vi.fn(),
      setSessionRailWidth: vi.fn(),
      setActiveSessionId: vi.fn(),
      setActiveSessionTitle: vi.fn(),
      requestSessionListRefresh: vi.fn(),
      openSessionRail: vi.fn(),
      closeSessionRail: vi.fn(),
      openRightPane: vi.fn(),
      closeRightPane: vi.fn(),
      setSessionSearchQuery: vi.fn(),
      togglePinSession: vi.fn(),
      isSessionPinned: vi.fn(() => false),
      setSelectedEvidenceSource: vi.fn(),
      setActiveRightTab: vi.fn(),
    };
    return typeof selector === "function" ? selector(state) : state;
  }),
  useChatShellStore: vi.fn((selector?: (s: any) => any) => {
    const state = {
      sessionRailOpen: true,
      rightPaneOpen: true,
      rightPaneWidth: 360,
      sessionRailWidth: 260,
      activeSessionId: "session-1",
      activeSessionTitle: "Test Session",
      sessionListRefreshToken: 0,
      sessionSearchQuery: "",
      pinnedSessionIds: [] as number[],
      selectedEvidenceSource: null,
      activeRightTab: "evidence" as const,
      toggleSessionRail: vi.fn(),
      toggleRightPane: vi.fn(),
      setRightPaneWidth: vi.fn(),
      setSessionRailWidth: vi.fn(),
      setActiveSessionId: vi.fn(),
      setActiveSessionTitle: vi.fn(),
      requestSessionListRefresh: vi.fn(),
      openSessionRail: vi.fn(),
      closeSessionRail: vi.fn(),
      openRightPane: vi.fn(),
      closeRightPane: vi.fn(),
      setSessionSearchQuery: vi.fn(),
      togglePinSession: vi.fn(),
      isSessionPinned: vi.fn(() => false),
      setSelectedEvidenceSource: vi.fn(),
      setActiveRightTab: vi.fn(),
    };
    return typeof selector === "function" ? selector(state) : state;
  }),
}));

vi.mock("@/stores/useChatStore", () => ({
  useChatStore: vi.fn((selector?: (s: any) => any) => {
    const state = {
      messageIds: [] as string[],
      messagesById: {} as Record<string, unknown>,
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
    };
    return typeof selector === "function" ? selector(state) : state;
  }),
  useChatMessages: vi.fn(() => []),
  useMessageIds: vi.fn(() => []),
  useMessage: vi.fn(() => null),
}));

vi.mock("@/hooks/useSendMessage", () => ({
  useSendMessage: vi.fn(() => ({
    handleSend: vi.fn(),
    handleStop: vi.fn(),
    sendDirect: vi.fn(),
  })),
  MAX_INPUT_LENGTH: 2000,
}));

vi.mock("@/hooks/useChatHistory", () => ({
  useChatHistory: vi.fn(() => ({
    refreshHistory: vi.fn(),
    chatHistory: [],
    isChatLoading: false,
  })),
}));

vi.mock("@/fixtures/TestModeContext", () => ({
  useTestMode: vi.fn(() => false),
}));

vi.mock("@/lib/api", () => ({
  getChatSession: vi.fn(() => Promise.resolve({ messages: [] })),
  chatStream: vi.fn(),
  uploadDocument: vi.fn(),
  getDocumentStatus: vi.fn(),
}));

// =============================================================================
// MOCK UI COMPONENTS
// =============================================================================

vi.mock("framer-motion", () => ({
  motion: {
    div: ({ children, ...props }: { children: React.ReactNode }) => (
      <div data-testid="motion-div" {...props}>{children}</div>
    ),
  },
  AnimatePresence: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  useReducedMotion: () => false,
}));

vi.mock("react-router-dom", () => ({
  BrowserRouter: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  useNavigate: () => vi.fn(),
  useParams: vi.fn(() => ({})),
}));

vi.mock("@/components/shared/KeyboardShortcuts", () => ({
  useKeyboardShortcuts: vi.fn(() => ({
    open: false,
    setOpen: vi.fn(),
  })),
  KeyboardShortcutsDialog: () => <div data-testid="keyboard-shortcuts-dialog" />,
}));

vi.mock("@/components/vault/VaultSelector", () => ({
  VaultSelector: () => <div data-testid="vault-selector" />,
}));

vi.mock("@/components/shared/ErrorState", () => ({
  ErrorState: ({ title }: { title?: string }) => (
    <div data-testid="error-state" data-title={title} role="alert">
      ErrorState: {title ?? "Something went wrong"}
    </div>
  ),
}));

// =============================================================================
// MOCK PANE COMPONENTS — per-test behaviour is set via module-level refs
// =============================================================================

interface PaneConfig {
  throwOnRender: boolean;
  renderName: string;
}

let paneConfigs: Record<string, PaneConfig> = {
  SessionRail: { throwOnRender: false, renderName: "SessionRail" },
  TranscriptPane: { throwOnRender: false, renderName: "TranscriptPane" },
  RightPane: { throwOnRender: false, renderName: "RightPane" },
};

vi.mock("@/components/chat/SessionRail", () => ({
  SessionRail: () => {
    if (paneConfigs.SessionRail.throwOnRender) {
      throw new Error("SessionRail crashed");
    }
    return <div data-testid="pane-session-rail">SessionRail content</div>;
  },
}));

vi.mock("@/components/chat/TranscriptPane", () => ({
  TranscriptPane: () => {
    if (paneConfigs.TranscriptPane.throwOnRender) {
      throw new Error("TranscriptPane crashed");
    }
    return <div data-testid="pane-transcript">TranscriptPane content</div>;
  },
}));

vi.mock("@/components/chat/RightPane", () => ({
  RightPane: () => {
    if (paneConfigs.RightPane.throwOnRender) {
      throw new Error("RightPane crashed");
    }
    return <div data-testid="pane-right">RightPane content</div>;
  },
}));

// =============================================================================
// HELPER — render ChatShell
// =============================================================================

function renderChatShell() {
  return render(
    <BrowserRouter>
      <ChatShell />
    </BrowserRouter>
  );
}

// =============================================================================
// TESTS — FR-017: Per-pane ErrorBoundary isolation
// =============================================================================

describe("FR-017: ChatShell per-pane ErrorBoundary isolation", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // Reset all pane configs to non-throwing
    paneConfigs = {
      SessionRail: { throwOnRender: false, renderName: "SessionRail" },
      TranscriptPane: { throwOnRender: false, renderName: "TranscriptPane" },
      RightPane: { throwOnRender: false, renderName: "RightPane" },
    };
  });

  it("renders all three panes normally when none throw", async () => {
    await act(async () => {
      renderChatShell();
    });
    expect(screen.getByTestId("pane-session-rail")).toBeInTheDocument();
    expect(screen.getByTestId("pane-transcript")).toBeInTheDocument();
    expect(screen.getByTestId("pane-right")).toBeInTheDocument();
  });

  it("TranscriptPane crash shows its fallback without taking down SessionRail or RightPane", async () => {
    paneConfigs.TranscriptPane.throwOnRender = true;

    await act(async () => {
      renderChatShell();
    });

    // TranscriptPane ErrorBoundary caught the error and shows its fallback
    const transcriptFallback = screen.getByTestId("error-state");
    expect(transcriptFallback).toBeInTheDocument();
    expect(transcriptFallback).toHaveAttribute("data-title", "Chat area error");

    // SessionRail is still rendered
    expect(screen.getByTestId("pane-session-rail")).toBeInTheDocument();

    // RightPane is still rendered
    expect(screen.getByTestId("pane-right")).toBeInTheDocument();
  });

  it("SessionRail crash shows its fallback without taking down TranscriptPane or RightPane", async () => {
    paneConfigs.SessionRail.throwOnRender = true;

    await act(async () => {
      renderChatShell();
    });

    // SessionRail ErrorBoundary caught the error and shows its fallback
    const sessionsFallback = screen.getByTestId("error-state");
    expect(sessionsFallback).toBeInTheDocument();
    expect(sessionsFallback).toHaveAttribute("data-title", "Sessions error");

    // TranscriptPane is still rendered
    expect(screen.getByTestId("pane-transcript")).toBeInTheDocument();

    // RightPane is still rendered
    expect(screen.getByTestId("pane-right")).toBeInTheDocument();
  });

  it("RightPane crash shows its fallback without taking down SessionRail or TranscriptPane", async () => {
    paneConfigs.RightPane.throwOnRender = true;

    await act(async () => {
      renderChatShell();
    });

    // RightPane ErrorBoundary caught the error and shows its fallback
    const sourcesFallback = screen.getByTestId("error-state");
    expect(sourcesFallback).toBeInTheDocument();
    expect(sourcesFallback).toHaveAttribute("data-title", "Sources error");

    // SessionRail is still rendered
    expect(screen.getByTestId("pane-session-rail")).toBeInTheDocument();

    // TranscriptPane is still rendered
    expect(screen.getByTestId("pane-transcript")).toBeInTheDocument();
  });

  it("fallback titles are pane-appropriate", async () => {
    paneConfigs.TranscriptPane.throwOnRender = true;

    await act(async () => {
      renderChatShell();
    });

    const fallback = screen.getByTestId("error-state");
    expect(fallback).toHaveAttribute("data-title", "Chat area error");
  });
});
