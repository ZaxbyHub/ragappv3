// frontend/src/hooks/useSendMessage.issue815.test.ts
// Issue #815 acceptance checks (arm's-length, frozen):
//
// The keepalive batch-save wrapper (addChatMessagesBatchKeepalive) hits the
// SAME /chat/sessions/:id/messages/batch endpoint the normal path's
// addChatMessagesBatch uses, so the server answers the same
// { messages: [{ id, created_at, seq }, ...] } durable-order payload
// (#683/#685 contract). Two behaviors are pinned here:
//
// 1. Wrapper level — the keepalive promise must RESOLVE with the parsed
//    save results (RED at base, GREEN after the fix: at base the wrapper
//    returns Promise<void> and never reads the fetch response). The
//    best-effort guards (no CSRF -> no send; oversized body -> no send;
//    transport rejection -> resolve, never throw) are pinned alongside.
// 2. Hook level — when persistTurn's keepalive branch runs (pagehide) and
//    the response settles while the page's store state survived (bfcache
//    restore / canceled unload), the returned durable ids and seqs must be
//    migrated into the chat store rows via the same migrateId forwarding
//    the normal path uses, so the rows count toward durableKeepSeq (the
//    Retry/Edit truncate anchor) — RED at base, GREEN after the fix.

import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// ── module mocks ────────────────────────────────────────────────────────────
// The wrapper describe exercises the REAL @/lib/api/sessions module with only
// its ./core dependency mocked (the sessions.keepalive.test.ts idiom); the
// hook describe exercises the REAL useSendMessage hook with the @/lib/api
// barrel mocked (the useSendMessage.liveSeq/issue552 idiom). The two mocks
// are independent module ids and coexist in this file.

const csrfTokenMock = vi.hoisted(() => vi.fn());
const csrfCookieMock = vi.hoisted(() => vi.fn());
const jwtAccessToken = vi.hoisted(() => ({ value: "jwt-token" as string | null }));

vi.mock("@/lib/api/core", () => ({
  apiClient: { post: vi.fn() },
  API_BASE_URL: "/api",
  get _jwtAccessToken() {
    return jwtAccessToken.value;
  },
  getCsrfCookie: () => csrfCookieMock(),
  getCsrfToken: () => csrfTokenMock(),
}));

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

// Import after mocks: the real wrapper (under the core mock) and the real hook.
import { addChatMessagesBatchKeepalive } from "@/lib/api/sessions";
import { useSendMessage } from "./useSendMessage";
import { useChatStore, type Message } from "@/stores/useChatStore";

const wrapperMessages = [
  { role: "user", content: "question", turn_id: "turn-815" },
  { role: "assistant", content: "partial", turn_id: "turn-815", status: "interrupted" },
];

describe("addChatMessagesBatchKeepalive resolves durable save results (issue #815)", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    csrfTokenMock.mockReturnValue("csrf-token");
    csrfCookieMock.mockReturnValue(null);
    jwtAccessToken.value = "jwt-token";
    fetchMock.mockReset();
    fetchMock.mockResolvedValue({ ok: true });
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("resolves with the parsed batch-save results (RED at base: resolves undefined)", async () => {
    // Contract-accurate body of the batch endpoint: the same payload shape
    // addChatMessagesBatch parses (response.data.messages) — ChatSessionMessage
    // rows carrying the server-issued id/created_at/seq durable order.
    fetchMock.mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        messages: [
          { id: 100, created_at: "2026-10-26T00:00:00Z", seq: 1 },
          { id: 101, created_at: "2026-10-26T00:00:01Z", seq: 2 },
        ],
      }),
    });

    const result = await addChatMessagesBatchKeepalive(42, wrapperMessages);

    expect(result).toEqual([
      { id: 100, created_at: "2026-10-26T00:00:00Z", seq: 1 },
      { id: 101, created_at: "2026-10-26T00:00:01Z", seq: 2 },
    ]);
  });

  it("resolves without sending when no CSRF token is available (best-effort guard)", async () => {
    csrfTokenMock.mockReturnValue(null);
    csrfCookieMock.mockReturnValue(null);

    // Value-agnostic: the guard must settle the promise (never throw during
    // pagehide) while failing closed — no request leaves without credentials.
    await addChatMessagesBatchKeepalive(42, wrapperMessages);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("resolves without sending when the body exceeds the keepalive quota (best-effort guard)", async () => {
    const oversized = [
      { role: "user", content: "x".repeat(70_000), turn_id: "turn-815" },
    ];

    await addChatMessagesBatchKeepalive(42, oversized);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("resolves (never throws) when the keepalive fetch rejects (best-effort guard)", async () => {
    fetchMock.mockRejectedValueOnce(new Error("document is unloading"));

    await addChatMessagesBatchKeepalive(42, wrapperMessages);
  });
});

// ── hook consumption ────────────────────────────────────────────────────────

type StreamHandlers = {
  onMessage: (chunk: string) => void;
  onError: (error: Error) => void;
  onComplete: () => Promise<void> | void;
};

let activeHandlers: StreamHandlers | null = null;

function installAbortableStream(): void {
  apiMocks.chatStream.mockImplementation(
    (_messages: unknown, handlers: StreamHandlers) => {
      activeHandlers = handlers;
      // The real transport consumes AbortError without invoking onError.
      return vi.fn();
    },
  );
}

// The keepalive save answers the batch endpoint's durable order, exactly as
// the server would for the same two-row payload.
const keepaliveSaveResults = [
  { id: 100, created_at: "2026-10-26T00:00:00Z", seq: 1 },
  { id: 101, created_at: "2026-10-26T00:00:01Z", seq: 2 },
];

async function startSend(refreshHistory: ReturnType<typeof vi.fn>) {
  await act(async () => {
    useChatStore.setState({ activeChatId: "42", input: "question" });
  });
  installAbortableStream();

  const hook = renderHook(() => useSendMessage(7, refreshHistory));
  await act(async () => {
    await hook.result.current.handleSend();
  });
  expect(apiMocks.chatStream).toHaveBeenCalledTimes(1);
  return hook;
}

async function emitPartialAnswer(): Promise<void> {
  const handlers = activeHandlers;
  if (!handlers) throw new Error("chatStream was not started");
  await act(async () => {
    handlers.onMessage("partial answer");
  });
}

async function flushPersistence(): Promise<void> {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 10));
  });
}

describe("useSendMessage migrates keepalive save results into store rows (issue #815)", () => {
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

    apiMocks.addChatMessagesBatch.mockResolvedValue(keepaliveSaveResults);
    apiMocks.addChatMessagesBatchKeepalive.mockResolvedValue(keepaliveSaveResults);
  });

  afterEach(() => {
    cleanup();
  });

  it("carries the keepalive-saved durable ids and seqs into the store (RED at base)", async () => {
    const refreshHistory = vi.fn().mockResolvedValue(undefined);
    await startSend(refreshHistory);

    await emitPartialAnswer();
    await act(async () => {
      window.dispatchEvent(new Event("pagehide"));
    });
    await flushPersistence();

    expect(apiMocks.addChatMessagesBatchKeepalive).toHaveBeenCalledTimes(1);

    const state = useChatStore.getState();
    // The store rows must be the SERVER rows now: migrateId forwarded the
    // durable ids onto the turn's user and assistant messages.
    expect(state.messageIds).toEqual(["100", "101"]);
    expect(state.messagesById["100"]).toMatchObject({
      saveState: "saved",
      seq: 1,
    });
    expect(state.messagesById["101"]).toMatchObject({
      saveState: "saved",
      seq: 2,
    });
  });

  it("gives the keepalive-saved rows a numeric seq so durableKeepSeq counts them (RED at base)", async () => {
    const refreshHistory = vi.fn().mockResolvedValue(undefined);
    await startSend(refreshHistory);

    await emitPartialAnswer();
    await act(async () => {
      window.dispatchEvent(new Event("pagehide"));
    });
    await flushPersistence();

    const state = useChatStore.getState();
    const savedRows = state.messageIds.map((id) => state.messagesById[id]);
    expect(savedRows).toHaveLength(2);
    // TranscriptPane.durableKeepSeq counts only `typeof seq === "number"`
    // rows; a keepalive-saved row without a seq contributes nothing to the
    // Retry/Edit truncate anchor and is permanently deleted server-side.
    for (const row of savedRows) {
      expect(typeof row.seq).toBe("number");
    }
    // The numeric durable order must cover BOTH turn rows.
    expect(savedRows.map((row) => row.seq)).toEqual([1, 2]);
  });
});
