# Lease-mode reindex and ingestion job durability, visibility and retry budget (issue #702)

Workstream B PR 13 of 16 (frontier audit 20260923T174456Z, findings P02-SK2-01/-02/-03/-04/-05/-07/-08/-09/-10/-11/-12/-14).

## Backend

- The reindex lease worker loop now survives a job-level exception: a failed
  settle (e.g. a transient `sqlite3.OperationalError`) is logged and the loop
  claims the next job, mirroring the ingestion transport's settle guard. The
  abandoned lease keeps a dead heartbeat and is reclaimed by the janitor, so
  no work is lost (P02-SK2-01).
- Reindex failure detail reaches the persisted error: `_reindex_embed_all`
  returns the failure as an exception OBJECT (per-file failures return the
  first failing exception) so the #562 redaction guard at the lease settle
  boundary actually fires; the attempts-cap message is never
  `attempt_cap_exceeded: None`, and raw exception text/paths never reach
  `jobs.error`. Hand-written operator guidance (vault-scoped migration
  refusal, rebuild-aborted counts, identity-moved) is raised as
  `ReindexOperatorGuidance` and persisted verbatim, preserving the actionable
  text (#819's constraint) (P02-SK2-03, P02-SK2-10).
- Reindex retries are file-scoped: a below-cap failure records the failed
  file ids in the job payload (`$.retry_file_ids`, written under the same
  worker/status fence as the requeue, committed as one unit with it) and the
  next attempt re-embeds only those files instead of the whole scope
  (P02-SK2-04).
- The janitor docstring now states the real boot contract: a lease settles
  only after its heartbeat ages past the reclaim timeout (the timeout IS the
  crash detector — expiring a boot-fresh heartbeat would fence a live
  worker); the boot sync complements by never minting over a non-terminal
  job (P02-SK2-05).
- `/health`'s `ingestion_queue_size` reports the durable backlog in lease
  mode: `BackgroundProcessor.queue_size` counts non-terminal `jobs` rows
  when the lease transport is active, falling back to the in-memory queue
  depth otherwise (P02-SK2-02).
- The janitor's reclaim and files-row resync run in ONE transaction, and the
  cap resync acts only on jobs newly capped by that sweep (a job-id diff), so
  a historical capped job can never flip an unrelated pending file to
  `error` (P02-SK2-07).
- The boot sync never mints a fresh zero-attempt job for a file whose job
  settled at the attempts cap (anti-join keyed on the attempt-cap marker
  only) and settles that file's row to the matching terminal state in the
  same transaction — the crash window between two commits can no longer
  bypass the cap or leave an invisible `processing` row (P02-SK2-08).
- Lease-to-legacy rollback preserves running work: a `running` reindex jobs
  row is copied back to `document_reindex_jobs` as claimable `pending`, and
  the ingestion queue gets its own rollback leg — files rows owning
  non-terminal ingestion jobs are reset to `pending`/`queued` (collected
  before settling) and the abandoned jobs rows are settled terminally with
  `error='lease_disabled_rollback'` so they can never double-serve after a
  re-enable (P02-SK2-09).
- A graceful lease release refunds the attempt the claim consumed
  (`JobLease.release` and both shutdown release sweeps): a stop/start cycle
  no longer burns durable retry budget (P02-SK2-11).
- A graceful stop no longer strands parked legacy retries: discarded retry
  tickets (and the ticket held in the retry scheduler's delay sleep at
  cancellation) reset their ingestion files rows to `pending` so startup
  recovery re-enqueues them (P02-SK2-12).
- The admin retry route answers `already_in_progress` on a second call in
  lease mode (new `enqueue(..., _dedupe_existing_job=True)` file-scoped
  dedupe) instead of silently inserting a second jobs row (P02-SK2-14).

## Operator notes

- Regenerate-on-crash guidance is unchanged: a job stuck `running` still
  settles after `JOBS_LEASE_RECLAIM_TIMEOUT_SECONDS`; the metric
  `ingestion_queue_size` now reflects real durable backlog under the default
  transport.
- `jobs.error='lease_disabled_rollback'` marks ingestion rows settled when
  the ingestion lease switch was disabled at boot; the work itself was
  re-served through the legacy files-row recovery path.
- The bandit baseline was regenerated (pure line-number drift; finding
  content signatures unchanged, count flat at 131).
