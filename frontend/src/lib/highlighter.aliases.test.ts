// Alias-resolution lock for the shared highlighter (issue #572).
//
// The lazy GRAMMAR_LOADERS map must resolve every fence spelling shiki itself
// resolves for the supported languages — the 20 base names plus their 17
// runtime aliases (enumerated from bundledLanguages loader identity; see the
// #572 trace). A fence using an alias must produce real highlighted output,
// not the plain-text fallback. Bases without an alias are exercised by their
// base name so all 20 grammars are covered.
import { describe, expect, it } from "vitest";

import { loadHighlighter } from "@/lib/highlighter";

const SAMPLES: Record<string, string> = {
  javascript: "const x = 1;",
  js: "const x = 1;",
  cjs: "module.exports = 1;",
  mjs: "export const x = 1;",
  typescript: "const x: number = 1;",
  ts: "const x: number = 1;",
  cts: "const x: number = 1;",
  mts: "const x: number = 1;",
  tsx: "const el = <div />;",
  jsx: "const el = <div />;",
  python: "print('hello')",
  py: "print('hello')",
  bash: "echo hello",
  sh: "echo hello",
  shell: "echo hello",
  zsh: "echo hello",
  shellscript: "echo hello",
  json: '{"key": 1}',
  yaml: "key: value",
  yml: "key: value",
  toml: "key = 'value'",
  css: "body { color: red; }",
  html: "<p>hello</p>",
  xml: "<item>hello</item>",
  markdown: "# Heading",
  md: "# Heading",
  sql: "SELECT 1;",
  rust: "fn main() {}",
  rs: "fn main() {}",
  go: "func main() {}",
  java: "class A {}",
  c: "int main() { return 0; }",
  cpp: "int main() { return 0; }",
  "c++": "int main() { return 0; }",
  csharp: "class A {}",
  cs: "class A {}",
  "c#": "class A {}",
};

describe("highlighter alias resolution", () => {
  // Real grammars emit per-token `style="color:` spans; the themed
  // `lang:"text"` fallback emits shiki line wrappers but ZERO token-color
  // spans — so counting them distinguishes a real grammar match from the
  // fallback path (a deleted alias entry would otherwise pass silently).
  const tokenSpanCount = (html: string) => (html.match(/style="color:/g) || []).length;

  it(
    "every supported fence spelling highlights (base names and aliases)",
    { timeout: 120_000 },
    async () => {
      const hl = await loadHighlighter();
      for (const [lang, sample] of Object.entries(SAMPLES)) {
        const html = await hl(sample, lang);
        expect(
          tokenSpanCount(html),
          `${lang}: expected tokenized output (token-color spans > 0), got: ${html.slice(0, 160)}`,
        ).toBeGreaterThan(0);
      }
    },
  );

  it(
    "unknown languages render as themed plain text with no token spans",
    { timeout: 60_000 },
    async () => {
      const hl = await loadHighlighter();
      const html = await hl("plain words", "not-a-language");
      expect(html.includes("shiki")).toBe(true);
      expect(html).toContain("plain words");
      expect(tokenSpanCount(html)).toBe(0);
    },
  );
});
