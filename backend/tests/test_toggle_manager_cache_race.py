"""Regression test for issue #596: stale-write-back-after-invalidation race
in ``ToggleManager``'s in-process TTL cache.

The bug (same shape as PR #593 review F-001, fixed in ``MaintenanceService``
at de4d03ba): ``get_toggle`` reads the DB value OUTSIDE the cache lock, then
reacquires the lock only to UNCONDITIONALLY overwrite the cached entry. An
in-flight read whose SELECT started before a concurrent
``set_toggle``/``update_cache`` invalidation can land after it and re-poison
the cache with the stale pre-write value for a full TTL window (30 s).
"""

import os
import tempfile
import threading
from pathlib import Path

from app.models.database import SQLiteConnectionPool, init_db
from app.services.toggle_manager import ToggleManager


class TestToggleManagerCacheRace:
    """Deterministic concurrent regression for the #596 stale write-back."""

    def test_stale_read_cannot_repoison_cache_after_toggle(self):
        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        pool = SQLiteConnectionPool(db_path, max_size=2)
        try:
            init_db(db_path)
            manager = ToggleManager(pool)

            # Seed an existing toggle so the reader's SELECT is a real read.
            manager.set_toggle("feature_x", False)
            assert manager.get_toggle("feature_x", False) is False
            # Invalidate so the reader is on a cache miss (the race window).
            manager.clear_cache("feature_x")

            original_release = pool.release_connection
            read_started = threading.Event()
            release_read = threading.Event()
            paused_once = []

            def slow_release(conn):
                # Pause ONLY the reader's first release: its SELECT has read
                # the old (disabled) value; its write-back has not happened yet.
                original_release(conn)
                if not paused_once:
                    paused_once.append(True)
                    read_started.set()
                    release_read.wait(timeout=10)

            pool.release_connection = slow_release
            reader_result = {}

            def reader():
                reader_result["value"] = manager.get_toggle("feature_x", False)

            reader_t = threading.Thread(target=reader)
            reader_t.start()
            assert read_started.wait(timeout=5)

            # Toggle commits + invalidates WHILE the reader's stale value is
            # in hand (pre-fix, the reader then re-poisons the cache).
            manager.set_toggle("feature_x", True)
            release_read.set()
            reader_t.join(timeout=10)

            assert reader_result["value"] is False, (
                "reader's DB SELECT predated the toggle, so it read the old "
                "value"
            )
            # The generation guard must discard the stale write-back: the
            # very next cached read sees the toggle.
            assert manager.get_toggle("feature_x", False) is True
        finally:
            pool.close_all()
            Path(db_path).unlink(missing_ok=True)
