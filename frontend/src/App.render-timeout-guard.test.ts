import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

// Issue #657 structural guard: the full-App render specs
// (src/App.subpath.test.tsx, src/App.draft-room.test.tsx) must always carry an
// explicit per-suite timeout budget. Vitest's 5000ms default is below their
// observed full-suite-loaded cost on slow hosts, so a spec that loses its
// budget re-enters the intermittent Windows-only timeout class
// (docs/engineering/testing.md, "Windows host test baseline").
//
// Two layers:
//  1. Budget assertions — every top-level `describe(` in the two spec files
//     must pass an options object as the SECOND argument with
//     `timeout >= 15_000`. The arg-position check here is what closes the
//     `describe(name, fn, { timeout })` false-accept: this guard's own parsing
//     rejects that shape. (`npm run typecheck` does NOT backstop placement —
//     tsconfig excludes test files — though the misplaced object-third-arg
//     shape is a type error wherever typecheck does run.)
//  2. Class tripwire — every test file under src/ that renders the full
//     `<App />` must carry at least one explicit `timeout:` budget token, so a
//     future whole-app render spec cannot silently re-enter the class.
//     Known limits (recorded in the trace): the tripwire proves a budget
//     token is present per file — not its per-test placement, and not that the
//     token is live code rather than a comment; per-test placement is
//     enforced by this guard's layer 1 on the named specs and by vitest's
//     own runtime watchdog everywhere else.

const SPEC_FILES = ["src/App.subpath.test.tsx", "src/App.draft-room.test.tsx"];
const MIN_BUDGET_MS = 15_000;
const SELF = "src/App.render-timeout-guard.test.ts";

interface DescribeCall {
  line: number;
  optionsArg: string | null;
  body: string;
}

// Scan `source` for column-0 `describe(` calls. Returns each call's line
// number and its SECOND top-level argument text (the options slot), or null
// when that slot is not an object literal. Fails closed: unbalanced parens
// produce truncated args, which the assertions below reject.
function parseTopLevelDescribes(source: string): DescribeCall[] {
  const lines = source.split("\n");
  const calls: DescribeCall[] = [];
  for (let i = 0; i < lines.length; i++) {
    if (!/^describe\s*\(/.test(lines[i])) continue;
    // Walk characters from the opening paren, honoring strings, template
    // literals, and comments, until the matching close paren.
    let depth = 0;
    let inString: string | null = null;
    let inLineComment = false;
    let inBlockComment = false;
    let end = -1;
    const start = lines[i].indexOf("(");
    for (let j = i; j < lines.length; j++) {
      const text = j === i ? lines[j].slice(start) : lines[j];
      for (let k = 0; k < text.length; k++) {
        const ch = text[k];
        const next = text[k + 1];
        if (inLineComment) continue;
        if (inBlockComment) {
          if (ch === "*" && next === "/") {
            inBlockComment = false;
            k++;
          }
          continue;
        }
        if (inString) {
          if (ch === "\\") {
            k++;
          } else if (inString === "`" && ch === "$" && next === "{") {
            // Template expression: not tracked beyond string exit; the spec
            // files under guard contain no nested templates today.
          } else if (ch === inString) {
            inString = null;
          }
          continue;
        }
        if (ch === "/" && next === "/") {
          inLineComment = true;
          k++;
          continue;
        }
        if (ch === "/" && next === "*") {
          inBlockComment = true;
          k++;
          continue;
        }
        if (ch === "'" || ch === '"' || ch === "`") {
          inString = ch;
          continue;
        }
        if (ch === "(") depth++;
        if (ch === ")") {
          depth--;
          if (depth === 0) {
            end = j;
            break;
          }
        }
      }
      // Line comments cannot span lines, and the per-line `text` above
      // carries no newline character, so the flag must be cleared here.
      inLineComment = false;
      if (end !== -1) break;
    }
    if (end === -1) {
      calls.push({ line: i + 1, optionsArg: null, body: "" });
      continue;
    }
    const inner = lines.slice(i, end + 1).join("\n").slice(start + 1, -1);
    // Split top-level arguments on commas at paren/brace/bracket depth 0,
    // outside strings.
    const args: string[] = [];
    let argDepth = 0;
    let current = "";
    inString = null;
    for (const ch of inner) {
      if (inString) {
        current += ch;
        if (ch === inString) inString = null;
        continue;
      }
      if (ch === "'" || ch === '"' || ch === "`") {
        inString = ch;
        current += ch;
        continue;
      }
      if (ch === "(" || ch === "[" || ch === "{") argDepth++;
      if (ch === ")" || ch === "]" || ch === "}") argDepth--;
      if (ch === "," && argDepth === 0) {
        args.push(current);
        current = "";
        continue;
      }
      current += ch;
    }
    args.push(current);
    const optionsArg = args.length >= 3 && args[1].trim().startsWith("{") ? args[1] : null;
    calls.push({ line: i + 1, optionsArg, body: inner });
  }
  return calls;
}

function* walkTestFiles(dir: string): Generator<string> {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    const stat = statSync(full);
    if (stat.isDirectory()) {
      yield* walkTestFiles(full);
    } else if (/\.(test|spec)\.(ts|tsx|mjs|cjs|js)$/.test(entry)) {
      yield full;
    }
  }
}

describe("App render timeout guard", { timeout: 30_000 }, () => {
  it("every top-level describe in the full-App render specs carries an explicit budget >= 15000", () => {
    for (const file of SPEC_FILES) {
      const source = readFileSync(file, "utf-8");
      const describes = parseTopLevelDescribes(source);
      expect(
        describes.length,
        `${file} must contain top-level describe blocks (guard cannot verify an empty file)`,
      ).toBeGreaterThan(0);
      for (const call of describes) {
        expect(
          /\b(it|test)\s*\(/.test(call.body),
          `${file}:${call.line} describe must contain at least one it()/test() — a budgeted but empty describe guards nothing`,
        ).toBe(true);
        expect(
          call.optionsArg,
          `${file}:${call.line} describe must pass an options object as its second argument with an explicit timeout`,
        ).not.toBeNull();
        const match = /timeout\s*:\s*([0-9_]+)/.exec(call.optionsArg ?? "");
        expect(
          match,
          `${file}:${call.line} describe options must include timeout: N`,
        ).not.toBeNull();
        const value = Number((match?.[1] ?? "0").replace(/_/g, ""));
        expect(
          value,
          `${file}:${call.line} describe timeout budget must be >= ${MIN_BUDGET_MS}ms`,
        ).toBeGreaterThanOrEqual(MIN_BUDGET_MS);
      }
    }
  });

  it("every test file rendering the full App carries an explicit timeout budget (class tripwire)", () => {
    const offenders: string[] = [];
    for (const file of walkTestFiles("src")) {
      const normalized = file.replace(/\\/g, "/");
      if (normalized === SELF) continue;
      const source = readFileSync(file, "utf-8");
      if (!/render\(\s*<App\b/.test(source)) continue;
      const budget = /timeout\s*:\s*([0-9_]+)/.exec(source);
      const value = budget ? Number(budget[1].replace(/_/g, "")) : 0;
      if (!budget || value < MIN_BUDGET_MS) {
        offenders.push(`${normalized} (budget: ${budget ? value : "none"})`);
      }
    }
    expect(
      offenders,
      `full-App render specs without an explicit timeout budget >= ${MIN_BUDGET_MS}ms: ${offenders.join("; ")}`,
    ).toEqual([]);
  });
});
