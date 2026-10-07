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
  `attempt_cap_exceeded: None` — it carries the #562 stable code for the
  failure (the per-file `details` ledger stays transport metadata) — and raw
  exception text/paths never reach `jobs.error`. Hand-written operator
  guidance (vault-scoped migration refusal, narrowed-retry migration
  refusal, rebuild-aborted counts, identity-moved) is raised as
  `ReindexOperatorGuidance` and persisted verbatim on the lease transport,
  preserving the actionable text (#819's constraint); on the legacy
  transport the guidance object feeds the existing result-JSON persistence
  shape (P02-SK2-03, P02-SK2-10).
- A narrowed file-scoped retry never enters the staged index rebuild: when
  a retry scope is recorded and the model identity or dimension changed, the
  job fails with operator guidance to run a fresh full reindex instead of
  committing a global index swap containing only the retried files
  (feedback round PRR-002 / out-of-band F-001).
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
  settled terminally at the attempts cap — keyed on terminal-at-cap
  (`status='failed' AND attempts >= JOBS_MAX_ATTEMPTS`) regardless of the
  row's error text, because the literal marker is only written when the
  error column was empty (a requeue-then-crash job keeps its old error) —
  and settles that file's row to the matching terminal state in the same
  transaction; the crash window between two commits can no longer bypass
  the cap or leave an invisible `processing` row (P02-SK2-08, feedback
  round PRR-001 / out-of-band F-002).
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
- A failure that lands in the shutdown window settles as a plain requeue
  with its attempt ledger kept — never as a refunded release — so a retry
  budget is only refunded for work that never ran (feedback round
  out-of-band F-004).
- The compile-queue (wiki/KMS/reindex) reverse sync settles its copied
  source rows terminally (`lease_disabled_rollback`), like the ingestion
  leg, so a disable/re-enable cycle can never serve the same job twice
  (feedback round out-of-band F-005).
- `ingestion_queue_size` is omitted from `/health` (with a WARNING log)
  when the durable count fails, instead of reporting a fabricated zero;
  re-enqueueing a file that is already queued no longer discards a pending
  cancellation request (feedback round out-of-band F-007).
- The admin retry route answers `already_in_progress` on a second call in
  lease mode (new `enqueue(..., _dedupe_existing_job=True)` file-scoped
  dedupe) instead of silently inserting a second jobs row (P02-SK2-14).

## Operator notes

- Regenerate-on-crash guidance is unchanged: a job stuck `running` still
  settles after `JOBS_LEASE_RECLAIM_TIMEOUT_SECONDS`; the metric
  `ingestion_queue_size` now reflects real durable backlog under the default
  transport.
- `jobs.error='lease_disabled_rollback'` marks rows settled when their
  queue's lease switch was disabled at boot (ingestion AND copied
  wiki/KMS/reindex sources); the work itself was re-served through the
  legacy files-row recovery path or the copied legacy-table rows.
- The bandit baseline was regenerated: the 131 suppressed findings keep
  identical `(test_id, filename, issue_text)` signatures (locations moved
  with the code), and TWO new justified `# nosec B608` markers were added
  for placeholder-interpolated SQL with bound parameters (baseline
  `skipped_tests` 64 → 66).
