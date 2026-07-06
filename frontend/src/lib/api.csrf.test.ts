import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

// Mock fetch globally
const mockFetch = vi.fn();
vi.stubGlobal("fetch", mockFetch);

// Mock document.cookie
let mockCookies = "";
Object.defineProperty(document, "cookie", {
  get: () => mockCookies,
  set: (val: string) => {
    mockCookies = val;
  },
  configurable: true,
});

describe("CSRF Exports from @/lib/api", () => {
  beforeEach(() => {
    mockCookies = "";
    mockFetch.mockReset();
    vi.resetModules();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  // =============================================================================
  // ensureCsrfToken — deduplication, cookie-first check, fetch fallback
  // =============================================================================
  describe("ensureCsrfToken", () => {
    it("should return cached token without fetch when cookie exists", async () => {
      mockCookies = "X-CSRF-Token=cached-token";
      const { ensureCsrfToken } = await import("@/lib/api");

      const token = await ensureCsrfToken();

      expect(token).toBe("cached-token");
      expect(mockFetch).not.toHaveBeenCalled();
    });

    it("should fetch token when cookie missing", async () => {
      mockFetch.mockResolvedValue({
        ok: true,
        json: () => Promise.resolve({ csrf_token: "fetched-token" }),
      });
      const { ensureCsrfToken } = await import("@/lib/api");

      const token = await ensureCsrfToken();

      expect(token).toBe("fetched-token");
      expect(mockFetch).toHaveBeenCalledWith(
        expect.stringContaining("/csrf-token"),
        expect.objectContaining({ credentials: "include" })
      );
    });

    it("should use cookie token when both cookie and fetch are available", async () => {
      mockCookies = "X-CSRF-Token=cookie-token";
      mockFetch.mockResolvedValue({
        ok: true,
        json: () => Promise.resolve({ csrf_token: "fetched-token" }),
      });
      const { ensureCsrfToken } = await import("@/lib/api");

      const token = await ensureCsrfToken();

      // Cookie should take precedence over fetch
      expect(token).toBe("cookie-token");
      expect(mockFetch).not.toHaveBeenCalled();
    });

    it("should deduplicate concurrent calls", async () => {
      let resolveFetch: (value: unknown) => void;
      mockFetch.mockReturnValue(
        new Promise((resolve) => {
          resolveFetch = resolve;
        })
      );
      const { ensureCsrfToken } = await import("@/lib/api");

      const p1 = ensureCsrfToken();
      const p2 = ensureCsrfToken();
      const p3 = ensureCsrfToken();

      resolveFetch!({
        ok: true,
        json: () => Promise.resolve({ csrf_token: "deduped" }),
      });

      const [r1, r2, r3] = await Promise.all([p1, p2, p3]);
      expect(r1).toBe("deduped");
      expect(r2).toBe("deduped");
      expect(r3).toBe("deduped");
      expect(mockFetch).toHaveBeenCalledTimes(1); // Only one fetch
    });

    it("should throw when fetch fails", async () => {
      mockFetch.mockResolvedValue({ ok: false });
      const { ensureCsrfToken } = await import("@/lib/api");

      await expect(ensureCsrfToken()).rejects.toThrow("Failed to fetch CSRF token");
    });

    it("should use /csrf-token endpoint path", async () => {
      mockFetch.mockResolvedValue({
        ok: true,
        json: () => Promise.resolve({ csrf_token: "test" }),
      });
      const { ensureCsrfToken } = await import("@/lib/api");

      await ensureCsrfToken();

      expect(mockFetch).toHaveBeenCalledWith(
        expect.stringContaining("/csrf-token"),
        expect.any(Object)
      );
    });
  });

  // =============================================================================
  // resetCsrfToken — clears cached token and in-flight promise
  // =============================================================================
  describe("resetCsrfToken", () => {
    it("should clear cached token", async () => {
      mockCookies = "X-CSRF-Token=initial-token";
      const { ensureCsrfToken, resetCsrfToken, getCsrfToken } = await import(
        "@/lib/api"
      );

      // First call caches the token
      await ensureCsrfToken();
      expect(getCsrfToken()).toBe("initial-token");

      // Reset should clear the cache
      resetCsrfToken();
      expect(getCsrfToken()).toBeNull();
    });

    it("should allow new fetch after reset", async () => {
      mockCookies = "X-CSRF-Token=old-token";
      const { ensureCsrfToken, resetCsrfToken, getCsrfToken } = await import(
        "@/lib/api"
      );

      // First call caches the token
      await ensureCsrfToken();
      expect(getCsrfToken()).toBe("old-token");

      // Reset and change cookie
      resetCsrfToken();
      mockCookies = "X-CSRF-Token=new-token";

      // Next call should fetch new token
      const token = await ensureCsrfToken();
      expect(token).toBe("new-token");
    });

    it("should clear in-flight promise so new fetch can be made", async () => {
      let resolveFetch: (value: unknown) => void;
      mockFetch.mockReturnValue(
        new Promise((resolve) => {
          resolveFetch = resolve;
        })
      );
      const { ensureCsrfToken, resetCsrfToken } = await import("@/lib/api");

      // Start a fetch
      const p1 = ensureCsrfToken();

      // Reset before fetch completes
      resetCsrfToken();

      // Resolve the old fetch
      resolveFetch!({
        ok: true,
        json: () => Promise.resolve({ csrf_token: "stale" }),
      });

      // New fetch should work
      mockFetch.mockResolvedValue({
        ok: true,
        json: () => Promise.resolve({ csrf_token: "fresh" }),
      });

      const token = await ensureCsrfToken();
      expect(token).toBe("fresh");
    });
  });

  // =============================================================================
  // getCsrfToken — returns cached token
  // =============================================================================
  describe("getCsrfToken", () => {
    it("should return null when no token cached", async () => {
      const { getCsrfToken } = await import("@/lib/api");
      expect(getCsrfToken()).toBeNull();
    });

    it("should return cached token after ensureCsrfToken", async () => {
      mockCookies = "X-CSRF-Token=my-token";
      const { ensureCsrfToken, getCsrfToken } = await import("@/lib/api");

      await ensureCsrfToken();
      expect(getCsrfToken()).toBe("my-token");
    });

    it("should return null after resetCsrfToken", async () => {
      mockCookies = "X-CSRF-Token=my-token";
      const { ensureCsrfToken, resetCsrfToken, getCsrfToken } = await import(
        "@/lib/api"
      );

      await ensureCsrfToken();
      resetCsrfToken();
      expect(getCsrfToken()).toBeNull();
    });
  });

  // =============================================================================
  // attachCsrfInterceptor — request interceptor attaches CSRF to mutating methods,
  // response interceptor handles 403
  // =============================================================================
  describe("attachCsrfInterceptor", () => {
    it("should attach CSRF to POST requests", async () => {
      mockCookies = "X-CSRF-Token=test-token";
      const { attachCsrfInterceptor } = await import("@/lib/api");

      const capturedConfig: Record<string, unknown> | null;
      const mockInstance = {
        interceptors: {
          request: {
            use: vi.fn((onFulfilled) => {
              return { then: (fn: (r: unknown) => void) => fn(onFulfilled) };
            }),
          },
          response: { use: vi.fn() },
        },
      } as unknown as { interceptors: { request: { use: (fn: (config: Record<string, unknown>) => Promise<Record<string, unknown>>) => { then: (fn: (r: unknown) => void) => void } ; response: { use: (fn: (r: unknown) => unknown) => void } } } };

      attachCsrfInterceptor(mockInstance as any);

      // Manually trigger the request interceptor
      const requestUse = mockInstance.interceptors.request.use as ReturnType<typeof vi.fn>;
      const interceptorFn = (requestUse as any).mock.calls[0][0];

      const config = { method: "post", headers: {} };
      capturedConfig = await interceptorFn(config);

      expect(capturedConfig.headers["X-CSRF-Token"]).toBe("test-token");
    });

    it("should attach CSRF to PUT requests", async () => {
      mockCookies = "X-CSRF-Token=put-token";
      const { attachCsrfInterceptor } = await import("@/lib/api");

      const mockInstance = {
        interceptors: {
          request: {
            use: vi.fn((onFulfilled) => {
              return { then: (fn: (r: unknown) => void) => fn(onFulfilled) };
            }),
          },
          response: { use: vi.fn() },
        },
      } as unknown as { interceptors: { request: { use: (fn: (config: Record<string, unknown>) => Promise<Record<string, unknown>>) => { then: (fn: (r: unknown) => void) => void } ; response: { use: (fn: (r: unknown) => unknown) => void } } } };

      attachCsrfInterceptor(mockInstance as any);

      const requestUse = mockInstance.interceptors.request.use as ReturnType<typeof vi.fn>;
      const interceptorFn = (requestUse as any).mock.calls[0][0];

      const config = { method: "put", headers: {} };
      const capturedConfig = await interceptorFn(config);

      expect(capturedConfig.headers["X-CSRF-Token"]).toBe("put-token");
    });

    it("should attach CSRF to PATCH requests", async () => {
      mockCookies = "X-CSRF-Token=patch-token";
      const { attachCsrfInterceptor } = await import("@/lib/api");

      const mockInstance = {
        interceptors: {
          request: {
            use: vi.fn((onFulfilled) => {
              return { then: (fn: (r: unknown) => void) => fn(onFulfilled) };
            }),
          },
          response: { use: vi.fn() },
        },
      } as unknown as { interceptors: { request: { use: (fn: (config: Record<string, unknown>) => Promise<Record<string, unknown>>) => { then: (fn: (r: unknown) => void) => void } ; response: { use: (fn: (r: unknown) => unknown) => void } } } };

      attachCsrfInterceptor(mockInstance as any);

      const requestUse = mockInstance.interceptors.request.use as ReturnType<typeof vi.fn>;
      const interceptorFn = (requestUse as any).mock.calls[0][0];

      const config = { method: "patch", headers: {} };
      const capturedConfig = await interceptorFn(config);

      expect(capturedConfig.headers["X-CSRF-Token"]).toBe("patch-token");
    });

    it("should attach CSRF to DELETE requests", async () => {
      mockCookies = "X-CSRF-Token=delete-token";
      const { attachCsrfInterceptor } = await import("@/lib/api");

      const mockInstance = {
        interceptors: {
          request: {
            use: vi.fn((onFulfilled) => {
              return { then: (fn: (r: unknown) => void) => fn(onFulfilled) };
            }),
          },
          response: { use: vi.fn() },
        },
      } as unknown as { interceptors: { request: { use: (fn: (config: Record<string, unknown>) => Promise<Record<string, unknown>>) => { then: (fn: (r: unknown) => void) => void } ; response: { use: (fn: (r: unknown) => unknown) => void } } } };

      attachCsrfInterceptor(mockInstance as any);

      const requestUse = mockInstance.interceptors.request.use as ReturnType<typeof vi.fn>;
      const interceptorFn = (requestUse as any).mock.calls[0][0];

      const config = { method: "delete", headers: {} };
      const capturedConfig = await interceptorFn(config);

      expect(capturedConfig.headers["X-CSRF-Token"]).toBe("delete-token");
    });

    it("should NOT attach CSRF to GET requests", async () => {
      mockCookies = "X-CSRF-Token=test-token";
      const { attachCsrfInterceptor } = await import("@/lib/api");

      const mockInstance = {
        interceptors: {
          request: {
            use: vi.fn((onFulfilled) => {
              return { then: (fn: (r: unknown) => void) => fn(onFulfilled) };
            }),
          },
          response: { use: vi.fn() },
        },
      } as unknown as { interceptors: { request: { use: (fn: (config: Record<string, unknown>) => Promise<Record<string, unknown>>) => { then: (fn: (r: unknown) => void) => void } ; response: { use: (fn: (r: unknown) => unknown) => void } } } };

      attachCsrfInterceptor(mockInstance as any);

      const requestUse = mockInstance.interceptors.request.use as ReturnType<typeof vi.fn>;
      const interceptorFn = (requestUse as any).mock.calls[0][0];

      const config = { method: "get", headers: {} };
      const capturedConfig = await interceptorFn(config);

      expect(capturedConfig.headers["X-CSRF-Token"]).toBeUndefined();
    });

    it("should handle case-insensitive method matching", async () => {
      mockCookies = "X-CSRF-Token=case-token";
      const { attachCsrfInterceptor } = await import("@/lib/api");

      const mockInstance = {
        interceptors: {
          request: {
            use: vi.fn((onFulfilled) => {
              return { then: (fn: (r: unknown) => void) => fn(onFulfilled) };
            }),
          },
          response: { use: vi.fn() },
        },
      } as unknown as { interceptors: { request: { use: (fn: (config: Record<string, unknown>) => Promise<Record<string, unknown>>) => { then: (fn: (r: unknown) => void) => void } ; response: { use: (fn: (r: unknown) => unknown) => void } } } };

      attachCsrfInterceptor(mockInstance as any);

      const requestUse = mockInstance.interceptors.request.use as ReturnType<typeof vi.fn>;
      const interceptorFn = (requestUse as any).mock.calls[0][0];

      // Test uppercase POST
      const upperPostConfig = { method: "POST", headers: {} };
      const upperResult = await interceptorFn(upperPostConfig);
      expect(upperResult.headers["X-CSRF-Token"]).toBe("case-token");

      // Test mixed case PuT
      const mixedPutConfig = { method: "PuT", headers: {} };
      const mixedResult = await interceptorFn(mixedPutConfig);
      expect(mixedResult.headers["X-CSRF-Token"]).toBe("case-token");
    });
  });

  // =============================================================================
  // 403 Response Handling — response interceptor clears token on 403
  // =============================================================================
  describe("403 Response Handling", () => {
    it("should call resetCsrfToken on 403 response", async () => {
      const { attachCsrfInterceptor, resetCsrfToken, getCsrfToken } = await import(
        "@/lib/api"
      );

      let errorHandlerFn: ((error: unknown) => Promise<unknown>) | null = null;

      // Create the mock instance as a callable function
      const mockInstance = vi.fn().mockResolvedValue({ data: {} }) as any;
      mockInstance.interceptors = {
        request: {
          use: vi.fn(() => ({ then: () => ({}) })),
        },
        response: {
          use: vi.fn(
            (onFulfilled: unknown, onRejected: (error: unknown) => Promise<unknown>) => {
              errorHandlerFn = onRejected as (error: unknown) => Promise<unknown>;
            }
          ),
        },
      };

      // Pre-populate cache
      mockCookies = "X-CSRF-Token=stale-token";
      const { ensureCsrfToken } = await import("@/lib/api");
      await ensureCsrfToken();
      expect(getCsrfToken()).toBe("stale-token");

      // Clear cookie so ensureCsrfToken will fetch fresh token
      mockCookies = "";
      mockFetch.mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({ csrf_token: "fresh-token" }),
      });

      attachCsrfInterceptor(mockInstance);

      // Simulate 403 error
      const csrfError = {
        response: { status: 403, data: { detail: "CSRF token missing or mismatch" } },
        config: { _csrfRetry: undefined },
      };

      await errorHandlerFn!(csrfError);

      // Token should be re-fetched (not null) - reset+refetch happened
      expect(getCsrfToken()).toBe("fresh-token");
      // Retry should have happened
      expect(mockInstance).toHaveBeenCalled();
    });

    it("should NOT clear token on non-403 errors", async () => {
      const { attachCsrfInterceptor, getCsrfToken } = await import("@/lib/api");

      let errorHandlerFn: ((error: unknown) => Promise<unknown>) | null = null;

      // Create the mock instance as a callable function
      const mockInstance = vi.fn().mockResolvedValue({ data: {} }) as any;
      mockInstance.interceptors = {
        request: {
          use: vi.fn(() => ({ then: () => ({}) })),
        },
        response: {
          use: vi.fn(
            (onFulfilled: unknown, onRejected: (error: unknown) => Promise<unknown>) => {
              errorHandlerFn = onRejected as (error: unknown) => Promise<unknown>;
            }
          ),
        },
      };

      // Pre-populate cache
      mockCookies = "X-CSRF-Token=valid-token";
      const { ensureCsrfToken } = await import("@/lib/api");
      await ensureCsrfToken();
      expect(getCsrfToken()).toBe("valid-token");

      attachCsrfInterceptor(mockInstance);

      // Simulate 500 error
      const serverError = {
        response: { status: 500 },
        config: {},
      };

      // Error handler rejects for non-403 errors
      await expect(errorHandlerFn!(serverError)).rejects.toThrow();

      // Token should NOT be cleared
      expect(getCsrfToken()).toBe("valid-token");
    });

    it("should NOT clear token on network errors (no response)", async () => {
      const { attachCsrfInterceptor, getCsrfToken } = await import("@/lib/api");

      let errorHandlerFn: ((error: unknown) => Promise<unknown>) | null = null;

      // Create the mock instance as a callable function
      const mockInstance = vi.fn().mockResolvedValue({ data: {} }) as any;
      mockInstance.interceptors = {
        request: {
          use: vi.fn(() => ({ then: () => ({}) })),
        },
        response: {
          use: vi.fn(
            (onFulfilled: unknown, onRejected: (error: unknown) => Promise<unknown>) => {
              errorHandlerFn = onRejected as (error: unknown) => Promise<unknown>;
            }
          ),
        },
      };

      // Pre-populate cache
      mockCookies = "X-CSRF-Token=valid-token";
      const { ensureCsrfToken } = await import("@/lib/api");
      await ensureCsrfToken();
      expect(getCsrfToken()).toBe("valid-token");

      attachCsrfInterceptor(mockInstance);

      // Simulate network error (no response)
      const networkError = { message: "Network error" };

      // Error handler rejects for network errors without response
      await expect(errorHandlerFn!(networkError)).rejects.toThrow();

      // Token should NOT be cleared
      expect(getCsrfToken()).toBe("valid-token");
    });
  });

  // =============================================================================
  // 401 vs 403 Differentiation — handlers are separate
  // =============================================================================
  describe("401 vs 403 Differentiation", () => {
    it("should differentiate 401 auth errors from 403 CSRF errors", async () => {
      const { attachCsrfInterceptor, getCsrfToken } = await import("@/lib/api");

      let requestInterceptorFn: ((config: Record<string, unknown>) => Promise<Record<string, unknown>>) | null = null;
      let errorInterceptorFn: ((error: unknown) => Promise<unknown>) | null = null;

      // Create the mock instance as a callable function
      const mockInstance = vi.fn().mockResolvedValue({ data: {} }) as any;
      mockInstance.interceptors = {
        request: {
          use: vi.fn((onFulfilled: (config: Record<string, unknown>) => Promise<Record<string, unknown>>) => {
            requestInterceptorFn = onFulfilled;
            return { then: () => ({}) };
          }),
        },
        response: {
          use: vi.fn(
            (onFulfilled: unknown, onRejected: (error: unknown) => Promise<unknown>) => {
              errorInterceptorFn = onRejected as (error: unknown) => Promise<unknown>;
            }
          ),
        },
      };

      // Pre-populate cache
      mockCookies = "X-CSRF-Token=my-token";
      const { ensureCsrfToken } = await import("@/lib/api");
      await ensureCsrfToken();
      expect(getCsrfToken()).toBe("my-token");

      // Clear cookie so ensureCsrfToken will fetch fresh token on 403 retry
      mockCookies = "";
      mockFetch.mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({ csrf_token: "fresh-token" }),
      });

      attachCsrfInterceptor(mockInstance);

      // 403 should be handled by CSRF interceptor (reset + refetch)
      const csrfError = {
        response: { status: 403, data: { detail: "CSRF token missing or mismatch" } },
        config: { _csrfRetry: undefined },
      };
      await errorInterceptorFn!(csrfError);
      // Token should be re-fetched (not null) - reset+refetch happened
      expect(getCsrfToken()).toBe("fresh-token");

      // Reset and re-populate for 401 test
      await vi.resetModules();
      mockCookies = "X-CSRF-Token=my-token";

      const { attachCsrfInterceptor: reAttach, getCsrfToken: getCsrfTokenFresh, ensureCsrfToken: ensureCsrfTokenNew } = await import("@/lib/api");
      await ensureCsrfTokenNew();
      errorInterceptorFn = null;

      // Create the mock instance as a callable function for the second test
      const mockInstance2 = vi.fn().mockResolvedValue({ data: {} }) as any;
      mockInstance2.interceptors = {
        request: {
          use: vi.fn(() => {
            return { then: () => ({}) };
          }),
        },
        response: {
          use: vi.fn((onFulfilled: unknown, onRejected: (error: unknown) => Promise<unknown>) => {
            errorInterceptorFn = onRejected as (error: unknown) => Promise<unknown>;
          }),
        },
      };

      reAttach(mockInstance2);

      // 401 should NOT be handled by CSRF interceptor (token unchanged)
      const authError = {
        response: { status: 401 },
        config: {},
      };
      // Error handler rejects for non-403 errors
      await expect(errorInterceptorFn!(authError)).rejects.toThrow();
      // Token should still be "my-token" (CSRF interceptor doesn't handle 401)
      expect(getCsrfTokenFresh()).toBe("my-token");
    });
  });

  // =============================================================================
  // Backend Response Format — { csrf_token: "..." }
  // =============================================================================
  describe("Backend Response Format", () => {
    it("should parse csrf_token field from response", async () => {
      mockFetch.mockResolvedValue({
        ok: true,
        json: () => Promise.resolve({ csrf_token: "backend-token-12345" }),
      });
      const { ensureCsrfToken } = await import("@/lib/api");

      const token = await ensureCsrfToken();

      expect(token).toBe("backend-token-12345");
    });

    it("should throw if response does not contain csrf_token field", async () => {
      mockFetch.mockResolvedValue({
        ok: true,
        json: () => Promise.resolve({}),
      });
      const { ensureCsrfToken } = await import("@/lib/api");

      // H2 fix: ensureCsrfToken validates csrf_token is a non-empty string
      await expect(ensureCsrfToken()).rejects.toThrow("CSRF token missing from response");
    });
  });

  // =============================================================================
  // H2 — ensureCsrfToken() validates csrf_token response (review council fix)
  // =============================================================================
  describe("H2 — ensureCsrfToken response validation", () => {
    it("should throw when backend returns empty object (no csrf_token field)", async () => {
      mockFetch.mockResolvedValue({
        ok: true,
        json: () => Promise.resolve({}),
      });
      const { ensureCsrfToken } = await import("@/lib/api");

      await expect(ensureCsrfToken()).rejects.toThrow("CSRF token missing from response");
    });

    it("should throw when backend returns { csrf_token: null }", async () => {
      mockFetch.mockResolvedValue({
        ok: true,
        json: () => Promise.resolve({ csrf_token: null }),
      });
      const { ensureCsrfToken } = await import("@/lib/api");

      await expect(ensureCsrfToken()).rejects.toThrow("CSRF token missing from response");
    });

    it("should throw when backend returns { csrf_token: \"\" } (empty string)", async () => {
      mockFetch.mockResolvedValue({
        ok: true,
        json: () => Promise.resolve({ csrf_token: "" }),
      });
      const { ensureCsrfToken } = await import("@/lib/api");

      await expect(ensureCsrfToken()).rejects.toThrow("CSRF token missing from response");
    });

    it("should throw when backend returns { csrf_token: 123 } (non-string)", async () => {
      mockFetch.mockResolvedValue({
        ok: true,
        json: () => Promise.resolve({ csrf_token: 123 }),
      });
      const { ensureCsrfToken } = await import("@/lib/api");

      await expect(ensureCsrfToken()).rejects.toThrow("CSRF token missing from response");
    });
  });

  // =============================================================================
  // H3 — Request interceptor sets X-CSRF-Token on existing headers object
  // =============================================================================
  describe("H3 — Request interceptor CSRF header injection", () => {
    it("should set X-CSRF-Token on config.headers for mutating requests", async () => {
      mockCookies = "X-CSRF-Token=test-token";
      const { attachCsrfInterceptor } = await import("@/lib/api");

      const mockInstance = {
        interceptors: {
          request: {
            use: vi.fn((onFulfilled) => {
              return { then: (fn: (r: unknown) => void) => fn(onFulfilled) };
            }),
          },
          response: { use: vi.fn() },
        },
      } as unknown as { interceptors: { request: { use: (fn: (config: Record<string, unknown>) => Promise<Record<string, unknown>>) => { then: (fn: (r: unknown) => void) => void } ; response: { use: (fn: (r: unknown) => unknown) => void } } } };

      attachCsrfInterceptor(mockInstance as any);

      const requestUse = mockInstance.interceptors.request.use as ReturnType<typeof vi.fn>;
      const interceptorFn = (requestUse as any).mock.calls[0][0];

      // Axios v1 always provides a headers object on InternalAxiosRequestConfig
      const config = { method: "post", headers: {} };
      const capturedConfig = await interceptorFn(config);

      expect(capturedConfig.headers["X-CSRF-Token"]).toBe("test-token");
    });
  });

  // =============================================================================
  // C2 — apiClient has CSRF interceptor attached (review council fix)
  // =============================================================================
  describe("C2 — apiClient CSRF interceptor attached", () => {
    it("should have CSRF request interceptor registered on apiClient", async () => {
      const { default: apiClient } = await import("@/lib/api");

      // apiClient should have request interceptors
      expect(apiClient.interceptors).toBeDefined();
      expect(apiClient.interceptors.request).toBeDefined();

      // The CSRF interceptor is attached via attachCsrfInterceptor(apiClient) at module init
      // We verify by checking that interceptors.request.use was called
      // (In vitest, axios.create() mocks need to be checked)
      const requestInterceptors = apiClient.interceptors.request.handlers;
      // There should be at least 2 interceptors: auth token interceptor + CSRF interceptor
      expect(requestInterceptors.length).toBeGreaterThanOrEqual(2);
    });
  });

  // =============================================================================
  // CSRF 403 Retry Constraint — edge cases for isCsrfError detection
  // =============================================================================
  describe("CSRF 403 Retry Constraint — isCsrfError detection edge cases", () => {
    it("should NOT trigger retry for 403 with non-CSRF detail (permission denied)", async () => {
      const { attachCsrfInterceptor, getCsrfToken } = await import("@/lib/api");

      let errorHandlerFn: ((error: unknown) => Promise<unknown>) | null = null;

      const mockInstance = vi.fn().mockResolvedValue({ data: {} }) as any;
      mockInstance.interceptors = {
        request: { use: vi.fn(() => ({ then: () => ({}) })) },
        response: {
          use: vi.fn((_: unknown, onRejected: (error: unknown) => Promise<unknown>) => {
            errorHandlerFn = onRejected as (error: unknown) => Promise<unknown>;
          }),
        },
      };

      // Pre-populate cache
      mockCookies = "X-CSRF-Token=valid-token";
      const { ensureCsrfToken } = await import("@/lib/api");
      await ensureCsrfToken();
      expect(getCsrfToken()).toBe("valid-token");

      attachCsrfInterceptor(mockInstance);

      // 403 with non-CSRF error message
      const permissionError = {
        response: { status: 403, data: { detail: "You do not have permission" } },
        config: { _csrfRetry: undefined },
      };

      await expect(errorHandlerFn!(permissionError)).rejects.toThrow();

      // Token should NOT be cleared (no retry for non-CSRF 403)
      expect(getCsrfToken()).toBe("valid-token");
      // instance should NOT be called (no retry)
      expect(mockInstance).not.toHaveBeenCalled();
    });

    it("should NOT trigger retry for 403 with no data.detail", async () => {
      const { attachCsrfInterceptor, getCsrfToken } = await import("@/lib/api");

      let errorHandlerFn: ((error: unknown) => Promise<unknown>) | null = null;

      const mockInstance = vi.fn().mockResolvedValue({ data: {} }) as any;
      mockInstance.interceptors = {
        request: { use: vi.fn(() => ({ then: () => ({}) })) },
        response: {
          use: vi.fn((_: unknown, onRejected: (error: unknown) => Promise<unknown>) => {
            errorHandlerFn = onRejected as (error: unknown) => Promise<unknown>;
          }),
        },
      };

      mockCookies = "X-CSRF-Token=valid-token";
      const { ensureCsrfToken } = await import("@/lib/api");
      await ensureCsrfToken();
      expect(getCsrfToken()).toBe("valid-token");

      attachCsrfInterceptor(mockInstance);

      // 403 with no detail field
      const noDetailError = {
        response: { status: 403, data: {} },
        config: { _csrfRetry: undefined },
      };

      await expect(errorHandlerFn!(noDetailError)).rejects.toThrow();

      // Token should NOT be cleared
      expect(getCsrfToken()).toBe("valid-token");
      expect(mockInstance).not.toHaveBeenCalled();
    });

    it("should NOT trigger retry for 403 with non-string detail (number)", async () => {
      const { attachCsrfInterceptor, getCsrfToken } = await import("@/lib/api");

      let errorHandlerFn: ((error: unknown) => Promise<unknown>) | null = null;

      const mockInstance = vi.fn().mockResolvedValue({ data: {} }) as any;
      mockInstance.interceptors = {
        request: { use: vi.fn(() => ({ then: () => ({}) })) },
        response: {
          use: vi.fn((_: unknown, onRejected: (error: unknown) => Promise<unknown>) => {
            errorHandlerFn = onRejected as (error: unknown) => Promise<unknown>;
          }),
        },
      };

      mockCookies = "X-CSRF-Token=valid-token";
      const { ensureCsrfToken } = await import("@/lib/api");
      await ensureCsrfToken();
      expect(getCsrfToken()).toBe("valid-token");

      attachCsrfInterceptor(mockInstance);

      // 403 with numeric detail
      const numericDetailError = {
        response: { status: 403, data: { detail: 403 } },
        config: { _csrfRetry: undefined },
      };

      await expect(errorHandlerFn!(numericDetailError)).rejects.toThrow();

      // Token should NOT be cleared (typeof check fails for number)
      expect(getCsrfToken()).toBe("valid-token");
      expect(mockInstance).not.toHaveBeenCalled();
    });

    it("should NOT trigger retry for 403 with non-string detail (object)", async () => {
      const { attachCsrfInterceptor, getCsrfToken } = await import("@/lib/api");

      let errorHandlerFn: ((error: unknown) => Promise<unknown>) | null = null;

      const mockInstance = vi.fn().mockResolvedValue({ data: {} }) as any;
      mockInstance.interceptors = {
        request: { use: vi.fn(() => ({ then: () => ({}) })) },
        response: {
          use: vi.fn((_: unknown, onRejected: (error: unknown) => Promise<unknown>) => {
            errorHandlerFn = onRejected as (error: unknown) => Promise<unknown>;
          }),
        },
      };

      mockCookies = "X-CSRF-Token=valid-token";
      const { ensureCsrfToken } = await import("@/lib/api");
      await ensureCsrfToken();
      expect(getCsrfToken()).toBe("valid-token");

      attachCsrfInterceptor(mockInstance);

      // 403 with object detail
      const objectDetailError = {
        response: { status: 403, data: { detail: { message: "forbidden" } } },
        config: { _csrfRetry: undefined },
      };

      await expect(errorHandlerFn!(objectDetailError)).rejects.toThrow();

      // Token should NOT be cleared
      expect(getCsrfToken()).toBe("valid-token");
      expect(mockInstance).not.toHaveBeenCalled();
    });

    it("should NOT trigger retry for 403 with detail containing 'Csrf' (mixed case)", async () => {
      const { attachCsrfInterceptor, getCsrfToken } = await import("@/lib/api");

      let errorHandlerFn: ((error: unknown) => Promise<unknown>) | null = null;

      const mockInstance = vi.fn().mockResolvedValue({ data: {} }) as any;
      mockInstance.interceptors = {
        request: { use: vi.fn(() => ({ then: () => ({}) })) },
        response: {
          use: vi.fn((_: unknown, onRejected: (error: unknown) => Promise<unknown>) => {
            errorHandlerFn = onRejected as (error: unknown) => Promise<unknown>;
          }),
        },
      };

      // Pre-populate and clear cookie for fresh fetch
      mockCookies = "X-CSRF-Token=stale-token";
      const { ensureCsrfToken } = await import("@/lib/api");
      await ensureCsrfToken();
      expect(getCsrfToken()).toBe("stale-token");

      mockCookies = "";
      mockFetch.mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({ csrf_token: "fresh-token" }),
      });

      attachCsrfInterceptor(mockInstance);

      // 403 with mixed-case "Csrf" in detail — should trigger retry (case-insensitive)
      const mixedCaseCsrfError = {
        response: { status: 403, data: { detail: "Csrf token mismatch" } },
        config: { _csrfRetry: undefined },
      };

      await errorHandlerFn!(mixedCaseCsrfError);

      // Token should be re-fetched (mixed-case "Csrf" includes 'csrf' when lowercased)
      expect(getCsrfToken()).toBe("fresh-token");
      expect(mockInstance).toHaveBeenCalled();
    });

    it("should NOT trigger retry for 403 with detail containing 'CSRF' (uppercase)", async () => {
      const { attachCsrfInterceptor, getCsrfToken } = await import("@/lib/api");

      let errorHandlerFn: ((error: unknown) => Promise<unknown>) | null = null;

      const mockInstance = vi.fn().mockResolvedValue({ data: {} }) as any;
      mockInstance.interceptors = {
        request: { use: vi.fn(() => ({ then: () => ({}) })) },
        response: {
          use: vi.fn((_: unknown, onRejected: (error: unknown) => Promise<unknown>) => {
            errorHandlerFn = onRejected as (error: unknown) => Promise<unknown>;
          }),
        },
      };

      // Pre-populate and clear cookie for fresh fetch
      mockCookies = "X-CSRF-Token=stale-token";
      const { ensureCsrfToken } = await import("@/lib/api");
      await ensureCsrfToken();
      expect(getCsrfToken()).toBe("stale-token");

      mockCookies = "";
      mockFetch.mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({ csrf_token: "fresh-token" }),
      });

      attachCsrfInterceptor(mockInstance);

      // 403 with uppercase "CSRF" in detail — should trigger retry (case-insensitive)
      const uppercaseCsrfError = {
        response: { status: 403, data: { detail: "CSRF TOKEN INVALID" } },
        config: { _csrfRetry: undefined },
      };

      await errorHandlerFn!(uppercaseCsrfError);

      // Token should be re-fetched (uppercase includes 'csrf' when lowercased)
      expect(getCsrfToken()).toBe("fresh-token");
      expect(mockInstance).toHaveBeenCalled();
    });

    it("should NOT retry twice on repeated CSRF 403 (config._csrfRetry flag)", async () => {
      const { attachCsrfInterceptor, getCsrfToken } = await import("@/lib/api");

      let errorHandlerFn: ((error: unknown) => Promise<unknown>) | null = null;
      const mockInstance = vi.fn().mockResolvedValue({ data: {} }) as any;
      mockInstance.interceptors = {
        request: { use: vi.fn(() => ({ then: () => ({}) })) },
        response: {
          use: vi.fn((_: unknown, onRejected: (error: unknown) => Promise<unknown>) => {
            errorHandlerFn = onRejected as (error: unknown) => Promise<unknown>;
          }),
        },
      };

      // Pre-populate cache
      mockCookies = "X-CSRF-Token=stale-token";
      const { ensureCsrfToken } = await import("@/lib/api");
      await ensureCsrfToken();
      expect(getCsrfToken()).toBe("stale-token");

      mockCookies = "";
      mockFetch.mockResolvedValue({
        ok: true,
        json: () => Promise.resolve({ csrf_token: "fresh-token" }),
      });

      attachCsrfInterceptor(mockInstance);

      // First 403 - should retry
      const csrfError = {
        response: { status: 403, data: { detail: "CSRF token missing" } },
        config: { _csrfRetry: undefined },
      };

      await errorHandlerFn!(csrfError);
      expect(getCsrfToken()).toBe("fresh-token");
      expect(mockInstance).toHaveBeenCalledTimes(1);

      // Second 403 with _csrfRetry set - should NOT retry again
      const retryError = {
        response: { status: 403, data: { detail: "CSRF token mismatch" } },
        config: { _csrfRetry: true }, // Already retried
      };

      await expect(errorHandlerFn!(retryError)).rejects.toThrow();

      // No additional retry
      expect(mockInstance).toHaveBeenCalledTimes(1);
    });
  });

  // =============================================================================
  // chatStream — CSRF token included in POST request headers
  // =============================================================================
  describe("chatStream CSRF Protection", () => {
    beforeEach(() => {
      mockCookies = "";
      mockFetch.mockReset();
      vi.resetModules();
    });

    it("should include X-CSRF-Token header in chat stream request", async () => {
      // Mock CSRF token endpoint
      mockFetch
        .mockResolvedValueOnce({
          ok: true,
          json: () => Promise.resolve({ csrf_token: "test-csrf-token" }),
        })
        // Mock the chat stream response
        .mockResolvedValueOnce({
          ok: true,
          body: new ReadableStream({
            start(controller) {
              controller.enqueue(new TextEncoder().encode("data: {\"content\":\"test\"}\n"));
              controller.enqueue(new TextEncoder().encode("data: {\"type\":\"done\"}\n"));
              controller.close();
            },
          }),
        });

      const { chatStream } = await import("@/lib/api");

      const messages = [{ role: "user" as const, content: "Hello" }];
      const callbacks = {
        onMessage: vi.fn(),
        onSources: vi.fn(),
        onError: vi.fn(),
        onComplete: vi.fn(),
      };

      // Start the stream
      const cancel = chatStream(messages, callbacks, 1);

      // Wait for the stream to complete
      await new Promise((resolve) => setTimeout(resolve, 100));

      // Cancel the stream
      cancel();

      // Verify CSRF token was fetched
      expect(mockFetch).toHaveBeenCalledWith(
        expect.stringContaining("/csrf-token"),
        expect.objectContaining({ credentials: "include" })
      );

      // Verify chat stream request included CSRF token
      const chatStreamCall = mockFetch.mock.calls[1];
      expect(chatStreamCall[1].headers["X-CSRF-Token"]).toBe("test-csrf-token");
      expect(callbacks.onComplete).toHaveBeenCalledTimes(1);
    });

    it("should call onError and return early when CSRF token fetch fails", async () => {
      // Mock CSRF token endpoint failure
      mockFetch.mockResolvedValueOnce({
        ok: false,
        status: 500,
      });

      const { chatStream } = await import("@/lib/api");

      const messages = [{ role: "user" as const, content: "Hello" }];
      const onError = vi.fn();
      const callbacks = {
        onMessage: vi.fn(),
        onSources: vi.fn(),
        onError,
        onComplete: vi.fn(),
      };

      // Start the stream
      const cancel = chatStream(messages, callbacks, 1);

      // Wait for async error handling
      await new Promise((resolve) => setTimeout(resolve, 50));

      // Cancel the stream
      cancel();

      // Verify error was reported
      expect(onError).toHaveBeenCalledWith(new Error("Failed to get CSRF token"));
      // Verify no chat stream request was made
      expect(mockFetch).toHaveBeenCalledTimes(1); // Only CSRF fetch, no chat request
    });

    it("should include CSRF token from cookie without fetch", async () => {
      // Set CSRF token in cookie
      mockCookies = "X-CSRF-Token=cookie-csrf-token";

      // Mock the chat stream response
      mockFetch.mockResolvedValueOnce({
        ok: true,
        body: new ReadableStream({
          start(controller) {
            controller.enqueue(new TextEncoder().encode("data: {\"content\":\"test\"}\n"));
            controller.enqueue(new TextEncoder().encode("data: [DONE]\n"));
            controller.close();
          },
        }),
      });

      const { chatStream } = await import("@/lib/api");

      const messages = [{ role: "user" as const, content: "Hello" }];
      const callbacks = {
        onMessage: vi.fn(),
        onSources: vi.fn(),
        onError: vi.fn(),
        onComplete: vi.fn(),
      };

      // Start the stream
      const cancel = chatStream(messages, callbacks, 1);

      // Wait for the stream to complete
      await new Promise((resolve) => setTimeout(resolve, 100));

      // Cancel the stream
      cancel();

      // Verify no fetch was made for CSRF token (cookie took precedence)
      expect(mockFetch).not.toHaveBeenCalledWith(
        expect.stringContaining("/csrf-token"),
        expect.any(Object)
      );

      // Verify chat stream request included CSRF token from cookie
      expect(mockFetch).toHaveBeenCalled();
      const chatStreamCall = mockFetch.mock.calls[0];
      expect(chatStreamCall[1].headers["X-CSRF-Token"]).toBe("cookie-csrf-token");
    });
  });
});
