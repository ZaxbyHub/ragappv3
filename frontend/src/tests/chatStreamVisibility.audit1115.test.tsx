// frontend/src/tests/chatStreamVisibility.audit1115.test.tsx
// Issue #1153 AC3 (arm's-length check C3, round-1 critic revision): a
// streamed CONTENT event must be visible on the assistant message BEFORE the
// stream completes. The pipeline under pin has no completion gate:
//
//   chatStream onMessage (lib/api/sessions.ts)
//     -> useSendMessage's streamedContent mirror + useCoalescedAppend buffer
//     -> the content-sync effect (useSendMessage.ts)
//     -> useChatStore.updateMessage
//     -> MessageRow -> AssistantMessage (renders the content live)
//
// Two legs, both against the REAL useSendMessage hook into the REAL chat
// store with the api barrel mocked at the module boundary (the issue #553
// harness pattern). The chatStream mock hands the test the live stream
// callbacks and NEVER fires onComplete, so any visibility can only come from
// the mid-stream pipeline, not from a terminal write:
//
//  - STORE leg: the streaming row (useChatStore's streamingMessageId
//    message) already contains the chunk while isStreaming is true and no
//    persist call has been made.
//  - DOM leg (critic round 1): the same store row RENDERED LIVE through the
//    real AssistantMessage -- the exact component the transcript's MessageRow
//    renders -- shows the chunk in the DOM while the stream is still open.
//    The probe replicates MessageRow's production wiring (useMessage
//    subscription), so the DOM updates from the store mid-stream.
//
// Preserving pin: both legs hold at base and must keep holding after the
// #1153 changes (session scoping, empty-state copy, follow-up topics).

import { act, cleanup, render, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createElement } from "react";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { useSendMessage, __resetChatInputCapForTests } from "@/hooks/useSendMessage";
import { useChatStore, useMessage } from "@/stores/useChatStore";
import { useChatModeStore } from "@/stores/useChatModeStore";
import { useChatShellStore } from "@/stores/useChatShellStore";
import { useLlmHealthStore } from "@/stores/useLlmHealthStore";
import { AssistantMessage } from "@/components/chat/AssistantMessage";

const apiMocks = vi.hoisted(() => ({
  createChatSession: vi.fn(),
  addChatMessagesBatch: vi.fn(),
  addChatMessagesBatchKeepalive: vi.fn(),
  chatStream: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  getSettings: async () => ({ chat_message_max_chars: 20000 }),
  createChatSession: (...args: unknown[]) => apiMocks.createChatSession(...(args as [])),
  addChatMessagesBatch: (...args: unknown[]) =>
    apiMocks.addChatMessagesBatch(...(args as [])),
  addChatMessagesBatchKeepalive: (...args: unknown[]) =>
    apiMocks.addChatMessagesBatchKeepalive(...(args as [])),
  chatStream: (...args: unknown[]) => apiMocks.chatStream(...(args as [])),
}));

// Unrelated capability hook on the real AssistantMessage path: it rides a
// react-query useQuery (canvas UI affordances), which has no provider in
// this harness. The DOM leg is about CONTENT rendering, so the hook is
// pinned to "canvas not visible".
vi.mock("@/hooks/useCanvasCapabilities", () => ({
  useCanvasVisible: () => false,
}));

interface StreamHandlers {
  onMessage: (chunk: string) => void;
  onComplete: () => void;
  onRetired?: () => void;
}

let streamHandlers: StreamHandlers | null = null;

function installStream(): void {
  apiMocks.chatStream.mockImplementation(
    (_messages: unknown, handlers: StreamHandlers) => {
      streamHandlers = handlers;
      return vi.fn();
    },
  );
}

/**
 * Production-shaped probe for the DOM leg: subscribes to the streaming row
 * the way TranscriptPane's MessageRow does (useMessage) and renders it
 * through the REAL AssistantMessage, so the DOM must update from the store
 * while the stream is open.
 */
function StreamingRowProbe(): ReactNode {
  const streamingId = useChatStore((s) => s.streamingMessageId);
  const message = useMessage(streamingId ?? "__none__");
  if (!message) return null;
  return createElement(AssistantMessage, {
    message,
    isStreaming: true,
    sessionId: "42",
    showDebug: false,
  });
}

/** Drive one send whose stream delivers a chunk and then STAYS OPEN. */
async function sendAndStreamOneChunk(): Promise<void> {
  act(() => {
    useChatStore.setState({ activeChatId: "42", activeSessionVaultId: 7, input: "hello" });
  });
  const { result } = renderHook(() => useSendMessage(7, vi.fn()));
  let sent: "sent" | "blocked" | undefined;
  await act(async () => {
    sent = await result.current.handleSend();
  });
  expect(sent).toBe("sent");
  expect(apiMocks.chatStream).toHaveBeenCalledTimes(1);

  // One content event arrives and the stream STAYS OPEN (no onComplete).
  await act(async () => {
    streamHandlers!.onMessage("Visible chunk");
  });
  // Let the rAF-coalesced append flush and the content-sync effect run.
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 120));
  });
}

describe("chat stream content visibility before completion (issue #1153 AC3)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    streamHandlers = null;
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
    installStream();
  });

  afterEach(() => {
    cleanup();
  });

  it("STORE leg: lands the content chunk on the streaming store row while the stream is open", async () => {
    await sendAndStreamOneChunk();

    const state = useChatStore.getState();
    // The stream has NOT completed: the turn is still live.
    expect(state.isStreaming).toBe(true);
    expect(state.streamingMessageId).not.toBeNull();

    // The content event is already on the assistant message the transcript
    // renders (MessageRow subscribes to this exact store row).
    const streaming = state.messagesById[state.streamingMessageId as string];
    expect(streaming).toBeDefined();
    expect(streaming.role).toBe("assistant");
    expect(streaming.content).toContain("Visible chunk");

    // Visibility did not wait for completion: nothing persisted yet.
    expect(apiMocks.addChatMessagesBatch).not.toHaveBeenCalled();
    expect(apiMocks.addChatMessagesBatchKeepalive).not.toHaveBeenCalled();
  });

  it("DOM leg: renders the content chunk through the real AssistantMessage while the stream is open", async () => {
    // Mount the live-row probe BEFORE the send: the probe must pick the
    // streaming row up and re-render it as content lands (no completion).
    const { container } = render(
      createElement(MemoryRouter, null, createElement(StreamingRowProbe)),
    );

    await sendAndStreamOneChunk();

    // The DOM shows the chunk -- this is what the user sees mid-stream.
    await waitFor(() => {
      expect(container.textContent ?? "").toContain("Visible chunk");
    });

    // Still mid-stream at the moment the DOM shows the content: the turn
    // never completed and nothing was persisted.
    expect(useChatStore.getState().isStreaming).toBe(true);
    expect(useChatStore.getState().streamingMessageId).not.toBeNull();
    expect(apiMocks.addChatMessagesBatch).not.toHaveBeenCalled();
    expect(apiMocks.addChatMessagesBatchKeepalive).not.toHaveBeenCalled();
  });
});
