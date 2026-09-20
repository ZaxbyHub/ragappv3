import { describe, expect, it, afterEach } from "vitest";
import { createHash } from "node:crypto";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { assertFontIntegrity, WOFF2_SIZE_FLOOR_BYTES } from "./helpers/font-integrity";

/**
 * Font integrity detection matrix (issue #640, AC7).
 *
 * Proves assertFontIntegrity actually DETECTS corruption — the defect the
 * #627 review flagged was font assertions so weak (existence-only) that a
 * corrupted binary passed. Each fixture below plants one corruption class
 * into an isolated temp directory and asserts the helper rejects it; a
 * structurally valid synthetic font is the control that passes.
 */

const tempDirs: string[] = [];

afterEach(() => {
  while (tempDirs.length > 0) {
    const dir = tempDirs.pop();
    if (dir) rmSync(dir, { recursive: true, force: true });
  }
});

function makeFixtureDir(): string {
  const dir = mkdtempSync(join(tmpdir(), "ragappv3-font-integrity-"));
  tempDirs.push(dir);
  return dir;
}

/** A synthetic but structurally valid woff2: magic + padding past the floor. */
function syntheticWoff2(): Buffer {
  const buffer = Buffer.alloc(WOFF2_SIZE_FLOOR_BYTES + 137, 0x61);
  buffer.write("wOF2", 0, "latin1");
  return buffer;
}

function sha256Hex(buffer: Buffer): string {
  return createHash("sha256").update(buffer).digest("hex");
}

function writeManifest(dir: string, lines: string[]): string {
  const text = `${lines.join("\n")}\n`;
  writeFileSync(join(dir, "SHA256SUMS"), text, "utf-8");
  return text;
}

describe("font integrity helper detects corruption (AC7)", () => {
  it("accepts a valid synthetic woff2 (control)", () => {
    const dir = makeFixtureDir();
    const bytes = syntheticWoff2();
    writeFileSync(join(dir, "fixture-latin-normal-400.woff2"), bytes);
    const manifest = writeManifest(dir, [`${sha256Hex(bytes)}  fixture-latin-normal-400.woff2`]);

    const result = assertFontIntegrity(dir, manifest);
    expect(result.verifiedCount).toBe(1);
  });

  it("rejects a truncated woff2 (below the size floor)", () => {
    const dir = makeFixtureDir();
    const bytes = syntheticWoff2().subarray(0, WOFF2_SIZE_FLOOR_BYTES - 1);
    writeFileSync(join(dir, "fixture-latin-normal-400.woff2"), bytes);
    const manifest = writeManifest(dir, [`${sha256Hex(bytes)}  fixture-latin-normal-400.woff2`]);

    expect(() => assertFontIntegrity(dir, manifest)).toThrow(/below the .*-byte floor/);
  });

  it("rejects a file with the wrong magic bytes", () => {
    const dir = makeFixtureDir();
    const bytes = syntheticWoff2();
    bytes.write("OTTO", 0, "latin1"); // a TTF/OTF magic, not woff2
    writeFileSync(join(dir, "fixture-latin-normal-400.woff2"), bytes);
    const manifest = writeManifest(dir, [`${sha256Hex(bytes)}  fixture-latin-normal-400.woff2`]);

    expect(() => assertFontIntegrity(dir, manifest)).toThrow(/bad magic/);
  });

  it("rejects a manifest hash mismatch (content changed after vendoring)", () => {
    const dir = makeFixtureDir();
    const bytes = syntheticWoff2();
    writeFileSync(join(dir, "fixture-latin-normal-400.woff2"), bytes);
    const wrongHash = sha256Hex(Buffer.from("different bytes entirely"));
    const manifest = writeManifest(dir, [`${wrongHash}  fixture-latin-normal-400.woff2`]);

    expect(() => assertFontIntegrity(dir, manifest)).toThrow(/sha256 mismatch/);
  });

  it("rejects a manifest line whose file is missing", () => {
    const dir = makeFixtureDir();
    const manifest = writeManifest(dir, [
      `${"0".repeat(64)}  never-vendored-latin-normal-400.woff2`,
    ]);

    expect(() => assertFontIntegrity(dir, manifest)).toThrow(/manifest lists missing file/);
  });

  it("rejects a woff2 in the directory that the manifest does not cover", () => {
    const dir = makeFixtureDir();
    const bytes = syntheticWoff2();
    writeFileSync(join(dir, "fixture-latin-normal-400.woff2"), bytes);
    writeFileSync(join(dir, "stowaway-latin-normal-400.woff2"), syntheticWoff2());
    const manifest = writeManifest(dir, [`${sha256Hex(bytes)}  fixture-latin-normal-400.woff2`]);

    expect(() => assertFontIntegrity(dir, manifest)).toThrow(/not covered by manifest/);
  });
});
