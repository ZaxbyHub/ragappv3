# Durable ingestion job leases (issue #559, stage 1)

## Backend

- Ingestion work is now claimed through one DB-backed lease: every enqueue
  inserts a row into a shared `jobs` table (queue `ingestion`) and workers
  claim it with a single atomic statement — `BEGIN IMMEDIATE` plus
  `UPDATE ... WHERE status = 'pending' ... RETURNING *` — so two workers can
  never take the same job (issue #559, audit finding E02; SQLite >= 3.35,
  runtime ships 3.45.1).
- A per-job heartbeat renews the lease while a worker runs, and a janitor
  task reclaims leases whose heartbeat expired: a crash mid-job settles the
  job (back to pending, or terminally failed at `jobs_max_attempts`) while
  the process lives — settlement no longer depends on a restart or on how
  many times the process restarted. A reclaimed job's files row is reset to
  `pending`/`queued` (or `error` at the cap) in the same pass.
- Retry attempts for ingestion are now durable (`jobs.attempts`), replacing
  the in-memory counter that was lost on every crash. The bounded retry
  scheduler still owns backoff delay delivery; the deferred row is gated by
  `run_after` so a delayed retry cannot be claimed early.
- Boot migration: files rows awaiting work with no live jobs counterpart are
  copied into `jobs` by a detached, idempotent migration; workers and the
  janitor wait on a barrier until it completes, so no claim is served before
  the rows exist. Shutdown releases every still-held lease immediately
  instead of waiting out the reclaim timeout.
- Cancellation of not-yet-started uploads (`cancel_pending_jobs`) now
  cancels the durable pending rows (and still handles legacy in-memory
  items).
- New settings: `jobs_heartbeat_interval_seconds` (5-600, default 30),
  `jobs_lease_reclaim_timeout_seconds` (15-3600, default 300; must exceed 4x
  the heartbeat), `jobs_max_attempts` (1-20, default 3) — all admin-settable
  with two-layer validation. `ingestion_job_lease_enabled` is env-only by
  design: it selects the claim path at process start and flipping it
  requires a restart.
- The hourly stranded-row rescan (`_orphan_rescan_loop`) is not published in
  lease mode — the janitor owns orphan settlement; it remains active when
  the lease is disabled (legacy mode, `INGESTION_JOB_LEASE_ENABLED=false`).

## Operator notes (issue #518 obligation)

- The issue's 3,000-file hard-kill (`kill -9`) scenario is a LIVE operator
  gate for this stage; CI proves the scaled form (the C05 integration test,
  25 rows, plus the frozen lease-level crash-restart checks). Run the live
  gate on a staging instance before declaring stage 2.
- Recovery actions: a job stuck `running` is reclaimed automatically after
  `JOBS_LEASE_RECLAIM_TIMEOUT_SECONDS` without operator action; inspect
  `SELECT * FROM jobs WHERE queue = 'ingestion' AND status != 'completed'`
  for the live set. `status = 'failed'` with error
  `lease_attempt_cap_exceeded` (janitor-settled) or `attempt_cap_exceeded`
  (worker-settled) means the job exceeded `JOBS_MAX_ATTEMPTS`
  across crash/reclaim cycles — re-upload or raise the cap.
- Rollback: set `INGESTION_JOB_LEASE_ENABLED=false` and restart; the legacy
  in-memory queue path is unchanged and rows left in `jobs` are re-derived
  from the `files` table on the next lease-enabled boot.
- Multi-worker scale-out remains a documented open item (#518); the lease
  and fencing are safe under concurrent processes even though the shipped
  topology is single-process.
