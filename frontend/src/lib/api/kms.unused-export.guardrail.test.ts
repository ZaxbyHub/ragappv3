// frontend/src/lib/api/kms.unused-export.guardrail.test.ts
// Issue-trace 783 — the missing guard the audit named (RV-FU158-01: "an
// unused-export check in frontend lint; barrel.test.ts only asserts the
// export exists, not that anything calls it").
//
// Census, scoped to the KMS module: every FUNCTION export in
// src/lib/api/kms.ts must have at least one non-test importer outside
// src/lib/api, or carry a dated entry in the ALLOWLIST below with a
// reason. Function-scoped by construction — type-only exports are not API
// clients. RED on the pre-fix tree (listKMSJobs was the audit's finding);
// GREEN once the poll wiring consumes it.
//
// A repo-wide census is deliberately NOT this guard: roughly a third of the
// api modules' function exports have no non-test importers at base (trace
// 08a census) — re-wiring them is not this PR's scope and each module
// deserves its own decision.

import { describe, it, expect } from "vitest";
import { readFileSync, readdirSync, statSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const SRC_ROOT = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../.."
);
const KMS_MODULE = path.join(SRC_ROOT, "lib", "api", "kms.ts");

/**
 * Dated allowlist — each entry must stay unused; if one gains a production
 * importer, delete it here (a wired allowlist entry fails the test below).
 */
const ALLOWLIST: Record<string, string> = {
  // Per-document compile affordance is Activity-tray-shaped work; issue
  // #783's Constraints: "wire it or remove it: decide" — recorded, not
  // trivially wired beside the polling fix. Consumer lands with #784.
  compileDocumentKMS: "issue #783 Scope: not trivial alongside listKMSJobs; #784 tray",
  // The page's search flow goes through listKMSEntries' search param;
  // a second search client is redundant UI work outside this PR's
  // contract (trace 783 pre-run census finding).
  searchKMS: "trace 783 census: page search uses listKMSEntries search param",
};

function walkFiles(dir: string, out: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    const full = path.join(dir, entry);
    const st = statSync(full);
    if (st.isDirectory()) {
      walkFiles(full, out);
    } else if (/\.(ts|tsx)$/.test(entry)) {
      out.push(full);
    }
  }
  return out;
}

function functionExportsOf(moduleSource: string): string[] {
  const names: string[] = [];
  const re = /^export (?:async )?function ([A-Za-z0-9_]+)/gm;
  let m: RegExpExecArray | null;
  while ((m = re.exec(moduleSource)) !== null) {
    names.push(m[1]);
  }
  return names;
}

/**
 * Strip comments and string/template literals before consumer matching:
 * a comment or doc prose mentioning an export must never count as an
 * importer (reviewer probe: deleting the real wiring while a comment named
 * the export kept this guard green). Line comments, block comments, single/
 * double-quoted strings, and template literals are all removed.
 */
function stripNonCode(source: string): string {
  return source
    .replace(/\/\*[\s\S]*?\*\//g, " ")
    .replace(/\/\/[^\n]*/g, " ")
    .replace(/`(?:\\[\s\S]|[^\\`])*`/g, " ")
    .replace(/"(?:\\.|[^"\\\n])*"/g, " ")
    .replace(/'(?:\\.|[^'\\\n])*'/g, " ");
}

function nonTestImportersOf(name: string): string[] {
  // Named, namespace-member, and dynamic-import uses all match a
  // whole-word search on the importing file's CODE (comments/strings
  // stripped); test files and the api directory itself are excluded so a
  // definition or a mock never counts as a consumer.
  const consumers: string[] = [];
  const wordRe = new RegExp(`\\b${name}\\b`);
  for (const file of walkFiles(SRC_ROOT)) {
    const rel = path.relative(SRC_ROOT, file).split(path.sep).join("/");
    if (rel.startsWith("lib/api/")) continue;
    if (/\.test\.[tj]sx?$|\.spec\.[tj]sx?$/.test(rel)) continue;
    const src = stripNonCode(readFileSync(file, "utf-8"));
    if (wordRe.test(src)) consumers.push(rel);
  }
  return consumers;
}

describe("KMS api module unused-export guardrail (issue-trace 783)", () => {
  it("every kms.ts function export has a production importer or a dated allowlist entry", () => {
    const exports = functionExportsOf(readFileSync(KMS_MODULE, "utf-8"));
    expect(exports.length).toBeGreaterThan(0);

    const unused: string[] = [];
    for (const name of exports) {
      if (nonTestImportersOf(name).length > 0) continue;
      if (!(name in ALLOWLIST)) unused.push(name);
    }
    expect(
      unused,
      `KMS api exports with no non-test importer and no allowlist entry: ${unused.join(", ")}`
    ).toEqual([]);
  });

  it("no allowlisted export has silently gained a production importer", () => {
    for (const [name, reason] of Object.entries(ALLOWLIST)) {
      const importers = nonTestImportersOf(name);
      expect(
        importers,
        `${name} is allowlisted (${reason}) but now has importers: ${importers.join(", ")} — delete the allowlist entry`
      ).toEqual([]);
    }
  });
});
