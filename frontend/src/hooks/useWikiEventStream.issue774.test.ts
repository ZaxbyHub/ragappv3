/**
 * Issue #774 regression tests (non-frozen) for useWikiEventStream:
 *  - a transport-class refreshAccessToken rejection during the token_expired
 *    path is contained (treated like a null refresh: "stop"), not an unhandled
 *    rejection;
 *  - the reconnect backoff is abort-aware: disposing during the backoff window
 *    does not issue another fetch after the abort.
 */
import { renderHook, waitFor, cleanup } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const refreshAccessTokenMock = vi.hoisted(() => vi.fn());
const getJwtAccessTokenMock = vi.hoisted(() => vi.fn(() => "test-jwt-token"));

vi.mock("@/lib/api", () => ({
  API_BASE_URL: "/api",
  getJwtAccessToken: getJwtAccessTokenMock,
  refreshAccessToken: refreshAccessTokenMock,
}));

function sseResponse() {
  const encoder = new TextEncoder();
  let resolveRead: ((r: { value?: Uint8Array; done: boolean }) => void) | null = null;
  const reader = {
    read: vi.fn(
      () =>
        new Promise<{ value?: Uint8Array; done: boolean }>((resolve) => {
          resolveRead = resolve;
        })
    ),
    cancel: vi.fn(() => Promise.resolve()),
  };
  const response = {
    ok: true,
    status: 200,
    headers: new Headers(),
    body: { getReader: () => reader },
    json: () => Promise.resolve({}),
  } as unknown as Response;
  const emitHeartbeat = () => {
    resolveRead?.({ value: encoder.encode(": keepalive\n\n"), done: false });
  };
  return { response, reader, emitHeartbeat };
}

function errorResponse(status: number, detail: string): Response {
  return {
    ok: false,
    status,
    headers: new Headers(),
    json: () => Promise.resolve({ detail }),
  } as unknown as Response;
}

describe("useWikiEventStream issue 774 hardening", () => {
  let fetchMock: ReturnType<typeof vi.fn>;
  let useWikiEventStream: typeof import("./useWikiEventStream").useWikiEventStream;

  beforeEach(async () => {
    vi.resetModules();
    refreshAccessTokenMock.mockReset();
    getJwtAccessTokenMock.mockReset();
    getJwtAccessTokenMock.mockReturnValue("test-jwt-token");
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    ({ useWikiEventStream } = await import("./useWikiEventStream"));
  });

  afterEach(() => {
    cleanup();
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("contains a transport-class refresh rejection on the token_expired path (no unhandled rejection, stream stops)", async () => {
    const unhandled = vi.fn();
    process.on("unhandledRejection", unhandled);
    try {
      // 401 token_expired, then the refresh rejects with a network error.
      fetchMock.mockResolvedValueOnce(errorResponse(401, "token_expired"));
      refreshAccessTokenMock.mockRejectedValueOnce(new TypeError("Failed to fetch"));

      renderHook(() => useWikiEventStream(42, vi.fn()));
      await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
      await vi.waitFor(() => expect(refreshAccessTokenMock).toHaveBeenCalledTimes(1));
      // Give any unhandled rejection a chance to surface.
      await new Promise((r) => setTimeout(r, 25));

      expect(unhandled).not.toHaveBeenCalled();
    } finally {
      // The probe listener must not leak when the assertion it guards fails
      // (PRR-021).
      process.off("unhandledRejection", unhandled);
    }
  });

  it("the first backoff cycle's timer-win settle detaches its listener (exact balance; F-004 wiki leg)", async () => {
    vi.useFakeTimers();
    // Call 1 fails (-> "error" -> 1s backoff); from call 2 on the fetch hangs
    // forever, freezing the loop at the instant cycle 1 fully settled.
    fetchMock.mockResolvedValueOnce(errorResponse(503, "unavailable"));
    fetchMock.mockImplementation(() => new Promise<Response>(() => undefined));

    let addCount = 0;
    let allAddCount = 0;
    let allRemoveCount = 0;
    let removeCount = 0;
    // Count the backoff timer listener by callback identity; the owner
    // abort listener is a separate once-only registration.
    const timerListeners = new Set<Parameters<AbortSignal["addEventListener"]>[1]>();
    const activeHookListeners = new Set<Parameters<AbortSignal["addEventListener"]>[1]>();
    const origAdd = AbortSignal.prototype.addEventListener;
    const origRemove = AbortSignal.prototype.removeEventListener;
    const spyAdd = vi
      .spyOn(AbortSignal.prototype, "addEventListener")
      .mockImplementation(function (
        this: AbortSignal,
        ...args: Parameters<AbortSignal["addEventListener"]>
      ) {
        if (args[0] === "abort" && (new Error().stack || "").includes("useWikiEventStream.ts")) {
          activeHookListeners.add(args[1]);
          allAddCount += 1;
          if (args[2] === undefined) addCount += 1;
          if (args[2] === undefined) timerListeners.add(args[1]);
        }
        return origAdd.apply(this, args);
      });
    const spyRemove = vi
      .spyOn(AbortSignal.prototype, "removeEventListener")
      .mockImplementation(function (
        this: AbortSignal,
        ...args: Parameters<AbortSignal["removeEventListener"]>
      ) {
        if (args[0] === "abort" && (new Error().stack || "").includes("useWikiEventStream.ts")) {
          if (activeHookListeners.delete(args[1])) allRemoveCount += 1;
          if (timerListeners.delete(args[1])) removeCount += 1;
        }
        return origRemove.apply(this, args);
      });

    try {
      const { unmount } = renderHook(() => useWikiEventStream(42, vi.fn()));
      await vi.advanceTimersByTimeAsync(2_000);

      // Deterministic exact balance: the timer-win settle path detached.
      // (Mutation: timer path skipping removeEventListener leaves remove 0
      // and fails this pin — mirrors the draft-hook pin, PRR-003/F-004.)
      expect(addCount).toBe(1);
      expect(removeCount).toBe(1);
      expect(removeCount).toBe(addCount);
      unmount();
      expect(activeHookListeners.size).toBe(0);
      expect(allRemoveCount).toBe(allAddCount);
    } finally {
      spyAdd.mockRestore();
      spyRemove.mockRestore();
    }
  });

  it("balances per-read abort listeners as chunks settle and an unmounted read is cancelled", async () => {
    const listenersBySignal = new Map<AbortSignal, Set<Parameters<AbortSignal["addEventListener"]>[1]>>();
    let additions = 0;
    let removals = 0;
    const trackedListenerCount = () => [...listenersBySignal.values()].reduce((total, listeners) => total + listeners.size, 0);
    const origAdd = AbortSignal.prototype.addEventListener;
    const origRemove = AbortSignal.prototype.removeEventListener;
    const spyAdd = vi.spyOn(AbortSignal.prototype, "addEventListener").mockImplementation(
      function (this: AbortSignal, ...args: Parameters<AbortSignal["addEventListener"]>) {
        if (args[0] === "abort" && (new Error().stack || "").includes("useWikiEventStream.ts")) {
          let listeners = listenersBySignal.get(this);
          if (!listeners) {
            listeners = new Set();
            listenersBySignal.set(this, listeners);
          }
          if (!listeners.has(args[1])) {
            listeners.add(args[1]);
            additions += 1;
          }
        }
        return origAdd.apply(this, args);
      },
    );
    const spyRemove = vi.spyOn(AbortSignal.prototype, "removeEventListener").mockImplementation(
      function (this: AbortSignal, ...args: Parameters<AbortSignal["removeEventListener"]>) {
        if (args[0] === "abort" && (new Error().stack || "").includes("useWikiEventStream.ts")) {
          const listeners = listenersBySignal.get(this);
          if (listeners?.delete(args[1])) {
            removals += 1;
            if (listeners.size === 0) listenersBySignal.delete(this);
          }
        }
        return origRemove.apply(this, args);
      },
    );
    try {
      const body = new ReadableStream<Uint8Array>({
        start(controller) {
          controller.enqueue(new TextEncoder().encode("first"));
          controller.enqueue(new TextEncoder().encode("second"));
        },
      });
      fetchMock.mockResolvedValueOnce(new Response(body, { status: 200 }));

      const { unmount } = renderHook(() => useWikiEventStream(42, vi.fn()));
      await waitFor(() => {
        expect(additions).toBe(4); // Owner listener and three per-read listeners.
        expect(removals).toBe(2); // The first two reads settled and removed their listeners.
        expect(trackedListenerCount()).toBe(2); // Owner plus the pending third read.
      });

      unmount();
      await waitFor(() => {
        expect(removals).toBe(4);
        expect(trackedListenerCount()).toBe(0);
      });
      expect(spyAdd).toHaveBeenCalled();
      expect(spyRemove).toHaveBeenCalled();
    } finally {
      spyAdd.mockRestore();
      spyRemove.mockRestore();
    }
  });
  it("does not fetch again after dispose during the reconnect backoff", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    // Non-401 failure -> "error" -> reconnect after RECONNECT_BASE_MS (1s).
    fetchMock.mockResolvedValueOnce(errorResponse(503, "unavailable"));

    const { unmount } = renderHook(() => useWikiEventStream(42, vi.fn()));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

    // Dispose while parked in the 1s backoff sleep.
    unmount();

    await vi.advanceTimersByTimeAsync(5_000);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
