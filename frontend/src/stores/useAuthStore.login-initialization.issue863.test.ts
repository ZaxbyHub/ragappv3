import { act, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  ensureCsrfToken,
  ensureCsrfTokenPhysical,
  setJwtAccessToken,
} from "@/lib/api";
import { reserveReplacementAuthOwner } from "@/lib/api/auth-lifecycle";
import { resetInitState, useAuthStore } from "@/stores/useAuthStore";

const transport = vi.hoisted(() => ({
  jwtAccessToken: null as string | null,
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

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    API_BASE_URL: "/api",
    attachCsrfInterceptor: vi.fn(),
    ensureCsrfToken: vi.fn().mockResolvedValue("fixture-csrf"),
    ensureCsrfTokenPhysical: vi.fn().mockResolvedValue("fixture-csrf"),
    getJwtAccessToken: vi.fn(() => actual.getJwtAccessToken()),
    resetCsrfToken: vi.fn(),
    setJwtAccessToken: vi.fn((value: string | null) => {
      transport.jwtAccessToken = value;
      actual.setJwtAccessToken(value);
    }),
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

type User = {
  id: number;
  username: string;
  full_name: string;
  role: "admin" | "user";
  is_active: boolean;
};

type Deferred<T> = {
  promise: Promise<T>;
  resolve: (value: T) => void;
};

const pendingDeferreds = new Set<{ resolve: (value: unknown) => void }>();

function deferred<T>(): Deferred<T> {
  let resolvePromise!: (value: T) => void;
  const promise = new Promise<T>((resolve) => {
    resolvePromise = resolve;
  });
  promise.catch(() => undefined);
  pendingDeferreds.add({ resolve: resolvePromise as (value: unknown) => void });
  return { promise, resolve: resolvePromise };
}

const userA: User = {
  id: 101,
  username: "login-a",
  full_name: "Login A",
  role: "admin",
  is_active: true,
};

const userB: User = {
  id: 202,
  username: "login-b",
  full_name: "Login B",
  role: "admin",
  is_active: true,
};

function loginResponse(accessToken: string, user: User) {
  return { data: { access_token: accessToken, user } };
}

function setUnauthenticatedState(accessToken = "jwt-init") {
  useAuthStore.setState({
    user: null,
    accessToken,
    isAuthenticated: false,
    isInitialized: false,
    initializationFailed: false,
    isLoading: false,
    needsSetup: null,
    authMode: "unknown",
  });
}

function expectCurrentLogin(user: User, accessToken: string) {
  expect(useAuthStore.getState()).toMatchObject({
    user,
    accessToken,
    isAuthenticated: true,
    isInitialized: true,
    initializationFailed: false,
    isLoading: false,
    authMode: "jwt",
  });
}

describe("issue 863 login initialization ownership", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    transport.get.mockReset();
    transport.post.mockReset();
    transport.patch.mockReset();
    transport.put.mockReset();
    transport.delete.mockReset();
    transport.fetchVaults.mockReset();
    transport.jwtAccessToken = null;
    setJwtAccessToken(null);
    transport.fetchVaults.mockResolvedValue(undefined);
    vi.mocked(ensureCsrfToken).mockResolvedValue("fixture-csrf");
    vi.mocked(ensureCsrfTokenPhysical).mockResolvedValue("fixture-csrf");
    resetInitState();
    reserveReplacementAuthOwner();
    setUnauthenticatedState();
  });

  afterEach(async () => {
    reserveReplacementAuthOwner();
    for (const pending of pendingDeferreds) pending.resolve(undefined);
    pendingDeferreds.clear();
    await Promise.resolve();
    resetInitState();
    setJwtAccessToken(null);
    vi.restoreAllMocks();
  });

  it("keeps a successful current-owner login initialized when a retired init settles", async () => {
    const staleMe = deferred<{ data: User }>();
    transport.get.mockImplementation((url: string) => {
      if (url === "/auth/me") return staleMe.promise;
      return Promise.reject(new Error(`unexpected auth URL: ${url}`));
    });
    transport.post.mockResolvedValue(loginResponse("jwt-login-b", userB));

    const retiredInit = useAuthStore.getState().init();
    await waitFor(() => expect(transport.get).toHaveBeenCalledWith("/auth/me", expect.anything()));

    await act(async () => {
      await useAuthStore.getState().login("login-b", "correct-password");
    });
    expectCurrentLogin(userB, "jwt-login-b");

    await act(async () => {
      staleMe.resolve({ data: userA });
      await retiredInit;
    });
    expectCurrentLogin(userB, "jwt-login-b");
    expect(transport.fetchVaults).toHaveBeenCalledTimes(1);
  });

  it("does not let a login paused in CSRF setup publish after a successor login", async () => {
    const csrfA = deferred<string>();
    const vaultB = deferred<void>();
    vi.mocked(ensureCsrfToken)
      .mockImplementationOnce(() => csrfA.promise)
      .mockResolvedValue("fixture-csrf-b");
    transport.fetchVaults.mockImplementationOnce(() => vaultB.promise);
    transport.post
      .mockResolvedValueOnce(loginResponse("jwt-login-a", userA))
      .mockResolvedValueOnce(loginResponse("jwt-login-b", userB));

    const loginA = useAuthStore.getState().login("login-a", "correct-password");
    await waitFor(() => expect(ensureCsrfToken).toHaveBeenCalledTimes(1));
    const loginB = useAuthStore.getState().login("login-b", "correct-password");
    await waitFor(() => expect(transport.fetchVaults).toHaveBeenCalledTimes(1));
    expect(useAuthStore.getState()).toMatchObject({ user: userB, accessToken: "jwt-login-b", isInitialized: false });

    await act(async () => {
      csrfA.resolve("fixture-csrf-a");
      await loginA;
    });
    expect(useAuthStore.getState()).toMatchObject({ user: userB, accessToken: "jwt-login-b", isInitialized: false });

    await act(async () => {
      vaultB.resolve(undefined);
      await loginB;
    });
    expectCurrentLogin(userB, "jwt-login-b");
  });

  it("does not let a login paused in vault setup publish after a successor login", async () => {
    const vaultA = deferred<void>();
    const vaultB = deferred<void>();
    transport.fetchVaults
      .mockImplementationOnce(() => vaultA.promise)
      .mockImplementationOnce(() => vaultB.promise);
    transport.post
      .mockResolvedValueOnce(loginResponse("jwt-login-a", userA))
      .mockResolvedValueOnce(loginResponse("jwt-login-b", userB));

    const loginA = useAuthStore.getState().login("login-a", "correct-password");
    await waitFor(() => expect(transport.fetchVaults).toHaveBeenCalledTimes(1));
    const loginB = useAuthStore.getState().login("login-b", "correct-password");
    await waitFor(() => expect(transport.fetchVaults).toHaveBeenCalledTimes(2));
    expect(useAuthStore.getState()).toMatchObject({ user: userB, accessToken: "jwt-login-b", isInitialized: false });

    await act(async () => {
      vaultA.resolve(undefined);
      await loginA;
    });
    expect(useAuthStore.getState()).toMatchObject({ user: userB, accessToken: "jwt-login-b", isInitialized: false });

    await act(async () => {
      vaultB.resolve(undefined);
      await loginB;
    });
    expectCurrentLogin(userB, "jwt-login-b");
    expect(transport.fetchVaults).toHaveBeenCalledTimes(2);
  });
});
