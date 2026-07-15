# Streaming-chat pool release + configurable DB pool size (Issue #397, #301, #302)

## What changed

### Backend — streaming chat no longer pins a pooled connection across LLM generation (#301)
- Added a `get_stream_auth` dependency (`chat.py`) that resolves authentication
  AND the vault read-permission / all-vaults admin check inside one short-lived
  `with request.app.state.db_pool.connection() as conn:` block. The connection
  is released when the dependency returns — **before** `chat_stream` constructs
  the `StreamingResponse`. Previously, `chat_stream` resolved auth via
  `Depends(get_current_active_user)` + `Depends(get_evaluate_policy)`, both
  carrying the request-scoped yield-dependency `get_db`; FastAPI deferred its
  teardown until the SSE body completed, pinning one pooled connection across
  the entire LLM generation and capping concurrency at the pool size (~10).
- Factored `get_current_active_user`'s body into a shared
  `_resolve_active_user(conn, request, authorization, access_token)` helper
  (`deps.py`) so the DI path and the streaming auth boundary enforce identical
  authentication (JWT, denylist, fingerprint, active-user cache, `is_active`,
  `must_change_password`, password-change invalidation). No status code, detail,
  or header changed. The streaming route uses `request.app.state.db_pool`, NOT
  `get_pool`, preserving the S-003 no-standalone-`get_pool` invariant.

### Backend — DB pool no longer silently seeds at max_size=5 (#302)
- Added `Settings.db_pool_max_size` (default 10, validator `>= 1`) (`config.py`).
- Lifespan now seeds the application pool **before** `migrate_uploads`
  (`lifespan.py`), reading `settings.db_pool_max_size`. Previously,
  `migrate_uploads` ran first and — when a legacy uploads file existed —
  `_lookup_vault_id` seeded the singleton cache at the default 5, silently
  halving intended capacity.
- `_lookup_vault_id` (`upload_path.py`) now passes
  `max_size=settings.db_pool_max_size` (defense in depth).
- `get_pool` (`database.py`) now WARNS (does not raise, does not resize) when a
  caller requests a larger `max_size` than the cached pool, making the
  silent-seeding class of bug observable.

### Tests
- `test_chat_stream_connection_release.py` — mid-stream assertion that the
  outstanding connection count is 0 DURING generation (the #301
  discriminator), plus a concurrency test (max_size streams with simulated LLM
  latency) proving the pool is free during generation.
- `test_db_pool_config.py` — `db_pool_max_size` default/env/validator and the
  `get_pool` upward-drift warning.
- `test_db_pool_seeding_order.py` — pool seeded at the configured size even
  when a legacy uploads file is present (the #302 regression condition).
- Extended `test_route_policy_no_double_connection.py` with a `/api/chat/stream`
  case asserting `get_pool` is not called on the stream path.
- Updated existing stream-route tests to override the new `get_stream_auth`
  seam (`test_chat_streaming`, `test_chat_route_policy_di`,
  `test_chat_require_vault`, `test_chat_stage_events`, `test_vaults`,
  `test_integration`) and `test_deps_auth_to_thread` to inspect
  `_resolve_active_user`.

### Backend — wiki_events_stream same #301 fix
- Applied the same short-lived-connection pattern to `GET /wiki/events`
  (`wiki.py`): auth + the vault read-permission check now run inside a
  `with request.app.state.db_pool.connection() as conn:` block released before
  the SSE stream begins (was holding a pooled connection for the entire stream
  lifetime via `Depends(get_db)`, same hazard `chat_stream` had). The route no
  longer declares `get_current_active_user`/`get_evaluate_policy` deps; its S-003
  test was rewritten to assert the new boundary.

### Backend — observability + startup resilience
- Structured `db_released` event (`deps.log_db_released`) emitted by the
  streaming auth boundaries (`get_stream_auth`, `wiki_events_stream`) when the
  pre-stream connection is released — proves the #301 fix at runtime.
- Structured `pool_exhausted` event logged by `SQLiteConnectionPool.get_connection`
  when a checkout cannot be satisfied within the wait budget — surfaces the
  #301/#302 capacity class at runtime.
- Lifespan pool-seeding is now wrapped in try/except (mirroring
  `run_migrations`/`migrate_uploads`): on a seeding failure the app falls back
  to a fresh default-size pool (constructed directly, bypassing the singleton
  cache) so `app.state.db_pool` is always set and startup completes with
  degraded capacity instead of hard-crashing.

### Tests added
- `test_lifespan_pool_seeding_order.py` — source-text guard that lifespan seeds
  the pool before `migrate_uploads` and reads `settings.db_pool_max_size` (the
  primary #302 fix had no executable guard; precedent: `test_issue_263`).

## Why
Two same-class pooled-connection-lifecycle defects capped chat concurrency at
~10 (#301) and could silently halve that to ~5 (#302). #301 held a connection
across the full LLM generation via a yield-dependency whose teardown FastAPI
defers until the StreamingResponse body completes. #302's `get_pool`
first-call-wins cache let an early `migrate_uploads` caller seed the pool at the
default before lifespan sized it.

## Migration / configuration
- New optional config `DB_POOL_MAX_SIZE` (default 10). Operators with heavier
  concurrent load may raise it; values `< 1` are rejected at startup. No action
  required for existing deployments (default preserves prior behavior).

## Known caveats
- The legacy `SQLiteConnectionPool.get_connection()` is a synchronous blocking
  call; over-subscribing beyond `max_size` with truly simultaneous arrivals can
  block the event loop regardless of this fix. That is a pre-existing
  sync-in-async characteristic, separate from #301.
- **The new `get_pool` warning only fires on UPWARD size drift** (a caller
  requests a `max_size` larger than the cached pool's). It does NOT surface the
  smaller-size / no-arg callers that also pass inert values under the singleton:
  `document_processor.py` (`max_size=1`/`1`/`2`), `file_watcher.py:180`
  (`max_size=2`), and `file_watcher.py:115` (`get_pool(...)` with NO `max_size`,
  defaulting to 5). These remain functionally inert (lifespan seeds the pool at
  `db_pool_max_size` first) but are silent — candidates for cleanup in a
  follow-up. Note `file_watcher.py:115` is a latent #302-class caller: if it
  ever won the first-call race it would seed at 5; lifespan ordering is what
  keeps it inert today.
