import { describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

/**
 * Alias grammar loads dedupe to one module import (issue #640, AC11).
 *
 * Pre-#640, every alias key in GRAMMAR_LOADERS carried its own arrow-function
 * literal (`js: () => import("@shikijs/langs/javascript")` etc.), so the
 * `loaded` Set — keyed by loader REFERENCE — never deduplicated aliases:
 * highlighting `js` then `mjs` imported the javascript grammar chunk twice.
 * The fix gives each base grammar ONE module-level loader const shared by
 * all its aliases; pinned here three ways: the observed import count, the
 * exported map's function identities, and the source shape.
 *
 * Mocking style follows highlighter.lazy-langs.test.ts: the factory counts
 * invocations AND returns the real module so the positive path is exercised.
 */

const loads = vi.hoisted(() => ({ javascript: 0 }));

vi.mock("@shikijs/langs/javascript", async () => {
  loads.javascript += 1;
  return await vi.importActual("@shikijs/langs/javascript");
});

import { loadHighlighter, GRAMMAR_LOADERS } from "@/lib/highlighter";

const tokenSpanCount = (html: string) => (html.match(/style="color:/g) || []).length;

describe("alias grammar loads share one module import (AC11 #640)", () => {
  it(
    "highlighting js then mjs imports @shikijs/langs/javascript exactly once",
    async () => {
      const hl = await loadHighlighter();

      const viaJs = await hl("const x = 1;", "js");
      const viaMjs = await hl("export const y = 2;", "mjs");

      expect(tokenSpanCount(viaJs), "js: real grammar output expected").toBeGreaterThan(0);
      expect(tokenSpanCount(viaMjs), "mjs: real grammar output expected").toBeGreaterThan(0);
      expect(loads.javascript, "two aliases must share ONE grammar module load").toBe(1);
    },
    60000,
  );

  it("GRAMMAR_LOADERS alias entries reference identical loader identities", () => {
    expect(GRAMMAR_LOADERS.js).toBe(GRAMMAR_LOADERS.javascript);
    expect(GRAMMAR_LOADERS.cjs).toBe(GRAMMAR_LOADERS.javascript);
    expect(GRAMMAR_LOADERS.mjs).toBe(GRAMMAR_LOADERS.javascript);
    expect(GRAMMAR_LOADERS.ts).toBe(GRAMMAR_LOADERS.typescript);
    expect(GRAMMAR_LOADERS.cts).toBe(GRAMMAR_LOADERS.typescript);
    expect(GRAMMAR_LOADERS.mts).toBe(GRAMMAR_LOADERS.typescript);
    expect(GRAMMAR_LOADERS.py).toBe(GRAMMAR_LOADERS.python);
    expect(GRAMMAR_LOADERS.sh).toBe(GRAMMAR_LOADERS.bash);
    expect(GRAMMAR_LOADERS.shell).toBe(GRAMMAR_LOADERS.bash);
    expect(GRAMMAR_LOADERS.zsh).toBe(GRAMMAR_LOADERS.bash);
    expect(GRAMMAR_LOADERS.shellscript).toBe(GRAMMAR_LOADERS.bash);
    expect(GRAMMAR_LOADERS.yml).toBe(GRAMMAR_LOADERS.yaml);
    expect(GRAMMAR_LOADERS.md).toBe(GRAMMAR_LOADERS.markdown);
    expect(GRAMMAR_LOADERS.rs).toBe(GRAMMAR_LOADERS.rust);
    expect(GRAMMAR_LOADERS["c++"]).toBe(GRAMMAR_LOADERS.cpp);
    expect(GRAMMAR_LOADERS.cs).toBe(GRAMMAR_LOADERS.csharp);
    expect(GRAMMAR_LOADERS["c#"]).toBe(GRAMMAR_LOADERS.csharp);
  });

  it("the source defines one import literal per base grammar, not per alias", () => {
    const source = readFileSync(resolve(__dirname, "highlighter.ts"), "utf-8");
    // 20 supported base grammars -> exactly 20 loader consts. The per-alias
    // inline literals this test replaces would count 37.
    expect((source.match(/\(\)\s*=>\s*import\("@shikijs\/langs\//g) || []).length).toBe(20);
  });
});
