# Pool release-time rollback: dirty connections can no longer poison the SQLite pool (Issue #548)

## What changed

### Backend — `SQLiteConnectionPool.release_connection` now resets open transactions on release (#548, audit finding C03)
- `release_connection` (`database.py`) rolls back any open transaction before a
  connection returns to the pool, covering both the queue path and the
  pool-full close path. Previously, a handler that returned between a DML
  statement and its `commit()` handed the pool a connection holding an open
  write transaction; the next borrower inherited it, and its own `commit()`
  durably persisted the first handler's abandoned rows (phantom commit). The
  stale transaction also silenced `PRAGMA foreign_keys` enforcement (the
  PRAGMA is a no-op inside an open transaction), so the re-borrowed connection
  ran with FK checks off.
- The rollback is a no-op for clean connections (`conn.in_transaction` is
  `False` on the common path) and can never raise: the `in_transaction` access
  and the `rollback()` call are wrapped separately so an already-closed
  connection degrades to a no-op instead of masking a handler's original
  exception in `get_db`'s bare `finally`.
- **New operator-visible log event**: a dirty release logs
  `WARNING pool_release_rollback sqlite_path=<path> dirty_release=1` (and
  `rollback_failed=1` if the rollback itself errors). This is the signal that
  some call site still leaks an open transaction — after this change it is
  defense in depth, not the only line of defense.
- `MaintenanceService.set_flag`'s optimistic-lock version-miss branch (the one
  live trigger at audit time: a concurrent maintenance-flag toggle race) now
  explicitly rolls back its no-op UPDATE instead of relying on the pool-level
  backstop.
- The misleading `auth.py` rollback rationale comments were corrected (a plain
  `SELECT` opens no transaction under `isolation_level=""`); the rollback code
  itself is retained as defense in depth.

## Operator impact

- No settings, schema, migration, or public-API changes. No action required.
- Watch for `pool_release_rollback` WARNINGs in backend logs: each one names a
  call site that returned a connection mid-transaction. After this change such
  rows no longer corrupt the pool, but the leaking call site should be fixed.
- Full-suite backend behavior is otherwise unchanged (verified: complete
  pytest suite failure/error sets identical before and after the fix).

## Rollback

Revert of the commit restores the pre-fix pool-contamination exposure
documented in audit finding C03 (phantom commits across handlers, FK
enforcement silently off on re-borrowed dirty connections) and should not be
done without re-accepting that risk.
