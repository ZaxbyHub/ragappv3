import { describe, it, expect } from "vitest";
import { existsSync, readFileSync } from "node:fs";
import { resolve, sep } from "node:path";

/**
 * Self-hosted font assets contract (issue #572, AC2).
 *
 * Every font family the app's CSS actually references (Spline Sans,
 * Inter, Commissioner, Electrolize) must be declared via local
 * @font-face rules in frontend/src/index.css with font-display: swap,
 * backed by vendored .woff2 files under frontend/src/assets/fonts/
 * and an OFL LICENSE.md record in that directory.
 *
 * Fails at HEAD: index.css has no @font-face rules and the assets
 * directory does not exist (the CDN link was the only font source).
 */

const srcDir = resolve(__dirname, "..");
const cssPath = resolve(srcDir, "index.css");
const fontsDir = resolve(srcDir, "assets", "fonts");

const REQUIRED_FAMILIES = [
  "Spline Sans",
  "Inter",
  "Commissioner",
  "Electrolize",
] as const;

interface FontFaceBlock {
  family: string | null;
  hasFontDisplaySwap: boolean;
  urls: string[];
}

function parseFontFaceBlocks(css: string): FontFaceBlock[] {
  const blocks: FontFaceBlock[] = [];
  const re = /@font-face\s*\{([^}]*)\}/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(css)) !== null) {
    const body = m[1];
    const familyMatch = body.match(/font-family\s*:\s*["']?([^;"'}]+?)["']?\s*;/);
    const urls: string[] = [];
    let u: RegExpExecArray | null;
    const urlRe = /url\(\s*["']?([^"')]+)["']?\s*\)/g;
    while ((u = urlRe.exec(body)) !== null) urls.push(u[1]);
    blocks.push({
      family: familyMatch ? familyMatch[1].trim() : null,
      hasFontDisplaySwap: /font-display\s*:\s*swap/.test(body),
      urls,
    });
  }
  return blocks;
}

function resolveFontUrl(raw: string): string {
  const clean = raw.split("?")[0].split("#")[0];
  if (/^https?:\/\//i.test(clean) || /^data:/i.test(clean)) return clean;
  if (clean.startsWith("@/")) return resolve(srcDir, clean.slice(2));
  return resolve(cssPath, "..", clean);
}

describe("self-hosted @font-face declarations and assets (AC2)", () => {
  const css = readFileSync(cssPath, "utf-8");
  const blocks = parseFontFaceBlocks(css);

  it("declares @font-face with font-display: swap for each required family", () => {
    expect(blocks.length).toBeGreaterThanOrEqual(REQUIRED_FAMILIES.length);
    for (const family of REQUIRED_FAMILIES) {
      const familyBlocks = blocks.filter(
        (b) => b.family?.toLowerCase() === family.toLowerCase(),
      );
      expect(
        familyBlocks.length,
        `expected at least one @font-face rule for "${family}" in src/index.css`,
      ).toBeGreaterThan(0);
      expect(
        familyBlocks.every((b) => b.hasFontDisplaySwap),
        `every @font-face rule for "${family}" must declare font-display: swap`,
      ).toBe(true);
    }
  });

  it("points every @font-face url() at an existing .woff2 under src/assets/fonts", () => {
    expect(
      blocks.flatMap((b) => b.urls),
      "every @font-face rule must reference at least one local font file",
    ).not.toHaveLength(0);
    for (const block of blocks) {
      for (const rawUrl of block.urls) {
        const resolved = resolveFontUrl(rawUrl);
        expect(
          resolved.toLowerCase().endsWith(".woff2"),
          `font url must be a vendored .woff2 file, got: ${rawUrl}`,
        ).toBe(true);
        expect(
          resolved.startsWith(fontsDir + sep),
          `font url must resolve under src/assets/fonts, got: ${resolved}`,
        ).toBe(true);
        expect(
          existsSync(resolved),
          `vendored font file does not exist: ${resolved}`,
        ).toBe(true);
      }
    }
  });

  it("ships an OFL license record in src/assets/fonts", () => {
    expect(
      existsSync(resolve(fontsDir, "LICENSE.md")),
      "expected src/assets/fonts/LICENSE.md with per-family OFL license records",
    ).toBe(true);
  });
});
