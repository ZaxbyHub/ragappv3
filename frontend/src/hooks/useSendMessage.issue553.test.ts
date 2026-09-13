// frontend/src/hooks/useSendMessage.issue553.test.ts
// Issue #553 client wiring for server-side durable turns: every send opts
// the turn into the server-side pre-write/finalize by passing
// { sessionId, turnId } to chatStream; the canonical mapper passes the new
// "pending" status through untouched.

import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useSendMessage } from "./useSendMessage";
import { useChatStore } from "@/stores/useChatStore";
import { mapSessionMessage } from "@/lib/chatMessageMapper";

const apiMocks = vi.hoisted(() => ({
  createChatSession: vi.fn(),
  addChatMessagesBatch: vi.fn(),
  addChatMessagesBatchKeepalive: vi.fn(),
  chatStream: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  createChatSession: (...args: unknown[]) => apiMocks.createChatSession(...args),
  addChatMessagesBatch: (...args: unknown[]) => apiMocks.addChatMessagesBatch(...args),
  addChatMessagesBatchKeepalive: (...args: unknown[]) =>
    apiMocks.addChatMessagesBatchKeepalive(...args),
  chatStream: (...args: unknown[]) => apiMocks.chatStream(...args),
}));

let activeHandlers: {
  onMessage: (chunk: string) => void;
  onComplete: () => void;
} | null = null;

function installStream(): void {
  apiMocks.chatStream.mockImplementation(
    (
      _messages: unknown,
      handlers: { onMessage: (chunk: string) => void; onComplete: () => void },
    ) => {
      activeHandlers = handlers;
      return vi.fn();
    },
  );
}

async function completeStream(): Promise<void> {
  if (!activeHandlers) throw new Error("chatStream was not started");
  // One content chunk first so the turn has a persistable answer (LIVE-01
  // empty-content guard otherwise skips the batch reconcile).
  await act(async () => {
    activeHandlers!.onMessage("partial answer");
  });
  await act(async () => {
    await activeHandlers!.onComplete();
  });
}

describe("useSendMessage issue #553 server-side durable turn wiring", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    activeHandlers = null;
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
      });
    });
    apiMocks.addChatMessagesBatch.mockImplementation(
      async (_sessionId: number, messages: unknown[]) =>
        messages.map((_, index) => ({
          id: 100 + index,
          created_at: `2026-09-13T00:00:0${index}Z`,
        })),
    );
  });

  afterEach(() => {
    cleanup();
  });

  it("passes the session id and the client turn id on the stream request", async () => {
    await act(async () => {
      useChatStore.setState({ activeChatId: "42", input: "question" });
    });
    installStream();

    const hook = renderHook(() => useSendMessage(7, vi.fn()));
    await act(async () => {
      await hook.result.current.handleSend();
    });
    await completeStream();

    expect(apiMocks.chatStream).toHaveBeenCalledTimes(1);
    const durableArg = apiMocks.chatStream.mock.calls[0].at(-1) as {
      sessionId: number;
      turnId: string;
    };
    expect(durableArg.sessionId).toBe(42);
    expect(typeof durableArg.turnId).toBe("string");
    expect(durableArg.turnId.length).toBeGreaterThan(0);
    // The same turn id is stamped on the local turn rows and on the batch
    // reconcile payload, so client and server agree on the key.
    const store = useChatStore.getState();
    const turnIds = new Set(
      store.messageIds.map((id) => store.messagesById[id]?.turnId),
    );
    expect(turnIds.has(durableArg.turnId)).toBe(true);
    expect(apiMocks.addChatMessagesBatch).toHaveBeenCalledWith(
      42,
      expect.arrayContaining([
        expect.objectContaining({ turn_id: durableArg.turnId }),
      ]),
    );
    // The local temp ids are replaced by the server's durable ids
    // (migrateId/replaceMessageId) once the reconcile resolves — every row
    // in the store carries the mock backend's returned id shape and the
    // saved saveState (PR review PRR-018).
    const savedIds = store.messageIds;
    expect(savedIds).toHaveLength(2);
    for (const id of savedIds) {
      expect(store.messagesById[id]?.saveState).toBe("saved");
      expect(store.messagesById[id]?.created_at).toBeTruthy();
    }
  });

  it("creates a session first and opts the new session's first turn in", async () => {
    apiMocks.createChatSession.mockResolvedValue({ id: 99 });
    installStream();

    const hook = renderHook(() => useSendMessage(7, vi.fn()));
    await act(async () => {
      useChatStore.setState({ input: "first question" });
      await hook.result.current.handleSend();
    });
    await completeStream();

    expect(apiMocks.createChatSession).toHaveBeenCalledWith({ vault_id: 7 });
    const durableArg = apiMocks.chatStream.mock.calls[0].at(-1) as {
      sessionId: number;
      turnId: string;
    };
    expect(durableArg.sessionId).toBe(99);
  });
});

describe("chatMessageMapper issue #553 pending status", () => {
  it("passes the server's pending status through to the store Message", () => {
    const mapped = mapSessionMessage({
      id: 5,
      role: "assistant",
      content: "partial",
      created_at: "2026-09-13T00:00:00Z",
      turn_id: "t-1",
      status: "pending",
      seq: 2,
    });
    expect(mapped.status).toBe("pending");
    expect(mapped.turnId).toBe("t-1");
  });
});
