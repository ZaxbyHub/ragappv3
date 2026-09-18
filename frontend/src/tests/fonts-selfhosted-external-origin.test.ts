import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

/**
 * Self-hosting font contract (issue #572, AC1).
 *
 * The SPA shell must not reference the Google Fonts CDN anywhere in
 * frontend/index.html - no preconnect links, no stylesheet link, no
 * comments hinting at it. The product contract (README "Self-host
 * everything on your own infrastructure") forbids a per-page-load
 * dependency on fonts.googleapis.com / fonts.gstatic.com; fonts are
 * vendored locally instead (see fonts-selfhosted-assets.test.ts).
 *
 * Source-level contract test: fails at HEAD (CDN links present),
 * passes once index.html is free of both origins.
 */

const indexPath = resolve(__dirname, "..", "..", "index.html");

const FORBIDDEN_ORIGINS = [
  "fonts.googleapis.com",
  "fonts.gstatic.com",
] as const;

describe("frontend/index.html external font origins (AC1)", () => {
  it("contains no reference to fonts.googleapis.com or fonts.gstatic.com", () => {
    const html = readFileSync(indexPath, "utf-8");

    const offenders = FORBIDDEN_ORIGINS.filter((origin) =>
      html.toLowerCase().includes(origin),
    );

    expect(
      offenders,
      `index.html must not reference external font CDNs (found: ${offenders.join(", ")}). ` +
        "Self-hosted fonts must be vendored under src/assets/fonts with " +
        "@font-face rules in src/index.css instead of CDN links.",
    ).toEqual([]);
  });
});
