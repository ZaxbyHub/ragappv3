import { describe, it, expect, beforeEach, vi } from "vitest";
import { useChatStore, type Message } from "./useChatStore";

/**
 * Regression tests for issue #216: switching sessions (loadChat) or starting a
 * new chat (newChat) while a stream is in flight must abort that stream and
 * reset streaming state, so the orphan stream can't lock the composer or drop
 * the assistant reply.
 */
describe("useChatStore abort-on-switch", () => {
  beforeEach(() => {
    useChatStore.setState({
      messageIds: [],
      messagesById: {},
      streamingMessageId: null,
      isStreaming: false,
      abortFn: null,
      activeChatId: null,
    });
  });

  const streamingState = (abortFn: () => void) => ({
    activeChatId: "1",
    isStreaming: true,
    abortFn,
    streamingMessageId: "a1",
    messageIds: ["u1", "a1"],
    messagesById: {
      u1: { id: "u1", role: "user", content: "hi" } as Message,
      a1: { id: "a1", role: "assistant", content: "partial" } as Message,
    },
  });

  it("loadChat aborts the in-flight stream and resets streaming state", () => {
    const abortFn = vi.fn();
    useChatStore.setState(streamingState(abortFn));

    const loaded: Message[] = [
      { id: "10", role: "user", content: "older" },
      { id: "11", role: "assistant", content: "older reply" },
    ];
    useChatStore.getState().loadChat("2", loaded);

    expect(abortFn).toHaveBeenCalledTimes(1);
    const s = useChatStore.getState();
    expect(s.isStreaming).toBe(false);
    expect(s.abortFn).toBeNull();
    expect(s.streamingMessageId).toBeNull();
    expect(s.activeChatId).toBe("2");
    expect(s.messageIds).toEqual(["10", "11"]);
  });

  it("newChat aborts the in-flight stream and resets streaming state", () => {
    const abortFn = vi.fn();
    useChatStore.setState(streamingState(abortFn));

    useChatStore.getState().newChat();

    expect(abortFn).toHaveBeenCalledTimes(1);
    const s = useChatStore.getState();
    expect(s.isStreaming).toBe(false);
    expect(s.abortFn).toBeNull();
    expect(s.streamingMessageId).toBeNull();
    expect(s.activeChatId).toBeNull();
    expect(s.messageIds).toEqual([]);
  });

  it("loadChat is a no-op on streaming state when nothing is in flight", () => {
    // abortFn null → no throw, just loads.
    useChatStore.getState().loadChat("3", [
      { id: "20", role: "user", content: "x" },
    ]);
    const s = useChatStore.getState();
    expect(s.activeChatId).toBe("3");
    expect(s.isStreaming).toBe(false);
    expect(s.abortFn).toBeNull();
  });
});
