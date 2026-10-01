// frontend/src/tests/dynamicImportRejection.a07.guardrail.test.ts
// Issue #689 recurrence guardrail (TQ-sibling-batch-01-06): every dynamic
// import() promise CHAIN in production code must carry a rejection handler,
// so a failed chunk load can never leave a spinner mounted forever.
// Source-scan guardrail: RED on the pre-fix tree (MermaidDiagram.tsx's
// import("mermaid").then(...) had no .catch), GREEN on the fixed tree.
import { describe, it, expect } from "vitest";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";

const SRC_ROOT = join(__dirname, "..");

function collectFiles(dir: string, out: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    if (/\.test\.[jt]sx?$/.test(entry) || entry === "test") continue;
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) {
      collectFiles(full, out);
    } else if (/\.(tsx|ts)$/.test(entry)) {
      out.push(full);
    }
  }
  return out;
}

/** Extract `import(...).then(...)` statement chains and report the ones whose
 * statement (up to the terminating `;` at matching paren depth) lacks `.catch`. */
function unhandledImportThenChains(source: string): number {
  let unhandled = 0;
  const marker = /import\([^)]*\)\s*\.\s*then\s*\(/g;
  let m: RegExpExecArray | null;
  while ((m = marker.exec(source)) !== null) {
    // Walk to the end of the statement: track paren depth from the .then(
    let i = source.indexOf("(", m.index + m[0].length - 1);
    let depth = 0;
    let sawCatch = false;
    for (; i < source.length && i < m.index + 4000; i++) {
      const ch = source[i];
      if (ch === "(") depth++;
      else if (ch === ")") {
        depth--;
        if (depth === 0) {
          // After the chain closes, look ahead for .catch before the `;`
          const rest = source.slice(i, i + 200);
          const semi = rest.indexOf(";");
          const segment = semi >= 0 ? rest.slice(0, semi) : rest;
          if (/\.\s*catch\s*\(/.test(segment)) sawCatch = true;
          break;
        }
      }
    }
    if (!sawCatch) unhandled++;
  }
  return unhandled;
}

describe("dynamic import rejection guardrail (issue #689)", () => {
  it("every import(...).then( chain in production source carries a .catch", () => {
    const offenders: string[] = [];
    for (const file of collectFiles(SRC_ROOT)) {
      const source = readFileSync(file, "utf8");
      const count = unhandledImportThenChains(source);
      if (count > 0) offenders.push(`${file} (${count} chain(s))`);
    }
    expect(
      offenders,
      `dynamic import chains without a rejection handler:\n${offenders.join("\n")}`
    ).toEqual([]);
  });
});
