import { describe, expect, it } from "vitest";
import { render, waitFor } from "@testing-library/react";
import { MarkdownMessage } from "./MarkdownMessage";

/**
 * Preserving check for the supported-language list (issue #572, AC4).
 *
 * Renders one fenced code block per supported language through the REAL
 * highlighter path (no shiki mock) and asserts every language still
 * produces syntax-highlighted output: a .shiki-wrapper div containing
 * a <pre> with non-empty innerHTML.
 *
 * Green at HEAD (the full-bundle path highlights all 21) and must stay
 * green after the lazy fine-grained loader replaces it.
 */

const LANGUAGES = [
  "javascript", "typescript", "tsx", "jsx",
  "python", "bash", "sh", "json", "yaml", "toml",
  "css", "html", "xml", "markdown", "sql",
  "rust", "go", "java", "c", "cpp", "csharp",
] as const;

const SNIPPETS: Record<string, string> = {
  javascript: "const x = 1;",
  typescript: "const x: number = 1;",
  tsx: "const el = <div />;",
  jsx: "const el = <div />;",
  python: "print('hi')",
  bash: "echo done",
  sh: "echo done",
  json: '{"k": 1}',
  yaml: "key: value",
  toml: "key = 1",
  css: "a { color: red; }",
  html: "<p>hi</p>",
  xml: "<a>b</a>",
  markdown: "# Head",
  sql: "SELECT 1;",
  rust: "let x = 1;",
  go: "var x = 1",
  java: "int x = 1;",
  c: "int x = 1;",
  cpp: "int x = 1;",
  csharp: "int x = 1;",
};

function badgeFor(lang: string): HTMLElement | null {
  const badges = Array.from(document.querySelectorAll("span.font-mono"));
  return badges.find((b) => b.textContent === lang) ?? null;
}

describe("MarkdownMessage supported-language highlighting (AC4)", () => {
  it(
    "renders syntax-highlighted output for every supported language",
    async () => {
      const content = LANGUAGES.map(
        (lang) => "```" + lang + "\n" + SNIPPETS[lang] + "\n```",
      ).join("\n\n");

      const { unmount } = render(<MarkdownMessage content={content} />);

      await waitFor(() => {
        expect(document.querySelectorAll(".shiki-wrapper").length).toBe(
          LANGUAGES.length,
        );
      }, { timeout: 30000 });

      const badges = Array.from(document.querySelectorAll("span.font-mono"));
      expect(badges.filter((b) => b.textContent && LANGUAGES.includes(b.textContent as (typeof LANGUAGES)[number])).length).toBe(
        LANGUAGES.length,
      );

      for (const lang of LANGUAGES) {
        const badge = badgeFor(lang);
        expect(badge, `missing language badge for "${lang}"`).not.toBeNull();
        const block = badge?.parentElement?.parentElement ?? null;
        const wrapper = block?.querySelector(".shiki-wrapper") ?? null;
        expect(wrapper, `missing .shiki-wrapper for "${lang}"`).not.toBeNull();
        const pre = wrapper?.querySelector("pre") ?? null;
        expect(pre, `missing <pre> for "${lang}"`).not.toBeNull();
        expect(
          pre?.innerHTML.trim().length ?? 0,
          `empty highlighted output for "${lang}"`,
        ).toBeGreaterThan(0);
      }

      unmount();
    },
    120000,
  );
});
