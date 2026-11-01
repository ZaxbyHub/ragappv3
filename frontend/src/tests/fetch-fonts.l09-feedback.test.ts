// frontend/src/tests/fetch-fonts.l09-feedback.test.ts
// Issue #780 L09 feedback round (PR #880 review) — defence-in-depth pins for
// fetch-fonts.mjs behaviours the frozen l09 drivers cannot cover without an
// amend (new non-frozen file, per the frozen-file policy):
//   - a missing requested family fails the run, leaves a seeded SHA256SUMS
//     byte-identical, sweeps stale *.part files, and renames the present
//     families (the deliberate post-download guard ordering);
//   - a successful run excludes orphan woff2 files from the run-scoped
//     SHA256SUMS and prunes them, so the tree is self-consistent;
//   - an interrupted download leaves no .part residue and does not rewrite a
//     seeded SHA256SUMS;
//   - a zero-block css2 response names the failure on stderr (not just any
//     non-zero exit).
// Mechanism: same patched-copy driver as the frozen l09 tests (the script
// runs outside vitest's module graph, so vi.mock cannot engage); payloads are
// ≥5000 bytes with the wOF2 magic so they pass the script's publish-point
// validation.
import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { afterAll, describe, expect, it } from "vitest";

const HERE = dirname(fileURLToPath(import.meta.url));
const REAL_SCRIPT = join(HERE, "..", "..", "scripts", "fetch-fonts.mjs");

// One block per expected family (all four EXPECTED_FAMILIES must be served
// for a success leg — the family guard is load-bearing); payloads are wOF2
// magic + digest padding past the 5000-byte publish floor.
const CSS_FAMILIES = ["Inter", "Spline Sans", "Commissioner", "Electrolize"] as const;
const slug = (s: string) => s.replace(/[^a-z0-9]+/gi, "-").replace(/^-|-$/g, "").toLowerCase();
const fileName = (family: string) => `${slug(family)}-latin-normal-400.woff2`;
const fontUrl = (family: string) => `https://fonts.example/${fileName(family)}`;

function payloadFor(url: string): Buffer {
  const digest = createHash("sha256").update(url).digest();
  return Buffer.concat([Buffer.from("wOF2", "latin1"), ...Array.from({ length: 170 }, () => digest)]).subarray(0, 5120);
}

const cssFor = (families: readonly string[]) =>
  families
    .map(
      (family) =>
        `/* latin */\n@font-face { font-family: '${family}'; font-style: normal; font-weight: 400; ` +
        `src: url(${fontUrl(family)}) format('woff2'); }`,
    )
    .join("\n");

// curl stub: css2 call echoes the served stylesheet; download call writes the
// URL's deterministic 5120-byte wOF2 payload — or 2048 magic-less bytes and
// exit 18 when L09_DOWNLOAD_THROW=1 (the interrupted shape, pre-validation).
const CURL_STUB_CJS = `const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const args = process.argv.slice(2);
if (args.includes("-o")) {
  const dest = args[args.indexOf("-o") + 1];
  fs.mkdirSync(path.dirname(dest), { recursive: true });
  const url = args[args.length - 1];
  const digest = crypto.createHash("sha256").update(url).digest();
  if (process.env.L09_DOWNLOAD_THROW === "1") {
    fs.writeFileSync(dest, Buffer.alloc(2048, 0x50));
    process.stderr.write("curl: (18) transfer closed with outstanding read data remaining\\n");
    process.exit(18);
  }
  const payload = Buffer.concat([Buffer.from("wOF2", "latin1"), ...new Array(170).fill(digest)]).subarray(0, 5120);
  fs.writeFileSync(dest, payload);
  process.exit(0);
}
if (process.env.L09_CSS_FILE) {
  process.stdout.write(fs.readFileSync(process.env.L09_CSS_FILE, "utf8"));
}
process.exit(0);
`;

interface RunResult {
  code: number;
  stderr: string;
  stdout: string;
}

function runScript(
  root: string,
  css: string,
  downloadsThrow: boolean,
): Promise<RunResult> {
  const cssFile = join(root, "css2-response.css");
  writeFileSync(cssFile, css, "utf8");
  return new Promise((resolve) => {
    const child = spawn(process.execPath, [join(root, "scripts", "fetch-fonts.mjs")], {
      cwd: root,
      env: { ...process.env, L09_CSS_FILE: cssFile, L09_DOWNLOAD_THROW: downloadsThrow ? "1" : "0" },
      stdio: ["ignore", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (d) => (stdout += d));
    child.stderr.on("data", (d) => (stderr += d));
    child.on("error", () => resolve({ code: -1, stderr, stdout }));
    child.on("close", (code) => resolve({ code: code ?? -1, stderr, stdout }));
  });
}

interface Fixture {
  root: string;
  fontsDir: string;
  manifestPath: string;
  writeManifest: (content: string) => void;
  readManifest: () => string;
}

function seedFixture(seedManifest: string, extras: Record<string, Buffer> = {}): Fixture {
  const root = mkdtempSync(join(tmpdir(), "l09-fetch-fonts-feedback-"));
  const scriptsDir = join(root, "scripts");
  const fontsDir = join(root, "src", "assets", "fonts");
  mkdirSync(scriptsDir, { recursive: true });
  mkdirSync(fontsDir, { recursive: true });
  writeFileSync(join(scriptsDir, "curl-stub.cjs"), CURL_STUB_CJS, "utf8");

  const source = readFileSync(REAL_SCRIPT, "utf8");
  let patchedCallSites = 0;
  const patched = source.replace(/execFileSync\(\s*"curl",\s*\[/g, () => {
    patchedCallSites += 1;
    return `execFileSync(process.execPath, [${JSON.stringify(join(scriptsDir, "curl-stub.cjs").replace(/\\/g, "/"))}, `;
  });
  expect(patchedCallSites).toBe(2);
  writeFileSync(join(scriptsDir, "fetch-fonts.mjs"), patched, "utf8");

  const manifestPath = join(fontsDir, "SHA256SUMS");
  writeFileSync(manifestPath, seedManifest, "utf8");
  for (const [name, bytes] of Object.entries(extras)) {
    writeFileSync(join(fontsDir, name), bytes);
  }
  return {
    root,
    fontsDir,
    manifestPath,
    writeManifest: (content: string) => writeFileSync(manifestPath, content, "utf8"),
    readManifest: () => readFileSync(manifestPath, "utf8"),
  };
}

const tempRoots: string[] = [];
function tracked<T extends Fixture>(fixture: T): T {
  tempRoots.push(fixture.root);
  return fixture;
}

afterAll(() => {
  for (const root of tempRoots) {
    rmSync(root, { recursive: true, force: true });
  }
});

const SEED_MANIFEST = "0000000000000000000000000000000000000000000000000000000000000000  seeded-latin-normal-400.woff2\n";

describe("fetch-fonts.mjs feedback hardening (issue #780 L09 review)", () => {
  it("a missing requested family fails the run, leaves the manifest untouched, and sweeps stale staging files", async () => {
    const orphanBytes = Buffer.from("orphan from an older vendoring");
    const fixture = tracked(
      seedFixture(SEED_MANIFEST, {
        "orphan-latin-normal-400.woff2": orphanBytes,
        "commissioner-latin-normal-400.woff2.part": Buffer.alloc(128, 0x51),
      }),
    );

    const result = await runScript(fixture.root, cssFor(CSS_FAMILIES.slice(0, 3)), false);

    expect(result.code).not.toBe(0);
    expect(result.stderr).toContain("missing requested font families: Electrolize");
    // The seeded manifest is untouched (the guard exits before the write).
    expect(fixture.readManifest()).toBe(SEED_MANIFEST);
    // Present families were renamed onto their final paths before the abort.
    for (const family of CSS_FAMILIES.slice(0, 3)) {
      expect(readFileSync(join(fixture.fontsDir, fileName(family)))).toEqual(payloadFor(fontUrl(family)));
    }
    // Stale staging file swept; no new residue; the orphan (a real woff2 the
    // css2 response no longer references) survives a FAILED run — pruning is
    // a success-path behaviour only.
    const names = readdirSync(fixture.fontsDir);
    expect(names.filter((name) => name.includes(".part"))).toEqual([]);
    expect(names).toContain("orphan-latin-normal-400.woff2");
    // The operator learns which final paths the aborted run already replaced.
    expect(result.stderr).toContain("final paths already replaced");
  });

  it("a successful run excludes orphan woff2 files from the run-scoped manifest and prunes them", async () => {
    const fixture = tracked(
      seedFixture(SEED_MANIFEST, {
        "orphan-latin-normal-400.woff2": Buffer.from("orphan from an older vendoring"),
      }),
    );

    const result = await runScript(fixture.root, cssFor(CSS_FAMILIES), false);

    expect(result.code).toBe(0);
    // The orphan is pruned and absent from the manifest (the run-scoped
    // property: a disk-sourced manifest would have legitimized it instead).
    const names = readdirSync(fixture.fontsDir);
    expect(names).not.toContain("orphan-latin-normal-400.woff2");
    expect(result.stdout).toContain("pruned 1 orphaned woff2 file(s)");
    const expectedManifest =
      [...CSS_FAMILIES.map(fileName)]
        .sort()
        .map((name) => {
          const family = CSS_FAMILIES.find((f) => fileName(f) === name)!;
          const hash = createHash("sha256").update(payloadFor(fontUrl(family))).digest("hex");
          return `${hash}  ${name}`;
        })
        .join("\n") + "\n";
    expect(fixture.readManifest()).toBe(expectedManifest);
    expect(names.filter((name) => name.includes(".part"))).toEqual([]);
  });

  it("an interrupted download leaves no staging residue and does not rewrite a seeded manifest", async () => {
    const seededFont = payloadFor(fontUrl("Inter"));
    const fixture = tracked(
      seedFixture(SEED_MANIFEST, {
        "inter-latin-normal-400.woff2": seededFont,
        "inter-latin-normal-400.woff2.part": Buffer.alloc(64, 0x52),
      }),
    );

    const result = await runScript(fixture.root, cssFor(CSS_FAMILIES), true);

    expect(result.code).not.toBe(0);
    expect(fixture.readManifest()).toBe(SEED_MANIFEST);
    expect(readFileSync(join(fixture.fontsDir, "inter-latin-normal-400.woff2"))).toEqual(seededFont);
    const names = readdirSync(fixture.fontsDir);
    expect(names.filter((name) => name.includes(".part"))).toEqual([]);
  });

  it("a zero-block css2 response names the failure on stderr", async () => {
    const fixture = tracked(seedFixture(SEED_MANIFEST));

    const result = await runScript(fixture.root, "", false);

    expect(result.code).not.toBe(0);
    expect(result.stderr).toContain("0 @font-face blocks");
    expect(fixture.readManifest()).toBe(SEED_MANIFEST);
  });
});
