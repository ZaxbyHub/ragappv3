// Regression tests for issue #516 acceptance checks AC11 (UI-018 reconnect
// refresh) and AC19 (UI-020 parse-aware polling). Mirrors the harness of
// useDraftRoomEvents.test.ts (controllable SSE body, stubbed fetch, fresh
// QueryClient per test). These tests assert REQUIRED behavior and are
// expected to FAIL until the fix lands; each prints an AC<n> CHECK sentinel.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as React from "react";
import { draftRoomKeys, getDraftEventsUrl, type DraftDetail } from "@/lib/api/draftRoom";

const refreshAccessTokenMock = vi.hoisted(() => vi.fn());
const getJwtAccessTokenMock = vi.hoisted(() => vi.fn(() => "test-jwt-token"));

vi.mock("@/lib/api", () => ({
  getJwtAccessToken: getJwtAccessTokenMock,
  refreshAccessToken: refreshAccessTokenMock,
}));

const DRAFT_ID = 42;

// A controllable SSE body: `emit` pushes a chunk to the reader; reads pend
// until a chunk is available or the stream is cancelled. Copied verbatim
// from useDraftRoomEvents.test.ts so both files share the same semantics.
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
  // Reject the in-flight (or next) read — an ordinary mid-stream disconnect.
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

function createWrapper(queryClient: QueryClient) {
  return function Wrapper({ children }: { children: React.ReactNode }) {
    return React.createElement(QueryClientProvider, { client: queryClient }, children);
  };
}

describe("useDraftRoomEvents (issue #516)", () => {
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

  // --------------------------------------------------------------------------
  // AC11 (UI-018): after a stream gap during which a job completes, the
  // reconnect's `subscribed` frame ALONE must heal the canonical draft
  // detail/jobs queries. The server has no replay (no Last-Event-ID), so a
  // job_completed frame missed during the gap is gone forever — invalidating
  // on `subscribed` is the only way the UI ever learns the job finished.
  // Current defect: `case "subscribed": ... return;` performs no
  // invalidation, so the workspace shows a stale "running" job until the
  // user reloads.
  // --------------------------------------------------------------------------
  it("AC11: on reconnect after a gap, the subscribed frame alone invalidates detail and jobs", async () => {
    try {
      vi.useFakeTimers();

      // Register the canonical queries so the refresh is observable both on
      // the invalidateQueries spy and through the real query-core
      // (isInvalidated), exactly like the #437 regression tests below.
      queryClient.setQueryData(
        draftRoomKeys.detail(DRAFT_ID),
        { active_compile_job: { id: 1, status: "running" } } as unknown as DraftDetail
      );
      queryClient.setQueryData(draftRoomKeys.jobs(DRAFT_ID), {
        items: [{ id: 1, status: "running" }],
        total: 1,
        page: 1,
        per_page: 10,
      });

      const conn1 = controllableSse();
      const conn2 = controllableSse();
      fetchMock.mockResolvedValueOnce(conn1.response).mockResolvedValue(conn2.response);

      const { result, unmount } = renderEvents();

      // First connection: subscribe cleanly.
      await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
      await act(async () => {
        conn1.emit('data: {"type":"subscribed","draft_id":42}\n\n');
      });
      await vi.waitFor(() => expect(result.current.lastEvent?.type).toBe("subscribed"));
      expect(getJwtAccessTokenMock).toHaveBeenCalled();

      // Simulated stream gap: an ordinary mid-stream disconnect (proxy
      // idle-kill / wifi flap) — not a clean server close.
      await act(async () => {
        conn1.fail(new Error("network drop"));
      });

      // While disconnected, the job completes server-side: the cache (what
      // the server would now return) is terminal. The job_completed frame
      // for it will NEVER arrive on this client — it was published into the
      // gap. Only the reconnect can heal this.
      queryClient.setQueryData(
        draftRoomKeys.detail(DRAFT_ID),
        { active_compile_job: null, summary: { id: DRAFT_ID, active_job_id: null } } as unknown as DraftDetail
      );
      queryClient.setQueryData(draftRoomKeys.jobs(DRAFT_ID), {
        items: [{ id: 1, status: "completed" }],
        total: 1,
        page: 1,
        per_page: 10,
      });

      // Reconnect fires at the base 1s backoff.
      await advance(1100);
      await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));

      // The second connection's first — and ONLY — frame is `subscribed`.
      // No job event is ever delivered on it.
      invalidateSpy.mockClear();
      await act(async () => {
        conn2.emit('data: {"type":"subscribed","draft_id":42}\n\n');
      });

      await vi.waitFor(() =>
        expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: draftRoomKeys.detail(DRAFT_ID) })
      );
      expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: draftRoomKeys.jobs(DRAFT_ID) });

      // Through the real query-core: the registered canonical queries are
      // actually marked invalidated (not merely spied on).
      await vi.waitFor(() => {
        const detailQuery = queryClient.getQueryCache().find({ queryKey: draftRoomKeys.detail(DRAFT_ID) });
        const jobsQuery = queryClient.getQueryCache().find({ queryKey: draftRoomKeys.jobs(DRAFT_ID) });
        expect(detailQuery!.state.isInvalidated).toBe(true);
        expect(jobsQuery!.state.isInvalidated).toBe(true);
      });

      unmount();
      console.log("AC11 CHECK: PASS");
    } catch (err) {
      console.log("AC11 CHECK: FAIL");
      throw err;
    }
  });

  // --------------------------------------------------------------------------
  // AC19 (UI-020): fallback polling's stop condition must be parse-aware.
  // With NO active compile job but an active PARSE job (summary.active_job_id
  // set / input active_parse_job_id set), polling must CONTINUE invalidating
  // detail on subsequent ticks and only stop once no active work remains.
  // Current defect: the stop condition reads only
  // `detail.active_compile_job == null`, so polling stops on the first tick
  // and the parse job's progress is never refreshed.
  // --------------------------------------------------------------------------
  it("AC19: polling continues while a parse job is active even with no active compile job", async () => {
    try {
      vi.useFakeTimers();

      // Terminal for compile, but a parse job is actively running: the
      // summary reports the active job and the input has an active parse job.
      queryClient.setQueryData(
        draftRoomKeys.detail(DRAFT_ID),
        {
          summary: { id: DRAFT_ID, active_job_id: 55 },
          inputs: [{ id: 1, active_parse_job_id: 77, parse_status: "parsing" }],
          active_compile_job: null,
        } as unknown as DraftDetail
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

      // First poll tick: no active COMPILE job, but a parse job is running.
      // Polling must keep refreshing detail + jobs until parse finishes too.
      await advance(2100);
      await vi.waitFor(() =>
        expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: draftRoomKeys.detail(DRAFT_ID) })
      );
      expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: draftRoomKeys.jobs(DRAFT_ID) });
      expect(result.current.pollingFallback).toBe(true);

      // Only once no active work remains anywhere (compile AND parse) does
      // polling stop.
      invalidateSpy.mockClear();
      queryClient.setQueryData(
        draftRoomKeys.detail(DRAFT_ID),
        {
          summary: { id: DRAFT_ID, active_job_id: null },
          inputs: [{ id: 1, active_parse_job_id: null, parse_status: "ready" }],
          active_compile_job: null,
        } as unknown as DraftDetail
      );
      await advance(2100);
      await vi.waitFor(() => expect(result.current.pollingFallback).toBe(false));

      unmount();
      console.log("AC19 CHECK: PASS");
    } catch (err) {
      console.log("AC19 CHECK: FAIL");
      throw err;
    }
  });
});
