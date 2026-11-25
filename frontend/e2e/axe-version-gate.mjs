// frontend/e2e/axe-version-gate.mjs — issue #871 AC5.
//
// The a11y specs inject ../node_modules/axe-core/axe.min.js directly, so the
// INSTALLED axe version silently IS the checker (4.14 changed
// label-content-name-mismatch behavior — exactly the risk this gate pins).
// assertAxeVersion reads the axe package.json that ships beside the injected
// bundle and throws a named error when the installed version is below the
// minimum, so a regressed pin fails the suite at load time instead of
// silently changing what the gate measures.
//
// Dependency-free ESM: imported by a11y-matrix.spec.ts (FROZEN #778 setup)
// and accessibility_gate_extensions.spec.ts with the SAME axePath they
// inject, and unit-tested by src/__tests__/a11y-axe-version-gate.test.ts.

import fs from "node:fs";
import path from "node:path";

export class AxeVersionError extends Error {
  constructor(installed, minimum, axePath) {
    super(
      `axe-core ${installed} at ${axePath} is below the a11y gate minimum ${minimum}; ` +
        "the installed version IS the checker — bump frontend's axe-core devDependency",
    );
    this.name = "AxeVersionError";
    this.installed = installed;
    this.minimum = minimum;
  }
}

/** Semver-ish compare: returns negative when a < b (pre-release segments unsupported — axe stable only). */
export function compareVersions(a, b) {
  const pa = String(a).split(".").map((n) => Number.parseInt(n, 10) || 0);
  const pb = String(b).split(".").map((n) => Number.parseInt(n, 10) || 0);
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const d = (pa[i] ?? 0) - (pb[i] ?? 0);
    if (d !== 0) return d;
  }
  return 0;
}

/**
 * Read the installed axe-core version beside the injected axe.min.js and
 * assert it is >= minimum. `axePath` is the same path the caller injects.
 */
export function assertAxeVersion({ axePath, minimum }) {
  const manifestPath = path.join(path.dirname(axePath), "package.json");
  let version;
  try {
    version = JSON.parse(fs.readFileSync(manifestPath, "utf-8")).version;
  } catch (err) {
    throw new AxeVersionError("unreadable", minimum, manifestPath);
  }
  if (compareVersions(version, minimum) < 0) {
    throw new AxeVersionError(version, minimum, axePath);
  }
  return version;
}
