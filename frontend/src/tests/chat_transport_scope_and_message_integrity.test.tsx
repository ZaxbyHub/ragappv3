// frontend/src/tests/chat_transport_scope_and_message_integrity.test.tsx
// Issue #1153 AC9 (arm's-length check C9, round-1 critic revision): a
// DELAYED interrupt arriving after a turn has durably COMPLETED must not
// damage the completed message -- not in the live store, and not after a
// RELOAD of the session. The interrupt vector: the user presses Stop after
// completion has landed, the stream's dispose function fires late, and the
// transport's own late onRetired/onError callbacks (here a
// ChatInterruptedError) fire after that -- every guard that clears at
// completion must hold against all of them.
//
// Legs:
//  (a) the assistant message in the store still contains the FULL completed
//      text after the delayed interrupt;
//  (b) the message's status is not downgraded to "interrupted" (it stays
//      complete/undefined);
//  (c) the persist API was called exactly once -- the completed batch --
//      whose assistant row was persisted with status "complete" and the
//      FULL canonical content; no call anywhere carries an "interrupted"
//      row;
//  (d) RELOAD leg (critic round 1): after every late writer has fired, the
//      session is reloaded through the same path ChatShell uses (mocked
//      getChatSession returning exactly the rows the batch-persist mock
//      received/returned -- a stateful in-test server -- mapped through the
//      REAL mapSessionMessage into the REAL loadChat). The reloaded
//      transcript must still show the FULL completed content with no
//      interrupted downgrade on any row.
//
// Persist mock realism (critic round 1): the batch mock is a STATEFUL fake
// that returns realistic rows (ids, timestamps, seqs, status, and the
// canonical content it received) and records them as the server's durable
// state -- the reload leg serves exactly those rows.
//
// CLASS: if every leg is GREEN at base the check is PRESERVING (the
// completion guards already reject the delayed interrupt and the reload
// shows the completed turn; the server-side completion fence of plan F7b is
// then the guardrail, not the fix). If ANY leg is RED at base (a late writer
// DOES downgrade), the check is DISCRIMINATING with that leg named. The
// base verdict is reported from the actual run; no assertion is weakened to
// keep a leg green.
//
// Harness: the REAL useSendMessage hook into the REAL chat store with the
// api barrel mocked at the module boundary (the issue #553 pattern). The
// chatStream mock hands the test the live callbacks.

import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useSendMessage, __resetChatInputCapForTests } from "@/hooks/useSendMessage";
import { useChatStore } from "@/stores/useChatStore";
import { useChatModeStore } from "@/stores/useChatModeStore";
import { useChatShellStore } from "@/stores/useChatShellStore";
import { useLlmHealthStore } from "@/stores/useLlmHealthStore";
import { getChatSession } from "@/lib/api";
import { mapSessionMessage } from "@/lib/chatMessageMapper";

const apiMocks = vi.hoisted(() => ({
  getChatSession: vi.fn(),
  createChatSession: vi.fn(),
  addChatMessagesBatch: vi.fn(),
  addChatMessagesBatchKeepalive: vi.fn(),
  chatStream: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  getSettings: async () => ({ chat_message_max_chars: 20000 }),
  getChatSession: (...args: unknown[]) => apiMocks.getChatSession(...(args as [])),
  createChatSession: (...args: unknown[]) => apiMocks.createChatSession(...(args as [])),
  addChatMessagesBatch: (...args: unknown[]) =>
    apiMocks.addChatMessagesBatch(...(args as [])),
  addChatMessagesBatchKeepalive: (...args: unknown[]) =>
    apiMocks.addChatMessagesBatchKeepalive(...(args as [])),
  chatStream: (...args: unknown[]) => apiMocks.chatStream(...(args as [])),
}));

interface StreamHandlers {
  onMessage: (chunk: string) => void;
  onComplete: () => void;
  onError?: (error: Error) => void;
  onRetired?: () => void;
}

interface TurnRow {
  role: string;
  content?: string;
  status?: string;
  turn_id?: string;
}

/** Server-row shape the stateful batch fake stores and the reload serves. */
interface ServerRow {
  id: number;
  role: string;
  content: string;
  status?: string;
  created_at: string;
  seq: number;
  sources: null;
}

const FULL_ANSWER = "Full completed answer.";

/** The in-test server's durable rows (reset per test). */
const server: { rows: ServerRow[]; nextId: number } = { rows: [], nextId: 100 };

let streamHandlers: StreamHandlers | null = null;
let disposeStream: ReturnType<typeof vi.fn> | null = null;

function findAssistant(): { id: string; content: string; status?: string; error?: string } | undefined {
  const state = useChatStore.getState();
  for (const id of state.messageIds) {
    const message = state.messagesById[id];
    if (message?.role === "assistant") return message;
  }
  return undefined;
}

describe("delayed interrupt after durable completion (issue #1153 AC9)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    streamHandlers = null;
    disposeStream = null;
    server.rows = [];
    server.nextId = 100;
    __resetChatInputCapForTests();
    act(() => {
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
    });
    useLlmHealthStore.setState({ thinking: true, instant: true });
    useChatShellStore.setState({ sessionListRefreshToken: 0 });
    useChatModeStore.setState({ scopeDocumentIds: null });

    // STATEFUL batch-persist fake: returns realistic rows (ids, timestamps,
    // seqs, status, the canonical content it received) and records them as
    // the server's durable state for the reload leg.
    apiMocks.addChatMessagesBatch.mockImplementation(
      async (_sessionId: number, messages: TurnRow[]) => {
        const saved: ServerRow[] = messages.map((message) => {
          const row: ServerRow = {
            id: server.nextId++,
            role: message.role,
            content: message.content ?? "",
            status: message.status,
            created_at: "2026-11-01T00:00:05Z",
            seq: server.rows.length + 1,
            sources: null,
          };
          server.rows.push(row);
          return row;
        });
        return saved;
      },
    );
    apiMocks.chatStream.mockImplementation(
      (_messages: unknown, handlers: StreamHandlers) => {
        streamHandlers = handlers;
        disposeStream = vi.fn();
        return disposeStream;
      },
    );
  });

  afterEach(() => {
    cleanup();
  });

  it("keeps the completed turn intact when the interrupt fires after durable completion, across a reload", async () => {
    act(() => {
      useChatStore.setState({ activeChatId: "42", activeSessionVaultId: 7, input: "question" });
    });

    const { result } = renderHook(() => useSendMessage(7, vi.fn()));
    await act(async () => {
      await result.current.handleSend();
    });

    // Stream the answer, then COMPLETE the turn (durable persist path runs
    // and resolves: the batch fake returns server rows).
    await act(async () => {
      streamHandlers!.onMessage("Full ");
    });
    await act(async () => {
      streamHandlers!.onMessage("completed answer.");
    });
    await act(async () => {
      await streamHandlers!.onComplete();
    });
    await waitForCondition(() => apiMocks.addChatMessagesBatch.mock.calls.length === 1);

    // Post-completion snapshot: full text is on the row, nothing streams.
    const completed = findAssistant();
    expect(completed).toBeDefined();
    expect(completed!.content).toBe(FULL_ANSWER);
    expect(useChatStore.getState().isStreaming).toBe(false);
    expect(useChatStore.getState().streamingMessageId).toBeNull();

    // THE DELAYED INTERRUPT: Stop pressed after completion, the stream's
    // dispose invoked late, then the transport's own late onRetired and a
    // ChatInterruptedError onError firing after that.
    act(() => {
      result.current.handleStop();
    });
    act(() => {
      disposeStream?.();
    });
    await act(async () => {
      streamHandlers!.onRetired?.();
    });
    await act(async () => {
      const interrupted = new Error("The response stream ended before the answer finished.");
      interrupted.name = "ChatInterruptedError";
      await streamHandlers!.onError?.(interrupted);
    });

    // (a) The completed message still carries the FULL completed text.
    const after = findAssistant();
    expect(after).toBeDefined();
    expect(after!.content).toBe(FULL_ANSWER);

    // (b) The status was not downgraded to interrupted.
    expect(after!.status).not.toBe("interrupted");
    expect(after!.status === undefined || after!.status === "complete").toBe(true);
    expect(after!.error).toBeUndefined();

    // (c) The persist API was called exactly once -- the completed batch --
    // never with an interrupted assistant row, and the persisted assistant
    // payload carries the FULL canonical content.
    expect(apiMocks.addChatMessagesBatch).toHaveBeenCalledTimes(1);
    const persistedRows = apiMocks.addChatMessagesBatch.mock.calls[0][1] as TurnRow[];
    const assistantRow = persistedRows.find((row) => row.role === "assistant");
    expect(assistantRow).toBeDefined();
    expect(assistantRow!.status).toBe("complete");
    expect(assistantRow!.content).toBe(FULL_ANSWER);
    const interruptedRows = apiMocks.addChatMessagesBatch.mock.calls.flatMap(
      ([, rows]) => (rows as TurnRow[]).filter((row) => row.status === "interrupted"),
    );
    expect(interruptedRows).toHaveLength(0);
    expect(apiMocks.addChatMessagesBatchKeepalive).not.toHaveBeenCalled();

    // Persist-mock realism: the server's durable state (exactly what the
    // batch fake returned) holds realistic rows -- ids, seqs, timestamps,
    // status, and the full canonical content for the assistant row.
    expect(server.rows).toHaveLength(2);
    const serverAssistant = server.rows.find((row) => row.role === "assistant");
    expect(serverAssistant).toBeDefined();
    expect(serverAssistant!.id).toBeGreaterThan(0);
    expect(serverAssistant!.seq).toBeGreaterThan(0);
    expect(serverAssistant!.created_at).toBeTruthy();
    expect(serverAssistant!.status).toBe("complete");
    expect(serverAssistant!.content).toBe(FULL_ANSWER);

    // The UI is not wedged by the late callbacks.
    expect(useChatStore.getState().isStreaming).toBe(false);
    expect(useChatStore.getState().streamingMessageId).toBeNull();

    // (d) RELOAD leg: after every late writer, reload the session through
    // the ChatShell load path -- getChatSession serves exactly the durable
    // rows the batch fake recorded, mapped through the REAL
    // mapSessionMessage into the REAL loadChat.
    apiMocks.getChatSession.mockResolvedValue({
      id: 42,
      vault_id: 7,
      title: "Completed turn",
      created_at: "2026-11-01T00:00:00Z",
      updated_at: "2026-11-01T00:00:05Z",
      messages: server.rows.map((row) => ({ ...row })),
    });
    await act(async () => {
      const detail = await getChatSession(42);
      useChatStore
        .getState()
        .loadChat("42", (detail.messages ?? []).map(mapSessionMessage));
    });

    const reloadedState = useChatStore.getState();
    expect(reloadedState.activeChatId).toBe("42");
    const reloadedAssistant = findAssistant();
    expect(reloadedAssistant).toBeDefined();
    // The reloaded transcript still shows the FULL completed content...
    expect(reloadedAssistant!.content).toBe(FULL_ANSWER);
    // ...with the completed status and no downgrade anywhere.
    expect(reloadedAssistant!.status).toBe("complete");
    expect(reloadedAssistant!.error).toBeUndefined();
    const interruptedReloaded = reloadedState.messageIds
      .map((id) => reloadedState.messagesById[id])
      .filter((message) => message?.status === "interrupted");
    expect(interruptedReloaded).toHaveLength(0);
  });
});

/** Bounded poll helper (waitFor is component-level; this is store-level). */
async function waitForCondition(condition: () => boolean, timeoutMs = 3000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (!condition()) {
    if (Date.now() > deadline) {
      throw new Error("waitForCondition: condition not met before timeout");
    }
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 10));
    });
  }
}
