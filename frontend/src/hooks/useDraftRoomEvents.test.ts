import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as React from "react";
import { draftRoomKeys, getDraftEventsUrl, type DraftDetail, type DraftRoomCapabilities } from "@/lib/api/draftRoom";

const refreshAccessTokenMock = vi.hoisted(() => vi.fn());
const getJwtAccessTokenMock = vi.hoisted(() => vi.fn(() => "test-jwt-token"));

vi.mock("@/lib/api", () => ({
  getJwtAccessToken: getJwtAccessTokenMock,
  refreshAccessToken: refreshAccessTokenMock,
}));

const DRAFT_ID = 42;

// A controllable SSE body: `emit` pushes a chunk to the reader; reads pend
// until a chunk is available or the stream is cancelled. Mirrors
// useWikiEventStream.test.ts's helper.
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

function createWrapper(queryClient: QueryClient) {
  return function Wrapper({ children }: { children: React.ReactNode }) {
    return React.createElement(QueryClientProvider, { client: queryClient }, children);
  };
}

describe("useDraftRoomEvents", () => {
  let fetchMock: ReturnType<typeof vi.fn>;
  let queryClient: QueryClient;
  let invalidateSpy: ReturnType<typeof vi.spyOn>;
  let useDraftRoomEvents: typeof import("./useDraftRoomEvents").useDraftRoomEvents;

  beforeEach(async () => {
    vi.resetModules();
    refreshAccessTokenMock.mockReset();
    getJwtAccessTokenMock.mockReset();
    getJwtAccessTokenMock.mockReturnValue("test-jwt-token");
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");
    ({ useDraftRoomEvents } = await import("./useDraftRoomEvents"));
  });

  afterEach(() => {
    cleanup();
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  function renderEvents(draftId: number | null | undefined = DRAFT_ID, options?: { enabled?: boolean }) {
    return renderHook(() => useDraftRoomEvents(draftId, options), { wrapper: createWrapper(queryClient) });
  }

  // Fake-timer advances that let a pending setState commit must be wrapped in
  // act() or React logs an "update not wrapped in act" warning.
  async function advance(ms: number) {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(ms);
    });
  }

  it("opens the stream at getDraftEventsUrl(draftId) with a Bearer header", async () => {
    fetchMock.mockResolvedValue(controllableSse().response);
    renderEvents();

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe(getDraftEventsUrl(DRAFT_ID));
    expect(init.method).toBe("GET");
    expect(init.headers.Authorization).toBe("Bearer test-jwt-token");
  });

  it("does not open a stream when draftId is null", () => {
    fetchMock.mockResolvedValue(controllableSse().response);
    renderEvents(null);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("does not open a stream when options.enabled is false", () => {
    fetchMock.mockResolvedValue(controllableSse().response);
    renderEvents(DRAFT_ID, { enabled: false });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("parses a subscribed frame and exposes it as lastEvent", async () => {
    const { response, emit } = controllableSse();
    fetchMock.mockResolvedValue(response);
    const { result } = renderEvents();

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    emit('data: {"type":"subscribed","draft_id":42}\n\n');

    await waitFor(() => expect(result.current.lastEvent).toEqual({ type: "subscribed", draft_id: 42 }));
    // "subscribed" is not state-changing — it must not trigger an invalidation.
    expect(invalidateSpy).not.toHaveBeenCalled();
  });

  it("ignores SSE comment (keepalive) lines and does not surface them as events", async () => {
    const { response, emit } = controllableSse();
    fetchMock.mockResolvedValue(response);
    const { result } = renderEvents();

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    emit(": keepalive\n\n");
    // Give the read loop a tick; lastEvent must remain untouched by the comment.
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(result.current.lastEvent).toBeNull();

    emit('data: {"type":"job_started","job_id":1}\n\n');
    await waitFor(() => expect(result.current.lastEvent).toEqual({ type: "job_started", job_id: 1 }));
  });

  it("reassembles a frame split across two chunks", async () => {
    const { response, emit } = controllableSse();
    fetchMock.mockResolvedValue(response);
    const { result } = renderEvents();

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    emit('data: {"type":"job_');
    emit('completed","job_id":9}\n\n');

    await waitFor(() =>
      expect(result.current.lastEvent).toEqual({ type: "job_completed", job_id: 9 })
    );
  });

  it("ignores an unknown event type without throwing", async () => {
    const { response, emit } = controllableSse();
    fetchMock.mockResolvedValue(response);
    const { result } = renderEvents();

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    emit('data: {"type":"something_the_client_has_never_heard_of"}\n\n');
    emit('data: {"type":"job_started","job_id":2}\n\n');

    await waitFor(() => expect(result.current.lastEvent).toEqual({ type: "job_started", job_id: 2 }));
  });

  it("discards a buffer exceeding 64 KiB without a terminator, without throwing", async () => {
    const { response, emit } = controllableSse();
    fetchMock.mockResolvedValue(response);
    const { result } = renderEvents();

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

    // 70,000 bytes with no "\n\n" terminator — must be dropped, not accumulated.
    emit("x".repeat(70000));
    // The stream must still be usable afterwards.
    emit('data: {"type":"job_started","job_id":5}\n\n');

    await waitFor(() => expect(result.current.lastEvent).toEqual({ type: "job_started", job_id: 5 }));
  });

  it("invalidates detail, jobs, revisions, findings, and claims on job_completed", async () => {
    const { response, emit } = controllableSse();
    fetchMock.mockResolvedValue(response);
    renderEvents();

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    emit('data: {"type":"job_completed","job_id":1}\n\n');

    await waitFor(() =>
      expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: draftRoomKeys.detail(DRAFT_ID) })
    );
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: draftRoomKeys.jobs(DRAFT_ID) });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: draftRoomKeys.revisions(DRAFT_ID) });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: draftRoomKeys.findings(DRAFT_ID) });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: draftRoomKeys.claims(DRAFT_ID) });
  });

  it("invalidates only findings on finding_created", async () => {
    const { response, emit } = controllableSse();
    fetchMock.mockResolvedValue(response);
    renderEvents();

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    emit('data: {"type":"finding_created","finding_id":3}\n\n');

    await waitFor(() =>
      expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: draftRoomKeys.findings(DRAFT_ID) })
    );
    expect(invalidateSpy).not.toHaveBeenCalledWith({ queryKey: draftRoomKeys.claims(DRAFT_ID) });
    expect(invalidateSpy).not.toHaveBeenCalledWith({ queryKey: draftRoomKeys.revisions(DRAFT_ID) });
  });

  it("coalesces stage_progress bursts to at most one jobs invalidation per second", async () => {
    vi.useFakeTimers();
    const { response, emit } = controllableSse();
    fetchMock.mockResolvedValue(response);
    renderEvents();

    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

    emit('data: {"type":"stage_progress","progress_percent":10}\n\n');
    emit('data: {"type":"stage_progress","progress_percent":20}\n\n');
    emit('data: {"type":"stage_progress","progress_percent":30}\n\n');
    await vi.waitFor(() =>
      expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: draftRoomKeys.jobs(DRAFT_ID) })
    );
    const callsAfterBurst = invalidateSpy.mock.calls.filter(
      (call) => JSON.stringify(call[0]) === JSON.stringify({ queryKey: draftRoomKeys.jobs(DRAFT_ID) })
    ).length;
    expect(callsAfterBurst).toBe(1);

    // Past the 1s coalescing window, the next stage_progress invalidates again.
    await advance(1100);
    emit('data: {"type":"stage_progress","progress_percent":40}\n\n');
    await vi.waitFor(() => {
      const count = invalidateSpy.mock.calls.filter(
        (call) => JSON.stringify(call[0]) === JSON.stringify({ queryKey: draftRoomKeys.jobs(DRAFT_ID) })
      ).length;
      expect(count).toBe(2);
    });
  });

  it("refreshes the token on a 401 token_expired response and reconnects with the new token", async () => {
    vi.useFakeTimers();
    fetchMock
      .mockResolvedValueOnce(errorResponse(401, "token_expired"))
      .mockResolvedValue(controllableSse().response);
    refreshAccessTokenMock.mockResolvedValue("refreshed-jwt-token");
    getJwtAccessTokenMock.mockReturnValueOnce("test-jwt-token").mockReturnValue("refreshed-jwt-token");

    renderEvents();

    await vi.waitFor(() => expect(refreshAccessTokenMock).toHaveBeenCalledTimes(1));
    await advance(1100);
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));

    const [, init] = fetchMock.mock.calls[1];
    expect(init.headers.Authorization).toBe("Bearer refreshed-jwt-token");
  });

  it("stops permanently on a 401 token_invalid response without refreshing", async () => {
    vi.useFakeTimers();
    fetchMock.mockResolvedValue(errorResponse(401, "token_invalid"));

    renderEvents();

    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    await advance(5000);
    expect(refreshAccessTokenMock).not.toHaveBeenCalled();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("doubles the reconnect backoff on consecutive errors and resets after a clean close", async () => {
    vi.useFakeTimers();
    fetchMock
      .mockResolvedValueOnce(errorResponse(500, "server_error"))
      .mockResolvedValueOnce(doneResponse())
      .mockResolvedValue(controllableSse().response);

    renderEvents();

    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    // First reconnect fires at RECONNECT_BASE_MS (1000 ms) after the error.
    await advance(1100);
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));

    // The clean disconnect resets backoff to 1000 ms rather than doubling to 2000 ms.
    await advance(1100);
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("aborts the controller and stops fetching on unmount", async () => {
    fetchMock.mockResolvedValue(controllableSse().response);
    const { unmount } = renderEvents();

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    const signal = fetchMock.mock.calls[0][1].signal as AbortSignal;
    expect(signal.aborted).toBe(false);

    unmount();
    expect(signal.aborted).toBe(true);
  });

  it("engages polling fallback after 3 consecutive failed connection attempts", async () => {
    vi.useFakeTimers();
    queryClient.setQueryData(
      draftRoomKeys.detail(DRAFT_ID),
      { active_compile_job: { id: 1 } } as unknown as DraftDetail
    );
    fetchMock
      .mockResolvedValueOnce(errorResponse(500, "e1"))
      .mockResolvedValueOnce(errorResponse(500, "e2"))
      .mockResolvedValueOnce(errorResponse(500, "e3"))
      .mockImplementation(() => new Promise<Response>(() => {}));

    const { result, unmount } = renderEvents();

    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    await advance(1100);
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    await advance(2100);
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));

    await vi.waitFor(() => expect(result.current.pollingFallback).toBe(true));

    // The bounded poll interval (default 2s) refetches detail + jobs while a job is active.
    await advance(2100);
    await vi.waitFor(() =>
      expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: draftRoomKeys.detail(DRAFT_ID) })
    );
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: draftRoomKeys.jobs(DRAFT_ID) });

    unmount();
  });

  it("stops polling once the draft has no active job", async () => {
    vi.useFakeTimers();
    queryClient.setQueryData(
      draftRoomKeys.detail(DRAFT_ID),
      { active_compile_job: { id: 1 } } as unknown as DraftDetail
    );
    fetchMock
      .mockResolvedValueOnce(errorResponse(500, "e1"))
      .mockResolvedValueOnce(errorResponse(500, "e2"))
      .mockResolvedValueOnce(errorResponse(500, "e3"))
      .mockImplementation(() => new Promise<Response>(() => {}));

    const { result, unmount } = renderEvents();

    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    await advance(1100);
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    await advance(2100);
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
    await vi.waitFor(() => expect(result.current.pollingFallback).toBe(true));

    // First tick: job still active, polling continues.
    await advance(2100);
    await vi.waitFor(() =>
      expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: draftRoomKeys.jobs(DRAFT_ID) })
    );

    // The job finishes — the next tick must stop polling.
    queryClient.setQueryData(
      draftRoomKeys.detail(DRAFT_ID),
      { active_compile_job: null } as unknown as DraftDetail
    );
    await advance(2100);
    await vi.waitFor(() => expect(result.current.pollingFallback).toBe(false));

    unmount();
  });

  it("does not log event payloads (no console.log calls from the hook)", async () => {
    const logSpy = vi.spyOn(console, "log").mockImplementation(() => {});
    const { response, emit } = controllableSse();
    fetchMock.mockResolvedValue(response);
    const { result } = renderEvents();

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    emit('data: {"type":"job_started","job_id":1}\n\n');
    await waitFor(() => expect(result.current.lastEvent).not.toBeNull());

    expect(logSpy).not.toHaveBeenCalled();
    logSpy.mockRestore();
  });
});
