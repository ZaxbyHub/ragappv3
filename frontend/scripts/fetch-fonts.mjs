#!/usr/bin/env node
/**
 * Re-vendor the four self-hosted Google Font families (issue #572 / #640).
 *
 * Ported from .agents/issue-traces/572-self-host-fonts-lazy-chunks/repro/fetch-fonts.mjs
 * (the original fetch script) with two changes:
 *   - `stat -c%s` (Linux-only) replaced by fs.statSync(...).size so the script
 *     runs on Windows/macOS/Linux alike;
 *   - at the end it (re)writes frontend/src/assets/fonts/SHA256SUMS — the
 *     integrity manifest verified by src/tests/font-integrity-manifest.test.ts
 *     and the frozen #640 acceptance check. The manifest pins the bytes that
 *     ship (TOFU: trust-on-first-use) — it proves the vendored files have not
 *     drifted since the pin, not that the original vendoring was untampered;
 *     the css2 URL below documents the origin for re-vendoring.
 *
 * Fetches the css2 stylesheet with a modern-Chrome UA (woff2 + variable fonts,
 * full unicode-range subset split), downloads every woff2 it references into
 * frontend/src/assets/fonts/ under the vendored slug naming
 * (<family>-<subset>-<style>-<weight>.woff2), and prints the matching
 * @font-face CSS block to stdout (the canonical block lives in src/index.css).
 *
 * #780 (L09) failure hygiene: each woff2 downloads to a `.part` staging file
 * and is renamed onto its final path only after a completed transfer, so an
 * interrupted run leaves no truncated bytes at any final path; a css2 response
 * that yields zero @font-face blocks (or is missing any requested family)
 * fails the run instead of "succeeding" with a manifest regenerated from
 * whatever is already on disk; the manifest itself is written atomically and
 * covers only the files this run actually vendored.
 *
 * No dependencies: node stdlib + curl via execSync, like the original.
 *
 * Run from anywhere:  node frontend/scripts/fetch-fonts.mjs
 * (the output directory is resolved relative to this script's location).
 */
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdirSync, readdirSync, readFileSync, renameSync, rmSync, statSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const scriptDir = dirname(fileURLToPath(import.meta.url));
const OUT_DIR = join(scriptDir, "..", "src", "assets", "fonts");

const EXPECTED_FAMILIES = ["Inter", "Spline Sans", "Commissioner", "Electrolize"];
const CSS2_URL =
  "https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Spline+Sans:wght@300..700&family=Commissioner:wght@100..900&family=Electrolize&display=swap";
const UA =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36";

// F-004/F-005 (#651 review): execFileSync (no shell -> no injection
// surface if the URL ever becomes dynamic) and a bounded network time.
const CSS_TIMEOUT_S = 60;
const css = execFileSync(
  "curl",
  ["-sfL", "--max-time", String(CSS_TIMEOUT_S), "-A", UA, CSS2_URL],
  { encoding: "utf8", maxBuffer: 16 * 1024 * 1024 },
);

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

// T1-20-MH-07 (#780): a css2 response that parsed to zero @font-face blocks
// (e.g. Google changes the response format) must fail loudly BEFORE anything
// is downloaded, never "succeed" by regenerating the manifest from disk.
if (blocks.length === 0) {
  console.error(`fetch-fonts: css2 response parsed to 0 @font-face blocks (${CSS2_URL}) — aborting without touching the vendored fonts`);
  process.exit(1);
}

mkdirSync(OUT_DIR, { recursive: true });
// Sweep staging files orphaned by earlier interrupted runs (#780 review): a
// hard kill (power loss / SIGKILL) mid-curl bypasses the per-file catch and
// would otherwise leave a foo.woff2.part behind that no gate scans.
for (const stale of readdirSync(OUT_DIR)) {
  if (stale.endsWith(".part")) {
    rmSync(join(OUT_DIR, stale), { force: true });
  }
}
const slug = (s) => s.replace(/[^a-z0-9]+/gi, "-").replace(/^-|-$/g, "").toLowerCase();
const cssOut = [];
const seen = new Set();
const downloadedFiles = []; // files vendored by THIS run — the only manifest source (#780)
let totalBytes = 0;
for (const b of blocks) {
  const fam = b.family.replace(/'/g, "");
  const file = `${slug(fam)}-${b.subset}-${slug(b.style)}-${slug(b.weight)}.woff2`;
  const dest = join(OUT_DIR, file);
  if (!seen.has(file)) {
    seen.add(file);
    // T1-20-S2-12 (#780): download to a staging name and rename into place
    // only after a completed transfer, so an interrupted download leaves the
    // previous vendored bytes intact at the final path and no truncated file.
    const part = join(OUT_DIR, `${file}.part`);
    try {
      execFileSync(
        "curl",
        ["-sfL", "--max-time", String(CSS_TIMEOUT_S), "-A", UA, "-o", part, b.url],
      );
      renameSync(part, dest);
    } catch (err) {
      rmSync(part, { force: true });
      throw err;
    }
    downloadedFiles.push(file);
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

// T1-20-MH-07 (#780, second half): every family this run requested must have
// been seen. Runs AFTER the download loop and BEFORE the manifest write on
// purpose — a pre-download family guard would let the frozen interrupted-
// download check pass without exercising the staging fix, and this ordering
// leaves a failed family check detectable by the font-integrity CI test
// (files downloaded, manifest not rewritten) instead of silently partial.
const seenFamilies = new Set(blocks.map((b) => b.family.replace(/'/g, "")));
const missingFamilies = EXPECTED_FAMILIES.filter((f) => !seenFamilies.has(f));
if (missingFamilies.length > 0) {
  console.error(`fetch-fonts: css2 response is missing requested font families: ${missingFamilies.join(", ")} — aborting before writing SHA256SUMS`);
  process.exit(1);
}

// Provenance manifest (issue #640): `<sha256>  <filename>` (two spaces), one
// line per woff2 vendored by THIS run, sorted by filename — the format the
// integrity tests and the frozen #640 acceptance check parse. Written via a
// staging name + rename so a crash mid-write cannot leave a truncated
// manifest behind (#780). For a clean vendored directory this is byte-identical
// to the previous disk-sourced manifest; orphan files from older runs no
// longer get legitimized into it.
const manifestLines = downloadedFiles
  .slice()
  .sort()
  .map((name) => `${createHash("sha256").update(readFileSync(join(OUT_DIR, name))).digest("hex")}  ${name}`);
const manifestPart = join(OUT_DIR, "SHA256SUMS.part");
writeFileSync(manifestPart, manifestLines.join("\n") + "\n", "utf8");
renameSync(manifestPart, join(OUT_DIR, "SHA256SUMS"));

console.log(`files: ${seen.size / 2}, blocks: ${blocks.length}, total woff2 bytes: ${totalBytes}`);
console.log(`SHA256SUMS written (${manifestLines.length} entries) to ${join(OUT_DIR, "SHA256SUMS")}`);
console.log("@font-face CSS block (canonical copy lives in src/index.css):");
console.log(cssOut.join("\n\n"));
