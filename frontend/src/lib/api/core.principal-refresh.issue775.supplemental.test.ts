import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  getCsrfToken,
  refreshAccessToken,
  resetCsrfToken,
  setJwtAccessToken,
} from "./core";
import {
  captureAuthOwner,
  captureAuthPrincipalGeneration,
  publishAuthPrincipal,
  reserveReplacementAuthOwner,
  StaleAuthOwnerError,
} from "./auth-lifecycle";

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (reason?: unknown) => void;
}

const pendingDeferreds = new Set<{ resolve: (value: unknown) => void }>();

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((promiseResolve, promiseReject) => {
    resolve = promiseResolve;
    reject = promiseReject;
  });
  promise.catch(() => undefined);
  pendingDeferreds.add({ resolve: resolve as (value: unknown) => void });
  return { promise, resolve, reject };
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

async function settleMicrotasks(): Promise<void> {
  // Flush the full credential/Response/SSE promise chain without advancing
  // retry or inactivity clocks. Assertions still prove actual transport starts.
  for (let turn = 0; turn < 24; turn += 1) await Promise.resolve();
}

describe("public refresh principal scope (issue #775)", () => {
  const fetchMock = vi.fn<typeof fetch>();

  beforeEach(() => {
    vi.useFakeTimers();
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
    resetCsrfToken();
    setJwtAccessToken(null);
    publishAuthPrincipal(null);
    publishAuthPrincipal({ id: 7001, role: "user" });
  });

  afterEach(async () => {
    fetchMock.mockReset();
    reserveReplacementAuthOwner();
    for (const pending of pendingDeferreds) pending.resolve(undefined);
    pendingDeferreds.clear();
    await settleMicrotasks();
    vi.clearAllTimers();
    vi.useRealTimers();
    resetCsrfToken();
    setJwtAccessToken(null);
    reserveReplacementAuthOwner();
    publishAuthPrincipal(null);
    vi.unstubAllGlobals();
  });

  it("uses one public promise for concurrent refresh callers in one owner and principal scope", async () => {
    const csrf = deferred<Response>();
    const refresh = deferred<Response>();
    fetchMock.mockImplementation((input) => {
      const url = String(input);
      if (url.endsWith("/csrf-token")) return csrf.promise;
      if (url.endsWith("/auth/refresh")) return refresh.promise;
      throw new Error(`unexpected fetch: ${url}`);
    });

    const owner = captureAuthOwner();
    const first = refreshAccessToken(owner);
    const second = refreshAccessToken(owner);
    expect(second).toBe(first);
    expect(fetchMock).toHaveBeenCalledTimes(1);

    csrf.resolve(jsonResponse({ csrf_token: "csrf-scope-a" }));
    await settleMicrotasks();
    expect(fetchMock).toHaveBeenCalledTimes(2);
    refresh.resolve(jsonResponse({ access_token: "jwt-scope-a" }));
    await expect(first).resolves.toBe("jwt-scope-a");
    expect(getCsrfToken()).toBeNull();
  });

  it("blocks a stale held CSRF attempt from dispatching refresh or publishing its token", async () => {
    const oldCsrf = deferred<Response>();
    fetchMock.mockImplementation((input) => {
      const url = String(input);
      if (url.endsWith("/csrf-token")) return oldCsrf.promise;
      if (url.endsWith("/auth/refresh")) throw new Error("must never dispatch");
      throw new Error(`unexpected fetch: ${url}`);
    });

    const owner = captureAuthOwner();
    const principalGeneration = captureAuthPrincipalGeneration();
    const stale = refreshAccessToken(owner);
    expect(fetchMock).toHaveBeenCalledTimes(1);

    publishAuthPrincipal({ id: 7002, role: "admin" });
    expect(captureAuthPrincipalGeneration()).toBeGreaterThan(principalGeneration);

    oldCsrf.resolve(jsonResponse({ csrf_token: "stale-csrf" }));
    await expect(stale).rejects.toBeInstanceOf(StaleAuthOwnerError);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(getCsrfToken()).toBeNull();

    await settleMicrotasks();
  });

  it("admits a replacement principal after the retired physical FIFO entry settles", async () => {
    const oldCsrf = deferred<Response>();
    const newCsrf = deferred<Response>();
    const newRefresh = deferred<Response>();
    let csrfCalls = 0;
    fetchMock.mockImplementation((input) => {
      const url = String(input);
      if (url.endsWith("/csrf-token")) {
        csrfCalls += 1;
        return csrfCalls === 1 ? oldCsrf.promise : newCsrf.promise;
      }
      if (url.endsWith("/auth/refresh")) return newRefresh.promise;
      throw new Error(`unexpected fetch: ${url}`);
    });

    const oldOwner = captureAuthOwner();
    const oldPromise = refreshAccessToken(oldOwner);
    publishAuthPrincipal({ id: 7003, role: "reviewer" });
    const newPromise = refreshAccessToken(oldOwner);

    // The public replacement call is queued behind the still-held physical CSRF
    // operation. Resolve the old body only after the principal boundary exists.
    expect(fetchMock).toHaveBeenCalledTimes(1);
    oldCsrf.resolve(jsonResponse({ csrf_token: "old-csrf" }));
    await expect(oldPromise).rejects.toBeInstanceOf(StaleAuthOwnerError);
    await settleMicrotasks();

    expect(fetchMock).toHaveBeenCalledTimes(2);
    newCsrf.resolve(jsonResponse({ csrf_token: "new-csrf" }));
    await settleMicrotasks();
    expect(fetchMock).toHaveBeenCalledTimes(3);
    newRefresh.resolve(jsonResponse({ access_token: "jwt-new-principal" }));
    await expect(newPromise).resolves.toBe("jwt-new-principal");
    expect(getCsrfToken()).toBeNull();
  });
});
