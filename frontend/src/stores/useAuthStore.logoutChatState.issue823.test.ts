// frontend/src/stores/useAuthStore.logoutChatState.issue823.test.ts
// Issue #823 acceptance check (arm's-length, frozen):
//
// WHEN logout completes, per-user chat state must not survive the identity
// boundary: the chat store's rows + activeChatId and the shell store's
// activeSessionId must be cleared, so a different user logging in within the
// same tab and landing on /chat/:id cannot hit ChatShell's
// `activeChatId === sessionId` early-return and render the previous user's
// transcript from memory. RED at base (logout resets auth/CSRF/citation/init
// state but never touches the chat stores), GREEN after the fix.
//
// Transport mocking mirrors the established useAuthStore.test.ts pattern
// (axios mock + @/lib/api barrel mock + onboarding/vault-store mocks); the
// chat stores under assertion are the REAL modules.

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

const { mockPostFn, mockGetFn, mockPatchFn, mockResetCsrfToken, mockEnsureCsrfToken, mockResetSubpathRefreshDiagnostic, mockResetCitationReport } = vi.hoisted(() => ({
  mockPostFn: vi.fn(),
  mockGetFn: vi.fn(),
  mockPatchFn: vi.fn(),
  mockResetCsrfToken: vi.fn(),
  mockEnsureCsrfToken: vi.fn().mockResolvedValue("mock-csrf-token"),
  mockResetSubpathRefreshDiagnostic: vi.fn(),
  mockResetCitationReport: vi.fn(),
}));

const { mockJwtTokenHolder } = vi.hoisted(() => ({
  mockJwtTokenHolder: { value: null as string | null },
}));

// Mock axios before importing the store
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

// Keep the direct core import used by useAuthStore synchronized with the
// public API mock.
vi.mock("@/lib/api/core", () => ({
  getJwtAccessToken: vi.fn(() => mockJwtTokenHolder.value),
  onJwtAccessTokenPublished: vi.fn(() => () => {}),
}));

vi.mock("@/lib/api", () => ({
  API_BASE_URL: "/api",
  setJwtAccessToken: vi.fn((token: string | null) => {
    mockJwtTokenHolder.value = token;
  }),
  getJwtAccessToken: vi.fn(() => mockJwtTokenHolder.value),
  refreshAccessToken: vi.fn(),
  resetCsrfToken: mockResetCsrfToken,
  ensureCsrfToken: mockEnsureCsrfToken,
  ensureCsrfTokenPhysical: mockEnsureCsrfToken,
  resetSubpathRefreshDiagnostic: mockResetSubpathRefreshDiagnostic,
  attachCsrfInterceptor: vi.fn(),
  default: {
    get: vi.fn(),
    post: vi.fn(),
    patch: vi.fn(),
    interceptors: {
      request: { use: vi.fn() },
      response: { use: vi.fn() },
    },
  },
}));

vi.mock("@/lib/api/onboarding", () => ({
  resetCitationReport: mockResetCitationReport,
  getOnboardingMilestones: vi.fn(),
  markCitationOpened: vi.fn(),
  dismissChecklist: vi.fn(),
}));

const { mockFetchVaults } = vi.hoisted(() => ({
  mockFetchVaults: vi.fn(),
}));
vi.mock("@/stores/useVaultStore", () => ({
  useVaultStore: {
    getState: vi.fn(() => ({
      fetchVaults: mockFetchVaults,
    })),
  },
}));

// Import after mocks. The chat stores under assertion are the REAL modules —
// the whole point of this check is what the real logout does (or does not)
// reset in them.
import { useAuthStore, resetInitState } from "./useAuthStore";
import { useChatStore } from "@/stores/useChatStore";
import { useChatShellStore } from "@/stores/useChatShellStore";

describe("useAuthStore.logout clears per-user chat state (issue #823)", () => {
  const mockUser = {
    id: 1,
    username: "usera",
    full_name: "User A",
    role: "admin" as const,
    is_active: true,
  };

  const mockPost = mockPostFn;

  beforeEach(() => {
    mockPost.mockReset();
    mockGetFn.mockReset();
    mockPatchFn.mockReset();
    mockResetCsrfToken.mockReset();
    mockEnsureCsrfToken.mockReset();
    mockEnsureCsrfToken.mockResolvedValue("mock-csrf-token");
    mockResetCitationReport.mockReset();
    mockJwtTokenHolder.value = null;

    // Reset module-level init guard state
    resetInitState();

    useAuthStore.setState({
      user: null,
      accessToken: null,
      isAuthenticated: false,
      isInitialized: false,
      initializationFailed: false,
      isLoading: false,
      needsSetup: false,
      authMode: "unknown",
    });

    const localStorageMock = {
      getItem: vi.fn().mockReturnValue(null),
      setItem: vi.fn(),
      removeItem: vi.fn(),
      clear: vi.fn(),
    };
    Object.defineProperty(window, "localStorage", { value: localStorageMock });
  });

  afterEach(() => {
    vi.restoreAllMocks();
    resetInitState();
  });

  it("clears chat-store rows, activeChatId, and the shell's activeSessionId on a successful logout (RED at base)", async () => {
    // User A is logged in and has an active chat session in memory: the exact
    // state ChatShell's fast path (`activeChatId === sessionId`) serves
    // without a server re-fetch.
    useAuthStore.setState({
      user: mockUser,
      accessToken: "jwt123",
      isAuthenticated: true,
      authMode: "jwt",
    });
    useChatStore.getState().loadChat("7", [
      { id: "row-user", role: "user", content: "user A question" },
      { id: "row-assistant", role: "assistant", content: "user A answer" },
    ]);
    useChatShellStore.getState().setActiveSessionId("7");

    // Preconditions: the per-user chat state is live before logout.
    expect(useChatStore.getState().activeChatId).toBe("7");
    expect(useChatStore.getState().messageIds).toHaveLength(2);
    expect(useChatShellStore.getState().activeSessionId).toBe("7");

    mockPost.mockResolvedValueOnce({ data: {} });

    const { logout } = useAuthStore.getState();
    await logout();

    // Auth side (already correct at base).
    const auth = useAuthStore.getState();
    expect(auth.user).toBeNull();
    expect(auth.isAuthenticated).toBe(false);

    // Chat side: nothing per-user may survive the identity boundary —
    // RED at base (all three survive: logout never touches the chat stores).
    expect(useChatStore.getState().activeChatId).toBeNull();
    expect(useChatStore.getState().messageIds).toEqual([]);
    expect(useChatShellStore.getState().activeSessionId).toBeNull();
  });

  it("still clears the chat state when the server logout request fails (best-effort reset)", async () => {
    useAuthStore.setState({
      user: mockUser,
      accessToken: "jwt123",
      isAuthenticated: true,
      authMode: "jwt",
    });
    useChatStore.getState().loadChat("9", [
      { id: "row-user-9", role: "user", content: "user A question" },
    ]);
    useChatShellStore.getState().setActiveSessionId("9");

    mockPost.mockRejectedValueOnce(new Error("Network error"));

    const { logout } = useAuthStore.getState();
    await logout();

    expect(useAuthStore.getState().isAuthenticated).toBe(false);
    expect(useChatStore.getState().activeChatId).toBeNull();
    expect(useChatStore.getState().messageIds).toEqual([]);
    expect(useChatShellStore.getState().activeSessionId).toBeNull();
  });
});
