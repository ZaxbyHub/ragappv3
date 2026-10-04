"""Edge pins for issue #700 store/pool transaction work (non-frozen corpus).

Two edges the plan critic flagged as unverified by the frozen checks:

1. ``move_documents(vault, [], missing_target)`` must STILL raise
   FolderNotFoundError after the #700 reordering (BEGIN -> require ->
   compute -> empty: rollback+return 0). The naive reorder would put the
   empty-``valid_files`` early return before the target check and silently
   flip this documented 404 into a 200-with-zero-moved.

2. A creator whose turn to create arrives after its own deadline must
   return its already-incremented ``_created_count`` slot (the refusal-path
   decrement). A forgotten decrement there would permanently shrink the
   pool's usable capacity.
"""

import sqlite3
import threading
import time
from pathlib import Path

from app.models.database import (
    CHECKOUT_WAIT_SECONDS,
    SQLiteConnectionPool,
    run_migrations,
)
from app.services.folder_store import FolderNotFoundError, FolderStore


def _fresh_conn(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _seed_vault(conn: sqlite3.Connection) -> int:
    cur = conn.execute("INSERT INTO vaults (name) VALUES ('v1')")
    conn.commit()
    return int(cur.lastrowid)


def test_move_documents_empty_list_missing_target_still_raises(tmp_path):
    """Edge pin (plan-critic round 1, test gap 2): with no valid files AND a
    missing target folder, the documented FolderNotFoundError contract wins
    over the empty-list return-0 path."""
    db_path = tmp_path / "edges.db"
    run_migrations(str(db_path))
    conn = _fresh_conn(db_path)
    try:
        vault_id = _seed_vault(conn)
        store = FolderStore(conn)
        try:
            store.move_documents(vault_id, [999999], folder_id=424242)
            raised = "none"
        except FolderNotFoundError:
            raised = "FolderNotFoundError"
        assert raised == "FolderNotFoundError"
    finally:
        conn.close()


def test_deadline_refused_creation_returns_its_slot(tmp_path):
    """Edge pin (plan-critic round 1, test gap 3): a creator that queued on
    the creation mutex and only got its turn after its own deadline refuses
    — and must give back the ``_created_count`` slot it incremented, or the
    pool's usable capacity permanently shrinks by one per refusal."""
    pool = SQLiteConnectionPool(str(tmp_path / "slots.db"), max_size=2)

    real_create = pool._create_connection
    first_turn_started = threading.Event()
    held: list[sqlite3.Connection] = []
    first_done = threading.Event()

    def slow_then_fast_create():
        # First creator holds its turn long enough that the second creator's
        # short deadline passes while queued behind it on _create_lock.
        if not first_turn_started.is_set():
            first_turn_started.set()
            time.sleep(CHECKOUT_WAIT_SECONDS + 1.0)
        return real_create()

    def first_caller() -> None:
        held.append(pool.get_connection(max_wait_attempts=5))
        first_done.set()

    pool._create_connection = slow_then_fast_create  # type: ignore[method-assign]
    try:
        t = threading.Thread(target=first_caller)
        t.start()
        assert first_turn_started.wait(timeout=5.0), "first creator never started"
        # Second caller: a fresh short deadline that expires while it is
        # queued behind the first creator's slow turn. max_size=2 lets both
        # increment; only the deadline refusal stands between them.
        deadline = time.monotonic() + 0.1
        try:
            pool.get_connection(max_wait_attempts=1, deadline=deadline)
            refused = False
        except RuntimeError:
            refused = True
        assert refused, "second creator should have refused (deadline passed)"
        assert first_done.wait(timeout=15.0), "first creator never finished"
        t.join(timeout=5.0)
        # Both slots accounted: one held by the first caller, one returned
        # by the refusal — never decremented twice, never leaked.
        assert pool._created_count == 1
    finally:
        pool._create_connection = real_create  # type: ignore[method-assign]
        for conn in held:
            pool.release_connection(conn)
        pool.close_all()
