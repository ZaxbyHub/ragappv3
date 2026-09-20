# 592 — Move async-route pool checkouts off the event loop

## What changed

- All 42 remaining `pool.get_connection()` / `db_pool.get_connection()` call
  sites that executed directly inside `async def` route handlers and helpers
  in `backend/app/api/routes/` now dispatch the checkout to a worker thread
  via `asyncio.to_thread`, matching the pattern the #549/#593 maintenance-surface
  fix established. The blocking checkout (`Queue.get(timeout=5)` for up to
  3 attempts, plus per-checkout `SELECT 1` + PRAGMA validation) no longer runs
  on the asyncio event loop thread.
- Under an exhausted pool, one DB route stalling on its checkout no longer
  freezes unrelated concurrent requests: the wait occupies a worker thread
  while the loop keeps scheduling.
- A new AST guard test (`test_issue592_no_on_loop_checkout.py`) pins the
  invariant: no `<pool>.get_connection()` call may execute lexically inside an
  `async def` under `backend/app/api/routes/` (nested sync defs/lambdas
  dispatched to a worker thread are safe by construction). A liveness proving
  test (`test_issue592_pool_exhaustion_liveness.py`) drives a real route
  handler under an exhausted pool and asserts unrelated concurrent requests
  complete promptly.

## Migration

- No schema, config-key, or API-shape changes. Behavior only: latency profile
  under pool pressure improves; per-request behavior and error semantics are
  unchanged (checkout failures still raise `RuntimeError` at the same call
  site — e.g. the upload compensation path keeps its fail-open behavior).

## Breaking changes

- None.

## Known limitations

- The fix covers the checkout sites in `backend/app/api/routes/` — the issue's
  stated surface. The same loop-blocking class exists OUTSIDE routes (async
  FastAPI dependencies `evaluate_policy` / `require_health_probe_auth` /
  `require_service_account`, the document-processor ingestion path,
  file-watcher / email / memory-store background loops, and the
  `db_transaction` context manager); those are tracked separately and are not
  addressed here.
- `release_connection` still runs on the loop where the handler releases its
  connection; it has no unbounded queue wait (only a dirty-connection
  rollback), so it is not part of this issue's blocking-checkout class.
