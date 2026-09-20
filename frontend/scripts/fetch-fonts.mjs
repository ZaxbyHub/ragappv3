#!/usr/bin/env node
/**
 * Re-vendor the four self-hosted Google Font families (issue #572 / #640).
 *
 * Ported from .agents/issue-traces/572-self-host-fonts-lazy-chunks/repro/fetch-fonts.mjs
 * (the original fetch script) with two changes:
 *   - `stat -c%s` (Linux-only) replaced by fs.statSync(...).size so the script
 *     runs on Windows/macOS/Linux alike;
 *   - at the end it (re)writes frontend/src/assets/fonts/SHA256SUMS — the
 *     provenance manifest verified by src/tests/font-integrity-manifest.test.ts
 *     and the frozen #640 acceptance check.
 *
 * Fetches the css2 stylesheet with a modern-Chrome UA (woff2 + variable fonts,
 * full unicode-range subset split), downloads every woff2 it references into
 * frontend/src/assets/fonts/ under the vendored slug naming
 * (<family>-<subset>-<style>-<weight>.woff2), and prints the matching
 * @font-face CSS block to stdout (the canonical block lives in src/index.css).
 *
 * No dependencies: node stdlib + curl via execSync, like the original.
 *
 * Run from anywhere:  node frontend/scripts/fetch-fonts.mjs
 * (the output directory is resolved relative to this script's location).
 */
import { execSync } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdirSync, readdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const scriptDir = dirname(fileURLToPath(import.meta.url));
const OUT_DIR = join(scriptDir, "..", "src", "assets", "fonts");

const CSS2_URL =
  "https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Spline+Sans:wght@300..700&family=Commissioner:wght@100..900&family=Electrolize&display=swap";
const UA =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36";

const css = execSync(`curl -sfL -A "${UA}" "${CSS2_URL}"`, { encoding: "utf8", maxBuffer: 16 * 1024 * 1024 });

// css2 output: /* subset */\n@font-face { ... } blocks.
const blocks = [];
const re = /\/\*\s*([a-z0-9-]+)\s*\*\/\s*@font-face\s*\{([^}]+)\}/g;
let m;
while ((m = re.exec(css)) !== null) {
  const subset = m[1];
  const body = m[2];
  const pick = (prop) => {
    const pm = body.match(new RegExp(`${prop}:\\s*([^;]+);`));
    return pm ? pm[1].trim() : "";
  };
  const src = pick("src");
  const url = (src.match(/url\((https:\/\/[^)]+\.woff2)\)/) || [])[1];
  if (!url) continue;
  blocks.push({
    subset,
    family: pick("font-family"),
    style: pick("font-style") || "normal",
    weight: pick("font-weight") || "400",
    stretch: pick("font-stretch"),
    range: pick("unicode-range"),
    url,
  });
}

mkdirSync(OUT_DIR, { recursive: true });
const slug = (s) => s.replace(/[^a-z0-9]+/gi, "-").replace(/^-|-$/g, "").toLowerCase();
const cssOut = [];
const seen = new Set();
let totalBytes = 0;
for (const b of blocks) {
  const fam = b.family.replace(/'/g, "");
  const file = `${slug(fam)}-${b.subset}-${slug(b.style)}-${slug(b.weight)}.woff2`;
  const dest = join(OUT_DIR, file);
  if (!seen.has(file)) {
    seen.add(file);
    execSync(`curl -sfL -A "${UA}" -o "${dest}" "${b.url}"`);
  }
  const bytes = statSync(dest).size; // cross-platform size (was `stat -c%s`)
  totalBytes += seen.has(file + "c") ? 0 : bytes;
  seen.add(file + "c");
  cssOut.push(
    [
      `/* ${b.subset} */`,
      "@font-face {",
      `  font-family: ${b.family};`,
      `  font-style: ${b.style};`,
      `  font-weight: ${b.weight};`,
      b.stretch ? `  font-stretch: ${b.stretch};` : null,
      `  font-display: swap;`,
      `  src: url('./assets/fonts/${file}') format('woff2');`,
      b.range ? `  unicode-range: ${b.range};` : null,
      "}",
    ].filter(Boolean).join("\n"),
  );
}

// Provenance manifest (issue #640): `<sha256>  <filename>` (two spaces), one
// line per woff2 in the directory, sorted by filename — the format the
// integrity tests and the frozen #640 acceptance check parse.
const manifestLines = readdirSync(OUT_DIR)
  .filter((name) => name.toLowerCase().endsWith(".woff2"))
  .sort()
  .map((name) => `${createHash("sha256").update(readFileSync(join(OUT_DIR, name))).digest("hex")}  ${name}`);
writeFileSync(join(OUT_DIR, "SHA256SUMS"), manifestLines.join("\n") + "\n", "utf8");

console.log(`files: ${seen.size / 2}, blocks: ${blocks.length}, total woff2 bytes: ${totalBytes}`);
console.log(`SHA256SUMS written (${manifestLines.length} entries) to ${join(OUT_DIR, "SHA256SUMS")}`);
console.log("@font-face CSS block (canonical copy lives in src/index.css):");
console.log(cssOut.join("\n\n"));
