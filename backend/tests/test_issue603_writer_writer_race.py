"""Regression tests for issue #603: unsynchronized writer-vs-writer race in
``ToggleManager``'s cache publish (follow-up from PR #599 review).

The bug: both toggle write paths committed the durable change and THEN
published the new value to the shared TTL cache as a separate, unsynchronized
step. Concurrent writers could land cache entries out of commit order
(commit-A -> commit-B -> cache-B -> cache-A), leaving the 30 s TTL cache
holding an older value than the durable DB until expiry.

The fix folds each writer's commit and cache publish into one critical
section under ``ToggleManager._lock`` via ``commit_and_publish``. These tests
port the frozen issue-tracer acceptance checks (C1-C5) into the repo suite.
"""

import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from app.api.routes.admin import _write_toggle_with_audit
from app.models.database import SQLiteConnectionPool, init_db
from app.services.toggle_manager import ToggleManager

PARK_TIMEOUT = 1.0
JOIN_TIMEOUT = 10.0


class _ObservingConn:
    """Proxy over a real pooled connection recording ``lock.locked()`` at the
    instant ``commit()`` executes (attributes cannot be set on real
    sqlite3.Connection objects)."""

    def __init__(self, real, lock):
        self.real = real
        self._lock = lock
        self.lock_observations = []
        self.rollback_calls = 0

    @property
    def in_transaction(self):
        return self.real.in_transaction

    def execute(self, sql, params=()):
        return self.real.execute(sql, params)

    def commit(self):
        self.lock_observations.append(bool(self._lock.locked()))
        return self.real.commit()

    def rollback(self):
        self.rollback_calls += 1
        return self.real.rollback()

    def __getattr__(self, name):
        return getattr(self.real, name)


class _FailingCommitConn(_ObservingConn):
    """Observing proxy whose ``commit()`` always raises (injected failure)."""

    def __init__(self, real, lock):
        super().__init__(real, lock)

    def commit(self):
        self.lock_observations.append(bool(self._lock.locked()))
        raise sqlite3.Error("injected commit failure")


class _PublishObservingCache(dict):
    """Cache dict recording whether the manager lock was held at each write.

    Pins the publish-inside-the-lock invariant directly: even if a future
    refactor moves the cache publish out of ``commit_and_publish``'s critical
    section (every other check only observes the lock at commit time), an
    unlocked publish lands here as ``False`` and fails the test.
    """

    def __init__(self, lock):
        super().__init__()
        self._lock = lock
        self.write_lock_observations = []

    def __setitem__(self, key, value):
        self.write_lock_observations.append(bool(self._lock.locked()))
        super().__setitem__(key, value)


class _ObservingPool:
    """Pool wrapper handing out observing proxies and unwrapping on release,
    so ``ToggleManager.set_toggle``'s own commit can be observed."""

    def __init__(self, pool, lock, conn_class):
        self._pool = pool
        self._lock = lock
        self._conn_class = conn_class
        self.observed = None

    def get_connection(self):
        proxy = self._conn_class(self._pool.get_connection(), self._lock)
        self.observed = proxy
        return proxy

    def release_connection(self, conn):
        self._pool.release_connection(conn.real)

    def close_all(self):
        self._pool.close_all()


class ToggleWriterCacheSerializationTest(unittest.TestCase):
    """The #603 writer-vs-writer serialization contract."""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.pool = SQLiteConnectionPool(self.db_path, max_size=4)
        init_db(self.db_path)
        self.manager = ToggleManager(self.pool)

    def tearDown(self):
        self.pool.close_all()
        Path(self.db_path).unlink(missing_ok=True)

    def _db_value(self, feature):
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT enabled FROM admin_toggles WHERE feature = ?", (feature,)
            ).fetchone()
            return None if row is None else bool(row[0])
        finally:
            conn.close()

    def _route_writer(self, feature, enabled):
        conn = self.manager.pool.get_connection()
        try:
            _write_toggle_with_audit(
                conn,
                self.manager,
                feature,
                enabled,
                user_id=None,
                ip=None,
                key_version="kv",
                hmac_digest="h",
                timestamp="ts",
            )
        finally:
            self.manager.pool.release_connection(conn)

    def test_route_writer_publishes_under_the_cache_lock(self):
        proxy = _ObservingConn(self.pool.get_connection(), self.manager._lock)
        try:
            _write_toggle_with_audit(
                proxy,
                self.manager,
                "c2_route_lock",
                True,
                user_id=None,
                ip=None,
                key_version="kv",
                hmac_digest="h",
                timestamp="ts",
            )
        finally:
            self.pool.release_connection(proxy.real)
        self.assertEqual(proxy.lock_observations, [True])
        self.assertTrue(self._db_value("c2_route_lock"))

    def test_service_writer_publishes_under_the_cache_lock(self):
        self.manager.pool = _ObservingPool(self.pool, self.manager._lock, _ObservingConn)
        self.manager.set_toggle("c3_service_lock", True)
        self.assertEqual(self.manager.pool.observed.lock_observations, [True])
        self.assertTrue(self._db_value("c3_service_lock"))
        self.assertTrue(self.manager.get_toggle("c3_service_lock", False))

    def test_commit_and_publish_contract(self):
        generation_before = self.manager._generation
        observing_cache = _PublishObservingCache(self.manager._lock)
        self.manager._cache = observing_cache
        proxy = _ObservingConn(self.pool.get_connection(), self.manager._lock)
        try:
            self.manager.set_toggle_on_connection(proxy, "contract", True)
            self.manager.commit_and_publish(proxy, "contract", True)
        finally:
            self.pool.release_connection(proxy.real)
        self.assertEqual(proxy.lock_observations, [True])
        # The publish itself must also happen under the lock, not just the
        # commit (the second_done/interleave drivers cannot see this split).
        self.assertEqual(observing_cache.write_lock_observations, [True])
        self.assertEqual(self.manager._generation, generation_before + 1)
        self.assertTrue(self.manager.get_toggle("contract", False))

        failing = _FailingCommitConn(self.pool.get_connection(), self.manager._lock)
        try:
            self.manager.set_toggle_on_connection(failing, "contract_fail", True)
            with self.assertRaises(sqlite3.Error):
                self.manager.commit_and_publish(failing, "contract_fail", True)
        finally:
            self.pool.release_connection(failing.real)
        self.assertGreaterEqual(failing.rollback_calls, 1)
        self.assertEqual(self.manager._generation, generation_before + 1)
        self.assertNotIn("contract_fail", self.manager._cache)
        # The failed write must not have published anything to the cache.
        self.assertEqual(observing_cache.write_lock_observations, [True])
        self.assertIsNone(self._db_value("contract_fail"))

    def test_service_writer_failed_commit_rolls_back_and_skips_publish(self):
        self.manager.pool = _ObservingPool(
            self.pool, self.manager._lock, _FailingCommitConn
        )
        generation_before = self.manager._generation
        with self.assertRaises(sqlite3.Error):
            self.manager.set_toggle("c5_service_failed_commit", True)
        observed = self.manager.pool.observed
        # Both rollback layers ran: commit_and_publish's guarded rollback and
        # set_toggle's own except-branch rollback.
        self.assertGreaterEqual(observed.rollback_calls, 2)
        self.assertNotIn("c5_service_failed_commit", self.manager._cache)
        self.assertEqual(self.manager._generation, generation_before)
        self.assertIsNone(self._db_value("c5_service_failed_commit"))

    def test_failed_commit_leaves_cache_untouched(self):
        publishes = []
        original_update = self.manager.update_cache
        self.manager.update_cache = lambda f, e: publishes.append((f, e))
        failing = _FailingCommitConn(self.pool.get_connection(), self.manager._lock)
        raised = None
        try:
            try:
                _write_toggle_with_audit(
                    failing,
                    self.manager,
                    "c5_failed_commit",
                    True,
                    user_id=None,
                    ip=None,
                    key_version="kv",
                    hmac_digest="h",
                    timestamp="ts",
                )
            except sqlite3.Error as exc:
                raised = exc
            finally:
                self.pool.release_connection(failing.real)
        finally:
            self.manager.update_cache = original_update
        self.assertIsInstance(raised, sqlite3.Error)
        self.assertEqual(publishes, [])
        self.assertNotIn("c5_failed_commit", self.manager._cache)
        self.assertIsNone(self._db_value("c5_failed_commit"))

    def _forced_interleave(self, first_enabled, surface):
        """Try to hold the first writer's cache publish until the second
        writer's whole write+publish has landed, on one writer surface.

        On a tree where commit and publish are one atomic step the second
        writer cannot complete while the first holds the serialization, the
        park times out, and the end state stays consistent - that is the
        assertion, not an error.
        """
        parked = threading.Event()
        second_done = threading.Event()
        original_update = self.manager.update_cache
        original_commit_and_publish = self.manager.commit_and_publish

        def hold_publish(feature_arg, enabled_arg, original):
            if enabled_arg is first_enabled and not parked.is_set():
                parked.set()
                second_done.wait(timeout=PARK_TIMEOUT)
            original(feature_arg, enabled_arg)

        self.manager.update_cache = (
            lambda f, e: hold_publish(f, e, original_update)
        )
        self.manager.commit_and_publish = (
            lambda conn, f, e: hold_publish(f, e, lambda fa, ea: original_commit_and_publish(conn, fa, ea))
        )
        errors = []

        def writer(enabled, signal_done):
            try:
                if surface == "route":
                    self._route_writer("c1_interleave", enabled)
                else:
                    self.manager.set_toggle("c1_interleave", enabled)
            except Exception as exc:
                errors.append(repr(exc))
            finally:
                # Signal the parked first writer as soon as the second writer's
                # whole write+publish finished. On the fixed tree the first
                # writer holds its open SQLite transaction across the park, so
                # the second writer cannot reach this until the park timed out;
                # on a tree where commit and publish are split again, the
                # second writer completes during the park and releases it
                # early — the invariant assert below then catches the race.
                if signal_done:
                    second_done.set()

        first = threading.Thread(target=writer, args=(first_enabled, False))
        first.start()
        self.assertTrue(parked.wait(timeout=JOIN_TIMEOUT))
        second = threading.Thread(target=writer, args=(not first_enabled, True))
        second.start()
        second.join(timeout=JOIN_TIMEOUT)
        first.join(timeout=JOIN_TIMEOUT)
        self.manager.update_cache = original_update
        self.manager.commit_and_publish = original_commit_and_publish
        self.assertFalse(errors, f"writer raised: {errors}")
        self.assertFalse(first.is_alive() or second.is_alive(), "writer thread stuck")
        return self._db_value("c1_interleave"), self.manager.get_toggle("c1_interleave", None)

    def test_forced_interleave_route_surface_cannot_diverge_cache_from_db(self):
        for first_enabled in (True, False):
            with self.subTest(first_enabled=first_enabled):
                db_value, cache_value = self._forced_interleave(first_enabled, "route")
                self.assertEqual(cache_value, db_value)
                self.assertIsNotNone(db_value)

    def test_forced_interleave_service_surface_cannot_diverge_cache_from_db(self):
        for first_enabled in (True, False):
            with self.subTest(first_enabled=first_enabled):
                db_value, cache_value = self._forced_interleave(first_enabled, "service")
                self.assertEqual(cache_value, db_value)
                self.assertIsNotNone(db_value)


if __name__ == "__main__":
    unittest.main()
