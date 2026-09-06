import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useSendMessage } from "./useSendMessage";
import { useChatStore } from "@/stores/useChatStore";
import { useChatShellStore } from "@/stores/useChatShellStore";
import { useLlmHealthStore } from "@/stores/useLlmHealthStore";

const apiMocks = vi.hoisted(() => ({
  createChatSession: vi.fn(),
  addChatMessage: vi.fn(),
  addChatMessagesBatch: vi.fn(),
  chatStream: vi.fn(),
  getLlmModeHealth: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  createChatSession: (...args: unknown[]) => apiMocks.createChatSession(...args),
  addChatMessage: (...args: unknown[]) => apiMocks.addChatMessage(...args),
  addChatMessagesBatch: (...args: unknown[]) => apiMocks.addChatMessagesBatch(...args),
  chatStream: (...args: unknown[]) => apiMocks.chatStream(...args),
  getLlmModeHealth: (...args: unknown[]) => apiMocks.getLlmModeHealth(...args),
}));

type StreamHandlers = {
  onMessage: (chunk: string) => void;
  onSources: (sources: unknown[]) => void;
  onMemories: (memories: unknown[]) => void;
  onWiki: (wikiRefs: unknown[]) => void;
  onKMS: (kmsRefs: unknown[]) => void;
  onMode: (mode: string) => void;
  onFinalContent?: (content: string) => void;
  onCitationConfidence?: (confidence: unknown) => void;
  onUnverifiableClaims?: (claims: unknown[]) => void;
  onError: (error: Error) => void;
  onComplete: () => Promise<void> | void;
};

// Install a chatStream mock that captures the handlers so each test can
// fire them at will. The captured handler is read from a mutable cell so
// subsequent mock invocations (e.g. retries) update the reference and
// trigger calls operate on the latest invocation.
function installCapturingStreamMock(): { trigger: { error: (e: Error) => void; complete: () => void } } {
  const cell: { current: StreamHandlers | null } = { current: null };
  apiMocks.chatStream.mockImplementation((_messages: unknown, handlers: StreamHandlers) => {
    cell.current = handlers;
    return vi.fn(); // abort function
  });
  return {
    trigger: {
      message: (chunk: string) => {
        if (!cell.current) throw new Error("chatStream was not invoked yet");
        cell.current.onMessage(chunk);
      },
      citationConfidence: (confidence: Record<string, number>) => {
        if (!cell.current) throw new Error("chatStream was not invoked yet");
        cell.current.onCitationConfidence?.(confidence);
      },
      unverifiableClaims: (claims: string[]) => {
        if (!cell.current) throw new Error("chatStream was not invoked yet");
        cell.current.onUnverifiableClaims?.(claims);
      },
      error: (e: Error) => {
        if (!cell.current) throw new Error("chatStream was not invoked yet");
        cell.current.onError(e);
      },
      complete: () => {
        if (!cell.current) throw new Error("chatStream was not invoked yet");
        void cell.current.onComplete();
      },
    },
  };
}

describe("useSendMessage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
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
    useLlmHealthStore.setState({ thinking: true, instant: true });
    useChatShellStore.setState({ sessionListRefreshToken: 0 });
    apiMocks.createChatSession.mockResolvedValue({ id: 42 });
    // Default batch save: user row gets id 100, assistant row id 101 (issue #507
    // realignment — the hook persists a turn via one addChatMessagesBatch call).
    apiMocks.addChatMessagesBatch.mockResolvedValue([
      { id: 100, created_at: "2026-05-12T00:00:00Z" },
      { id: 101, created_at: "2026-05-12T00:00:01Z" },
    ]);
    apiMocks.chatStream.mockImplementation((_messages: unknown, handlers: {
      onMessage: (chunk: string) => void;
      onComplete: () => Promise<void>;
    }) => {
      handlers.onMessage("hello");
      void handlers.onComplete();
      return vi.fn();
    });
  });
  it("force-refreshes history after a newly created session is persisted", async () => {
    const refreshHistory = vi.fn().mockResolvedValue(undefined);
    useChatStore.setState({ input: "What changed?" });

    const { result } = renderHook(() => useSendMessage(7, refreshHistory));

    await act(async () => {
      await result.current.handleSend();
    });

    await waitFor(() => {
      expect(refreshHistory).toHaveBeenCalledWith(true);
    });
    expect(useChatShellStore.getState().sessionListRefreshToken).toBeGreaterThan(0);
    expect(apiMocks.createChatSession).toHaveBeenCalledWith({ vault_id: 7 });
    // Realigned to the issue #507 batch save: one ordered call carrying the
    // user row then the assistant row.
    expect(apiMocks.addChatMessagesBatch).toHaveBeenCalledTimes(1);
    expect(apiMocks.addChatMessagesBatch).toHaveBeenCalledWith(42, [
      expect.objectContaining({ role: "user", content: "What changed?" }),
      expect.objectContaining({ role: "assistant" }),
    ]);
  });

  it("persists the FULL streamed assistant content (rAF batching must flush before persist — UI-PERF-2)", async () => {
    // The default chatStream mock calls onMessage("hello") then immediately
    // onComplete(). With rAF batching, the buffered "hello" must be flushed
    // synchronously in onComplete before the store content is persisted,
    // otherwise the assistant message is saved truncated.
    const refreshHistory = vi.fn().mockResolvedValue(undefined);
    useChatStore.setState({ input: "hi" });

    const { result } = renderHook(() => useSendMessage(7, refreshHistory));

    await act(async () => {
      await result.current.handleSend();
    });

    await waitFor(() => {
      expect(refreshHistory).toHaveBeenCalledWith(true);
    });

    // The assistant payload in the batch must carry the streamed content.
    const batchArgs = apiMocks.addChatMessagesBatch.mock.calls[0];
    const assistantPayload = (batchArgs[1] as Array<{ role: string; content: string }>).find(
      (m) => m.role === "assistant",
    );
    expect(assistantPayload).toBeDefined();
    expect(assistantPayload!.content).toContain("hello");
  });

  it("onFinalContent replaces streamed content without re-injecting stripped citations (UI-PERF-2 + rAF buffer)", async () => {
    // Regression for the rAF-batching interaction with citation-stripping:
    // stream a citation-dirty token, then fire onFinalContent (cleaned) and
    // onComplete back-to-back (the SSE done-event ordering). The persisted
    // assistant content must equal the REPAIRED text — no duplicated body,
    // no re-injected [S5] citation.
    apiMocks.chatStream.mockImplementation((_messages: unknown, handlers: StreamHandlers) => {
      // Buffer a citation-dirty token (rAF has not flushed).
      handlers.onMessage("The answer is 42 [S5]");
      // done event carries repaired_content (citation stripped).
      handlers.onFinalContent?.("The answer is 42");
      void handlers.onComplete();
      return vi.fn();
    });

    const refreshHistory = vi.fn().mockResolvedValue(undefined);
    useChatStore.setState({ input: "what is the answer" });

    const { result } = renderHook(() => useSendMessage(7, refreshHistory));
    await act(async () => {
      await result.current.handleSend();
    });
    await waitFor(() => {
      expect(refreshHistory).toHaveBeenCalled();
    });

    const batchArgs = apiMocks.addChatMessagesBatch.mock.calls[0];
    const assistantPayload = (batchArgs[1] as Array<{ role: string; content: string }>).find(
      (m) => m.role === "assistant",
    );
    expect(assistantPayload).toBeDefined();
    const persisted = assistantPayload!.content;
    // Must equal the repaired content exactly (no duplication, no [S5]).
    expect(persisted).toBe("The answer is 42");
    expect(persisted).not.toContain("[S5]");
  });

  it("force-refreshes history and the session rail after an existing session is persisted", async () => {
    const refreshHistory = vi.fn().mockResolvedValue(undefined);
    useChatStore.setState({ activeChatId: "42", input: "Follow up" });

    const { result } = renderHook(() => useSendMessage(7, refreshHistory));

    await act(async () => {
      await result.current.handleSend();
    });

    await waitFor(() => {
      expect(refreshHistory).toHaveBeenCalledWith(true);
    });
    expect(apiMocks.createChatSession).not.toHaveBeenCalled();
    expect(useChatShellStore.getState().sessionListRefreshToken).toBe(1);
  });

  describe("vault null guard", () => {
    it("shows error and returns early when activeVaultId is null with no activeChatId", async () => {
      const refreshHistory = vi.fn().mockResolvedValue(undefined);
      useChatStore.setState({ input: "Hello" });

      const { result } = renderHook(() => useSendMessage(null, refreshHistory));

      await act(async () => {
        await result.current.handleSend();
      });

      await waitFor(() => {
        expect(useChatStore.getState().inputError).toBe(
          "Please select a vault before starting a chat."
        );
      });
      expect(apiMocks.createChatSession).not.toHaveBeenCalled();
      expect(refreshHistory).not.toHaveBeenCalled();
    });

    it("allows send when activeVaultId is null but activeChatId exists", async () => {
      const refreshHistory = vi.fn().mockResolvedValue(undefined);
      useChatStore.setState({ activeChatId: "42", input: "Follow up" });

      const { result } = renderHook(() => useSendMessage(null, refreshHistory));

      await act(async () => {
        await result.current.handleSend();
      });

      await waitFor(() => {
        expect(useChatStore.getState().inputError).toBeNull();
      });
      expect(apiMocks.createChatSession).not.toHaveBeenCalled();
    });
  });

  describe("vault_id defaulting — regression (F#)", () => {
    it("createChatSession not called and error shown when activeVaultId is null with no activeChatId", async () => {
      const refreshHistory = vi.fn().mockResolvedValue(undefined);
      useChatStore.setState({ input: "Hello" });

      const { result } = renderHook(() => useSendMessage(null, refreshHistory));

      await act(async () => {
        await result.current.handleSend();
      });

      await waitFor(() => {
        expect(apiMocks.createChatSession).not.toHaveBeenCalled();
        expect(useChatStore.getState().inputError).toBe(
          "Please select a vault before starting a chat."
        );
      });
    });

    it("chatStream not called when activeVaultId is null with no activeChatId", async () => {
      const refreshHistory = vi.fn().mockResolvedValue(undefined);
      useChatStore.setState({ input: "Hello" });

      const { result } = renderHook(() => useSendMessage(null, refreshHistory));

      await act(async () => {
        await result.current.handleSend();
      });

      await waitFor(() => {
        expect(apiMocks.chatStream).not.toHaveBeenCalled();
      });
    });

    it("chatStream receives the actual activeVaultId when it is non-null", async () => {
      const refreshHistory = vi.fn().mockResolvedValue(undefined);
      useChatStore.setState({ activeChatId: "99", input: "Hello" });

      const { result } = renderHook(() => useSendMessage(5, refreshHistory));

      await act(async () => {
        await result.current.handleSend();
      });

      await waitFor(() => {
        expect(apiMocks.chatStream).toHaveBeenCalled();
        const callArgs = apiMocks.chatStream.mock.calls[0];
        expect(callArgs[2]).toBe(5);
      });
    });

    it("createChatSession receives the actual activeVaultId when it is non-null", async () => {
      const refreshHistory = vi.fn().mockResolvedValue(undefined);
      useChatStore.setState({ input: "Hello" });

      const { result } = renderHook(() => useSendMessage(5, refreshHistory));

      await act(async () => {
        await result.current.handleSend();
      });

      await waitFor(() => {
        expect(apiMocks.createChatSession).toHaveBeenCalledWith({ vault_id: 5 });
      });
    });
  });

  describe("error-path coverage (issue #55)", () => {
    it("flips isStreaming to true synchronously when send begins", async () => {
      // Arrange: a stream that never completes, so we can observe the
      // mid-flight isStreaming value.
      apiMocks.chatStream.mockImplementation((_messages: unknown, _handlers: StreamHandlers) => {
        return vi.fn();
      });
      const refreshHistory = vi.fn().mockResolvedValue(undefined);
      useChatStore.setState({ activeChatId: "42", input: "Stream start" });

      const { result } = renderHook(() => useSendMessage(7, refreshHistory));

      // Don't await — observe store state immediately after kicking off the send.
      act(() => {
        void result.current.handleSend();
      });

      // isStreaming must be true right after the send call returns, before any
      // stream events have fired. This guards the immediate flip behavior.
      expect(useChatStore.getState().isStreaming).toBe(true);
    });

    it("handles AbortError: clears isStreaming, abortFn, streamingMessageId, sendingRef — without stamping an error on the message", async () => {
      const capture = installCapturingStreamMock();
      const refreshHistory = vi.fn().mockResolvedValue(undefined);
      useChatStore.setState({ activeChatId: "42", input: "Will be aborted" });

      const { result } = renderHook(() => useSendMessage(7, refreshHistory));

      await act(async () => {
        await result.current.handleSend();
      });

      // The send should have created the assistant message and marked it as
      // streaming. Capture the streamingMessageId so we can inspect the
      // message after abort.
      const streamingId = useChatStore.getState().streamingMessageId;
      expect(streamingId).toBeTruthy();
      expect(useChatStore.getState().isStreaming).toBe(true);

      // Simulate the AbortError bubbling up through the SSE stream.
      await act(async () => {
        capture.trigger.error(new DOMException("aborted", "AbortError"));
      });

      await waitFor(() => {
        expect(useChatStore.getState().isStreaming).toBe(false);
      });
      expect(useChatStore.getState().abortFn).toBeNull();
      expect(useChatStore.getState().streamingMessageId).toBeNull();

      // Aborts must NOT mark the assistant message with an error field —
      // they're a normal user action, not a failure.
      const assistant = useChatStore.getState().messagesById[streamingId!];
      expect(assistant).toBeDefined();
      expect(assistant?.error).toBeUndefined();

      // refreshHistory must NOT be called for an aborted send (no onComplete fired).
      expect(refreshHistory).not.toHaveBeenCalled();

      // sendingRef must have been cleared: a subsequent send must not be silently dropped.
      useChatStore.setState({ input: "second send" });
      await act(async () => {
        await result.current.handleSend();
      });
      // chatStream called twice total — once for the aborted send, once for the follow-up.
      expect(apiMocks.chatStream).toHaveBeenCalledTimes(2);
    });

    it("handles AbortError when error.message mentions 'abort' but name is not 'AbortError'", async () => {
      // The code also matches /aborted|abort/i on message — exercise that branch.
      const capture = installCapturingStreamMock();
      const refreshHistory = vi.fn().mockResolvedValue(undefined);
      useChatStore.setState({ activeChatId: "42", input: "Will be aborted via message" });

      const { result } = renderHook(() => useSendMessage(7, refreshHistory));

      await act(async () => {
        await result.current.handleSend();
      });

      const streamingId = useChatStore.getState().streamingMessageId;
      expect(streamingId).toBeTruthy();

      await act(async () => {
        // Plain Error (not DOMException) — only the message regex will catch this.
        capture.trigger.error(new Error("Request was aborted by client"));
      });

      await waitFor(() => {
        expect(useChatStore.getState().isStreaming).toBe(false);
      });
      expect(useChatStore.getState().abortFn).toBeNull();
      expect(useChatStore.getState().streamingMessageId).toBeNull();
      const assistant = useChatStore.getState().messagesById[streamingId!];
      expect(assistant?.error).toBeUndefined();
    });

    it("network error: stamps a friendly 'Connection lost' message and rolls back streaming state", async () => {
      const capture = installCapturingStreamMock();
      const refreshHistory = vi.fn().mockResolvedValue(undefined);
      useChatStore.setState({ activeChatId: "42", input: "Will fail with network error" });

      const { result } = renderHook(() => useSendMessage(7, refreshHistory));

      await act(async () => {
        await result.current.handleSend();
      });

      const streamingId = useChatStore.getState().streamingMessageId;
      expect(streamingId).toBeTruthy();
      expect(useChatStore.getState().isStreaming).toBe(true);

      await act(async () => {
        // PRR-001: a mid-stream network failure with partial content must not
        // lose the turn — it persists as "failed" instead of vanishing.
        capture.trigger.message("partial answer");
        capture.trigger.error(new TypeError("Failed to fetch"));
      });

      await waitFor(() => {
        expect(useChatStore.getState().isStreaming).toBe(false);
      });
      expect(useChatStore.getState().abortFn).toBeNull();
      expect(useChatStore.getState().streamingMessageId).toBeNull();

      // The partial turn is durably saved with the failed terminal status.
      await waitFor(() => {
        expect(apiMocks.addChatMessagesBatch).toHaveBeenCalledTimes(1);
      });
      const payloads = apiMocks.addChatMessagesBatch.mock.calls[0][1] as Array<{
        role: string;
        status?: string;
      }>;
      const assistantPayload = payloads.find((m) => m.role === "assistant");
      expect(assistantPayload?.status).toBe("failed");

      // The friendly network error stays stamped on the assistant row (which
      // the successful save migrates to its server id — read via the list).
      await waitFor(() => {
        const assistant = useChatStore
          .getState()
          .messageIds.map((id) => useChatStore.getState().messagesById[id])
          .find((m) => m.role === "assistant");
        expect(assistant?.error).toBe("Connection lost. Check your network and try again.");
        expect(assistant?.status).toBe("failed");
      });
      // The failed turn's save landed, so history is force-refreshed.
      await waitFor(() => {
        expect(refreshHistory).toHaveBeenCalledWith(true);
      });
    });

    it("non-network, non-abort error before any content: stamps status failed and surfaces the empty-response error (PRR-001 + LIVE-01)", async () => {
      const capture = installCapturingStreamMock();
      const refreshHistory = vi.fn().mockResolvedValue(undefined);
      useChatStore.setState({ activeChatId: "42", input: "Will fail with server error" });

      const { result } = renderHook(() => useSendMessage(7, refreshHistory));

      await act(async () => {
        await result.current.handleSend();
      });

      const streamingId = useChatStore.getState().streamingMessageId;

      await act(async () => {
        // A pre-content server failure: the generic branch stamps status
        // "failed", but persistTurn's empty-content guard (LIVE-01) keeps the
        // turn unpersisted and replaces the raw message with the
        // empty-response error.
        capture.trigger.error(new Error("upstream LLM returned 500"));
      });

      await waitFor(() => {
        expect(useChatStore.getState().isStreaming).toBe(false);
      });
      const assistant = useChatStore.getState().messagesById[streamingId!];
      expect(assistant?.status).toBe("failed");
      expect(assistant?.error).toBe("The model returned an empty response. Try again.");
      expect(apiMocks.addChatMessagesBatch).not.toHaveBeenCalled();
    });

    it("failure rollback: after a non-abort error, sendingRef is reset so a follow-up send is not silently dropped", async () => {
      const capture = installCapturingStreamMock();
      const refreshHistory = vi.fn().mockResolvedValue(undefined);
      useChatStore.setState({ activeChatId: "42", input: "First send will fail" });

      const { result } = renderHook(() => useSendMessage(7, refreshHistory));

      await act(async () => {
        await result.current.handleSend();
      });
      expect(apiMocks.chatStream).toHaveBeenCalledTimes(1);

      await act(async () => {
        capture.trigger.error(new Error("boom"));
      });

      await waitFor(() => {
        expect(useChatStore.getState().isStreaming).toBe(false);
      });

      // Now retry — must not be blocked by the previous send's sendingRef.
      useChatStore.setState({ input: "Second send" });
      await act(async () => {
        await result.current.handleSend();
      });
      expect(apiMocks.chatStream).toHaveBeenCalledTimes(2);
    });
  });

  describe("abandoned-stream save guard (issue #235)", () => {
    // When loadChat/newChat aborts an in-flight stream, the orphan stream's
    // onComplete can still fire asynchronously. The assistant save was already
    // guarded; the user-save was not, so the old session could end up with a
    // dangling user message. These tests pin down the guard in both the
    // loadChat-switch and newChat cases.
    it("does NOT persist the user message when loadChat aborts the stream before onComplete fires", async () => {
      const capture = installCapturingStreamMock();
      const refreshHistory = vi.fn().mockResolvedValue(undefined);
      useChatStore.setState({ activeChatId: "42", input: "abandoned user" });

      const { result } = renderHook(() => useSendMessage(7, refreshHistory));

      await act(async () => {
        await result.current.handleSend();
      });
      expect(apiMocks.chatStream).toHaveBeenCalledTimes(1);

      // Simulate the user switching sessions mid-stream. loadChat calls
      // abortFn() (which the wrapping useSendMessage setAbortFn installs),
      // and then clears messagesById. That's the exact state the orphan
      // stream's onComplete will observe.
      await act(async () => {
        useChatStore.getState().loadChat("99", [
          { id: "older-1", role: "user", content: "older" },
        ]);
      });

      // Sanity: loadChat replaced messagesById with the new session's
      // messages. The orphan stream's assistant id is gone from the store.
      const after = useChatStore.getState();
      expect(after.activeChatId).toBe("99");
      expect(after.messagesById).toEqual({
        "older-1": expect.objectContaining({ id: "older-1", role: "user" }),
      });

      // Now fire onComplete — it should be a no-op for persistence.
      apiMocks.addChatMessagesBatch.mockClear();
      await act(async () => {
        capture.trigger.complete();
      });

      // Critical assertion: the batch save is NOT invoked. The orphan stream
      // must not persist anything to the old (or any) session.
      expect(apiMocks.addChatMessagesBatch).not.toHaveBeenCalled();
      // refreshHistory must not be called either — no save happened.
      expect(refreshHistory).not.toHaveBeenCalled();
    });

    it("does NOT persist the user message when newChat aborts the stream before onComplete fires", async () => {
      const capture = installCapturingStreamMock();
      const refreshHistory = vi.fn().mockResolvedValue(undefined);
      useChatStore.setState({ activeChatId: "42", input: "abandoned again" });

      const { result } = renderHook(() => useSendMessage(7, refreshHistory));

      await act(async () => {
        await result.current.handleSend();
      });

      await act(async () => {
        useChatStore.getState().newChat();
      });

      expect(useChatStore.getState().messagesById).toEqual({});
      expect(useChatStore.getState().messageIds).toEqual([]);

      apiMocks.addChatMessagesBatch.mockClear();
      await act(async () => {
        capture.trigger.complete();
      });

      expect(apiMocks.addChatMessagesBatch).not.toHaveBeenCalled();
      expect(refreshHistory).not.toHaveBeenCalled();
    });

    it("DOES persist both messages when onComplete fires with the assistant message still present", async () => {
      // Negative control: the guard must not over-trigger. A normal completion
      // (no session switch in between) must still save both messages.
      const capture = installCapturingStreamMock();
      const refreshHistory = vi.fn().mockResolvedValue(undefined);
      useChatStore.setState({ activeChatId: "42", input: "normal completion" });
      apiMocks.addChatMessagesBatch
        .mockReset()
        .mockResolvedValue([
          { id: 200, created_at: "2026-06-01T00:00:00Z" },
          { id: 201, created_at: "2026-06-01T00:00:01Z" },
        ]);

      const { result } = renderHook(() => useSendMessage(7, refreshHistory));

      await act(async () => {
        await result.current.handleSend();
      });

      // Don't switch sessions — fire a real chunk then onComplete directly
      // (empty content is never persisted at all — LIVE-01 — so the negative
      // control must stream something before completing).
      await act(async () => {
        capture.trigger.message("normal completion answer");
        capture.trigger.complete();
      });

      await waitFor(() => {
        expect(refreshHistory).toHaveBeenCalledWith(true);
      });
      // Both the user and assistant message must be persisted in ONE ordered
      // batch call (issue #507), user row first.
      expect(apiMocks.addChatMessagesBatch).toHaveBeenCalledTimes(1);
      expect(apiMocks.addChatMessagesBatch).toHaveBeenCalledWith(42, [
        expect.objectContaining({ role: "user", content: "normal completion" }),
        expect.objectContaining({ role: "assistant" }),
      ]);
    });
  });

  describe("chat turn lifecycle (issue #507)", () => {
    it("does not start generation when Stop is pressed during session creation (UI-003)", async () => {
      // createChatSession stays pending so Stop lands before the stream exists.
      let resolveCreation!: (session: { id: number }) => void;
      apiMocks.createChatSession.mockReturnValue(
        new Promise<{ id: number }>((resolve) => {
          resolveCreation = resolve;
        })
      );
      const refreshHistory = vi.fn().mockResolvedValue(undefined);
      useChatStore.setState({ input: "stop me mid-creation" });

      const { result } = renderHook(() => useSendMessage(7, refreshHistory));

      let sendPromise = Promise.resolve();
      act(() => {
        sendPromise = result.current.handleSend();
      });

      // Stop while the session is still being created — no stream to abort yet.
      act(() => {
        result.current.handleStop();
      });

      // Resolve creation and flush: the cancelled generation must not start.
      await act(async () => {
        resolveCreation({ id: 42 });
        await sendPromise;
      });

      expect(apiMocks.chatStream).not.toHaveBeenCalled();
      expect(useChatStore.getState().messageIds).toEqual([]);
      expect(useChatStore.getState().isStreaming).toBe(false);
      expect(refreshHistory).not.toHaveBeenCalled();

      // The sending guard must have been reset — a subsequent send works.
      useChatStore.setState({ input: "second attempt" });
      await act(async () => {
        await result.current.handleSend();
      });
      expect(apiMocks.chatStream).toHaveBeenCalledTimes(1);
    });

    it("saves the turn via one ordered batch with a shared turn id", async () => {
      const capture = installCapturingStreamMock();
      const refreshHistory = vi.fn().mockResolvedValue(undefined);
      useChatStore.setState({ activeChatId: "42", input: "batch me" });

      const { result } = renderHook(() => useSendMessage(7, refreshHistory));

      await act(async () => {
        await result.current.handleSend();
      });

      const streamingId = useChatStore.getState().streamingMessageId;
      expect(streamingId).toBeTruthy();

      const confidence = { S1: 0.9 };
      const claims = ["claim A", "claim B"];
      await act(async () => {
        capture.trigger.message("hello ");
        capture.trigger.message("world");
        capture.trigger.citationConfidence(confidence);
        capture.trigger.unverifiableClaims(claims);
      });
      await act(async () => {
        capture.trigger.complete();
      });

      await waitFor(() => {
        expect(refreshHistory).toHaveBeenCalledWith(true);
      });

      expect(apiMocks.addChatMessagesBatch).toHaveBeenCalledTimes(1);
      const [sessionIdArg, payloads] = apiMocks.addChatMessagesBatch.mock.calls[0] as [
        number,
        Array<{ role: string; content: string; turn_id?: string; status?: string; citation_confidence?: unknown; unverifiable_claims?: unknown }>,
      ];
      expect(sessionIdArg).toBe(42);
      expect(payloads).toHaveLength(2);
      const [userPayload, assistantPayload] = payloads;
      // Ordered: user row first, assistant row second.
      expect(userPayload.role).toBe("user");
      expect(userPayload.content).toBe("batch me");
      expect(assistantPayload.role).toBe("assistant");
      expect(assistantPayload.content).toContain("hello");
      // Shared durable turn linkage — one turn id on both rows.
      expect(typeof userPayload.turn_id).toBe("string");
      expect(userPayload.turn_id!.length).toBeGreaterThan(0);
      expect(assistantPayload.turn_id).toBe(userPayload.turn_id);
      // A happy completion is persisted as complete with the assessment
      // fields forwarded from the stream callbacks.
      expect(assistantPayload.status).toBe("complete");
      expect(assistantPayload.citation_confidence).toEqual(confidence);
      expect(assistantPayload.unverifiable_claims).toEqual(claims);

      // Store: ids migrated to the server ids with saveState "saved".
      const state = useChatStore.getState();
      const messages = state.messageIds.map((id) => state.messagesById[id]);
      const savedUser = messages.find((m) => m.role === "user");
      const savedAssistant = messages.find((m) => m.role === "assistant");
      expect(savedUser?.saveState).toBe("saved");
      expect(savedAssistant?.saveState).toBe("saved");
      expect(state.messagesById["100"]).toBeDefined();
      expect(state.messagesById["101"]).toBeDefined();
      expect(savedUser?.turnId).toBe(savedAssistant?.turnId);
    });

    it("marks the exchange failed and keeps the answer visible when the batch save rejects (UI-002)", async () => {
      const capture = installCapturingStreamMock();
      apiMocks.addChatMessagesBatch.mockRejectedValueOnce(new Error("server exploded"));
      const refreshHistory = vi.fn().mockResolvedValue(undefined);
      useChatStore.setState({ activeChatId: "42", input: "will fail to save" });

      const { result } = renderHook(() => useSendMessage(7, refreshHistory));

      await act(async () => {
        await result.current.handleSend();
      });

      const streamingId = useChatStore.getState().streamingMessageId;
      expect(streamingId).toBeTruthy();

      await act(async () => {
        capture.trigger.message("precious answer");
        capture.trigger.complete();
      });

      await waitFor(() => {
        expect(useChatStore.getState().messagesById[streamingId!]?.saveState).toBe("failed");
      });

      const state = useChatStore.getState();
      const assistant = state.messagesById[streamingId!];
      const user = state.messageIds
        .map((id) => state.messagesById[id])
        .find((m) => m.role === "user");
      // Both rows of the failed exchange are visibly marked failed...
      expect(assistant?.saveState).toBe("failed");
      expect(assistant?.error).toBe("Couldn't save this exchange. Retry to avoid losing it.");
      expect(user?.saveState).toBe("failed");
      // ...and the streamed answer stays visible (never a silent loss).
      expect(assistant?.content).toContain("precious answer");
      expect(refreshHistory).not.toHaveBeenCalled();
    });

    it("persists an interrupted stream as interrupted, not complete (CHAT-004)", async () => {
      const capture = installCapturingStreamMock();
      const refreshHistory = vi.fn().mockResolvedValue(undefined);
      useChatStore.setState({ activeChatId: "42", input: "interrupted turn" });

      const { result } = renderHook(() => useSendMessage(7, refreshHistory));

      await act(async () => {
        await result.current.handleSend();
      });

      const streamingId = useChatStore.getState().streamingMessageId;
      expect(streamingId).toBeTruthy();

      // Parser-level EOF: partial content, then ChatInterruptedError.
      await act(async () => {
        capture.trigger.message("partial");
        const interrupted = new Error(
          "The response stream ended before the answer finished."
        );
        interrupted.name = "ChatInterruptedError";
        capture.trigger.error(interrupted);
      });

      // The interrupted turn is still persisted — as interrupted.
      await waitFor(() => {
        expect(apiMocks.addChatMessagesBatch).toHaveBeenCalledTimes(1);
      });
      const payloads = apiMocks.addChatMessagesBatch.mock.calls[0][1] as Array<{
        role: string;
        content: string;
        status?: string;
      }>;
      const assistantPayload = payloads.find((m) => m.role === "assistant");
      // An interrupted answer is persisted as interrupted — never as complete.
      expect(assistantPayload?.status).toBe("interrupted");
      expect(assistantPayload?.content).toBe("partial");

      // Store state after the save migrated the temp id to server id 101:
      // the partial answer stays visible and is marked retryable.
      await waitFor(() => {
        expect(useChatStore.getState().messagesById["101"]).toBeDefined();
      });
      const state = useChatStore.getState();
      const assistant = state.messagesById["101"];
      expect(assistant?.status).toBe("interrupted");
      expect(assistant?.error).toBe("Response interrupted. You can retry.");
      expect(assistant?.content).toBe("partial");
      expect(state.isStreaming).toBe(false);
      expect(state.streamingMessageId).toBeNull();
    });

    it("does not persist or present an empty response as success", async () => {
      // Zero onMessage chunks, then a normal onComplete: the empty answer
      // must neither be saved nor look like a successful turn (LIVE-01).
      const capture = installCapturingStreamMock();
      const refreshHistory = vi.fn().mockResolvedValue(undefined);
      useChatStore.setState({ activeChatId: "42", input: "say anything" });

      const { result } = renderHook(() => useSendMessage(7, refreshHistory));

      await act(async () => {
        await result.current.handleSend();
      });

      const streamingId = useChatStore.getState().streamingMessageId;
      expect(streamingId).toBeTruthy();

      await act(async () => {
        capture.trigger.complete();
      });

      // Nothing is persisted for an empty answer.
      expect(apiMocks.addChatMessagesBatch).not.toHaveBeenCalled();
      expect(refreshHistory).not.toHaveBeenCalled();

      // The assistant message surfaces the empty response instead of success.
      const assistant = useChatStore.getState().messagesById[streamingId!];
      expect(assistant?.error).toBe("The model returned an empty response. Try again.");
      expect(assistant?.saveState).not.toBe("saved");
    });

    it("persists a failed stream as failed so a reload never loses the turn (PRR-001)", async () => {
      const capture = installCapturingStreamMock();
      const refreshHistory = vi.fn().mockResolvedValue(undefined);
      useChatStore.setState({ activeChatId: "42", input: "will fail mid-stream" });

      const { result } = renderHook(() => useSendMessage(7, refreshHistory));

      await act(async () => {
        await result.current.handleSend();
      });

      const streamingId = useChatStore.getState().streamingMessageId;
      expect(streamingId).toBeTruthy();

      // Partial content, then a plain server-side error — the generic onError
      // branch (network / SSE error frame), not abort and not interruption.
      await act(async () => {
        capture.trigger.message("partial answer before the crash");
        capture.trigger.error(new Error("Chat processing failed"));
      });

      // The failed turn IS durably persisted — once, as a batch.
      await waitFor(() => {
        expect(apiMocks.addChatMessagesBatch).toHaveBeenCalledTimes(1);
      });
      const payloads = apiMocks.addChatMessagesBatch.mock.calls[0][1] as Array<{
        role: string;
        content: string;
        status?: string;
      }>;
      const assistantPayload = payloads.find((m) => m.role === "assistant");
      expect(assistantPayload).toBeDefined();
      expect(assistantPayload!.status).toBe("failed");
      expect(assistantPayload!.content).toContain("partial answer before the crash");

      // Store: the assistant row carries the failed terminal status and the
      // raw error message (the save itself succeeds, so nothing overwrites it).
      await waitFor(() => {
        const assistant = useChatStore
          .getState()
          .messageIds.map((id) => useChatStore.getState().messagesById[id])
          .find((m) => m.role === "assistant");
        expect(assistant?.status).toBe("failed");
        expect(assistant?.error).toBe("Chat processing failed");
      });
    });

    it("does not persist a failed stream that produced no content (LIVE-01 under PRR-001)", async () => {
      const capture = installCapturingStreamMock();
      const refreshHistory = vi.fn().mockResolvedValue(undefined);
      useChatStore.setState({ activeChatId: "42", input: "say something" });

      const { result } = renderHook(() => useSendMessage(7, refreshHistory));

      await act(async () => {
        await result.current.handleSend();
      });

      const streamingId = useChatStore.getState().streamingMessageId;
      expect(streamingId).toBeTruthy();

      await act(async () => {
        capture.trigger.error(new Error("boom"));
      });

      // An empty answer — even a failed one — is never persisted.
      expect(apiMocks.addChatMessagesBatch).not.toHaveBeenCalled();
      expect(refreshHistory).not.toHaveBeenCalled();

      const assistant = useChatStore.getState().messagesById[streamingId!];
      expect(assistant?.error).toBe("The model returned an empty response. Try again.");
    });

    it("exposes the in-flight turn save on the store and clears it when settled (PRR-003)", async () => {
      const capture = installCapturingStreamMock();
      let resolveBatch!: (saved: unknown[]) => void;
      apiMocks.addChatMessagesBatch.mockReturnValueOnce(
        new Promise<unknown[]>((resolve) => {
          resolveBatch = resolve;
        })
      );
      const refreshHistory = vi.fn().mockResolvedValue(undefined);
      useChatStore.setState({ activeChatId: "42", input: "slow save" });

      const { result } = renderHook(() => useSendMessage(7, refreshHistory));

      await act(async () => {
        await result.current.handleSend();
      });

      await act(async () => {
        capture.trigger.message("answer");
        capture.trigger.complete();
      });

      // While the batch save is in flight it is registered on the store so
      // revision operations (retry/edit truncate, fork) can await it instead
      // of racing it.
      const pending = useChatStore.getState().pendingTurnPersist;
      expect(pending).toBeInstanceOf(Promise);
      expect(apiMocks.addChatMessagesBatch).toHaveBeenCalledTimes(1);

      await act(async () => {
        resolveBatch([
          { id: 200, created_at: "2026-06-01T00:00:00Z" },
          { id: 201, created_at: "2026-06-01T00:00:01Z" },
        ]);
        await pending;
      });

      await waitFor(() => {
        expect(useChatStore.getState().pendingTurnPersist).toBeNull();
      });
    });
  });
});
