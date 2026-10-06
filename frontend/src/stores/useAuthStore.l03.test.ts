/**
 * Issue #774 L03 red checkpoint — a network error during silent token refresh
 * must not log the user out. refreshToken()'s catch clears user / accessToken
 * / isAuthenticated on ANY rejection, but a fetch rejection (offline, VPN
 * drop, backend restart) says nothing about session validity — the refresh
 * cookie may still be perfectly good on the next attempt.
 *
 * Mirrors the mock layout of ./useAuthStore.test.ts (axios + @/lib/api +
 * @/lib/api/onboarding + @/stores/useVaultStore).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

const { mockPostFn, mockGetFn, mockPatchFn } = vi.hoisted(() => ({
  mockPostFn: vi.fn(),
  mockGetFn: vi.fn(),
  mockPatchFn: vi.fn(),
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

vi.mock("@/lib/api", () => ({
  API_BASE_URL: "/api",
  setJwtAccessToken: vi.fn(),
  getJwtAccessToken: vi.fn(() => null),
  refreshAccessToken: vi.fn(),
  resetCsrfToken: vi.fn(),
  ensureCsrfToken: vi.fn().mockResolvedValue("mock-csrf-token"),
  resetSubpathRefreshDiagnostic: vi.fn(),
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
  resetCitationReport: vi.fn(),
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

import { useAuthStore, resetInitState } from "./useAuthStore";
import { refreshAccessToken } from "@/lib/api";

describe("issue 774 refreshToken network error", () => {
  const mockUser = {
    id: 1,
    username: "testuser",
    full_name: "Test User",
    role: "admin" as const,
    is_active: true,
  };

  beforeEach(() => {
    resetInitState();
    useAuthStore.setState({
      user: mockUser,
      accessToken: "tok",
      isAuthenticated: true,
      isInitialized: false,
      isLoading: false,
      needsSetup: false,
      authMode: "jwt",
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
    resetInitState();
  });

  it("keeps the session on a network error during token refresh", async () => {
    // A network failure is fetch rejecting — not the server rejecting the
    // refresh cookie. refreshAccessToken surfaces it as a thrown TypeError.
    vi.mocked(refreshAccessToken).mockRejectedValue(
      new TypeError("Failed to fetch"),
    );

    await useAuthStore.getState().refreshToken();

    // The session must survive: the next request can retry the refresh.
    expect(useAuthStore.getState().isAuthenticated).toBe(true);
  });
});
