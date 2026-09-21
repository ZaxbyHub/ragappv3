/// <reference types="node" />
// (TypeScript 6 no longer auto-includes @types packages for ambient module
// declarations — without this directive, `node:crypto` et al. fail TS2591
// under `tsc --noEmit` even though @types/node is installed transitively.)
import { createHash } from "node:crypto";
import { existsSync, readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";

/**
 * Font integrity helper (issue #640, AC1-AC3/AC7).
 *
 * The vendored woff2 set under frontend/src/assets/fonts/ is pinned by a
 * SHA256SUMS manifest written by frontend/scripts/fetch-fonts.mjs. These
 * helpers turn that manifest into an enforced contract instead of the
 * existence-only checks the #627 review flagged: every manifest line must
 * name an existing file with the wOF2 magic, a sane size floor, and a
 * matching sha256 — and every woff2 on disk must be covered.
 */

/** Minimum plausible woff2 size; anything smaller is a truncated/corrupt file. */
export const WOFF2_SIZE_FLOOR_BYTES = 5000;

/** woff2 files start with the four-byte magic "wOF2" (RFC: WOFF2 spec §4). */
export const WOFF2_MAGIC = "wOF2";

export interface FontManifestEntry {
  /** File name only (no path components). */
  name: string;
  /** Lowercase hex sha256 of the file bytes. */
  sha256: string;
}

export interface FontIntegrityResult {
  /** Number of manifest entries verified. */
  verifiedCount: number;
}

/**
 * Parse SHA256SUMS text: one `<64 hex>  <filename>` line per font (exactly
 * two spaces between hash and name), blank lines ignored. Throws on
 * malformed lines, duplicate entries, or entries carrying path separators.
 */
export function parseFontManifest(manifestText: string): FontManifestEntry[] {
  const entries: FontManifestEntry[] = [];
  const lines = manifestText.split(/\r?\n/).filter((line) => line.trim().length > 0);
  for (let i = 0; i < lines.length; i += 1) {
    const match = /^([0-9a-fA-F]{64}) {2}(.+)$/.exec(lines[i].trim());
    if (!match) {
      throw new Error(`malformed manifest line ${i + 1}: ${lines[i]}`);
    }
    const name = match[2].trim();
    if (name.includes("/") || name.includes("\\")) {
      throw new Error(`manifest line ${i + 1} must use filename only, got path: ${name}`);
    }
    if (entries.some((entry) => entry.name === name)) {
      throw new Error(`duplicate manifest entry: ${name}`);
    }
    entries.push({ name, sha256: match[1].toLowerCase() });
  }
  return entries;
}

/** Compute the lowercase-hex sha256 of the file at `filePath`. */
export function computeFileSha256(filePath: string): string {
  return createHash("sha256").update(readFileSync(filePath)).digest("hex");
}

/**
 * Assert the full font-integrity contract for `fontsDir` against the
 * SHA256SUMS `manifestText`:
 *
 * - every manifest line names an existing woff2 (missing file fails);
 * - every covered file is at least WOFF2_SIZE_FLOOR_BYTES bytes;
 * - every covered file starts with the wOF2 magic;
 * - every covered file's sha256 matches the manifest;
 * - every *.woff2 in the directory is covered by the manifest (an uncovered
 *   file fails — an extra font slipped into the tree is a provenance break,
 *   not an addition).
 *
 * Throws a single Error listing every violation (all problems are collected
 * before throwing, so one run reports the whole picture); returns the number
 * of verified entries on success.
 */
export function assertFontIntegrity(fontsDir: string, manifestText: string): FontIntegrityResult {
  const entries = parseFontManifest(manifestText);
  const problems: string[] = [];

  const dirFiles = readdirSync(fontsDir)
    .filter((name) => name.toLowerCase().endsWith(".woff2"))
    .sort();

  const manifestNames = entries.map((entry) => entry.name);
  if (JSON.stringify(manifestNames.slice().sort()) !== JSON.stringify(manifestNames)) {
    problems.push("manifest lines are not sorted by filename");
  }

  for (const entry of entries) {
    const filePath = join(fontsDir, entry.name);
    if (!existsSync(filePath)) {
      problems.push(`manifest lists missing file: ${entry.name}`);
      continue;
    }
    const bytes = readFileSync(filePath);
    if (bytes.length < WOFF2_SIZE_FLOOR_BYTES) {
      problems.push(
        `${entry.name}: size ${bytes.length} is below the ${WOFF2_SIZE_FLOOR_BYTES}-byte floor (truncated?)`,
      );
      continue;
    }
    const magic = bytes.subarray(0, 4).toString("latin1");
    if (magic !== WOFF2_MAGIC) {
      problems.push(`${entry.name}: bad magic ${JSON.stringify(magic)}, expected "${WOFF2_MAGIC}"`);
      continue;
    }
    const actual = createHash("sha256").update(bytes).digest("hex");
    if (actual !== entry.sha256) {
      problems.push(`${entry.name}: sha256 mismatch (manifest ${entry.sha256}, actual ${actual})`);
    }
  }

  // Case-insensitive coverage comparison: on Windows existsSync/hash open
  // files case-insensitively, so a case mismatch between manifest and disk
  // would otherwise surface as a confusing "not covered" error instead of
  // pointing at the manifest/disk case divergence (PR #651 review NEW-001).
  const covered = new Set(manifestNames.map((name) => name.toLowerCase()));
  for (const name of dirFiles) {
    if (!covered.has(name.toLowerCase())) {
      problems.push(`woff2 not covered by manifest: ${name}`);
    }
  }

  // F-009 disposition (PR #651 review): the detection test covers the
  // load-bearing rejections (truncation, bad magic, hash mismatch, missing
  // and uncovered files); the remaining defensive branches (unreadable dir,
  // malformed manifest header) are accepted uncovered — they guard against
  // environment faults, not the vendored-drift class this helper exists for.
  if (problems.length > 0) {
    throw new Error(`font integrity failures in ${fontsDir}:\n  - ${problems.join("\n  - ")}`);
  }
  return { verifiedCount: entries.length };
}
