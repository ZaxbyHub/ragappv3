import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  resetCsrfToken,
  setJwtAccessToken,
  type ChatMessage,
  type ChatStreamCallbacks,
} from "./core";
import {
  publishAuthPrincipal,
  reserveReplacementAuthOwner,
} from "./auth-lifecycle";
import { chatStream, parseSSEStream } from "./sessions";

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (reason?: unknown) => void;
}

const pendingDeferreds = new Set<{ resolve: (value: unknown) => void }>();

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((promiseResolve, promiseReject) => {
    resolve = promiseResolve;
    reject = promiseReject;
  });
  promise.catch(() => undefined);
  pendingDeferreds.add({ resolve: resolve as (value: unknown) => void });
  return { promise, resolve, reject };
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function sseResponse(...frames: string[]): Response {
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      const encoder = new TextEncoder();
      for (const frame of frames) controller.enqueue(encoder.encode(frame));
      controller.close();
    },
  });
  return new Response(stream, { status: 200 });
}

function heldReaderResponse(readResult: Deferred<ReadableStreamReadResult<Uint8Array>>, onCancel: () => void): Response {
  const body = new ReadableStream<Uint8Array>({
    pull(controller) {
      return readResult.promise.then((result) => {
        if (result.done) controller.close();
        else controller.enqueue(result.value);
      });
    },
    cancel() {
      onCancel();
    },
  });
  return new Response(body, { status: 200 });
}

async function settleMicrotasks(): Promise<void> {
  // Flush the full credential/Response/SSE promise chain without advancing
  // retry or inactivity clocks. Assertions still prove actual transport starts.
  for (let turn = 0; turn < 24; turn += 1) await Promise.resolve();
}

const messages: ChatMessage[] = [{ role: "user", content: "hello" }];

describe("chat stream ownership and principal scope (issue #775)", () => {
  const fetchMock = vi.fn<typeof fetch>();
  const disposers: Array<() => void> = [];

  beforeEach(() => {
    vi.useFakeTimers();
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
    resetCsrfToken();
    setJwtAccessToken(null);
    document.cookie = "X-CSRF-Token=; Max-Age=0; Path=/";
    publishAuthPrincipal(null);
    publishAuthPrincipal({ id: 8101, role: "user" });
  });

  afterEach(async () => {
    for (const dispose of disposers.splice(0)) dispose();
    reserveReplacementAuthOwner();
    for (const pending of pendingDeferreds) pending.resolve(undefined);
    pendingDeferreds.clear();
    await settleMicrotasks();
    vi.clearAllTimers();
    vi.useRealTimers();
    resetCsrfToken();
    setJwtAccessToken(null);
    document.cookie = "X-CSRF-Token=; Max-Age=0; Path=/";
    reserveReplacementAuthOwner();
    publishAuthPrincipal(null);
    fetchMock.mockReset();
    vi.unstubAllGlobals();
  });

  it("captures owner and principal immediately, so a held fetch cannot publish or reconnect after replacement", async () => {
    const heldFetch = deferred<Response>();
    const callbacks: ChatStreamCallbacks = {
      onMessage: vi.fn(),
      onSources: vi.fn(),
      onMemories: vi.fn(),
      onWiki: vi.fn(),
      onKMS: vi.fn(),
      onCitationValidation: vi.fn(),
      onFinalContent: vi.fn(),
      onCitationConfidence: vi.fn(),
      onUnverifiableClaims: vi.fn(),
      onCurrencyWarnings: vi.fn(),
      onCitationEnforcement: vi.fn(),
      onMode: vi.fn(),
      onStage: vi.fn(),
      onEvidenceCandidates: vi.fn(),
      onReasoning: vi.fn(),
      onReasoningMetrics: vi.fn(),
      onFinishReason: vi.fn(),
      onError: vi.fn(),
      onComplete: vi.fn(),
    };
    fetchMock.mockImplementation((input) => {
      const url = String(input);
      if (url.endsWith("/csrf-token")) return Promise.resolve(jsonResponse({ csrf_token: "csrf-chat" }));
      return heldFetch.promise;
    });

    const dispose = chatStream(messages, callbacks, 4, "instant", undefined, undefined, undefined, undefined, undefined, {
      sessionId: 44,
      turnId: "held-fetch",
    });
    disposers.push(dispose);
    await settleMicrotasks();
    publishAuthPrincipal({ id: 8102, role: "admin" });
    heldFetch.resolve(sseResponse(
      'data: {"type":"content","content":"stale","sources":[{"title":"stale"}],"repaired_content":"stale-final","citation_confidence":{"stale":1}}\n\n',
      'data: [DONE]\n\n',
    ));
    await settleMicrotasks();

    expect(fetchMock).toHaveBeenCalledTimes(2);
    for (const callback of Object.values(callbacks)) expect(callback).not.toHaveBeenCalled();
  });

  it("does not dispatch a chat fetch when a held CSRF attempt becomes stale", async () => {
    const csrf = deferred<Response>();
    const callbacks = { onError: vi.fn(), onComplete: vi.fn(), onMessage: vi.fn() };
    fetchMock.mockImplementation((input) => {
      const url = String(input);
      if (url.endsWith("/csrf-token")) return csrf.promise;
      throw new Error("stale CSRF must prevent chat dispatch");
    });

    const dispose = chatStream(messages, callbacks);
    disposers.push(dispose);
    publishAuthPrincipal({ id: 8103, role: "reviewer" });
    csrf.resolve(jsonResponse({ csrf_token: "stale-csrf" }));
    await settleMicrotasks();

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(callbacks.onError).not.toHaveBeenCalled();
    expect(callbacks.onComplete).not.toHaveBeenCalled();
    expect(callbacks.onMessage).not.toHaveBeenCalled();
  });

  it("does not retry a stale 401 JSON response for a replacement principal", async () => {
    setJwtAccessToken("not-a-real-jwt");
    const errorBody = deferred<unknown>();
    fetchMock.mockImplementation((input) => {
      const url = String(input);
      if (url.endsWith("/csrf-token")) return Promise.resolve(jsonResponse({ csrf_token: "csrf-401" }));
      return Promise.resolve({
        ok: false,
        status: 401,
        json: () => errorBody.promise,
      } as Response);
    });
    const callbacks = { onError: vi.fn(), onComplete: vi.fn() };
    const dispose = chatStream(messages, callbacks);
    disposers.push(dispose);
    await settleMicrotasks();
    publishAuthPrincipal({ id: 8104, role: "user" });
    errorBody.resolve({ detail: "token_expired" });
    await settleMicrotasks();

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(callbacks.onError).not.toHaveBeenCalled();
    expect(callbacks.onComplete).not.toHaveBeenCalled();
  });

  it("cancels the owned reader and removes inactivity cleanup when disposed while a chunk is held", async () => {
    const readResult = deferred<ReadableStreamReadResult<Uint8Array>>();
    let readerCancelled = 0;
    fetchMock.mockImplementation((input) => {
      const url = String(input);
      if (url.endsWith("/csrf-token")) return Promise.resolve(jsonResponse({ csrf_token: "csrf-reader" }));
      return Promise.resolve(heldReaderResponse(readResult, () => { readerCancelled += 1; }));
    });
    const callbacks = { onError: vi.fn(), onComplete: vi.fn(), onMessage: vi.fn() };
    const dispose = chatStream(messages, callbacks);
    disposers.push(dispose);
    await settleMicrotasks();

    dispose();
    readResult.resolve({ value: undefined, done: true });
    await settleMicrotasks();
    vi.advanceTimersByTime(150_000);
    await settleMicrotasks();

    expect(readerCancelled).toBe(1);
    expect(callbacks.onError).not.toHaveBeenCalled();
    expect(callbacks.onComplete).not.toHaveBeenCalled();
    expect(callbacks.onMessage).not.toHaveBeenCalled();
  });

  it("suppresses later side channels when onMessage reentrantly replaces the principal", async () => {
    const sideChannels: string[] = [];
    const callbacks: ChatStreamCallbacks = {
      onMessage: () => {
        sideChannels.push("message");
        publishAuthPrincipal({ id: 8105, role: "admin" });
      },
      onSources: () => sideChannels.push("sources"),
      onFinalContent: () => sideChannels.push("final"),
      onCitationConfidence: () => sideChannels.push("confidence"),
      onComplete: () => sideChannels.push("complete"),
    };
    fetchMock.mockImplementation((input) => {
      const url = String(input);
      if (url.endsWith("/csrf-token")) return Promise.resolve(jsonResponse({ csrf_token: "csrf-reentrant" }));
      return Promise.resolve(sseResponse(
        'data: {"type":"content","content":"hello","sources":[{"title":"source"}],"repaired_content":"final","citation_confidence":{"hello":0.8}}\n\n',
        'data: [DONE]\n\n',
      ));
    });
    const dispose = chatStream(messages, callbacks);
    disposers.push(dispose);
    await settleMicrotasks();

    expect(sideChannels).toEqual(["message"]);
  });

  it("completes an independent stream with messages, metrics, citations, and DONE", async () => {
    const events: string[] = [];
    const callbacks: ChatStreamCallbacks = {
      onMessage: (message) => events.push(`message:${String(message)}`),
      onReasoningMetrics: (metrics) => events.push(`metrics:${JSON.stringify(metrics)}`),
      onFinishReason: (reason) => events.push(`finish:${reason}`),
      onSources: (sources) => events.push(`sources:${sources.length}`),
      onComplete: () => events.push("complete"),
    };
    fetchMock.mockImplementation((input) => {
      const url = String(input);
      if (url.endsWith("/csrf-token")) return Promise.resolve(jsonResponse({ csrf_token: "csrf-positive" }));
      return Promise.resolve(sseResponse(
        'id: answer-1\ndata: {"type":"content","content":"hello"}\n\n',
        'data: {"type":"content","content":" world","sources":[{"title":"doc"}]}\n\n',
        'data: {"type":"done","llm_metrics":{"reasoning_duration_ms":12,"reasoning_tokens_estimate":3,"finish_reason":"stop"}}\n\n',
        'data: [DONE]\n\n',
      ));
    });
    const dispose = chatStream(messages, callbacks, 7, "thinking");
    disposers.push(dispose);
    await settleMicrotasks();

    expect(events).toContain("message:hello");
    expect(events).toContain("message: world");
    expect(events).toContain("sources:1");
    expect(events.some((event) => event.startsWith("metrics:"))).toBe(true);
    expect(events[events.length - 1]).toBe("complete");
  });

  it("resumes durable interruptions at 500ms increments with the latest Last-Event-ID", async () => {
    const calls: Array<{ headers: HeadersInit | undefined; body: string | null }> = [];
    const messagesSeen: string[] = [];
    let chatCall = 0;
    fetchMock.mockImplementation((input, init) => {
      const url = String(input);
      if (url.endsWith("/csrf-token")) return Promise.resolve(jsonResponse({ csrf_token: "csrf-resume" }));
      calls.push({ headers: init?.headers, body: typeof init?.body === "string" ? init.body : null });
      const id = chatCall++;
      if (id < 3) {
        return Promise.resolve(sseResponse(
          `id: resume-${id}\ndata: {"type":"content","content":"part-${id}"}\n\n`,
        ));
      }
      return Promise.resolve(sseResponse(
        `id: resume-${id}\ndata: {"type":"content","content":"part-${id}"}\n\n`,
        'data: [DONE]\n\n',
      ));
    });
    const callbacks: ChatStreamCallbacks = {
      onMessage: (message) => messagesSeen.push(String(message)),
      onComplete: vi.fn(),
      onError: vi.fn(),
    };
    const dispose = chatStream(messages, callbacks, undefined, undefined, undefined, undefined, undefined, undefined, undefined, {
      sessionId: 8,
      turnId: "turn-resume",
    });
    disposers.push(dispose);
    await settleMicrotasks();
    vi.advanceTimersByTime(500);
    await settleMicrotasks();
    vi.advanceTimersByTime(1000);
    await settleMicrotasks();
    vi.advanceTimersByTime(1500);
    await settleMicrotasks();

    expect(calls).toHaveLength(4);
    expect(new Headers(calls[0].headers).get("Last-Event-ID")).toBeNull();
    expect(new Headers(calls[1].headers).get("Last-Event-ID")).toBe("resume-0");
    expect(new Headers(calls[2].headers).get("Last-Event-ID")).toBe("resume-1");
    expect(new Headers(calls[3].headers).get("Last-Event-ID")).toBe("resume-2");
    expect(calls.map((call) => call.body)).toEqual([calls[0].body, calls[0].body, calls[0].body, calls[0].body]);
    expect(messagesSeen).toEqual(["part-0", "part-1", "part-2", "part-3"]);
    expect(callbacks.onComplete).toHaveBeenCalledTimes(1);
    expect(callbacks.onError).not.toHaveBeenCalled();
  });

  it("does not let an old owner's backoff wake after a new stream starts", async () => {
    let chatCall = 0;
    const calls: number[] = [];
    fetchMock.mockImplementation((input) => {
      const url = String(input);
      if (url.endsWith("/csrf-token")) return Promise.resolve(jsonResponse({ csrf_token: "csrf-owner-backoff" }));
      const id = chatCall++;
      calls.push(id);
      return Promise.resolve(id === 0
        ? sseResponse('id: old\ndata: {"type":"content","content":"old"}\n\n')
        : sseResponse('id: new\ndata: {"type":"content","content":"new"}\n\ndata: [DONE]\n\n'));
    });
    const oldCallbacks = { onError: vi.fn(), onMessage: vi.fn() };
    const oldDispose = chatStream(messages, oldCallbacks, undefined, undefined, undefined, undefined, undefined, undefined, undefined, {
      sessionId: 9,
      turnId: "old-turn",
    });
    disposers.push(oldDispose);
    await settleMicrotasks();
    publishAuthPrincipal({ id: 8106, role: "user" });
    const newCallbacks = { onComplete: vi.fn(), onMessage: vi.fn() };
    const newDispose = chatStream(messages, newCallbacks);
    disposers.push(newDispose);
    await settleMicrotasks();
    vi.advanceTimersByTime(10_000);
    await settleMicrotasks();

    expect(calls).toEqual([0, 1]);
    expect(oldCallbacks.onError).not.toHaveBeenCalled();
    expect(newCallbacks.onComplete).toHaveBeenCalledTimes(1);
  });

  it("settles a real reader read rejection without turning undefined into a successful result", async () => {
    const readFailure = deferred<never>();
    const reader = {
      read: vi.fn(() => readFailure.promise),
      cancel: vi.fn(async () => undefined),
    } as unknown as ReadableStreamDefaultReader<Uint8Array>;
    const callbacks = { onError: vi.fn(), onComplete: vi.fn() };
    const parsePromise = parseSSEStream(reader, callbacks);
    readFailure.reject(new Error("reader failed"));
    await expect(parsePromise).rejects.toThrow("reader failed");
    expect(callbacks.onError).not.toHaveBeenCalled();
    expect(callbacks.onComplete).not.toHaveBeenCalled();
  });
});
