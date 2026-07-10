import { describe, it, expect } from "vitest";
import { readFileSync } from "fs";
import { resolve } from "path";

// Source-inspection guard (sanctioned for structural invariants that are
// disproportionately expensive to exercise behaviorally). The demo-credential
// bypass is dead-code-eliminated in production `vite build` by gating it on
// import.meta.env.DEV; reproducing that at runtime requires a full production
// build. Asserting the DEV gate is present in source catches a regression that
// removes it (B7-1, #290) and matches App.tsx's pattern.
describe("LoginPage TEST_MODE DEV gate (B7-1)", () => {
  const source = readFileSync(resolve(__dirname, "./LoginPage.tsx"), "utf-8");

  it("gates TEST_MODE on import.meta.env.DEV (dead-code-eliminated in prod builds)", () => {
    // The TEST_MODE definition must include the DEV gate.
    expect(source).toMatch(/TEST_MODE\s*=\s*import\.meta\.env\.DEV\s*&&\s*import\.meta\.env\.VITE_TEST_MODE/);
  });

  it("matches App.tsx's DEV-gated TEST_MODE pattern", () => {
    const appSource = readFileSync(resolve(__dirname, "../App.tsx"), "utf-8");
    expect(appSource).toMatch(/import\.meta\.env\.DEV\s*&&\s*import\.meta\.env\.VITE_TEST_MODE/);
  });
});
