// frontend/src/tests/fetch-fonts.l09.test.ts
// Issue #780 L09 / AC2+AC3 — fetch-fonts.mjs must not corrupt the vendored
// fonts on a failed or zero-effect run.
//
// AC2: when a woff2 download writes 2048 partial bytes onto its -o target
// and then fails (the curl exit-18 shape), every .woff2 at a FINAL path in
// the vendored directory must stay byte-identical to the seeded bytes: all
// pre-existing files untouched, and no truncated newcomer at a final path
// (a file that was not seeded compares against zero bytes, so anything the
// failed download left behind fails here).
//
// AC3: a css2 response yielding zero @font-face blocks must fail the run
// (non-zero exit / failed evaluation) instead of rewriting SHA256SUMS from
// whatever is on disk and exiting 0.
//
// Mechanism note (deviation from the natural vi.mock design, documented for
// the trace): vitest 5 executes a dynamically imported project .mjs outside
// its module graph, so vi.mock("node:child_process")/vi.mock("node:url")
// silently never engage — the script would run REAL curl against the REAL
// fonts directory (verified empirically; each attempt re-downloaded the
// vendored fonts). Instead, exactly like the trace's own Phase-2 repro
// (.agents/issue-traces/780-workstream-l-housekeeping/scratch/repro-fonts.mjs,
// which "runs REAL copies of the production script with only the CSS2_URL
// constant repointed"), this check runs a byte-copy of the CURRENT script
// read from frontend/scripts/fetch-fonts.mjs, placed so its self-relative
// OUT_DIR (<scriptDir>/../src/assets/fonts) lands inside a per-test temp
// tree seeded with copies of the real vendored woff2 files, with only the
// two `execFileSync("curl", ...)` commands repointed at a deterministic
// stub (the download/manifest logic under test is untouched). The patch is
// asserted to apply exactly twice, so a script reshape fails this check
// loudly instead of silently testing nothing. The real fonts tree is only
// ever read; every write lands in the temp tree, hermetically (no network).
import { spawn } from "node:child_process";
import { copyFileSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { basename, dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { afterAll, describe, expect, it } from "vitest";

const HERE = dirname(fileURLToPath(import.meta.url));
const REAL_SCRIPT = join(HERE, "..", "..", "scripts", "fetch-fonts.mjs");
const REAL_FONTS_DIR = join(HERE, "..", "assets", "fonts");

const ONE_BLOCK_CSS =
  "/* latin */\n@font-face { font-family: 'Probe Sans'; font-style: normal; font-weight: 400; src: url(https://fonts.example/probe-sans-latin-normal-400.woff2) format('woff2'); }";

// The curl stub the patched script copy invokes instead of the network:
// - a css2 call (argv carries the css URL and no -o flag) prints the
//   scripted stylesheet to stdout and exits 0;
// - a download call (argv contains -o) writes 2048 partial bytes onto the
//   -o target (creating parent dirs), then — when L09_DOWNLOAD_THROW=1 —
//   fails with curl's transfer-interrupted exit code 18, which makes the
//   script's execFileSync throw exactly like a real interrupted download.
const CURL_STUB_CJS = `const fs = require("node:fs");
const path = require("node:path");
const args = process.argv.slice(2);
if (args.includes("-o")) {
  const dest = args[args.indexOf("-o") + 1];
  fs.mkdirSync(path.dirname(dest), { recursive: true });
  fs.writeFileSync(dest, Buffer.alloc(2048, 0x50));
  if (process.env.L09_DOWNLOAD_THROW === "1") {
    process.stderr.write("curl: (18) transfer closed with outstanding read data remaining\\n");
    process.exit(18);
  }
  process.exit(0);
}
if (process.env.L09_CSS_FILE) {
  process.stdout.write(fs.readFileSync(process.env.L09_CSS_FILE, "utf8"));
}
process.exit(0);
`;

const tempRoots: string[] = [];

// Lay out <tmp>/scripts (patched script copy + curl stub) and
// <tmp>/src/assets/fonts (the script copy's computed OUT_DIR, seeded with
// byte-copies of every real vendored woff2), and snapshot each seeded file's
// bytes.
function seedTempFontsDir(): { root: string; fontsDir: string; seeded: Map<string, Buffer> } {
  const root = mkdtempSync(join(tmpdir(), "l09-fetch-fonts-"));
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
  // Fail loudly if the script's shape changes: exactly two curl call sites
  // (the css2 fetch and the woff2 download) must have been repointed.
  expect(patchedCallSites).toBe(2);
  writeFileSync(join(scriptsDir, "fetch-fonts.mjs"), patched, "utf8");

  const seeded = new Map<string, Buffer>();
  for (const name of readdirSync(REAL_FONTS_DIR)) {
    if (!name.toLowerCase().endsWith(".woff2")) {
      continue;
    }
    copyFileSync(join(REAL_FONTS_DIR, name), join(fontsDir, name));
    seeded.set(join(fontsDir, name), readFileSync(join(fontsDir, name)));
  }

  tempRoots.push(root);
  return { root, fontsDir, seeded };
}

// Run the patched script copy; a non-zero exit (or spawn failure) is the
// "rejected import" outcome of the in-process design.
function runFetchFonts(root: string, css: string, downloadsThrow: boolean): Promise<"resolved" | "rejected"> {
  const cssFile = join(root, "css2-response.css");
  writeFileSync(cssFile, css, "utf8");
  return new Promise((resolve) => {
    const child = spawn(process.execPath, [join(root, "scripts", "fetch-fonts.mjs")], {
      cwd: root,
      env: { ...process.env, L09_CSS_FILE: cssFile, L09_DOWNLOAD_THROW: downloadsThrow ? "1" : "0" },
      stdio: "ignore",
    });
    child.on("error", () => resolve("rejected"));
    child.on("close", (code) => resolve(code === 0 ? "resolved" : "rejected"));
  });
}

afterAll(() => {
  for (const root of tempRoots) {
    rmSync(root, { recursive: true, force: true });
  }
});

describe("fetch-fonts.mjs failure hygiene (issue #780 L09)", () => {
  it("an interrupted download leaves the vendored woff2 files untouched", async () => {
    const dir = seedTempFontsDir();

    // Run the script with the interrupted download. The outcome is recorded
    // for triage but not asserted: AC2 pins the directory bytes, not the
    // run's fate (a fix may fail the run by a thrown error or a non-zero
    // exit).
    const outcome = await runFetchFonts(dir.root, ONE_BLOCK_CSS, true);
    expect(["resolved", "rejected"]).toContain(outcome);

    // Every .woff2 at a final path in the vendored dir must be byte-identical
    // to the seeded bytes (a file the failed run left behind that was never
    // seeded compares against zero bytes and fails here).
    const finalNames = new Set([
      ...[...dir.seeded.keys()].map((p) => basename(p)),
      ...readdirSync(dir.fontsDir).filter((name) => name.toLowerCase().endsWith(".woff2")),
    ]);
    for (const name of finalNames) {
      const p = join(dir.fontsDir, name);
      const orig = dir.seeded.get(p) ?? Buffer.alloc(0);
      expect(readFileSync(p).equals(orig)).toBe(true);
    }
  });

  it("a zero-block css2 response fails the run", async () => {
    const dir = seedTempFontsDir();

    const outcome = await runFetchFonts(dir.root, "", false);

    expect(outcome).toBe("rejected");
  });
});
