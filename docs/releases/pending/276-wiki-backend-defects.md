# Wiki backend defect fixes (Issues #276, #99, #100, #103, #113)

## What changed

### Backend (issue #276 — eight defects)
- **Job-state race (A6-1)**: `reset_job_to_pending` now guards `status = 'failed'`
  in both `wiki_store` and `kms_store`, so the compile processor's delayed
  auto-retry can no longer resurrect a job the user cancelled during the backoff
  window.
- **Event-loop blocking (A6-2)**: `POST /api/wiki/promote-memory` now runs
  `compiler.promote_memory` via `asyncio.to_thread`, unblocking the loop when the
  curator is enabled (previously blocked up to 120s).
- **Claim-status filtering (A6-3)**: `_relation_lookup` and `_fts_claim_search`
  now exclude non-active claims (`unverified`/`superseded`/`contradicted`/
  `archived`/`needs_review`) so they no longer reach the LLM prompt as citable
  `[W#]` evidence.
- **Reindex dedup (A6-4)**: `_handle_reindex` now deduplicates `memory_id` before
  re-promoting, eliminating redundant `promote_memory` and curator calls;
  `reprocessed` now counts distinct memories.
- **N+1 provenance (E1-3)**: per-row `_claim_provenance` replaced by batched
  `_claim_provenance_for` (one `WHERE claim_id IN (...)` query) in
  `wiki_retrieval`.
- **Poll-loop head-of-line blocking (E2-3)**: failed-job backoff now runs as a
  GC-safe detached task (`self._bg_tasks`) in both the wiki and KMS compile
  processors, so a retrying job no longer blocks every other pending job for the
  2–4s backoff.

### Frontend (issue #276 — 1X-1)
- **Optimistic locking wired through**: the `WikiPage` TS interface now includes
  `version: number` and `updateWikiPage` accepts `expected_version?: number`;
  the edit flow passes the loaded `version` and surfaces HTTP 409 conflicts with
  a toast + refetch.

### Tests
- **C3-4**: `process_existing_file` gating now has the missing positive and
  `compile_on_ingest=False` tests.
- **#99**: added end-to-end coverage for the previously-untested retrieval
  pipeline phases (entity exact match + alias, relation predicate scoring,
  exact-entity page evidence, batched provenance, multi-candidate ranking).

### Closure (no code change)
- **#100, #103, #113**: independently verified as already resolved on `master`
  and closed with evidence in the PR body.

## Why
Resolves the wiki-subsystem defect batch from the 2026-07 integrated review
(#276) and the high-severity retrieval-coverage gap (#99), and closes three
issues that had been resolved on master without being closed on GitHub
(#100, #103, #113).

## Migration steps
No migration required. All changes are backward-compatible:
- The A6-3 status predicate uses `(c.status IS NULL OR c.status = 'active')`, so
  legacy NULL-status rows still pass.
- The frontend `expected_version` field is optional; omitting it skips the
  conflict check (backend default).
- No schema change (E2-3 uses a detached task, not a `not_before` column).

## Breaking changes
None.

## Known caveats
- **A6-3 changes LLM prompt content**: non-active claims no longer surface as
  citable wiki evidence via the retrieval service. The list/browse API
  (`list_claims`) still returns all statuses. This is the documented intent
  ('active' is the citable status).
- The new `WikiPage.tsx` 409 conflict handler ships without a dedicated frontend
  test (the existing test mocks the edit dialog as a stub); the logic is
  verified correct end-to-end.
