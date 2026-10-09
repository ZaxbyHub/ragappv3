import { AxiosError,
  type AxiosResponse,
  type InternalAxiosRequestConfig,
} from "axios";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import {
  apiClient,
  getJwtAccessToken,
  resetCsrfToken,
  setJwtAccessToken,
} from "@/lib/api/core";
import {
  AUTH_TRANSPORT_TIMEOUT_MS,
  AuthTransportTimeoutError,
  StaleAuthOwnerError,
  publishAuthPrincipal,
  reserveReplacementAuthOwner,
} from "@/lib/api/auth-lifecycle";
import {
  changePassword,
  revokeSession,
} from "@/lib/api/auth-sessions";

const originalAdapter = apiClient.defaults.adapter;

type Deferred<T> = {
  promise: Promise<T>;
  resolve: (value?: T) => void;
  reject: (reason?: unknown) => void;
};

function deferred<T>(): Deferred<T> {
  let resolve!: (value?: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((promiseResolve, promiseReject) => {
    resolve = (value) => promiseResolve(value as T);
    reject = promiseReject;
  });
  void promise.catch(() => undefined);
  return { promise, resolve, reject };
}

function ok(config: InternalAxiosRequestConfig, data: unknown = { ok: true }): AxiosResponse {
  return {
    config,
    data,
    headers: {},
    status: 200,
    statusText: "OK",
  };
}

function unauthorized(config: InternalAxiosRequestConfig, detail = "expired"): AxiosError {
  return new AxiosError(
    "Request failed with status code 401",
    "ERR_BAD_REQUEST",
    config,
    undefined,
    { config, data: { detail }, headers: {}, status: 401, statusText: "Unauthorized" },
  );
}

function jsonResponse(data: unknown): Response {
  return {
    ok: true,
    status: 200,
    statusText: "OK",
    headers: new Headers(),
    json: async () => data,
  } as Response;
}

beforeEach(() => {
  document.cookie = "X-CSRF-Token=csrf-test; path=/";
  reserveReplacementAuthOwner();
});

afterEach(() => {
  document.cookie = "X-CSRF-Token=; path=/; max-age=0";
  apiClient.defaults.adapter = originalAdapter;
  resetCsrfToken();
  setJwtAccessToken(null);
  publishAuthPrincipal(null);
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

it("returns the backend credential payload and preserves the exact password endpoint/body", async () => {
  const credentials = { access_token: "returned-token", token_type: "bearer", expires_in: 900 };
  let request!: InternalAxiosRequestConfig;
  apiClient.defaults.adapter = async (config) => {
    request = config;
    return ok(config, credentials);
  };

  await expect(changePassword("old secret", "new secret")).resolves.toEqual(credentials);
  expect(request.method).toBe("post");
  expect(request.url).toBe("/auth/change-password");
  expect(JSON.parse(String(request.data))).toEqual({
    current_password: "old secret",
    new_password: "new secret",
  });
});

it("keeps a stale queued revoke from dispatching, while a replacement owner waits for physical settlement", async () => {
  setJwtAccessToken("session-A-token");
  const heldA = deferred<AxiosResponse>();
  const dispatchedA = deferred<void>();
  let dispatches = 0;
  let configA!: InternalAxiosRequestConfig;
  let configB!: InternalAxiosRequestConfig;
  apiClient.defaults.adapter = async (config) => {
    dispatches += 1;
    if (dispatches === 1) {
      configA = config;
      dispatchedA.resolve();
      return heldA.promise;
    }
    configB = config;
    return ok(config);
  };

  const retired = revokeSession(41);
  const retiredOutcome = retired.then(
    (value) => ({ value, error: undefined }),
    (error: unknown) => ({ value: undefined, error }),
  );
  let queuedRetiredOutcome: Promise<{ error: unknown }> = Promise.resolve({ error: undefined });
  let currentOutcome: Promise<{ value: unknown; error: unknown }> = Promise.resolve({ value: undefined, error: undefined });
  try {
    await dispatchedA.promise;
    queuedRetiredOutcome = revokeSession(43).then(
      () => ({ error: undefined }), (error: unknown) => ({ error }),
    );
    reserveReplacementAuthOwner();
    setJwtAccessToken("session-B-token");
    const current = revokeSession(42);
    currentOutcome = current.then(
      (value) => ({ value, error: undefined }),
      (error: unknown) => ({ value: undefined, error }),
    );

    expect(dispatches).toBe(1);
    heldA.resolve(ok(configA));
    const retiredResult = await retiredOutcome;
    expect(retiredResult.error).toBeInstanceOf(StaleAuthOwnerError);
    await expect(current).resolves.toBeUndefined();
    expect(dispatches).toBe(2);
    expect(configB.url).toBe("/auth/sessions/42");
    expect((await queuedRetiredOutcome).error).toBeInstanceOf(StaleAuthOwnerError);
    expect(configB.headers.Authorization).toBe("Bearer session-B-token");
  } finally {
    heldA.resolve(ok(configA ?? ({} as InternalAxiosRequestConfig)));
    await retiredOutcome;
    await queuedRetiredOutcome;
    await currentOutcome;
  }
});

it("rechecks principal scope after held CSRF acquisition before the adapter, then admits the new principal", async () => {
  resetCsrfToken();
  document.cookie = "X-CSRF-Token=; path=/; max-age=0";
  const csrfBody = deferred<{ csrf_token: string }>();
  const csrfStarted = deferred<void>();
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve({
    ok: true,
    json: async () => {
      csrfStarted.resolve();
      return csrfBody.promise;
    },
  } as Response)));
  let dispatches = 0;
  apiClient.defaults.adapter = async (config) => {
    dispatches += 1;
    return ok(config);
  };

  const retired = revokeSession(51);
  const retiredOutcome = retired.then(
    (value) => ({ value, error: undefined }),
    (error: unknown) => ({ value: undefined, error }),
  );
  let currentOutcome: Promise<{ value: unknown; error: unknown }> = Promise.resolve({ value: undefined, error: undefined });
  try {
    await csrfStarted.promise;
    publishAuthPrincipal({ id: 9001, role: "admin" });
    csrfBody.resolve({ csrf_token: "csrf-retired" });
    const retiredResult = await retiredOutcome;
    expect(retiredResult.error).toBeInstanceOf(StaleAuthOwnerError);
    expect(dispatches).toBe(0);

    const current = revokeSession(52);
    currentOutcome = current.then(
      (value) => ({ value, error: undefined }),
      (error: unknown) => ({ value: undefined, error }),
    );
    await expect(current).resolves.toBeUndefined();
    expect(dispatches).toBe(1);
  } finally {
    csrfBody.resolve({ csrf_token: "csrf-cleanup" });
    await retiredOutcome;
    await currentOutcome;
  }
});

it("does not let a held retired password response return credentials into the replacement session", async () => {
  setJwtAccessToken("session-A-token");
  const heldA = deferred<AxiosResponse>();
  const dispatchedA = deferred<void>();
  const responseA = { access_token: "A-response", token_type: "bearer", expires_in: 60 };
  const responseB = { access_token: "B-response", token_type: "bearer", expires_in: 120 };
  let dispatches = 0;
  let configA!: InternalAxiosRequestConfig;
  let configB!: InternalAxiosRequestConfig;
  apiClient.defaults.adapter = async (config) => {
    dispatches += 1;
    if (dispatches === 1) {
      configA = config;
      dispatchedA.resolve();
      return heldA.promise;
    }
    configB = config;
    return ok(config, responseB);
  };

  const retired = changePassword("old-A", "new-A");
  const retiredOutcome = retired.then(
    (value) => ({ value, error: undefined }),
    (error: unknown) => ({ value: undefined, error }),
  );
  let currentOutcome: Promise<{ value: unknown; error: unknown }> = Promise.resolve({ value: undefined, error: undefined });
  try {
    await dispatchedA.promise;
    reserveReplacementAuthOwner();
    setJwtAccessToken("session-B-token");
    const current = changePassword("old-B", "new-B");
    currentOutcome = current.then(
      (value) => ({ value, error: undefined }),
      (error: unknown) => ({ value: undefined, error }),
    );

    expect(dispatches).toBe(1);
    heldA.resolve(ok(configA, responseA));
    const retiredResult = await retiredOutcome;
    expect(retiredResult.error).toBeInstanceOf(StaleAuthOwnerError);
    await expect(current).resolves.toEqual(responseB);
    expect(dispatches).toBe(2);
    expect(configB.data).toContain("old-B");
    expect(getJwtAccessToken()).toBe("session-B-token");
  } finally {
    heldA.resolve(ok(configA ?? ({} as InternalAxiosRequestConfig), responseA));
    await retiredOutcome;
    await currentOutcome;
  }
});

it("refreshes a 401 inside the password FIFO slot, then releases the queued mutation only after retry response settlement", async () => {
  vi.useFakeTimers();
  setJwtAccessToken("expired-token");
  const retryResponse = deferred<AxiosResponse>();
  const secondResponse = deferred<AxiosResponse>();
  const firstDispatched = deferred<void>();
  const refreshStarted = deferred<void>();
  const retryStarted = deferred<void>();
  const secondStarted = deferred<void>();
  const refreshResponse = deferred<Response>();
  let dispatches = 0;
  let firstConfig!: InternalAxiosRequestConfig;
  let retryConfig!: InternalAxiosRequestConfig;
  let secondConfig!: InternalAxiosRequestConfig;
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
    const url = String(input);
    if (url.endsWith("/csrf-token")) {
      document.cookie = "X-CSRF-Token=csrf-refresh; path=/";
      return Promise.resolve(jsonResponse({ csrf_token: "csrf-refresh" }));
    }
    if (url.endsWith("/auth/refresh")) {
      refreshStarted.resolve();
      return refreshResponse.promise;
    }
    return Promise.reject(new Error(`unexpected fetch: ${url}`));
  }));
  apiClient.defaults.adapter = async (config) => {
    dispatches += 1;
    if (dispatches === 1) {
      firstConfig = config;
      firstDispatched.resolve();
      return Promise.reject(unauthorized(config));
    }
    if (dispatches === 2) {
      retryConfig = config;
      retryStarted.resolve();
      return retryResponse.promise;
    }
    secondConfig = config;
    secondStarted.resolve();
    return secondResponse.promise;
  };

  const first = changePassword("old", "new");
  const firstOutcome = first.then(
    (value) => ({ value, error: undefined }),
    (error: unknown) => ({ value: undefined, error }),
  );
  let secondOutcome: Promise<{ value: unknown; error: unknown }> = Promise.resolve({ value: undefined, error: undefined });
  const originalSetTimeout = globalThis.setTimeout;
  const backoffScheduled = deferred<void>();
  const timeoutSpy = vi.spyOn(globalThis, "setTimeout").mockImplementation((callback, delay, ...args) => {
    if (delay === 1_000) backoffScheduled.resolve();
    return originalSetTimeout(callback, delay, ...args);
  });
  try {
    await firstDispatched.promise;
    const second = revokeSession(63);
    secondOutcome = second.then(
      (value) => ({ value, error: undefined }),
      (error: unknown) => ({ value: undefined, error }),
    );
    await backoffScheduled.promise;
    timeoutSpy.mockRestore();
    await vi.advanceTimersByTimeAsync(1_000);
    await refreshStarted.promise;
    expect(dispatches).toBe(1);

    refreshResponse.resolve(jsonResponse({ access_token: "refreshed-token" }));
    await retryStarted.promise;
    expect(dispatches).toBe(2);
    expect(retryConfig.headers.Authorization).toBe("Bearer refreshed-token");
    expect(getJwtAccessToken()).toBe("refreshed-token");
    expect(dispatches).toBe(2);
    retryResponse.resolve(ok(retryConfig, { access_token: "retry-result", token_type: "bearer", expires_in: 100 }));
    await expect(first).resolves.toEqual({ access_token: "retry-result", token_type: "bearer", expires_in: 100 });
    await secondStarted.promise;
    expect(dispatches).toBe(3);
    expect(secondConfig.url).toBe("/auth/sessions/63");
    secondResponse.resolve(ok(secondConfig));
    await expect(second).resolves.toBeUndefined();
    expect(firstConfig.url).toBe("/auth/change-password");
  } finally {
    timeoutSpy.mockRestore();
    refreshResponse.resolve(jsonResponse({ access_token: "cleanup-token" }));
    retryResponse.resolve(ok(retryConfig ?? ({} as InternalAxiosRequestConfig)));
    secondResponse.resolve(ok(secondConfig ?? ({} as InternalAxiosRequestConfig)));
    await vi.advanceTimersByTimeAsync(1_000);
    await firstOutcome;
    await secondOutcome;
  }
});

it("rejects a typed timeout while the non-cooperative physical request still blocks the next FIFO entry", async () => {
  vi.useFakeTimers();
  setJwtAccessToken("session-A-token");
  const heldA = deferred<AxiosResponse>();
  const dispatchedA = deferred<void>();
  const dispatchedB = deferred<void>();
  let dispatches = 0;
  let configA!: InternalAxiosRequestConfig;
  let configB!: InternalAxiosRequestConfig;
  apiClient.defaults.adapter = async (config) => {
    dispatches += 1;
    if (dispatches === 1) {
      configA = config;
      dispatchedA.resolve();
      return heldA.promise;
    }
    configB = config;
    dispatchedB.resolve();
    return ok(config, { access_token: "B-response", token_type: "bearer", expires_in: 90 });
  };

  const first = changePassword("old-timeout", "new-timeout");
  const firstOutcome = first.then(
    (value) => ({ value, error: undefined }),
    (error: unknown) => ({ value: undefined, error }),
  );
  let secondOutcome: Promise<{ value: unknown; error: unknown }> = Promise.resolve({ value: undefined, error: undefined });
  try {
    await dispatchedA.promise;
    await vi.advanceTimersByTimeAsync(AUTH_TRANSPORT_TIMEOUT_MS);
    expect((await firstOutcome).error).toBeInstanceOf(AuthTransportTimeoutError);
    reserveReplacementAuthOwner();
    setJwtAccessToken("session-B-token");
    const second = changePassword("old-current", "new-current");
    secondOutcome = second.then(
      (value) => ({ value, error: undefined }),
      (error: unknown) => ({ value: undefined, error }),
    );
    expect(dispatches).toBe(1);
    expect(configA.signal?.aborted).toBe(true);

    heldA.resolve(ok(configA, { access_token: "late-A", token_type: "bearer", expires_in: 30 }));
    await dispatchedB.promise;
    await expect(second).resolves.toEqual({ access_token: "B-response", token_type: "bearer", expires_in: 90 });
    expect(configB.data).toContain("old-current");
  } finally {
    heldA.resolve(ok(configA ?? ({} as InternalAxiosRequestConfig), { access_token: "cleanup-A" }));
    await vi.advanceTimersByTimeAsync(AUTH_TRANSPORT_TIMEOUT_MS);
    await firstOutcome;
    await secondOutcome;
  }
});
