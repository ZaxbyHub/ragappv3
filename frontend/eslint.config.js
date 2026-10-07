import js from '@eslint/js';
import tseslint from 'typescript-eslint';
import reactHooks from 'eslint-plugin-react-hooks';
import jsxA11yX from 'eslint-plugin-jsx-a11y-x';
import globals from 'globals';
import fs from 'node:fs';
import { fileURLToPath } from 'node:url';

// ---------------------------------------------------------------------------
// local/no-raw-palette (issue #776, UI-ENH-12): ban raw Tailwind palette
// classes (e.g. bg-amber-500) outside the design-system primitives. The app
// states color through semantic tokens (primary/success/warning/destructive/
// muted/...). The authoritative census is scripts/check_l05_raw_palette.py;
// this rule is the authoring-time guardrail.
//
// Allowlist contract (PRR-001): both consumers read
// frontend/raw-palette-allowlist.json, but NOT identically — the census
// honours BOTH keys with repo-root-relative entries ("frontend/src/…") while
// this rule consumes only whole-file `files` entries, normalized here from
// the documented repo-root form to flat-config form (flat-config globs
// resolve relative to frontend/, so a documented "frontend/src/X.tsx" entry
// would otherwise never match). Line-level `lines` entries are census-only:
// this rule cannot exempt a single line, so a `lines` exemption still fails
// lint by design. The census budget (10) is likewise looser than this rule's
// zero tolerance — both differences are intentional, not drift.
const RAW_PALETTE_ALLOWLIST_PATH = fileURLToPath(new URL('./raw-palette-allowlist.json', import.meta.url));
let rawPaletteAllowlistedFiles = [];
try {
  const parsed = JSON.parse(fs.readFileSync(RAW_PALETTE_ALLOWLIST_PATH, 'utf8'));
  const entries = Array.isArray(parsed.files) ? parsed.files : [];
  rawPaletteAllowlistedFiles = entries
    .map((entry) => String(entry).replace(/^frontend\//, ''))
    .filter((entry) => entry.startsWith('src/'));
  const dropped = entries.length - rawPaletteAllowlistedFiles.length;
  if (dropped > 0) {
    console.warn(`[local/no-raw-palette] ${dropped} raw-palette-allowlist "files" entr${dropped === 1 ? 'y' : 'ies'} not under frontend/src were ignored (entries must look like "frontend/src/...").`);
  }
} catch (error) {
  console.warn(`[local/no-raw-palette] raw-palette-allowlist.json unreadable (${error instanceof Error ? error.message : error}); running with no exemptions.`);
  rawPaletteAllowlistedFiles = [];
}

const RAW_PALETTE_RE =
  /(bg|text|border|ring|from|via|to|fill|stroke|outline|divide|placeholder)-[a-z]+-(?:50|100|200|300|400|500|600|700|800|900|950)(?![0-9])/g;

const noRawPaletteRule = {
  meta: {
    type: 'problem',
    docs: {
      description: 'Disallow raw Tailwind palette classes outside components/ui — use the semantic design tokens (issue #776 / UI-ENH-12).',
    },
    schema: [],
  },
  create(context) {
    const sourceCode = context.sourceCode ?? context.getSourceCode();
    function checkAttribute(node) {
      if (!node.name || node.name.name !== 'className') return;
      const text = sourceCode.getText(node);
      RAW_PALETTE_RE.lastIndex = 0;
      let match = RAW_PALETTE_RE.exec(text);
      while (match !== null) {
        context.report({
          node,
          message:
            'Raw Tailwind palette class "{{match}}" — use the semantic design tokens (primary/success/warning/destructive/…) instead. Allowlist: frontend/raw-palette-allowlist.json.',
          data: { match: match[0] },
        });
        match = RAW_PALETTE_RE.exec(text);
      }
    }
    return { JSXAttribute: checkAttribute };
  },
};

const localPlugin = { rules: { 'no-raw-palette': noRawPaletteRule } };
// ---------------------------------------------------------------------------

export default tseslint.config(
  { ignores: ['dist/**', 'node_modules/**', 'coverage/**'] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ['src/**/*.{ts,tsx}'],
    languageOptions: {
      globals: { ...globals.browser, ...globals.es2020 },
    },
    plugins: { 'react-hooks': reactHooks, 'jsx-a11y-x': jsxA11yX, local: localPlugin },
    rules: {
      ...reactHooks.configs.recommended.rules,
      'react-hooks/exhaustive-deps': 'error',
      'react-hooks/set-state-in-effect': 'off',
      'react-hooks/preserve-manual-memoization': 'off',
      'react-hooks/incompatible-library': 'off',
      'react-hooks/purity': 'off',
      'react-hooks/refs': 'off',
      '@typescript-eslint/no-unused-vars': 'off',
      '@typescript-eslint/no-explicit-any': 'off',
      'local/no-raw-palette': 'error',
    },
  },
  {
    // eslint-plugin-jsx-a11y-x — ESLint-10-compatible fork of
    // eslint-plugin-jsx-a11y (canonical 6.10.2 is incompatible with this
    // repo's ESLint ^10.7.0; see PR body on #408). Rule prefix is jsx-a11y-x/.
    files: ['src/**/*.{ts,tsx}'],
    plugins: { 'jsx-a11y-x': jsxA11yX },
    rules: {
      ...jsxA11yX.configs.recommended.rules,
      // Allow tabIndex on role="separator" — the APG window-splitter pattern
      // (ChatShell resize handles) requires a focusable separator with
      // arrow-key resize. Configured globally because the role is the
      // defining trait, not the call site.
      'jsx-a11y-x/no-noninteractive-tabindex': [
        'error',
        { roles: ['separator'] },
      ],
    },
  },
  {
    // Design-system primitives may keep palette classes (they define the
    // token-backed defaults everything else composes).
    files: ['src/components/ui/**/*.{ts,tsx}'],
    rules: { 'local/no-raw-palette': 'off' },
  },
  {
    files: ['src/**/*.{test,spec}.{ts,tsx}', 'src/test/**/*.{ts,tsx}', 'src/tests/**/*.{ts,tsx}'],
    rules: {
      '@typescript-eslint/no-explicit-any': 'off',
      '@typescript-eslint/no-non-null-assertion': 'off',
      // Test mocks and render fixtures don't need to mirror production a11y
      // semantics — disabled here so production rules stay strict.
      'jsx-a11y-x/click-events-have-key-events': 'off',
      'jsx-a11y-x/interactive-supports-focus': 'off',
      'jsx-a11y-x/no-static-element-interactions': 'off',
      'jsx-a11y-x/role-has-required-aria-props': 'off',
      'local/no-raw-palette': 'off',
    },
  },
  {
    // Whole-file exemptions from the shared allowlist (kept empty unless a
    // conversion is genuinely impossible; the census budget is 10).
    files: rawPaletteAllowlistedFiles.length > 0 ? rawPaletteAllowlistedFiles : ['src/__no-allowlist-entries__/**'],
    rules: { 'local/no-raw-palette': 'off' },
  },
);
