// Issue #823 ChatShell identity-switch integration (unfrozen, critic rounds
// 2-6). Composition contract: the frozen C4 check proves the REAL logout
// clears useChatStore.activeChatId; the auth identitySwitch suite proves the
// full A→logout→B-login transition; THIS file proves what ChatShell does at
// each end of it:
//   1. (exposure mechanism pin) with A's session live in the store, mounting
//      /chat/:id serves it from memory — getChatSession is NOT called (the
//      fast path the fix disarms);
//   2. after the identity boundary (logout; the store state logout leaves),
//      a fresh mount at /chat/:id MUST call getChatSession under the new
//      identity and MUST NOT render A's rows;
//   3. a 403/404 from that fetch falls back to the safe new-chat state —
//      user B never sees user A's transcript.
//
// Auth transport mocking mirrors useAuthStore.logoutChatState.issue823.test.ts;
// the chat store is the REAL module; ChatShell's shell store is mocked per
// ChatShell.test.tsx.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, cleanup } from "@testing-library/react";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const { mockPostFn, mockGetFn, mockPatchFn, mockResetCsrfToken, mockEnsureCsrfToken, mockResetSubpathRefreshDiagnostic, mockResetCitationReport, getChatSessionMock } = vi.hoisted(() => ({
  mockPostFn: vi.fn(),
  mockGetFn: vi.fn(),
  mockPatchFn: vi.fn(),
  mockResetCsrfToken: vi.fn(),
  mockEnsureCsrfToken: vi.fn().mockResolvedValue("mock-csrf-token"),
  mockResetSubpathRefreshDiagnostic: vi.fn(),
  mockResetCitationReport: vi.fn(),
  getChatSessionMock: vi.fn(async () => ({ messages: [] })),
}));

const { mockJwtTokenHolder } = vi.hoisted(() => ({
  mockJwtTokenHolder: { value: null as string | null },
}));

vi.mock("axios", () => ({
  default: {
    create: vi.fn(() => ({
      get: mockGetFn,
      post: mockPostFn,
      patch: mockPatchFn,
      put: vi.fn(),
      delete: vi.fn(),
      interceptors: {
        request: { use: vi.fn((cb) => cb) },
        response: { use: vi.fn((cb) => cb) },
      },
    })),
  },
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    chatStream: vi.fn(() => vi.fn()),
    getChatSession: getChatSessionMock,
    truncateChatSession: vi.fn(async () => ({ remaining_count: 0, tail_seq: 0 })),
    forkChatSession: vi.fn(async () => ({ id: 99, messages: [] })),
    listChatSessions: vi.fn(async () => ({ sessions: [] })),
    listAccessibleVaults: vi.fn(async () => ({ vaults: [] })),
    getLlmModeHealth: vi.fn(async () => ({ thinking: true, instant: true })),
  };
});

vi.mock("@/lib/api/canvas", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api/canvas")>(
    "@/lib/api/canvas"
  );
  return { ...actual, getCanvasCapabilities: vi.fn(async () => ({ enabled: false })) };
});

vi.mock("sonner", () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
    warning: vi.fn(),
    info: vi.fn(),
  },
}));

import ChatShell from "./ChatShell";
import { useAuthStore, resetInitState } from "@/stores/useAuthStore";
import { useChatStore } from "@/stores/useChatStore";
import { useChatShellStore } from "@/stores/useChatShellStore";

const USER_A = { id: 11, username: "usera", full_name: "User A", role: "admin" as const, is_active: true };

function renderAtChat7() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/chat/7"]}>
        <Routes>
          <Route path="/chat/:sessionId" element={<ChatShell />} />
          <Route path="*" element={<div>fallback route</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

function seedUserA() {
  useAuthStore.setState({
    user: USER_A,
    accessToken: "jwt-a",
    isAuthenticated: true,
    authMode: "jwt",
  });
  useChatStore.getState().loadChat("7", [
    { id: "a-user", role: "user", content: "user A private question" },
    { id: "a-assistant", role: "assistant", content: "user A private answer" },
  ]);
}

describe("ChatShell across an identity switch (issue #823)", () => {
  beforeEach(async () => {
    vi.clearAllMocks();
    mockEnsureCsrfToken.mockResolvedValue("mock-csrf-token");
    mockJwtTokenHolder.value = null;
    mockGetFn.mockResolvedValue({ data: { csrf_token: "mock-csrf" } });
    resetInitState();
    useAuthStore.setState({
      user: null,
      accessToken: null,
      isAuthenticated: false,
      isInitialized: true,
      initializationFailed: false,
      isLoading: false,
      needsSetup: false,
      authMode: "unknown",
    });
    useChatStore.getState().newChat();
    const { useChatShellStore } = await import("@/stores/useChatShellStore");
    useChatShellStore.setState({ activeSessionId: null, activeSessionTitle: null });
    const localStorageMock = {
      getItem: vi.fn().mockReturnValue(null),
      setItem: vi.fn(),
      removeItem: vi.fn(),
      clear: vi.fn(),
      key: vi.fn().mockReturnValue(null),
      length: 0,
    };
    Object.defineProperty(window, "localStorage", { value: localStorageMock, configurable: true });
  });

  afterEach(() => {
    cleanup();
    resetInitState();
  });

  it("exposure mechanism pin: a live activeChatId serves the session from memory without a fetch", async () => {
    seedUserA();
    getChatSessionMock.mockClear();

    renderAtChat7();

    // Give any stray effects a beat: the fast path must hold — no fetch.
    await new Promise((r) => setTimeout(r, 50));
    expect(getChatSessionMock).not.toHaveBeenCalled();
    expect(useChatStore.getState().activeChatId).toBe("7");
  });

  it("after logout, a fresh mount at /chat/7 refetches under the new identity and drops A's rows", async () => {
    seedUserA();
    mockPostFn.mockResolvedValueOnce({ data: {} }); // logout POST
    getChatSessionMock.mockResolvedValueOnce({
      messages: [
        { id: 1, role: "user", content: "user B question", seq: 1, created_at: "2026-10-27T00:00:00Z", sources: null },
      ],
    });

    // User A leaves: the REAL logout clears the per-user chat state.
    await useAuthStore.getState().logout();
    expect(useChatStore.getState().activeChatId).toBeNull();

    // User B lands on the same deep link in this tab.
    const { unmount } = renderAtChat7();
    await waitFor(() => {
      expect(getChatSessionMock).toHaveBeenCalledWith(7);
    });
    await waitFor(() => {
      expect(useChatStore.getState().activeChatId).toBe("7");
    });
    // B sees B's session — A's content is gone from the store the pane reads.
    expect(JSON.stringify(useChatStore.getState().messagesById)).not.toContain("user A private");
    unmount();
  });

  it("a 403 for the refetched session falls back to the safe new-chat state (no A content)", async () => {
    seedUserA();
    mockPostFn.mockResolvedValueOnce({ data: {} });
    await useAuthStore.getState().logout();

    const err = Object.assign(new Error("forbidden"), { response: { status: 403 } });
    getChatSessionMock.mockRejectedValueOnce(err);

    const { unmount } = renderAtChat7();
    await waitFor(() => {
      expect(getChatSessionMock).toHaveBeenCalledWith(7);
    });
    // The failure fallback leaves no previously-active session as the send
    // target and no A rows in the store (issue #685 T1-13-S-03 contract).
    await waitFor(() => {
      expect(useChatStore.getState().messageIds).toEqual([]);
    });
    expect(JSON.stringify(useChatStore.getState().messagesById)).not.toContain("user A private");
    unmount();
  });
});
