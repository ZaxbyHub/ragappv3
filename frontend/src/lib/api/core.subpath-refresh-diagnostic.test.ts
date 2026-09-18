// frontend/src/lib/api/core.subpath-refresh-diagnostic.test.ts
// Trace meridian-canvas-401-vault-zero — acceptance check C1 (AC1, RC1).
//
// FROZEN CONTRACT (frozen at Phase 2.5; refined by the PR #626 review round —
// the emission is gated to the two signatures a subpath cookie-path mismatch
// actually produces, so the contract below is the refined, reviewed form):
//
//   When the app is built for a subpath deployment — VITE_APP_BASENAME
//   non-empty, so the app's APP_BASENAME constant in src/lib/paths.ts is a
//   non-root prefix like "/meridian" — AND the silent token refresh fails
//   with an authentication-shaped rejection (refreshAccessToken() resolves
//   null because /auth/refresh returned 401 with the refresh cookie missing,
//   or 403 with x-csrf-error: true when csrf_protect rejects before the
//   handler — exactly the cookie-path-mismatch death loop in
//   04-root-cause.md RC1), the api/core module MUST emit an operator-targeted
//   diagnostic via console.error whose text includes BOTH literals
//   "APP_ROOT_PATH" and "VITE_APP_BASENAME" plus the actual prefix value.
//
//   Non-authentication failures (5xx, network errors) must NOT emit — the
//   diagnostic must not misattribute unrelated outages to a config mismatch.
//   The emission is once per failure burst: repeated failures do not re-emit,
//   a successful refresh resets the flag, and session boundaries
//   (login/register/logout in useAuthStore) start a new burst.
//
//   The refresh STILL resolves null: the fix adds an observable signal, it
//   does not change the return contract.
//
// Base behavior (94c0b925): _doRefresh() swallows the failure with no
// diagnostic anywhere, so the emission scenarios are RED at base for that
// reason; the suppression scenarios (no-emit) are GREEN at base but pin the
// reviewed semantics against regression.
//
// Mock boundary: global fetch — the same boundary the csrf/session api tests
// stub via vi.stubGlobal("fetch", ...). The module under test is the REAL
// @/lib/api/core, dynamically imported AFTER the env stubs so the subpath
// prefix is baked in at module-evaluation time (vi.stubEnv ordering gotcha).

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const fetchMock = vi.fn();

function jsonResponse(status: number, body: Record<string, unknown> = {}, headers: Record<string, string> = {}) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: new Headers(headers),
    json: async () => body,
  };
}

// Route by URL: ensureCsrfToken fetches {API_BASE_URL}/csrf-token before the
// refresh call; tests shape the two responses independently.
function routeFetch(csrfResponse: object, refreshResponse: object) {
  fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    return url.includes("/csrf-token") ? csrfResponse : refreshResponse;
  });
}

describe("api/core subpath refresh-failure diagnostic (trace meridian-canvas-401-vault-zero)", () => {
  beforeEach(() => {
    vi.resetModules();
    fetchMock.mockReset();
    // Hermetic env: both variables stubbed in beforeEach so no ambient env can
    // satisfy or defeat the module-evaluation-time constants (VITE_API_URL=''
    // is falsy and falls through to the derived appPath('/api')).
    vi.stubEnv("VITE_APP_BASENAME", "/meridian");
    vi.stubEnv("VITE_API_URL", "");
    vi.stubGlobal("fetch", fetchMock);
    vi.spyOn(console, "error").mockImplementation(() => {});
  });

  afterEach(() => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    vi.resetModules();
  });

  function diagnosticText(): string {
    return vi
      .mocked(console.error)
      .mock.calls.map((args) => args.map((arg) => String(arg)).join(" "))
      .join("\n");
  }

  it("emits a console.error diagnostic naming APP_ROOT_PATH and VITE_APP_BASENAME when the silent refresh fails under a baked subpath prefix", async () => {
    routeFetch(jsonResponse(401), jsonResponse(401));
    const core = await import("@/lib/api/core");
    const paths = await import("@/lib/paths");

    // Precondition: the env stubs really produced the subpath deployment
    // shape (if this fails the run is vacuous, not discriminating).
    expect(paths.APP_BASENAME).toBe("/meridian");
    expect(core.API_BASE_URL).toBe("/meridian/api");

    const token = await core.refreshAccessToken();

    // The return contract is unchanged: a failed refresh still resolves null.
    expect(token).toBeNull();

    const text = diagnosticText();

    expect(
      text,
      "subpath refresh-failure diagnostic missing: when refreshAccessToken() resolves null while APP_BASENAME is a non-empty prefix, the module must console.error a diagnostic naming APP_ROOT_PATH"
    ).toContain("APP_ROOT_PATH");
    expect(
      text,
      "subpath refresh-failure diagnostic missing: the diagnostic must also name VITE_APP_BASENAME"
    ).toContain("VITE_APP_BASENAME");
    // The operator value is the prefix itself, not just the variable names.
    expect(text).toContain("/meridian");
  });

  it("emits the diagnostic for a CSRF-marked 403 refresh rejection (backend prefix mismatch rejects before the handler)", async () => {
    routeFetch(jsonResponse(401), jsonResponse(403, {}, { "x-csrf-error": "true" }));
    const core = await import("@/lib/api/core");

    const token = await core.refreshAccessToken();

    expect(token).toBeNull();
    expect(diagnosticText()).toContain("APP_ROOT_PATH");
  });

  it("does NOT emit the diagnostic for a 5xx refresh failure (unrelated outage, not a config mismatch)", async () => {
    routeFetch(jsonResponse(401), jsonResponse(503));
    const core = await import("@/lib/api/core");

    const token = await core.refreshAccessToken();

    expect(token).toBeNull();
    expect(diagnosticText()).toBe("");
  });

  it("does NOT emit the diagnostic when fetch itself rejects (network error, not a config mismatch)", async () => {
    fetchMock.mockRejectedValue(new TypeError("Failed to fetch"));
    const core = await import("@/lib/api/core");

    const token = await core.refreshAccessToken();

    expect(token).toBeNull();
    expect(diagnosticText()).toBe("");
  });

  it("emits once per failure burst: a second consecutive failure does not re-emit, a successful refresh resets the flag, and a later failure emits again", async () => {
    routeFetch(jsonResponse(401), jsonResponse(401));
    const core = await import("@/lib/api/core");

    await core.refreshAccessToken();
    expect(diagnosticText()).toContain("APP_ROOT_PATH");

    // Second consecutive failure: same burst, suppressed.
    await core.refreshAccessToken();
    const afterSecondFailure = vi.mocked(console.error).mock.calls.length;
    expect(afterSecondFailure).toBe(1);

    // A successful refresh starts a new burst boundary.
    routeFetch(jsonResponse(401), jsonResponse(200, { access_token: "fresh-token" }));
    const refreshed = await core.refreshAccessToken();
    expect(refreshed).toBe("fresh-token");

    // The next failure after success emits again.
    routeFetch(jsonResponse(401), jsonResponse(401));
    await core.refreshAccessToken();
    expect(vi.mocked(console.error).mock.calls.length).toBe(afterSecondFailure + 1);
  });

  it("stays silent on root deployments (empty VITE_APP_BASENAME) even when the refresh fails", async () => {
    vi.stubEnv("VITE_APP_BASENAME", "");
    const core = await import("@/lib/api/core");
    const paths = await import("@/lib/paths");
    expect(paths.APP_BASENAME).toBe("");

    routeFetch(jsonResponse(401), jsonResponse(401));
    const token = await core.refreshAccessToken();

    expect(token).toBeNull();
    expect(diagnosticText()).toBe("");
  });
});
