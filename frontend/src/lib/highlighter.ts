// Shared syntax highlighter for chat code blocks and canvas previews (issue #572).
//
// Fine-grained shiki bundle: `shiki/core` + the JavaScript regex engine +
// explicit per-language grammar imports. This replaces the two former
// full-bundle `createHighlighter` loaders (MarkdownMessage.tsx and
// CanvasPreview.tsx), which (a) requested a fixed 21-language list on the
// first code block of any language — fetching every grammar chunk together —
// and (b) made the build ship chunks for all ~200 bundled grammars plus the
// 622 kB oniguruma wasm. A grammar module now loads only when a rendered code
// fence actually uses its language.
import type { HighlighterCore } from "shiki/core";

export type HighlightFn = (code: string, lang: string) => Promise<string>;

/** Structural shape shared by every @shikijs/langs grammar module. */
type GrammarModule = { default: Parameters<HighlighterCore["loadLanguage"]>[0] };

const THEME_LOADERS = {
  "github-light": () => import("@shikijs/themes/github-light"),
  "github-dark": () => import("@shikijs/themes/github-dark"),
} as const;

type ThemeName = keyof typeof THEME_LOADERS;

// One loader per supported grammar. Keys are the 20 supported language names
// plus every alias shiki itself resolves to them (enumerated from
// `bundledLanguages` loader identity — see the issue #572 trace); fences using
// an alias load the same grammar module as the base name.
const GRAMMAR_LOADERS: Record<string, () => Promise<GrammarModule>> = {
  javascript: () => import("@shikijs/langs/javascript"),
  js: () => import("@shikijs/langs/javascript"),
  cjs: () => import("@shikijs/langs/javascript"),
  mjs: () => import("@shikijs/langs/javascript"),
  typescript: () => import("@shikijs/langs/typescript"),
  ts: () => import("@shikijs/langs/typescript"),
  cts: () => import("@shikijs/langs/typescript"),
  mts: () => import("@shikijs/langs/typescript"),
  tsx: () => import("@shikijs/langs/tsx"),
  jsx: () => import("@shikijs/langs/jsx"),
  python: () => import("@shikijs/langs/python"),
  py: () => import("@shikijs/langs/python"),
  bash: () => import("@shikijs/langs/bash"),
  sh: () => import("@shikijs/langs/bash"),
  shell: () => import("@shikijs/langs/bash"),
  zsh: () => import("@shikijs/langs/bash"),
  shellscript: () => import("@shikijs/langs/bash"),
  json: () => import("@shikijs/langs/json"),
  yaml: () => import("@shikijs/langs/yaml"),
  yml: () => import("@shikijs/langs/yaml"),
  toml: () => import("@shikijs/langs/toml"),
  css: () => import("@shikijs/langs/css"),
  html: () => import("@shikijs/langs/html"),
  xml: () => import("@shikijs/langs/xml"),
  markdown: () => import("@shikijs/langs/markdown"),
  md: () => import("@shikijs/langs/markdown"),
  sql: () => import("@shikijs/langs/sql"),
  rust: () => import("@shikijs/langs/rust"),
  rs: () => import("@shikijs/langs/rust"),
  go: () => import("@shikijs/langs/go"),
  java: () => import("@shikijs/langs/java"),
  c: () => import("@shikijs/langs/c"),
  cpp: () => import("@shikijs/langs/cpp"),
  "c++": () => import("@shikijs/langs/cpp"),
  csharp: () => import("@shikijs/langs/csharp"),
  cs: () => import("@shikijs/langs/csharp"),
  "c#": () => import("@shikijs/langs/csharp"),
};

let _highlightFn: HighlightFn | null = null;
let _highlightPromise: Promise<HighlightFn> | null = null;

/**
 * Escapes for HTML element-content context only (&, <, >). Not safe for
 * attribute contexts (quotes are not escaped) — do not reuse in attributes.
 */
function escapeCodeHtml(code: string) {
  return code
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

export function renderPlainCodeHtml(code: string) {
  return `<pre><code>${escapeCodeHtml(code)}</code></pre>`;
}

function currentTheme(): ThemeName {
  return document.documentElement.classList.contains("dark") ? "github-dark" : "github-light";
}

export function loadHighlighter(): Promise<HighlightFn> {
  if (_highlightFn) return Promise.resolve(_highlightFn);
  if (_highlightPromise) return _highlightPromise;

  _highlightPromise = (async () => {
    try {
      const [core, engine, light, dark] = await Promise.all([
        import("shiki/core"),
        import("shiki/engine/javascript"),
        THEME_LOADERS["github-light"](),
        THEME_LOADERS["github-dark"](),
      ]);
      const hl: HighlighterCore = await core.createHighlighterCore({
        themes: [light, dark],
        langs: [],
        engine: engine.createJavaScriptRegexEngine({ forgiving: true }),
      });
      // Tracks loader function REFERENCES (not fence keys) so aliases of the
      // same grammar (js/cjs/mjs, sh/shell/zsh, ...) share one entry, and
      // uses hasOwn so fence tags like "constructor" or "__proto__" resolve
      // as unknown languages instead of hitting Object.prototype members.
      const loaded = new Set<() => Promise<GrammarModule>>();
      const fn: HighlightFn = async (code, lang) => {
        const key = lang.trim().toLowerCase();
        const loader = Object.prototype.hasOwnProperty.call(GRAMMAR_LOADERS, key)
          ? GRAMMAR_LOADERS[key]
          : undefined;
        if (loader && !loaded.has(loader)) {
          try {
            await hl.loadLanguage((await loader()).default);
            loaded.add(loader);
          } catch {
            // Grammar chunk failed to fetch/parse — degrade to themed plain
            // text rather than unstyled HTML; the next call retries the load.
            return hl.codeToHtml(code, { lang: "text", theme: currentTheme() });
          }
        }
        if (!loader) {
          // Unknown language — fall back to plain text highlighting.
          return hl.codeToHtml(code, { lang: "text", theme: currentTheme() });
        }
        // Theme is read after any grammar await so a mid-load theme toggle is
        // reflected by this render.
        return hl.codeToHtml(code, { lang: key, theme: currentTheme() });
      };
      _highlightFn = fn;
      return fn;
    } catch {
      // Shiki unavailable — return no-op so code still renders as plain text.
      const fn: HighlightFn = (code) => Promise.resolve(renderPlainCodeHtml(code));
      _highlightFn = fn;
      return fn;
    }
  })();

  return _highlightPromise;
}
