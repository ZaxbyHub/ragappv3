import { beforeEach, describe, expect, it, vi } from "vitest";

/**
 * Lazy per-language grammar loading contract (issue #572, AC3).
 *
 * The shared highlighter (src/lib/highlighter.ts) must load a grammar
 * module only when that language is actually highlighted: loading and
 * highlighting a python fence must NOT pull in the cpp, csharp, java,
 * rust, or go grammar modules.
 *
 * vi.mock factories below record each grammar-module import. Mocking
 * works because the dynamic imports originate from a source file
 * (src/lib/highlighter.ts), not from externalized node_modules code.
 * The python mock records AND returns the real module so the positive
 * control exercises the real highlight path; the others return minimal
 * grammar stubs (recording is the assertion surface).
 */

const grammarLoads = vi.hoisted(() => [] as string[]);

vi.mock("@shikijs/langs/cpp", () => {
  grammarLoads.push("cpp");
  return { default: { name: "cpp", scopeName: "source.cpp", patterns: [] } };
});

vi.mock("@shikijs/langs/csharp", () => {
  grammarLoads.push("csharp");
  return { default: { name: "csharp", scopeName: "source.csharp", patterns: [] } };
});

vi.mock("@shikijs/langs/java", () => {
  grammarLoads.push("java");
  return { default: { name: "java", scopeName: "source.java", patterns: [] } };
});

vi.mock("@shikijs/langs/rust", () => {
  grammarLoads.push("rust");
  return { default: { name: "rust", scopeName: "source.rust", patterns: [] } };
});

vi.mock("@shikijs/langs/go", () => {
  grammarLoads.push("go");
  return { default: { name: "go", scopeName: "source.go", patterns: [] } };
});

vi.mock("@shikijs/langs/python", async () => {
  grammarLoads.push("python");
  return await vi.importActual("@shikijs/langs/python");
});

import { loadHighlighter } from "@/lib/highlighter";

describe("loadHighlighter lazy per-language grammar loading (AC3)", () => {
  // grammarLoads is a vi.hoisted module-scoped array; vite.config.ts's
  // clearMocks:true resets mock implementations but NOT hoisted arrays, so
  // without this reset a second test would inherit test 1's recorded loads
  // (#640 AC9 — the state leak the frozen C9 check pins).
  beforeEach(() => {
    grammarLoads.length = 0;
  });

  it(
    "highlights python without loading cpp/csharp/java/rust/go grammars",
    async () => {
      const hl = await loadHighlighter();

      const html = await hl("print('hi')", "python");
      expect(html).toContain("shiki");

      // Positive control: interception works (otherwise the negative
      // assertion below would be vacuous).
      expect(grammarLoads).toContain("python");

      // Negative assertion: unrelated heavy grammars were not requested.
      expect(grammarLoads).not.toContain("cpp");
      expect(grammarLoads).not.toContain("csharp");
      expect(grammarLoads).not.toContain("java");
      expect(grammarLoads).not.toContain("rust");
      expect(grammarLoads).not.toContain("go");

      // Second positive control: the cpp grammar loads only once cpp
      // is actually highlighted.
      await hl("int main(){}", "cpp");
      expect(grammarLoads).toContain("cpp");
    },
    60000,
  );

  it(
    "resets grammar state between tests (AC9 #640)",
    async () => {
      // The beforeEach reset means this second test starts from a clean
      // ledger even though the test above already recorded python + cpp —
      // without it, this assertion would see the previous test's loads.
      expect(grammarLoads).toEqual([]);

      const hl = await loadHighlighter();
      await hl('package main\nfunc main() {}', "go");

      // Only this test's own language is recorded — proving both the reset
      // (empty at start) and that lazy loading still records per language.
      expect(grammarLoads).toEqual(["go"]);
    },
    60000,
  );
});
