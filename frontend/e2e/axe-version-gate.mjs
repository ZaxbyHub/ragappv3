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
  constructor(installed, minimum, axePath, detail) {
    super(
      `axe-core ${installed} at ${axePath} is below the a11y gate minimum ${minimum}; ` +
        "the installed version IS the checker — bump frontend's axe-core devDependency" +
        (detail ? ` (${detail})` : ""),
    );
    this.name = "AxeVersionError";
    this.installed = installed;
    this.minimum = minimum;
  }
}

/**
 * Semver compare: returns negative when a < b. Pre-release precedence per
 * semver §11: a version WITH a pre-release sorts BELOW the same numeric
 * release; pre-release identifiers compare per-identifier (numeric
 * identifiers compare numerically and sort below alphanumeric ones; a
 * larger identifier set sorts above its own prefix).
 */
export function compareVersions(a, b) {
  const split = (v) => {
    const [core, pre = ""] = String(v).split("-", 2);
    return { nums: core.split(".").map((n) => Number.parseInt(n, 10) || 0), pre };
  };
  const pa = split(a);
  const pb = split(b);
  for (let i = 0; i < Math.max(pa.nums.length, pb.nums.length); i++) {
    const d = (pa.nums[i] ?? 0) - (pb.nums[i] ?? 0);
    if (d !== 0) return d;
  }
  return comparePrerelease(pa.pre, pb.pre);
}

/** semver §11 pre-release identifier precedence (feedback round F-009). */
export function comparePrerelease(a, b) {
  if (a === b) return 0;
  if (a && !b) return -1;
  if (!a && b) return 1;
  const idsA = a.split(".");
  const idsB = b.split(".");
  for (let i = 0; i < Math.max(idsA.length, idsB.length); i++) {
    const x = idsA[i];
    const y = idsB[i];
    if (x === undefined) return -1;
    if (y === undefined) return 1;
    const aNum = /^[0-9]+$/.test(x);
    const bNum = /^[0-9]+$/.test(y);
    if (aNum && bNum) {
      const d = Number.parseInt(x, 10) - Number.parseInt(y, 10);
      if (d !== 0) return d;
    } else if (aNum) return -1;
    else if (bNum) return 1;
    else if (x !== y) return x < y ? -1 : 1;
  }
  return 0;
}

/**
 * Read the installed axe-core version beside the injected axe.min.js and
 * assert it is >= minimum. `axePath` is the same path the caller injects.
 * The manifest must also carry name "axe-core" — the gate reads the package
 * metadata that ships beside the injected bundle, and a foreign or
 * mis-planted manifest must fail closed rather than lend its version field
 * to the check (feedback round F-009/SEC-004).
 */
export function assertAxeVersion({ axePath, minimum }) {
  const manifestPath = path.join(path.dirname(axePath), "package.json");
  let manifest;
  try {
    manifest = JSON.parse(fs.readFileSync(manifestPath, "utf-8"));
  } catch (err) {
    throw new AxeVersionError("unreadable", minimum, manifestPath, err instanceof Error ? err.message : String(err));
  }
  if (manifest.name !== "axe-core") {
    throw new AxeVersionError(
      String(manifest.version ?? "unknown"),
      minimum,
      manifestPath,
      `manifest beside the injected bundle is name "${manifest.name ?? "unknown"}", not "axe-core"`,
    );
  }
  if (compareVersions(manifest.version, minimum) < 0) {
    throw new AxeVersionError(manifest.version, minimum, axePath);
  }
  return manifest.version;
}
