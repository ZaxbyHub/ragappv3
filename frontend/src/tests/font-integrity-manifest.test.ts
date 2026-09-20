import { describe, expect, it } from "vitest";
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";

import { assertFontIntegrity } from "./helpers/font-integrity";

/**
 * Vendored font integrity manifest contract (issue #640, AC1/AC3).
 *
 * Every woff2 under the real frontend/src/assets/fonts/ must be pinned by
 * SHA256SUMS (written by frontend/scripts/fetch-fonts.mjs) with a matching
 * sha256, the wOF2 magic, and a size above the truncation floor — and the
 * manifest must cover every woff2 in the directory. This replaces bare
 * existence checks as the provenance gate for the vendored binaries.
 */

const fontsDir = join(__dirname, "..", "assets", "fonts");
const manifestPath = join(fontsDir, "SHA256SUMS");

describe("font integrity manifest over the vendored fonts (AC1/AC3)", () => {
  it("every vendored woff2 matches SHA256SUMS (magic, size floor, hash, full coverage)", () => {
    const manifest = readFileSync(manifestPath, "utf-8");
    const result = assertFontIntegrity(fontsDir, manifest);

    const woff2OnDisk = readdirSync(fontsDir).filter((name) =>
      name.toLowerCase().endsWith(".woff2"),
    );
    // The manifest must cover the whole directory — not a subset that would
    // let an uncovered font slip past verification.
    expect(result.verifiedCount, "verified count must equal the woff2 files on disk").toBe(
      woff2OnDisk.length,
    );
    expect(result.verifiedCount, "expected the vendored 37-font set").toBe(37);
  });
});
