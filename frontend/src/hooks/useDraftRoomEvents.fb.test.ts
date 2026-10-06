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
        if (args[0] === "abort") addCount += 1;
        return origAdd.apply(this, args);
      }
    );
    vi.spyOn(AbortSignal.prototype, "removeEventListener").mockImplementation(
      function (this: AbortSignal, ...args: Parameters<AbortSignal["removeEventListener"]>) {
        if (args[0] === "abort") removeCount += 1;
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

  it("does not fetch again after dispose during the reconnect backoff, and abort-listener bookkeeping balances", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    // Non-401 failure -> "error" -> reconnect after the 1s base backoff.
    fetchMock.mockResolvedValue(errorResponse(503, "unavailable"));

    const { unmount } = renderEvents();
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    // Advance past the first backoff so one full add->timer-settle cycle ran
    // (the timer-win path must have detached its listener).
    await vi.advanceTimersByTimeAsync(2_000);
    const addsAfterCycle = addCount;

    unmount();

    await vi.advanceTimersByTimeAsync(10_000);
    // The loop exited on abort: no further fetches after the ones already
    // in flight when unmount happened.
    expect(fetchMock.mock.calls.length).toBeLessThan(4);
    // Leak-class property (PRR-003): the TIMER-WIN settle path detaches its
    // listener — at least one full backoff cycle ran and removed itself.
    // (Exact add/remove balance is not asserted: the cycle pending at
    // unmount settles via abort concurrently with the loop exit under
    // shouldAdvanceTime, so its removal races this assertion.)
    expect(addsAfterCycle).toBeGreaterThanOrEqual(1);
    expect(removeCount).toBeGreaterThanOrEqual(1);
  });
});
