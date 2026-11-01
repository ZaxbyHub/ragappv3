import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { authPostMock, fetchVaultsMock, refreshAccessTokenMock } = vi.hoisted(() => ({
  authPostMock: vi.fn(),
  fetchVaultsMock: vi.fn(),
  refreshAccessTokenMock: vi.fn(),
}));

function axiosInstance() {
  return {
    get: vi.fn(),
    post: authPostMock,
    patch: vi.fn(),
    put: vi.fn(),
    delete: vi.fn(),
    interceptors: {
      request: { use: vi.fn() },
      response: { use: vi.fn() },
    },
  };
}

vi.mock("axios", () => ({
  default: { create: vi.fn(axiosInstance) },
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, refreshAccessToken: refreshAccessTokenMock };
});

vi.mock("@/stores/useVaultStore", () => ({
  useVaultStore: { getState: () => ({ fetchVaults: fetchVaultsMock }) },
}));

import { getJwtAccessToken, resetCsrfToken, setJwtAccessToken } from "@/lib/api/core";
import { resetInitState, useAuthStore } from "./useAuthStore";

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
}

const oldUser = {
  id: 1,
  username: "old-user",
  full_name: "Old User",
  role: "admin" as const,
  is_active: true,
};

const newUser = {
  id: 2,
  username: "new-user",
  full_name: "New User",
  role: "member" as const,
  is_active: true,
};

function resetStore() {
  useAuthStore.setState({
    user: oldUser,
    accessToken: "old-session-token",
    isAuthenticated: true,
    isInitialized: true,
    isLoading: false,
    needsSetup: false,
    authMode: "jwt",
  });
  setJwtAccessToken("old-session-token");
}

describe("public useAuthStore refresh session boundaries for issue #775", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    resetCsrfToken();
    resetInitState();
    refreshAccessTokenMock.mockReset();
    fetchVaultsMock.mockResolvedValue(undefined);
    authPostMock.mockImplementation(async (path: string) => {
      if (path === "/auth/login") {
        return { data: { access_token: "later-login-token", user: newUser } };
      }
      return { data: {} };
    });
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ csrf_token: "csrf-token" }), {
          headers: new Headers({ "content-type": "application/json" }),
        }),
      ),
    );
    resetStore();
  });

  afterEach(() => {
    resetInitState();
    resetCsrfToken();
    setJwtAccessToken(null);
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("keeps a later public login when an earlier refresh resolves after it", async () => {
    const oldRefresh = deferred<string | null>();
    refreshAccessTokenMock.mockReturnValueOnce(oldRefresh.promise);
    const refresh = useAuthStore.getState().refreshToken();
    await vi.waitFor(() => expect(refreshAccessTokenMock).toHaveBeenCalledTimes(1));

    try {
      await useAuthStore.getState().logout();
      await useAuthStore.getState().login("new-user", "new-password");
      oldRefresh.resolve("late-old-session-token");

      await expect(refresh).resolves.toBeNull();
      expect(useAuthStore.getState()).toMatchObject({
        accessToken: "later-login-token",
        isAuthenticated: true,
        user: newUser,
      });
      expect(getJwtAccessToken()).toBe("later-login-token");
    } finally {
      oldRefresh.resolve(null);
      await refresh.catch(() => undefined);
    }
  });

  it("does not restore a logged-out public store when an earlier refresh resolves", async () => {
    const oldRefresh = deferred<string | null>();
    refreshAccessTokenMock.mockReturnValueOnce(oldRefresh.promise);
    const refresh = useAuthStore.getState().refreshToken();
    await vi.waitFor(() => expect(refreshAccessTokenMock).toHaveBeenCalledTimes(1));

    try {
      await useAuthStore.getState().logout();
      oldRefresh.resolve("late-old-session-token");

      await expect(refresh).resolves.toBeNull();
      expect(useAuthStore.getState()).toMatchObject({
        accessToken: null,
        isAuthenticated: false,
        user: null,
      });
      expect(getJwtAccessToken()).toBeNull();
    } finally {
      oldRefresh.resolve(null);
      await refresh.catch(() => undefined);
    }
  });
});
