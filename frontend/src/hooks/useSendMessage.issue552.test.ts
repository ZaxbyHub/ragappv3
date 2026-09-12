import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useSendMessage } from "./useSendMessage";
import { useChatStore, type Message } from "@/stores/useChatStore";

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

function savedRows(messages: unknown[]): Array<{ id: number; created_at: string }> {
  return messages.map((_, index) => ({
    id: 100 + index,
    created_at: `2026-09-12T00:00:0${index}Z`,
  }));
}

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

async function waitForPersistence(): Promise<void> {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

describe("useSendMessage issue #552 acceptance coverage", () => {
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
      async (_sessionId: number, messages: unknown[]) => savedRows(messages),
    );
    apiMocks.addChatMessagesBatchKeepalive.mockImplementation(
      async (_sessionId: number, messages: unknown[]) => savedRows(messages),
    );
  });

  afterEach(() => {
    cleanup();
  });

  it("persists one interrupted user+assistant batch after Stop and refreshes history", async () => {
    const refreshHistory = vi.fn().mockResolvedValue(undefined);
    const { result } = await startSend(refreshHistory);

    await emitPartialAnswer();
    await act(async () => {
      result.current.handleStop();
    });
    await waitForPersistence();

    expect(apiMocks.addChatMessagesBatch).toHaveBeenCalledTimes(1);
    const [sessionId, payload] = apiMocks.addChatMessagesBatch.mock.calls[0] as [
      number,
      Array<Record<string, unknown>>,
    ];
    expect(sessionId).toBe(42);
    expect(payload).toHaveLength(2);
    expect(payload[0]).toMatchObject({
      role: "user",
      content: "question",
      turn_id: expect.any(String),
    });
    expect(payload[1]).toMatchObject({
      role: "assistant",
      content: "partial answer",
      status: "interrupted",
      turn_id: payload[0].turn_id,
    });
    expect(refreshHistory).toHaveBeenCalledWith(true);
  });

  it("persists only the user row when Stop occurs before the first token", async () => {
    const refreshHistory = vi.fn().mockResolvedValue(undefined);
    const { result } = await startSend(refreshHistory);

    await act(async () => {
      result.current.handleStop();
    });
    await waitForPersistence();

    expect(apiMocks.addChatMessagesBatch).toHaveBeenCalledTimes(1);
    const payload = apiMocks.addChatMessagesBatch.mock.calls[0][1] as Array<
      Record<string, unknown>
    >;
    expect(payload).toHaveLength(1);
    expect(payload[0]).toMatchObject({
      role: "user",
      content: "question",
      turn_id: expect.any(String),
    });
    expect(refreshHistory).toHaveBeenCalledWith(true);
  });

  it("surfaces a failed pre-token Stop save on the assistant row", async () => {
    const refreshHistory = vi.fn().mockResolvedValue(undefined);
    apiMocks.addChatMessagesBatch.mockRejectedValueOnce(new Error("save failed"));
    const { result } = await startSend(refreshHistory);

    await act(async () => {
      result.current.handleStop();
    });
    await waitForPersistence();

    expect(apiMocks.addChatMessagesBatch).toHaveBeenCalledTimes(1);
    const payload = apiMocks.addChatMessagesBatch.mock.calls[0][1] as Array<
      Record<string, unknown>
    >;
    expect(payload).toHaveLength(1);
    expect(payload[0]).toMatchObject({
      role: "user",
      content: "question",
      turn_id: expect.any(String),
    });
    const state = useChatStore.getState();
    const assistant = state.messageIds
      .map((id) => state.messagesById[id])
      .find((message) => message.role === "assistant");
    expect(assistant).toMatchObject({
      error: "Couldn't save this exchange. Retry to avoid losing it.",
    });
    expect(assistant?.saveState).not.toBe("saving");
    expect(state.messagesById[state.messageIds[0]]?.saveState).toBe("failed");
    expect(refreshHistory).not.toHaveBeenCalled();
  });

  it("uses the keepalive batch API for a partial pagehide with the same payload", async () => {
    const refreshHistory = vi.fn().mockResolvedValue(undefined);
    await startSend(refreshHistory);

    await emitPartialAnswer();
    await act(async () => {
      window.dispatchEvent(new Event("pagehide"));
    });
    await waitForPersistence();

    expect(apiMocks.addChatMessagesBatchKeepalive).toHaveBeenCalledTimes(1);
    const [sessionId, payload] =
      apiMocks.addChatMessagesBatchKeepalive.mock.calls[0] as [
        number,
        Array<Record<string, unknown>>,
      ];
    expect(sessionId).toBe(42);
    expect(payload).toHaveLength(2);
    expect(payload[0]).toMatchObject({
      role: "user",
      content: "question",
      turn_id: expect.any(String),
    });
    expect(payload[1]).toMatchObject({
      role: "assistant",
      content: "partial answer",
      status: "interrupted",
      turn_id: payload[0].turn_id,
    });
  });

  it("uses one keepalive user row for a pre-token pagehide", async () => {
    const refreshHistory = vi.fn().mockResolvedValue(undefined);
    await startSend(refreshHistory);

    await act(async () => {
      window.dispatchEvent(new Event("pagehide"));
    });
    await waitForPersistence();

    expect(apiMocks.addChatMessagesBatch).not.toHaveBeenCalled();
    expect(apiMocks.addChatMessagesBatchKeepalive).toHaveBeenCalledTimes(1);
    const payload = apiMocks.addChatMessagesBatchKeepalive.mock.calls[0][1] as Array<
      Record<string, unknown>
    >;
    expect(payload).toHaveLength(1);
    expect(payload[0]).toMatchObject({
      role: "user",
      content: "question",
      turn_id: expect.any(String),
    });
  });

  it("tracks a pending keepalive save until the pagehide request settles", async () => {
    const refreshHistory = vi.fn().mockResolvedValue(undefined);
    let resolveKeepalive!: () => void;
    apiMocks.addChatMessagesBatchKeepalive.mockReturnValueOnce(
      new Promise<void>((resolve) => {
        resolveKeepalive = resolve;
      }),
    );
    await startSend(refreshHistory);

    await act(async () => {
      window.dispatchEvent(new Event("pagehide"));
    });

    const pending = useChatStore.getState().pendingTurnPersist;
    expect(pending).toBeInstanceOf(Promise);
    expect(apiMocks.addChatMessagesBatchKeepalive).toHaveBeenCalledTimes(1);

    await act(async () => {
      resolveKeepalive();
      await pending;
    });
    expect(useChatStore.getState().pendingTurnPersist).toBeNull();
  });

  it("cleans up the active UI when completion follows a pagehide claim", async () => {
    const refreshHistory = vi.fn().mockResolvedValue(undefined);
    const { result } = await startSend(refreshHistory);

    await emitPartialAnswer();
    await act(async () => {
      window.dispatchEvent(new Event("pagehide"));
    });
    expect(useChatStore.getState().isStreaming).toBe(true);

    await act(async () => {
      const handlers = activeHandlers;
      if (!handlers) throw new Error("chatStream handlers were not captured");
      handlers.onMessage(" final answer");
      await handlers.onComplete();
    });

    const state = useChatStore.getState();
    expect(state.isStreaming).toBe(false);
    expect(state.streamingMessageId).toBeNull();
    expect(state.abortFn).toBeNull();
    expect(state.messagesById[state.messageIds[1]]?.content).toBe(
      "partial answer final answer",
    );
    expect(apiMocks.addChatMessagesBatch).not.toHaveBeenCalled();
    expect(apiMocks.addChatMessagesBatchKeepalive).toHaveBeenCalledTimes(1);
  });

  it("cleans up and surfaces an error when an error follows a pagehide claim", async () => {
    const refreshHistory = vi.fn().mockResolvedValue(undefined);
    await startSend(refreshHistory);

    await emitPartialAnswer();
    await act(async () => {
      window.dispatchEvent(new Event("pagehide"));
      const handlers = activeHandlers;
      if (!handlers) throw new Error("chatStream handlers were not captured");
      handlers.onError(new Error("stream failed after pagehide"));
    });

    const state = useChatStore.getState();
    const assistant = state.messagesById[state.messageIds[1]];
    expect(state.isStreaming).toBe(false);
    expect(state.streamingMessageId).toBeNull();
    expect(state.abortFn).toBeNull();
    expect(assistant).toMatchObject({
      status: "failed",
      content: "partial answer",
      error: "stream failed after pagehide",
    });
    expect(apiMocks.addChatMessagesBatch).not.toHaveBeenCalled();
    expect(apiMocks.addChatMessagesBatchKeepalive).toHaveBeenCalledTimes(1);
  });

  it("does not let stale terminal callbacks change a newer stream", async () => {
    const refreshHistory = vi.fn().mockResolvedValue(undefined);
    const { result } = await startSend(refreshHistory);
    const firstHandlers = activeHandlers;
    if (!firstHandlers) throw new Error("first chatStream handlers were not captured");

    await act(async () => {
      result.current.handleStop();
    });
    await waitForPersistence();

    await act(async () => {
      useChatStore.setState({ input: "new question" });
    });
    await act(async () => {
      await result.current.handleSend();
    });
    const secondHandlers = activeHandlers;
    if (!secondHandlers) throw new Error("second chatStream handlers were not captured");
    expect(secondHandlers).not.toBe(firstHandlers);

    await act(async () => {
      secondHandlers.onMessage("new partial answer");
    });
    await waitForPersistence();
    const secondAssistantId = useChatStore.getState().streamingMessageId;
    expect(secondAssistantId).toBeTruthy();

    await act(async () => {
      await firstHandlers.onComplete();
      firstHandlers.onError(new Error("stale stream failure"));
    });

    const state = useChatStore.getState();
    expect(state.isStreaming).toBe(true);
    expect(state.streamingMessageId).toBe(secondAssistantId);
    expect(state.messagesById[secondAssistantId!]).toMatchObject({
      content: "new partial answer",
    });

    await act(async () => {
      await secondHandlers.onComplete();
    });
  });

  it("deduplicates a Stop/pagehide race to one persistence call", async () => {
    const refreshHistory = vi.fn().mockResolvedValue(undefined);
    const { result } = await startSend(refreshHistory);

    await emitPartialAnswer();
    await act(async () => {
      window.dispatchEvent(new Event("pagehide"));
      result.current.handleStop();
    });
    await waitForPersistence();

    const persistenceCalls =
      apiMocks.addChatMessagesBatch.mock.calls.length +
      apiMocks.addChatMessagesBatchKeepalive.mock.calls.length;
    expect(persistenceCalls).toBe(1);
  });

  it.each(["loadChat", "newChat"] as const)(
    "does not persist an abandoned stream when %s clears the chat",
    async (operation) => {
      const refreshHistory = vi.fn().mockResolvedValue(undefined);
      await startSend(refreshHistory);
      await emitPartialAnswer();

      await act(async () => {
        if (operation === "loadChat") {
          useChatStore.getState().loadChat("99", [
            { id: "existing", role: "user", content: "existing" },
          ] satisfies Message[]);
        } else {
          useChatStore.getState().newChat();
        }
      });
      await waitForPersistence();

      expect(apiMocks.addChatMessagesBatch).not.toHaveBeenCalled();
      expect(apiMocks.addChatMessagesBatchKeepalive).not.toHaveBeenCalled();
    },
  );
});
