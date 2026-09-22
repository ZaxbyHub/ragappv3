# docs: capture zustand 5 useShallow and vite/vitest lockfile conventions

## What changed
Added a "State selectors and zustand upgrades" section to `docs/engineering/conventions.md` documenting three hard-won lessons from the Phase 5 dependabot cleanup:

- Zustand 5 silently ignores the `equalityFn` second argument to `useStore`; array/object selectors must use `useStore(useShallow(selector))` from `zustand/shallow` to avoid React "Maximum update depth exceeded" loops.
- Dependency lockfiles must be regenerated with the same Node release as CI (the exact version lives in `.github/workflows/ci.yml` under `setup-node` and is enforced by `scripts/check_runtime_contract.py`) so the bundled npm version matches CI; lockfiles produced by a different npm may be rejected by CI.
- Keep the top-level `vite` dependency aligned with the `vitest` range (check `frontend/package.json`); the Frontend job's toolchain-graph step fails the build when they disagree.

## Why
These patterns caused real CI failures on dependabot PRs #320 and #332. Capturing them in the engineering conventions prevents regressions when future dependency upgrades touch the frontend store or build toolchain.

## Migration
No migration required.

## Caveats
- This is a documentation-only change; no runtime behavior is affected.
- The existing frontend code already follows these conventions.
