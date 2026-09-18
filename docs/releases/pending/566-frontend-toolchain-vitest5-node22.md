# Frontend toolchain: Vitest 5, warning-clean native Vite config loader (#566)

## Summary

The frontend test toolchain moves from vitest 4.1.11 to vitest 5.0.0 (with
`@vitest/coverage-v8` in lockstep, superseding dependabot #544), and
`frontend/vite.config.ts` is now fully compatible with Vite's native config
loader: every `npm run build` stops printing the
`configLoader: 'native'` deprecation warning that previously fired on every
build naming `__dirname` (vite.config.ts:16) and the extensionless
`./vite.paths` import (vite.config.ts:5). The issue's third leg — raising the
Node floor — was already landed by #258 (engines >=22.14.0, CI pinned to
22.14.0) and is untouched here; vitest 5's Node >=22 / Vite >=6.4 floors were
already satisfied (vite stays 8.2.2).

The scope correction over the issue text: a fresh build at the base revision
shows the resolved Vite 8.2.2 flags BOTH the `__dirname` use AND the
extensionless import (the issue and its verification comment expected only
the former). Isolation probes proved each trigger fires on its own, so
clearing the warning requires both one-token changes:
`path.resolve(import.meta.dirname, './src')` and `from './vite.paths.ts'`.

## Toolchain changes

| Piece | Before | After |
|---|---|---|
| `vitest` | `~4.1.11` | `~5.0.0` (lock resolves 5.0.0 — dependabot #544's reviewed target) |
| `@vitest/coverage-v8` | `^4.1.11` | `^5.0.0` (lock resolves 5.0.0, in lockstep) |
| `vite` | `^8.2.2` | unchanged (satisfies vitest 5's `>=6.4` peer floor) |
| `vite.config.ts` alias | `path.resolve(__dirname, './src')` | `path.resolve(import.meta.dirname, './src')` |
| `vite.config.ts` import | `'./vite.paths'` | `'./vite.paths.ts'` |
| `tsconfig.node.json` | — | `allowImportingTsExtensions` + `emitDeclarationOnly` + `outDir: "dist-dts"` (keeps the editor/`tsc -b` view of the config compile-clean with the `.ts`-extension import; nothing in CI compiles this project) |

`@stryker-mutator/vitest-runner@9.6.1` declares peer `vitest >=2.0.0`, which
5.0.0 satisfies; the nightly Stryker gate (`nightly-quality-gates.yml`) is the
designated runtime detector for that pairing.

## Guardrail

New `backend/tests/test_issue566_config_loader_native.py` (runs in the Backend
CI job) source-inspects the config-loaded files (`vite.config.ts`,
`vite.paths.ts`) and fails if a CommonJS-only global
(`__dirname`/`__filename`/`require(`) or an extensionless relative import is
reintroduced — the same source-inspection-contract shape as the #258 coverage
design pins. Demonstrated RED on the pre-fix config and GREEN on the fix.

## Contributor impact and rollback

- Contributors see no workflow change: `npm ci --engine-strict` under Node
  >=22.14 installs the new pair; all seven frontend gates
  (typecheck, typecheck:contracts, lint, test, test:a11y, test:coverage,
  build) run as before. Vitest 5's breaking changes (default mock clearing,
  stricter hoisting, awaited-assertion enforcement, coverage glob semantics)
  were swept against this codebase and the full suite passes unchanged.
- In-flight frontend PRs may need a rebase against the lockfile churn.
- **Rollback is one unit:** revert the whole change (vite.config.ts,
  package.json, package-lock.json, tsconfig.node.json, .gitignore, guardrail
  test, this note) together. Reverting subsets is also safe in every
  combination here because the Node pin is untouched; the issue's only
  forbidden combination (Node down while keeping vitest 5) cannot arise.

## Coverage-threshold policy under the new provider

Vitest 5's v8 coverage provider re-measures the scoped gate in
`vite.config.ts` (`src/lib/api/**` + `src/stores/**`, thresholds
56/59/50/58). The thresholds pass unchanged under 5.0.0 at the time of
landing. If a future re-measure ever drops below the documented floors, the
floors (>=50) and both include scopes are pinned by
`backend/tests/test_issue258_coverage_design.py` and must not be waived —
recalibrate per #258's documented rationale (measured baseline minus a ~2pt
margin), recording the new measured baseline in the `vite.config.ts` comment.
