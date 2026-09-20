# 592 — Move async-route pool checkouts off the event loop

## What changed

- All 42 remaining `pool.get_connection()` / `db_pool.get_connection()` call
  sites that executed directly inside `async def` route handlers and helpers
  in `backend/app/api/routes/` now check out through the new
  `SQLiteConnectionPool.get_connection_async()`, which runs the blocking
  checkout on a DEDICATED bounded executor owned by the pool
  (`CHECKOUT_EXECUTOR_MAX_WORKERS = 16`, shut down with the pool). The
  blocking checkout (`Queue.get(timeout=5)` for up to 3 attempts, plus
  per-checkout `SELECT 1` + PRAGMA validation) no longer runs on the asyncio
  event loop thread.
- The checkout executor is deliberately separate from the loop's default
  executor (which carries the handlers' SQL work): under pool exhaustion,
  waiting checkouts cannot starve the SQL/release work of handlers already
  holding connections, so released slots free up and queued checkouts drain
  instead of mass-failing after the wait budget (release-starvation convoy;
  pinned by `test_issue592_checkout_executor_convoy.py`).
- Under an exhausted pool, one DB route stalling on its checkout no longer
  freezes unrelated concurrent requests: the wait occupies a checkout-executor
  worker while the loop keeps scheduling.
- A new AST guard test (`test_issue592_no_on_loop_checkout.py`) pins the
  invariant: no `<pool>.get_connection()` call may execute lexically inside an
  `async def` under `backend/app/api/routes/` (nested sync defs/lambdas
  dispatched to a worker thread are safe by construction). A liveness proving
  test (`test_issue592_pool_exhaustion_liveness.py`) drives a real route
  handler under an exhausted pool and asserts unrelated concurrent requests
  complete promptly.

## Migration

- No schema, config-key, or HTTP API-contract changes. The pool class gains one
  new public method, `SQLiteConnectionPool.get_connection_async()`, which backs
  the converted routes; any test double that implements the pool interface and
  serves converted routes must implement it too (the in-repo fakes have been
  updated). Behavior is otherwise unchanged: checkout failures still raise
  `RuntimeError` at the same call site — e.g. the upload compensation path
  keeps its fail-open behavior.

## Breaking changes

- None.

## Rollback

- Revert the branch (pure backend change, no migrations/config). Rollback
  restores the on-loop blocking checkouts in routes — the #592 freeze and
  release-starvation convoy class returns under pool exhaustion, so treat
  rollback as a temporary mitigation.

## Known limitations

- The convoy isolation covers pool CHECKOUTS made through
  `get_connection_async()`. Handler SQL that runs via plain
  `asyncio.to_thread(...)` on the loop's default executor (e.g. the
  `documents.py` upload helpers) shares the default executor with other work
  and is not convoy-isolated; its latency is bounded by local SQLite I/O (no
  queue wait), so the 15 s stall mechanism cannot recur through it.
- The fix covers the checkout sites in `backend/app/api/routes/` — the issue's
  stated surface. The same loop-blocking class exists OUTSIDE routes (async
  FastAPI dependencies `evaluate_policy` / `require_health_probe_auth` /
  `require_service_account`, the document-processor ingestion path,
  file-watcher / email / memory-store background loops, and the
  `db_transaction` context manager); those are tracked by issue #645 and are
  not addressed here.
- `release_connection` still runs on the loop where the handler releases its
  connection; it has no unbounded queue wait (only a dirty-connection
  rollback), so it is not part of this issue's blocking-checkout class.
