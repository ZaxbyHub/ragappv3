/**
 * Issue #991 acceptance check — C8 (characterization, PRESERVING), split into
 * its own file so the file-level exit code reflects this check alone (its
 * DISCRIMINATING sibling C7 lives in sessions.issue991.test.ts and is RED at
 * base). This file is FROZEN for trace 1040-991-health-401-chat-cap-sync.
 *
 * C8: the chat stream-send path (chatStream in sessions.ts, fetch POST to
 * /chat/stream) answering 500 with a NON-JSON plain-text body ("Internal
 * Server Error") keeps today's error message shape
 * (`/^HTTP error! status: 500$/`). GREEN at base; must stay green after the
 * fix (a non-JSON body must fall back to the status-only message).
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

describe("chatStream status-only fallback pin (issue #991)", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  it("C8: a 500 with a non-JSON body keeps today's status-only error message (GREEN at base; must stay green)", async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(errorResponse(500, "Internal Server Error")),
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
      expect(delivered.message).toMatch(/^HTTP error! status: 500$/);
    } finally {
      dispose?.();
    }
  });
});
