// frontend/src/__tests__/a11y-axe-version-gate.test.ts
// Issue #871 AC5 gate clause (frozen acceptance check C5).
//
// Three layers (plan-critic round 1, finding 4):
//   (a) comparator boundaries — a fixture manifest below 4.14.0 is rejected,
//       the boundary itself (4.14.0) is accepted, and the REAL installed
//       axe-core beside the injected bundle is accepted and >= 4.14.0;
//   (b) static WIRING PIN — both production e2e call sites must invoke the
//       gate bound to the shared AXE_PATH resolver, so omitting a call site
//       fails this check;
//   (c) lockfile pin — the checkout's package-lock resolves axe-core >= 4.14.0
//       (read from the tracked lockfile, so the assertion is replay-stable
//       across the bump).

import { describe, it, expect } from "vitest";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { AxeVersionError, assertAxeVersion, compareVersions } from "../../e2e/axe-version-gate.mjs";

const e2eDir = resolve(__dirname, "../../e2e");

/** Write a fixture axe package dir carrying the given version; return its axe.min.js path. */
function fixtureAxe(version: string): string {
  const dir = resolve(e2eDir, `../node_modules/@fixtures/axe-${version}`);
  mkdirSync(dir, { recursive: true });
  writeFileSync(resolve(dir, "package.json"), JSON.stringify({ name: "axe-core", version }));
  const axePath = resolve(dir, "axe.min.js");
  writeFileSync(axePath, "/* fixture */");
  return axePath;
}

describe("axe version gate — comparator (#871 AC5)", () => {
  it("compareVersions orders 4.13.0 below 4.14.0 and 4.14.1 above it", () => {
    expect(compareVersions("4.13.0", "4.14.0")).toBeLessThan(0);
    expect(compareVersions("4.14.0", "4.14.0")).toBe(0);
    expect(compareVersions("4.14.1", "4.14.0")).toBeGreaterThan(0);
  });

  it("rejects an installed axe below the minimum with a named error", () => {
    // Fixture manifest carrying the pre-bump version.
    const fixturePath = fixtureAxe("4.13.0");

    expect(() => assertAxeVersion({ axePath: fixturePath, minimum: "4.14.0" })).toThrowError(AxeVersionError);
    try {
      assertAxeVersion({ axePath: fixturePath, minimum: "4.14.0" });
      expect.unreachable("gate must throw below the minimum");
    } catch (err) {
      const e = err as AxeVersionError;
      expect(e.name).toBe("AxeVersionError");
      expect(e.installed).toBe("4.13.0");
      expect(e.message).toContain("4.13.0");
      expect(e.message).toContain("4.14.0");
    }
  });

  it("accepts the exact 4.14.0 boundary", () => {
    const fixturePath = fixtureAxe("4.14.0");
    expect(assertAxeVersion({ axePath: fixturePath, minimum: "4.14.0" })).toBe("4.14.0");
  });

  it("the REAL installed axe-core beside the injected bundle is >= 4.14.0", () => {
    const realAxePath = resolve(e2eDir, "../node_modules/axe-core/axe.min.js");
    const version = assertAxeVersion({ axePath: realAxePath, minimum: "4.14.0" });
    expect(compareVersions(version, "4.14.0")).toBeGreaterThanOrEqual(0);
  });
});

describe("axe version gate — production wiring pin (#871 AC5)", () => {
  const matrixSpec = readFileSync(resolve(e2eDir, "a11y-matrix.spec.ts"), "utf-8");
  const extensionsSpec = readFileSync(resolve(e2eDir, "accessibility_gate_extensions.spec.ts"), "utf-8");

  it("a11y-matrix.spec.ts invokes the gate on the SAME path it injects", () => {
    expect(matrixSpec).toMatch(/assertAxeVersion\(\{\s*axePath:\s*AXE_PATH/);
    expect(matrixSpec).toMatch(/minimum:\s*"4\.14\.0"/);
    expect(matrixSpec).toContain('path.resolve(__dirname, "../node_modules/axe-core/axe.min.js")');
  });

  it("accessibility_gate_extensions.spec.ts invokes the gate on the SAME path it injects", () => {
    expect(extensionsSpec).toMatch(/assertAxeVersion\(\{\s*axePath:\s*AXE_PATH/);
    expect(extensionsSpec).toMatch(/minimum:\s*"4\.14\.0"/);
    expect(extensionsSpec).toContain('path.resolve(__dirname, "../node_modules/axe-core/axe.min.js")');
  });
});

describe("axe version gate — lockfile pin (#871 AC5)", () => {
  it("the checkout's package-lock resolves top-level axe-core >= 4.14.0", () => {
    const lock = JSON.parse(readFileSync(resolve(e2eDir, "../package-lock.json"), "utf-8")) as {
      packages?: Record<string, { version?: string }>;
    };
    const resolved = lock.packages?.["node_modules/axe-core"]?.version;
    expect(resolved, "top-level axe-core resolution").toBeTruthy();
    expect(compareVersions(resolved, "4.14.0")).toBeGreaterThanOrEqual(0);
  });
});
