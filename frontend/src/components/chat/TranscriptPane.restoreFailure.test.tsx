// frontend/src/components/chat/TranscriptPane.restoreFailure.test.tsx
// Issue #684 review F-006: the restore-failure branch of the rejected-retry
// path (disclosed residual 2 in the release fragment) had no shipped test.
// When the replacement stream is admission-rejected before any content AND
// the restore's own batch save fails (network death mid-retry), the catch in
// restoreRevisedTurn must surface the error toast — the original Q&A stays
// absent server-side (truncate already committed), which is the disclosed
// bound of the fix.
//
// Harness: the TranscriptPane.retryDurability.test.tsx real-useSendMessage
// pattern (stateful sessions-API fake + real parseSSEStream driven by the
// chatStream mock), with addChatMessagesBatch rejecting exactly once so the
// restore's catch executes while persistTurn's own early-return (empty
// assistant content) never reaches the batch.

import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, renderHook, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { toast } from "sonner";
import { TranscriptPane } from "./TranscriptPane";
import { useSendMessage } from "@/hooks/useSendMessage";
import { useChatStore } from "@/stores/useChatStore";
import { useChatModeStore } from "@/stores/useChatModeStore";
import { useChatShellStore } from "@/stores/useChatShellStore";
import { useLlmHealthStore } from "@/stores/useLlmHealthStore";
import { mapSessionMessage } from "@/lib/chatMessageMapper";
import { parseSSEStream } from "@/lib/api/sessions";
import type { AddMessageRequest, ChatSessionMessage } from "@/lib/api";

class MockResizeObserver {
  observe = vi.fn();
  unobserve = vi.fn();
  disconnect = vi.fn();
}
global.ResizeObserver = MockResizeObserver as unknown as typeof ResizeObserver;
Element.prototype.scrollIntoView = vi.fn();
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
  forkChatSession: forkChatSessionMock,
  getChatSession: vi.fn(),
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
  MessageBubble: ({ message }: { message: { id: string; role: string; content: string } }) => (
    <div data-testid="message-bubble" data-message-id={message.id}>
      {message.content}
    </div>
  ),
}));
vi.mock("./AssistantMessage", () => ({
  AssistantMessage: ({ message, onRetry }: { message: { id: string; role: string; content: string }; onRetry?: () => void }) => (
    <div data-testid="message-bubble" data-message-id={message.id}>
      {message.content}
      {onRetry && (
        <button type="button" aria-label={`Retry ${message.id}`} onClick={onRetry}>
          Retry
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
}));

describe("TranscriptPane restore failure (issue #684 review F-006)", () => {
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
      pendingTurnPersists: new Set(),
      messageEditVersions: {},
      activeEditVersion: {},
    });
    useLlmHealthStore.setState({ thinking: true, instant: true });
    useChatShellStore.setState({ sessionListRefreshToken: 0 });
    useChatModeStore.setState({ scopeDocumentIds: null });

    apiMocks.createChatSession.mockResolvedValue({ id: 42 });
    forkChatSessionMock.mockResolvedValue({ id: 20, messages: [] });

    truncateChatSessionMock.mockImplementation(
      (_sessionId: number, keepSeq: number) => {
        server.rows = server.rows.filter((r) => r.seq <= keepSeq);
        const tail = server.rows.length > 0 ? server.rows[server.rows.length - 1].seq : 0;
        return Promise.resolve({ remaining_count: server.rows.length, tail_seq: tail });
      },
    );
    // Default: successful append. The test overrides with a rejection.
    addChatMessagesBatchMock.mockImplementation(
      (_sessionId: number, messages: AddMessageRequest[]) => {
        const saved: ChatSessionMessage[] = [];
        for (const message of messages) {
          const seq = (server.rows[server.rows.length - 1]?.seq ?? 0) + 1;
          const row = seedRow(server.nextId++, message.role as "user" | "assistant", message.content, seq);
          server.rows.push(row);
          saved.push({ ...row });
        }
        return Promise.resolve(saved);
      },
    );

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

  it("toasts and keeps the truncated state when the restore batch save fails", async () => {
    // Two durable turns on the server (seq 1..4), loaded through the real
    // mapper so store rows carry those seqs.
    server.rows = [
      seedRow(1, "user", "question-1", 1),
      seedRow(2, "assistant", "answer-1", 2),
      seedRow(3, "user", "question-2", 3),
      seedRow(4, "assistant", "answer-2", 4),
    ];
    useChatStore.getState().loadChat("42", server.rows.map(mapSessionMessage));

    // Replacement stream rejected at the admission gate before any content —
    // the real parser consumes the frame so the restore gate fires.
    apiMocks.chatStream.mockImplementation(
      async (
        _messages: unknown,
        handlers: Parameters<typeof parseSSEStream>[1],
      ) => {
        const frame =
          "data: " +
          JSON.stringify({
            type: "error",
            message: "Chat capacity is saturated; retry shortly",
            code: "ADMISSION_REJECTED",
          }) +
          "\n\n";
        const reader = new ReadableStream<Uint8Array>({
          start(controller) {
            controller.enqueue(new TextEncoder().encode(frame));
            controller.close();
          },
        }).getReader();
        await parseSSEStream(reader, handlers);
        return vi.fn();
      },
    );

    // The restore's own batch save fails (network death mid-retry): the
    // truncate already committed, so residual 2 applies — the original stays
    // absent and the user sees the error toast.
    addChatMessagesBatchMock.mockRejectedValueOnce(new Error("restore save down"));

    renderHook(() => useSendMessage(7, mockRefreshHistory));
    render(<TranscriptPane />);

    await act(async () => {
      await userEvent.click(screen.getByLabelText("Retry 4"));
    });

    await waitFor(() => {
      expect(toast.error).toHaveBeenCalledWith("Couldn't update conversation history");
    });
    // The truncate committed and the restore failed: exactly the disclosed
    // residual-2 end state (original absent, replacement never started).
    expect(server.rows.map((r) => r.seq)).toEqual([1, 2]);
    // The failure toast must not be doubled by the regenerate-kept toast,
    // which only fires on a successful restore.
    expect(toast.error).not.toHaveBeenCalledWith(
      "Couldn't regenerate — kept your original answer.",
    );
  });
});
