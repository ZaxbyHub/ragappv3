import { StrictMode, useEffect } from "react";
import { act, cleanup, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { refreshAccessToken } from "@/lib/api";
import { captureAuthOwner, reserveReplacementAuthOwner } from "@/lib/api/auth-lifecycle";
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
    refreshAccessToken: vi.fn(),
    resetCsrfToken: vi.fn(),
    setJwtAccessToken: vi.fn((value: string | null) => {
      transport.jwtAccessToken = value;
      actual.setJwtAccessToken(value);
    }),
  };
});

vi.mock("@/lib/api/core", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api/core")>();
  return {
    ...actual,
    onJwtAccessTokenPublished: vi.fn((listener, bridge) =>
      actual.onJwtAccessTokenPublished(listener, bridge),
    ),
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
  reject: (reason?: unknown) => void;
};

function deferred<T>(): Deferred<T> {
  let resolvePromise!: (value: T) => void;
  let rejectPromise!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolve, reject) => {
    resolvePromise = resolve;
    rejectPromise = reject;
  });
  promise.catch(() => undefined);
  return { promise, resolve: resolvePromise, reject: rejectPromise };
}

const userA: User = {
  id: 101,
  username: "owner-a",
  full_name: "Owner A",
  role: "admin",
  is_active: true,
};

const userB: User = {
  id: 202,
  username: "owner-b",
  full_name: "Owner B",
  role: "admin",
  is_active: true,
};

function setUnauthenticatedState(accessToken: string | null = "jwt-a") {
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

function InitProbe() {
  const init = useAuthStore((state) => state.init);

  useEffect(() => {
    void init();
  }, [init]);

  return null;
}

function mockInitReads(meResponses: Array<Promise<{ data: User }>>) {
  const pending = [...meResponses];
  transport.get.mockImplementation((url: string) => {
    if (url === "/auth/me") {
      return pending.shift() ?? Promise.reject(new Error("unexpected /auth/me call"));
    }
    if (url === "/auth/setup-status") {
      return Promise.resolve({ data: { needs_setup: false, auth_mode: "jwt" } });
    }
    return Promise.reject(new Error(`unexpected auth URL: ${url}`));
  });
}

function meCallCount() {
  return transport.get.mock.calls.filter(([url]) => url === "/auth/me").length;
}

describe("issue 775 auth initialization ownership", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    transport.get.mockReset();
    transport.post.mockReset();
    transport.patch.mockReset();
    transport.put.mockReset();
    transport.delete.mockReset();
    transport.fetchVaults.mockReset();
    transport.jwtAccessToken = null;
    transport.fetchVaults.mockResolvedValue(undefined);
    resetInitState();
    reserveReplacementAuthOwner();
    setUnauthenticatedState();
  });

  afterEach(() => {
    cleanup();
    resetInitState();
    reserveReplacementAuthOwner();
    vi.restoreAllMocks();
  });

  it("coalesces concurrent init calls from StrictMode into one /me and one vault read", async () => {
    const me = deferred<{ data: User }>();
    mockInitReads([me.promise]);

    render(
      <StrictMode>
        <InitProbe />
      </StrictMode>
    );
    await waitFor(() => expect(transport.get).toHaveBeenCalledTimes(1));

    await act(async () => me.resolve({ data: userA }));
    await waitFor(() => expect(useAuthStore.getState().isInitialized).toBe(true));

    expect(meCallCount()).toBe(1);
    expect(transport.get).toHaveBeenCalledWith("/auth/setup-status");
    expect(transport.fetchVaults).toHaveBeenCalledTimes(1);
    expect(useAuthStore.getState()).toMatchObject({
      user: userA,
      isAuthenticated: true,
      isInitialized: true,
      isLoading: false,
    });
  });

  it("clears a persisted principal when refresh returns no token and completes setup", async () => {
    vi.mocked(refreshAccessToken).mockResolvedValue(null);
    mockInitReads([]);
    useAuthStore.setState({
      user: userA,
      accessToken: null,
      isAuthenticated: true,
      isInitialized: false,
      isLoading: false,
      needsSetup: null,
      authMode: "jwt",
    });

    const ownerBefore = captureAuthOwner();

    await act(async () => {
      await useAuthStore.getState().init();
    });

    expect(refreshAccessToken).toHaveBeenCalledTimes(1);
    expect(transport.get).toHaveBeenCalledWith("/auth/setup-status");
    expect(meCallCount()).toBe(0);
    expect(transport.fetchVaults).not.toHaveBeenCalled();
    expect(captureAuthOwner()).toBe(ownerBefore);
    expect(useAuthStore.getState()).toMatchObject({
      user: null,
      accessToken: null,
      isAuthenticated: false,
      isInitialized: true,
      isLoading: false,
      needsSetup: false,
      authMode: "jwt",
    });
  });

  it("completes normal init when /me publishes null to user without self-invalidating", async () => {
    mockInitReads([Promise.resolve({ data: userA })]);
    const ownerBefore = captureAuthOwner();

    await act(async () => {
      await useAuthStore.getState().init();
    });

    expect(useAuthStore.getState()).toMatchObject({
      user: userA,
      isAuthenticated: true,
      isInitialized: true,
      isLoading: false,
    });
    expect(useAuthStore.getState().needsSetup).toBe(false);
    expect(useAuthStore.getState().isInitialized).toBe(true);
    expect(captureAuthOwner()).toBe(ownerBefore);
    expect(meCallCount()).toBe(1);
    expect(transport.fetchVaults).toHaveBeenCalledTimes(1);
  });

  it("does not let an A promise or finalizer inherit into public owner replacement B", async () => {
    const aMe = deferred<{ data: User }>();
    const bMe = deferred<{ data: User }>();
    mockInitReads([aMe.promise, bMe.promise]);
    const aInit = useAuthStore.getState().init();
    await waitFor(() => expect(transport.get).toHaveBeenCalledTimes(1));

    setUnauthenticatedState("jwt-b");
    const ownerB = captureAuthOwner();
    const bInit = useAuthStore.getState().init();
    await waitFor(() => expect(meCallCount()).toBe(2));

    await act(async () => aMe.resolve({ data: userA }));
    await aInit;

    expect(useAuthStore.getState()).toMatchObject({
      accessToken: "jwt-b",
      isLoading: true,
      isInitialized: false,
    });

    const bConcurrent = useAuthStore.getState().init();
    expect(meCallCount()).toBe(2);

    await act(async () => {
      bMe.resolve({ data: userB });
      await bInit;
      await bConcurrent;
    });

    expect(captureAuthOwner()).toBe(ownerB);
    expect(meCallCount()).toBe(2);
    expect(transport.fetchVaults).toHaveBeenCalledTimes(1);
    expect(useAuthStore.getState()).toMatchObject({
      user: userB,
      accessToken: "jwt-b",
      isAuthenticated: true,
      isInitialized: true,
      isLoading: false,
    });
  });

  it("stops A vault publication on a reentrant subscriber replacement and lets B initialize", async () => {
    const aMe = deferred<{ data: User }>();
    mockInitReads([aMe.promise, Promise.resolve({ data: userB })]);
    let replaced = false;
    const unsubscribe = useAuthStore.subscribe((state, previous) => {
      if (!replaced && state.user?.id === userA.id && previous.user?.id !== userA.id) {
        replaced = true;
        reserveReplacementAuthOwner();
        setUnauthenticatedState("jwt-b");
      }
    });

    try {
      const aInit = useAuthStore.getState().init();
      await waitFor(() => expect(transport.get).toHaveBeenCalledTimes(1));
      await act(async () => aMe.resolve({ data: userA }));
      await aInit;

      expect(replaced).toBe(true);
      expect(transport.fetchVaults).not.toHaveBeenCalled();

      await act(async () => {
        await useAuthStore.getState().init();
      });

      expect(meCallCount()).toBe(2);
      expect(transport.fetchVaults).toHaveBeenCalledTimes(1);
      expect(useAuthStore.getState()).toMatchObject({
        user: userB,
        accessToken: "jwt-b",
        isAuthenticated: true,
        isInitialized: true,
        isLoading: false,
      });
    } finally {
      unsubscribe();
    }
  });

  it("coalesces a same-owner public init called by its own null-to-user subscriber", async () => {
    mockInitReads([Promise.resolve({ data: userA })]);
    let nestedInitCalls = 0;
    const unsubscribe = useAuthStore.subscribe((state, previous) => {
      if (state.user?.id === userA.id && previous.user?.id !== userA.id) {
        nestedInitCalls += 1;
        void useAuthStore.getState().init();
      }
    });

    try {
      await act(async () => {
        await useAuthStore.getState().init();
      });

      expect(nestedInitCalls).toBe(1);
      expect(meCallCount()).toBe(1);
      expect(transport.get).toHaveBeenCalledWith("/auth/setup-status");
      expect(transport.fetchVaults).toHaveBeenCalledTimes(1);
      expect(useAuthStore.getState()).toMatchObject({
        user: userA,
        isAuthenticated: true,
        isInitialized: true,
        isLoading: false,
      });
    } finally {
      unsubscribe();
    }
  });
  it("recovers from an invalid setup verdict on the next public init without resetInitState", async () => {
    setUnauthenticatedState(null);
    const ownerBefore = captureAuthOwner();
    let setupCalls = 0;
    vi.mocked(refreshAccessToken).mockResolvedValue(null);
    transport.get.mockImplementation((url: string) => {
      if (url !== "/auth/setup-status") {
        return Promise.reject(new Error(`unexpected auth URL: ${url}`));
      }
      setupCalls += 1;
      return setupCalls === 1
        ? Promise.resolve({ data: {} })
        : Promise.resolve({ data: { needs_setup: false, auth_mode: "jwt" } });
    });

    await act(async () => {
      await useAuthStore.getState().init();
    });

    expect(setupCalls).toBe(1);
    expect(meCallCount()).toBe(0);
    expect(transport.fetchVaults).not.toHaveBeenCalled();
    expect(captureAuthOwner()).toBe(ownerBefore);
    expect(useAuthStore.getState()).toMatchObject({
      user: null,
      accessToken: null,
      isAuthenticated: false,
      needsSetup: null,
      isInitialized: false,
      initializationFailed: true,
      isLoading: false,
    });

    await act(async () => {
      await useAuthStore.getState().init();
    });

    expect(setupCalls).toBe(2);
    expect(refreshAccessToken).toHaveBeenCalledTimes(2);
    expect(meCallCount()).toBe(0);
    expect(transport.fetchVaults).not.toHaveBeenCalled();
    expect(captureAuthOwner()).toBe(ownerBefore);
    expect(useAuthStore.getState()).toMatchObject({
      user: null,
      accessToken: null,
      isAuthenticated: false,
      needsSetup: false,
      authMode: "jwt",
      isInitialized: true,
      initializationFailed: false,
      isLoading: false,
    });
  });

  it("retires a completed init scope after public setup failure and permits retry", async () => {
    mockInitReads([Promise.resolve({ data: userA })]);
    await act(async () => {
      await useAuthStore.getState().init();
    });

    expect(useAuthStore.getState()).toMatchObject({
      user: userA,
      isAuthenticated: true,
      isInitialized: true,
      isLoading: false,
      needsSetup: false,
      initializationFailed: false,
    });
    expect(transport.fetchVaults).toHaveBeenCalledTimes(1);

    const setupStatusCalls = () =>
      transport.get.mock.calls.filter(([url]) => url === "/auth/setup-status").length;
    const setupCallsBeforeFailure = setupStatusCalls();
    transport.get.mockImplementation((url: string) => {
      if (url === "/auth/setup-status") {
        return Promise.reject(new Error("setup outage"));
      }
      return Promise.reject(new Error("unexpected auth URL: " + url));
    });

    await expect(useAuthStore.getState().checkSetupStatus()).rejects.toThrow("setup outage");
    expect(setupStatusCalls()).toBe(setupCallsBeforeFailure + 1);
    expect(useAuthStore.getState()).toMatchObject({
      needsSetup: null,
      isInitialized: false,
      initializationFailed: true,
      isLoading: false,
    });

    mockInitReads([Promise.resolve({ data: userA })]);
    await act(async () => {
      await useAuthStore.getState().init();
    });

    expect(setupStatusCalls()).toBe(setupCallsBeforeFailure + 2);
    expect(useAuthStore.getState()).toMatchObject({
      user: userA,
      isAuthenticated: true,
      isInitialized: true,
      isLoading: false,
      needsSetup: false,
      initializationFailed: false,
    });
  });

  it("rejects a late A setup failure without altering current owner B state", async () => {
    const setupA = deferred<{
      data: { needs_setup: boolean; auth_mode: "jwt" };
    }>();
    transport.get.mockImplementation((url: string) => {
      if (url === "/auth/setup-status") return setupA.promise;
      return Promise.reject(new Error("unexpected auth URL: " + url));
    });

    const ownerA = captureAuthOwner();
    const aStatus = useAuthStore.getState().checkSetupStatus();
    const aOutcome = aStatus.then(() => null, (error: unknown) => error);
    try {
    await waitFor(() =>
      expect(transport.get).toHaveBeenCalledWith("/auth/setup-status")
    );

    setUnauthenticatedState("jwt-b");
    const ownerB = captureAuthOwner();
    expect(ownerB).not.toBe(ownerA);
    transport.get.mockImplementation((url: string) => {
      if (url === "/auth/me") return Promise.resolve({ data: userB });
      if (url === "/auth/setup-status") {
        return Promise.resolve({ data: { needs_setup: false, auth_mode: "jwt" } });
      }
      return Promise.reject(new Error("unexpected auth URL: " + url));
    });

    await act(async () => {
      await useAuthStore.getState().init();
    });
    expect(useAuthStore.getState()).toMatchObject({
      user: userB,
      accessToken: "jwt-b",
      isAuthenticated: true,
      isInitialized: true,
      isLoading: false,
      needsSetup: false,
      initializationFailed: false,
    });
    expect(captureAuthOwner()).toBe(ownerB);

    await act(async () => {
      setupA.reject(new Error("late A failure"));
    });
    expect(await aOutcome).toMatchObject({ message: "late A failure" });

    expect(useAuthStore.getState()).toMatchObject({
      user: userB,
      accessToken: "jwt-b",
      isAuthenticated: true,
      isInitialized: true,
      isLoading: false,
      needsSetup: false,
      initializationFailed: false,
    });
    expect(captureAuthOwner()).toBe(ownerB);
    expect(
      transport.get.mock.calls.filter(([url]) => url === "/auth/setup-status")
    ).toHaveLength(2);
    } finally {
      setupA.resolve({ data: { needs_setup: false, auth_mode: "jwt" } });
      await aOutcome;
    }
  });

});
