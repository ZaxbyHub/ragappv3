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

// One loader const per BASE grammar, shared by every alias spelling: each
// alias entry below references the SAME function identity, so highlighting
// `js` after `javascript` (or `mjs`, `cjs`, ...) triggers exactly one
// @shikijs/langs/javascript import (#640 AC11 — previously every alias had
// its own arrow-function literal, defeating the `loaded` Set's
// reference-based dedupe). Keys are the 20 supported language names plus
// every alias shiki itself resolves to them (enumerated from
// `bundledLanguages` loader identity — see the issue #572 trace).
const loadJavascript = () => import("@shikijs/langs/javascript");
const loadTypescript = () => import("@shikijs/langs/typescript");
const loadTsx = () => import("@shikijs/langs/tsx");
const loadJsx = () => import("@shikijs/langs/jsx");
const loadPython = () => import("@shikijs/langs/python");
const loadBash = () => import("@shikijs/langs/bash");
const loadJson = () => import("@shikijs/langs/json");
const loadYaml = () => import("@shikijs/langs/yaml");
const loadToml = () => import("@shikijs/langs/toml");
const loadCss = () => import("@shikijs/langs/css");
const loadHtml = () => import("@shikijs/langs/html");
const loadXml = () => import("@shikijs/langs/xml");
const loadMarkdown = () => import("@shikijs/langs/markdown");
const loadSql = () => import("@shikijs/langs/sql");
const loadRust = () => import("@shikijs/langs/rust");
const loadGo = () => import("@shikijs/langs/go");
const loadJava = () => import("@shikijs/langs/java");
const loadC = () => import("@shikijs/langs/c");
const loadCpp = () => import("@shikijs/langs/cpp");
const loadCsharp = () => import("@shikijs/langs/csharp");

export const GRAMMAR_LOADERS: Record<string, () => Promise<GrammarModule>> = {
  javascript: loadJavascript,
  js: loadJavascript,
  cjs: loadJavascript,
  mjs: loadJavascript,
  typescript: loadTypescript,
  ts: loadTypescript,
  cts: loadTypescript,
  mts: loadTypescript,
  tsx: loadTsx,
  jsx: loadJsx,
  python: loadPython,
  py: loadPython,
  bash: loadBash,
  sh: loadBash,
  shell: loadBash,
  zsh: loadBash,
  shellscript: loadBash,
  json: loadJson,
  yaml: loadYaml,
  yml: loadYaml,
  toml: loadToml,
  css: loadCss,
  html: loadHtml,
  xml: loadXml,
  markdown: loadMarkdown,
  md: loadMarkdown,
  sql: loadSql,
  rust: loadRust,
  rs: loadRust,
  go: loadGo,
  java: loadJava,
  c: loadC,
  cpp: loadCpp,
  "c++": loadCpp,
  csharp: loadCsharp,
  cs: loadCsharp,
  "c#": loadCsharp,
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
      // Tracks loader function REFERENCES (not fence keys). Every alias of a
      // base grammar shares ONE module-level loader const (see GRAMMAR_LOADERS),
      // so an alias hit after the base load is a Set hit and performs no
      // second import. hasOwn keeps fence tags like "constructor" or
      // "__proto__" resolving as unknown languages instead of hitting
      // Object.prototype members.
      const loaded = new Set<() => Promise<GrammarModule>>();
      // Negative cache (#640 AC10): a grammar chunk that fails to fetch/parse
      // is retried at most once more (2 total attempts), after which it
      // degrades to plain text WITHOUT re-importing for the rest of the page
      // session — callers re-run per render, so unbounded retries meant every
      // re-render of a failing fence hammered the network. A page reload
      // resets the cache (fresh chances after a deploy fix). A later success
      // clears the failure record.
      const failedLoads = new Map<() => Promise<GrammarModule>, number>();
      // Single-flight (PR #651 review F-001): concurrent callers of the same
      // grammar share one in-flight load, so the retry-cap accounting counts
      // ATTEMPTS, not concurrent callers racing past the cap check.
      const inFlightLoads = new Map<() => Promise<GrammarModule>, Promise<void>>();
      const MAX_GRAMMAR_LOAD_ATTEMPTS = 2;
      const fn: HighlightFn = async (code, lang) => {
        const key = lang.trim().toLowerCase();
        const loader = Object.prototype.hasOwnProperty.call(GRAMMAR_LOADERS, key)
          ? GRAMMAR_LOADERS[key]
          : undefined;
        if (loader && !loaded.has(loader)) {
          const attempts = failedLoads.get(loader) ?? 0;
          if (attempts >= MAX_GRAMMAR_LOAD_ATTEMPTS) {
            // Permanently cached failure for this page session — degrade
            // immediately, no import attempt.
            return hl.codeToHtml(code, { lang: "text", theme: currentTheme() });
          }
          const loadOnce = async (): Promise<void> => {
            try {
              await hl.loadLanguage((await loader()).default);
              loaded.add(loader);
              failedLoads.delete(loader);
            } catch (error: unknown) {
              failedLoads.set(loader, (failedLoads.get(loader) ?? 0) + 1);
              inFlightLoads.delete(loader);
              // Grammar chunk failed to fetch/parse — degrade to themed plain
              // text rather than unstyled HTML; at most one retry remains.
              throw error;
            }
          };
          const cached = inFlightLoads.get(loader);
          const pending: Promise<void> = cached ?? loadOnce();
          inFlightLoads.set(loader, pending);
          try {
            await pending;
          } catch {
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
