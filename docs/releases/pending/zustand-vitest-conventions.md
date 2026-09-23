# docs: capture zustand 5 useShallow and vite/vitest lockfile conventions

## What changed
Added a "State selectors and zustand upgrades" section to `docs/engineering/conventions.md` documenting three hard-won lessons from the Phase 5 dependabot cleanup:

- Zustand 5 silently ignores the `equalityFn` second argument to `useStore`; array/object selectors must use `useStore(useShallow(selector))` from `zustand/shallow` to avoid React "Maximum update depth exceeded" loops.
- Dependency lockfiles must be regenerated with the same Node release as CI (Node 20.19.0 when this was written — a dated claim; see the `docs/releases/pending/` convention in `docs/releases/pending/655-runtime-contract-gate-doc-drift.md`. The current pin lives in `.github/workflows/ci.yml` under `setup-node` and is enforced by `scripts/check_runtime_contract.py`) so the bundled npm version matches CI; lockfiles produced by a different npm may be rejected by CI.
- Keep the top-level `vite` dependency aligned with the `vitest` range (Vitest 4.x required Vite >= 6 when this was written — again a dated claim; check `frontend/package.json` for the current ranges). The Frontend job's toolchain-graph step fails the build when the two disagree.

## Why
These patterns caused real CI failures on dependabot PRs #320 and #332. Capturing them in the engineering conventions prevents regressions when future dependency upgrades touch the frontend store or build toolchain.

## Migration
No migration required.

## Caveats
- This is a documentation-only change; no runtime behavior is affected.
- The existing frontend code already follows these conventions.
