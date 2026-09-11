# Draft Room transactional reliability (Issue #516, PR 2 of 3)

## What changed

### Backend — Draft Room lifecycle, freshness and approval state

Nineteen audited defects (DRAFT-001/002/004/005/006/007/011/013/014/020/022/023/024/025,
UI-018/019/020/021/022, API-003) are closed; the DRAFT-003 no-op-save guard that
a prior release already shipped is now pinned by regression tests.

- **Source-change invalidation (DRAFT-001, DRAFT-004, DRAFT-025):** bulk
  document deletion (delete-all and vault purge) and the upload-overwrite path
  now invoke the draft-evidence freshness hook in the same transaction as the
  mutation, matching single-delete semantics. Source-wide invalidation
  prioritizes current-revision evidence before historical rows, and truncated
  sweeps persist a backlog entry the startup reconciler drains (bounded per
  run). KMS compile-on-ingest upserts invoke `on_kms_entry_changed` for changed
  bodies, like manual edits already did.
- **Startup reconciliation cursor (DRAFT-002):** the bounded Ready-evidence
  sweep persists its continuation cursor, so an unchanged Ready prefix can no
  longer starve stale tails behind it across restarts.
- **Cancellation semantics (DRAFT-005, DRAFT-006):** parsed output is committed
  only after a cancellation re-check inside the output-commit transaction; a
  cancelled parse can no longer complete and save text. Startup recovery no
  longer resurrects inputs whose parse job the user cancelled — explicit retry
  still works.
- **Startup dependency ordering (DRAFT-007):** queued compile jobs stay pending
  until the RAG engine is wired instead of failing terminally on unwired
  retrieval; the fail-closed retrieval guard remains as defense in depth.
- **Budgets and deadlines (DRAFT-011, DRAFT-013):** recovered compile jobs
  resume the persisted model-call count (and wall-clock deadline from the
  original start) instead of receiving a fresh budget; in-flight model and
  retrieval awaits are bounded by the remaining job deadline, so a hung provider
  call settles a timeout failure instead of stalling the worker.
- **Publication atomicity (DRAFT-014):** the previous current revision stays
  current until the single publication transaction demotes old and promotes
  new atomically; a failed replacement publication no longer leaves the draft
  without a current revision.
- **Revision-scoped blockers (DRAFT-020):** the Ready gate and blocker counts
  consider only findings attached to the current revision; historical findings
  remain inspectable and can no longer block corrected revisions.
- **Approval freshness (DRAFT-022):** material brief or input-metadata changes
  move a Ready draft back to needs_review and clear the Ready pointer in the
  same transaction; title-only and unchanged-value edits keep approval.
- **Scheduling durability (DRAFT-024) and promotion compensation (DRAFT-023):**
  infrastructure errors while enqueueing a parse no longer strand a pending
  input (retry works without a restart), and cancellation after the ingestion
  enqueue cancels the queued task so no orphaned ingestion job survives.

### Backend — additive state migration

`run_migrations` creates two single-purpose tables (`draft_reconcile_cursor`,
`draft_reconcile_backlog`) idempotently on existing and fresh databases. No
existing table changes; rollback of the code reverts to the previous behavior
and the tables are simply unused.

### Frontend — Draft Room workspace and API client

- SSE (re)subscription now invalidates the canonical detail/jobs caches, so
  events missed during a stream gap are refreshed (UI-018).
- Fallback polling keeps running while parse work is active, not only compile
  work (UI-020).
- A successful save keeps text typed while the request was in flight; the
  editor only resets when the buffer still matches what was submitted (UI-019).
- Stage/artifact inspection binds to the active/latest compile job, so a failed
  recompile's stages are inspectable instead of the previous job's (UI-021).
- Assignment list textareas resynchronize when a new server brief arrives while
  preserving intentional blank-line edits made locally (UI-022).
- Blob error responses decode their JSON body for every status, preserving the
  backend `detail`/`code`/`context` in export errors (API-003); the CSRF-403
  retry behavior is unchanged (strict superset).

## Operator-visible outcomes

Ready approvals can no longer survive deletion or content change of their
underlying sources; cancelled parse work stays cancelled across restarts;
compile budgets and deadlines are honored across crash recovery; corrected
revisions can reach Ready without waiving stale findings; export failures show
the backend's structured reason.

## Rollout and rollback

Additive migration only; safe to roll back by reverting the release (the new
tables become unused). No configuration changes and no new dependencies.
