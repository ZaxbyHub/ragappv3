# 645 — move the remaining on-loop pooled checkouts off the event loop (issue #645)

## What changed

- `backend/app/models/database.py`:
  - new `SQLiteConnectionPool.connection_async()` async context manager mirroring
    the sync `connection()` (acquire via `get_connection_async` on the pool's
    dedicated bounded checkout executor, release-with-dirty-rollback in
    `finally`, same `conn is None` acquisition-failure semantics).
  - `get_connection` now computes a monotonic deadline of
    `max_wait_attempts * CHECKOUT_WAIT_SECONDS` and bounds each `Queue.get`
    wait by the remaining budget (constant `CHECKOUT_WAIT_SECONDS = 5` replaces
    the literal; exhaustion `RuntimeError` contract unchanged).
  - `_validate_connection` probes with a temporarily reduced busy timeout
    (`VALIDATION_BUSY_TIMEOUT_MS = 1000`) and restores the connection's original
    busy timeout in a `finally` on every exit path, so a contended database can
    no longer stretch a single checkout past the nominal 15 s ceiling (worst
    case before: ~30 s per validation attempt against a `busy_timeout=30000`
    connection), and healthy checkouts still hand out connections with the
    production 30000 busy timeout.
- A-class checkouts (14 sites) converted from `pool.get_connection()` to
  `await pool.get_connection_async()`: `api/deps.py`
  (`require_health_probe_auth`, `evaluate_policy`), `security.py`
  (service-account dependency), `services/document_processor.py` (7 sites:
  `_write_session`, chunk-retry, `process_file`, `process_existing_file`),
  `services/email_service.py` (`_resolve_vault_id`), `services/file_watcher.py`
  (`scan_once`), `services/memory_store.py`
  (`backfill_missing_embeddings`), `utils/transaction.py` (`db_transaction`).
- B-class context-manager checkouts (36 sites) converted from
  `with <pool>.connection() as conn:` to
  `async with <pool>.connection_async() as conn:`: `services/background_tasks.py`
  (26), `services/multimodal_enrichment.py` (2), `services/vision_evidence.py`
  (`_conn_ctx`), and — beyond the issue's literal outside-routes table, because
  the new repo-wide guard cannot pass without them — 7 routes sites the #592
  guard's `get_connection`-only matcher never flagged (`api/routes/chat.py`
  x5 incl. `get_stream_auth` which keeps its release-before-streaming property,
  `api/routes/draft_room.py`, `api/routes/wiki.py`).
- `services/document_progress.py`: `set_phase`, `clear_progress`,
  `set_wiki_pending` are now `async def`s checking out via
  `get_connection_async` internally; all 26 production call sites awaited
  (document_processor x24, `routes/documents.py`, `draft_promotion.py` where
  the `asyncio.to_thread(set_phase, ...)` wrapper was dropped since the helper
  now moves its own checkout off-loop). Their best-effort
  `except (sqlite3.Error, RuntimeError)` contract is unchanged.
- New tests: repo-wide AST guard (`test_issue645_no_on_loop_checkout.py`,
  both predicates + barrier rule + seeded-violation self-probe), bounded
  checkout budget pins (`test_issue645_checkout_bound.py`), document_progress
  dispatch guard (`test_issue645_document_progress_dispatch.py`), and
  route-level HTTP coverage for the seven #592-converted handlers named in the
  issue (`test_issue645_converted_route_http.py`). Existing fixture pools and
  helper-call tests were modernized for the async surfaces (assertion intents
  unchanged).

## Why

#592 moved the 42 route-handler checkouts off the event loop and shipped an
AST guard scoped to `backend/app/api/routes/` matching only `.get_connection()`.
The same defect class — a synchronous pooled checkout whose latency is
unbounded (`Queue.get(timeout=5)` x 3, plus unbounded validation/connect work)
executed directly on the asyncio event loop — remained at 14 direct sites and
36 context-manager sites outside that guard's surface (29 outside routes/, 7
inside). Under pool exhaustion each site could freeze the loop for the full
wait budget and beyond; issue #229 recorded a live production reindex failure
in the same abort-path class. This change completes the census with the
guard's own semantics, moves every checkout onto the proven dedicated
checkout-executor path, bounds the checkout's wall-clock budget, and extends
the guard repo-wide so a new on-loop checkout fails CI.

## Known limitations

- `release_connection` remains on-loop by design: it is non-blocking
  (`in_transaction` check + `put_nowait`, dirty-only rollback) — the issue
  classifies the 56 release sites as adjacent with no queue wait.
- Sync-def checkouts (dispatched to worker threads by their callers) remain
  sync by contract; the guard's innermost-async-callable barrier rule encodes
  that contract, and the document_progress dispatch guard pins the
  helper-dispatch shape.
- `utils/transaction.py::db_transaction` has no production callers today; it
  is fixed in place rather than deleted (public util surface — removal is an
  owner decision).
