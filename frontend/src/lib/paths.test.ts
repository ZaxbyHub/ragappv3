import { describe, expect, it } from "vitest";

import { appPath, normalizeBasePath } from "./paths";

describe("normalizeBasePath", () => {
  it.each([
    [undefined, ""],
    [null, ""],
    ["", ""],
    ["/", ""],
    ["knowledgevault", "/knowledgevault"],
    ["/knowledgevault", "/knowledgevault"],
    ["/knowledgevault/", "/knowledgevault"],
    ["/knowledge/vault", "/knowledge/vault"],
  ])("normalizes %s to %s", (input, expected) => {
    expect(normalizeBasePath(input)).toBe(expected);
  });

  it.each([
    " https://example.com/knowledgevault",
    "https://example.com/knowledgevault",
    "//example.com/knowledgevault",
    "/knowledgevault?next=/login",
    "/knowledgevault#hash",
    "/knowledgevault/../admin",
    "/knowledgevault/./admin",
    "/knowledgevault//admin",
    "/knowledgevault;Path=/",
    "/knowledge vault",
    "/knowledge\\vault",
    "/knowledgevault\n",
  ])("rejects unsafe base path %s", (input) => {
    expect(() => normalizeBasePath(input)).toThrow();
  });
});

describe("appPath", () => {
  it("returns root-relative paths when no basename is configured", () => {
    expect(appPath("/login", "")).toBe("/login");
    expect(appPath("login", "/")).toBe("/login");
  });

  it("prepends the app basename for absolute and relative suffixes", () => {
    expect(appPath("/login", "/knowledgevault")).toBe("/knowledgevault/login");
    expect(appPath("login", "/knowledgevault/")).toBe("/knowledgevault/login");
  });

  it("keeps a trailing slash for the app root", () => {
    expect(appPath("/", "/knowledgevault")).toBe("/knowledgevault/");
  });
});
