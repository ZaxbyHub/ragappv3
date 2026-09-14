// Issue #555 repo tests: the fetch-based SSE client tracks event ids and
// auto-resumes a broken durable stream with Last-Event-ID. Mirrors the frozen
// acceptance contract (repro/c5_contract.test.ts) as a permanent regression
// pin, plus parseSSEStream id-parsing units.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const coreMocks = vi.hoisted(() => ({
  ensureCsrfToken: vi.fn(async () => "test-token"),
  refreshAccessToken: vi.fn(async () => "refreshed-token"),
  isTokenNearExpiry: vi.fn(() => false),
}));

vi.mock("../core", () => ({
  apiClient: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
  API_BASE_URL: "/api",
  get _jwtAccessToken(): string | null {
    return null;
  },
  getCsrfCookie: () => null,
  getCsrfToken: () => null,
  ensureCsrfToken: (...args: unknown[]) =>
    coreMocks.ensureCsrfToken(...(args as [])) as Promise<string>,
  refreshAccessToken: (...args: unknown[]) =>
    coreMocks.refreshAccessToken(...(args as [])) as Promise<string | null>,
  isTokenNearExpiry: (...args: unknown[]) =>
    coreMocks.isTokenNearExpiry(...(args as [])) as boolean,
}));

import { chatStream, parseSSEStream, SSEResumeState } from "../sessions";

const encoder = new TextEncoder();

function sseResponse(frames: string[]): Response {
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const frame of frames) {
        controller.enqueue(encoder.encode(frame));
      }
      controller.close();
    },
  });
  return {
    ok: true,
    status: 200,
    body: { getReader: () => stream.getReader() },
  } as unknown as Response;
}

function headerValue(headers: unknown, name: string): string | null {
  if (headers == null) return null;
  if (typeof (headers as Headers).get === "function") {
    return (headers as Headers).get(name);
  }
  const record = headers as Record<string, string>;
  for (const key of Object.keys(record)) {
    if (key.toLowerCase() === name.toLowerCase()) return record[key];
  }
  return null;
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function waitUntil(predicate: () => boolean, deadlineMs: number): Promise<boolean> {
  const deadline = Date.now() + deadlineMs;
  while (Date.now() < deadline) {
    if (predicate()) return true;
    await sleep(20);
  }
  return predicate();
}

describe("parseSSEStream event-id tracking (issue #555)", () => {
  it("records each frame's id and lets an empty id reset it", async () => {
    const state: SSEResumeState = { lastEventId: null };
    const reader = sseResponse([
      "id: 4\ndata: {\"type\":\"content\",\"content\":\"a\"}\n\n",
      "id:5\ndata: {\"type\":\"content\",\"content\":\"b\"}\n\n",
      ": heartbeat\n\n",
      "id: \ndata: {\"type\":\"content\",\"content\":\"c\"}\n\n",
    ]).body!.getReader();
    await parseSSEStream(reader, {}, state);
    expect(state.lastEventId).toBeNull();
  });

  it("leaves lastEventId null when the stream carries no ids (pre-555 backends)", async () => {
    const state: SSEResumeState = { lastEventId: null };
    const reader = sseResponse([
      "data: {\"type\":\"mode\",\"mode\":\"instant\"}\n\n",
      "data: [DONE]\n\n",
    ]).body!.getReader();
    await parseSSEStream(reader, { onComplete: () => undefined }, state);
    expect(state.lastEventId).toBeNull();
  });
});

describe("chatStream reconnects with Last-Event-ID after mid-answer EOF (issue #555)", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("resumes a broken durable stream by re-fetching with Last-Event-ID and completes on the resumed stream", async () => {
    const contents: string[] = [];
    let completeCalls = 0;

    fetchMock.mockImplementation(async () => {
      if (fetchMock.mock.calls.length === 1) {
        // First connection: one id'd content frame, then transport EOF — the
        // done marker never arrives.
        return sseResponse([
          "id: 7\ndata: {\"type\":\"content\",\"content\":\"partial \"}\n\n",
        ]);
      }
      // Resumed connection: the missed remainder, ending with done.
      return sseResponse([
        "id: 8\ndata: {\"type\":\"content\",\"content\":\"resumed\"}\n\n",
        "id: 9\ndata: {\"type\":\"done\",\"sources\":[],\"memories_used\":[]}\n\n",
      ]);
    });

    const dispose = chatStream(
      [{ role: "user", content: "stream question" }] as never,
      {
        onMessage: (chunk: string) => contents.push(chunk),
        onComplete: () => {
          completeCalls += 1;
        },
      } as never,
      1,
      undefined,
      undefined,
      undefined,
      undefined,
      undefined,
      undefined,
      { sessionId: 42, turnId: "turn-555" },
    );

    try {
      const reconnectIssued = await waitUntil(() => fetchMock.mock.calls.length >= 2, 3200);
      expect(reconnectIssued, "chatStream did not issue a reconnect fetch").toBe(true);

      const completed = await waitUntil(() => completeCalls >= 1, 1500);
      expect(completed, "stream never completed after resume").toBe(true);

      const [secondUrl, secondInit] = fetchMock.mock.calls[1] as [string, RequestInit];
      expect(secondUrl).toBe("/api/chat/stream");
      expect(headerValue(secondInit?.headers, "Last-Event-ID")).toBe("7");
      expect(fetchMock.mock.calls.length).toBe(2);
      expect(contents).toEqual(["partial ", "resumed"]);
      expect(completeCalls).toBe(1);
    } finally {
      dispose();
    }
  }, 8000);

  it("does not resume non-durable streams (interruption surfaces immediately)", async () => {
    const errors: Error[] = [];
    fetchMock.mockImplementation(async () =>
      sseResponse(["id: 2\ndata: {\"type\":\"content\",\"content\":\"x\"}\n\n"])
    );
    const dispose = chatStream(
      [{ role: "user", content: "q" }] as never,
      { onMessage: () => undefined, onError: (e: Error) => errors.push(e) } as never,
    );
    await waitUntil(() => errors.length > 0, 1500);
    expect(fetchMock.mock.calls.length).toBe(1);
    expect(errors[0]?.name).toBe("ChatInterruptedError");
    dispose();
  }, 5000);
});
