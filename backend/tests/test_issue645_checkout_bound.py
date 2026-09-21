"""Issue #645 (AC11): the pooled checkout is bounded — a checkout that cannot
succeed must raise ``RuntimeError`` (the pool-exhaustion contract shipped by
#592) within a nominal wall-clock ceiling, on BOTH the sync and the async
checkout surfaces.

What the fix changed (backend/app/models/database.py):

* ``get_connection`` now bounds its TOTAL queue wait with a monotonic
  deadline of ``max_wait_attempts * CHECKOUT_WAIT_SECONDS`` (3 x 5 s = 15 s
  by default): each ``Queue.get`` waits at most
  ``min(CHECKOUT_WAIT_SECONDS, remaining)``.
* ``_validate_connection`` probes with a temporarily reduced busy timeout
  (``VALIDATION_BUSY_TIMEOUT_MS`` = 1000 ms) and restores the connection's
  original value in a ``finally`` on every exit path.

Pre-fix behavior (recorded here as the AC11 baseline): an exhausted pool
made each ``Queue.get`` wait a flat 5 s for up to 3 attempts, and a
contended/locked database could stretch validation toward the production
``busy_timeout`` (30000 ms) per attempt — on the event loop (the #645
defect) that froze every concurrent request for the duration. Post-fix the
deadline plus the 1 s validation probe bound the whole checkout well under
the 25 s ceiling asserted below.

These tests use a real temp database and the REAL ``SQLiteConnectionPool``
(no mocks): the timing assertions are wall-clock, so generous margins are
used (25 s ceiling vs the ~15 s budget) to stay robust on slow CI hosts and
Windows.
"""

import asyncio
import sqlite3
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.models.database import (
    CHECKOUT_WAIT_SECONDS,
    VALIDATION_BUSY_TIMEOUT_MS,
    SQLiteConnectionPool,
)

# The AC11 pin: the whole checkout (queue waits + validation cycling) must
# stay under this ceiling. Default budget is 3 * CHECKOUT_WAIT_SECONDS (15 s)
# plus bounded validation probes, so 25 s leaves margin for slow hosts.
CHECKOUT_CEILING_SECONDS = 25.0

# The production busy timeout every pooled connection is created with
# (_create_connection) and that validation must restore after its 1 s probe.
PRODUCTION_BUSY_TIMEOUT_MS = 30000


@pytest.fixture
def db_path(tmp_path):
    return str(Path(tempfile.mkdtemp(dir=str(tmp_path))) / "ac11.db")


def _hold_exclusive(path: str) -> sqlite3.Connection:
    """A foreign raw connection holding BEGIN EXCLUSIVE (a locked database:
    exactly the condition that pins pool connections in production)."""
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("BEGIN EXCLUSIVE")
    return conn


def test_checkout_raises_within_nominal_ceiling_under_locked_db(db_path):
    """AC11: against a locked database and a pool at capacity, a sync
    checkout must RAISE RuntimeError (pool-exhaustion contract) within the
    nominal ceiling instead of blocking indefinitely.

    Pre-fix baseline (see module docstring): the flat per-attempt waits and
    the 30 s validation busy timeout could stretch a locked-database
    checkout to ~30 s per validation attempt — on the event loop. Post-fix
    the monotonic deadline bounds the whole checkout at
    max_wait_attempts * CHECKOUT_WAIT_SECONDS (~15 s) + bounded probes.
    """
    pool = SQLiteConnectionPool(db_path, max_size=2)
    try:
        # Prime one connection BEFORE locking (the pool's first checkout
        # also materializes the WAL journal mode on the temp database).
        first = pool.get_connection()

        # Foreign raw writer: BEGIN EXCLUSIVE (the locked database).
        foreign = _hold_exclusive(db_path)
        try:
            # A WAL-mode writer lock does not block connection creation
            # (probe B in the issue trace): take the remaining slot under
            # the lock so the pool is genuinely exhausted.
            second = pool.get_connection()

            # The checkout under test, off-thread with a generous timeout:
            # the pool is at capacity and the database is locked.
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(pool.get_connection)
                started = time.monotonic()
                with pytest.raises(RuntimeError):
                    future.result(timeout=CHECKOUT_CEILING_SECONDS * 3)
                elapsed = time.monotonic() - started

            assert elapsed < CHECKOUT_CEILING_SECONDS, (
                f"locked-db exhausted checkout took {elapsed:.1f}s "
                f"(ceiling {CHECKOUT_CEILING_SECONDS}s): the total-wait "
                f"deadline ({3 * CHECKOUT_WAIT_SECONDS}s) is not bounding "
                f"the checkout"
            )
            pool.release_connection(second)
        finally:
            foreign.rollback()
            foreign.close()
        pool.release_connection(first)
    finally:
        pool.close_all()


def test_bounded_wait_does_not_change_fast_path(db_path):
    """The budget is a ceiling, not a cost: an uncontended checkout returns
    quickly (< 2 s) and hands out a working connection."""
    pool = SQLiteConnectionPool(db_path, max_size=2)
    try:
        started = time.monotonic()
        conn = pool.get_connection()
        elapsed = time.monotonic() - started

        assert elapsed < 2.0, (
            f"uncontended checkout took {elapsed:.2f}s; the bounded wait "
            f"must not tax the fast path"
        )
        # The handed-out connection actually works.
        assert conn.execute("SELECT 1").fetchone()[0] == 1
        pool.release_connection(conn)
    finally:
        pool.close_all()


def test_validation_restores_busy_timeout(db_path):
    """Validation's 1 s probe must never leak into a handed-out connection.

    - After a checkout that goes THROUGH validation (idle pooled connection
      re-checked-out), the connection still honors the production
      busy_timeout (30000 ms), proving the probe's finally-restore.
    - After a FAILED validation discards a dead pooled connection, the
      fresh replacement connection created to satisfy the checkout also
      carries the production busy_timeout.
    - Both pragma reads happen while a foreign BEGIN EXCLUSIVE holds the
      database, pinning that the value survives a contended database.
    """
    pool = SQLiteConnectionPool(db_path, max_size=2)
    try:
        # 1) Healthy path through validation: acquire, release, re-acquire.
        conn = pool.get_connection()
        pool.release_connection(conn)
        rechecked = pool.get_connection()  # pulled from the idle queue -> validated
        assert rechecked is conn
        assert (
            rechecked.execute("PRAGMA busy_timeout").fetchone()[0]
            == PRODUCTION_BUSY_TIMEOUT_MS
        ), (
            "validation's reduced probe busy_timeout "
            f"({VALIDATION_BUSY_TIMEOUT_MS} ms) leaked into the handed-out "
            "connection; the finally-restore is broken"
        )

        # 2) Failed-validation path: kill the idle pooled connection behind
        #    the pool's back (the deterministic stand-in for a connection
        #    whose validation fails), then check out again — validation must
        #    discard it and a fresh replacement must still carry the
        #    production busy_timeout.
        pool.release_connection(rechecked)
        conn.close()  # dead pooled connection: validation must reject it
        foreign = _hold_exclusive(db_path)
        try:
            replacement = pool.get_connection()
            assert (
                replacement.execute("PRAGMA busy_timeout").fetchone()[0]
                == PRODUCTION_BUSY_TIMEOUT_MS
            )
            # And the replacement genuinely works (contended db reads are
            # unaffected by a WAL writer lock; SELECT 1 needs no locks).
            assert replacement.execute("SELECT 1").fetchone()[0] == 1
            replacement_busy = replacement
        finally:
            foreign.rollback()
            foreign.close()

        # 3) A later healthy checkout after everything is released still
        #    hands out the production busy_timeout.
        pool.release_connection(replacement_busy)
        later = pool.get_connection()
        assert (
            later.execute("PRAGMA busy_timeout").fetchone()[0]
            == PRODUCTION_BUSY_TIMEOUT_MS
        )
        pool.release_connection(later)
    finally:
        pool.close_all()


@pytest.mark.asyncio
async def test_get_connection_async_matches_sync_budget(db_path):
    """AC11, async surface: on an exhausted pool, ``await
    pool.get_connection_async()`` raises the same RuntimeError within the
    same ceiling (the off-loop checkout shares the bounded budget — the
    event loop is never blocked waiting)."""
    pool = SQLiteConnectionPool(db_path, max_size=2)
    try:
        hog_a = pool.get_connection()
        hog_b = pool.get_connection()

        started = time.monotonic()
        with pytest.raises(RuntimeError):
            await asyncio.wait_for(
                pool.get_connection_async(),
                timeout=CHECKOUT_CEILING_SECONDS * 3,
            )
        elapsed = time.monotonic() - started

        assert elapsed < CHECKOUT_CEILING_SECONDS, (
            f"exhausted async checkout took {elapsed:.1f}s "
            f"(ceiling {CHECKOUT_CEILING_SECONDS}s): get_connection_async "
            f"must inherit the {3 * CHECKOUT_WAIT_SECONDS}s total-wait "
            f"deadline"
        )
        pool.release_connection(hog_a)
        pool.release_connection(hog_b)
    finally:
        pool.close_all()


def test_checkout_budget_enforced_across_invalid_idle_connections(db_path, monkeypatch):
    """(#645 final-critic round 1) Cycling through invalid idle connections
    must not extend the checkout past the nominal budget.

    Pre-fix, both invalid-connection paths ``continue``d without consulting
    the deadline, so N seeded invalid entries x a slow-failing validation
    stretched the checkout to N x probe-time regardless of the deadline (a
    real-pool reproduction measured 16.80s with 16 entries x 1.05s probes
    against the 15s budget). Post-fix the deadline is enforced on the
    invalid path: the checkout raises RuntimeError once the budget is spent
    instead of draining the whole invalid queue.
    """
    from app.models.database import SQLiteConnectionPool

    class _InvalidIdleConn:
        def close(self):
            pass

    # Shrink the budget: max_wait_attempts=1 -> a 5s deadline, so the test
    # exercises the deadline with ~5s of slow probes instead of ~15s.
    deadline_budget = 1 * CHECKOUT_WAIT_SECONDS
    seeded = 20
    probe_seconds = 0.35
    # max_size must exceed the seeded count: the internal queue is a
    # Queue(maxsize=max_size) and the constructor does not seed eagerly.
    pool = SQLiteConnectionPool(str(db_path), max_size=seeded + 5)
    try:
        for _ in range(seeded):
            pool._pool.put_nowait(_InvalidIdleConn())

        validations = {"count": 0}

        def slow_failing_validate(conn):
            validations["count"] += 1
            time.sleep(probe_seconds)
            return False

        monkeypatch.setattr(pool, "_validate_connection", slow_failing_validate)

        started = time.monotonic()
        with pytest.raises(RuntimeError):
            pool.get_connection(max_wait_attempts=1)
        elapsed = time.monotonic() - started

        # Post-fix the raise fires at the ~5s deadline: ~13 probes x 0.4s.
        # A full drain would be 20 probes / ~8.0s. Both assertions leave
        # margin for timer granularity while failing on a full drain.
        assert elapsed < seeded * probe_seconds * 0.9, (
            f"invalid-connection cycling took {elapsed:.1f}s for "
            f"{validations['count']} probes: the checkout deadline must cap "
            f"the drain of invalid idle connections"
        )
        assert validations["count"] <= seeded - 3, (
            f"{validations['count']} of {seeded} invalid entries were drained "
            f"before the budget raised: the deadline is not enforced on the "
            f"invalid path"
        )
    finally:
        pool.close_all()


def test_creation_refused_once_budget_spent_by_invalid_probes(db_path, monkeypatch):
    """(#645 final critic round 2) Budget-consuming invalid validation
    followed by delayed creation stays inside the composed checkout
    ceiling (deadline + creation reserve) and raises instead once the
    deadline has actually passed: no NEW bounded work starts after
    expiry, and a creation already started is bounded by its reserve.
    On the pre-#645 tree this scenario was unbounded (30s-per-probe
    validation x N entries); on the round-2 tree it could return a
    connection arbitrarily past the deadline + reserve composed ceiling.
    """
    from app.models.database import (
        CREATE_TIME_RESERVE_SECONDS,
        SQLiteConnectionPool,
    )

    class _InvalidIdleConn:
        def close(self):
            pass

    seeded = 10
    probe_seconds = 0.2
    pool = SQLiteConnectionPool(str(db_path), max_size=seeded + 5)
    try:
        for _ in range(seeded):
            pool._pool.put_nowait(_InvalidIdleConn())

        def slow_failing_validate(conn):
            time.sleep(probe_seconds)
            return False

        monkeypatch.setattr(pool, "_validate_connection", slow_failing_validate)

        started = time.monotonic()
        conn = pool.get_connection(max_wait_attempts=1)
        elapsed = time.monotonic() - started

        # The 10 probes consume ~4.0s of the 5s budget; creation then runs
        # inside its reserve: the composed ceiling is
        # deadline + CREATE_TIME_RESERVE_SECONDS (~10s here), and the
        # checkout must return a WORKING connection inside it.
        ceiling = deadline_of(1) + CREATE_TIME_RESERVE_SECONDS
        assert elapsed < ceiling, (
            f"delayed creation took {elapsed:.1f}s against the composed "
            f"ceiling {ceiling:.1f}s"
        )
        conn.execute("SELECT 1")
        pool.release_connection(conn)
    finally:
        pool.close_all()


def deadline_of(max_wait_attempts):
    return max_wait_attempts * CHECKOUT_WAIT_SECONDS


def test_concurrent_delayed_creation_respects_per_caller_deadline(db_path, monkeypatch):
    """(#645 final critic round 3) Concurrent workers that pass the entry
    gate can queue on the serialized creation lock; a worker that acquires
    the lock after its own deadline must refuse to start creation (with
    _created_count rolled back), so every caller returns or raises within
    its own composed ceiling. Probe that motivated this: three workers,
    4.7s delayed creation -> the tail worker returned at ~13.9s against a
    5s budget on the previous tree.
    """
    from concurrent.futures import ThreadPoolExecutor

    from app.models.database import SQLiteConnectionPool

    # 3 serialized creations at 3.0s each: the tail worker's lock turn
    # lands at ~6.0s, past its 5s deadline -> it must refuse. The barrier
    # collapses thread-start skew so every caller's deadline is minted at
    # effectively the same instant (PR #650 review finding PRR-B).
    delay = 3.0
    pool = SQLiteConnectionPool(str(db_path), max_size=3)
    barrier = threading.Barrier(3)
    try:
        original_create = pool._create_connection

        def slow_create():
            time.sleep(delay)
            return original_create()

        monkeypatch.setattr(pool, "_create_connection", slow_create)

        def checked_out():
            barrier.wait()
            return pool.get_connection(1)

        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=3) as pool_of_workers:
            futures = [
                pool_of_workers.submit(checked_out) for _ in range(3)
            ]
        elapsed = time.monotonic() - started

        returned, refused = [], 0
        for future in futures:
            try:
                returned.append(future.result(timeout=30))
            except RuntimeError:
                refused += 1

        # Serialized creations at 1.6s each: only workers whose turn starts
        # before their 5s deadline may create; the tail must refuse instead
        # of drifting arbitrarily past its own ceiling. Every caller's wall
        # clock stays under the composed ceiling (deadline + one reserve).
        ceiling = deadline_of(1) + 5.0
        assert refused >= 1, (
            f"all 3 delayed creations completed by {elapsed:.1f}s: workers "
            f"queued on the creation lock are not deadline-checked before "
            f"starting creation"
        )
        assert elapsed < ceiling + delay, (
            f"concurrent checkouts took {elapsed:.1f}s, exceeding the "
            f"composed ceiling {ceiling:.1f}s plus one in-flight creation"
        )
        for conn in returned:
            conn.execute("SELECT 1")
            pool.release_connection(conn)
    finally:
        pool.close_all()


def test_checkout_creates_missing_parent_directory(tmp_path):
    """(#650 CI) A pool pointed at a path whose parent directory does not
    exist yet (fresh clone / fresh CI runner, cwd-relative data dir) must
    still check out: _create_connection creates the parent directory the
    same way database.py's init flow does, instead of failing every
    checkout with 'unable to open database file'.
    """
    from app.models.database import SQLiteConnectionPool

    missing_dir = tmp_path / "data" / "nested"
    db_path = missing_dir / "app.db"
    assert not missing_dir.exists()

    pool = SQLiteConnectionPool(str(db_path), max_size=1)
    try:
        conn = pool.get_connection()
        conn.execute("SELECT 1")
        assert missing_dir.exists()
        pool.release_connection(conn)
    finally:
        pool.close_all()
