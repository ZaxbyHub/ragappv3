/**
 * Public refresh preserving controls for issue #775.
 *
 * These tests exercise the public refreshAccessToken export with the real core
 * implementation and stub only the browser fetch boundary.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { refreshAccessToken, resetCsrfToken } from "@/lib/api/core";

function jsonResponse(status: number, body: unknown = {}): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: new Headers(),
    json: () => Promise.resolve(body),
  } as unknown as Response;
}

describe("issue #775 public refresh preserving controls", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
    resetCsrfToken();
  });

  it("still attempts /auth/refresh when forced CSRF acquisition fails", async () => {
    const fetchMock = vi.fn()
      .mockRejectedValueOnce(new TypeError("CSRF bootstrap unavailable"))
      .mockResolvedValueOnce(jsonResponse(401, { detail: "refresh rejected" }));
    vi.stubGlobal("fetch", fetchMock);
    resetCsrfToken();

    await expect(refreshAccessToken()).resolves.toBeNull();

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(String(fetchMock.mock.calls[0][0])).toContain("/csrf-token");
    expect(String(fetchMock.mock.calls[1][0])).toContain("/auth/refresh");
  });

  it("rejects a non-SyntaxError refresh body read failure", async () => {
    const bodyError = new TypeError("refresh body read failed");
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse(200, { csrf_token: "csrf-body" }))
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        headers: new Headers(),
        json: () => Promise.reject(bodyError),
      } as unknown as Response);
    vi.stubGlobal("fetch", fetchMock);
    resetCsrfToken();

    await expect(refreshAccessToken()).rejects.toThrow("refresh body read failed");
    expect(String(fetchMock.mock.calls[1][0])).toContain("/auth/refresh");
  });
});
