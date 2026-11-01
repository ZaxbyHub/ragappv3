import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ProtectedRoute } from "@/components/auth/ProtectedRoute";
import {
  AuthTransportTimeoutError,
  reserveReplacementAuthOwner,
  StaleAuthOwnerError,
} from "@/lib/api/auth-lifecycle";
import { resetInitState, useAuthStore } from "@/stores/useAuthStore";

const transport = vi.hoisted(() => ({
  jwtAccessToken: null as string | null,
  setupStatusCalls: 0,
  get: vi.fn(),
  post: vi.fn(),
  patch: vi.fn(),
  put: vi.fn(),
  delete: vi.fn(),
  fetchVaults: vi.fn(),
}));

vi.mock("axios", () => ({
  default: {
    create: vi.fn(() => ({
      get: transport.get,
      post: transport.post,
      patch: transport.patch,
      put: transport.put,
      delete: transport.delete,
      interceptors: {
        request: { use: vi.fn((callback) => callback) },
        response: { use: vi.fn((callback) => callback) },
      },
    })),
  },
}));

vi.mock("@/lib/api/core", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api/core")>();
  return {
    ...actual,
    getJwtAccessToken: () => transport.jwtAccessToken,
    onJwtAccessTokenPublished: vi.fn(),
  };
});

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    API_BASE_URL: "/api",
    getJwtAccessToken: () => transport.jwtAccessToken,
    setJwtAccessToken: (token: string | null) => {
      transport.jwtAccessToken = token;
    },
    refreshAccessToken: vi.fn().mockResolvedValue(null),
    ensureCsrfToken: vi.fn().mockResolvedValue("csrf-token"),
    ensureCsrfTokenPhysical: vi.fn().mockResolvedValue("csrf-token"),
    resetCsrfToken: vi.fn(),
    resetSubpathRefreshDiagnostic: vi.fn(),
  };
});

vi.mock("@/lib/api/onboarding", () => ({
  resetCitationReport: vi.fn(),
}));

vi.mock("@/stores/useVaultStore", () => ({
  useVaultStore: {
    getState: () => ({ fetchVaults: transport.fetchVaults }),
  },
}));

const userA = {
  id: 1,
  username: "alice",
  full_name: "Alice",
  role: "admin" as const,
  is_active: true,
};

const userB = {
  id: 2,
  username: "bob",
  full_name: "Bob",
  role: "admin" as const,
  is_active: true,
};

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((promiseResolve, promiseReject) => {
    resolve = promiseResolve;
    reject = promiseReject;
  });
  return { promise, resolve, reject };
}

function authenticatedState(user = userA, token = "jwt-a") {
  transport.jwtAccessToken = token;
  useAuthStore.setState({
    user,
    accessToken: token,
    isAuthenticated: true,
    isInitialized: true,
    initializationFailed: false,
    isLoading: false,
    needsSetup: false,
    authMode: "jwt",
  });
}

beforeEach(() => {
  transport.jwtAccessToken = null;
  transport.setupStatusCalls = 0;
  transport.get.mockReset();
  transport.post.mockReset();
  transport.patch.mockReset();
  transport.put.mockReset();
  transport.delete.mockReset();
  transport.fetchVaults.mockReset().mockResolvedValue([]);
  resetInitState();
  reserveReplacementAuthOwner();
  useAuthStore.setState({
    user: null,
    accessToken: null,
    isAuthenticated: false,
    isInitialized: false,
    initializationFailed: false,
    isLoading: false,
    needsSetup: null,
    authMode: "unknown",
  });
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  resetInitState();
  reserveReplacementAuthOwner();
});

describe("useAuthStore failure recovery", () => {
  it("recovers from failed initialization through registration and the real protected route", async () => {
    transport.get.mockImplementation(async (path: string) => {
      if (path.endsWith("/auth/setup-status")) {
        transport.setupStatusCalls += 1;
        if (transport.setupStatusCalls === 1) {
          throw new Error("setup status unavailable");
        }
        return { data: { needs_setup: false } };
      }
      if (path.endsWith("/auth/me")) {
        return { data: transport.jwtAccessToken === "jwt-b" ? userB : userA };
      }
      throw new Error(`unexpected GET ${path}`);
    });
    transport.post.mockImplementation(async (path: string) => {
      if (path.endsWith("/auth/register")) {
        return { data: { access_token: "jwt-b", user: userB } };
      }
      throw new Error(`unexpected POST ${path}`);
    });

    transport.jwtAccessToken = "jwt-a";
    useAuthStore.setState({ accessToken: "jwt-a" });
    await useAuthStore.getState().init();
    expect(useAuthStore.getState()).toMatchObject({
      isInitialized: false,
      initializationFailed: true,
    });

    await useAuthStore.getState().register("bob", "password", "Bob");
    expect(useAuthStore.getState()).toMatchObject({
      isAuthenticated: true,
      user: userB,
      isInitialized: true,
      initializationFailed: false,
    });

    render(
      <MemoryRouter initialEntries={["/protected"]}>
        <ProtectedRoute testMode={false}>
          <div>Protected content</div>
        </ProtectedRoute>
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.getByText("Protected content")).toBeVisible());
    expect(screen.queryByText("Unable to initialize authentication.")).not.toBeInTheDocument();

    await useAuthStore.getState().init();
    expect(useAuthStore.getState()).toMatchObject({
      isInitialized: true,
      initializationFailed: false,
    });
  });

  it("rejects a hung logout at the original 10,000 ms deadline and clears current-owner loading", async () => {
    const physical = deferred<{ data: unknown }>();
    authenticatedState();
    transport.post.mockImplementation((path: string) => {
      if (path.endsWith("/auth/logout")) return physical.promise;
      return Promise.reject(new Error(`unexpected POST ${path}`));
    });
    vi.useFakeTimers();

    const logoutPromise = useAuthStore.getState().logout();
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(transport.post).toHaveBeenCalledTimes(1);
    const rejection = expect(logoutPromise).rejects.toBeInstanceOf(AuthTransportTimeoutError);
    await act(async () => {
      vi.advanceTimersByTime(10_000);
      await Promise.resolve();
      await Promise.resolve();
    });

    try {
      await rejection;
      expect(useAuthStore.getState()).toMatchObject({
        isLoading: false,
        isAuthenticated: false,
        user: null,
        accessToken: null,
      });
      expect(transport.jwtAccessToken).toBeNull();
    } finally {
      physical.resolve({ data: {} });
      await physical.promise;
      await Promise.resolve();
    }
  });

  it("keeps replacement credentials and loading after a logout owner is superseded", async () => {
    const physical = deferred<{ data: unknown }>();
    authenticatedState();
    transport.post.mockImplementation((path: string) => {
      if (path.endsWith("/auth/logout")) return physical.promise;
      return Promise.reject(new Error(`unexpected POST ${path}`));
    });

    const firstLogout = useAuthStore.getState().logout();
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(transport.post).toHaveBeenCalledTimes(1);
    reserveReplacementAuthOwner();
    transport.jwtAccessToken = "jwt-b";
    useAuthStore.setState({
      user: userB,
      accessToken: "jwt-b",
      isAuthenticated: true,
      isLoading: true,
    });
    const rejection = expect(firstLogout).rejects.toBeInstanceOf(StaleAuthOwnerError);

    try {
      physical.resolve({ data: {} });
      await rejection;
      expect(useAuthStore.getState()).toMatchObject({
        user: userB,
        accessToken: "jwt-b",
        isAuthenticated: true,
        isLoading: true,
      });
      expect(transport.jwtAccessToken).toBe("jwt-b");
    } finally {
      await physical.promise;
      await Promise.resolve();
    }
  });
});
