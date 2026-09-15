# Truthful startup and stream logging (Issue #558, C13/C14/C16)

## What changed

### Backend
- `VectorStore.has_parent_window_text_sample` now builds its metadata filter
  from lancedb's synchronous `table.query()` builder instead of
  `await self.table.search().where(...)`: on the previous chain the
  `.where`/`.limit` calls ran on the coroutine object itself (and lancedb
  0.36's no-argument `search()` raises internally), so every boot silently
  degraded the parent-window diagnostic to a 50-row scan and emitted a
  swallowed `RuntimeWarning: coroutine 'AsyncTable.search' was never
  awaited`. The 50-row fallback for other/older API shapes is unchanged.
- The chat streaming heartbeat's cleanup (`stream_chat_response`) now
  retrieves the outcome of an already-finished follow-up `__anext__` task
  instead of only cancelling pending ones. A failed stream no longer logs
  asyncio's `Task exception was never retrieved` at ERROR — a line that was
  indistinguishable from a crash in triage. Real (non-StopAsyncIteration)
  outcomes keep a DEBUG breadcrumb; cancel-on-disconnect is unchanged.
- `CSRFManager` treats an empty `REDIS_URL` as "disabled": the documented
  no-Redis configuration (used by CI and supported deployments) no longer
  attempts `redis.from_url("")` and no longer logs
  `Redis unavailable for CSRF: ...` (a scheme error) at WARNING on every
  boot. It goes straight to the SQLite-backed store at INFO, matching the
  guards at the sibling `from_url` sites (embeddings, query transformer).
  A genuine outage (non-empty URL that fails to connect) still logs the
  WARNING.

### Operator-visible outcome
- Boot logs no longer contain the parent-window `RuntimeWarning`, the
  spurious post-error asyncio ERROR, or the false Redis-outage WARNING.
- With parent-window retrieval enabled, the startup diagnostic now reflects
  a real query: chunks with stored parent windows are detected regardless
  of their position in the table (previously only the first 50 rows were
  ever consulted, and on real lancedb tables even those were misread).
- No API, SSE-frame, configuration, or storage behavior change; no
  migration involved.

### Rollback
Revert the commit. Each of the three fixes reverts independently (restore
the un-awaited search chain, the cancel-only cleanup, or the unconditional
`from_url` attempt). No schema/config changes to undo.

### Tests
- New regression suites `backend/tests/test_issue558_c13_*.py`,
  `test_issue558_c14_*.py`, `test_issue558_c16_*.py` (including a
  real-LanceDB check proving the primary query finds a parent-window row
  beyond the 50-row fallback window), a source-contract guardrail
  (`test_issue558_guardrail_no_arg_search.py`), and an updated call-shape
  pin in `test_bottleneck_fixes.py`.
