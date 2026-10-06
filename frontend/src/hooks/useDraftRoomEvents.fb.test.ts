/**
 * Feedback-round pins (PRR-009 + PRR-003) for useDraftRoomEvents — the hook
 * received both #774 changes (guarded refresh await; abort-aware backoff with
 * two-path listener detach) but had no dedicated coverage while its wiki twin
 * did:
 *   - a transport-class refreshAccessToken rejection on the token_expired
 *     path is contained (no unhandled rejection, stream stops);
 *   - disposing during a reconnect backoff issues no further fetch, and the
 *     abort-listener bookkeeping is balanced (every add has a matching
 *     remove once the settle path runs).
 */
import { renderHook, waitFor, cleanup } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import React from "react";

const refreshAccessTokenMock = vi.hoisted(() => vi.fn());
const getJwtAccessTokenMock = vi.hoisted(() => vi.fn(() => "test-jwt-token"));

vi.mock("@/lib/api", () => ({
  API_BASE_URL: "/api",
  getJwtAccessToken: getJwtAccessTokenMock,
  refreshAccessToken: refreshAccessTokenMock,
}));

function errorResponse(status: number, detail: string): Response {
  return {
    ok: false,
    status,
    headers: new Headers(),
    json: () => Promise.resolve({ detail }),
  } as unknown as Response;
}

describe("useDraftRoomEvents issue 774 hardening (feedback pins)", () => {
  let fetchMock: ReturnType<typeof vi.fn>;
  let useDraftRoomEvents: typeof import("./useDraftRoomEvents").useDraftRoomEvents;
  let queryClient: QueryClient;
  let addCount: number;
  let removeCount: number;

  beforeEach(async () => {
    vi.resetModules();
    refreshAccessTokenMock.mockReset();
    getJwtAccessTokenMock.mockReset();
    getJwtAccessTokenMock.mockReturnValue("test-jwt-token");
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    addCount = 0;
    removeCount = 0;
    const origAdd = AbortSignal.prototype.addEventListener;
    const origRemove = AbortSignal.prototype.removeEventListener;
    vi.spyOn(AbortSignal.prototype, "addEventListener").mockImplementation(
      function (this: AbortSignal, ...args: Parameters<AbortSignal["addEventListener"]>) {
        // Count only the hook's own listeners: vitest's internals also add
        // 'abort' listeners on AbortSignals during a test run.
        if (args[0] === "abort" && (new Error().stack || "").includes("useDraftRoomEvents.ts")) {
          addCount += 1;
        }
        return origAdd.apply(this, args);
      }
    );
    vi.spyOn(AbortSignal.prototype, "removeEventListener").mockImplementation(
      function (this: AbortSignal, ...args: Parameters<AbortSignal["removeEventListener"]>) {
        if (args[0] === "abort" && (new Error().stack || "").includes("useDraftRoomEvents.ts")) {
          removeCount += 1;
        }
        return origRemove.apply(this, args);
      }
    );
    queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    ({ useDraftRoomEvents } = await import("./useDraftRoomEvents"));
  });

  afterEach(() => {
    cleanup();
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  function createWrapper(queryClient_: QueryClient) {
    return function Wrapper({ children }: { children: React.ReactNode }) {
      return React.createElement(
        QueryClientProvider,
        { client: queryClient_ },
        children
      );
    };
  }

  function renderEvents(draftId: number | null | undefined = 7) {
    return renderHook(() => useDraftRoomEvents(draftId), {
      wrapper: createWrapper(queryClient),
    });
  }

  it("contains a transport-class refresh rejection on the token_expired path", async () => {
    const unhandled = vi.fn();
    process.on("unhandledRejection", unhandled);
    try {
      fetchMock.mockResolvedValueOnce(errorResponse(401, "token_expired"));
      refreshAccessTokenMock.mockRejectedValueOnce(
        new TypeError("Failed to fetch")
      );

      renderEvents();
      await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
      await vi.waitFor(() =>
        expect(refreshAccessTokenMock).toHaveBeenCalledTimes(1)
      );
      // Give any unhandled rejection a chance to surface.
      await new Promise((r) => setTimeout(r, 25));
      expect(unhandled).not.toHaveBeenCalled();
    } finally {
      process.off("unhandledRejection", unhandled);
    }
  });

  it("the first backoff cycle's timer-win settle detaches its listener (exact balance), and dispose ends the loop", async () => {
    // Call 1 fails (-> "error" -> 1s backoff); from call 2 on the fetch hangs
    // forever, which FREEZES the loop at a deterministic point: fetch(2) is
    // only invoked after cycle 1's backoff fully settled, so at that instant
    // addCount === removeCount === 1 exactly. Under the PRR-003 mutation
    // (timer path does not remove), removeCount stays 0 and this fails.
    vi.useFakeTimers();
    fetchMock.mockResolvedValueOnce(errorResponse(503, "unavailable"));
    fetchMock.mockImplementation(
      () => new Promise<Response>(() => undefined)
    );

    renderEvents();
    // Deterministic clock: at +2s the 1s backoff timer has fired (cycle 1
    // fully settled: add -> timer-win -> remove) and fetch(2) is invoked and
    // hangs — freezing the loop exactly there.
    await vi.advanceTimersByTimeAsync(2_000);

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(addCount).toBe(1);
    expect(removeCount).toBe(1);
    expect(removeCount).toBe(addCount);
  });
});
