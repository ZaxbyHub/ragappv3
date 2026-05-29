import { renderHook, waitFor, cleanup } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const refreshAccessTokenMock = vi.hoisted(() => vi.fn());
const getJwtAccessTokenMock = vi.hoisted(() => vi.fn(() => "test-jwt-token"));

vi.mock("@/lib/api", () => ({
  API_BASE_URL: "/knowledgevault/api",
  getJwtAccessToken: getJwtAccessTokenMock,
  refreshAccessToken: refreshAccessTokenMock,
}));

// A controllable SSE body: `emit` pushes a chunk to the reader; reads pend
// until a chunk is available or the stream is cancelled.
function controllableSse() {
  const encoder = new TextEncoder();
  let pending: ((r: { value?: Uint8Array; done: boolean }) => void) | null = null;
  const queue: Array<{ value?: Uint8Array; done: boolean }> = [];
  let closed = false;
  const reader = {
    read: vi.fn(
      () =>
        new Promise<{ value?: Uint8Array; done: boolean }>((resolve) => {
          if (queue.length) resolve(queue.shift()!);
          else if (closed) resolve({ done: true });
          else pending = resolve;
        })
    ),
    cancel: vi.fn(),
  };
  const emit = (chunk: string) => {
    const item = { value: encoder.encode(chunk), done: false };
    if (pending) {
      const r = pending;
      pending = null;
      r(item);
    } else {
      queue.push(item);
    }
  };
  const close = () => {
    closed = true;
    const doneItem = { done: true };
    if (pending) {
      const r = pending;
      pending = null;
      r(doneItem);
    } else {
      queue.push(doneItem);
    }
  };
  const response = {
    ok: true,
    status: 200,
    body: { getReader: () => reader },
  } as unknown as Response;
  return { response, emit, close, reader };
}

function errorResponse(status: number, detail: string): Response {
  return {
    ok: false,
    status,
    json: async () => ({ detail }),
  } as unknown as Response;
}

function bodylessResponse(status = 200): Response {
  return {
    ok: true,
    status,
    body: null,
  } as unknown as Response;
}

function doneResponse(): Response {
  return {
    ok: true,
    status: 200,
    body: {
      getReader: () => ({
        read: vi.fn().mockResolvedValue({ done: true, value: undefined }),
        cancel: vi.fn(),
      }),
    },
  } as unknown as Response;
}

describe("useWikiEventStream", () => {
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

  it("opens the stream with the API base URL and a Bearer header", async () => {
    fetchMock.mockResolvedValue(controllableSse().response);
    renderHook(() => useWikiEventStream(42, vi.fn()));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/knowledgevault/api/wiki/events?vault_id=42");
    expect(init.method).toBe("GET");
    expect(init.headers.Authorization).toBe("Bearer test-jwt-token");
  });

  it("does not open a stream when vaultId is falsy", () => {
    fetchMock.mockResolvedValue(controllableSse().response);
    renderHook(() => useWikiEventStream(null, vi.fn()));
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("fires the callback on job_completed and job_failed events", async () => {
    const { response, emit } = controllableSse();
    fetchMock.mockResolvedValue(response);
    const onTerminal = vi.fn();
    renderHook(() => useWikiEventStream(42, onTerminal));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

    emit('data: {"type":"job_completed"}\n\n');
    await waitFor(() => expect(onTerminal).toHaveBeenCalledTimes(1));

    emit('data: {"type":"job_failed"}\n\n');
    await waitFor(() => expect(onTerminal).toHaveBeenCalledTimes(2));
  });

  it("ignores non-terminal events and keepalive comments", async () => {
    const { response, emit } = controllableSse();
    fetchMock.mockResolvedValue(response);
    const onTerminal = vi.fn();
    renderHook(() => useWikiEventStream(42, onTerminal));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

    emit(': keepalive\n\n');
    emit('data: {"type":"subscribed"}\n\n');
    // Then a real event, to have a deterministic point to await.
    emit('data: {"type":"job_completed"}\n\n');

    await waitFor(() => expect(onTerminal).toHaveBeenCalledTimes(1));
  });

  it("refreshes the token on a 401 token_expired response", async () => {
    fetchMock
      .mockResolvedValueOnce({
        ok: false,
        status: 401,
        json: async () => ({ detail: "token_expired" }),
      } as unknown as Response)
      .mockResolvedValue(controllableSse().response);
    refreshAccessTokenMock.mockResolvedValue("new-token");

    renderHook(() => useWikiEventStream(42, vi.fn()));

    await waitFor(() => expect(refreshAccessTokenMock).toHaveBeenCalledTimes(1));
  });

  it("reconnects with the refreshed token after a 401 token_expired response", async () => {
    vi.useFakeTimers();
    fetchMock
      .mockResolvedValueOnce(errorResponse(401, "token_expired"))
      .mockResolvedValue(controllableSse().response);
    refreshAccessTokenMock.mockResolvedValue("refreshed-jwt-token");
    getJwtAccessTokenMock
      .mockReturnValueOnce("test-jwt-token")
      .mockReturnValue("refreshed-jwt-token");

    renderHook(() => useWikiEventStream(42, vi.fn()));

    // Wait for the initial fetch and the refresh.
    await vi.waitFor(() => expect(refreshAccessTokenMock).toHaveBeenCalledTimes(1));

    // Advance past RECONNECT_BASE_MS (1000 ms) so the backoff timer fires.
    await vi.advanceTimersByTimeAsync(1100);

    // The hook returns "error" after successful refresh, which triggers reconnect.
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));

    // Verify the second fetch uses the refreshed token.
    const [, init] = fetchMock.mock.calls[1];
    expect(init.headers.Authorization).toBe("Bearer refreshed-jwt-token");
  });

  it("stops retrying on a 401 token_expired response when refresh fails", async () => {
    vi.useFakeTimers();
    fetchMock.mockResolvedValueOnce(errorResponse(401, "token_expired"));
    refreshAccessTokenMock.mockResolvedValue(null);

    renderHook(() => useWikiEventStream(42, vi.fn()));

    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    await vi.advanceTimersByTimeAsync(2000);
    expect(refreshAccessTokenMock).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("stops (does not refresh) on a 401 token_invalid response", async () => {
    vi.useFakeTimers();
    fetchMock.mockResolvedValue(errorResponse(401, "token_invalid"));

    renderHook(() => useWikiEventStream(42, vi.fn()));

    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    await vi.advanceTimersByTimeAsync(2000);
    expect(refreshAccessTokenMock).not.toHaveBeenCalled();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("stops on a 401 user_inactive response", async () => {
    vi.useFakeTimers();
    fetchMock.mockResolvedValue(errorResponse(401, "user_inactive"));

    renderHook(() => useWikiEventStream(42, vi.fn()));

    await Promise.resolve();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(2000);
    expect(refreshAccessTokenMock).not.toHaveBeenCalled();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("retries after a response without a body", async () => {
    vi.useFakeTimers();
    fetchMock.mockResolvedValueOnce(bodylessResponse()).mockResolvedValueOnce(doneResponse());

    const { unmount } = renderHook(() => useWikiEventStream(42, vi.fn()));

    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    await vi.advanceTimersByTimeAsync(1000);
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));

    unmount();
  });

  it("retries after a non-401 error response", async () => {
    vi.useFakeTimers();
    fetchMock.mockResolvedValueOnce(errorResponse(500, "server_error")).mockResolvedValueOnce(doneResponse());

    const { unmount } = renderHook(() => useWikiEventStream(42, vi.fn()));

    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    await vi.advanceTimersByTimeAsync(1000);
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));

    unmount();
  });

  it("doubles the reconnect backoff on consecutive errors", async () => {
    vi.useFakeTimers();
    fetchMock
      .mockResolvedValueOnce(errorResponse(500, "server_error"))
      .mockResolvedValueOnce(errorResponse(500, "server_error"))
      .mockResolvedValueOnce(controllableSse().response);

    renderHook(() => useWikiEventStream(42, vi.fn()));

    // Initial fetch fires immediately.
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

    // First error retry fires at RECONNECT_BASE_MS (1000 ms).
    await vi.advanceTimersByTimeAsync(1100);
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));

    // Second error retry fires at 2000 ms (doubled from 1000).
    // Advance only 1500 ms — should NOT trigger yet (needs 2000 ms from last retry).
    await vi.advanceTimersByTimeAsync(1500);
    expect(fetchMock).toHaveBeenCalledTimes(2); // still 2

    // Advance past the 2000 ms mark — should trigger now.
    await vi.advanceTimersByTimeAsync(600);
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
  });

  it("reassembles partial SSE payloads across read calls", async () => {
    const { response, emit, close } = controllableSse();
    fetchMock.mockResolvedValue(response);
    const onTerminal = vi.fn();
    renderHook(() => useWikiEventStream(42, onTerminal));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

    emit('data: {"type":"job_');
    emit('completed"}\n\n');
    close();

    await waitFor(() => expect(onTerminal).toHaveBeenCalledTimes(1));
  });

  it("reassembles SSE payloads split across three chunks and at boundary", async () => {
    const { response, emit, close } = controllableSse();
    fetchMock.mockResolvedValue(response);
    const onTerminal = vi.fn();
    renderHook(() => useWikiEventStream(42, onTerminal));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

    // Three-chunk split
    emit('data: {"type":"job_');
    emit('completed"}\n');
    emit('\n');

    close();

    await waitFor(() => expect(onTerminal).toHaveBeenCalledTimes(1));
  });

  it("aborts the in-flight request on unmount", async () => {
    fetchMock.mockResolvedValue(controllableSse().response);
    const { unmount } = renderHook(() => useWikiEventStream(42, vi.fn()));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    const signal = fetchMock.mock.calls[0][1].signal as AbortSignal;
    expect(signal.aborted).toBe(false);

    unmount();
    expect(signal.aborted).toBe(true);
  });

  it("omits the Authorization header when no token is present", async () => {
    getJwtAccessTokenMock.mockReturnValue(null);
    fetchMock.mockResolvedValue(controllableSse().response);
    renderHook(() => useWikiEventStream(42, vi.fn()));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    const init = fetchMock.mock.calls[0][1];
    expect(init.headers.Authorization).toBeUndefined();
  });

  it("resets backoff to base after a clean stream end (F-001)", async () => {
    // Regression for F-001: after a stream disconnects cleanly (server closes
    // with done=true), the next reconnect must use RECONNECT_BASE_MS (1 s), not
    // whatever backoff was accumulated from a prior error-reconnect cycle.
    vi.useFakeTimers();

    // Sequence: error (doubles backoff 1000→2000) → clean disconnect (resets to 1000)
    // → reconnect fires at the reset 1000ms interval, not at the elevated 2000ms.
    fetchMock
      .mockResolvedValueOnce(errorResponse(500, "server_error"))
      .mockResolvedValueOnce(doneResponse())
      .mockResolvedValue(controllableSse().response);

    renderHook(() => useWikiEventStream(42, vi.fn()));

    // Wait for the initial fetch to fire.
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

    // First reconnect fires at RECONNECT_BASE_MS (1000 ms) after the error.
    await vi.advanceTimersByTimeAsync(1100);
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));

    // Clean disconnect (doneResponse) resets backoff to RECONNECT_BASE_MS (1000 ms).
    // Advance timers to let the next reconnect fire at the reset 1000 ms interval.
    await vi.advanceTimersByTimeAsync(1100);

    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("reconnects after a network error (fetch throws)", async () => {
    vi.useFakeTimers();
    fetchMock
      .mockRejectedValueOnce(new TypeError("Network error"))
      .mockResolvedValueOnce(controllableSse().response);

    renderHook(() => useWikiEventStream(42, vi.fn()));

    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    await vi.advanceTimersByTimeAsync(1100);
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
  });

  it("retries (does not refresh) on a 401 when no token is present", async () => {
    vi.useFakeTimers();
    getJwtAccessTokenMock.mockReturnValue(null);
    fetchMock
      .mockResolvedValueOnce(errorResponse(401, "token_expired"))
      .mockResolvedValueOnce(controllableSse().response);

    renderHook(() => useWikiEventStream(42, vi.fn()));

    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    expect(refreshAccessTokenMock).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1100);
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
  });

  it("stops if aborted during token refresh", async () => {
    vi.useFakeTimers();
    let resolveRefresh: ((token: string | null) => void) | undefined;
    refreshAccessTokenMock.mockImplementation(() => {
      return new Promise((resolve) => {
        resolveRefresh = resolve;
      });
    });
    fetchMock.mockResolvedValueOnce(errorResponse(401, "token_expired"));

    const { unmount } = renderHook(() => useWikiEventStream(42, vi.fn()));

    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

    // Abort while refresh is pending.
    unmount();

    // Now resolve the refresh — the hook should have already stopped.
    if (resolveRefresh) resolveRefresh("new-token");
    await vi.advanceTimersByTimeAsync(2000);

    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
