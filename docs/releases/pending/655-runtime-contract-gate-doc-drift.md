# chore(ci): harden the runtime-contract gate, repair doc drift, and label the secretscan gate's scope (#655)

## What changed
- `scripts/check_runtime_contract.py` gains a docs surface (`docs_surface_failures` + `check_docs_surfaces`): `docs/engineering/conventions.md` runtime-pin prose is checked against `ALLOWED_RUNTIME` and the live `frontend/package.json` vitest/vite majors (natural-language forms included: `Node.js X.Y`, `.x` wildcards, `@`-style), both engineering docs' CI job inventories are checked against the actual `ci.yml` job declarations — ground truth from a stdlib indentation-walking parser of the `jobs:` mapping (quote-stripped `name:` values, job-key fallback when a job declares no `name:`, so an inventory can never be silently truncated) — and `testing.md` must name all six `scripts/check_*.py` quality contracts. Both docs are required surfaces: a missing file fails loud instead of being skipped, and an unparseable `jobs:` mapping fails loud as parser/workflow drift.
- `docs/engineering/conventions.md`: the false runtime pins ("CI pins Node 20.19.0", "Vitest 4.x requires Vite >= 6") are replaced with non-versioned guidance that cannot drift; the CI line now names all seven ci.yml job families.
- `docs/engineering/testing.md`: the "What CI runs" inventory now names all seven ci.yml jobs with their step lists, all six quality-contract scripts, and the three auxiliary workflows (`closure-evidence.yml` warn-mode, `nightly.yml`, `nightly-quality-gates.yml`).
- `scripts/check_secretscan.py`: the success line and docstring now state that the `secretscan` scanner itself is not run by any CI job — the gate validates the ignore file only.
- `AGENTS.md` + `CONTRIBUTING.md` (Phase 4.2 sweep hits): CI gate inventories completed to the same 7-job / 6-script ground truth.
- `backend/tests/test_runtime_contract_docs.py` (new): pins all of the above, including natural-language pin forms, the single-message failure contract, fail-loud missing-file behavior, and the dependency-major derivation.

## Why
The 2026-09-22 frontier audit found the authoritative conventions doc instructing contributors to regenerate lockfiles with a Node release CI abandoned months earlier (20.19.0 vs the pinned 22.22.2), a testing doc that understated the CI gate lattice (3 of 7 jobs, 2 of 6 scripts), and a secretscan CI gate whose green status implied protection CI does not have. `check_runtime_contract.py` — the gate built to prevent exactly this — could not see any of it; it exited 0 while the docs lied. Post-fix documentation regression is the class; the gate now fails whenever a gated doc names fewer jobs or scripts than `ci.yml` defines.

## Migration
No migration required. If your PR adds a ci.yml job or a `scripts/check_*.py` gate, update the inventories in `docs/engineering/testing.md` (and the job list in `docs/engineering/conventions.md`) in the same change — the Quality contracts job now enforces it.

## Caveats
- The secretscan scanner itself remains unwired by design (wiring it is a separate supply-chain decision); its gate's PASS now says so explicitly.
- `docs/releases/pending/*.md` historical fragments intentionally keep their dated version claims (they were true when written); only the living guidance docs are gated.
- The checker reads `frontend/package.json` for the vitest/vite majors and fails loud if they are unparseable; the existing engines-node check is unchanged.
