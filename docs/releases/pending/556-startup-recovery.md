# Startup recovery remains bounded and race-safe (Issue #556)

## What changed

- Startup recovery runs detached from readiness while preserving ownership
  leases for live ingestion, reindex, enrichment, and atom-enrichment work.
- Recovery sweeps yield cooperatively, deduplicate queued work, and fence
  rows created after the current startup cutoff so new requests are not reset
  or re-enqueued by the recovery pass.
- Same-generation atom enrichment claims are now atomic, preventing duplicate
  provider calls when recovery overlaps an active worker.

## Rollout and rollback

No schema migration or API change is required. Revert the commit to roll back;
the changes are limited to in-process queue and recovery coordination.
