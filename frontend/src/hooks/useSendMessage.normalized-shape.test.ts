// frontend/src/hooks/useSendMessage.normalized-shape.test.ts
// Issue #1078 AC1: the send path's session-create 403 arm must fire for the
// error shape the api client ACTUALLY rejects with. core.ts's response
// interceptor returns a normalized Error whose status is at the TOP level
// and which has NO `.response` property (`originalError` nests the raw
// axios error). useSendMessage's catch read only `err.response.status`, so
// the permission branch was dead in production — the user always saw the
// generic connection message on a 403.
//
// Contract asserted at BOTH ends (the ActivityTray.normalized-403 pattern):
// the normalized legs (top-level `status`, and the re-wrapped
// `originalError.response.status`-only tier) are RED at base and GREEN
// after the fix; the raw-shape control leg is green at base and must stay
// green (AC2 — the widening is a superset); the non-403 leg keeps today's
// generic message.
//
// "@/lib/api" is mocked for the endpoint fns; "@/lib/api/core" is NOT
// mocked, so the reader under test runs the production shared helper.

import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useSendMessage } from "./useSendMessage";
import * as useSendMessageModule from "./useSendMessage";
import { useChatStore } from "@/stores/useChatStore";
import { useChatModeStore } from "@/stores/useChatModeStore";
import { useChatShellStore } from "@/stores/useChatShellStore";
import { useLlmHealthStore } from "@/stores/useLlmHealthStore";

const apiMocks = vi.hoisted(() => ({
  createChatSession: vi.fn(),
  addChatMessage: vi.fn(),
  addChatMessagesBatch: vi.fn(),
  chatStream: vi.fn(),
  getLlmModeHealth: vi.fn(),
  getSettings: vi.fn(async () => ({ chat_message_max_chars: 20000 })),
}));

vi.mock("@/lib/api", () => ({
  getSettings: (...args: unknown[]) => apiMocks.getSettings(...(args as [])),
  createChatSession: (...args: unknown[]) => apiMocks.createChatSession(...args),
  addChatMessage: (...args: unknown[]) => apiMocks.addChatMessage(...args),
  addChatMessagesBatch: (...args: unknown[]) => apiMocks.addChatMessagesBatch(...args),
  chatStream: (...args: unknown[]) => apiMocks.chatStream(...args),
  getLlmModeHealth: (...args: unknown[]) => apiMocks.getLlmModeHealth(...args),
}));

/**
 * Replica of core.ts's interceptor normalizer tail (the shape every
 * apiClient rejection carries): top-level `status`, raw axios error under
 * `originalError`, and NO `response` property on the error itself.
 */
function normalizedReject(status: number): Error {
  const error = new Error(`Request failed with status code ${status}`);
  error.name = "APIError";
  (error as unknown as { status?: number }).status = status;
  (error as unknown as { originalError?: unknown }).originalError = {
    response: { status },
  };
  return error;
}

/** The re-wrapped tier: status only under `originalError.response.status`. */
function originalTierReject(status: number): Error {
  const error = new Error(`Request failed with status code ${status}`);
  error.name = "APIError";
  (error as unknown as { originalError?: unknown }).originalError = {
    response: { status },
  };
  return error;
}

const PERMISSION_MESSAGE = "You don't have permission to chat in this vault.";
const GENERIC_MESSAGE = "Failed to start chat session. Please check your connection.";

describe("useSendMessage session-create 403 handling (issue #1078 normalized shape)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // Issue #991: clear the per-owner cap memo between cases (order
    // independence; no-op on pre-fix trees).
    (useSendMessageModule as { __resetChatInputCapForTests?: () => void })
      .__resetChatInputCapForTests?.();
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
    useChatModeStore.setState({ scopeDocumentIds: null });
  });

  async function sendWithError(rejection: unknown): Promise<"sent" | "blocked"> {
    apiMocks.createChatSession.mockReset();
    apiMocks.createChatSession.mockRejectedValue(rejection);
    useChatStore.setState({ input: "hello" });

    const { result } = renderHook(() => useSendMessage(7, vi.fn().mockResolvedValue(undefined)));
    let sent: "sent" | "blocked" | undefined;
    await act(async () => {
      sent = await result.current.handleSend();
    });
    return sent ?? "blocked";
  }

  it("surfaces the permission message for a NORMALIZED 403 (top-level status)", async () => {
    const sent = await sendWithError(normalizedReject(403));

    expect(sent).toBe("blocked");
    expect(useChatStore.getState().inputError).toBe(PERMISSION_MESSAGE);
  });

  it("surfaces the permission message when the 403 only survives under originalError.response.status (re-wrapped tier)", async () => {
    const sent = await sendWithError(originalTierReject(403));

    expect(sent).toBe("blocked");
    expect(useChatStore.getState().inputError).toBe(PERMISSION_MESSAGE);
  });

  it("still surfaces the permission message for the RAW axios shape { response: { status: 403 } } (control leg — superset)", async () => {
    const sent = await sendWithError({ response: { status: 403 } });

    expect(sent).toBe("blocked");
    expect(useChatStore.getState().inputError).toBe(PERMISSION_MESSAGE);
  });

  it("keeps the generic connection message for a normalized non-403 failure", async () => {
    const sent = await sendWithError(normalizedReject(500));

    expect(sent).toBe("blocked");
    expect(useChatStore.getState().inputError).toBe(GENERIC_MESSAGE);
  });
});
