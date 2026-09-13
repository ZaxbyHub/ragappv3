# Startup recovery remains bounded and race-safe (Issue #556)

## What changed

- Startup recovery runs detached from readiness while preserving ownership
  leases for live ingestion, reindex, enrichment, and atom-enrichment work.
- Recovery sweeps yield cooperatively, deduplicate queued work, and fence
  rows created after the current startup cutoff so new requests are not reset
  or re-enqueued by the recovery pass.
- Same-generation atom enrichment claims are now atomic, preventing duplicate
  provider calls when recovery overlaps an active worker.
- Recovery reservations are process-local in-memory coordination only; multi-worker
  deployments must rely on the durable database status and compare-and-set
  boundaries rather than treating these reservations as cross-process locks.
- The admin retry endpoint still returns `status: "scheduled"` when it adds a
  queue item. If recovery already owns the document, it now returns
  `status: "already_in_progress"` and records the same status in the audit
  action, so operators can distinguish an accepted retry from an existing
  in-flight recovery.

## Rollout and rollback

No schema migration is required. This is an additive API response change:
consumers that treat any successful response as accepted/in-flight remain
compatible, while clients with an exhaustive status match must accept both
`scheduled` and `already_in_progress`. Revert the commit to roll back; the
database schema is unchanged.
