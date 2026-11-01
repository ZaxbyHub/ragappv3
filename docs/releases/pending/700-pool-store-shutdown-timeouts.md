# 700 — connection-pool, store-transaction and shutdown-budget timeouts hold under concurrency

## What changed

- **Migration/startup connections (T1-05-S-05):** every `sqlite3.connect` in
  the startup/migration path — 78 sites in `database.py` (72 `migrate_*`
  functions, 4 `run_migrations` internals, `init_db`, `get_db_connection`;
  including #851's `migrate_add_files_status_cancelled`, timed during the
  master sync), both connects in `migration_journal.py`, and
  `lifespan._load_persisted_settings`, 81 census sites in all — now passes
  `timeout=MIGRATION_CONNECT_TIMEOUT_SECONDS` (30.0, defined in
  `migration_journal.py`, imported by `database.py`/`lifespan.py`), matching
  `init_db`'s 30000 ms busy timeout instead of Python's 5 s default. A
  standing AST census guardrail
  (`test_b11_migration_connect_census.py`) pins the class: 0 of its 81
  census connects may lack a >= 30000 ms busy timeout before any table
  access, and the population is floored so the guardrail cannot pass on an
  empty scan.
- **Async checkout ceiling (T1-06-S-01):** `get_connection` accepts a
  keyword-only `deadline`; `get_connection_async` mints it at REQUEST time
  (before `executor.submit`), so time queued in the bounded checkout
  executor counts against the documented
  `max_wait_attempts * CHECKOUT_WAIT_SECONDS` ceiling. 80 concurrent
  waiters now all resolve within their own budget (previously ~ceil(N/16)
  queue rounds grew wall time unbounded).
- **Pool observability (T1-06-K-02):** all five "could not obtain a
  connection" exits route through a new `_fail_checkout` helper that emits
  the structured `pool_exhausted` event, so the log stream reflects every
  failure mode, not just loop exhaustion. The readiness probe's
  capacity-wait signal is deliberately unchanged: it still means a caller
  was forced to wait for capacity, not that any checkout failed.
- **Event-loop reachability (T1-06-S-04):** connection creation is
  serialized by a dedicated `_create_lock` instead of the shared `._lock`,
  which is now held only for O(1) bookkeeping — `recent_capacity_wait()`
  (readiness probe) and `_checkout_executor_ready()` can no longer block
  behind mkdir/connect/WAL-PRAGMA I/O. `_created_count` decrements on every
  post-increment non-success exit (including BaseException, re-raised);
  the #262 count invariant and #645 turn-taking refusal are preserved (the
  mechanism-pinning test was rewritten to pin the invariant, per the issue
  mandate).
- **release_connection never raises (S03-SK2-05):** after `close_all()` a
  release now logs `pool_release_after_close`, closes the connection, and
  returns — never raising — so the ~100 bare-`finally` callers (get_db
  included) neither mask in-flight handler exceptions nor leak the
  connection. A pre-existing test pinning the old raise contract was
  updated to pin the new one.
- **Store transaction boundaries (T1-02-S2-05, T1-27-S-09):**
  `TagStore.update_tag` rolls back on the 0-row branch (no more open WAL
  write lock until pool release); `FolderStore.create_folder` checks the
  parent inside its `BEGIN IMMEDIATE` (the FOLDER-001 shape); and
  `move_documents` runs target-check + UPDATE in one transaction guarded
  by an `in_transaction` rollback, translating the folders-FK
  `IntegrityError` to `FolderNotFoundError` only after a post-rollback
  re-check proves the target is really gone (unrelated constraint failures
  keep surfacing) — concurrent deletes now surface as the documented 404
  instead of a 500. Batch moves are capped at 1000 ids and rate-limited so
  the transaction's write-lock hold stays bounded.
- **Shutdown budget (T1-04-S2-02):** the `knowledgevault` compose service
  sets `stop_grace_period: 90s` (own-line comment documents the coupling to
  `BackgroundProcessor.stop()`'s 60 s per-phase budget), so the common
  single-phase drain is no longer SIGKILLed at Docker's implicit 10 s.
  `stop()` applies its timeout per drain phase, so a busy multi-phase
  shutdown can still hit the grace period — tracked in #854. Note the
  grace period only matters once the container actually receives SIGTERM:
  until #748 ships (`CMD` runs uvicorn under `sh -c` without `exec`), the
  shell may not forward it. `stop()` itself is unchanged.

## Why

Audit remediation issue #700 (frontier audit 20260923T174456Z, Workstream B
PR 11 of 16): eight concurrency findings where a documented bound or
guarantee was not enforced on every path.

## Migration steps

None. No schema changes; the compose change takes effect on the next
`docker compose up` (operators should expect `docker compose stop` to wait
up to 90 s before SIGKILL where it previously waited ~10 s — see the
caveats for the multi-phase residual).

## Known caveats

- Request-time service connects (vector_store, rag_engine,
  feedback_reranker, security) still use default busy timeouts; they are a
  different class (not startup/migration) and are dispositioned out of
  scope in the trace's recurrence sweep.
- Root moves (`move_documents(..., folder_id=None)`) now run inside the
  same BEGIN/commit envelope as target moves (uniformity; the empty-batch
  path takes and immediately releases the write lock where base took no
  lock at all).
- A multi-phase shutdown (all four drain queues busy, or a long
  `flush_optimize`) can still exceed the 90 s grace period — #854 tracks
  the composed stop() budget.
