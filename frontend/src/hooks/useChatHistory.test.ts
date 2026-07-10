import { afterEach, describe, expect, it, vi, beforeEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";

vi.mock("@/lib/api", () => ({
  listChatSessions: vi.fn(),
  getChatSession: vi.fn(),
}));

vi.mock("@/stores/useChatStore", () => ({
  useChatStore: {
    getState: vi.fn(() => ({ isStreaming: false, loadChat: vi.fn() })),
  },
}));

import { useChatHistory } from "./useChatHistory";
import { listChatSessions, getChatSession } from "@/lib/api";

const mockedListChatSessions = vi.mocked(listChatSessions);
const mockedGetChatSession = vi.mocked(getChatSession);

describe("useChatHistory", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it("loads chat history on mount and exposes the sessions", async () => {
    mockedListChatSessions.mockResolvedValue({
      sessions: [{ id: 1, title: "S1" }, { id: 2, title: "S2" }],
    } as any);

    const { result } = renderHook(() => useChatHistory(42));

    await waitFor(() => expect(result.current.isChatLoading).toBe(false));
    expect(result.current.chatHistory).toHaveLength(2);
    expect(result.current.chatHistoryError).toBeNull();
    expect(mockedListChatSessions).toHaveBeenCalledWith(42);
  });

  it("surfaces an error message when the list fetch fails", async () => {
    // Use a distinct vault id so the module-level cache (keyed by vault id)
    // doesn't return a prior test's success entry.
    mockedListChatSessions.mockRejectedValue(new Error("network down"));

    const { result } = renderHook(() => useChatHistory(999));

    await waitFor(() => expect(result.current.isChatLoading).toBe(false));
    expect(result.current.chatHistoryError).toBe("network down");
    expect(result.current.chatHistory).toEqual([]);
  });

  it("refreshHistory(force) bypasses the cache and refetches", async () => {
    mockedListChatSessions
      .mockResolvedValueOnce({ sessions: [{ id: 1, title: "old" }] } as any)
      .mockResolvedValueOnce({ sessions: [{ id: 1, title: "old" }, { id: 2, title: "new" }] } as any);

    const { result } = renderHook(() => useChatHistory(7));
    await waitFor(() => expect(result.current.chatHistory).toHaveLength(1));

    await result.current.refreshHistory(true);
    await waitFor(() => expect(result.current.chatHistory).toHaveLength(2));
    expect(mockedListChatSessions).toHaveBeenCalledTimes(2);
  });

  it("handleLoadChat fetches session detail and loads messages into the store", async () => {
    mockedListChatSessions.mockResolvedValue({ sessions: [] } as any);
    mockedGetChatSession.mockResolvedValue({
      messages: [
        { id: 10, role: "user", content: "hi", sources: null, memories: null, created_at: "t", feedback: null },
      ],
    } as any);

    const { result } = renderHook(() => useChatHistory(1));
    await waitFor(() => expect(result.current.isChatLoading).toBe(false));

    await result.current.handleLoadChat({ id: 99, title: "x" } as any);
    expect(mockedGetChatSession).toHaveBeenCalledWith(99);
  });
});
