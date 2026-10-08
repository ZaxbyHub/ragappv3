/**
 * Candidate preserving coverage for the sessions.ts ownership boundaries.
 *
 * parseSSEStream owns reader cancellation on inactivity, and chatStream owns
 * the AbortController returned through its disposer. Hook or component state
 * ownership belongs above this API layer and is intentionally not asserted.
 * The deferred operations are settled in finally blocks so an assertion
 * failure cannot leave a reader or fetch promise alive between tests.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const coreMocks = vi.hoisted(() => ({
  ensureCsrfToken: vi.fn(async () => "csrf"),
  refreshAccessToken: vi.fn(async () => "fresh-token"),
  isTokenNearExpiry: vi.fn(() => false),
}));

vi.mock("./core", () => ({
  apiClient: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
  API_BASE_URL: "/api",
  get _jwtAccessToken(): string | null {
    return null;
  },
  getCsrfCookie: () => null,
  getCsrfToken: () => null,
  ensureCsrfToken: (...args: unknown[]) => coreMocks.ensureCsrfToken(...(args as [])),
  refreshAccessToken: (...args: unknown[]) => coreMocks.refreshAccessToken(...(args as [])),
  isTokenNearExpiry: (...args: unknown[]) => coreMocks.isTokenNearExpiry(...(args as [])),
}));

import { chatStream, parseSSEStream } from "./sessions";

function createDeferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

describe("sessions.ts cancellation ownership", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    vi.clearAllMocks();
  });

  it("cancels the reader and reports one ChatInterruptedError when inactivity expires", async () => {
    const readDeferred = createDeferred<ReadableStreamReadResult<Uint8Array>>();
    const cancelDeferred = createDeferred<void>();
    const reader = {
      read: vi.fn(() => readDeferred.promise),
      cancel: vi.fn(() => cancelDeferred.promise),
    };
    const onError = vi.fn();
    const pending = parseSSEStream(
      reader as unknown as ReadableStreamDefaultReader<Uint8Array>,
      { onError } as never,
    );

    try {
      await vi.advanceTimersByTimeAsync(150_000);
      expect(reader.cancel).toHaveBeenCalledTimes(1);
      cancelDeferred.resolve(undefined);
      await pending;

      expect(onError).toHaveBeenCalledTimes(1);
      expect(onError.mock.calls[0]?.[0]).toEqual(
        expect.objectContaining({ name: "ChatInterruptedError" }),
      );
    } finally {
      cancelDeferred.resolve(undefined);
      readDeferred.resolve({ done: true, value: undefined });
      await pending.catch(() => undefined);
    }
  });

  it("returns a disposer that aborts its fetch owner without publishing AbortError", async () => {
    const fetchDeferred = createDeferred<Response>();
    let fetchSettled = false;
    const settleFetch = () => {
      if (!fetchSettled) {
        fetchSettled = true;
        fetchDeferred.reject(Object.assign(new Error("aborted"), { name: "AbortError" }));
      }
    };
    const fetchMock = vi.fn(() => fetchDeferred.promise);
    vi.stubGlobal("fetch", fetchMock);
    const onError = vi.fn();
    let dispose: (() => void) | undefined;

    try {
      dispose = chatStream(
        [{ role: "user", content: "question" }] as never,
        { onError } as never,
        7,
      );
      await vi.advanceTimersByTimeAsync(0);

      expect(fetchMock).toHaveBeenCalledTimes(1);
      const request = fetchMock.mock.calls[0]?.[1] as RequestInit;
      expect(request.signal).toBeInstanceOf(AbortSignal);
      expect((request.signal as AbortSignal).aborted).toBe(false);

      dispose();
      expect((request.signal as AbortSignal).aborted).toBe(true);

      settleFetch();
      await vi.advanceTimersByTimeAsync(0);
      expect(onError).not.toHaveBeenCalled();
    } finally {
      dispose?.();
      settleFetch();
      await vi.advanceTimersByTimeAsync(0);
    }
  });
});
