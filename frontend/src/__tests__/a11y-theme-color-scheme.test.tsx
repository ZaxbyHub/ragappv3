// frontend/src/__tests__/a11y-theme-color-scheme.test.tsx
// Issue #1159 AC1 (frozen acceptance check C1) — every theme block in the
// global stylesheet declares a color-scheme, so native UA controls and
// scrollbars follow the active palette.
//
// jsdom does not implement color-scheme in getComputedStyle, so the check
// parses the stylesheet source directly: it extracts each theme block
// (`:root`, `.dark`, `.high-contrast`) by brace matching and asserts the
// declaration and its expected value inside that block. jsdom cannot exercise
// applied color-scheme behavior at all; the declaration itself is the pinned
// contract here (no Playwright leg carries a computed colorScheme assertion).
//
// Expected RED at base: the blocks parse but contain no color-scheme
// declaration ("No test files found" is the frozen base signature — this
// spec is net-new with the fix).

import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const cssPath = resolve(__dirname, "../index.css");

/** Extract a top-level block's body for a given selector prefix. */
function blockBody(source: string, selector: string): string {
  // Line-anchored: the theme blocks are standalone rules at line start
  // (modulo indent) inside @layer base — never `.dark .foo` compounds or
  // prose mentions, which an unanchored indexOf would match first.
  const pattern = new RegExp(`(^|\\n)[ \\t]*${selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}[ \\t]*\\{`);
  const match = pattern.exec(source);
  if (!match) throw new Error(`theme block ${selector} not found in index.css`);
  const open = source.indexOf("{", match.index);
  let depth = 0;
  for (let i = open; i < source.length; i++) {
    if (source[i] === "{") depth++;
    if (source[i] === "}") {
      depth--;
      if (depth === 0) return source.slice(open + 1, i);
    }
  }
  throw new Error(`unbalanced braces in block ${selector}`);
}

describe("a11y theme color-scheme (#1159 AC1)", () => {
  const css = readFileSync(cssPath, "utf-8");

  it(":root (light theme) declares color-scheme: light", () => {
    expect(blockBody(css, ":root")).toMatch(/color-scheme:\s*light\s*;/);
  });

  it(".dark theme declares color-scheme: dark", () => {
    expect(blockBody(css, ".dark")).toMatch(/color-scheme:\s*dark\s*;/);
  });

  it(".high-contrast theme declares color-scheme: light", () => {
    // high-contrast is light-based (pure-white surfaces, black text).
    expect(blockBody(css, ".high-contrast")).toMatch(/color-scheme:\s*light\s*;/);
  });

  it("no theme block declares a foreign color-scheme value", () => {
    const root = blockBody(css, ":root");
    const dark = blockBody(css, ".dark");
    const hc = blockBody(css, ".high-contrast");
    const declared = [root, dark, hc]
      .flatMap((body) => body.match(/color-scheme:\s*([^;]+);/g) ?? [])
      .map((decl) => decl.replace(/color-scheme:\s*|;$/g, "").trim());
    expect(declared).toEqual(["light", "dark", "light"]);
  });
});
