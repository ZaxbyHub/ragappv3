# 596 — toggle_manager.py TTL cache stale write-back race (issue #596)

## What changed

- `backend/app/services/toggle_manager.py`:
  `ToggleManager` now guards the write-back in `get_toggle` with an integer
  generation counter (`_generation`) following the same pattern the
  `MaintenanceService.get_flag_cached` fix (PR #593 / de4d03ba) established:
  - `get_toggle` snapshots `_generation` under `self._lock` before its
    out-of-lock pooled DB read, and only writes the fetched value back to
    `_cache` (under the lock) if `_generation` is unchanged. A read whose
    DB SELECT started before a concurrent write/invalidation can no longer
    re-poison the cache with the stale pre-write value.
  - `update_cache` and `clear_cache` bump `_generation` under the same lock
    on every invalidation, so an in-flight stale read is always discarded.
- New regression test `backend/tests/test_toggle_manager_cache_race.py`
  (`TestToggleManagerCacheRace.test_stale_read_cannot_repoison_cache_after_toggle`):
  a deterministic concurrent interleave that pauses the reader between its
  DB read and its cache write-back, toggles via `set_toggle` (which calls
  `update_cache`), then asserts the very next cached read serves the new
  value. RED on the pre-fix code (the stale read re-poisoned the cache;
  `AssertionError: assert False is True`), GREEN after the fix.

## Why

The TTL cache in `ToggleManager.get_toggle` (30 s `CACHE_TTL`) previously
read the underlying value outside its lock (correct — it avoids holding the
lock across blocking SQLite IO), then reacquired the lock only to
unconditionally overwrite the cached entry. A toggle write that commits
between the reader's DB SELECT and its write-back invalidates the cache via
`update_cache`, but the in-flight reader then lands its stale pre-write
value over the freshly updated entry, serving a stale toggle to the request
path (e.g. the `model_validation` toggle consulted via
`app.state.toggle_manager.get_toggle`) for up to a full 30 s TTL window.
This is the same race shape fixed in PR #593 review F-001 for
`MaintenanceService.get_flag_cached` (de4d03ba); this pre-existing
`ToggleManager` site was filed as #596.

## Migration steps

None. Behavior-only change; no schema, config-key, API-shape, or
persistence changes. The new `_generation` attribute is private to
`ToggleManager` and initialized in `__init__`.

## Known caveats

- Same-process visibility is immediate (the write path runs
  `update_cache`/`clear_cache` under the same process lock). Cross-process
  propagation remains bounded by the 30 s TTL, unchanged by this fix —
  a toggle written in another worker/process is still not observed until the
  cache expires, exactly as before.
- The generation guard makes the cache-miss write-back lossy by design: a
  read that loses the generation race is simply not cached (its result is
  still returned to its own caller, taken from a read that predated the
  write). The next read serves the fresh value.
