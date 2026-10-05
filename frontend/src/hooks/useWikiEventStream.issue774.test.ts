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
    cancel: vi.fn(),
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
    // 401 token_expired, then the refresh rejects with a network error.
    fetchMock.mockResolvedValueOnce(errorResponse(401, "token_expired"));
    refreshAccessTokenMock.mockRejectedValueOnce(new TypeError("Failed to fetch"));

    renderHook(() => useWikiEventStream(42, vi.fn()));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    await vi.waitFor(() => expect(refreshAccessTokenMock).toHaveBeenCalledTimes(1));
    // Give any unhandled rejection a chance to surface.
    await new Promise((r) => setTimeout(r, 25));

    expect(unhandled).not.toHaveBeenCalled();
    process.off("unhandledRejection", unhandled);
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
