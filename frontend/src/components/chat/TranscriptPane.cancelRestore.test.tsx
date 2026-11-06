// frontend/src/components/chat/TranscriptPane.cancelRestore.test.tsx
// Issue #991 review PRR-003(b) — the post-truncate CANCEL branch regression
// legs (delta reviewer round 3: the branch shipped without executed evidence;
// the retryDurability probe restores through sendCore's onError →
// restoreRevisedTurn, a different function).
//
// Two interleavings, both through the REAL TranscriptPane + REAL
// useSendMessage + REAL chat store (the retryDurability harness pattern):
//
// - Leg A (streamingMessageId arm): a composer submit whose guard read
//   PREDATES the revision's raised streaming flag starts its sendCore during
//   the truncate window. The revision must cancel, re-append the original
//   turn SERVER-SIDE, leave the local transcript untouched, and hand the
//   streaming flag to the live foreign send (whose terminal callbacks lower
//   it).
// - Leg B (identity arm with NO live send): a second Retry started while a
//   prior failed retry's restoreRevisedTurn is mid-restore — its local row
//   appends mutate messageIds during the second revision's truncate window.
//   The cancel branch fires with no sendCore in flight; before the
//   live-owner finally gate this left isStreaming raised forever (composer
//   dead until reload). The revision must lower the flag itself.
//
// Harness: the REAL useSendMessage hook into the REAL chat store with a
// STATEFUL in-test fake of the sessions API, plus controllable gates for the
// admission fetch, the truncate await, and the restore's batch save.

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
import { parseSSEStream } from "@/lib/api/sessions";
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

// Leg A parks BOTH admissions (submit + revision) on this gate; the submit
// attached first resumes first. Re-armed per test.
const capGate = vi.hoisted(() => ({ current: Promise.resolve() as Promise<void> }));
// Leg B parks the FIRST batch save (the prior retry's restore) so its local
// mirror steps land mid-window. Re-armed per test.
const batchHold = vi.hoisted(() => ({
  hold: false,
  release: null as null | ((value: ChatSessionMessage[]) => void),
}));

vi.mock("@/lib/api", () => ({
  // Leg A's admission gate: both callers park until the test releases.
  getSettings: async () => {
    await capGate.current;
    return { chat_message_max_chars: 20000 };
  },
  createChatSession: (...args: unknown[]) => apiMocks.createChatSession(...args),
  addChatMessagesBatch: (...args: unknown[]) => addChatMessagesBatchMock(...args),
  addChatMessagesBatchKeepalive: vi.fn(),
  chatStream: (...args: unknown[]) => apiMocks.chatStream(...args),
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
        <button
          type="button"
          aria-label={`Edit ${message.id}`}
          onClick={() => onEdit(message.id, message.content)}
        >
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
      <div data-testid="motion-div" {...props}>
        {children}
      </div>
    ),
  },
  AnimatePresence: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  useReducedMotion: () => false,
}));

import { useVaultStore } from "@/stores/useVaultStore";
import { toast } from "sonner";

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

/** The STATEFUL fake of the server's session rows (reset per test). */
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

describe("TranscriptPane cancel-restore (issue #991 review PRR-003(b))", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockRefreshHistory.mockResolvedValue(undefined);

    // Re-arm the controllable gates.
    capGate.current = Promise.resolve();
    batchHold.hold = false;
    batchHold.release = null;

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
    forkChatSessionMock.mockResolvedValue({ id: 20, messages: [] });

    // STATEFUL sessions-API fake: truncate deletes `seq > keepSeq`...
    truncateChatSessionMock.mockImplementation(
      (_sessionId: number, keepSeq: number) => {
        server.rows = server.rows.filter((r) => r.seq <= keepSeq);
        const tail = server.rows.length > 0 ? server.rows[server.rows.length - 1].seq : 0;
        return Promise.resolve({ remaining_count: server.rows.length, tail_seq: tail });
      },
    );
    // ...and a successful batch save APPENDS the batch rows (assigning the
    // next seqs), echoing contract-accurate saved rows back. When batchHold
    // is armed, the rows land server-side but the PROMISE parks until the
    // test releases it — the parked caller's continuation (the restore's
    // local mirror steps) runs only on release.
    addChatMessagesBatchMock.mockImplementation(
      (_sessionId: number, messages: AddMessageRequest[]) => {
        const saved: ChatSessionMessage[] = [];
        for (const message of messages) {
          const seq = (server.rows[server.rows.length - 1]?.seq ?? 0) + 1;
          const row = seedRow(server.nextId++, message.role as "user" | "assistant", message.content, seq);
          server.rows.push(row);
          saved.push({ ...row });
        }
        if (batchHold.hold) {
          return new Promise<ChatSessionMessage[]>((resolve) => {
            batchHold.release = (value: ChatSessionMessage[]) => {
              batchHold.hold = false;
              resolve(value);
            };
          });
        }
        return Promise.resolve(saved);
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

  it("Leg A: a submit predating the raised flag cancels the revision, re-appends server-side, leaves the transcript intact", async () => {
    // Two durable turns on the server (seq 1..4); the revision targets the
    // second turn.
    server.rows = [
      seedRow(1, "user", "question-1", 1),
      seedRow(2, "assistant", "answer-1", 2),
      seedRow(3, "user", "question-2", 3),
      seedRow(4, "assistant", "answer-2", 4),
    ];
    useChatStore.getState().loadChat("42", server.rows.map(mapSessionMessage));

    // Foreign stream: starts, feeds one token, then stays LIVE (no terminal
    // callback) so streamingMessageId is non-null when the revision's
    // post-truncate re-read runs. The handlers are captured so the test can
    // settle the stream later and observe the owner handoff of the flag.
    let foreignHandlers: Parameters<typeof parseSSEStream>[1] | null = null;
    apiMocks.chatStream.mockImplementation(
      async (
        _messages: unknown,
        handlers: Parameters<typeof parseSSEStream>[1],
      ) => {
        foreignHandlers = handlers;
        handlers.onMessage("fore");
        return vi.fn();
      },
    );

    // Arm the admission gate: BOTH the foreign submit and the revision park
    // here; the submit attached first resumes first.
    let releaseCap!: () => void;
    capGate.current = new Promise<void>((resolve) => {
      releaseCap = resolve;
    });

    // Foreign submit: its guard read happens NOW (isStreaming false, flag
    // not yet raised); the submit then parks at the admission gate. The pane
    // renders FIRST and its mount effects settle — the Composer's draft
    // restore runs on mount and would otherwise overwrite the input set
    // before mount.
    const hook = renderHook(() => useSendMessage(7, mockRefreshHistory));
    render(<TranscriptPane />);
    await act(async () => {
      await new Promise((r) => setTimeout(r, 0));
    });
    useChatStore.setState({ input: "foreign question" });
    let foreignP: Promise<unknown> | null = null;
    await act(async () => {
      foreignP = hook.result.current.handleSend();
    });
    await act(async () => {
      await new Promise((r) => setTimeout(r, 0));
    });

    // Revision click: its guard ALSO reads false (the foreign sendCore has
    // not started — it is parked at the gate).
    await act(async () => {
      await userEvent.click(screen.getByLabelText("Retry 4"));
    });

    // Release the gate: the foreign resumes FIRST (attached its await
    // first) → sendCore starts synchronously (streamingMessageId set, rows
    // appended); the revision's continuation then runs the truncate and
    // hits the post-truncate re-read.
    await act(async () => {
      releaseCap();
      await new Promise((r) => setTimeout(r, 0));
    });
    await waitFor(() => {
      expect(useChatStore.getState().streamingMessageId).not.toBeNull();
    });

    // Resolve the truncate: the post-truncate re-read detects the foreign
    // sendCore and cancels; the cancel re-appends the original turn
    // server-side. The truncate fake is not gated here — it resolves
    // immediately, so the cancel lands on its own.
    await waitFor(() => {
      expect(addChatMessagesBatchMock).toHaveBeenCalledTimes(1);
    });

    // The re-append payload is the ORIGINAL turn: the revision's snapshot
    // predates the foreign rows.
    expect(addChatMessagesBatchMock.mock.calls[0][0]).toBe(42);
    const cancelPayload = addChatMessagesBatchMock.mock.calls[0][1] as AddMessageRequest[];
    expect(cancelPayload.map((m) => m.content)).toEqual(["question-2", "answer-2"]);

    // The local transcript is untouched: the removal never ran, and the
    // foreign send's pair is present at the tail.
    const local = useChatStore.getState();
    expect(local.messageIds).toHaveLength(6);
    expect(local.messagesById["4"]?.content).toBe("answer-2");
    const foreignUserRow = local.messageIds[local.messageIds.length - 2];
    expect(local.messagesById[foreignUserRow]?.content).toBe("foreign question");

    // The foreign send owns the flag while it is live...
    expect(useChatStore.getState().isStreaming).toBe(true);
    // ...and its terminal callbacks lower it — the owner handoff leaves no
    // stuck flag.
    await act(async () => {
      foreignHandlers?.onComplete?.();
      await new Promise((r) => setTimeout(r, 0));
    });
    await waitFor(() => {
      expect(useChatStore.getState().isStreaming).toBe(false);
    });
    expect(apiMocks.chatStream).toHaveBeenCalledTimes(1);
    void foreignP;
  });

  it("Leg B: a second Retry during a prior retry's restore cancels and STILL lowers the streaming flag (no live send)", async () => {
    // Two durable turns (seq 1..4); the replacement stream dies at the
    // admission gate before any content (real parseSSEStream error frame),
    // exactly the retryDurability scenario.
    server.rows = [
      seedRow(1, "user", "question-1", 1),
      seedRow(2, "assistant", "answer-1", 2),
      seedRow(3, "user", "question-2", 3),
      seedRow(4, "assistant", "answer-2", 4),
    ];
    useChatStore.getState().loadChat("42", server.rows.map(mapSessionMessage));

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

    renderHook(() => useSendMessage(7, mockRefreshHistory));
    render(<TranscriptPane />);

    // Retry #1: dispatch → ADMISSION_REJECTED → the terminal callbacks clear
    // the flag (empty content keeps the failed turn unpersisted) → the
    // restore's batch save PARKS (batchHold armed before the click).
    batchHold.hold = true;
    await act(async () => {
      await userEvent.click(screen.getByLabelText("Retry 4"));
    });
    await waitFor(() => {
      expect(addChatMessagesBatchMock).toHaveBeenCalledTimes(1);
    });
    // Terminal cleanup has run: the pane is non-streaming, so the entry
    // guard will admit a second retry.
    expect(useChatStore.getState().isStreaming).toBe(false);

    // Retry #2 starts while the restore is parked: raise flag → snapshot
    // (still WITHOUT the restored rows) → admission (memoized) → truncate
    // — gated here so the restore's mirror lands mid-window.
    let releaseTruncate2!: () => void;
    const truncate2 = new Promise<{ remaining_count: number; tail_seq: number }>((resolve) => {
      releaseTruncate2 = () =>
        resolve({
          remaining_count: server.rows.filter((r) => r.seq <= 2).length,
          tail_seq: 2,
        });
    });
    // Retry #1's truncate already consumed the default impl (it fired before
    // the parked restore), so a Once impl set NOW parks only retry #2.
    truncateChatSessionMock.mockImplementationOnce(() => truncate2);
    await act(async () => {
      const retryButtons = screen.getAllByLabelText(/^Retry /);
      await userEvent.click(retryButtons[retryButtons.length - 1]);
    });
    await waitFor(() => {
      expect(truncateChatSessionMock).toHaveBeenCalledTimes(2);
    });

    // Release the parked restore: its removeMessagesFrom + addMessage pair
    // mutates messageIds during the second revision's truncate window.
    await act(async () => {
      batchHold.release?.(server.rows.slice(-2) as ChatSessionMessage[]);
      await new Promise((r) => setTimeout(r, 0));
    });
    await waitFor(() => {
      expect(useChatStore.getState().messageIds).toHaveLength(4);
    });

    // Resolve the second truncate: the identity compare fires (snapshot had
    // the failed pair; the transcript now holds the restored rows) and the
    // cancel branch runs — with NO sendCore in flight.
    await act(async () => {
      releaseTruncate2();
      await new Promise((r) => setTimeout(r, 0));
    });

    // Cancel evidence: the toast fired and the cancel re-appended the
    // revision's snapshot rows server-side (batch call #2).
    await waitFor(() => {
      expect(toast.error).toHaveBeenCalledWith(
        "Retry cancelled — a newer message was sent. Your original turn was kept.",
      );
    });
    expect(addChatMessagesBatchMock).toHaveBeenCalledTimes(2);
    expect(addChatMessagesBatchMock.mock.calls[1][0]).toBe(42);
    const cancelPayload = addChatMessagesBatchMock.mock.calls[1][1] as AddMessageRequest[];
    expect(cancelPayload.map((m) => m.content)).toEqual(["question-2", ""]);

    // THE FIX under test: with no live send to own the flag, this revision
    // lowers it — at the pre-fix tree `foreignSendAdmitted` skipped the
    // restore and isStreaming stayed raised forever.
    await waitFor(() => {
      expect(useChatStore.getState().isStreaming).toBe(false);
    });
    // The second retry never dispatched a replacement stream.
    expect(apiMocks.chatStream).toHaveBeenCalledTimes(1);
  });
});
