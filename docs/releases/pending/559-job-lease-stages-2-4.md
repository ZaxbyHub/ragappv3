# Unified DB-claimed job leases for wiki, KMS, reindex and draft (issue #559, stages 2-4)

## Backend

- The wiki and KMS compile queues moved onto the shared `jobs` lease table
  (issue #559, audit finding E02): every enqueue inserts a `jobs` row
  (queues `wiki`/`kms`) whose payload carries the enqueue-time inputs, and
  workers claim it with one atomic statement — `BEGIN IMMEDIATE` plus
  `UPDATE ... WHERE status = 'pending' ... RETURNING *` — with worker fencing,
  lease generation and durable attempts. `WikiStore.claim_next_pending_job`
  previously had no lease semantics at all and `KMSStore` used the
  two-statement `BEGIN IMMEDIATE` + SELECT + UPDATE claim; both now delegate
  to the same shared `JobLease` primitive ingestion uses.
- Wiki/KMS compile workers renew a per-job heartbeat while dispatching, and
  the shared janitor reclaims expired leases — a crash mid-compile settles
  the job while the process lives, instead of waiting for the next restart's
  blanket `_reset_orphans` reset (which is retired in lease mode). Retryable
  failures requeue through the durable `run_after` gate with exponential
  backoff, bounded by `jobs_max_attempts`; the detached sleep-and-reset tasks
  are gone in lease mode.
- The reindex queue joined the same lease (queue `reindex`): the non-atomic
  UPDATE+SELECT claim became the shared single-statement claim, and the
  startup/shutdown `interrupted` marking is retired — a crashed reindex job
  is reclaimed by the janitor and RETRIED (bounded by `jobs_max_attempts`)
  instead of being killed terminally. The `interrupted` status is mapped away
  in the same PR: the reindex status endpoint now serves the unified
  vocabulary (`pending/running/completed/failed/cancelled`), and historical
  `interrupted` rows are presented as `failed` with the original error
  preserved. No frontend consumer maps reindex job status (grep evidence in
  the PR).
- Draft jobs stay on their typed `draft_jobs` table but route through the
  SAME `JobLease` class, parameterized on the table (`job_type` is the queue
  discriminator; claims keep the oldest-`created_at`-first ordering and the
  compile claim keeps the issue #516 DRAFT-013 `started_at` deadline
  continuation). `draft_jobs` gains `worker_id`, `lease_generation` and
  `attempts` columns (idempotent migration, fresh-DB DDL converges). A draft
  janitor recovers only leases whose `heartbeat_at` expired past the reclaim
  timeout (reusing the business-aware orphan recovery), the pipeline's
  stage-progress/model-call heartbeat writes are fenced, and a run whose
  lease is lost aborts without a terminal write — closing the R4-S16
  double-execution window. Cancellation (`cancel_requested_at`) semantics,
  idempotent enqueue and the retry-endpoint behavior are unchanged.
- Boot migration: pending/running rows in `wiki_compile_jobs`,
  `kms_compile_jobs` and `document_reindex_jobs` are copied into `jobs`
  (idempotent, barrier-gated, detached — workers and the janitor never claim
  before it completes). Historical terminal rows are copied too so the job
  list endpoints keep showing them. A boot with a queue's switch disabled
  reverse-syncs that queue's not-yet-claimed `jobs` rows back to the legacy
  table and resets stuck legacy `running` rows to `pending` — a flag-flip
  rollback never strands unclaimed work.

## New settings

- `WIKI_KMS_JOB_LEASE_ENABLED`, `REINDEX_JOB_LEASE_ENABLED`,
  `DRAFT_JOB_LEASE_ENABLED` (all default `true`): env-only per-queue lease
  switches, same rule as `INGESTION_JOB_LEASE_ENABLED` — deliberately absent
  from the settings API, selecting the code path at process start; flipping
  one requires a restart. Forwarded by docker-compose.

## Operator notes (issue #518 obligation)

- Recovery actions: a stuck `running` job on any covered queue is reclaimed
  automatically after `JOBS_LEASE_RECLAIM_TIMEOUT_SECONDS`; inspect
  `SELECT * FROM jobs WHERE status != 'completed'` for the live set across
  queues (`queue` column: `ingestion`, `wiki`, `kms`, `reindex`). Draft rows
  live in `draft_jobs` (columns `worker_id`/`lease_generation`/`attempts`).
  `failed` with error `lease_attempt_cap_exceeded` (janitor-settled) or
  `attempt_cap_exceeded` (worker-settled) means the job exceeded
  `JOBS_MAX_ATTEMPTS` across crash/reclaim cycles — re-trigger the work or
  raise the cap. Draft reclaims use the same cap; a draft job that keeps
  crashing now settles `failed` instead of being resurrected on every
  restart.
- Rollback: set the relevant switch(es) to `false` and restart. The legacy
  claim paths are unchanged and the boot reverse-sync re-materializes
  not-yet-claimed work into the legacy tables. Draft has no separate legacy
  table: its switch changes only the claim code path (`draft_jobs` is the
  lease store in both modes), so a draft rollback needs no data movement.
- The issue's 3,000-file `kill -9` live gate remains an operator gate for
  the ingestion stage (see the stage-1 note); CI proves the scaled form for
  stages 2-4 through the janitor-reclaim-without-restart, crash-reclaim and
  attempts-cap check families.

## Known limitations

- Multi-worker scale-out remains a documented open item (#518): the lease,
  fencing and janitor are safe under concurrent processes even though the
  shipped topology is single-process.
- The wiki/KMS `jobs` payload stores enqueue inputs (`vault_id`,
  `trigger_type`, `trigger_id`, `input_json`) and `result_json` stores the
  compile result exactly as before; API response shapes are unchanged.
- `ingestion_job_lease_enabled=false` (stage 1) and the new per-queue
  switches are independent; mixed-mode operation (some queues leased, some
  legacy) is supported and exercised by checks.
