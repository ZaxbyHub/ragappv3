// frontend/src/tests/dynamicImportRejection.a07.guardrail.test.ts
// Issue #689 recurrence guardrail (TQ-sibling-batch-01-06): every dynamic
// import() promise CHAIN in production code must carry a rejection handler,
// so a failed chunk load can never leave a spinner mounted forever.
// Source-scan guardrail: RED on the pre-fix tree (MermaidDiagram.tsx's
// import("mermaid").then(...) had no .catch), GREEN on the fixed tree.
//
// Feedback round (PR #835 review PRR-005 / external F-6): the detector is a
// balanced-paren parser, not a line regex — parenthesized specifiers are
// matched, a two-argument .then(ok, err) counts as handled, .finally-only
// chains are flagged, and the rejection-handler window is the whole
// statement rather than a fixed char count. React.lazy and `await import()`
// are out of scope (different rejection surfaces); template literals
// containing parens/semicolons can confuse the statement scanner, which is
// an accepted heuristic for this guardrail.
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

/** Index of the paren matching the one at openIdx, or -1. */
function findMatchingParen(source: string, openIdx: number): number {
  let depth = 0;
  for (let i = openIdx; i < source.length; i++) {
    const ch = source[i];
    if (ch === "(") depth++;
    else if (ch === ")") {
      depth--;
      if (depth === 0) return i;
    }
  }
  return -1;
}

/** Does the argument list contain a comma at nesting depth 0? */
function hasTopLevelComma(args: string): boolean {
  let depth = 0;
  for (const ch of args) {
    if (ch === "(" || ch === "[" || ch === "{") depth++;
    else if (ch === ")" || ch === "]" || ch === "}") depth--;
    else if (ch === "," && depth === 0) return true;
  }
  return false;
}

/** End of the import chain's statement: the first `;` at paren/bracket/brace
 * depth 0 after the import call's closing paren (capped for safety). */
function statementEnd(source: string, from: number): number {
  let depth = 0;
  const limit = Math.min(source.length, from + 8000);
  for (let i = from; i < limit; i++) {
    const ch = source[i];
    if (ch === "(" || ch === "[" || ch === "{") depth++;
    else if (ch === ")" || ch === "]" || ch === "}") depth--;
    else if (ch === ";" && depth <= 0) return i;
  }
  return limit;
}

/** Count `import(...)` statement chains whose member chain (before the
 * statement's terminating `;`) carries no rejection handler: no `.catch`
 * link, every `.then` single-argument, and not a bare `.finally`-only
 * chain (a `.finally` after a handled chain is fine; a `.finally` as the
 * ONLY link still leaks the rejection). */
export function countUnhandledImportChains(source: string): number {
  let unhandled = 0;
  let searchFrom = 0;
  for (;;) {
    const importIdx = source.indexOf("import(", searchFrom);
    if (importIdx < 0) break;
    searchFrom = importIdx + 7;
    const openParen = importIdx + "import(".length - 1;
    const closeParen = findMatchingParen(source, openParen);
    if (closeParen < 0) continue;
    const end = statementEnd(source, closeParen + 1);
    const chain = source.slice(closeParen + 1, end);
    // Scope: only statement chains that start a member call on the import
    // promise itself (`import(...).then(...)...`). Rejections from other
    // shapes are handled by their own conventions a statement scanner
    // cannot verify — React.lazy(() => import(...)) (ErrorBoundary) and
    // `await import(...)` (enclosing try/catch). External F-6 reviewed and
    // accepted exactly this scope for the Mermaid defect class.
    if (!/^\s*\.\s*(then|catch|finally)/.test(chain)) {
      searchFrom = end + 1;
      continue;
    }
    const hasCatch = /\.catch\s*\(/.test(chain);
    // Every .then link must be two-argument for the chain to be handled
    // without a .catch.
    const thenArgs: string[] = [];
    const thenRegex = /\.then\s*\(/g;
    let m: RegExpExecArray | null;
    while ((m = thenRegex.exec(chain)) !== null) {
      const tOpen = m.index + m[0].length - 1;
      const tClose = findMatchingParen(chain, tOpen);
      if (tClose < 0) break;
      thenArgs.push(chain.slice(tOpen + 1, tClose));
      thenRegex.lastIndex = tClose + 1;
    }
    const allThenTwoArg =
      thenArgs.length > 0 && thenArgs.every((a) => hasTopLevelComma(a));
    // A trailing .finally does NOT rescue an unhandled chain, and a
    // .finally as the ONLY link still leaks the rejection — neither counts
    // as a rejection handler (PRR-005).
    if (!(hasCatch || allThenTwoArg)) {
      unhandled++;
    }
    searchFrom = end + 1;
  }
  return unhandled;
}

describe("dynamic import rejection guardrail (issue #689)", () => {
  it("detector self-test: handles every chain shape it claims to", () => {
    expect(countUnhandledImportChains('import("a").then(ok);')).toBe(1);
    expect(countUnhandledImportChains('import("a").then(ok).catch(err);')).toBe(0);
    expect(countUnhandledImportChains('import("a").then(ok, err);')).toBe(0);
    expect(countUnhandledImportChains('import("a").then(ok).catch(err).finally(f);')).toBe(0);
    // Parenthesized specifiers must be matched (PRR-005 false negative).
    expect(countUnhandledImportChains('import(("a")).then(ok);')).toBe(1);
    expect(countUnhandledImportChains('import(("a")).then(ok).catch(err);')).toBe(0);
    // finally-only chains leak the rejection; a trailing finally does not
    // rescue an unhandled chain (PRR-005).
    expect(countUnhandledImportChains('import("a").finally(done);')).toBe(1);
    expect(countUnhandledImportChains('import("a").then(ok).finally(f);')).toBe(1);
    // Two-arg then counts as handled even with a trailing finally.
    expect(countUnhandledImportChains('import("a").then(ok, err).finally(f);')).toBe(0);
  });

  it("every import(...) chain in production source carries a rejection handler", () => {
    const offenders: string[] = [];
    for (const file of collectFiles(SRC_ROOT)) {
      const source = readFileSync(file, "utf8");
      const count = countUnhandledImportChains(source);
      if (count > 0) offenders.push(`${file} (${count} chain(s))`);
    }
    expect(
      offenders,
      `dynamic import chains without a rejection handler:\n${offenders.join("\n")}`
    ).toEqual([]);
  });
});
