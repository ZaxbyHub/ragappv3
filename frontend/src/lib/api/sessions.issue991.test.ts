/**
 * Issue #991 acceptance check — C7 (AC7, DISCRIMINATING). This file is FROZEN
 * for trace 1040-991-health-401-chat-cap-sync. (Its PRESERVING sibling C8
 * lives in sessions.issue991.statuspin.test.ts so each file's exit code
 * reflects its own check class.)
 *
 * C7: the chat stream-send path (chatStream in sessions.ts, fetch POST to
 * /chat/stream) answering 422 with the server's named detail
 * (`{"detail": "the final message exceeds chat_message_max_chars (20000
 * characters)"}` — the exact 422 body backend/app/api/routes/chat.py:2123-2128
 * emits for the final live message) must deliver an Error to onError whose
 * message includes `chat_message_max_chars`. At base sessions.ts:515-517
 * throws `HTTP error! status: 422` and discards the body → RED at base, GREEN
 * after the fix.
 *
 * Two PRESERVING fallback pins ride in this file (the C8 500 leg's mirrors at
 * 422, so the detail-extraction fix is discriminated within the same file):
 * a 422 with a NON-JSON plain-text body, and a 422 whose body read (json()
 * AND text()) rejects, both keep today's status-only message
 * (`/^HTTP error! status: 422$/`) — GREEN at base and after the fix; the file
 * stays RED at base through the C7 detail test.
 *
 * Round-4 amendments add five more PRESERVING pins (all GREEN at base and
 * must stay green after the fix — base never reads the body, so every one of
 * them already produces the status-only line):
 * - a 422 whose body is valid JSON with detail "" (empty string) keeps the
 *   status-only message — only a NON-EMPTY string detail is surfaced;
 * - a 422 whose body is valid JSON with detail ABSENT ({} body) keeps it;
 * - a 422 whose body is valid JSON with detail NULL keeps it;
 * - a 422 whose body is valid JSON with detail an OBJECT (e.g.
 *   {msg:"x"} — FastAPI validation-shape details) keeps it;
 * - a disposal pin: a 422 whose body read (json() AND text()) NEVER settles,
 *   with dispose() called while the request is pending — no crash and no
 *   onError call (the sessions.issue775.cancellation disposal idiom).
 * The file stays RED at base through the C7 detail test.
 *
 * Mocking idiom mirrors sessions.issue775.cancellation.test.ts: "./core" is
 * mocked (CSRF ensured, no JWT, API_BASE_URL "/api") and global fetch is
 * stubbed per test. Real timers throughout.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

const coreMocks = vi.hoisted(() => ({
  ensureCsrfToken: vi.fn(async () => "csrf"),
  refreshAccessToken: vi.fn(async () => "fresh-token"),
  isTokenNearExpiry: vi.fn(() => false),
}));

vi.mock("./core", () => ({
  apiClient: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
  API_BASE_URL: "/api",
  get _jwtAccessToken(): string | null {
    return null;
  },
  getCsrfCookie: () => null,
  getCsrfToken: () => null,
  ensureCsrfToken: (...args: unknown[]) => coreMocks.ensureCsrfToken(...(args as [])),
  refreshAccessToken: (...args: unknown[]) => coreMocks.refreshAccessToken(...(args as [])),
  isTokenNearExpiry: (...args: unknown[]) => coreMocks.isTokenNearExpiry(...(args as [])),
}));

import { chatStream } from "./sessions";

/** Minimal Response-like object: both json() and text() exist so either
 * extraction strategy the fix uses is exercisable. */
function errorResponse(
  status: number,
  body: unknown,
): Pick<Response, "ok" | "status" | "statusText" | "json" | "text"> {
  const text =
    typeof body === "string" ? body : JSON.stringify(body);
  return {
    ok: false,
    status,
    statusText: `HTTP ${status}`,
    json:
      typeof body === "string"
        ? () => Promise.reject(new SyntaxError("Unexpected token in JSON"))
        : () => Promise.resolve(body),
    text: () => Promise.resolve(text),
  };
}

/** Response variant whose json() AND text() BOTH reject (e.g. a locked or
 * disturbed body stream): whatever extraction strategy the fix uses, the
 * read itself fails and must fall back to the status-only message. */
function unreadableErrorResponse(
  status: number,
): Pick<Response, "ok" | "status" | "statusText" | "json" | "text"> {
  const failure = () => Promise.reject(new Error("body stream locked"));
  return {
    ok: false,
    status,
    statusText: `HTTP ${status}`,
    json: failure,
    text: failure,
  };
}

/** Bounded real-timer poll (no fake timers where a settle wait is needed). */
async function until(predicate: () => boolean, timeoutMs = 2_000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (!predicate()) {
    if (Date.now() > deadline) {
      throw new Error("condition not met before deadline");
    }
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
}

describe("chatStream error surfacing (issue #991)", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  it("C7: a 422 with the server's chat_message_max_chars detail delivers that detail to onError (RED at base, GREEN after the fix)", async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(
        errorResponse(422, {
          detail:
            "the final message exceeds chat_message_max_chars (20000 characters)",
        }),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    const onError = vi.fn();
    let dispose: (() => void) | undefined;
    try {
      dispose = chatStream(
        [{ role: "user", content: "x".repeat(25_000) }] as never,
        { onError } as never,
        7,
      );
      await until(() => onError.mock.calls.length > 0);

      expect(onError).toHaveBeenCalledTimes(1);
      const delivered = onError.mock.calls[0]?.[0] as Error;
      expect(
        delivered.message,
        "the stream 422 must surface the server's named rejection, not a status-only line",
      ).toContain("chat_message_max_chars");
    } finally {
      dispose?.();
    }
  });

  it("C7 fallback pin: a 422 with a NON-JSON body keeps the status-only message (GREEN at base; must stay green)", async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(errorResponse(422, "Unprocessable Entity")),
    );
    vi.stubGlobal("fetch", fetchMock);
    const onError = vi.fn();
    let dispose: (() => void) | undefined;
    try {
      dispose = chatStream(
        [{ role: "user", content: "hello" }] as never,
        { onError } as never,
        7,
      );
      await until(() => onError.mock.calls.length > 0);

      expect(onError).toHaveBeenCalledTimes(1);
      const delivered = onError.mock.calls[0]?.[0] as Error;
      expect(
        delivered.message,
        "a 422 whose body is not JSON must fall back to the exact status-only message — the detail extraction must not append, invent, or throw",
      ).toMatch(/^HTTP error! status: 422$/);
    } finally {
      dispose?.();
    }
  });

  it("C7 fallback pin: a 422 whose body read rejects falls back to the status-only message (GREEN at base; must stay green)", async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(unreadableErrorResponse(422)),
    );
    vi.stubGlobal("fetch", fetchMock);
    const onError = vi.fn();
    let dispose: (() => void) | undefined;
    try {
      dispose = chatStream(
        [{ role: "user", content: "hello" }] as never,
        { onError } as never,
        7,
      );
      await until(() => onError.mock.calls.length > 0);

      expect(onError).toHaveBeenCalledTimes(1);
      const delivered = onError.mock.calls[0]?.[0] as Error;
      expect(
        delivered.message,
        "a 422 whose body read fails (json() and text() both reject) must keep the status-only message — a failed extraction is never a failed error report",
      ).toMatch(/^HTTP error! status: 422$/);
    } finally {
      dispose?.();
    }
  });

  it("C7 fallback pin: a 422 whose body is valid JSON with an EMPTY-STRING detail keeps the status-only message (GREEN at base; must stay green)", async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(errorResponse(422, { detail: "" })),
    );
    vi.stubGlobal("fetch", fetchMock);
    const onError = vi.fn();
    let dispose: (() => void) | undefined;
    try {
      dispose = chatStream(
        [{ role: "user", content: "hello" }] as never,
        { onError } as never,
        7,
      );
      await until(() => onError.mock.calls.length > 0);

      expect(onError).toHaveBeenCalledTimes(1);
      const delivered = onError.mock.calls[0]?.[0] as Error;
      expect(
        delivered.message,
        "an empty-string detail carries no information: it must fall back to the exact status-only message, not an error ending in a bare separator",
      ).toMatch(/^HTTP error! status: 422$/);
    } finally {
      dispose?.();
    }
  });

  it("C7 fallback pin: a 422 whose body is valid JSON with detail ABSENT keeps the status-only message (GREEN at base; must stay green)", async () => {
    const fetchMock = vi.fn(() => Promise.resolve(errorResponse(422, {})));
    vi.stubGlobal("fetch", fetchMock);
    const onError = vi.fn();
    let dispose: (() => void) | undefined;
    try {
      dispose = chatStream(
        [{ role: "user", content: "hello" }] as never,
        { onError } as never,
        7,
      );
      await until(() => onError.mock.calls.length > 0);

      expect(onError).toHaveBeenCalledTimes(1);
      const delivered = onError.mock.calls[0]?.[0] as Error;
      expect(
        delivered.message,
        "a JSON body without a detail key must keep the status-only message",
      ).toMatch(/^HTTP error! status: 422$/);
    } finally {
      dispose?.();
    }
  });

  it("C7 fallback pin: a 422 whose body is valid JSON with detail NULL keeps the status-only message (GREEN at base; must stay green)", async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(errorResponse(422, { detail: null })),
    );
    vi.stubGlobal("fetch", fetchMock);
    const onError = vi.fn();
    let dispose: (() => void) | undefined;
    try {
      dispose = chatStream(
        [{ role: "user", content: "hello" }] as never,
        { onError } as never,
        7,
      );
      await until(() => onError.mock.calls.length > 0);

      expect(onError).toHaveBeenCalledTimes(1);
      const delivered = onError.mock.calls[0]?.[0] as Error;
      expect(
        delivered.message,
        "a null detail must keep the status-only message — null is not a string to surface",
      ).toMatch(/^HTTP error! status: 422$/);
    } finally {
      dispose?.();
    }
  });

  it("C7 fallback pin: a 422 whose body is valid JSON with detail an OBJECT keeps the status-only message (GREEN at base; must stay green)", async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(errorResponse(422, { detail: { msg: "x" } })),
    );
    vi.stubGlobal("fetch", fetchMock);
    const onError = vi.fn();
    let dispose: (() => void) | undefined;
    try {
      dispose = chatStream(
        [{ role: "user", content: "hello" }] as never,
        { onError } as never,
        7,
      );
      await until(() => onError.mock.calls.length > 0);

      expect(onError).toHaveBeenCalledTimes(1);
      const delivered = onError.mock.calls[0]?.[0] as Error;
      expect(
        delivered.message,
        "an object detail (FastAPI validation shape) must keep the status-only message — only a non-empty STRING detail is ever surfaced",
      ).toMatch(/^HTTP error! status: 422$/);
    } finally {
      dispose?.();
    }
  });

  it("C7 disposal pin: disposing while a 422 body read is pending never crashes and never surfaces onError (GREEN at base; must stay green)", async () => {
    // The issue775.cancellation disposal idiom: the fetch is held open on a
    // deferred, dispose() runs while the request is still pending, and only
    // then does a 422 arrive whose body read (json() AND text()) NEVER
    // settles. The disposer must keep its contract — no crash, no onError —
    // whether the fix reads the body before or after its isCurrent() guard:
    // a disposed stream returns at the guard (base and guard-first fixes),
    // and a body-first read simply never completes against the
    // never-settling promise.
    const neverSettles = () => new Promise<never>(() => {});
    let resolveFetch!: (response: Response) => void;
    const fetchDeferred = new Promise<Response>((resolve) => {
      resolveFetch = resolve;
    });
    const fetchMock = vi.fn(() => fetchDeferred);
    vi.stubGlobal("fetch", fetchMock);
    const onError = vi.fn();
    let dispose: (() => void) | undefined;
    try {
      dispose = chatStream(
        [{ role: "user", content: "hello" }] as never,
        { onError } as never,
        7,
      );
      await until(() => fetchMock.mock.calls.length > 0);

      // Dispose while the request is pending (mirrors the issue775
      // disposer leg: the in-flight owner is aborted before any body
      // exists).
      dispose();
      dispose = undefined;

      resolveFetch({
        ok: false,
        status: 422,
        statusText: "HTTP 422",
        json: neverSettles,
        text: neverSettles,
      } as unknown as Response);
      await new Promise((resolve) => setTimeout(resolve, 50));

      expect(
        onError,
        "a disposed stream must never surface the 422 — not the status-only error and not a body-read artifact",
      ).not.toHaveBeenCalled();
    } finally {
      dispose?.();
    }
  });
});
