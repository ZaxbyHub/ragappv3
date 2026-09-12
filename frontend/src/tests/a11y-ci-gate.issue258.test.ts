// Issue 258 / AC21 part (a) (ENH-005) — CI gate for the a11y suite.
//
// Phase 2.5 acceptance check. ENH-005 requires automated accessibility
// checks wired into CI. The a11y tooling itself already exists at base
// (eslint jsx-a11y-x recommended config + jest-axe smoke suite +
// `npm run test:a11y`); this check pins the CI WIRING as a cross-artifact
// contract so the gate cannot be silently dropped:
//
//   .github/workflows/ci.yml must contain a step whose `run:` invokes the
//   a11y suite (`npm run test:a11y`, or an equivalent vitest invocation
//   naming an a11y target), AND the referenced npm script must actually
//   exist in frontend/package.json (a workflow step referencing a missing
//   script is a broken gate that only fails when first needed).
//
// Note (measured at base a543361): ci.yml already has
// `- name: Accessibility smoke tests / run: npm run test:a11y`
// (added in d8aa440, 2026-06-30). The trace's localization log claims "no
// dedicated CI gate" — that is stale for the gate half; the remaining AC21
// gap is surface coverage (see a11y.smoke.surfaces.issue258.test.tsx).
// This check is therefore a PRESERVING pin at base: green now, RED the
// moment the gate is removed or pointed at a missing script.

import { describe, expect, it } from "vitest";
import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";

// src/tests/a11y-ci-gate.issue258.test.ts → repo root (frontend/src/tests)
const repoRoot = resolve(__dirname, "..", "..", "..");
const workflowPath = resolve(repoRoot, ".github", "workflows", "ci.yml");
const packageJsonPath = resolve(__dirname, "..", "..", "package.json");

/** A step `run:` line that executes the a11y suite. */
const A11Y_STEP_RUN = /run:\s*npm run test:a11y\b|run:\s*npx vitest[^\n]*a11y/;

describe("AC21 — CI a11y gate wiring (ENH-005)", () => {
  it("AC21: ci.yml runs the a11y suite in CI and the referenced script exists", () => {
    const workflowExists = existsSync(workflowPath);
    console.log(
      `AC21 CHECK: FAIL — expecting a CI step running the a11y suite in ${workflowPath} (exists=${workflowExists})`
    );
    expect(workflowExists, `missing ${workflowPath}`).toBe(true);

    const workflow = workflowExists ? readFileSync(workflowPath, "utf-8") : "";

    // Discriminating assertion 1: the workflow actually executes the suite.
    console.log("AC21 CHECK: FAIL — expecting ci.yml to contain a step run-line invoking the a11y suite");
    expect(workflow, "ci.yml has no step running the a11y suite (test:a11y or vitest a11y target)").toMatch(
      A11Y_STEP_RUN
    );

    // Discriminating assertion 2 (cross-artifact): the npm script the gate
    // invokes exists and points at an a11y test target.
    const pkg = JSON.parse(readFileSync(packageJsonPath, "utf-8")) as { scripts?: Record<string, string> };
    const script = pkg.scripts?.["test:a11y"];
    console.log(
      `AC21 CHECK: FAIL — expecting package.json scripts["test:a11y"] to exist and target an a11y suite, measured ${JSON.stringify(script)}`
    );
    expect(script, "package.json scripts['test:a11y'] is missing — the CI gate would fail to run").toBeTruthy();
    expect(script ?? "", "scripts['test:a11y'] does not target an a11y test surface").toMatch(/a11y/i);
  });
});
