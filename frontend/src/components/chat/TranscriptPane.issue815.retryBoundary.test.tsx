// Issue #815 retry-boundary pin (unfrozen, plan-critic rounds 1-2):
// NOTE (out-of-band review C-04/M-26): this suite drives the REAL
// TranscriptPane Retry path and the REAL truncate-request builder, but the
// keepalive migration itself is simulated at the store level
// (replaceMessageId with the server ids/seqs) because the pagehide +
// deferred-response interleaving cannot be produced in jsdom. The
// end-to-end keepalive migration is pinned by
// useSendMessage.issue815.test.ts; this file pins what the Retry path does
// with rows that HAVE been migrated.
// the REAL truncate inputs must cover the keepalive turn's server seqs.
//
// Scenario (#815's interleaving): turn 1 was persisted through the keepalive
// path (durable server-side at seq 1/2 but, pre-migration, carrying no seq in
// the store); turn 2 went through the normal path (seq 3/4). Retry on turn 2
// keeps ONLY turn 1's rows — if the keepalive migration (the tracked
// pending-save chain the fix registers) is not awaited before the boundary is
// computed, durableKeepSeq sees no numeric seq on the kept rows and the
// truncate call ships keep_seq=0, deleting turn 1's durable rows server-side.
// With the fix, the awaited tracked chain migrates seq 1/2 into the rows and
// the truncate call carries keep_seq=2.
//
// Harness mirrors TranscriptPane.retryDurability.test.tsx (real chat store,
// stateful server fake, batch/truncate mocks).

import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, act, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { Message } from "@/lib/api";

class MockResizeObserver {
  observe = vi.fn();
  unobserve = vi.fn();
  disconnect = vi.fn();
}
global.ResizeObserver = MockResizeObserver as unknown as typeof ResizeObserver;
Element.prototype.scrollIntoView = vi.fn();
Element.prototype.scrollTo = vi.fn();

const truncateChatSessionMock = vi.hoisted(() => vi.fn());
const addChatMessagesBatchMock = vi.hoisted(() => vi.fn());
const createChatSessionMock = vi.hoisted(() => vi.fn());
const chatStreamMock = vi.hoisted(() => vi.fn());
const forkChatSessionMock = vi.hoisted(() => vi.fn());
const mockRefreshHistory = vi.hoisted(() => vi.fn());
const mockNavigate = vi.hoisted(() => vi.fn());
const mockGetActiveVault = vi.hoisted(() => vi.fn());

vi.mock("@/lib/api", () => ({
  createChatSession: createChatSessionMock,
  addChatMessagesBatch: addChatMessagesBatchMock,
  addChatMessagesBatchKeepalive: vi.fn(),
  chatStream: chatStreamMock,
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
  MessageBubble: ({ message }: { message: { id: string; content: string } }) => (
    <div data-testid="message-bubble" data-message-id={message.id}>{message.content}</div>
  ),
}));
vi.mock("./AssistantMessage", () => ({
  AssistantMessage: ({
    message,
    onRetry,
  }: { message: { id: string }; onRetry?: () => void }) => (
    <div data-testid="assistant-message" data-message-id={message.id}>
      {onRetry && (
        <button type="button" aria-label={`Retry ${message.id}`} onClick={onRetry}>
          Retry
        </button>
      )}
    </div>
  ),
}));

import { TranscriptPane } from "./TranscriptPane";
import { useVaultStore } from "@/stores/useVaultStore";
import { useChatStore } from "@/stores/useChatStore";
import { useLlmHealthStore } from "@/stores/useLlmHealthStore";
import { useChatShellStore } from "@/stores/useChatShellStore";
import { useChatModeStore } from "@/stores/useChatModeStore";

describe("Retry truncate boundary covers keepalive-saved rows (issue #815)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockRefreshHistory.mockResolvedValue(undefined);

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

    createChatSessionMock.mockResolvedValue({ id: 42 });
    forkChatSessionMock.mockResolvedValue({ id: 20, messages: [] });
    truncateChatSessionMock.mockResolvedValue({ remaining_count: 2, tail_seq: 2 });
    addChatMessagesBatchMock.mockResolvedValue([]);
    chatStreamMock.mockImplementation(async function* () {
      yield { type: "done" } as never;
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
  });

  it("keep_seq covers the keepalive turn's server seqs once the tracked chain migrates them", async () => {
    // Turn 1: keepalive-saved — durable server-side (ids 1/2, seq 1/2) but
    // PRE-migration in the store (temp ids, no seq).
    useChatStore.getState().loadChat("7", [
      { id: "k-user", role: "user", content: "keepalive question" },
      { id: "k-assistant", role: "assistant", content: "keepalive answer" },
    ] as unknown as Message[]);
    // Turn 2: normal path — store rows already carry the server ids/seqs.
    useChatStore.getState().addMessage({ id: "3", role: "user", content: "second question", seq: 3 } as unknown as Message);
    useChatStore.getState().addMessage({ id: "4", role: "assistant", content: "second answer", seq: 4 } as unknown as Message);

    // The fixed keepalive path registers a TRACKED pending-save chain that
    // migrates the durable ids/seqs into turn 1's rows when it settles.
    let settleChain: () => void = () => {};
    const tracked = new Promise<void>((resolve) => {
      settleChain = () => {
        const store = useChatStore.getState();
        store.replaceMessageId("k-user", "1", { seq: 1, saveState: "saved" } as Partial<Message>);
        store.replaceMessageId("k-assistant", "2", { seq: 2, saveState: "saved" } as Partial<Message>);
        resolve();
      };
    });
    useChatStore.getState().setPendingTurnPersist(tracked);

    render(<TranscriptPane />);

    // Retry turn 2: kept rows are ONLY turn 1's (the retried turn is
    // discarded). The boundary computation must await the tracked chain.
    await act(async () => {
      await userEvent.click(screen.getByLabelText("Retry 4"));
    });

    // Let the awaited chain settle inside the retry's pending-save wait.
    await act(async () => {
      settleChain();
      await tracked;
    });
    await waitFor(() => {
      expect(truncateChatSessionMock).toHaveBeenCalled();
    });
    const keepSeq = truncateChatSessionMock.mock.calls[0]?.[1];
    expect(keepSeq).toBe(2); // covers turn 1's server seqs — NOT 0
  });
});

