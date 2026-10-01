// frontend/src/components/chat/TranscriptPane.a07.highlightTimer.test.tsx
// A07 (AC9): "evidence:jump-to-answer" highlights the target message and
// clears the highlight after 1500ms. Two jumps in quick succession (X at
// t=0, Y at t=1000) must leave Y highlighted at t=1600 — the first jump's
// 1500ms timer must not clear the second jump's highlight. On the current
// tree the single shared setTimeout set by X's jump fires at t=1500 and
// nulls the state Y had just set.

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, act } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { TranscriptPane } from "./TranscriptPane";
import { useChatStore } from "@/stores/useChatStore";
import { useChatShellStore } from "@/stores/useChatShellStore";
import { useVaultStore } from "@/stores/useVaultStore";
import { useLlmHealthStore } from "@/stores/useLlmHealthStore";
import { useChatModeStore } from "@/stores/useChatModeStore";
import type { Vault } from "@/lib/api";

// Mock ResizeObserver for Radix UI ScrollArea; jsdom omits the scroll APIs
// (the jump handler and auto-scroll effects call them).
class MockResizeObserver {
  observe = vi.fn();
  unobserve = vi.fn();
  disconnect = vi.fn();
}
global.ResizeObserver = MockResizeObserver as unknown as typeof ResizeObserver;
Element.prototype.scrollIntoView = vi.fn();
Element.prototype.scrollTo = vi.fn();

const chatStreamMock = vi.hoisted(() => vi.fn());
const listChatSessionsMock = vi.hoisted(() => vi.fn());

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    chatStream: (...args: unknown[]) => chatStreamMock(...args),
    listChatSessions: (...args: unknown[]) => listChatSessionsMock(...args),
    getLlmModeHealth: vi.fn(async () => ({ thinking: true, instant: true })),
  };
});

vi.mock("sonner", () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
    warning: vi.fn(),
    info: vi.fn(),
  },
}));

// Heavy children are stubbed: the highlight ring lives in MessageRow's root
// div inside TranscriptPane, so nothing under test is lost.
vi.mock("./AssistantMessage", () => ({
  AssistantMessage: ({ message }: { message: { content: string } }) => (
    <div data-testid="assistant-stub">{message.content}</div>
  ),
}));
vi.mock("./Composer", () => ({
  Composer: () => <div data-testid="composer-stub" />,
}));

const TEST_VAULT: Vault = { id: 1, name: "Test Vault", file_count: 5 } as Vault;

const sharedSource = { id: "chunk-shared", filename: "handbook.pdf" };

describe("TranscriptPane jump highlight timer (A07 AC9)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    chatStreamMock.mockReturnValue(vi.fn());
    listChatSessionsMock.mockResolvedValue({ sessions: [] });

    useChatStore.setState({
      activeChatId: "7",
      messageIds: ["msg-x", "msg-y"],
      messagesById: {
        "msg-x": {
          id: "msg-x",
          role: "assistant",
          content: "first answer cites the shared chunk [S1]",
          status: "complete",
          sources: [sharedSource],
        },
        "msg-y": {
          id: "msg-y",
          role: "assistant",
          content: "second answer cites the shared chunk [S1]",
          status: "complete",
          sources: [sharedSource],
        },
      },
      isStreaming: false,
      streamingMessageId: null,
      input: "",
      abortFn: null,
      inputError: null,
      expandedSources: new Set(),
      pendingTurnPersist: null,
      messageEditVersions: {},
      activeEditVersion: {},
    });
    useChatShellStore.setState({
      activeSessionId: "7",
      activeSessionTitle: null,
      selectedEvidenceSource: sharedSource,
      selectedEvidenceMessageId: "msg-y",
      activeRightTab: "evidence",
    });
    useVaultStore.setState({
      vaults: [TEST_VAULT],
      activeVaultId: 1,
      loading: false,
      error: null,
    });
    useLlmHealthStore.setState({ thinking: true, instant: true });
    useChatModeStore.setState({ scopeDocumentIds: null });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("a second jump is not cleared by the first jump's timer", async () => {
    vi.useFakeTimers();

    render(
      <MemoryRouter>
        <TranscriptPane />
      </MemoryRouter>
    );
    // Let mount-time async work (the history load) settle inside act.
    await act(async () => {});

    const jumpTo = (messageId: string) => {
      act(() => {
        window.dispatchEvent(
          new CustomEvent("evidence:jump-to-answer", {
            detail: { sourceId: "chunk-shared", messageId },
          })
        );
      });
    };

    jumpTo("msg-x"); // t = 0
    act(() => {
      vi.advanceTimersByTime(1000);
    });
    jumpTo("msg-y"); // t = 1000
    act(() => {
      vi.advanceTimersByTime(600); // t = 1600 — X's 1500ms timer has fired
    });

    // Exactly one row is highlighted, and it is message Y's.
    const highlightedRows = document.querySelectorAll(
      "[data-message-id] .ring-2"
    );
    expect(highlightedRows.length).toBe(1);
    expect(
      highlightedRows[0]?.closest("[data-message-id]")?.getAttribute("data-message-id")
    ).toBe("msg-y");
  });
});
