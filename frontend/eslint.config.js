import js from '@eslint/js';
import tseslint from 'typescript-eslint';
import reactHooks from 'eslint-plugin-react-hooks';
import jsxA11yX from 'eslint-plugin-jsx-a11y-x';
import globals from 'globals';

export default tseslint.config(
  { ignores: ['dist/**', 'node_modules/**', 'coverage/**'] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ['src/**/*.{ts,tsx}'],
    languageOptions: {
      globals: { ...globals.browser, ...globals.es2020 },
    },
    plugins: { 'react-hooks': reactHooks, 'jsx-a11y-x': jsxA11yX },
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
      // autoFocus on auth/setup forms is intentional UX (FolderTree,
      // ChangePasswordRequiredPage, KMSPage, LoginPage, RegisterPage,
      // SetupPage). Removing it would degrade first-paint UX on those forms.
      'jsx-a11y-x/no-autofocus': 'off',
      // shadcn/ui primitives spread {...props} so the AST checker can't see
      // the children/htmlFor callers attach. False positive at the primitive
      // definition site (card.tsx CardTitle, label.tsx Label).
      'jsx-a11y-x/heading-has-content': 'off',
      'jsx-a11y-x/label-has-associated-control': 'off',
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
    },
  },
);
