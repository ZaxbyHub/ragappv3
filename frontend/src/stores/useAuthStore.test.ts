import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

// Use vi.hoisted to make mock functions available at mock time
const { mockPostFn, mockGetFn, mockPatchFn, mockResetCsrfToken, mockEnsureCsrfToken } = vi.hoisted(() => ({
  mockPostFn: vi.fn(),
  mockGetFn: vi.fn(),
  mockPatchFn: vi.fn(),
  mockResetCsrfToken: vi.fn(),
  mockEnsureCsrfToken: vi.fn().mockResolvedValue("mock-csrf-token"),
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

// Mock @/lib/api
vi.mock("@/lib/api", () => ({
  API_BASE_URL: "/api",
  setJwtAccessToken: vi.fn(),
  getJwtAccessToken: vi.fn(() => null),
  refreshAccessToken: vi.fn(),
  resetCsrfToken: mockResetCsrfToken,
  ensureCsrfToken: mockEnsureCsrfToken,
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

// Mock @/stores/useVaultStore
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

// Import after mocks
import { useAuthStore, resetInitState } from "./useAuthStore";
import { setJwtAccessToken, getJwtAccessToken, refreshAccessToken } from "@/lib/api";

describe("useAuthStore", () => {
  // Grab references to the mock functions for tests
  const mockPost = mockPostFn;
  const mockGet = mockGetFn;
  const mockPatch = mockPatchFn;

  const mockUser = {
    id: 1,
    username: "testuser",
    full_name: "Test User",
    role: "admin" as const,
    is_active: true,
  };

  beforeEach(() => {
    // Reset all mocks including implementations
    mockPost.mockReset();
    mockGet.mockReset();
    mockPatch.mockReset();
    mockResetCsrfToken.mockReset();
    mockEnsureCsrfToken.mockReset();
    mockEnsureCsrfToken.mockResolvedValue("mock-csrf-token");
    mockFetchVaults.mockReset();
    mockFetchVaults.mockResolvedValue(undefined);

    // Reset module-level init guard state
    resetInitState();

    // Reset store state
    useAuthStore.setState({
      user: null,
      accessToken: null,
      isAuthenticated: false,
      isInitialized: false,
      isLoading: false,
      needsSetup: false,
      authMode: "unknown",
    });

    // Reset localStorage mock
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
    // Reset module-level init guard state to prevent test pollution
    resetInitState();
  });

  describe("Initial State", () => {
    it("should have correct initial state values", () => {
      const state = useAuthStore.getState();
      expect(state.user).toBeNull();
      expect(state.accessToken).toBeNull();
      expect(state.isAuthenticated).toBe(false);
      expect(state.isLoading).toBe(false);
      expect(state.needsSetup).toBe(false);
      expect(state.authMode).toBe("unknown");
    });
  });

  describe("login", () => {
    it("should login successfully with user in response", async () => {
      const { login } = useAuthStore.getState();

      mockPost?.mockResolvedValueOnce({
        data: {
          access_token: "jwt123",
          user: mockUser,
        },
      });

      await login("testuser", "password123");

      const state = useAuthStore.getState();
      expect(state.user).toEqual(mockUser);
      expect(state.accessToken).toBe("jwt123");
      expect(state.isAuthenticated).toBe(true);
      expect(state.authMode).toBe("jwt");
      expect(setJwtAccessToken).toHaveBeenCalledWith("jwt123");
      // Vault state is initialized to validate cached activeVaultId
      expect(mockFetchVaults).toHaveBeenCalledTimes(1);
    });

    it("should login successfully and fetch user if user not in response", async () => {
      const { login, fetchMe } = useAuthStore.getState();

      mockPost?.mockResolvedValueOnce({
        data: {
          access_token: "jwt123",
        },
      });

      // Mock fetchMe response
      mockGet?.mockResolvedValueOnce({
        data: mockUser,
      });

      await login("testuser", "password123");

      const state = useAuthStore.getState();
      expect(state.user).toEqual(mockUser);
      expect(state.accessToken).toBe("jwt123");
      expect(state.isAuthenticated).toBe(true);
      expect(state.authMode).toBe("jwt");
      expect(setJwtAccessToken).toHaveBeenCalledWith("jwt123");
      // Vault state is initialized to validate cached activeVaultId
      expect(mockFetchVaults).toHaveBeenCalledTimes(1);
    });

    it("should throw error on login failure", async () => {
      const { login } = useAuthStore.getState();

      mockPost?.mockRejectedValueOnce({
        response: { status: 401, data: { detail: "Invalid credentials" } },
      });

      await expect(login("testuser", "wrongpassword")).rejects.toThrow();

      const state = useAuthStore.getState();
      expect(state.user).toBeNull();
      expect(state.accessToken).toBeNull();
      expect(state.isAuthenticated).toBe(false);
    });

    // Task 2.2: Verify fetchVaults is called on each login when guard is reset between calls
    it("should call fetchVaults on each login when guard is reset between calls", async () => {
      const { login } = useAuthStore.getState();

      // First successful login — sets _vaultsInitialized = true and calls fetchVaults
      mockPost?.mockResolvedValueOnce({
        data: {
          access_token: "jwt-first",
          user: mockUser,
        },
      });

      await login("testuser", "password123");

      expect(mockFetchVaults).toHaveBeenCalledTimes(1);

      // Second login after guard reset — should call fetchVaults again
      mockPost?.mockResolvedValueOnce({
        data: {
          access_token: "jwt-second",
          user: mockUser,
        },
      });

      await login("testuser2", "password456");

      // fetchVaults should be called twice (once per login)
      expect(mockFetchVaults).toHaveBeenCalledTimes(2);
    });

    it("should set isLoading during login", async () => {
      const { login } = useAuthStore.getState();
      
      let loadingDuringRequest = false;
      mockPost?.mockImplementationOnce(async () => {
        const state = useAuthStore.getState();
        loadingDuringRequest = state.isLoading;
        return {
          data: {
            access_token: "jwt123",
            user: mockUser,
          },
        };
      });

      await login("testuser", "password123");
      
      expect(loadingDuringRequest).toBe(true);
      expect(useAuthStore.getState().isLoading).toBe(false);
    });
  });

  describe("register", () => {
    it("should register successfully and auto-login", async () => {
      const { register } = useAuthStore.getState();
      
      mockPost?.mockResolvedValueOnce({
        data: {
          access_token: "jwt123",
          user: mockUser,
        },
      });

      await register("newuser", "password123", "New User");

      const state = useAuthStore.getState();
      expect(state.user).toEqual(mockUser);
      expect(state.accessToken).toBe("jwt123");
      expect(state.isAuthenticated).toBe(true);
      expect(state.authMode).toBe("jwt");
      expect(setJwtAccessToken).toHaveBeenCalledWith("jwt123");
    });

    it("should handle register without full name", async () => {
      const { register } = useAuthStore.getState();

      mockPost?.mockResolvedValueOnce({
        data: {
          access_token: "jwt123",
          user: mockUser,
        },
      });

      await register("anotheruser", "password123");

      const state = useAuthStore.getState();
      expect(state.accessToken).toBe("jwt123");
      expect(state.isAuthenticated).toBe(true);
    });

    // =============================================================================
    // C1 — register() calls resetCsrfToken and ensureCsrfToken (review council fix)
    // =============================================================================
    it("should call resetCsrfToken and ensureCsrfToken after successful registration", async () => {
      const { register } = useAuthStore.getState();

      mockPost?.mockResolvedValueOnce({
        data: {
          access_token: "jwt789",
          user: {
            id: 2,
            username: "newuser2",
            full_name: "New User 2",
            role: "member",
            is_active: true,
          },
        },
      });

      await register("newuser2", "password123", "New User 2");

      // C1 fix: register() resets and re-fetches CSRF for the new session
      expect(mockResetCsrfToken).toHaveBeenCalledTimes(1);
      expect(mockEnsureCsrfToken).toHaveBeenCalledTimes(1);
    });
  });

  describe("logout", () => {
    it("should logout successfully and clear state", async () => {
      // First login to set up state
      useAuthStore.setState({
        user: mockUser,
        accessToken: "jwt123",
        isAuthenticated: true,
        authMode: "jwt",
      });

      const { logout } = useAuthStore.getState();
      
      mockPost?.mockResolvedValueOnce({ data: {} });

      await logout();

      const state = useAuthStore.getState();
      expect(state.user).toBeNull();
      expect(state.accessToken).toBeNull();
      expect(state.isAuthenticated).toBe(false);
      expect(setJwtAccessToken).toHaveBeenCalledWith(null);
    });

    it("should clear state even if logout request fails", async () => {
      useAuthStore.setState({
        user: mockUser,
        accessToken: "jwt123",
        isAuthenticated: true,
        authMode: "jwt",
      });

      const { logout } = useAuthStore.getState();
      
      mockPost?.mockRejectedValueOnce(new Error("Network error"));

      await logout();

      const state = useAuthStore.getState();
      expect(state.user).toBeNull();
      expect(state.accessToken).toBeNull();
      expect(state.isAuthenticated).toBe(false);
    });
  });

  describe("refreshToken", () => {
    it("should refresh token successfully", async () => {
      useAuthStore.setState({
        accessToken: "old-jwt",
      });

      const { refreshToken } = useAuthStore.getState();
      
      vi.mocked(refreshAccessToken).mockResolvedValueOnce("new-jwt");

      const result = await refreshToken();

      expect(result).toBe("new-jwt");
      expect(useAuthStore.getState().accessToken).toBe("new-jwt");
      expect(setJwtAccessToken).toHaveBeenCalledWith("new-jwt");
    });

    it("should return null and clear auth on refresh failure", async () => {
      useAuthStore.setState({
        user: mockUser,
        accessToken: "expired-jwt",
        isAuthenticated: true,
      });

      const { refreshToken } = useAuthStore.getState();
      
      vi.mocked(refreshAccessToken).mockResolvedValueOnce(null);

      const result = await refreshToken();

      expect(result).toBeNull();
      const state = useAuthStore.getState();
      expect(state.user).toBeNull();
      expect(state.accessToken).toBeNull();
      expect(state.isAuthenticated).toBe(false);
      expect(setJwtAccessToken).toHaveBeenCalledWith(null);
    });
  });

  describe("fetchMe", () => {
    it("should fetch user successfully", async () => {
      useAuthStore.setState({
        accessToken: "jwt123",
      });

      const { fetchMe } = useAuthStore.getState();
      
      mockGet?.mockResolvedValueOnce({
        data: mockUser,
      });

      await fetchMe();

      const state = useAuthStore.getState();
      expect(state.user).toEqual(mockUser);
      expect(state.isAuthenticated).toBe(true);
    });

    it("should use getJwtAccessToken when accessToken is null", async () => {
      useAuthStore.setState({
        accessToken: null,
      });

      const { fetchMe } = useAuthStore.getState();
      
      (getJwtAccessToken as ReturnType<typeof vi.fn>).mockReturnValueOnce("jwt-from-storage");
      
      mockGet?.mockResolvedValueOnce({
        data: mockUser,
      });

      await fetchMe();

      expect(getJwtAccessToken).toHaveBeenCalled();
      const state = useAuthStore.getState();
      expect(state.user).toEqual(mockUser);
    });

    it("should throw error when no token available", async () => {
      useAuthStore.setState({
        accessToken: null,
      });

      const { fetchMe } = useAuthStore.getState();
      
      (getJwtAccessToken as ReturnType<typeof vi.fn>).mockReturnValueOnce(null);

      await expect(fetchMe()).rejects.toThrow("No access token available");
    });
  });

  describe("checkSetupStatus", () => {
    it("should set needsSetup to true when server indicates setup is needed", async () => {
      const { checkSetupStatus } = useAuthStore.getState();
      
      mockGet?.mockResolvedValueOnce({
        data: { needs_setup: true },
      });

      await checkSetupStatus();

      expect(useAuthStore.getState().needsSetup).toBe(true);
    });

    it("should set single_admin auth mode when users are disabled", async () => {
      const { checkSetupStatus } = useAuthStore.getState();

      mockGet?.mockResolvedValueOnce({
        data: { needs_setup: false, users_enabled: false, auth_mode: "single_admin" },
      });

      await checkSetupStatus();

      expect(useAuthStore.getState().needsSetup).toBe(false);
      expect(useAuthStore.getState().authMode).toBe("single_admin");
    });

    it("should set needsSetup to false when server indicates setup is not needed", async () => {
      const { checkSetupStatus } = useAuthStore.getState();
      
      mockGet?.mockResolvedValueOnce({
        data: { needs_setup: false },
      });

      await checkSetupStatus();

      expect(useAuthStore.getState().needsSetup).toBe(false);
    });

    it("should set needsSetup to false on error", async () => {
      const { checkSetupStatus } = useAuthStore.getState();
      
      mockGet?.mockRejectedValueOnce(new Error("Network error"));

      await checkSetupStatus();

      expect(useAuthStore.getState().needsSetup).toBe(false);
    });
  });

  describe("init", () => {
    it("should set JWT mode with existing token when /auth/me succeeds", async () => {
      useAuthStore.setState({
        accessToken: "existing-jwt",
      });

      const { init } = useAuthStore.getState();

      mockGet?.mockResolvedValueOnce({
        data: mockUser,
      });

      await init();

      const state = useAuthStore.getState();
      expect(state.authMode).toBe("jwt");
      expect(state.isAuthenticated).toBe(true);
      expect(state.user).toEqual(mockUser);
      // Vault state is initialized to validate cached activeVaultId
      expect(mockFetchVaults).toHaveBeenCalledTimes(1);
    });

    it("should call fetchVaults when refresh token succeeds in init", async () => {
      useAuthStore.setState({
        accessToken: null,
      });

      const { init } = useAuthStore.getState();

      // Mock refreshAccessToken to return a new token (called by refreshToken())
      vi.mocked(refreshAccessToken).mockResolvedValueOnce("refreshed-jwt");
      // Mock fetchMe after refresh (GET /auth/me via authClient)
      mockGetFn?.mockResolvedValueOnce({
        data: mockUser,
      });

      await init();

      const state = useAuthStore.getState();
      expect(state.authMode).toBe("jwt");
      expect(state.isAuthenticated).toBe(true);
      expect(state.user).toEqual(mockUser);
      // Vault state is initialized via refresh token branch
      expect(mockFetchVaults).toHaveBeenCalledTimes(1);
    });

    it("should default to jwt mode after failed refresh when no token exists", async () => {
      useAuthStore.setState({
        accessToken: null,
      });

      const { init } = useAuthStore.getState();

      // Mock refresh token failure (no httpOnly cookie)
      mockPostFn?.mockRejectedValueOnce(new Error("Unauthorized"));
      // Mock setup-status success
      mockGet?.mockResolvedValueOnce({
        data: { needs_setup: false },
      });

      await init();

      const state = useAuthStore.getState();
      // After auth consolidation (H-10), always defaults to jwt
      expect(state.authMode).toBe("jwt");
      expect(state.isAuthenticated).toBe(false);
    });

    it("should default to jwt mode when no auth methods available", async () => {
      useAuthStore.setState({
        accessToken: null,
      });

      const { init } = useAuthStore.getState();

      // Mock refresh token failure (no access token, cookie-based refresh fails)
      mockPostFn?.mockRejectedValueOnce(new Error("Unauthorized"));
      // Mock setup-status failure
      mockGet?.mockRejectedValueOnce(new Error("Network error"));

      await init();

      const state = useAuthStore.getState();
      // After auth consolidation (H-10), we always default to jwt mode
      expect(state.authMode).toBe("jwt");
      expect(state.isAuthenticated).toBe(false);
    });

    it("should use initial needsSetup value for authMode when no API key", async () => {
      // Set initial needsSetup to true (simulating already-known setup requirement)
      useAuthStore.setState({
        accessToken: null,
        needsSetup: true,
      });

      const { init } = useAuthStore.getState();
      
      // Mock checkSetupStatus success (this runs but result is ignored for authMode)
      mockGet?.mockResolvedValueOnce({
        data: { needs_setup: false },
      });
      // Mock localStorage returning null (no API key)
      Object.defineProperty(window, "localStorage", {
        value: {
          getItem: vi.fn().mockReturnValue(null),
          setItem: vi.fn(),
          removeItem: vi.fn(),
          clear: vi.fn(),
        },
        writable: true,
      });

      await init();

      // The source code captures state.needsSetup at the START of init()
      // So even though checkSetupStatus sets needsSetup=false, the authMode
      // decision uses the initial snapshot (needsSetup=true)
      const state = useAuthStore.getState();
      expect(state.authMode).toBe("jwt");
      // Note: needsSetup gets updated by checkSetupStatus to false
      expect(state.needsSetup).toBe(false);
    });
  });

  describe("updateProfile", () => {
    it("should update profile successfully", async () => {
      useAuthStore.setState({
        user: mockUser,
        accessToken: "jwt123",
      });

      const { updateProfile } = useAuthStore.getState();
      
      const updatedUser = { ...mockUser, full_name: "Updated Name" };
      
      mockPatch?.mockResolvedValueOnce({
        data: updatedUser,
      });

      await updateProfile({ full_name: "Updated Name" });

      const state = useAuthStore.getState();
      expect(state.user).toEqual(updatedUser);
      expect(mockPatch).toHaveBeenCalled();
    });

    it("should throw error when no token available", async () => {
      useAuthStore.setState({
        accessToken: null,
      });

      const { updateProfile } = useAuthStore.getState();
      
      (getJwtAccessToken as ReturnType<typeof vi.fn>).mockReturnValueOnce(null);

      await expect(updateProfile({ full_name: "New Name" })).rejects.toThrow(
        "No access token available"
      );
    });

    it("should use token from getJwtAccessToken when accessToken is null", async () => {
      useAuthStore.setState({
        accessToken: null,
        user: mockUser,
      });

      const { updateProfile } = useAuthStore.getState();
      
      (getJwtAccessToken as ReturnType<typeof vi.fn>).mockReturnValueOnce("jwt-from-storage");
      
      const updatedUser = { ...mockUser, full_name: "New Name" };
      mockPatch?.mockResolvedValueOnce({
        data: updatedUser,
      });

      await updateProfile({ full_name: "New Name" });

      expect(getJwtAccessToken).toHaveBeenCalled();
      expect(mockPatch).toHaveBeenCalled();
    });
  });

  describe("Token Persistence", () => {
    it("should persist accessToken in store state", async () => {
      const { login } = useAuthStore.getState();
      
      mockPost?.mockResolvedValueOnce({
        data: {
          access_token: "persistent-jwt",
          user: mockUser,
        },
      });

      await login("testuser", "password123");

      const state = useAuthStore.getState();
      expect(state.accessToken).toBe("persistent-jwt");
    });

    it("should call setJwtAccessToken when token changes", async () => {
      // The store subscribes to token changes
      useAuthStore.setState({ accessToken: "new-token" });

      expect(setJwtAccessToken).toHaveBeenCalledWith("new-token");
    });
  });

  describe("localStorage persistence (TEST-FE-001)", () => {
    // The store uses zustand persist() with name "auth-storage" and partializes
    // only { user, authMode, needsSetup } — accessToken is deliberately NOT
    // persisted (H-11 XSS-risk fix). The persist middleware captures its
    // storage at module-load time, so these tests verify the persistence
    // contract via the store's persist options (name + partialize) directly,
    // which is the exact surface that decides what reaches localStorage.

    it("persist store name is 'auth-storage'", () => {
      // zustand exposes persist config on the store under the persist key.
      const persistApi: any = (useAuthStore as any).persist;
      expect(persistApi).toBeDefined();
      // The options carry the storage name.
      const options = persistApi?.getOptions?.() ?? persistApi?.options;
      expect(options?.name).toBe("auth-storage");
    });

    it("partialize persists only user, authMode, needsSetup", () => {
      const persistApi: any = (useAuthStore as any).persist;
      const options = persistApi?.getOptions?.() ?? persistApi?.options;
      const partialize = options?.partialize;
      expect(typeof partialize).toBe("function");

      const fullState = {
        user: mockUser,
        authMode: "jwt",
        needsSetup: false,
        accessToken: "secret-jwt-should-not-persist",
        isAuthenticated: true,
        isLoading: false,
        isInitialized: true,
      };
      const persisted = partialize(fullState);

      // Must include the three persisted fields.
      expect(persisted).toEqual({
        user: mockUser,
        authMode: "jwt",
        needsSetup: false,
      });
      // Must NOT include accessToken or any other runtime-only field.
      expect(persisted).not.toHaveProperty("accessToken");
      expect(persisted).not.toHaveProperty("isAuthenticated");
    });

    it("partialize never includes accessToken regardless of store state", () => {
      const persistApi: any = (useAuthStore as any).persist;
      const options = persistApi?.getOptions?.() ?? persistApi?.options;
      const partialize = options?.partialize;

      // Even when accessToken is set, it must not appear in persisted output.
      const persisted = partialize({
        ...useAuthStore.getState(),
        accessToken: "super-secret-jwt",
      });
      expect(persisted).not.toHaveProperty("accessToken");
      // A token present in the live state must never reach the partialized blob.
      const serialized = JSON.stringify(persisted);
      expect(serialized).not.toContain("super-secret-jwt");
    });
  });

  describe("setAuthMode", () => {
    it("should set authMode to jwt", () => {
      const { setAuthMode } = useAuthStore.getState();
      setAuthMode("jwt");
      expect(useAuthStore.getState().authMode).toBe("jwt");
    });

    it("should set authMode to single_admin", () => {
      const { setAuthMode } = useAuthStore.getState();
      setAuthMode("single_admin");
      expect(useAuthStore.getState().authMode).toBe("single_admin");
    });

    // "apikey" mode removed — JWT is the only auth mode
  });

  describe("_setLoading", () => {
    it("should set isLoading to true", () => {
      const { _setLoading } = useAuthStore.getState();
      _setLoading(true);
      expect(useAuthStore.getState().isLoading).toBe(true);
    });

    it("should set isLoading to false", () => {
      useAuthStore.setState({ isLoading: true });
      const { _setLoading } = useAuthStore.getState();
      _setLoading(false);
      expect(useAuthStore.getState().isLoading).toBe(false);
    });
  });
});
