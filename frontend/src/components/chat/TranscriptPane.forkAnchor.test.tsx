// frontend/src/components/chat/TranscriptPane.forkAnchor.test.tsx
// Issue #684 (C6): fork must anchor on a DURABLE seq, not the LOCAL
// positional index. Today handleFork sends `ids.indexOf(messageId)` as
// `message_index`; the server slices its OWN ordered rows and 400s when the
// index is past the end — which is exactly what happens when an earlier turn
// exists locally but was never persisted (failed save): the local index
// overcounts the server's rows. The contract: the fixed client sends
// `through_seq` — the highest durable seq among rows up to and including the
// clicked message — and the server copies the rows with `seq <= through_seq`.
//
// Harness: the REAL useSendMessage hook into the REAL chat store (the
// TranscriptPane.liveSeqRevision.test.tsx pattern) with a STATEFUL fake of
// the sessions API. The fork fake implements BOTH contracts honestly:
//   * legacy `message_index` (number) → slice ordered rows [0..index]; an
//     index past the end rejects with an axios-shaped 400 (exactly what
//     backend chat.py's fork_session does);
//   * durable anchor `through_seq` ({ through_seq }) → copy exactly the rows
//     with `seq <= through_seq`.
// Today's client calls forkChatSession(sessionId, messageIndex) — a plain
// number — so the fake's second parameter accepts
// `number | { through_seq?: number }`: the current numeric call exercises the
// legacy branch, the fixed anchor payload exercises the durable branch, and
// neither behavior is invented (both mirror the real server semantics).
// `lastForkRowCount` is set ONLY on a successful fork, to the number of
// copied rows.

import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, render, renderHook, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { TranscriptPane } from "./TranscriptPane";
import { useSendMessage } from "@/hooks/useSendMessage";
import { useChatStore } from "@/stores/useChatStore";
import { useChatModeStore } from "@/stores/useChatModeStore";
import { useChatShellStore } from "@/stores/useChatShellStore";
import { useLlmHealthStore } from "@/stores/useLlmHealthStore";
import { mapSessionMessage } from "@/lib/chatMessageMapper";
import type { AddMessageRequest, ChatSessionMessage } from "@/lib/api";

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

const apiMocks = vi.hoisted(() => ({
  createChatSession: vi.fn(),
  chatStream: vi.fn(),
}));
const truncateChatSessionMock = vi.hoisted(() => vi.fn());
const addChatMessagesBatchMock = vi.hoisted(() => vi.fn());
const forkChatSessionMock = vi.hoisted(() => vi.fn());
const mockRefreshHistory = vi.hoisted(() => vi.fn());
const mockNavigate = vi.hoisted(() => vi.fn());
const mockGetActiveVault = vi.hoisted(() => vi.fn());

vi.mock("@/lib/api", () => ({
  createChatSession: (...args: unknown[]) => apiMocks.createChatSession(...args),
  addChatMessagesBatch: (...args: unknown[]) => addChatMessagesBatchMock(...args),
  addChatMessagesBatchKeepalive: vi.fn(),
  chatStream: (...args: unknown[]) => apiMocks.chatStream(...args),
  truncateChatSession: truncateChatSessionMock,
  forkChatSession: (...args: unknown[]) => forkChatSessionMock(...args),
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
        <button
          type="button"
          aria-label={`Edit ${message.id}`}
          onClick={() => onEdit(message.id, message.content)}
        >
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
      <div data-testid="motion-div" {...props}>
        {children}
      </div>
    ),
  },
  AnimatePresence: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  useReducedMotion: () => false,
}));

import { useVaultStore } from "@/stores/useVaultStore";

/** Server-ordered row shape the in-test fake stores. */
type ServerRow = {
  id: number;
  role: "user" | "assistant";
  content: string;
  created_at: string;
  seq: number;
  sources: null;
};

const seedRow = (
  id: number,
  role: "user" | "assistant",
  content: string,
  seq: number,
): ServerRow => ({
  id,
  role,
  content,
  created_at: `2026-05-12T00:00:0${seq}:00Z`,
  seq,
  sources: null,
});

/** The STATEFUL fake of the server (reset per test). */
const server = vi.hoisted(() => ({
  rows: [] as Array<{
    id: number;
    role: "user" | "assistant";
    content: string;
    created_at: string;
    seq: number;
    sources: null;
  }>,
  nextId: 100,
  /** Rows copied by the LAST SUCCESSFUL fork (0 until one succeeds). */
  lastForkRowCount: 0,
  forkAttempts: 0,
}));

describe("TranscriptPane fork durable anchor (issue #684)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockRefreshHistory.mockResolvedValue(undefined);

    // Reset the REAL chat store between tests.
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

    apiMocks.createChatSession.mockResolvedValue({ id: 42 });
    truncateChatSessionMock.mockResolvedValue({ remaining_count: 0, tail_seq: 0 });

    // A successful batch save APPENDS the batch rows (assigning the next
    // seqs), echoing contract-accurate saved rows back.
    addChatMessagesBatchMock.mockImplementation(
      (_sessionId: number, messages: AddMessageRequest[]) => {
        const saved: ChatSessionMessage[] = [];
        for (const message of messages) {
          const seq = (server.rows[server.rows.length - 1]?.seq ?? 0) + 1;
          const row = seedRow(
            server.nextId++,
            message.role as "user" | "assistant",
            message.content,
            seq,
          );
          server.rows.push(row);
          saved.push({ ...row });
        }
        return Promise.resolve(saved);
      },
    );

    // Dual-contract fork fake (see the file header): legacy numeric
    // message_index slices ordered rows and 400s past the end; a durable
    // { through_seq } anchor copies exactly the rows with seq <= through_seq.
    forkChatSessionMock.mockImplementation(
      (
        sessionId: number,
        messageIndexOrAnchor: number | { through_seq?: number },
      ) => {
        server.forkAttempts += 1;
        let copied: ServerRow[];
        let legacyIndex: number | null = null;
        if (typeof messageIndexOrAnchor === "number") {
          legacyIndex = messageIndexOrAnchor;
          if (messageIndexOrAnchor >= server.rows.length) {
            return Promise.reject({ response: { status: 400 } });
          }
          copied = server.rows.slice(0, messageIndexOrAnchor + 1);
        } else {
          copied = server.rows.filter(
            (r) => r.seq <= (messageIndexOrAnchor.through_seq ?? 0),
          );
        }
        server.lastForkRowCount = copied.length;
        const forkId = 500 + server.forkAttempts;
        return Promise.resolve({
          id: forkId,
          vault_id: 1,
          title: "Branch of Original",
          created_at: "2026-05-12T00:00:00Z",
          updated_at: "2026-05-12T00:00:00Z",
          forked_from_session_id: sessionId,
          fork_message_index: legacyIndex,
          messages: copied.map((r, i) => ({ ...r, id: 600 + i })),
        });
      },
    );

    // Successful stream shape for the live sends (liveSeqRevision pattern).
    apiMocks.chatStream.mockImplementation(
      (
        _messages: unknown,
        handlers: {
          onMessage: (chunk: string) => void;
          onComplete: () => Promise<void>;
        },
      ) => {
        handlers.onMessage("hello");
        void handlers.onComplete();
        return vi.fn();
      },
    );

    // Mock useVaultStore with selector support (mirrors the revision suite).
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

  it("fork after an unpersisted turn copies the right server rows", async () => {
    // Turn 1: durable on the server, loaded into the store through the real
    // mapper (store rows carry seq 1/2).
    server.rows = [seedRow(1, "user", "question-1", 1), seedRow(2, "assistant", "answer-1", 2)];
    useChatStore.getState().loadChat("42", server.rows.map(mapSessionMessage));

    const { result } = renderHook(() => useSendMessage(7, mockRefreshHistory));

    // Turn 2: sent live, its durable save REJECTS — the turn exists locally
    // only and the server rows are unchanged.
    addChatMessagesBatchMock.mockRejectedValueOnce(new Error("save failed"));
    useChatStore.setState({ input: "question-2" });
    await act(async () => {
      await result.current.handleSend();
    });
    await waitFor(() => expect(addChatMessagesBatchMock).toHaveBeenCalledTimes(1));
    const turn2AssistantId = useChatStore.getState().messageIds[3];
    await waitFor(() => {
      expect(useChatStore.getState().messagesById[turn2AssistantId].saveState).toBe("failed");
    });
    expect(server.rows.length).toBe(2);

    // Turn 3: sent live and SAVED — server rows are now seq 1..4 and the
    // store holds 6 local messages (turn 2's included).
    await waitFor(() => {
      expect(useChatStore.getState().pendingTurnPersist).toBeNull();
    });
    useChatStore.setState({ input: "question-3" });
    await act(async () => {
      await result.current.handleSend();
    });
    await waitFor(() => expect(addChatMessagesBatchMock).toHaveBeenCalledTimes(2));
    await waitFor(() => {
      expect(useChatStore.getState().pendingTurnPersist).toBeNull();
    });
    expect(server.rows.map((r) => r.seq)).toEqual([1, 2, 3, 4]);
    expect(useChatStore.getState().messageIds).toHaveLength(6);

    render(<TranscriptPane />);

    // Fork at turn 3's USER message (migrated to its server id 100, seq 3).
    // The durable anchor through that message is seq 3 → the fork must copy
    // the 3 server rows with seq <= 3. The legacy LOCAL index would be 4
    // against only 4 server rows — past the end — which 400s.
    await act(async () => {
      await userEvent.click(screen.getByLabelText("Fork 100"));
    });
    await waitFor(() => expect(server.forkAttempts).toBeGreaterThanOrEqual(1));
    await act(async () => {
      await Promise.resolve();
    });

    expect(server.lastForkRowCount).toBe(3);
  });
});
