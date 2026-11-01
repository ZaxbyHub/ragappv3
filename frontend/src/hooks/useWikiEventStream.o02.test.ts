/**
 * Issue #792 (Workstream O, PR 2) acceptance check: the wiki event stream
 * fires the terminal callback on job_cancelled.
 *
 * The wiki/KMS processors publish a `job_cancelled` SSE event (e.g. when
 * compile flags went off between enqueue and claim), but the stream hook
 * only dispatches the callback for `job_completed` / `job_failed`, so a
 * cancelled job leaves the UI waiting for a terminal refresh that never
 * comes.
 *
 * RED at base (job_cancelled is dropped, so after a job_cancelled followed
 * by a job_completed the callback has fired exactly once); GREEN after the
 * fix (job_cancelled is dispatched as a terminal event, so both events fire
 * the callback).
 *
 * Mocking mirrors useWikiEventStream.test.ts: a stubbed global fetch fed by
 * a controllable SSE body, with @/lib/api mocked for the token helpers.
 */
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
    cancel: vi.fn(() => Promise.resolve()),
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

describe("useWikiEventStream (issue #792 acceptance)", () => {
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

  it("fires the callback on job_cancelled", async () => {
    const { response, emit } = controllableSse();
    fetchMock.mockResolvedValue(response);
    const onTerminal = vi.fn();
    renderHook(() => useWikiEventStream(42, onTerminal));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

    emit('data: {"type":"job_cancelled"}\n\n');
    // Trailing job_completed is the deterministic flush marker: once it has
    // fired the callback once, any pending dispatch of the earlier
    // job_cancelled has necessarily been processed (or dropped) already.
    emit('data: {"type":"job_completed"}\n\n');

    await waitFor(() => expect(onTerminal.mock.calls.length).toBeGreaterThanOrEqual(1));
    await waitFor(() => expect(onTerminal.mock.calls.length).toBe(2));
  });
});
