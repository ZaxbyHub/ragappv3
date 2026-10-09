import axios, {
  AxiosError,
  type AxiosResponse,
  type InternalAxiosRequestConfig,
} from "axios";
import {
  afterEach,
  beforeEach,
  expect,
  it,
  vi,
} from "vitest";
import {
  apiClient,
  attachCsrfInterceptor,
  ensureCsrfToken,
  getCsrfToken,
  getJwtAccessToken,
  resetCsrfToken,
  setJwtAccessToken,
} from "@/lib/api/core";
import {
  StaleAuthOwnerError,
  reserveReplacementAuthOwner,
} from "@/lib/api/auth-lifecycle";

const originalAdapter = apiClient.defaults.adapter;

function ok(config: InternalAxiosRequestConfig, data: unknown = { current: true }): AxiosResponse {
  return {
    config,
    data,
    headers: {},
    status: 200,
    statusText: "OK",
  };
}

function unauthorized(config: InternalAxiosRequestConfig, detail: string): AxiosError {
  return new AxiosError(
    "Request failed with status code 401",
    "ERR_BAD_REQUEST",
    config,
    undefined,
    { config, data: { detail }, headers: {}, status: 401, statusText: "Unauthorized" },
  );
}

beforeEach(() => {
  document.cookie = "X-CSRF-Token=; path=/; max-age=0";
});

afterEach(() => {
  document.cookie = "X-CSRF-Token=; path=/; max-age=0";
  apiClient.defaults.adapter = originalAdapter;
  resetCsrfToken();
  setJwtAccessToken(null);
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

it("captures the apiClient invocation owner before a replacement can reach the JWT microtask", async () => {
  reserveReplacementAuthOwner();
  setJwtAccessToken("session-A-token");
  let dispatches = 0;
  apiClient.defaults.adapter = async (config) => {
    dispatches += 1;
    return ok(config);
  };

  const retired = apiClient.get("/issue775-invocation-owner");
  const retiredOutcome = retired.then(
    (response) => ({ response, error: undefined }),
    (error: unknown) => ({ response: undefined, error }),
  );
  reserveReplacementAuthOwner();
  setJwtAccessToken("session-B-token");

  expect((await retiredOutcome).error).toBeInstanceOf(StaleAuthOwnerError);
  expect(dispatches).toBe(0);
  expect(getJwtAccessToken()).toBe("session-B-token");

  const current = await apiClient.get("/issue775-current-owner");
  expect(current.config.headers.Authorization).toBe("Bearer session-B-token");
  expect(dispatches).toBe(1);
});

it("admission-checks a helper client GET through attachCsrfInterceptor", async () => {
  const helper = axios.create();
  attachCsrfInterceptor(helper);
  reserveReplacementAuthOwner();
  let dispatches = 0;
  helper.defaults.adapter = async (config) => {
    dispatches += 1;
    return ok(config);
  };

  const retired = helper.get("/issue775-helper-owner");
  const retiredOutcome = retired.then(
    (response) => ({ response, error: undefined }),
    (error: unknown) => ({ response: undefined, error }),
  );
  reserveReplacementAuthOwner();

  expect((await retiredOutcome).error).toBeInstanceOf(StaleAuthOwnerError);
  expect(dispatches).toBe(0);
  await expect(helper.get("/issue775-helper-current")).resolves.toMatchObject({
    data: { current: true },
  });
  expect(dispatches).toBe(1);
});

it("keeps a replacement session isolated while a small Blob CSRF 403 is decoded", async () => {
  reserveReplacementAuthOwner();
  setJwtAccessToken("session-A-token");
  let rejectA!: (reason: unknown) => void;
  let dispatchedA!: () => void;
  const dispatched = new Promise<void>((resolve) => { dispatchedA = resolve; });
  let configA!: InternalAxiosRequestConfig;
  let dispatches = 0;
  apiClient.defaults.adapter = async (config) => {
    dispatches += 1;
    if (dispatches === 1) {
      configA = config;
      dispatchedA();
      return new Promise<AxiosResponse>((_, reject) => { rejectA = reject; });
    }
    return ok(config);
  };

  let releaseBlobText!: (value: string) => void;
  const heldText = new Promise<string>((resolve) => { releaseBlobText = resolve; });
  let markBlobStarted!: () => void;
  const blobStarted = new Promise<void>((resolve) => { markBlobStarted = resolve; });
  const blob = new Blob(["placeholder"], { type: "application/json" });
  Object.defineProperty(blob, "text", {
    configurable: true,
    value: () => {
      markBlobStarted();
      return heldText;
    },
  });
  const retired = apiClient.get("/issue775-held-blob");
  const retiredOutcome = retired.then(
    (response) => ({ response, error: undefined }),
    (error: unknown) => ({ response: undefined, error }),
  );
  try {
    await dispatched;
    rejectA(new AxiosError(
    "Request failed with status code 403",
    "ERR_BAD_REQUEST",
    configA,
    undefined,
    { config: configA, data: blob, headers: { "x-csrf-error": "true" }, status: 403, statusText: "Forbidden" },
    ));
    await blobStarted;

    reserveReplacementAuthOwner();
    setJwtAccessToken("session-B-token");
    document.cookie = "X-CSRF-Token=csrf-B; path=/";
    await expect(ensureCsrfToken()).resolves.toBe("csrf-B");
    expect(getCsrfToken()).toBe("csrf-B");
    releaseBlobText('{"detail":"csrf"}');

    expect((await retiredOutcome).error).toBeInstanceOf(StaleAuthOwnerError);
    expect(dispatches).toBe(1);
    expect(getJwtAccessToken()).toBe("session-B-token");
    expect(getCsrfToken()).toBe("csrf-B");
    const current = await apiClient.post("/issue775-blob-current", { value: true });
    expect(current.config.headers.Authorization).toBe("Bearer session-B-token");
    expect(current.config.headers["X-CSRF-Token"]).toBe("csrf-B");
    expect(dispatches).toBe(2);
  } finally {
    if (rejectA) rejectA(new Error("blob cleanup"));
    if (releaseBlobText) releaseBlobText('{"detail":"cleanup"}');
    await retiredOutcome;
  }
});

it("does not refresh or retry a retired 401 after the existing backoff", async () => {
  vi.useFakeTimers();
  const scheduledSetTimeout = globalThis.setTimeout;
  let markBackoffEntered!: () => void;
  const backoffEntered = new Promise<void>((resolve) => { markBackoffEntered = resolve; });
  const timeoutSpy = vi.spyOn(globalThis, "setTimeout").mockImplementation((callback, delay, ...args) => {
    if (delay === 1_000) markBackoffEntered();
    return scheduledSetTimeout(callback, delay, ...args);
  });
  reserveReplacementAuthOwner();
  setJwtAccessToken("session-A-token");
  let rejectA!: (reason: unknown) => void;
  let dispatchedA!: () => void;
  const dispatched = new Promise<void>((resolve) => { dispatchedA = resolve; });
  let configA!: InternalAxiosRequestConfig;
  let dispatches = 0;
  apiClient.defaults.adapter = async (config) => {
    dispatches += 1;
    if (dispatches === 1) {
      configA = config;
      dispatchedA();
      return new Promise<AxiosResponse>((_, reject) => { rejectA = reject; });
    }
    return ok(config);
  };

  const retired = apiClient.get("/issue775-held-401");
  const retiredOutcome = retired.then(
    (response) => ({ response, error: undefined }),
    (error: unknown) => ({ response: undefined, error }),
  );
  try {
    await dispatched;
    rejectA(unauthorized(configA, "expired"));
    await backoffEntered;
    timeoutSpy.mockRestore();
    reserveReplacementAuthOwner();
    setJwtAccessToken("session-B-token");
    await vi.advanceTimersByTimeAsync(1_000);

    expect((await retiredOutcome).error).toBeInstanceOf(StaleAuthOwnerError);
    expect(dispatches).toBe(1);
    const current = await apiClient.get("/issue775-refresh-current");
    expect(current.data).toEqual({ current: true });
    expect(current.config.headers.Authorization).toBe("Bearer session-B-token");
    expect(dispatches).toBe(2);
  } finally {
    timeoutSpy.mockRestore();
    reserveReplacementAuthOwner();
    if (rejectA) rejectA(new Error("401 cleanup"));
    await vi.advanceTimersByTimeAsync(1_000);
    await retiredOutcome;
  }
});

it("does not retry a retired transient failure after the existing delay", async () => {
  vi.useFakeTimers();
  const scheduledSetTimeout = globalThis.setTimeout;
  let markTransientEntered!: () => void;
  const transientEntered = new Promise<void>((resolve) => { markTransientEntered = resolve; });
  const timeoutSpy = vi.spyOn(globalThis, "setTimeout").mockImplementation((callback, delay, ...args) => {
    if (delay === 300) markTransientEntered();
    return scheduledSetTimeout(callback, delay, ...args);
  });
  reserveReplacementAuthOwner();
  setJwtAccessToken("session-A-token");
  let rejectA!: (reason: unknown) => void;
  let dispatchedA!: () => void;
  const dispatched = new Promise<void>((resolve) => { dispatchedA = resolve; });
  let configA!: InternalAxiosRequestConfig;
  let dispatches = 0;
  apiClient.defaults.adapter = async (config) => {
    dispatches += 1;
    if (dispatches === 1) {
      configA = config;
      dispatchedA();
      return new Promise<AxiosResponse>((_, reject) => { rejectA = reject; });
    }
    return ok(config);
  };

  const retired = apiClient.get("/issue775-held-503");
  const retiredOutcome = retired.then(
    (response) => ({ response, error: undefined }),
    (error: unknown) => ({ response: undefined, error }),
  );
  try {
    await dispatched;
    rejectA(new AxiosError(
    "Request failed with status code 503",
    "ERR_BAD_RESPONSE",
    configA,
    undefined,
    { config: configA, data: { detail: "temporarily unavailable" }, headers: {}, status: 503, statusText: "Service Unavailable" },
    ));
    await transientEntered;
    timeoutSpy.mockRestore();
    reserveReplacementAuthOwner();
    setJwtAccessToken("session-B-token");
    await vi.advanceTimersByTimeAsync(300);

    expect((await retiredOutcome).error).toBeInstanceOf(StaleAuthOwnerError);
    expect(dispatches).toBe(1);
    const current = await apiClient.get("/issue775-transient-current");
    expect(current.data).toEqual({ current: true });
    expect(current.config.headers.Authorization).toBe("Bearer session-B-token");
    expect(dispatches).toBe(2);
  } finally {
    timeoutSpy.mockRestore();
    reserveReplacementAuthOwner();
    if (rejectA) rejectA(new Error("503 cleanup"));
    await vi.advanceTimersByTimeAsync(300);
    await retiredOutcome;
  }
});

it("uses the logical CSRF deadline for an ordinary Axios POST with a non-cooperative body", async () => {
  vi.useFakeTimers();
  reserveReplacementAuthOwner();
  setJwtAccessToken("session-A-token");
  let releaseBody!: (value: { csrf_token: string }) => void;
  const heldBody = new Promise<{ csrf_token: string }>((resolve) => { releaseBody = resolve; });
  let markBodyStarted!: () => void;
  const bodyStarted = new Promise<void>((resolve) => { markBodyStarted = resolve; });
  let fetches = 0;
  vi.stubGlobal("fetch", () => {
    fetches += 1;
    if (fetches === 1) {
      return Promise.resolve({
        json: async () => { markBodyStarted(); return heldBody; },
        ok: true,
      } as Response);
    }
    return Promise.resolve({
      json: async () => ({ csrf_token: "csrf-current" }),
      ok: true,
    } as Response);
  });
  let dispatches = 0;
  apiClient.defaults.adapter = async (config) => {
    dispatches += 1;
    return ok(config);
  };

  const pending = apiClient.post("/issue775-logical-csrf-timeout", { value: true });
  const pendingOutcome = pending.then(
    (response) => ({ response, error: undefined }),
    (error: unknown) => ({ response: undefined, error }),
  );
  try {
    await bodyStarted;
    await vi.advanceTimersByTimeAsync(10_000);
    expect((await pendingOutcome).error).toMatchObject({
      message: "csrf token fetch timed out",
    });
    expect(dispatches).toBe(0);
  } finally {
    releaseBody({ csrf_token: "late-token" });
    await pendingOutcome;
  }

  const current = await apiClient.post("/issue775-logical-csrf-current", { value: true });
  expect(current.config.headers.Authorization).toBe("Bearer session-A-token");
  expect(current.config.headers["X-CSRF-Token"]).toBe("csrf-current");
  expect(dispatches).toBe(1);
});
