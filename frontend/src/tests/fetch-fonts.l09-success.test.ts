// frontend/src/tests/fetch-fonts.l09-success.test.ts
// Issue #780 L09 (plan-critic round 1+2) — the SUCCESS path of
// fetch-fonts.mjs: staged `.part` downloads renamed onto final paths, and a
// run-scoped SHA256SUMS that is byte-identical to the vendoring contract
// (`<sha256-of-bytes>  <name>`, one line per file this run downloaded,
// sorted, trailing newline). The frozen l09 checks cover only the interrupted
// (C2) and zero-block (C3) paths, and C8 pins the committed tree — nothing
// else executes a successful vendoring.
//
// Mechanism (same as the frozen driver): vitest runs a dynamically imported
// project .mjs outside its module graph, so vi.mock never engages; instead a
// byte-copy of the CURRENT script is placed in a temp tree (its self-relative
// OUT_DIR lands inside the tree) with only the two `execFileSync("curl", [`
// call sites repointed at a deterministic stub (asserted to apply exactly
// twice). The stub serves a multi-block css whose families are LOAD-BEARING:
// one block per EXPECTED_FAMILIES entry (Inter, Spline Sans, Commissioner,
// Electrolize) plus an extra subset block, so the script's post-download
// family guard passes without relaxation — weakening or reordering the guard
// to satisfy a fixture is the exact regression this file exists to prevent.
import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { copyFileSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { afterAll, describe, expect, it } from "vitest";

const HERE = dirname(fileURLToPath(import.meta.url));
const REAL_SCRIPT = join(HERE, "..", "..", "scripts", "fetch-fonts.mjs");
const REAL_FONTS_DIR = join(HERE, "..", "assets", "fonts");

// One block per expected family + one extra Inter subset => five distinct
// payloads. Family names must stay in sync with EXPECTED_FAMILIES in
// frontend/scripts/fetch-fonts.mjs.
const CSS_BLOCKS: ReadonlyArray<{ family: string; subset: string; weight: string }> = [
  { family: "Inter", subset: "latin", weight: "400" },
  { family: "Inter", subset: "latin-ext", weight: "400" },
  { family: "Spline Sans", subset: "latin", weight: "300..700" },
  { family: "Commissioner", subset: "latin", weight: "100..900" },
  { family: "Electrolize", subset: "latin", weight: "400" },
];

const slug = (s: string) => s.replace(/[^a-z0-9]+/gi, "-").replace(/^-|-$/g, "").toLowerCase();
const fileName = (b: { family: string; subset: string; weight: string }) =>
  `${slug(b.family)}-${b.subset}-normal-${slug(b.weight)}.woff2`;
const fontUrl = (b: { family: string; subset: string; weight: string }) =>
  `https://fonts.example/${fileName(b)}`;

// Deterministic, per-URL payload bytes (512 each, distinct per font).
function payloadFor(url: string): Buffer {
  const digest = createHash("sha256").update(url).digest();
  return Buffer.concat([Buffer.from("wOF2", "latin1"), ...Array.from({ length: 170 }, () => digest)]).subarray(0, 5120);
}

const SUCCESS_CSS = CSS_BLOCKS.map(
  (b) =>
    `/* ${b.subset} */\n@font-face { font-family: '${b.family}'; font-style: normal; font-weight: ${b.weight}; ` +
    `src: url(${fontUrl(b)}) format('woff2'); }`,
).join("\n");

// curl stub: css2 call (no -o) echoes the served stylesheet; download call
// writes that URL's deterministic payload to the -o target, exit 0.
const CURL_STUB_CJS = `const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const args = process.argv.slice(2);
if (args.includes("-o")) {
  const dest = args[args.indexOf("-o") + 1];
  fs.mkdirSync(path.dirname(dest), { recursive: true });
  const url = args[args.length - 1];
  const digest = crypto.createHash("sha256").update(url).digest();
  const payload = Buffer.concat([Buffer.from("wOF2", "latin1"), ...new Array(170).fill(digest)]).subarray(0, 5120);
  fs.writeFileSync(dest, payload);
  process.exit(0);
}
if (process.env.L09_CSS_FILE) {
  process.stdout.write(fs.readFileSync(process.env.L09_CSS_FILE, "utf8"));
}
process.exit(0);
`;

const tempRoots: string[] = [];

afterAll(() => {
  for (const root of tempRoots) {
    rmSync(root, { recursive: true, force: true });
  }
});

describe("fetch-fonts.mjs success path (issue #780 L09)", () => {
  it("a full successful vendoring stages, renames, and writes the exact run-scoped manifest", async () => {
    const root = mkdtempSync(join(tmpdir(), "l09-fetch-fonts-success-"));
    tempRoots.push(root);
    const scriptsDir = join(root, "scripts");
    const fontsDir = join(root, "src", "assets", "fonts");
    mkdirSync(scriptsDir, { recursive: true });
    mkdirSync(fontsDir, { recursive: true });

    const stubPath = join(scriptsDir, "curl-stub.cjs");
    writeFileSync(stubPath, CURL_STUB_CJS, "utf8");

    const source = readFileSync(REAL_SCRIPT, "utf8");
    let patchedCallSites = 0;
    const patched = source.replace(/execFileSync\(\s*"curl",\s*\[/g, () => {
      patchedCallSites += 1;
      return `execFileSync(process.execPath, [${JSON.stringify(stubPath.replace(/\\/g, "/"))}, `;
    });
    expect(patchedCallSites).toBe(2);
    writeFileSync(join(scriptsDir, "fetch-fonts.mjs"), patched, "utf8");

    // The fixture families must cover the script's OWN EXPECTED_FAMILIES —
    // asserted from the script source, so adding a family to the script
    // fails here with a pointing finger instead of an opaque family-guard
    // exit (and the cheapest fix is extending the fixture, not relaxing the
    // guard).
    const scriptFamilies = source.match(/const EXPECTED_FAMILIES = \[([^\]]+)\]/)?.[1] ?? "";
    for (const family of scriptFamilies.split(",").map((s) => s.trim().replace(/^["']|["']$/g, "")).filter(Boolean)) {
      expect(
        CSS_BLOCKS.some((b) => b.family === family),
        `fixture must serve a block for EXPECTED_FAMILIES entry "${family}"`,
      ).toBe(true);
    }

    const cssFile = join(root, "css2-response.css");
    writeFileSync(cssFile, SUCCESS_CSS, "utf8");

    const exitCode = await new Promise<number>((resolve) => {
      const child = spawn(process.execPath, [join(root, "scripts", "fetch-fonts.mjs")], {
        cwd: root,
        env: { ...process.env, L09_CSS_FILE: cssFile, L09_DOWNLOAD_THROW: "0" },
        stdio: "ignore",
      });
      child.on("error", () => resolve(-1));
      child.on("close", (code) => resolve(code ?? -1));
    });
    expect(exitCode).toBe(0);

    // (a) every served payload lands byte-identical at its final path…
    const expectedFiles = CSS_BLOCKS.map(fileName);
    const onDisk = readdirSync(fontsDir).filter((name) => name.toLowerCase().endsWith(".woff2")).sort();
    expect(onDisk).toEqual([...expectedFiles].sort());
    for (const block of CSS_BLOCKS) {
      const final = join(fontsDir, fileName(block));
      expect(readFileSync(final).equals(payloadFor(fontUrl(block)))).toBe(true);
    }

    // (b) the manifest is exactly the run-scoped, sorted, two-space lines…
    const expectedManifest =
      [...expectedFiles]
        .sort()
        .map((name) => {
          const block = CSS_BLOCKS.find((b) => fileName(b) === name)!;
          const hash = createHash("sha256").update(payloadFor(fontUrl(block))).digest("hex");
          return `${hash}  ${name}`;
        })
        .join("\n") + "\n";
    expect(readFileSync(join(fontsDir, "SHA256SUMS"), "utf8")).toBe(expectedManifest);

    // (c) no staging residue: no .part file anywhere in the vendored dir.
    expect(readdirSync(fontsDir).filter((name) => name.includes(".part"))).toEqual([]);
  });
});
