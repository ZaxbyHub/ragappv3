import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { axiosGetMock, axiosPostMock, axiosPatchMock, axiosPutMock, axiosDeleteMock } = vi.hoisted(() => ({
  axiosGetMock: vi.fn(),
  axiosPostMock: vi.fn(),
  axiosPatchMock: vi.fn(),
  axiosPutMock: vi.fn(),
  axiosDeleteMock: vi.fn(),
}));

vi.mock("axios", () => ({
  default: {
    create: vi.fn(() => ({
      get: axiosGetMock,
      post: axiosPostMock,
      patch: axiosPatchMock,
      put: axiosPutMock,
      delete: axiosDeleteMock,
      interceptors: {
        request: { use: vi.fn() },
        response: { use: vi.fn() },
      },
    })),
  },
}));

vi.mock("@/stores/useVaultStore", () => ({
  useVaultStore: { getState: () => ({ fetchVaults: vi.fn() }) },
}));

import {
  apiClient,
  getJwtAccessToken,
  refreshAccessToken,
  resetCsrfToken,
  setJwtAccessToken,
} from "@/lib/api/core";
import { useAuthStore } from "@/stores/useAuthStore";

function jsonResponse(status: number, body: unknown = {}): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: new Headers(),
    json: () => Promise.resolve(body),
  } as unknown as Response;
}

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

function responseWithDeferredBody(body: Promise<unknown>): Response {
  return {
    ok: true,
    status: 200,
    headers: new Headers(),
    json: () => body,
  } as unknown as Response;
}

describe("public core auth refresh carry contracts for issue #775", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    vi.resetAllMocks();
    vi.useRealTimers();
    resetCsrfToken();
    setJwtAccessToken(null);
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    axiosGetMock.mockResolvedValue({ data: { ok: true } });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
    resetCsrfToken();
    setJwtAccessToken(null);
  });

  it("preserves public refresh outcome classes: terminal 4xx resolves null", async () => {
    for (const status of [401, 403, 404]) {
      resetCsrfToken();
      fetchMock.mockResolvedValueOnce(jsonResponse(200, { csrf_token: `csrf-${status}` }));
      fetchMock.mockResolvedValueOnce(jsonResponse(status, { detail: "terminal auth response" }));

      await expect(refreshAccessToken()).resolves.toBeNull();
    }
  });

  it("preserves public refresh rejection for network and 5xx transport failures", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, { csrf_token: "csrf-network" }));
    fetchMock.mockRejectedValueOnce(new TypeError("network unavailable"));
    await expect(refreshAccessToken()).rejects.toThrow("network unavailable");

    resetCsrfToken();
    fetchMock.mockResolvedValueOnce(jsonResponse(200, { csrf_token: "csrf-503" }));
    fetchMock.mockResolvedValueOnce(jsonResponse(503, { detail: "upstream outage" }));
    await expect(refreshAccessToken()).rejects.toThrow();
  });

  it("preserves the public refresh deadline as a rejection", async () => {
    vi.useFakeTimers();
    fetchMock.mockResolvedValueOnce(jsonResponse(200, { csrf_token: "csrf-timeout" }));
    fetchMock.mockImplementationOnce((_input: unknown, init?: RequestInit) =>
      new Promise<never>((_resolve, reject) => {
        init?.signal?.addEventListener("abort", () => {
          reject(new DOMException("refresh deadline", "AbortError"));
        });
      }),
    );

    const refresh = refreshAccessToken();
    const outcome = refresh.then(
      (value) => ({ status: "fulfilled" as const, value }),
      (error) => ({ status: "rejected" as const, error }),
    );
    await vi.advanceTimersByTimeAsync(10_001);
    await expect(outcome).resolves.toMatchObject({ status: "rejected" });
  });

  it("publishes the refreshed token before a public subscriber issues an independent request", async () => {
    const originalAuthState = useAuthStore.getState();
    const originalJwtAccessToken = getJwtAccessToken();
    useAuthStore.setState({
      user: {
        id: 1,
        username: "subscriber-user",
        full_name: "Subscriber User",
        role: "member",
        is_active: true,
      },
      accessToken: "old-token",
      isAuthenticated: true,
      isInitialized: true,
      isLoading: false,
      needsSetup: false,
      authMode: "jwt",
    });
    setJwtAccessToken("old-token");
    fetchMock.mockResolvedValueOnce(jsonResponse(200, { csrf_token: "csrf-public" }));
    fetchMock.mockResolvedValueOnce(jsonResponse(200, { access_token: "fresh-token" }));

    const observedTokens: string[] = [];
    const requestSettled = deferred<void>();
    const unsubscribe = useAuthStore.subscribe((state) => {
      if (state.accessToken === "fresh-token") {
        observedTokens.push(getJwtAccessToken() ?? "missing");
        void apiClient.get("/independent-subscriber-request").then(
          () => requestSettled.resolve(),
          () => requestSettled.resolve(),
        );
      }
    });

    try {
      await useAuthStore.getState().refreshToken();
      expect(observedTokens).toEqual(["fresh-token"]);
      await requestSettled.promise;
      expect(axiosGetMock).toHaveBeenCalledWith("/independent-subscriber-request");
    } finally {
      unsubscribe();
      useAuthStore.setState(originalAuthState);
      setJwtAccessToken(originalJwtAccessToken);
    }
  });

  it("holds a second public refresh until the first response body settles", async () => {
    const originalAuthState = useAuthStore.getState();
    const originalJwtAccessToken = getJwtAccessToken();
    const firstBody = deferred<unknown>();
    const logoutBody = deferred<unknown>();
    let firstReleased = false;
    let logoutReleased = false;
    let refreshCalls = 0;
    const outcomes: Promise<unknown>[] = [];
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/csrf-token")) {
        return Promise.resolve(jsonResponse(200, { csrf_token: "csrf-fifo" }));
      }
      if (url.includes("/auth/refresh")) {
        refreshCalls += 1;
        return refreshCalls === 1
          ? responseWithDeferredBody(firstBody.promise)
          : Promise.resolve(jsonResponse(200, { access_token: "second-token" }));
      }
      throw new Error(`Unexpected fetch URL: ${url}`);
    });
    axiosPostMock.mockImplementationOnce(() => logoutBody.promise);

    useAuthStore.setState({
      user: {
        id: 1,
        username: "fifo-user",
        full_name: "FIFO User",
        role: "member",
        is_active: true,
      },
      accessToken: "first-token",
      isAuthenticated: true,
      isInitialized: true,
      isLoading: false,
      needsSetup: false,
      authMode: "jwt",
    });
    setJwtAccessToken("first-token");

    try {
      const firstRefresh = useAuthStore.getState().refreshToken();
      const firstOutcome = firstRefresh.then(
        (value) => ({ status: "fulfilled" as const, value }),
        (error) => ({ status: "rejected" as const, error }),
      );
      outcomes.push(firstOutcome);
      await vi.waitFor(() => expect(refreshCalls).toBe(1));

      const logout = useAuthStore.getState().logout();
      const logoutOutcome = logout.then(
        (value) => ({ status: "fulfilled" as const, value }),
        (error) => ({ status: "rejected" as const, error }),
      );
      outcomes.push(logoutOutcome);
      const secondRefresh = useAuthStore.getState().refreshToken();
      const secondOutcome = secondRefresh.then(
        (value) => ({ status: "fulfilled" as const, value }),
        (error) => ({ status: "rejected" as const, error }),
      );
      outcomes.push(secondOutcome);
      let secondSettled = false;
      void secondOutcome.then(() => {
        secondSettled = true;
      });
      await Promise.resolve();
      expect(refreshCalls).toBe(1);
      expect(axiosPostMock).not.toHaveBeenCalled();
      expect(secondSettled).toBe(false);

      firstBody.resolve({ access_token: "first-token" });
      firstReleased = true;
      await expect(firstOutcome).resolves.toMatchObject({ status: "fulfilled", value: null });
      await vi.waitFor(() => expect(axiosPostMock).toHaveBeenCalledTimes(1));
      expect(refreshCalls).toBe(1);
      expect(secondSettled).toBe(false);
      logoutBody.resolve({});
      logoutReleased = true;
      await expect(logoutOutcome).resolves.toMatchObject({ status: "fulfilled", value: undefined });
      await expect(secondOutcome).resolves.toMatchObject({ status: "fulfilled", value: null });
      expect(refreshCalls).toBe(1);

      useAuthStore.setState({
        user: {
          id: 1,
          username: "fifo-user",
          full_name: "FIFO User",
          role: "member",
          is_active: true,
        },
        accessToken: "fresh-session-token",
        isAuthenticated: true,
        isInitialized: true,
        isLoading: false,
        needsSetup: false,
        authMode: "jwt",
      });
      setJwtAccessToken("fresh-session-token");
      await expect(useAuthStore.getState().refreshToken()).resolves.toBe("second-token");
      expect(refreshCalls).toBe(2);
    } finally {
      if (!firstReleased) firstBody.resolve({ access_token: "first-token" });
      if (!logoutReleased) logoutBody.resolve({});
      try {
        await Promise.all(outcomes);
      } finally {
        useAuthStore.setState(originalAuthState);
        setJwtAccessToken(originalJwtAccessToken);
      }
    }
  });

  it("queues public login, logout, and refresh until each cookie-mutating body settles", async () => {
    const originalAuthState = useAuthStore.getState();
    const originalJwtAccessToken = getJwtAccessToken();
    const loginBody = deferred<{
      data: {
        access_token: string;
        user: { id: number; username: string; full_name: string; role: "member"; is_active: boolean };
      };
    }>();
    const logoutBody = deferred<{ data: Record<string, never> }>();
    const refreshBody = deferred<unknown>();
    let loginReleased = false;
    let logoutReleased = false;
    let refreshReleased = false;
    let refreshCalls = 0;
    const outcomes: Promise<unknown>[] = [];
    axiosPostMock.mockImplementation((path: string) => {
      if (path === "/auth/login") return loginBody.promise;
      if (path === "/auth/logout") return logoutBody.promise;
      throw new Error(`unexpected cookie-mutating path: ${path}`);
    });
    fetchMock.mockImplementation((input: unknown) => {
      const url = String(input);
      if (url.includes("/csrf-token")) return Promise.resolve(jsonResponse(200, { csrf_token: "csrf-url-aware" }));
      if (url.includes("/auth/refresh")) {
        refreshCalls += 1;
        return Promise.resolve(responseWithDeferredBody(refreshBody.promise));
      }
      throw new Error(`unexpected fetch path: ${url}`);
    });

    try {
      const login = useAuthStore.getState().login("a", "pw");
      const loginOutcome = login.then(
        (value) => ({ status: "fulfilled" as const, value }),
        (error) => ({ status: "rejected" as const, error }),
      );
      outcomes.push(loginOutcome);
      await vi.waitFor(() => expect(axiosPostMock.mock.calls[0]?.[0]).toBe("/auth/login"));

      const logout = useAuthStore.getState().logout();
      const logoutOutcome = logout.then(
        (value) => ({ status: "fulfilled" as const, value }),
        (error) => ({ status: "rejected" as const, error }),
      );
      outcomes.push(logoutOutcome);
      const refresh = useAuthStore.getState().refreshToken();
      const refreshOutcome = refresh.then(
        (value) => ({ status: "fulfilled" as const, value }),
        (error) => ({ status: "rejected" as const, error }),
      );
      outcomes.push(refreshOutcome);
      expect(refreshCalls).toBe(0);

      loginBody.resolve({
        data: {
          access_token: "login-token",
          user: { id: 1, username: "a", full_name: "A", role: "member", is_active: true },
        },
      });
      loginReleased = true;
      await expect(login).resolves.toBeUndefined();
      await vi.waitFor(() => expect(axiosPostMock.mock.calls.some(([path]) => path === "/auth/logout")).toBe(true));
      expect(refreshCalls).toBe(0);

      logoutBody.resolve({ data: {} });
      logoutReleased = true;
      await expect(logout).resolves.toBeUndefined();
      await vi.waitFor(() => expect(refreshCalls).toBe(1));
      let refreshSettled = false;
      void refreshOutcome.then(() => {
        refreshSettled = true;
      });
      expect(refreshSettled).toBe(false);
      refreshBody.resolve({ access_token: "refreshed-after-logout" });
      refreshReleased = true;
      await expect(refreshOutcome).resolves.toMatchObject({ status: "fulfilled", value: "refreshed-after-logout" });
      expect(axiosPostMock.mock.calls.map(([path]) => path)).toEqual(["/auth/login", "/auth/logout"]);
    } finally {
      if (!loginReleased) loginBody.resolve({ data: { access_token: "cleanup", user: { id: 1, username: "cleanup", full_name: "Cleanup", role: "member", is_active: true } } });
      if (!logoutReleased) logoutBody.resolve({ data: {} });
      if (!refreshReleased) refreshBody.resolve({ access_token: "cleanup" });
      try {
        await Promise.all(outcomes);
      } finally {
        useAuthStore.setState(originalAuthState);
        setJwtAccessToken(originalJwtAccessToken);
      }
    }
  });
});
