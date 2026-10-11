# 780: Workstream L housekeeping — stale review-skill trigger, font-vendoring hardening, dead favicon, phantom response fields, unsafe localStorage write

Issue: #780 ([Workstream L] PR 9 of 9; frontier audit run 20260923T174456Z
findings TQ-instruction-batch-3-12, T1-20-S2-12, T1-20-MH-07, T1-20-KR-12,
TQ-differential-d-06, TQ-sibling-batch-03-08).

## What changed

Six independent, low-risk housekeeping fixes plus the guardrail wiring the
repo's contract requires for them:

- **The generated `pr-review-database-api` skill's file trigger works again.**
  Its `include:` glob still named `frontend/src/lib/api.ts`, deleted when the
  API client was split into `frontend/src/lib/api/*.ts` (#295) — the trigger
  never fired for API-client changes and the checklist sent reviewers to a
  nonexistent file. The glob is now `frontend/src/lib/api/**/*.ts` (plus
  `frontend/src/stores/**` and `frontend/src/hooks/**`, where the
  state/contract logic its checklist covers actually lives), and the
  checklist reference names the per-domain modules.
- **`fetch-fonts.mjs` can no longer corrupt the vendored fonts on a failed or
  zero-effect run.** Each woff2 downloads to a `.part` staging file, is
  validated (wOF2 magic + minimum size), and is renamed onto its final path
  only after a completed transfer; the staging file is cleaned up on failure
  (with a bounded retry on Windows rename locks), and any `.part` file
  orphaned by an earlier hard-killed run is swept at the start of every run —
  so a download that fails mid-loop leaves every not-yet-reached final
  `.woff2` untouched (files from earlier iterations of the same run keep
  their new bytes beside a deliberately unwritten manifest; recovery is
  `git checkout -- frontend/src/assets/fonts`). A css2 response that parses
  to zero `@font-face` blocks fails the run before any download, and a
  response missing any requested family (Inter, Spline Sans, Commissioner,
  Electrolize) fails after the downloads but before the manifest write — the
  script's own exit and stderr are the signal for that case, not CI.
  `SHA256SUMS` is built only from the files the run actually vendored and is
  itself written via temp+rename; a successful run prunes vendored `.woff2`
  files the css2 response no longer references, so the tree it leaves is
  self-consistent (orphan files from older runs no longer get legitimized
  into the manifest, and none linger uncovered). A successful re-vendor over
  the current clean vendored directory produces byte-identical output.
- **The favicon exists.** `index.html` linked `%BASE_URL%vite.svg` — the Vite
  scaffold default that never existed in the repo or any build — so every
  page load fetched HTML as the icon and the browser showed its default. The
  link now points at a new self-authored original `frontend/public/favicon.svg`
  (Meridian "M" mark; no third-party asset, no notice changes).
- **The phantom `status` field is gone from `AddMemoryResponse`.** The type
  is now an alias of `MemoryResult` — a deliberate five-field subset of the
  backend's eight-field `MemoryResponse` (id, content, metadata, score,
  importance, expires_at, created_at, updated_at) covering everything the app
  consumes; the CI guard enforces the phantom direction (no TS field the
  backend never sends), not subset completeness. `metadata` is additionally
  typed `| null` to match the backend's explicit-null emission.
  `getUserGroups` — a zero-caller wrapper whose four-field return shape the
  audit also flagged — was deleted outright (the issue's own suggested
  alternative); the real consumer of `GET /users/{id}/groups` uses its own
  typed client.
- **A storage failure no longer discards a successful vault fetch.**
  `fetchVaults`/`removeVault` commit their state slice pure of side effects
  and persist afterwards via a guarded `setActiveVault` call, so a quota
  error surfaces neither as a fetch/removal failure nor as a lost list.
  Unlike `addVault` (whose guard cannot lose state), the in-memory selection
  IS corrected before the best-effort persist, so memory and storage can
  differ until the next successful persist or reload — the tests pin the
  in-memory correction explicitly.
- **Three new CI quality-contract guards** (wired into the Quality contracts
  job, the justfile recipe, and all four documented inventories):
  `check_l09_skill_trigger_paths.py` (every generated-skill include glob
  matches an existing non-test file, body path references resolve, and the
  parsers fail closed on zero/undecodable input), `check_l09_index_assets.py`
  (every `%BASE_URL%` href/src link in index.html exists under
  `frontend/public/`, both quote styles, fail-closed on parser drift), and
  `check_l09_memory_response_fields.py` (the `AddMemoryResponse` TS type
  declares no field the backend `MemoryResponse` model lacks, with
  docstring/nested-key parser hardening). Each guard has planted-violation
  tests asserting exit 1 plus fail-closed exit-2 pins.

New tests: `frontend/src/tests/fetch-fonts.l09.test.ts` (frozen acceptance
checks: interrupted download leaves final bytes untouched; zero-block css2
fails the run), `frontend/src/tests/fetch-fonts.l09-success.test.ts` (a full
successful vendoring stages, renames, and writes the exact run-scoped
manifest), `frontend/src/tests/fetch-fonts.l09-feedback.test.ts` and
`frontend/src/stores/useVaultStore.l09-feedback.test.ts` (PR #880 review
round: missing-family abort leaves the manifest untouched and sweeps
staging files; orphans are pruned and excluded; interrupted downloads leave
no residue; the zero-block failure names itself on stderr; fetchVaults
corrects the in-memory selection under a storage throw; removeVault resolves
when its persist throws), and `backend/tests/test_l09_gate_wiring.py`
(the three guards are wired into CI/justfile via non-comment run lines, pass
on the real tree, fail closed when their scanned surfaces are absent, and
DETECT planted violations with exit 1).

## Why

Each defect was independently verified against master before the fix (all six
reproduced; see the trace's `02-reproduction.md`), and each fix is pinned by a
frozen acceptance check that was RED at base and GREEN at head, with
defence-in-depth behaviour pins added in the PR #880 review round.

## Migration

None required. The font re-vendor script's interface is unchanged; only
failure/partial-run behavior changed. Operators who re-vendor fonts get the
same `SHA256SUMS`/woff2 output for a successful run over the current vendored
set. If a run fails mid-loop, `git checkout -- frontend/src/assets/fonts`
restores the committed state.

## Known limitations

- The five prose references to the deleted `frontend/src/lib/api.ts` in
  hand-written skills/agent docs (`.agents/skills/config-env-contract-check`,
  `.agents/skills/engineering-conventions` (short-form), `.agents/skills/subpath-deployment` ×2,
  `.github/agents/pr-reviewer.agent.md`)
  are out of this PR's scope per the issue's Scope clause — candidates for
  Workstream J issue #760 (agent workflow skills citing stale paths).
  `CHANGELOG.md`'s mention is the historical record of the #295 split itself.
- The font-family guard fails AFTER downloads complete: a future css2
  response missing one family leaves freshly downloaded (valid, complete)
  files on disk beside an unwritten manifest. That is deliberate — the
  alternative ordering would let the interrupted-download guarantee go
  untested. The script's non-zero exit and stderr (which names the missing
  families and the already-replaced paths) are the only signals; the
  font-integrity CI test compares the unchanged manifest against the
  unchanged files and would still pass, so it does NOT catch this case.
- Concurrent `fetch-fonts.mjs` runs are not supported (a second run's sweep
  can delete a first run's in-flight staging file); the script is hand-run
  only — no CI or justfile target invokes it.
- `AddMemoryResponse` intentionally under-declares three backend-only fields
  (importance, expires_at, created_at); the CI guard enforces the phantom
  direction only.
