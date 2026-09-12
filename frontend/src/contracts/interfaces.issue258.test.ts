// Issue 258 / AC7 (TEST-006) — negative-compile interface contract suite.
//
// Phase 2.5 acceptance check. The current interface "tests"
// (src/lib/api.interfaces.test.ts) are runtime constructions of
// self-authored literals — they cannot fail when an interface regresses
// (e.g. `vault_id` becoming optional on CreateSessionRequest). AC7 requires
// a typecheck-enabled negative-compile suite instead.
//
// THE CONTRACT THIS CHECK ENFORCES (delivered by the Phase 4 fix):
//
//   1. frontend/src/contracts/interfaces.contracts.ts — a `.ts` file whose
//      ONLY job is to assign deliberately-invalid shapes to the real
//      interfaces imported from `@/lib/api`, each guarded by
//      `// @ts-expect-error`:
//        a. `CreateSessionRequest` with `vault_id` OMITTED  (vault_id is
//           required — omitting it must be a compile error);
//        b. `Vault` with `is_default` PRESENT (Vault has no such field —
//           adding it must be an excess-property compile error);
//        c. `Vault` with required fields MISSING (id/name/... are required —
//           omitting them must be a compile error);
//      plus at least one positive assignment each (valid shapes must
//      compile with no directive).
//   2. frontend/tsconfig.contracts.json — a minimal project that typechecks
//      ONLY that contracts file, e.g.:
//        {
//          "compilerOptions": {
//            "target": "ES2020",
//            "lib": ["ES2020", "DOM"],
//            "module": "ESNext",
//            "moduleResolution": "bundler",
//            "strict": true,
//            "noEmit": true,
//            "skipLibCheck": true,
//            "ignoreDeprecations": "6.0",
//            "baseUrl": ".",
//            "paths": { "@/*": ["./src/*"] }
//          },
//          "include": ["src/vite-env.d.ts", "src/contracts/interfaces.contracts.ts"]
//        }
//      (ignoreDeprecations is required by this repo's TypeScript with
//      baseUrl, matching the main tsconfig.json; src/vite-env.d.ts is
//      included so `import.meta.env` in the transitively imported api
//      modules typechecks.)
//
// MECHANISM: `npx tsc --noEmit -p tsconfig.contracts.json` must exit 0.
// Exit 0 means every `@ts-expect-error` directive was ACTUALLY consumed by
// a real compile error. If an interface regresses to allow a bad shape (the
// Phase 4.5 mutant: `vault_id?: number` on CreateSessionRequest), the
// directive goes unused → TS2578 "Unused '@ts-expect-error' directive" →
// non-zero exit → this check is RED. A positive assignment failing to
// compile is also a non-zero exit. No runtime assertion is involved.
//
// At base a543361 neither file exists → the tsc invocation fails → RED.

import { describe, expect, it } from "vitest";
import { existsSync, readFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { resolve } from "node:path";

// src/contracts/interfaces.issue258.test.ts → frontend/
const frontendDir = resolve(__dirname, "..", "..");
const tsconfigPath = resolve(frontendDir, "tsconfig.contracts.json");
const contractsPath = resolve(frontendDir, "src", "contracts", "interfaces.contracts.ts");

describe("AC7 — negative-compile interface contracts (TEST-006)", () => {
  it("AC7: negative-compile suite passes (tsc --noEmit -p tsconfig.contracts.json exits 0)", { timeout: 180_000 }, () => {
    const projectExists = existsSync(tsconfigPath);
    const contractsExists = existsSync(contractsPath);

    // Sentinel first: every discriminating assertion below is preceded by it.
    console.log(
      "AC7 CHECK: FAIL — expecting tsconfig.contracts.json and interfaces.contracts.ts to exist" +
        ` (tsconfig.contracts.json exists=${projectExists}, interfaces.contracts.ts exists=${contractsExists})`
    );
    expect(projectExists, `missing ${tsconfigPath} — see header comment for the required project shape`).toBe(true);
    expect(contractsExists, `missing ${contractsPath} — see header comment for the required contract cases`).toBe(true);

    const result = spawnSync("npx", ["tsc", "--noEmit", "-p", "tsconfig.contracts.json"], {
      cwd: frontendDir,
      encoding: "utf-8",
      shell: process.platform === "win32",
    });

    console.log(
      `AC7 CHECK: FAIL — expecting tsc exit code 0, measured ${result.status}` +
        (result.status === 0 ? "" : " (unused @ts-expect-error directives or real type errors regress the contracts)")
    );
    expect(
      result.status,
      `tsc --noEmit -p tsconfig.contracts.json failed (exit ${result.status})\n` +
        `stdout:\n${result.stdout}\nstderr:\n${result.stderr}`
    ).toBe(0);
  });

  it("AC7: contracts file pins the three audited shapes (anti-trivial floor)", () => {
    // Floor guard: an empty or trivial contracts file would satisfy the
    // tsc-exit-0 check without pinning anything. The real gate stays the
    // tsc run above (plus Phase 4.5 mutation evidence); this only refuses
    // a pass-through file. Source-scoped, mirroring the sanctioned pattern
    // in src/pages/LoginPage.test.tsx.
    expect(existsSync(contractsPath), `missing ${contractsPath}`).toBe(true);
    const source = existsSync(contractsPath) ? readFileSync(contractsPath, "utf-8") : "";
    const expectErrorCount = (source.match(/@ts-expect-error/g) ?? []).length;

    console.log(
      `AC7 CHECK: FAIL — expecting >=3 @ts-expect-error directives pinning vault_id / is_default / required Vault fields, measured ${expectErrorCount}`
    );
    expect(expectErrorCount).toBeGreaterThanOrEqual(3);
    expect(source).toMatch(/vault_id/);
    expect(source).toMatch(/is_default/);
  });
});
