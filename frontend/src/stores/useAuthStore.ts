import { create } from "zustand";
import { persist } from "zustand/middleware";
import axios from "axios";
import {
  API_BASE_URL,
  setJwtAccessToken,
  getJwtAccessToken,
  refreshAccessToken,
  ensureCsrfToken,
  resetCsrfToken,
  attachCsrfInterceptor,
} from "@/lib/api";

interface User {
  id: number;
  username: string;
  full_name: string;
  role: "superadmin" | "admin" | "member" | "viewer";
  is_active: boolean;
}

interface LoginResponse {
  access_token: string;
  user?: User;
}

interface RegisterResponse {
  access_token: string;
  user: User;
}

interface AuthState {
  // State
  user: User | null;
  accessToken: string | null;
  isAuthenticated: boolean;
  isLoading: boolean;
  isInitialized: boolean;
  needsSetup: boolean | null;
  authMode: "jwt" | "unknown";

  // Actions
  login: (username: string, password: string) => Promise<void>;
  register: (username: string, password: string, fullName?: string) => Promise<void>;
  logout: () => Promise<void>;
  refreshToken: () => Promise<string | null>;
  fetchMe: () => Promise<void>;
  checkSetupStatus: () => Promise<void>;
  setAuthMode: (mode: "jwt") => void;
  updateProfile: (data: { full_name?: string }) => Promise<void>;
  init: () => Promise<void>;

  // Internal
  _setLoading: (loading: boolean) => void;
}

// Create a separate axios instance for auth calls to avoid interceptor loops
const authClient = axios.create({
  baseURL: API_BASE_URL,
  timeout: 30000,
  headers: {
    "Content-Type": "application/json",
  },
  withCredentials: true, // Required for httpOnly refresh cookie
});

// Guard to prevent concurrent/duplicate init calls.
// Stores the in-flight promise so additional callers await the same result
// instead of short-circuiting with a stale isInitialized=true state.
let _initAttempted = false;
let _initPromise: Promise<void> | null = null;

// Reset init guard state (exported for testing)
export const resetInitState = () => {
  _initAttempted = false;
  _initPromise = null;
};

// Wire up CSRF via centralized api.ts utilities
attachCsrfInterceptor(authClient);

export const useAuthStore = create<AuthState>()(
  persist(
    (set, get) => ({
      // Initial state
      user: null,
      accessToken: null,
      isAuthenticated: false,
      isInitialized: false,
      isLoading: false,
      needsSetup: null as boolean | null,
      authMode: "unknown",

      _setLoading: (loading: boolean) => set({ isLoading: loading }),

      setAuthMode: (mode: "jwt") => set({ authMode: mode }),

      init: async () => {
        // Guard: if init is already in-flight, await the same promise instead of
        // short-circuiting with isInitialized=true before auth has resolved.
        // This prevents React StrictMode's double-effect invocation from marking
        // the route as initialized while isAuthenticated is still false.
        if (_initAttempted) {
          if (_initPromise) await _initPromise;
          return;
        }
        _initAttempted = true;

        _initPromise = (async () => {
          const state = get();
          set({ isLoading: true });

          // Try in-memory token first, then attempt httpOnly cookie refresh
          try {
            if (state.accessToken) {
              await get().fetchMe();
              set({ authMode: "jwt", isAuthenticated: true, isLoading: false, isInitialized: true });
              return;
            }
            // No in-memory token — attempt refresh via httpOnly cookie (H-7 fix)
            const newToken = await get().refreshToken();
            if (newToken) {
              await get().fetchMe();
              set({ authMode: "jwt", isAuthenticated: true, isLoading: false, isInitialized: true });
              return;
            }
          } catch {
            // Token invalid and refresh failed — clear all auth state
            set({
              accessToken: null,
              user: null,
              isAuthenticated: false,
            });
            setJwtAccessToken(null);
          }

          try {
            await get().checkSetupStatus();
          } catch {
            // Backend unreachable
          }

          set({ authMode: "jwt", isLoading: false, isInitialized: true });
        })();

        await _initPromise;
        _initPromise = null;
      },

      checkSetupStatus: async () => {
        try {
          const response = await authClient.get<{ needs_setup: boolean }>(
            "/auth/setup-status"
          );
          set({ needsSetup: response.data.needs_setup });
        } catch (error) {
          console.error("Failed to check setup status:", error);
          set({ needsSetup: false });
        }
      },

      login: async (username: string, password: string) => {
        // Reset init guard so re-login works after a failed init
        _initAttempted = false;
        _initPromise = null;

        get()._setLoading(true);
        try {
          // Force a fresh CSRF token before login to avoid 403 on first attempt
          resetCsrfToken();
          await ensureCsrfToken();

          const response = await authClient.post<LoginResponse>("/auth/login", {
            username,
            password,
          });

          const { access_token, user } = response.data;

          // Update store state
          set({
            accessToken: access_token,
            user: user || null,
            isAuthenticated: true,
            authMode: "jwt",
          });

          // Sync with apiClient
          setJwtAccessToken(access_token);

          // If user wasn't included in login response, fetch it
          if (!user) {
            await get().fetchMe();
          }
        } finally {
          get()._setLoading(false);
        }
      },

      register: async (username: string, password: string, fullName?: string) => {
        get()._setLoading(true);
        try {
          const response = await authClient.post<RegisterResponse>("/auth/register", {
            username,
            password,
            full_name: fullName,
          });

          // Backend returns flat response, construct User object manually
          const { access_token, id, username: uname, full_name, role, is_active } = response.data as any;
          const user: User = { id, username: uname, full_name, role, is_active };

          // Update store state
          set({
            accessToken: access_token,
            user: user,
            isAuthenticated: true,
            authMode: "jwt",
            needsSetup: false,
          });

          // Sync with apiClient
          setJwtAccessToken(access_token);

          // Reset and re-fetch CSRF token for new session
          resetCsrfToken();
          await ensureCsrfToken();
        } finally {
          get()._setLoading(false);
        }
      },

      logout: async () => {
        get()._setLoading(true);
        try {
          // Call logout endpoint to clear httpOnly cookie
          await authClient.post("/auth/logout", {}, { withCredentials: true });
        } catch (error) {
          console.error("Logout request failed:", error);
          // Continue with local cleanup even if server logout fails
        } finally {
          // Clear all auth state
          set({
            user: null,
            accessToken: null,
            isAuthenticated: false,
          });
          setJwtAccessToken(null);
          resetCsrfToken();
          get()._setLoading(false);
          // Reset init guard so re-login works after logout
          _initAttempted = false;
          _initPromise = null;
        }
      },

      refreshToken: async (): Promise<string | null> => {
        try {
          // Use the centralized refreshAccessToken from api.ts (singleton lock)
          // instead of calling authClient directly. This prevents concurrent refresh
          // from rotating the same cookie twice.
          const newToken = await refreshAccessToken();
          if (newToken) {
            // Update both store state AND apiClient module-level token
            set({ accessToken: newToken });
            setJwtAccessToken(newToken);
            return newToken;
          }
          // Clear auth state when refresh returns null (server rejected refresh)
          set({
            user: null,
            accessToken: null,
            isAuthenticated: false,
          });
          setJwtAccessToken(null);
          return null;
        } catch (error) {
          console.error("Token refresh failed:", error);
          // Clear auth state on refresh failure
          set({
            user: null,
            accessToken: null,
            isAuthenticated: false,
          });
          setJwtAccessToken(null);
          return null;
        }
      },

      fetchMe: async () => {
        try {
          const token = get().accessToken || getJwtAccessToken();
          if (!token) {
            throw new Error("No access token available");
          }

          const response = await authClient.get<User>("/auth/me", {
            headers: {
              Authorization: `Bearer ${token}`,
            },
          });

          set({
            user: response.data,
            isAuthenticated: true,
          });
        } catch (error) {
          console.error("Failed to fetch user:", error);
          // Don't clear auth here - let the caller decide (e.g., redirect to login)
          throw error;
        }
      },

      updateProfile: async (data: { full_name?: string }) => {
        get()._setLoading(true);
        try {
          const token = get().accessToken || getJwtAccessToken();
          if (!token) {
            throw new Error("No access token available");
          }

          const response = await authClient.patch<User>("/auth/me", data, {
            headers: {
              Authorization: `Bearer ${token}`,
            },
          });

          set({ user: response.data });
        } finally {
          get()._setLoading(false);
        }
      },
    }),
    {
      name: "auth-storage",
      // H-11 fix: Do NOT persist accessToken to localStorage (XSS risk).
      // The httpOnly refresh cookie handles session persistence.
      partialize: (state) => ({
        user: state.user,
        authMode: state.authMode,
        needsSetup: state.needsSetup,
      }),
    }
  )
);

// Subscribe to token changes to keep api.ts in sync
useAuthStore.subscribe((state, prevState) => {
  if (state.accessToken !== prevState.accessToken) {
    setJwtAccessToken(state.accessToken);
  }
});
