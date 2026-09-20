import { describe, expect, it } from "vitest";
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";

import { scanForExternalOrigins } from "./helpers/external-origin-scan";

/**
 * External-origin scan over the real CSS surface (issue #640, AC6).
 *
 * The #572 test covered frontend/index.html only; a CDN @import inside any
 * bundled stylesheet would have been invisible to the contract. Here the
 * shared scanner is (1) proven to flag a remote @import fixture — the exact
 * corruption class the HTML-only scan missed — and (2) applied to the real
 * frontend/src/index.css plus every .css file under frontend/src, which
 * must be clean.
 */

const srcDir = join(__dirname, "..");

/** Recursively collect every .css file path under `dir`, sorted. */
function collectCssFiles(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) out.push(...collectCssFiles(full));
    else if (entry.name.toLowerCase().endsWith(".css")) out.push(full);
  }
  return out.sort();
}

describe("external origin scan across CSS sources (AC6)", () => {
  it("flags a CSS @import of the Google Fonts CDN", () => {
    const fixture = [
      "/* theme imports */",
      '@import url("https://fonts.googleapis.com/css2?family=Inter:wght@400;700&display=swap");',
      "body { font-family: Inter, sans-serif; }",
    ].join("\n");

    const offenders = scanForExternalOrigins([{ path: "fixture/theme.css", content: fixture }]);

    expect(offenders).toEqual(["fixture/theme.css"]);
  });

  it("flags a fonts.gstatic.com url() even without an @import", () => {
    const fixture =
      "@font-face { font-family: X; src: url(https://fonts.gstatic.com/s/x.woff2) format('woff2'); }";

    const offenders = scanForExternalOrigins([{ path: "fixture/font.css", content: fixture }]);

    expect(offenders).toEqual(["fixture/font.css"]);
  });

  it("the real frontend/src CSS surface references no forbidden origin", () => {
    const cssPaths = collectCssFiles(srcDir);
    // Anti-vacuity: the scan must actually cover the tree's stylesheets
    // (index.css at minimum), or a clean result would prove nothing.
    expect(cssPaths.length, "expected at least one .css file under frontend/src").toBeGreaterThan(0);
    expect(cssPaths).toContain(join(srcDir, "index.css"));

    const offenders = scanForExternalOrigins(
      cssPaths.map((path) => ({ path, content: readFileSync(path, "utf-8") })),
    );

    expect(
      offenders,
      "no .css file under frontend/src may reference fonts.googleapis.com or fonts.gstatic.com — " +
        "fonts are vendored under src/assets/fonts (see fonts-selfhosted-assets.test.ts)",
    ).toEqual([]);
  });
});
