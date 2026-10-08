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
import { chatStream } from "./sessions";

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
}

const pendingDeferreds = new Set<{ resolve: (value: unknown) => void }>();

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((promiseResolve) => {
    resolve = promiseResolve;
  });
  promise.catch(() => undefined);
  pendingDeferreds.add({ resolve: resolve as (value: unknown) => void });
  return { promise, resolve };
}

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
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

async function settleMicrotasks(): Promise<void> {
  for (let turn = 0; turn < 24; turn += 1) await Promise.resolve();
}

function callbacks(): ChatStreamCallbacks & { onRetired: ReturnType<typeof vi.fn> } {
  return {
    onMessage: vi.fn(), onSources: vi.fn(), onMemories: vi.fn(), onWiki: vi.fn(),
    onKMS: vi.fn(), onCitationValidation: vi.fn(), onFinalContent: vi.fn(),
    onCitationConfidence: vi.fn(), onUnverifiableClaims: vi.fn(), onCurrencyWarnings: vi.fn(),
    onCitationEnforcement: vi.fn(), onMode: vi.fn(), onStage: vi.fn(),
    onEvidenceCandidates: vi.fn(), onReasoning: vi.fn(), onReasoningMetrics: vi.fn(),
    onFinishReason: vi.fn(), onError: vi.fn(), onComplete: vi.fn(), onRetired: vi.fn(),
  };
}

const messages: ChatMessage[] = [{ role: "user", content: "hello" }];

describe("chat stream LOW03 retirement notification", () => {
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
    publishAuthPrincipal(null);
    fetchMock.mockReset();
    vi.unstubAllGlobals();
  });

  function holdChatFetch(heldFetch: Deferred<Response>): void {
    fetchMock.mockImplementation((input) => {
      if (String(input).endsWith("/csrf-token")) {
        return Promise.resolve(jsonResponse({ csrf_token: "csrf-low03" }));
      }
      return heldFetch.promise;
    });
  }

  function start(callbackSet: ChatStreamCallbacks): () => void {
    const dispose = chatStream(messages, callbackSet, 4, "instant", undefined, undefined, undefined, undefined, undefined, {
      sessionId: 44,
      turnId: "low03-retirement",
    });
    disposers.push(dispose);
    return dispose;
  }

  it.each([
    ["owner replacement", () => reserveReplacementAuthOwner()],
    ["principal replacement", () => publishAuthPrincipal({ id: 8102, role: "admin" })],
  ])("notifies once for %s after the chat fetch is admitted", async (_name, retire) => {
    const heldFetch = deferred<Response>();
    const callbackSet = callbacks();
    holdChatFetch(heldFetch);
    const dispose = start(callbackSet);
    await settleMicrotasks();
    expect(fetchMock.mock.calls.some(([input]) => String(input).includes("/chat"))).toBe(true);

    retire();
    dispose();
    dispose();
    await settleMicrotasks();
    heldFetch.resolve(sseResponse(
      'data: {"type":"content","content":"stale"}\n\n',
      "data: [DONE]\n\n",
    ));
    await settleMicrotasks();

    expect(callbackSet.onRetired).toHaveBeenCalledTimes(1);
    expect(callbackSet.onMessage).not.toHaveBeenCalled();
    expect(callbackSet.onError).not.toHaveBeenCalled();
    expect(callbackSet.onComplete).not.toHaveBeenCalled();
  });
});
