/**
 * Portal-resilience client contracts (branch: fix/portal-resilience).
 *
 * - apiPatch expresses PATCH as POST + X-HTTP-Method-Override (the portal
 *   rejects PATCH on the wire; the backend rewrites the method before
 *   routing only for paths that have a PATCH handler).
 * - parseDraftRoomError falls back to the X-Meridian-Error-Code response
 *   header when the portal replaced the JSON error body with HTML.
 * - draftCompileFailureMessage maps provider_origin_not_allowed to an
 *   administrator-configuration message (retrying cannot help).
 */
import { describe, expect, it, vi } from "vitest";

import {
  METHOD_OVERRIDE_HEADER,
  apiClient,
  shouldRefreshAndRetryAfter403,
  withMethodOverride,
} from "./core";
import { draftCompileFailureMessage, parseDraftRoomError } from "./draftRoom";

describe("shouldRefreshAndRetryAfter403 (T4b)", () => {
  it("retries a mutating 403 exactly once", () => {
    expect(shouldRefreshAndRetryAfter403(403, "post", false)).toBe(true);
    expect(shouldRefreshAndRetryAfter403(403, "put", false)).toBe(true);
    expect(shouldRefreshAndRetryAfter403(403, "patch", false)).toBe(true);
    expect(shouldRefreshAndRetryAfter403(403, "delete", false)).toBe(true);
  });

  it("never retries a second time", () => {
    expect(shouldRefreshAndRetryAfter403(403, "post", true)).toBe(false);
  });

  it("never retries non-403 statuses", () => {
    for (const status of [400, 401, 409, 422, 429, 500, 502, 503]) {
      expect(shouldRefreshAndRetryAfter403(status, "post", false)).toBe(false);
    }
  });

  it("never retries non-mutating methods", () => {
    for (const method of ["get", "head", "options", undefined]) {
      expect(shouldRefreshAndRetryAfter403(403, method, false)).toBe(false);
    }
  });
});

describe("withMethodOverride (T2b)", () => {
  it("adds X-HTTP-Method-Override: PATCH to the headers", () => {
    const config = withMethodOverride();
    expect(config.headers).toEqual({ "X-HTTP-Method-Override": "PATCH" });
  });

  it("preserves caller headers and config alongside the override header", () => {
    const signal = new AbortController().signal;
    const config = withMethodOverride({
      headers: { "X-Custom": "1" },
      signal,
    });
    expect((config.headers as Record<string, string>)["X-Custom"]).toBe("1");
    expect((config.headers as Record<string, string>)["X-HTTP-Method-Override"]).toBe("PATCH");
    expect(config.signal).toBe(signal);
  });

  it("is used by call sites through apiClient.post so the module seam tests mock stays intact", async () => {
    const spy = vi
      .spyOn(apiClient, "post")
      .mockResolvedValue({ data: { ok: true } } as never);
    try {
      await apiClient.post("/drafts/42", { title: "t" }, withMethodOverride());
      expect(spy).toHaveBeenCalledTimes(1);
      const [url, data, config] = spy.mock.calls[0];
      expect(url).toBe("/drafts/42");
      expect(data).toEqual({ title: "t" });
      expect(
        (config as { headers: Record<string, string> }).headers[
          "X-HTTP-Method-Override"
        ],
      ).toBe("PATCH");
    } finally {
      spy.mockRestore();
    }
  });
});

describe("parseDraftRoomError header fallback", () => {
  const normalized = (headers: Record<string, string>, body: unknown) => ({
    message: "Request failed",
    originalError: { response: { status: 503, headers, data: body } },
  });

  it("reads the code from X-Meridian-Error-Code when the body is not JSON", () => {
    const parsed = parseDraftRoomError(
      normalized(
        { "x-meridian-error-code": "provider_origin_not_allowed" },
        "<html>503 Service Unavailable</html>",
      ),
    );
    expect(parsed.code).toBe("provider_origin_not_allowed");
    expect(parsed.status).toBe(503);
  });

  it("prefers the body code over the header when both exist", () => {
    const parsed = parseDraftRoomError(
      normalized(
        { "x-meridian-error-code": "header_code" },
        { detail: "d", code: "body_code" },
      ),
    );
    expect(parsed.code).toBe("body_code");
  });

  it("falls back to unknown when neither exists", () => {
    const parsed = parseDraftRoomError(normalized({}, "<html>503</html>"));
    expect(parsed.code).toBe("unknown");
  });
});

describe("draftCompileFailureMessage", () => {
  it("maps provider_origin_not_allowed to an administrator-configuration message", () => {
    const msg = draftCompileFailureMessage({
      message: "Request failed (provider_origin_not_allowed)",
      originalError: {
        response: {
          status: 503,
          headers: { "x-meridian-error-code": "provider_origin_not_allowed" },
          data: "<html>503</html>",
        },
      },
    });
    expect(msg).toMatch(/administrator/i);
    expect(msg).toMatch(/retrying will not help/i);
  });

  it("passes the detail through for other codes", () => {
    const msg = draftCompileFailureMessage({
      message: "boom",
      originalError: {
        response: {
          status: 409,
          headers: {},
          data: { detail: "invalid state", code: "invalid_state" },
        },
      },
    });
    expect(msg).toBe("invalid state");
  });
});
