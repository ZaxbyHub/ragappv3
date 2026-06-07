import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useSendMessage } from "./useSendMessage";
import { useChatStore } from "@/stores/useChatStore";
import { useChatShellStore } from "@/stores/useChatShellStore";
import { useLlmHealthStore } from "@/stores/useLlmHealthStore";

const apiMocks = vi.hoisted(() => ({
  createChatSession: vi.fn(),
  addChatMessage: vi.fn(),
  chatStream: vi.fn(),
  getLlmModeHealth: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  createChatSession: (...args: unknown[]) => apiMocks.createChatSession(...args),
  addChatMessage: (...args: unknown[]) => apiMocks.addChatMessage(...args),
  chatStream: (...args: unknown[]) => apiMocks.chatStream(...args),
  getLlmModeHealth: (...args: unknown[]) => apiMocks.getLlmModeHealth(...args),
}));

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
    });
    useLlmHealthStore.setState({ thinking: true, instant: true });
    useChatShellStore.setState({ sessionListRefreshToken: 0 });
    apiMocks.createChatSession.mockResolvedValue({ id: 42 });
    apiMocks.addChatMessage
      .mockResolvedValueOnce({ id: 100, created_at: "2026-05-12T00:00:00Z" })
      .mockResolvedValueOnce({ id: 101, created_at: "2026-05-12T00:00:01Z" });
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
    expect(apiMocks.addChatMessage).toHaveBeenCalledWith(42, {
      role: "user",
      content: "What changed?",
    });
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
    type StreamHandlers = {
      onMessage: (chunk: string) => void;
      onSources: (sources: unknown[]) => void;
      onMemories: (memories: unknown[]) => void;
      onWiki: (wikiRefs: unknown[]) => void;
      onKMS: (kmsRefs: unknown[]) => void;
      onMode: (mode: string) => void;
      onError: (error: Error) => void;
      onComplete: () => Promise<void> | void;
    };

    // Install a chatStream mock that captures the handlers so each test can
    // fire them at will. The captured handler is read from a mutable cell
    // so subsequent mock invocations (e.g. retries) update the reference
    // and trigger calls operate on the latest invocation.
    function installCapturingStreamMock(): { trigger: { error: (e: Error) => void; complete: () => void } } {
      const cell: { current: StreamHandlers | null } = { current: null };
      apiMocks.chatStream.mockImplementation((_messages: unknown, handlers: StreamHandlers) => {
        cell.current = handlers;
        return vi.fn(); // abort function
      });
      return {
        trigger: {
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
        capture.trigger.error(new TypeError("Failed to fetch"));
      });

      await waitFor(() => {
        expect(useChatStore.getState().isStreaming).toBe(false);
      });
      expect(useChatStore.getState().abortFn).toBeNull();
      expect(useChatStore.getState().streamingMessageId).toBeNull();

      const assistant = useChatStore.getState().messagesById[streamingId!];
      expect(assistant?.error).toBe(
        "Connection lost. Check your network and try again."
      );
      // refreshHistory is only called from onComplete, which never fired.
      expect(refreshHistory).not.toHaveBeenCalled();
    });

    it("non-network, non-abort error: stamps the raw error.message on the assistant message", async () => {
      const capture = installCapturingStreamMock();
      const refreshHistory = vi.fn().mockResolvedValue(undefined);
      useChatStore.setState({ activeChatId: "42", input: "Will fail with server error" });

      const { result } = renderHook(() => useSendMessage(7, refreshHistory));

      await act(async () => {
        await result.current.handleSend();
      });

      const streamingId = useChatStore.getState().streamingMessageId;

      await act(async () => {
        capture.trigger.error(new Error("upstream LLM returned 500"));
      });

      await waitFor(() => {
        expect(useChatStore.getState().isStreaming).toBe(false);
      });
      const assistant = useChatStore.getState().messagesById[streamingId!];
      expect(assistant?.error).toBe("upstream LLM returned 500");
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
});
