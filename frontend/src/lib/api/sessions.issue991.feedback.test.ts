/**
 * Issue #991 feedback round (F-12/C05 + F-07): regression legs for the
 * stream error surfacing that live OUTSIDE the frozen issue991 suite.
 * UNFROZEN — free to extend in later review rounds.
 *
 * - C05: the surfaced detail error carries the HTTP status (`.status`).
 * - F-07: a 401 whose body was already consumed by the token-refresh probe
 *   still surfaces its detail via the retained errorBody parse (exercised
 *   with a non-null JWT so the 401 probe arm runs, then a non-retryable
 *   `token_invalid` detail falls through to the !response.ok arm).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

let jwtAccessToken: string | null = null;

const coreMocks = vi.hoisted(() => ({
  ensureCsrfToken: vi.fn(async () => "csrf"),
  refreshAccessToken: vi.fn(async () => "fresh-token"),
  isTokenNearExpiry: vi.fn(() => false),
}));

vi.mock("./core", () => ({
  apiClient: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
  API_BASE_URL: "/api",
  get _jwtAccessToken(): string | null {
    return jwtAccessToken;
  },
  getCsrfCookie: () => null,
  getCsrfToken: () => null,
  ensureCsrfToken: (...args: unknown[]) => coreMocks.ensureCsrfToken(...(args as [])),
  refreshAccessToken: (...args: unknown[]) => coreMocks.refreshAccessToken(...(args as [])),
  isTokenNearExpiry: (...args: unknown[]) => coreMocks.isTokenNearExpiry(...(args as [])),
}));

import { chatStream } from "./sessions";

function errorResponse(
  status: number,
  body: unknown,
): Pick<Response, "ok" | "status" | "statusText" | "json" | "text"> {
  const text = typeof body === "string" ? body : JSON.stringify(body);
  return {
    ok: false,
    status,
    statusText: `HTTP ${status}`,
    json: (() => {
      // Real Response semantics: the body can be read ONCE. A second json()
      // rejects with "body already used" — this is what makes the F-07 leg
      // below discriminating for the retained-parse reuse.
      let consumed = false;
      return () => {
        if (consumed) {
          return Promise.reject(new TypeError("body already used"));
        }
        consumed = true;
        return Promise.resolve(body);
      };
    })(),
    text: () => Promise.resolve(text),
  };
}

async function until(predicate: () => boolean, timeoutMs = 2_000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (!predicate()) {
    if (Date.now() > deadline) throw new Error("condition not met before deadline");
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
}

describe("chatStream detail surfacing — feedback pins (issue #991)", () => {
  beforeEach(() => {
    jwtAccessToken = null;
  });

  afterEach(() => {
    jwtAccessToken = null;
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  it("C05: the surfaced detail error carries the response status", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          errorResponse(422, {
            detail: "the final message exceeds chat_message_max_chars (20000 characters)",
          }),
        ),
      ),
    );
    const onError = vi.fn();
    let dispose: (() => void) | undefined;
    try {
      dispose = chatStream(
        [{ role: "user", content: "x".repeat(25_000) }] as never,
        { onError } as never,
        7,
      );
      await until(() => onError.mock.calls.length > 0);
      const delivered = onError.mock.calls[0]?.[0] as Error & { status?: number };
      expect(delivered.message).toContain("chat_message_max_chars");
      expect(delivered.status).toBe(422);
    } finally {
      dispose?.();
    }
  });

  it("F-07: a 401 whose body the refresh probe consumed still surfaces the retained detail", async () => {
    // Non-null JWT makes the 401 probe arm run and consume the body; the
    // detail is `token_invalid` (not retryable), so the flow falls through
    // to the !response.ok arm with errorBody already parsed.
    jwtAccessToken = "expired-token";
    const fetchMock = vi.fn(() =>
      Promise.resolve(
        errorResponse(401, { detail: "token_invalid: please sign in again" }),
      ),
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
      const delivered = onError.mock.calls[0]?.[0] as Error & { status?: number };
      // The retained parse surfaces the server detail (with status attached),
      // not the "body already used" status-only fallback. Removing the
      // errorBody reuse makes the second json() reject on this consumed-body
      // stub, so this leg fails — it pins the F-07 mechanism itself.
      expect(delivered.message).toContain("token_invalid");
      expect(delivered.status).toBe(401);
      // token_invalid is not retryable: the silent-refresh path must not run.
      expect(coreMocks.refreshAccessToken).not.toHaveBeenCalled();
    } finally {
      dispose?.();
    }
  });
});
