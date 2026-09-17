// CSRF resilience unit pins (issue #202 / FU-002, AC7).
//
// Three behaviors that were the exact surfaces involved in the F-004/F-005
// incidents had no unit-level protection:
//   (a) purgeStaleCsrfCookies never purges the authoritative app-root cookie
//       (subpath deployments);
//   (b) ensureCsrfToken(true) bypasses BOTH the in-memory cache and the
//       cookie jar (post-403 / post-refresh the cached token is known-stale);
//   (c) a 403 whose body is a small JSON Blob is decoded BEFORE the CSRF
//       detection, so the retry fires — and an over-cap Blob is left as a
//       Blob (no decode, no retry).
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const cookieWrites: string[] = [];
let mockCookies = "";
Object.defineProperty(document, "cookie", {
  get: () => mockCookies,
  set: (val: string) => {
    cookieWrites.push(val);
    mockCookies = val;
  },
  configurable: true,
});

describe("core.ts CSRF resilience pins (FU-002)", () => {
  beforeEach(() => {
    mockCookies = "";
    cookieWrites.length = 0;
    vi.resetModules();
  });

  afterEach(() => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  describe("purgeStaleCsrfCookies app-root exclusion", () => {
    const EXACT_APP_ROOT_WRITE = /path=\/meridian(?:\/)?;/;

    it("subpath deployment: purges shadows but never the /meridian app-root cookie", async () => {
      // Control the paths module directly: APP_BASENAME is computed at module
      // load from import.meta.env, which static replacement makes inert under
      // dynamic re-import. The doMock covers both specifiers core.ts uses.
      vi.resetModules();
      const pathsMock = {
        APP_BASENAME: "/meridian",
        appPath: (path: string, basename = "/meridian") =>
          basename ? `${basename}${path}` : path,
      };
      vi.doMock("@/lib/paths", () => pathsMock);
      vi.doMock("../paths", () => pathsMock);
      history.pushState({}, "", "/meridian/page");
      const { purgeStaleCsrfCookies } = await import("./core");

      purgeStaleCsrfCookies();

      // Shadows (Path=/ and the /api base) must be purged...
      expect(cookieWrites).toContain("X-CSRF-Token=; path=/; max-age=0");
      expect(cookieWrites).toContain(
        "X-CSRF-Token=; path=/meridian/api/; max-age=0"
      );
      // ...but the authoritative app-root cookie must never be touched.
      expect(
        cookieWrites.some((w) => EXACT_APP_ROOT_WRITE.test(w)),
        "app-root cookie (path=/meridian) must never be purged"
      ).toBe(false);
    });

    it("root deployment: purges candidate paths including /", async () => {
      vi.resetModules();
      // vi.doMock registrations survive resetModules, so the subpath test's
      // paths mock would silently apply here; unmock both specifiers so this
      // test exercises the REAL paths module (APP_BASENAME resolves to '' —
      // BASE_URL '/' normalizes to empty — and no exclusion applies).
      vi.doUnmock("@/lib/paths");
      vi.doUnmock("../paths");
      // The '/' candidate is written (the next token fetch re-establishes the
      // cookie if it was legitimate).
      const { purgeStaleCsrfCookies } = await import("./core");

      purgeStaleCsrfCookies();

      expect(cookieWrites).toContain("X-CSRF-Token=; path=/; max-age=0");
    });
  });

  describe("ensureCsrfToken force bypass", () => {
    it("force=true bypasses the in-memory cache AND the cookie jar", async () => {
      mockCookies = "X-CSRF-Token=cookie-token-b";
      const mockFetch = vi.fn();
      mockFetch.mockResolvedValue({
        ok: true,
        json: () => Promise.resolve({ csrf_token: "network-token-a" }),
      });
      vi.stubGlobal("fetch", mockFetch);
      const { ensureCsrfToken, getCsrfToken } = await import("./core");

      // Populate the cache from the cookie (no network).
      const cached = await ensureCsrfToken();
      expect(cached).toBe("cookie-token-b");
      expect(mockFetch).not.toHaveBeenCalled();

      // A cookie value is available, but force must ignore it and refetch.
      mockFetch.mockResolvedValue({
        ok: true,
        json: () => Promise.resolve({ csrf_token: "network-token-c" }),
      });
      const forced = await ensureCsrfToken(true);
      expect(forced).toBe("network-token-c");
      expect(mockFetch).toHaveBeenCalledTimes(1);
      expect(getCsrfToken()).toBe("network-token-c");

      // Non-forced reads still serve the (fresh) cache without a fetch.
      const again = await ensureCsrfToken();
      expect(again).toBe("network-token-c");
      expect(mockFetch).toHaveBeenCalledTimes(1);
    });
  });

  describe("403 + Blob body decode feeding the CSRF retry", () => {
    const originalAdapter = vi.fn();
    let apiClient: (typeof import("./core"))["apiClient"];

    beforeEach(async () => {
      const mod = await import("./core");
      apiClient = mod.apiClient;
      apiClient.defaults.adapter = originalAdapter;
    });

    afterEach(() => {
      apiClient.defaults.adapter = originalAdapter;
    });

    it("a small JSON Blob 403 body is decoded and triggers the CSRF retry", async () => {
      // Seed the cookie jar so the request interceptor resolves its token
      // without a network fetch on the first call.
      mockCookies = "X-CSRF-Token=stale-token";
      const mockFetch = vi.fn().mockResolvedValue({
        ok: true,
        json: () => Promise.resolve({ csrf_token: "fresh-token" }),
      });
      vi.stubGlobal("fetch", mockFetch);

      const blobBody = new Blob(
        [JSON.stringify({ detail: "CSRF token missing or mismatch" })],
        { type: "application/json" }
      );
      let calls = 0;
      apiClient.defaults.adapter = (async (config: unknown) => {
        calls += 1;
        if (calls === 1) {
          // NO x-csrf-error header: detection must come from the decoded
          // blob body — this is what makes the decode observable.
          throw Object.assign(new Error("Request failed with status code 403"), {
            isAxiosError: true,
            config: { method: "post", url: "/widgets", headers: {} },
            response: {
              status: 403,
              statusText: "Forbidden",
              data: blobBody,
              headers: {},
            },
          });
        }
        return {
          data: { ok: true },
          status: 200,
          statusText: "OK",
          headers: {},
          config,
        };
      }) as never;

      const response = await apiClient.post("/widgets");

      expect(response.data).toEqual({ ok: true });
      expect(calls).toBe(2);
      expect(mockFetch).toHaveBeenCalled();
    });

    it("an over-cap Blob 403 body is never decoded and never retried", async () => {
      mockCookies = "X-CSRF-Token=stale-token";
      vi.stubGlobal(
        "fetch",
        vi.fn().mockResolvedValue({
          ok: true,
          json: () => Promise.resolve({ csrf_token: "fresh-token" }),
        })
      );

      // Deliberately VALID JSON above the cap: if the cap were missing, the
      // decode would succeed and the CSRF detection would fire a retry.
      const oversized = JSON.stringify({
        detail: "CSRF token missing or mismatch",
        padding: "x".repeat(65 * 1024),
      });
      const blobBody = new Blob([oversized], { type: "application/json" });
      expect(blobBody.size).toBeGreaterThan(64 * 1024);

      let calls = 0;
      apiClient.defaults.adapter = (async () => {
        calls += 1;
        throw Object.assign(new Error("Request failed with status code 403"), {
          isAxiosError: true,
          config: { method: "post", url: "/widgets", headers: {} },
          response: {
            status: 403,
            statusText: "Forbidden",
            data: blobBody,
            headers: {},
          },
        });
      }) as never;

      const caught = await apiClient
        .post("/widgets")
        .then(
          () => null,
          (error: { response?: { data?: unknown } }) => error
        );

      // No CSRF retry fired (detection never saw a decoded detail) and the
      // error body survived as a Blob (axios may wrap the adapter error, so
      // accept either layer).
      expect(calls).toBe(1);
      expect(caught).not.toBeNull();
      const err = caught as {
        response?: { data?: unknown };
        originalError?: { response?: { data?: unknown } };
      };
      const body = err.response?.data ?? err.originalError?.response?.data;
      expect(body).toBeInstanceOf(Blob);
    });
  });
});
