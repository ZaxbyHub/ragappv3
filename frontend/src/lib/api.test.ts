import { afterEach, describe, expect, it, vi } from "vitest";

describe("API path configuration", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.resetModules();
  });

  it("defaults API calls and login redirects to root deployment paths", async () => {
    vi.stubEnv("VITE_API_URL", "");
    vi.stubEnv("VITE_APP_BASENAME", "");
    vi.resetModules();

    const { API_BASE_URL, loginRedirectPath } = await import("./api");

    expect(API_BASE_URL).toBe("/api");
    expect(loginRedirectPath()).toBe("/login");
  });

  it("uses public-prefixed API and login paths when configured", async () => {
    vi.stubEnv("VITE_API_URL", "/knowledgevault/api");
    vi.stubEnv("VITE_APP_BASENAME", "/knowledgevault");
    vi.resetModules();

    const { API_BASE_URL, loginRedirectPath } = await import("./api");

    expect(API_BASE_URL).toBe("/knowledgevault/api");
    expect(loginRedirectPath()).toBe("/knowledgevault/login");
  });

  it("derives meridian API path from basename when VITE_API_URL is empty", async () => {
    vi.stubEnv("VITE_API_URL", "");
    vi.stubEnv("VITE_APP_BASENAME", "/meridian");
    vi.resetModules();

    const { API_BASE_URL, loginRedirectPath } = await import("./api");

    expect(API_BASE_URL).toBe("/meridian/api");
    expect(loginRedirectPath()).toBe("/meridian/login");
  });
});
