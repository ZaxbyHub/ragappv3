# 603 — toggle writer commit+cache serialization (issue #603)

## What changed

- `backend/app/services/toggle_manager.py`:
  new `ToggleManager.commit_and_publish(conn, feature, enabled)` commits the
  caller's durable toggle transaction and publishes the new value to the TTL
  cache **inside one critical section** under `_lock`, then bumps the cache
  generation. `set_toggle` now routes its commit through it.
- `backend/app/api/routes/admin.py`:
  `_write_toggle_with_audit` (the `POST /admin/toggles` write path) ends with
  `commit_and_publish` instead of a bare `conn.commit()` followed by an
  unsynchronized `toggle_manager.update_cache(...)` call.
- `update_cache` itself is unchanged and remains available for callers that
  already hold a durable commit; its docstring now points writers at
  `commit_and_publish`.

## Why

`_write_toggle_with_audit` committed the toggle and then updated the cache as
a second, unsynchronized step (commit at the old `admin.py:92`, cache write at
the old `:96`), and `ToggleManager.set_toggle` had the same shape. `BEGIN
IMMEDIATE` serializes the SQLite writes and `update_cache`'s internal `_lock`
serializes the cache mutations, but nothing ordered a writer's cache publish
against *another writer's* commit — two concurrent `POST /admin/toggles`
calls could interleave commit-A → commit-B → cache-B → cache-A, leaving the
30 s TTL cache holding the older value while the DB held the newer one until
the next invalidation or TTL expiry. Holding `_lock` across commit + publish
makes the pair atomic with respect to other writers: the cache can never hold
a value older than the latest durable commit. Deadlock-free by construction:
every writer acquires SQLite's write lock before it can reach `_lock`, and no
holder of `_lock` blocks on SQLite. The reader-side #596 generation guard is
unchanged and the publish still bumps `_generation`, so in-flight stale reads
continue to be discarded.

Latent today (the only wired toggle's `app.state` value has no readers), but
the endpoint is wired and any new toggle consumer would have observed the
divergence, per the issue's revisit trigger.
