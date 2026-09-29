// frontend/src/components/chat/TranscriptPane.a03.stepperHistory.test.tsx
// Issue #685 — stepping an edited turn's version stepper is DISPLAY-ONLY:
// it must never rewrite the message's live content, because the live content
// is what the NEXT send uses as conversation history. At the pre-fix tree
// handleSelectEditVersion writes the displayed snapshot into the message via
// updateMessage, so after stepping back to the older version a follow-up
// send ships the ORIGINAL text to the model instead of the edited one.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { TranscriptPane } from "./TranscriptPane";
import { useChatStore } from "@/stores/useChatStore";
import { useChatShellStore } from "@/stores/useChatShellStore";
import { useVaultStore } from "@/stores/useVaultStore";
import { useLlmHealthStore } from "@/stores/useLlmHealthStore";
import { useChatModeStore } from "@/stores/useChatModeStore";
import type { Vault } from "@/lib/api";

// Mock ResizeObserver for Radix UI ScrollArea
class MockResizeObserver {
  observe = vi.fn();
  unobserve = vi.fn();
  disconnect = vi.fn();
}
global.ResizeObserver = MockResizeObserver as unknown as typeof ResizeObserver;
Element.prototype.scrollIntoView = vi.fn();

const chatStreamMock = vi.hoisted(() => vi.fn());
const addChatMessagesBatchMock = vi.hoisted(() => vi.fn());
const createChatSessionMock = vi.hoisted(() => vi.fn());
const truncateChatSessionMock = vi.hoisted(() =>
  vi.fn(async () => ({ remaining_count: 0, tail_seq: 0 }))
);
const forkChatSessionMock = vi.hoisted(() => vi.fn(async () => ({ id: 99, messages: [] })));
const getChatSessionMock = vi.hoisted(() => vi.fn(async () => ({ messages: [] })));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    chatStream: (...args: unknown[]) => chatStreamMock(...args),
    addChatMessagesBatch: (...args: unknown[]) => addChatMessagesBatchMock(...args),
    createChatSession: (...args: unknown[]) => createChatSessionMock(...args),
    truncateChatSession: truncateChatSessionMock,
    forkChatSession: forkChatSessionMock,
    getChatSession: getChatSessionMock,
    listChatSessions: vi.fn(async () => ({ sessions: [] })),
    listAccessibleVaults: vi.fn(async () => ({ vaults: [] })),
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

// AssistantMessage's canvas bridge (useCanvasCapabilities) imports the canvas
// API module DIRECTLY (not via the barrel) and needs a QueryClient. Once the
// turn's rows migrate to numeric server ids the bridge mounts, so keep the
// capabilities query offline and fail-closed.
vi.mock("@/lib/api/canvas", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api/canvas")>(
    "@/lib/api/canvas"
  );
  return {
    ...actual,
    getCanvasCapabilities: vi.fn(async () => ({ enabled: false })),
  };
});

const TEST_VAULT: Vault = { id: 1, name: "Test Vault", file_count: 5 } as Vault;

describe("TranscriptPane stepper history (issue #685)", () => {
  beforeEach(() => {
    vi.clearAllMocks();

    useChatStore.setState({
      activeChatId: "7",
      messageIds: ["u1", "a1"],
      messagesById: {
        u1: {
          id: "u1",
          role: "user",
          content: "original question",
          created_at: "2026-05-12T00:00:00Z",
        },
        a1: {
          id: "a1",
          role: "assistant",
          content: "original answer",
          status: "complete",
          created_at: "2026-05-12T00:00:01Z",
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
    useChatShellStore.setState({ activeSessionId: "7", activeSessionTitle: null });
    useVaultStore.setState({
      vaults: [TEST_VAULT],
      activeVaultId: 1,
      loading: false,
      error: null,
    });
    useLlmHealthStore.setState({ thinking: true, instant: true });
    useChatModeStore.setState({ scopeDocumentIds: null });

    createChatSessionMock.mockResolvedValue({ id: 42 });
    addChatMessagesBatchMock.mockResolvedValue([
      { id: 100, created_at: "2026-05-12T00:00:00Z", seq: 1 },
      { id: 101, created_at: "2026-05-12T00:00:01Z", seq: 2 },
    ]);

    // Successful stream shape (TranscriptPane.pendingPersist pattern).
    chatStreamMock.mockImplementation(
      (
        _messages: unknown,
        handlers: {
          onMessage: (chunk: string) => void;
          onComplete: () => Promise<void>;
        }
      ) => {
        handlers.onMessage("hello");
        void handlers.onComplete();
        return vi.fn();
      }
    );
  });

  afterEach(() => {
    useChatStore.getState().newChat();
  });

  it("stepping versions does not change send history", async () => {
    render(
      <QueryClientProvider
        client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
      >
        <MemoryRouter>
          <TranscriptPane />
        </MemoryRouter>
      </QueryClientProvider>
    );

    // 1. Edit the user turn: the real edit path truncates server-side, trims
    //    the local transcript and restores the text into the composer.
    fireEvent.click(screen.getByRole("button", { name: "Edit message" }));
    await waitFor(() => expect(truncateChatSessionMock).toHaveBeenCalledTimes(1));
    await waitFor(() =>
      expect(screen.queryByText("original answer")).not.toBeInTheDocument()
    );

    // 2. Re-send the edited question from the main composer. The turn saves
    //    durably and the slot now holds two versions ("2 / 2").
    const composer = screen.getByLabelText("Message input");
    fireEvent.change(composer, { target: { value: "edited question" } });
    fireEvent.keyDown(composer, { key: "Enter" });
    await waitFor(() => expect(screen.getByText("2 / 2")).toBeInTheDocument());
    await waitFor(() => expect(addChatMessagesBatchMock).toHaveBeenCalledTimes(1));
    // Let the id migration settle before stepping (it moves the row ids).
    await waitFor(() => {
      const state = useChatStore.getState();
      const assistant = state.messageIds
        .map((id) => state.messagesById[id])
        .find((m) => m?.role === "assistant");
      expect(assistant?.saveState).not.toBe("saving");
    });

    // 3. Step BACK to the older version — a display-only action.
    fireEvent.click(screen.getByRole("button", { name: "Show previous version" }));
    await waitFor(() => expect(screen.getByText("1 / 2")).toBeInTheDocument());

    // 4. Send a follow-up from the main composer. The history the backend
    //    receives for the edited turn must still carry the EDITED text.
    const composerAfterStep = screen.getByLabelText("Message input");
    fireEvent.change(composerAfterStep, { target: { value: "follow up question" } });
    fireEvent.keyDown(composerAfterStep, { key: "Enter" });

    await waitFor(() => expect(chatStreamMock).toHaveBeenCalledTimes(2));
    const followUpMessages = chatStreamMock.mock.calls[1][0] as Array<{
      role: string;
      content: string;
    }>;
    const editedTurn = followUpMessages.find((m) => m.role === "user");
    expect(editedTurn?.content).toBe("edited question");
  });
});
