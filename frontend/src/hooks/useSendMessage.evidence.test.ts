// Candidate-evidence wiring in useSendMessage (issue #508 / CHAT-UX-04):
// streamed candidates attach ONLY to the message their send is actively
// streaming, and they are cleared at every terminal path (done / stop /
// error) so a cancelled send's late events can never contaminate a later one.
import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useSendMessage } from "./useSendMessage";
import { useChatStore, type Message } from "@/stores/useChatStore";
import { useChatShellStore } from "@/stores/useChatShellStore";
import { useLlmHealthStore } from "@/stores/useLlmHealthStore";
import type { Source } from "@/lib/api";

const apiMocks = vi.hoisted(() => ({
  createChatSession: vi.fn(),
  addChatMessagesBatch: vi.fn(),
  chatStream: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  createChatSession: (...args: unknown[]) => apiMocks.createChatSession(...args),
  addChatMessagesBatch: (...args: unknown[]) => apiMocks.addChatMessagesBatch(...args),
  chatStream: (...args: unknown[]) => apiMocks.chatStream(...args),
}));

type StreamHandlers = {
  onMessage: (chunk: string) => void;
  onEvidenceCandidates?: (candidates: Source[]) => void;
  onError: (error: Error) => void;
  onComplete: () => Promise<void> | void;
};

// Capture EVERY handler set the mock receives so a test can fire events from
// an EARLIER (possibly cancelled) send after a later send has started.
function installCapturingStreamMock(): { captured: StreamHandlers[] } {
  const captured: StreamHandlers[] = [];
  apiMocks.chatStream.mockImplementation((_messages: unknown, handlers: StreamHandlers) => {
    captured.push(handlers);
    return vi.fn();
  });
  return { captured };
}

const makeCandidate = (id: string): Source => ({
  id,
  filename: `${id}.pdf`,
  source_label: "S1",
  snippet: `snippet for ${id}`,
});

const messagesWithCandidates = (): string[] => {
  const { messageIds, messagesById } = useChatStore.getState();
  return messageIds.filter((id) => messagesById[id]?.candidateSources !== undefined);
};

describe("useSendMessage — evidence candidates", () => {
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
    apiMocks.addChatMessagesBatch.mockResolvedValue([
      { id: 100, created_at: "2026-09-06T00:00:00Z" },
      { id: 101, created_at: "2026-09-06T00:00:01Z" },
    ]);
  });

  it("sets candidateSources on the active streaming message and clears them on done", async () => {
    const { captured } = installCapturingStreamMock();
    const refreshHistory = vi.fn().mockResolvedValue(undefined);
    useChatStore.setState({ activeChatId: "42", input: "question" });

    const { result } = renderHook(() => useSendMessage(7, refreshHistory));
    await act(async () => {
      await result.current.handleSend();
    });

    const streamingId = useChatStore.getState().streamingMessageId;
    expect(streamingId).toBeTruthy();

    await act(async () => {
      captured[0].onEvidenceCandidates?.([makeCandidate("c1"), makeCandidate("c2")]);
    });
    const midStream = useChatStore.getState().messagesById[streamingId!];
    expect(midStream?.candidateSources?.map((c) => c.id)).toEqual(["c1", "c2"]);

    await act(async () => {
      captured[0].onMessage("final answer");
      await captured[0].onComplete();
    });

    await waitFor(() => {
      expect(refreshHistory).toHaveBeenCalledWith(true);
    });
    // The done event delivered final sources — the streaming-only candidate
    // preview must be gone.
    const doneMessage = useChatStore.getState().messagesById["101"] as Message | undefined;
    expect(doneMessage?.candidateSources).toBeUndefined();
    expect(messagesWithCandidates()).toEqual([]);
  });

  it("clears candidateSources when the send is stopped", async () => {
    const { captured } = installCapturingStreamMock();
    const refreshHistory = vi.fn().mockResolvedValue(undefined);
    useChatStore.setState({ activeChatId: "42", input: "stop me" });

    const { result } = renderHook(() => useSendMessage(7, refreshHistory));
    await act(async () => {
      await result.current.handleSend();
    });

    const streamingId = useChatStore.getState().streamingMessageId;
    await act(async () => {
      captured[0].onEvidenceCandidates?.([makeCandidate("c1")]);
      captured[0].onMessage("partial");
    });
    expect(
      useChatStore.getState().messagesById[streamingId!]?.candidateSources
    ).toHaveLength(1);

    await act(async () => {
      result.current.handleStop();
    });

    expect(
      useChatStore.getState().messagesById[streamingId!]?.candidateSources
    ).toBeUndefined();
    expect(messagesWithCandidates()).toEqual([]);
  });

  it("a cancelled send's late candidates never attach to the next send's message", async () => {
    const { captured } = installCapturingStreamMock();
    const refreshHistory = vi.fn().mockResolvedValue(undefined);
    useChatStore.setState({ activeChatId: "42", input: "one" });

    const { result } = renderHook(() => useSendMessage(7, refreshHistory));
    await act(async () => {
      await result.current.handleSend();
    });
    const firstId = useChatStore.getState().streamingMessageId;
    expect(firstId).toBeTruthy();

    await act(async () => {
      result.current.handleStop();
    });
    expect(useChatStore.getState().streamingMessageId).toBeNull();

    useChatStore.setState({ input: "two" });
    await act(async () => {
      await result.current.handleSend();
    });
    const secondId = useChatStore.getState().streamingMessageId;
    expect(secondId).toBeTruthy();

    // Send 1's stream is still alive transport-wise and delivers its
    // candidates AFTER send 2 started streaming.
    await act(async () => {
      captured[0].onEvidenceCandidates?.([makeCandidate("late-from-send-1")]);
    });

    expect(messagesWithCandidates()).toEqual([]);
    expect(
      useChatStore.getState().messagesById[secondId!]?.candidateSources
    ).toBeUndefined();
  });

  it("a cancelled send's candidates arriving after the next send completed leave saved state clean", async () => {
    const { captured } = installCapturingStreamMock();
    const refreshHistory = vi.fn().mockResolvedValue(undefined);
    useChatStore.setState({ activeChatId: "42", input: "one" });

    const { result } = renderHook(() => useSendMessage(7, refreshHistory));
    await act(async () => {
      await result.current.handleSend();
    });

    await act(async () => {
      result.current.handleStop();
    });

    useChatStore.setState({ input: "two" });
    await act(async () => {
      await result.current.handleSend();
    });

    // Send 2 completes and is persisted (id migrated to the server id).
    await act(async () => {
      captured[1].onMessage("answer two");
      await captured[1].onComplete();
    });
    await waitFor(() => {
      expect(refreshHistory).toHaveBeenCalledWith(true);
    });

    // Send 1's very late candidates arrive only now — nothing may change.
    await act(async () => {
      captured[0].onEvidenceCandidates?.([makeCandidate("very-late")]);
    });

    expect(messagesWithCandidates()).toEqual([]);
    const assistantPayload = (
      apiMocks.addChatMessagesBatch.mock.calls[0][1] as Array<Record<string, unknown>>
    ).find((m) => m.role === "assistant");
    expect(assistantPayload).toBeDefined();
    expect(Object.keys(assistantPayload!)).not.toContain("candidateSources");
  });
});
