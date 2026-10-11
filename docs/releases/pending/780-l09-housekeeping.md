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
  nonexistent file. The glob is now `frontend/src/lib/api/**/*.ts` and the
  checklist reference names the per-domain modules.
- **`fetch-fonts.mjs` can no longer corrupt the vendored fonts on a failed or
  zero-effect run.** Each woff2 downloads to a `.part` staging file and is
  renamed onto its final path only after a completed transfer, with the
  staging file cleaned up on failure — and any `.part` file orphaned by an
  earlier hard-killed run (power loss/SIGKILL bypasses the failure cleanup,
  including a stranded `SHA256SUMS.part`) is swept at the start of the next
  run — so an interrupted run leaves every final `.woff2` byte-identical
  instead of truncated. A css2 response that parses
  to zero `@font-face` blocks fails the run before touching disk, and a
  response missing any requested family (Inter, Spline Sans, Commissioner,
  Electrolize) fails after the downloads but before the manifest write (the
  mismatch then trips the font-integrity CI test loudly). `SHA256SUMS` is
  built only from the files the run actually vendored — orphan files from
  older runs are no longer legitimized into it — and is itself written via
  temp+rename. A successful run over the current clean vendored directory
  produces byte-identical output to before.
- **The favicon exists.** `index.html` linked `%BASE_URL%vite.svg` — the Vite
  scaffold default that never existed in the repo or any build — so every
  page load fetched HTML as the icon and the browser showed its default. The
  link now points at a new self-authored original `frontend/public/favicon.svg`
  (Meridian "M" mark; no third-party asset, no notice changes).
- **`AddMemoryResponse` no longer declares a field the backend never sends.**
  `POST /memories` returns `MemoryResponse` (`id, content, metadata, score,
  importance, expires_at`), but the hand-written type claimed `{id, status}`;
  it is now an alias of the real-shape `MemoryResult` interface, so it cannot
  drift again. `getUserGroups` likewise gets its own `UserGroupSummary` type
  matching `GET /users/{id}/groups`'s actual four-field shape (the full
  `Group` interface stays for the `/groups` endpoints that really return
  `created_at`/`organization_name`). No consumer read the phantom fields.
- **A storage failure no longer discards a successful vault fetch.**
  `fetchVaults`/`removeVault` used to run `localStorage.setItem`/`removeItem`
  inside their `set()` updaters, so a quota error aborted the state update and
  surfaced as a fetch/removal failure. Both now commit their state slice pure
  of side effects and persist afterwards via a guarded `setActiveVault` call —
  the same best-effort pattern `addVault` already used (59f387b3).
- **Three new CI quality-contract guards** (wired into the Quality contracts
  job, the justfile recipe, and all four documented inventories):
  `check_l09_skill_trigger_paths.py` (every generated-skill include glob
  matches at least one existing file), `check_l09_index_assets.py` (every
  `%BASE_URL%` link in index.html exists under `frontend/public/`), and
  `check_l09_memory_response_fields.py` (the `AddMemoryResponse` TS type
  declares no field the backend `MemoryResponse` model lacks).

New tests: `frontend/src/tests/fetch-fonts.l09.test.ts` (frozen acceptance
checks: interrupted download leaves final bytes untouched; zero-block css2
fails the run), `frontend/src/tests/fetch-fonts.l09-success.test.ts` (a full
successful vendoring stages, renames, and writes the exact run-scoped
manifest), `frontend/src/stores/useVaultStore.l09.test.ts` (storage failure
keeps the fetched vault list), and `backend/tests/test_l09_gate_wiring.py`
(the three guards are wired into CI/justfile, pass on the real tree, and fail
closed when their scanned surfaces are absent).

## Why

Each defect was independently verified against master before the fix (all six
reproduced; see the trace's `02-reproduction.md`), and each fix is pinned by a
frozen acceptance check that was RED at base and GREEN at head.

## Migration

None required. The font re-vendor script's interface is unchanged; only
failure/partial-run behavior changed. Operators who re-vendor fonts get the
same `SHA256SUMS`/woff2 output for a successful run.

## Known limitations

- The four prose references to the deleted `frontend/src/lib/api.ts` in
  hand-written skills/agent docs (`.agents/skills/config-env-contract-check`,
  `.agents/skills/subpath-deployment` ×2, `.github/agents/pr-reviewer.agent.md`)
  are out of this PR's scope per the issue's Scope clause — candidates for
  Workstream J issue #760 (agent workflow skills citing stale paths).
  `CHANGELOG.md`'s mention is the historical record of the #295 split itself.
- The font-family guard fails AFTER downloads complete: a future css2
  response missing one family leaves freshly downloaded (valid, complete)
  files on disk with an unwritten manifest. That is deliberate — the
  alternative ordering would let the interrupted-download guarantee go
  untested — and the mismatch is caught loudly by
  `font-integrity-manifest.test.ts` in CI.
