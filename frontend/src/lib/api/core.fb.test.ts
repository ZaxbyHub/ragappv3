/**
 * Feedback-round pin (PRR-008): the 401 interceptor decision path — the
 * shipping site of the #774 UI-R4-09 behavior. Drives apiClient's LAST
 * response-error handler (the normalizer/401 interceptor) with a constructed
 * AxiosError and the REAL refreshAccessToken behind a stubbed fetch:
 *   - refresh transport failure (fetch rejects) → the original 401 is
 *     re-rejected in the SHARED normalized shape (.message/.status/
 *     .originalError), the in-memory token is RETAINED, and no logout fires;
 *   - auth-shaped refresh rejection (401 from /auth/refresh → null) → the
 *     token is cleared (logout path).
 * (New unfrozen file; the frozen checkpoint manifest is byte-locked.)
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  apiClient,
  getJwtAccessToken,
  resetCsrfToken,
  setJwtAccessToken,
} from "./core";

function axiosError401(detail = "token_expired") {
  return {
    config: { url: "/x", method: "get", headers: {} },
    response: { status: 401, data: { detail }, headers: {} },
    isAxiosError: true,
    message: "Request failed with status code 401",
    name: "AxiosError",
  };
}

function lastResponseRejectHandler(): (e: unknown) => Promise<unknown> {
  const handlers = (
    apiClient.interceptors as unknown as {
      response: {
        handlers: Array<{ fulfilled: unknown; rejected: (e: unknown) => Promise<unknown> }>;
      };
    }
  ).response.handlers;
  expect(handlers.length).toBeGreaterThan(0);
  return handlers[handlers.length - 1].rejected;
}

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: new Headers(),
    json: () => Promise.resolve(body),
  } as unknown as Response;
}

describe("issue 774 401 interceptor decision path (feedback pin)", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    resetCsrfToken();
    setJwtAccessToken("live-jwt");
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    setJwtAccessToken(null);
  });

  it("keeps the token and re-rejects the NORMALIZED original 401 when the refresh fails on transport", async () => {
    // Every network call fails: ensureCsrfToken(true) and /auth/refresh both
    // reject → _doRefresh rethrows (transport-class) → the interceptor must
    // skip logout and fall through to the shared normalizer (PRR-011).
    fetchMock.mockRejectedValue(new TypeError("Failed to fetch"));

    const rejected = await lastResponseRejectHandler()(axiosError401()).then(
      () => "resolved",
      (e: unknown) => e
    );

    expect(rejected).toBeInstanceOf(Error);
    const e = rejected as Error & { status?: number; originalError?: unknown };
    // Normalized shape: the shared tail extracted detail from response.data
    // (the raw AxiosError's own message was "Request failed with status code
    // 401") and attached status + originalError (PRR-011).
    expect(e.message).toBe("token_expired");
    expect(e.status).toBe(401);
    expect(e.originalError).toBeTruthy();
    // Session retained: no logout cleared the in-memory token.
    expect(getJwtAccessToken()).toBe("live-jwt");
  });

  it("clears the token (logout path) when the refresh is auth-shaped null", async () => {
    // CSRF fetch succeeds; /auth/refresh answers 401 (auth-shaped) → null.
    fetchMock.mockImplementationOnce(() =>
      Promise.resolve(jsonResponse(200, { csrf_token: "t" }))
    );
    fetchMock.mockImplementationOnce(() =>
      Promise.resolve(jsonResponse(401, { detail: "Refresh token missing" }))
    );

    const rejected = await lastResponseRejectHandler()(axiosError401()).then(
      () => "resolved",
      (e: unknown) => e
    );

    expect(rejected).toBeInstanceOf(Error);
    const e = rejected as Error & { status?: number };
    expect(e.message).toBe("token_expired");
    expect(e.status).toBe(401);
    // Logout path ran: the in-memory token is gone (redirectToLogin is a
    // jsdom navigation no-op).
    expect(getJwtAccessToken()).toBeNull();
  });
});
