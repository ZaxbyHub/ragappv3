import { renderHook, waitFor } from "@testing-library/react";
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
  const reader = {
    read: vi.fn(
      () =>
        new Promise<{ value?: Uint8Array; done: boolean }>((resolve) => {
          if (queue.length) resolve(queue.shift()!);
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
  const response = {
    ok: true,
    status: 200,
    body: { getReader: () => reader },
  } as unknown as Response;
  return { response, emit };
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

  it("stops (does not refresh) on a 401 token_invalid response", async () => {
    fetchMock.mockResolvedValue({
      ok: false,
      status: 401,
      json: async () => ({ detail: "token_invalid" }),
    } as unknown as Response);

    renderHook(() => useWikiEventStream(42, vi.fn()));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    // Give any erroneous reconnect a chance to fire; it must not.
    await Promise.resolve();
    expect(refreshAccessTokenMock).not.toHaveBeenCalled();
    expect(fetchMock).toHaveBeenCalledTimes(1);
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

    // First connection: immediately done (clean server close).
    const doneResponse = {
      ok: true,
      status: 200,
      body: {
        getReader: () => ({
          read: vi.fn().mockResolvedValue({ value: undefined, done: true }),
          cancel: vi.fn(),
        }),
      },
    } as unknown as Response;

    // Second connection: open (never done), so the hook stays connected.
    fetchMock
      .mockResolvedValueOnce(doneResponse)
      .mockResolvedValue(controllableSse().response);

    renderHook(() => useWikiEventStream(42, vi.fn()));

    // Wait for the first fetch to fire.
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

    // Advance past the base reconnect delay (1 s) but well under the max (30 s).
    // If backoff were not reset the second fetch would never fire within 2 s.
    await vi.advanceTimersByTimeAsync(1500);

    expect(fetchMock).toHaveBeenCalledTimes(2);

    vi.useRealTimers();
  });
});
