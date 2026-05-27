import { describe, expect, it } from "vitest";

import {
  createApiProxy,
  createApiProxyPath,
  normalizeBasePath,
  normalizeViteBase,
  rewriteApiProxyPath,
} from "../vite.paths";

describe("Vite subpath configuration helpers", () => {
  it("normalizes router/proxy base paths without a trailing slash", () => {
    expect(normalizeBasePath("")).toBe("");
    expect(normalizeBasePath("/")).toBe("");
    expect(normalizeBasePath("knowledgevault")).toBe("/knowledgevault");
    expect(normalizeBasePath("/knowledgevault/")).toBe("/knowledgevault");
  });

  it("normalizes Vite base paths with a trailing slash", () => {
    expect(normalizeViteBase("")).toBe("/");
    expect(normalizeViteBase("/")).toBe("/");
    expect(normalizeViteBase("/knowledgevault")).toBe("/knowledgevault/");
  });

  it("creates root and prefixed API proxy entries", () => {
    expect(createApiProxyPath("")).toBe("/api");
    expect(createApiProxyPath("/knowledgevault")).toBe("/knowledgevault/api");
    expect(Object.keys(createApiProxy(""))).toEqual(["/api"]);
    // When a prefix is set, only the prefixed proxy is created (no bare /api).
    // This enforces dev/prod parity: bare /api requests fail in dev the same
    // way they fail in production behind the prefix-stripping proxy.
    expect(Object.keys(createApiProxy("/knowledgevault"))).toEqual([
      "/knowledgevault/api",
    ]);
  });

  it("rewrites prefixed dev API requests back to backend internal /api routes", () => {
    expect(rewriteApiProxyPath("/knowledgevault/api", "/knowledgevault")).toBe("/api");
    expect(rewriteApiProxyPath("/knowledgevault/api/auth/login", "/knowledgevault")).toBe(
      "/api/auth/login"
    );
    expect(rewriteApiProxyPath("/api/auth/login", "")).toBe("/api/auth/login");
  });

  it.each([
    "https://example.com/knowledgevault",
    "//example.com/knowledgevault",
    "/knowledgevault?x=1",
    "/knowledgevault#hash",
    "/knowledgevault/../admin",
    "/knowledgevault//admin",
    "/knowledgevault;Path=/",
  ])("rejects unsafe base path %s", (value) => {
    expect(() => normalizeViteBase(value)).toThrow();
  });
});
