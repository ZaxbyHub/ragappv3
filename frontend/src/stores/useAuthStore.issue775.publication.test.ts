import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const authClient = vi.hoisted(() => ({
  get: vi.fn().mockResolvedValue({ data: { csrf_token: "csrf-test" } }),
  post: vi.fn().mockResolvedValue({
    data: {
      access_token: "store-token",
      user: {
        id: 90,
        username: "store-user-90",
        full_name: "Store User 90",
        role: "member",
        is_active: true,
      },
    },
  }),
  patch: vi.fn(),
  interceptors: {
    request: { use: vi.fn() },
    response: { use: vi.fn() },
  },
}));

vi.mock("axios", () => ({
  default: {
    create: vi.fn(() => authClient),
    get: vi.fn(),
    post: vi.fn(),
  },
}));
import {
  canNavigateRegisterPublication,
  captureRegisterPublicationScope,
  useAuthStore,
  withRegisterPublicationScope,
} from "@/stores/useAuthStore";
import {
  captureAuthPrincipalGeneration,
  captureAuthOwner,
  enqueueAuthTransport,
  publishAuthPrincipal,
  reserveReplacementAuthOwner,
} from "@/lib/api/auth-lifecycle";
import { getJwtAccessToken, setJwtAccessToken } from "@/lib/api/core";

const pendingDeferreds = new Set<() => void>();

function deferred<T>(cleanupValue: T) {
  let settled = false;
  let resolvePromise!: (value: T) => void;
  let rejectPromise!: (error: unknown) => void;
  const promise = new Promise<T>((resolve, reject) => {
    resolvePromise = (value) => { settled = true; resolve(value); };
    rejectPromise = (error) => { settled = true; reject(error); };
  });
  pendingDeferreds.add(() => { if (!settled) resolvePromise(cleanupValue); });
  return { promise, resolve: resolvePromise, reject: rejectPromise };
}

function principal(id: number, role: "admin" | "member" = "member") {
  return {
    id,
    username: `store-user-${id}`,
    full_name: `Store User ${id}`,
    role,
    is_active: true,
  } as const;
}

describe("Register store publication contract", () => {
  const originalState = useAuthStore.getState();
  const originalJwt = getJwtAccessToken();

  afterEach(async () => {
    reserveReplacementAuthOwner();
    for (const settle of pendingDeferreds) settle();
    pendingDeferreds.clear();
    await Promise.resolve();
    await Promise.resolve();
    setJwtAccessToken(originalJwt);
    useAuthStore.setState(originalState, true);
  });

  beforeEach(() => {
    vi.clearAllMocks();
    authClient.post.mockReset().mockResolvedValue({ data: { access_token: "store-token", user: principal(90) } });
    authClient.patch.mockReset();
    authClient.get.mockReset().mockResolvedValue({ data: { csrf_token: "csrf-test" } });
    reserveReplacementAuthOwner();
    setJwtAccessToken(null);
    publishAuthPrincipal(null);
    useAuthStore.setState({
      user: null,
      accessToken: null,
      isAuthenticated: false,
      isLoading: false,
      needsSetup: null,
      initializationFailed: false,
      isInitialized: false,
      authMode: "unknown",
    });
  });

  it("uses the real lifecycle owner and principal publisher for direct three-argument register", async () => {
    const scope = captureRegisterPublicationScope();
    const result = await withRegisterPublicationScope(scope, () =>
      useAuthStore.getState().register("store-direct", "GoodPass1", "Store Direct"),
    );
    expect(result).toBeUndefined();
    expect(getJwtAccessToken()).toBe("store-token");
    expect(useAuthStore.getState().user).toMatchObject({ id: 90, username: "store-user-90", role: "member" });
    expect(scope.receipt?.user).toBe(useAuthStore.getState().user);
    expect(canNavigateRegisterPublication(scope)).toBe(true);
    expect(useAuthStore.getState().isLoading).toBe(false);
  });

  it("restores an outer scope after an inner scoped action is consumed", async () => {
    const staleInner = captureRegisterPublicationScope();
    reserveReplacementAuthOwner();
    const outer = captureRegisterPublicationScope();
    const nested = withRegisterPublicationScope(outer, () => {
      const inner = withRegisterPublicationScope(staleInner, () =>
        useAuthStore.getState().register("stale-inner", "GoodPass1"),
      );
      const outerAction = useAuthStore.getState().register("outer", "GoodPass1", "Outer");
      return Promise.all([inner, outerAction]);
    });
    await nested;
    expect(staleInner.reservedOwner).toBeUndefined();
    expect(outer.receipt?.user).toBe(useAuthStore.getState().user);
  });

  it("publishes a JWT holder through the real core bridge before store state", () => {
    setJwtAccessToken("store-token");
    expect(getJwtAccessToken()).toBe("store-token");
  });

  it("binds deferred FIFO dispatch to its captured principal predicate", async () => {
    const owner = captureAuthOwner();
    const generation = captureAuthPrincipalGeneration();
    const firstStarted = deferred<void>(undefined);
    const first = enqueueAuthTransport(owner, async () => {
      await firstStarted.promise;
      return "first";
    });
    const result = enqueueAuthTransport(
      owner,
      async (context) => {
        context.assertCurrent();
        return "registered";
      },
      undefined,
      () => captureAuthPrincipalGeneration() === generation,
    );
    void first.catch(() => undefined);
    void result.catch(() => undefined);
    publishAuthPrincipal(principal(91));
    firstStarted.resolve(undefined);
    await expect(first).resolves.toBe("first");
    await expect(result).rejects.toThrow("Authentication owner was replaced");
  });

  it("retires a role transition even when the token holder remains unchanged", () => {
    setJwtAccessToken("same-holder");
    const before = captureAuthPrincipalGeneration();
    useAuthStore.setState({ user: principal(92, "member") });
    const afterMember = captureAuthPrincipalGeneration();
    useAuthStore.setState({ user: principal(92, "admin") });
    expect(afterMember).toBeGreaterThan(before);
    expect(captureAuthPrincipalGeneration()).toBeGreaterThan(afterMember);
  });

  it("keeps a newer same-owner profile mutation busy after Register settles", async () => {
    setJwtAccessToken("profile-token");
    useAuthStore.setState({ accessToken: "profile-token", user: principal(90), isAuthenticated: true });
    const registerRequest = deferred({ data: { access_token: "cleanup-register", user: principal(90) } });
    authClient.post.mockImplementationOnce(() => registerRequest.promise);
    const profileRequest = deferred({ data: { ...principal(90), full_name: "cleanup-profile" } });
    authClient.patch.mockImplementationOnce(() => profileRequest.promise);
    const registerPromise = useAuthStore.getState().register("profile-register", "GoodPass1");
    await Promise.resolve();
    const profilePromise = useAuthStore.getState().updateProfile({ full_name: "Profile" });
    await Promise.resolve();
    registerRequest.reject(Object.assign(new Error("409"), { response: { status: 409 } }));
    await expect(registerPromise).rejects.toMatchObject({ response: { status: 409 } });
    expect(useAuthStore.getState().isLoading).toBe(true);
    profileRequest.resolve({ data: { ...principal(90), full_name: "Updated Profile" } });
    await profilePromise;
    expect(useAuthStore.getState().isLoading).toBe(false);
    expect(useAuthStore.getState().user?.full_name).toBe("Updated Profile");
  });
});
