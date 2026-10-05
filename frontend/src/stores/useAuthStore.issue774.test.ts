/**
 * Issue #774 regression tests (non-frozen): the auth store's session
 * retention on transport-class failures. A network error, timeout, or 5xx
 * during token refresh (or init's fetchMe) must keep the session; only an
 * auth-shaped 401/403 clears it.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";

const { mockPostFn, mockGetFn, mockRefreshAccessToken, mockSetJwtAccessToken } = vi.hoisted(() => ({
  mockPostFn: vi.fn(),
  mockGetFn: vi.fn(),
  mockRefreshAccessToken: vi.fn(),
  mockSetJwtAccessToken: vi.fn(),
}));

vi.mock("axios", () => ({
  default: {
    create: vi.fn(() => ({
      get: mockGetFn,
      post: mockPostFn,
      patch: vi.fn(),
      put: vi.fn(),
      delete: vi.fn(),
      interceptors: {
        request: { use: vi.fn((cb) => cb) },
        response: { use: vi.fn((cb) => cb) },
      },
    })),
  },
}));

vi.mock("@/lib/api", () => ({
  API_BASE_URL: "/api",
  setJwtAccessToken: mockSetJwtAccessToken,
  getJwtAccessToken: vi.fn(() => null),
  refreshAccessToken: mockRefreshAccessToken,
  resetCsrfToken: vi.fn(),
  ensureCsrfToken: vi.fn().mockResolvedValue("csrf"),
  resetSubpathRefreshDiagnostic: vi.fn(),
  attachCsrfInterceptor: vi.fn(),
}));

vi.mock("@/stores/useVaultStore", () => ({
  useVaultStore: {
    getState: vi.fn(() => ({ fetchVaults: vi.fn().mockResolvedValue(undefined) })),
    setState: vi.fn(),
    subscribe: vi.fn(() => vi.fn()),
  },
}));

vi.mock("@/lib/api/onboarding", () => ({
  resetCitationReport: vi.fn(),
}));

import { useAuthStore, resetInitState } from "./useAuthStore";

const mockUser = {
  id: 1,
  username: "tester",
  full_name: "Test User",
  role: "superadmin" as const,
  is_active: true,
};

beforeEach(() => {
  vi.clearAllMocks();
  resetInitState();
  useAuthStore.setState({
    user: mockUser,
    accessToken: "live-jwt",
    isAuthenticated: true,
    isLoading: false,
    isInitialized: false,
  });
});

describe("issue 774 session retention on transport failures", () => {
  it("keeps the session when a 5xx refresh rejects (outage, not a session verdict)", async () => {
    mockRefreshAccessToken.mockRejectedValueOnce(
      new Error("auth refresh failed with status 503")
    );

    const result = await useAuthStore.getState().refreshToken();

    expect(result).toBeNull();
    const state = useAuthStore.getState();
    expect(state.isAuthenticated).toBe(true);
    expect(state.user).toEqual(mockUser);
    expect(state.accessToken).toBe("live-jwt");
  });

  it("keeps the session when the refresh times out", async () => {
    mockRefreshAccessToken.mockRejectedValueOnce(new Error("auth refresh timed out"));

    const result = await useAuthStore.getState().refreshToken();

    expect(result).toBeNull();
    expect(useAuthStore.getState().isAuthenticated).toBe(true);
  });

  it("init keeps the session when fetchMe fails with a network error", async () => {
    // init path: in-memory accessToken -> fetchMe -> network failure.
    mockGetFn.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    // checkSetupStatus also fails (same outage) — the finally path must cope.
    mockGetFn.mockRejectedValueOnce(new TypeError("Failed to fetch"));

    await useAuthStore.getState().init();

    const state = useAuthStore.getState();
    expect(state.isAuthenticated).toBe(true);
    expect(state.user).toEqual(mockUser);
    expect(state.accessToken).toBe("live-jwt");
    expect(state.isInitialized).toBe(true);
  });

  it("init clears the session when fetchMe is rejected with 401", async () => {
    mockGetFn.mockRejectedValueOnce({
      response: { status: 401, data: { detail: "token_invalid" } },
      isAxiosError: true,
    });
    mockGetFn.mockResolvedValueOnce({
      data: { needs_setup: false, auth_mode: "jwt" },
    });

    await useAuthStore.getState().init();

    const state = useAuthStore.getState();
    expect(state.isAuthenticated).toBe(false);
    expect(state.user).toBeNull();
    expect(state.accessToken).toBeNull();
  });
});
