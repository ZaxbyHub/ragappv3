/**
 * Issue #774 L03 red checkpoint — two sessions.ts defects:
 *
 * AC9: parseSSEStream's read loop has no inactivity guard, so a stream whose
 *      reader never yields (stalled connection) parks the promise forever.
 *      A healthy stream yields bytes at least every ~15s (backend heartbeat),
 *      so 600s of fake silence is definitively stalled.
 *
 * AC10: chatStream's token_expired branch sleeps a fixed 1s and then calls
 *       refreshAccessToken with no abort-signal check — disposing during the
 *       wait still fires the refresh (and a follow-up request the caller
 *       believes cancelled).
 *
 * The ./core partial mock mirrors frontend/src/lib/api/__tests__/sessions.reconnect.test.ts.
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
    // Non-null so chatStream's 401 token_expired branch is reachable.
    return "live-jwt";
  },
  getCsrfCookie: () => null,
  getCsrfToken: () => null,
  ensureCsrfToken: (...args: unknown[]) =>
    coreMocks.ensureCsrfToken(...(args as [])) as Promise<string>,
  refreshAccessToken: (...args: unknown[]) =>
    coreMocks.refreshAccessToken(...(args as [])) as Promise<string | null>,
  isTokenNearExpiry: (...args: unknown[]) =>
    coreMocks.isTokenNearExpiry(...(args as [])) as boolean,
}));

import { chatStream, parseSSEStream } from "./sessions";

describe("issue 774 parseSSEStream inactivity guard", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("parseSSEStream gives up on a stalled stream", async () => {
    const fakeReader = {
      read: vi.fn(() => new Promise(() => undefined)),
      cancel: vi.fn(() => Promise.resolve()),
    };

    let settledOrErrored = false;
    const streamPromise = parseSSEStream(
      fakeReader as unknown as ReadableStreamDefaultReader<Uint8Array>,
      {
        onMessage: () => undefined,
        onComplete: () => undefined,
        onError: () => {
          settledOrErrored = true;
        },
      },
    );
    streamPromise.then(
      () => {
        settledOrErrored = true;
      },
      () => {
        settledOrErrored = true;
      },
    );

    // 10 minutes of fake time with zero bytes — far beyond the ~15s
    // heartbeat cadence a healthy stream always beats. advanceTimersByTimeAsync
    // also flushes microtasks.
    await vi.advanceTimersByTimeAsync(600_000);

    expect(settledOrErrored).toBe(true);
  });
});

describe("issue 774 chatStream token_expired dispose", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    vi.useFakeTimers();
    fetchMock = vi.fn(async () => {
      const response401 = {
        ok: false,
        status: 401,
        json: async () => ({ detail: "token_expired" }),
      };
      return response401 as unknown as Response;
    });
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
    vi.clearAllMocks();
  });

  it("dispose during the token_expired wait never refreshes the token", async () => {
    const dispose = chatStream(
      [{ role: "user", content: "q" }] as never,
      { onMessage: () => undefined, onError: () => undefined } as never,
      1,
    );

    // Flush microtasks until the stream is parked inside the 1s
    // token_expired backoff sleep (fake timers hold it there).
    await vi.advanceTimersByTimeAsync(0);
    await vi.advanceTimersByTimeAsync(0);
    expect(fetchMock).toHaveBeenCalledTimes(1);

    // The caller disposes while the stream is still in the wait window.
    dispose();

    // The 1s sleep elapses — the disposed stream must NOT refresh the token.
    await vi.advanceTimersByTimeAsync(1000);

    expect(coreMocks.refreshAccessToken.mock.calls.length).toBe(0);
  });
});
