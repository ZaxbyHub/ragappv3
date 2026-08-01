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
type SseReadResult = { value?: Uint8Array; done: boolean };
type SseQueueEntry = { kind: "value"; item: SseReadResult } | { kind: "error"; error: unknown };

function controllableSse() {
  const encoder = new TextEncoder();
  let pending: ((r: SseReadResult) => void) | null = null;
  let pendingReject: ((e: unknown) => void) | null = null;
  const queue: SseQueueEntry[] = [];
  let closed = false;
  const reader = {
    read: vi.fn(
      () =>
        new Promise<SseReadResult>((resolve, reject) => {
          if (queue.length) {
            const next = queue.shift()!;
            if (next.kind === "error") reject(next.error);
            else resolve(next.item);
          } else if (closed) resolve({ done: true });
          else {
            pending = resolve;
            pendingReject = reject;
          }
        })
    ),
    cancel: vi.fn(),
  };
  const emit = (chunk: string) => {
    const item = { value: encoder.encode(chunk), done: false };
    if (pending) {
      const r = pending;
      pending = null;
      pendingReject = null;
      r(item);
    } else {
      queue.push({ kind: "value", item });
    }
  };
  const close = () => {
    closed = true;
    const doneItem = { done: true };
    if (pending) {
      const r = pending;
      pending = null;
      pendingReject = null;
      r(doneItem);
    } else {
      queue.push({ kind: "value", item: doneItem });
    }
  };
  // Reject the in-flight (or next) read — an ordinary mid-stream disconnect
  // (proxy idle-kill, sleep, wifi flap), as opposed to `close()`'s clean
  // `done: true` end.
  const fail = (error: unknown = new Error("stream error")) => {
    if (pendingReject) {
      const rej = pendingReject;
      pending = null;
      pendingReject = null;
      rej(error);
    } else {
      queue.push({ kind: "error", error });
    }
  };
  const response = {
    ok: true,
    status: 200,
    body: { getReader: () => reader },
  } as unknown as Response;
  return { response, emit, close, fail, reader };
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

  it("invalidates detail and findings on finding_created", async () => {
    const { response, emit } = controllableSse();
    fetchMock.mockResolvedValue(response);
    renderEvents();

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    emit('data: {"type":"finding_created","finding_id":3}\n\n');

    await waitFor(() =>
      expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: draftRoomKeys.findings(DRAFT_ID) })
    );
    // detail(id) must also be invalidated: DraftSummary.open_blocker_count is
    // computed server-side, so a new finding leaves the blocker badge stale
    // without it.
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: draftRoomKeys.detail(DRAFT_ID) });
    expect(invalidateSpy).not.toHaveBeenCalledWith({ queryKey: draftRoomKeys.claims(DRAFT_ID) });
    expect(invalidateSpy).not.toHaveBeenCalledWith({ queryKey: draftRoomKeys.revisions(DRAFT_ID) });
  });

  // Regression test for issue #437 finding 1: stage_started/stage_completed
  // must invalidate a query registered under draftRoomKeys.stages(...) (the
  // live stage rail) and draftRoomKeys.evidence(...) (the live Evidence tab).
  // Asserting only that invalidateQueries was called with *some* key is
  // exactly how the gap shipped unnoticed, so this drives real queries
  // through the real query-core prefix matcher instead.
  it("actually invalidates a registered stages(...) query on stage_completed", async () => {
    const jobId = 7;
    const stagesKey = draftRoomKeys.stages(DRAFT_ID, jobId, false);
    queryClient.setQueryData(stagesKey, { stages: [] });
    const stagesQuery = queryClient.getQueryCache().find({ queryKey: stagesKey });
    expect(stagesQuery).toBeDefined();
    const isInvalidatedBefore = stagesQuery!.state.isInvalidated;
    expect(isInvalidatedBefore).toBe(false);

    const { response, emit } = controllableSse();
    fetchMock.mockResolvedValue(response);
    renderEvents();

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    emit(`data: {"type":"stage_completed","job_id":${jobId},"stage":"research"}\n\n`);

    await waitFor(() => {
      const updated = queryClient.getQueryCache().find({ queryKey: stagesKey });
      expect(updated!.state.isInvalidated).toBe(true);
    });
  });

  it("actually invalidates a registered evidence(...) query on stage_started", async () => {
    const evidenceKey = draftRoomKeys.evidence(DRAFT_ID, { job_id: 7 });
    queryClient.setQueryData(evidenceKey, { items: [] });

    const { response, emit } = controllableSse();
    fetchMock.mockResolvedValue(response);
    renderEvents();

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    emit('data: {"type":"stage_started","job_id":7,"stage":"research"}\n\n');

    await waitFor(() => {
      const updated = queryClient.getQueryCache().find({ queryKey: evidenceKey });
      expect(updated!.state.isInvalidated).toBe(true);
    });
  });

  it("coalesces stage_progress bursts to at most one detail invalidation per second", async () => {
    vi.useFakeTimers();
    const { response, emit } = controllableSse();
    fetchMock.mockResolvedValue(response);
    renderEvents();

    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

    emit('data: {"type":"stage_progress","progress_percent":10}\n\n');
    emit('data: {"type":"stage_progress","progress_percent":20}\n\n');
    emit('data: {"type":"stage_progress","progress_percent":30}\n\n');
    await vi.waitFor(() =>
      expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: draftRoomKeys.detail(DRAFT_ID) })
    );
    const callsAfterBurst = invalidateSpy.mock.calls.filter(
      (call) => JSON.stringify(call[0]) === JSON.stringify({ queryKey: draftRoomKeys.detail(DRAFT_ID) })
    ).length;
    expect(callsAfterBurst).toBe(1);

    // Past the 1s coalescing window, the next stage_progress invalidates again.
    await advance(1100);
    emit('data: {"type":"stage_progress","progress_percent":40}\n\n');
    await vi.waitFor(() => {
      const count = invalidateSpy.mock.calls.filter(
        (call) => JSON.stringify(call[0]) === JSON.stringify({ queryKey: draftRoomKeys.detail(DRAFT_ID) })
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

  it("stops permanently on a 403 vault_access_revoked response without retrying", async () => {
    vi.useFakeTimers();
    fetchMock.mockResolvedValue(errorResponse(403, "no read access to this vault"));

    renderEvents();

    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    await advance(35000); // well past the 30s backoff cap
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  // Regression test for issue #437 finding 3: an ordinary drop after a long,
  // healthy stream (proxy idle-kill, sleep, wifi flap) must retry at the base
  // delay, not the backoff cap. The server generator is `while True` and
  // essentially never sends `done: true`, so every real disconnect lands in
  // the "error" branch — a naive "only reset on clean close" implementation
  // ratchets to 30s and stays there for the rest of the session.
  it("resets backoff to the base delay after a connection streams well past the healthy threshold then errors", async () => {
    vi.useFakeTimers();
    const healthy = controllableSse();
    fetchMock
      // call 1: fails immediately, escalating backoff 1000ms -> 2000ms.
      .mockResolvedValueOnce(errorResponse(500, "e1"))
      // call 2: connects, streams well past STREAM_HEALTHY_MS, then a plain
      // read-loop error (no `done: true`) — an ordinary mid-stream disconnect
      // (proxy idle-kill, sleep, wifi flap), not a clean server close.
      .mockResolvedValueOnce(healthy.response)
      // call 3+: further connections just hang; only their timing matters.
      .mockResolvedValue(controllableSse().response);

    renderEvents();

    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    await advance(1100);
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));

    // The health clock starts at the first byte actually received (a
    // keepalive comment counts), never at connect time — so this stream must
    // actually emit something before the healthy-duration clock can run.
    await act(async () => {
      healthy.emit(": keepalive\n\n");
    });
    await advance(25000); // well over STREAM_HEALTHY_MS (20s) since that byte
    await act(async () => {
      healthy.fail(new Error("network drop"));
    });

    // Without the fix, backoff would already be doubled to 2000ms by call 1's
    // failure and would stay there since this disconnect isn't a clean
    // `done: true` close. With the fix, streaming healthily resets backoff to
    // the base 1000ms even though the disconnect is classified "error".
    await advance(900);
    expect(fetchMock).toHaveBeenCalledTimes(2); // not yet — still short of 1000ms
    await advance(200);
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
  });

  // Regression test for the critic-overturned version of finding 3: a
  // connection that returns 200 OK but never delivers a single byte (e.g. a
  // proxy that accepts the connection and then hangs) must NOT be treated as
  // healthy just because wall-clock time passed with the socket open. It must
  // count toward both backoff escalation and the polling fallback exactly
  // like a fast connection error would.
  it("escalates backoff and engages polling fallback for connections that open but never deliver a byte", async () => {
    vi.useFakeTimers();
    queryClient.setQueryData(
      draftRoomKeys.detail(DRAFT_ID),
      { active_compile_job: { id: 1 } } as unknown as DraftDetail
    );
    const hung1 = controllableSse();
    const hung2 = controllableSse();
    const hung3 = controllableSse();
    fetchMock
      .mockResolvedValueOnce(hung1.response)
      .mockResolvedValueOnce(hung2.response)
      .mockResolvedValueOnce(hung3.response)
      .mockImplementation(() => new Promise<Response>(() => {}));

    const { result, unmount } = renderEvents();

    // Connection 1: 200 OK, never emits a byte, then well past
    // STREAM_HEALTHY_MS (20s) the connection drops. If this were wrongly
    // classified healthy, backoff would reset and polling would never engage.
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    await advance(25000);
    await act(async () => {
      hung1.fail(new Error("network drop"));
    });

    // Reconnect fires at the base delay (1000ms) since nothing has reset yet.
    await advance(1100);
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));

    // Connection 2: same hung pattern. Backoff must be escalating (doubled to
    // 2000ms), not still at base — proof that 25s of silence did not count as
    // healthy.
    await advance(25000);
    await act(async () => {
      hung2.fail(new Error("network drop"));
    });
    await advance(1900);
    expect(fetchMock).toHaveBeenCalledTimes(2); // not yet — needs ~2000ms, not 1000ms
    await advance(200);
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));

    // Connection 3: third consecutive failure with no byte ever received —
    // the polling fallback must engage, exactly as it would for three fast
    // 500 responses.
    await advance(25000);
    await act(async () => {
      hung3.fail(new Error("network drop"));
    });
    await vi.waitFor(() => expect(result.current.pollingFallback).toBe(true));

    unmount();
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
