import { readFileSync } from "node:fs";

import { describe, it, expect } from "vitest";

/**
 * Regression test for issue #290: LoginPage demo-credential bypass missing
 * production DEV gate.
 *
 * The security property: `TEST_MODE` in LoginPage.tsx must be gated on
 * `import.meta.env.DEV` so Vite statically replaces it with `false` during
 * `vite build`, dead-code-eliminating the demo-credential login bypass
 * (lines that call `useAuthStore.setState` with a synthetic superadmin user
 * and `accessToken='demo-token'`).
 *
 * This invariant cannot be tested via component rendering because Vitest runs
 * in DEV mode (`import.meta.env.DEV === true`), so both the buggy and fixed
 * code behave identically in tests. The vulnerability only manifests at build
 * time. Therefore we verify the source-level invariant directly.
 */
const source = readFileSync("src/pages/LoginPage.tsx", "utf-8");

describe("LoginPage TEST_MODE DEV gate (issue #290)", () => {
  it("gates TEST_MODE on import.meta.env.DEV, matching App.tsx", () => {
    // Extract the TEST_MODE constant definition
    const match = source.match(/const\s+TEST_MODE\s*=\s*(.+?);/s);
    expect(match).not.toBeNull();
    const testModeExpr = match![1];

    // The expression MUST contain the import.meta.env.DEV guard
    expect(testModeExpr).toContain("import.meta.env.DEV");
    // The expression MUST contain the VITE_TEST_MODE check
    expect(testModeExpr).toContain("import.meta.env.VITE_TEST_MODE");
  });

  it("does NOT define TEST_MODE without the DEV guard (the original bug)", () => {
    // This regex matches the buggy pattern: TEST_MODE = import.meta.env.VITE_TEST_MODE === "true"
    // WITHOUT the import.meta.env.DEV && prefix
    const buggyPattern =
      /const\s+TEST_MODE\s*=\s*import\.meta\.env\.VITE_TEST_MODE\s*===\s*"true"\s*;/;
    expect(source).not.toMatch(buggyPattern);
  });
});
