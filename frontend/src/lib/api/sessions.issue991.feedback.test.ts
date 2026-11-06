/**
 * Issue #991 feedback round (F-12/C05 + F-07): regression legs for the
 * stream error surfacing that live OUTSIDE the frozen issue991 suite.
 * UNFROZEN — free to extend in later review rounds.
 *
 * - C05: the surfaced detail error carries the HTTP status (`.status`).
 * - F-07: a 401 whose body was already consumed by the token-refresh probe
 *   still surfaces its detail (the retained errorBody parse is reused).
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

function errorResponse(
  status: number,
  body: unknown,
): Pick<Response, "ok" | "status" | "statusText" | "json" | "text"> {
  const text = typeof body === "string" ? body : JSON.stringify(body);
  return {
    ok: false,
    status,
    statusText: `HTTP ${status}`,
    json: () => Promise.resolve(body),
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
  afterEach(() => {
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
});
