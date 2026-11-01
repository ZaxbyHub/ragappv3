/**
 * Issue #774 regression tests (non-frozen): the refreshAccessToken transport
 * contract. Rejection ⇔ transport-class (network error, deadline, 5xx);
 * null ⇔ auth-shaped rejection or non-5xx 4xx. Timeout errors must never be
 * AbortError-named or abort-worded (user-cancel sentinels in sessions.ts and
 * useSendMessage.ts match on exactly those).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { resetCsrfToken } from "./core";

function jsonResponse(status: number, body: unknown = {}): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: new Headers(),
    json: () => Promise.resolve(body),
  } as unknown as Response;
}

describe("issue 774 refreshAccessToken transport contract", () => {
  let fetchMock: ReturnType<typeof vi.fn>;
  let core: typeof import("./core");

  beforeEach(async () => {
    vi.resetModules();
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    core = await import("./core");
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it("resolves null for an auth-shaped 401 rejection", async () => {
    resetCsrfToken();
    // First fetch: csrf token; second: the 401 refresh.
    fetchMock.mockResolvedValueOnce(jsonResponse(200, { csrf_token: "t" }));
    fetchMock.mockResolvedValueOnce(jsonResponse(401, { detail: "Refresh token missing" }));

    await expect(core.refreshAccessToken()).resolves.toBeNull();
  });

  it("rejects for a 5xx refresh response (outage, not a session verdict)", async () => {
    resetCsrfToken();
    fetchMock.mockResolvedValueOnce(jsonResponse(200, { csrf_token: "t" }));
    fetchMock.mockResolvedValueOnce(jsonResponse(503));

    await expect(core.refreshAccessToken()).rejects.toThrow(/status 503/);
  });

  it("rethrows the original error for a network failure", async () => {
    resetCsrfToken();
    fetchMock.mockResolvedValueOnce(jsonResponse(200, { csrf_token: "t" }));
    fetchMock.mockRejectedValueOnce(new TypeError("Failed to fetch"));

    await expect(core.refreshAccessToken()).rejects.toThrow("Failed to fetch");
  });

  it("rejects with a non-AbortError, non-abort-worded timeout error when the refresh fetch hangs", async () => {
    resetCsrfToken();
    vi.useFakeTimers();
    // The CSRF bootstrap fetch resolves; the /auth/refresh fetch hangs until
    // its own deadline aborts it.
    fetchMock.mockImplementationOnce(() =>
      Promise.resolve(jsonResponse(200, { csrf_token: "t" }))
    );
    fetchMock.mockImplementation(
      (_input: RequestInfo | URL, init?: RequestInit) =>
        new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () => {
            reject(new DOMException("Aborted", "AbortError"));
          });
        })
    );

    const attempt = core.refreshAccessToken();
    attempt.catch(() => undefined);
    await vi.advanceTimersByTimeAsync(10_000);

    await expect(attempt).rejects.toSatisfy((err: unknown) => {
      const e = err as Error;
      // The two user-cancel sentinels must NOT match (#774 plan constraint):
      // sessions.ts checks error.name === "AbortError"; useSendMessage.ts
      // checks /aborted|abort/i on the message.
      expect(e.name).not.toBe("AbortError");
      expect(e.message).not.toMatch(/abort/i);
      expect(e.message).toContain("timed out");
      return true;
    });
  });

  it("clears the csrf in-flight singleton on timeout so a retry issues a new fetch", async () => {
    resetCsrfToken();
    vi.useFakeTimers();
    const hung = vi.fn(
      (_input: RequestInfo | URL, init?: RequestInit) =>
        new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () => {
            reject(new DOMException("Aborted", "AbortError"));
          });
        })
    );
    vi.stubGlobal("fetch", hung);

    const first = core.ensureCsrfToken();
    first.catch(() => undefined);
    expect(hung).toHaveBeenCalledTimes(1);

    await vi.advanceTimersByTimeAsync(10_000);
    const second = core.ensureCsrfToken();
    second.catch(() => undefined);
    await vi.advanceTimersByTimeAsync(1);

    expect(hung).toHaveBeenCalledTimes(2);
  });

  it("rejects with the typed timeout when the refresh BODY stalls past the deadline, then a retry proceeds fresh", async () => {
    resetCsrfToken();
    vi.useFakeTimers();
    // Headers arrive (200 OK); the body never does — json() only settles when
    // the deadline aborts the fetch signal (implementation-review probe: the
    // clearTimeout placement is the regression vector under test).
    fetchMock.mockImplementationOnce(() =>
      Promise.resolve(jsonResponse(200, { csrf_token: "t" }))
    );
    // Headers arrive IMMEDIATELY (the fetch resolves); only the body hangs,
    // and json() settles solely when the deadline aborts the signal. This
    // ordering is what makes the stopDeadline() placement observable: an
    // after-fetch placement never fires the abort, the json promise never
    // settles, and the test times out RED (implementation-review round 3
    // verified both legs of this mock shape).
    fetchMock.mockImplementationOnce(
      (_input: RequestInfo | URL, init?: RequestInit) =>
        Promise.resolve({
          ok: true,
          status: 200,
          headers: new Headers(),
          json: () =>
            new Promise<never>((_resolve, reject) => {
              init?.signal?.addEventListener("abort", () => {
                reject(new DOMException("Aborted", "AbortError"));
              });
            }),
        } as unknown as Response)
    );

    const attempt = core.refreshAccessToken();
    attempt.catch(() => undefined);
    await vi.advanceTimersByTimeAsync(10_000);

    await expect(attempt).rejects.toSatisfy((err: unknown) => {
      const e = err as Error;
      expect(e.name).not.toBe("AbortError");
      expect(e.message).not.toMatch(/abort/i);
      expect(e.message).toContain("timed out");
      return true;
    });

    // The singleton must have cleared: a second attempt issues new fetches
    // instead of awaiting the wedged first promise.
    const callsAfterStall = fetchMock.mock.calls.length;
    fetchMock.mockImplementationOnce(() =>
      Promise.resolve(jsonResponse(200, { csrf_token: "t" }))
    );
    fetchMock.mockImplementationOnce(() =>
      Promise.resolve(jsonResponse(200, { access_token: "fresh" }))
    );
    await expect(core.refreshAccessToken()).resolves.toBe("fresh");
    expect(fetchMock.mock.calls.length).toBeGreaterThan(callsAfterStall);
  });

  it("resolves null when a 2xx refresh body fails to parse (proxy 200 + HTML)", async () => {
    resetCsrfToken();
    fetchMock.mockImplementationOnce(() =>
      Promise.resolve(jsonResponse(200, { csrf_token: "t" }))
    );
    fetchMock.mockImplementationOnce(() => {
      const syntaxError = new SyntaxError("Unexpected token < in JSON");
      return Promise.resolve({
        ok: true,
        status: 200,
        headers: new Headers(),
        json: () => Promise.reject(syntaxError),
      } as unknown as Response);
    });

    await expect(core.refreshAccessToken()).resolves.toBeNull();
  });
});
