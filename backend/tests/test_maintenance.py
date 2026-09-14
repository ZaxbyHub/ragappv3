"""Unit tests for maintenance service with system_flags table."""

import os
import sqlite3
import sys
import tempfile
import unittest

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.models.database import SQLiteConnectionPool, init_db, run_migrations
from app.services.maintenance import MaintenanceService


class TestMaintenanceService(unittest.TestCase):
    """Test cases for MaintenanceService with system_flags table."""

    def setUp(self):
        """Create a temporary database file for each test."""
        self.temp_fd, self.temp_db_path = tempfile.mkstemp(suffix='.db')
        os.close(self.temp_fd)
        self.pool = SQLiteConnectionPool(self.temp_db_path, max_size=2)

    def tearDown(self):
        """Clean up the temporary database file."""
        self.pool.close_all()
        if os.path.exists(self.temp_db_path):
            os.remove(self.temp_db_path)

    def test_init_db_creates_system_flags_table(self):
        """Test that init_db creates system_flags table."""
        # Initialize the database
        init_db(self.temp_db_path)

        # Connect and check for system_flags table
        conn = sqlite3.connect(self.temp_db_path)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT name, type FROM sqlite_master WHERE type IN ('table') AND name = 'system_flags'"
        )
        result = cursor.fetchone()
        conn.close()

        # Assert system_flags table exists
        self.assertIsNotNone(result, "system_flags table was not created by init_db()")
        self.assertEqual(result[0], 'system_flags')

    def test_maintenance_service_initialization(self):
        """Test MaintenanceService initializes correctly."""
        init_db(self.temp_db_path)
        MaintenanceService(self.pool)

        # Check that maintenance flag row was created
        conn = sqlite3.connect(self.temp_db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT name, value, reason FROM system_flags WHERE name = 'maintenance'")
        result = cursor.fetchone()
        conn.close()

        self.assertIsNotNone(result, "maintenance flag row was not created")
        self.assertEqual(result[0], 'maintenance')
        self.assertEqual(result[1], 0)  # Should default to 0 (disabled)
        self.assertEqual(result[2], '')  # Should default to empty reason

    def test_get_flag_returns_disabled_by_default(self):
        """Test that get_flag returns disabled state by default."""
        init_db(self.temp_db_path)
        service = MaintenanceService(self.pool)

        flag = service.get_flag()

        self.assertFalse(flag.enabled)
        self.assertEqual(flag.reason, "")
        self.assertEqual(flag.version, 0)

    def test_set_flag_enables_maintenance(self):
        """Test that set_flag can enable maintenance mode."""
        init_db(self.temp_db_path)
        service = MaintenanceService(self.pool)

        service.set_flag(True, "Scheduled maintenance")
        flag = service.get_flag()

        self.assertTrue(flag.enabled)
        self.assertEqual(flag.reason, "Scheduled maintenance")
        self.assertEqual(flag.version, 1)  # Version should increment

    def test_set_flag_disables_maintenance(self):
        """Test that set_flag can disable maintenance mode."""
        init_db(self.temp_db_path)
        service = MaintenanceService(self.pool)

        # First enable
        service.set_flag(True, "Scheduled maintenance")
        flag = service.get_flag()
        self.assertTrue(flag.enabled)

        # Then disable
        service.set_flag(False, "Maintenance complete")
        flag = service.get_flag()

        self.assertFalse(flag.enabled)
        self.assertEqual(flag.reason, "Maintenance complete")
        self.assertEqual(flag.version, 2)  # Version should increment again

    def test_set_flag_updates_version_correctly(self):
        """Test that set_flag increments version on each update."""
        init_db(self.temp_db_path)
        service = MaintenanceService(self.pool)

        flag1 = service.get_flag()
        self.assertEqual(flag1.version, 0)

        service.set_flag(True, "First update")
        flag2 = service.get_flag()
        self.assertEqual(flag2.version, 1)

        service.set_flag(False, "Second update")
        flag3 = service.get_flag()
        self.assertEqual(flag3.version, 2)

        service.set_flag(True, "Third update")
        flag4 = service.get_flag()
        self.assertEqual(flag4.version, 3)

    def test_get_flag_returns_updated_at(self):
        """Test that get_flag returns the updated_at timestamp."""
        init_db(self.temp_db_path)
        service = MaintenanceService(self.pool)

        # Initial flag should have updated_at
        flag1 = service.get_flag()
        self.assertIsNotNone(flag1.updated_at)

        # After update, updated_at should be present
        service.set_flag(True, "Update")
        flag2 = service.get_flag()
        self.assertIsNotNone(flag2.updated_at)

    def test_set_flag_with_empty_reason(self):
        """Test that set_flag works with empty reason."""
        init_db(self.temp_db_path)
        service = MaintenanceService(self.pool)

        service.set_flag(True)
        flag = service.get_flag()

        self.assertTrue(flag.enabled)
        self.assertEqual(flag.reason, "")

    def test_multiple_service_instances_share_same_database(self):
        """Test that multiple MaintenanceService instances share the same database state."""
        init_db(self.temp_db_path)

        service1 = MaintenanceService(self.pool)
        service2 = MaintenanceService(self.pool)

        # Set flag via service1
        service1.set_flag(True, "Set by service1")

        # Read via service2
        flag = service2.get_flag()

        self.assertTrue(flag.enabled)
        self.assertEqual(flag.reason, "Set by service1")



class TestMaintenanceFlagCache(unittest.TestCase):
    """Issue #549 C02: the request-path flag read is TTL-cached and
    invalidated by set_flag, so same-process toggles are immediate."""

    def setUp(self):
        self.temp_fd, self.temp_db_path = tempfile.mkstemp(suffix='.db')
        os.close(self.temp_fd)
        init_db(self.temp_db_path)
        self.pool = SQLiteConnectionPool(self.temp_db_path, max_size=2)

    def tearDown(self):
        self.pool.close_all()
        if os.path.exists(self.temp_db_path):
            os.remove(self.temp_db_path)

    def _counting_pool(self, service):
        """Attach a get_connection counter AFTER service construction."""
        calls = {"n": 0}
        original = self.pool.get_connection

        def counting(*args, **kwargs):
            calls["n"] += 1
            return original(*args, **kwargs)

        self.pool.get_connection = counting
        return calls

    def test_cached_read_serves_within_ttl_without_pool(self):
        service = MaintenanceService(self.pool, flag_cache_ttl_seconds=60.0)
        first = service.get_flag_cached()
        calls = self._counting_pool(service)
        second = service.get_flag_cached()
        self.assertEqual(calls["n"], 0)
        self.assertEqual(first.version, second.version)

    def test_set_flag_invalidates_cache_immediately(self):
        service = MaintenanceService(self.pool, flag_cache_ttl_seconds=60.0)
        self.assertFalse(service.get_flag_cached().enabled)
        service.set_flag(True, "window")
        self.assertTrue(service.get_flag_cached().enabled)
        service.set_flag(False, "")
        self.assertFalse(service.get_flag_cached().enabled)

    def test_ttl_expiry_forces_a_fresh_pooled_read(self):
        service = MaintenanceService(self.pool, flag_cache_ttl_seconds=0.0)
        service.get_flag_cached()
        calls = self._counting_pool(service)
        service.get_flag_cached()
        self.assertGreaterEqual(calls["n"], 1)

    def test_raw_get_flag_stays_uncached(self):
        """get_flag() is the raw uncached read (admin response path)."""
        service = MaintenanceService(self.pool, flag_cache_ttl_seconds=60.0)
        service.get_flag_cached()
        calls = self._counting_pool(service)
        service.get_flag()
        self.assertEqual(calls["n"], 1)


class TestMaintenanceMiddlewareDispatch(unittest.TestCase):
    """Issue #549 C01/C02: dispatch behavior on the real middleware."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "mw.db")
        init_db(self.db_path)
        self.pool = SQLiteConnectionPool(self.db_path, max_size=2)
        self.service = MaintenanceService(self.pool)
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.middleware.maintenance import MaintenanceMiddleware

        app = FastAPI()
        app.state._maintenance_service_getter = lambda: self.service

        @app.get("/health")
        def health():
            return {"status": "ok"}

        @app.post("/api/auth/login")
        def login():
            return {"ok": "login"}

        @app.post("/api/documents/upload")
        def upload():
            return {"ok": "upload"}

        app.add_middleware(
            MaintenanceMiddleware,
            service_getter=app.state._maintenance_service_getter,
        )
        self.client = TestClient(app)
        self.TestClient = TestClient

    def tearDown(self):
        self.pool.close_all()
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_get_requests_never_touch_the_pool(self):
        calls = {"n": 0}
        original = self.pool.get_connection

        def counting(*args, **kwargs):
            calls["n"] += 1
            return original(*args, **kwargs)

        self.pool.get_connection = counting
        calls["n"] = 0
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(calls["n"], 0)

    def test_health_stays_fast_under_pool_exhaustion(self):
        held = [self.pool.get_connection() for _ in range(2)]
        try:
            import time

            t0 = time.monotonic()
            resp = self.client.get("/health")
            elapsed = time.monotonic() - t0
            self.assertEqual(resp.status_code, 200)
            self.assertLess(elapsed, 1.0, f"GET /health took {elapsed:.2f}s")
        finally:
            for conn in held:
                self.pool.release_connection(conn)

    def test_login_and_upload_dispatch_under_maintenance(self):
        self.service.set_flag(True, "window")
        login = self.client.post("/api/auth/login")
        self.assertEqual(login.status_code, 200)
        upload = self.client.post("/api/documents/upload")
        self.assertEqual(upload.status_code, 503)
        self.assertEqual(upload.json()["error"], "maintenance")
        self.assertEqual(upload.headers.get("Retry-After"), "300")

    def test_fail_open_when_pool_exhausted_on_mutating_request(self):
        """PR #593 review F-003: the documented fail-open path for pool
        exhaustion (RuntimeError from get_connection, not sqlite3.Error) —
        the middleware must warn and allow, not propagate."""
        import logging

        held = [self.pool.get_connection() for _ in range(2)]
        records = []

        class Capture(logging.Handler):
            def emit(self, record):
                records.append(record)

        capture = Capture(level=logging.WARNING)
        root = logging.getLogger()
        root.addHandler(capture)
        try:
            resp = self.client.post("/api/documents/upload")
        finally:
            root.removeHandler(capture)
            for conn in held:
                self.pool.release_connection(conn)
        self.assertEqual(resp.status_code, 200)
        maintenance_warnings = [
            r for r in records if "maintenance" in (r.name or "").lower()
        ]
        self.assertTrue(
            maintenance_warnings,
            "expected a maintenance-logger WARNING on pool-exhaustion fail-open",
        )

    def test_fail_open_warns_when_flag_row_is_missing(self):
        import logging

        conn = self.pool.get_connection()
        try:
            conn.execute("DELETE FROM system_flags WHERE name = 'maintenance'")
            conn.commit()
        finally:
            self.pool.release_connection(conn)
        self.service._invalidate_flag_cache()

        records = []

        class Capture(logging.Handler):
            def emit(self, record):
                records.append(record)

        capture = Capture(level=logging.WARNING)
        root = logging.getLogger()
        root.addHandler(capture)
        try:
            resp = self.client.post("/api/documents/upload")
        finally:
            root.removeHandler(capture)
        self.assertEqual(resp.status_code, 200)
        maintenance_warnings = [
            r for r in records if "maintenance" in (r.name or "").lower()
        ]
        self.assertTrue(
            maintenance_warnings,
            "expected a WARNING from a maintenance logger on flag-read failure",
        )



class TestMaintenanceAgainstRealApp(unittest.TestCase):
    """Issue #549 C01 end-to-end: the real auth/documents routers mounted
    under app.main keep sign-in reachable during a maintenance window while
    data-mutating routes stay blocked."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "real_app.db")
        init_db(self.db_path)
        run_migrations(self.db_path)

        from app.config import settings

        self._settings = settings
        self._original_jwt_secret = settings.jwt_secret_key
        self._original_users_enabled = settings.users_enabled
        settings.jwt_secret_key = "test-secret-key-for-testing-at-least-32-chars-long"
        settings.users_enabled = True

        self.pool = SQLiteConnectionPool(self.db_path, max_size=5)
        self.service = MaintenanceService(self.pool)

        from app.api.deps import get_db
        from app.main import app as main_app
        from app.security import CSRFManager

        def get_test_db():
            conn = self.pool.get_connection()
            try:
                yield conn
            finally:
                self.pool.release_connection(conn)

        main_app.dependency_overrides[get_db] = get_test_db
        main_app.state.csrf_manager = CSRFManager(
            redis_url="redis://localhost:6379/0", ttl=900
        )
        # The middleware's lazy getter (main.py) reads this attribute.
        main_app.state.maintenance_service = self.service

        from fastapi.testclient import TestClient

        self.client = TestClient(main_app)
        self.app = main_app

    def tearDown(self):
        self._settings.jwt_secret_key = self._original_jwt_secret
        self._settings.users_enabled = self._original_users_enabled
        self.app.dependency_overrides.clear()
        self.app.state.maintenance_service = None
        self.pool.close_all()
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _csrf_pair(self):
        response = self.client.get("/api/csrf-token")
        self.assertEqual(response.status_code, 200)
        token = response.json()["csrf_token"]
        cookie = self.client.cookies.get("X-CSRF-Token")
        self.assertIsNotNone(cookie)
        return cookie, token

    def test_sign_in_survives_maintenance_window(self):
        cookie, token = self._csrf_pair()
        registered = self.client.post(
            "/api/auth/register",
            json={"username": "mnt-window-user", "password": "Password123"},
            cookies={"X-CSRF-Token": cookie},
            headers={"X-CSRF-Token": token},
        )
        self.assertEqual(
            registered.status_code, 200, f"register failed: {registered.text}"
        )

        self.service.set_flag(True, "maintenance window test")

        cookie, token = self._csrf_pair()
        login = self.client.post(
            "/api/auth/login",
            json={"username": "mnt-window-user", "password": "Password123"},
            cookies={"X-CSRF-Token": cookie},
            headers={"X-CSRF-Token": token},
        )
        self.assertEqual(login.status_code, 200, f"login failed: {login.text}")

        # CSRF tokens are single-use: fetch a fresh pair per protected POST.
        cookie, token = self._csrf_pair()
        refresh = self.client.post(
            "/api/auth/refresh",
            cookies={"X-CSRF-Token": cookie},
            headers={"X-CSRF-Token": token},
        )
        self.assertEqual(refresh.status_code, 200, f"refresh failed: {refresh.text}")

        # Registration and data-mutating routes stay blocked in the same state.
        cookie, token = self._csrf_pair()
        register_blocked = self.client.post(
            "/api/auth/register",
            json={"username": "mnt-second-user", "password": "Password123"},
            cookies={"X-CSRF-Token": cookie},
            headers={"X-CSRF-Token": token},
        )
        self.assertEqual(register_blocked.status_code, 503)
        self.assertEqual(register_blocked.json()["error"], "maintenance")

        upload = self.client.post("/api/documents/upload")
        self.assertEqual(upload.status_code, 503)
        self.assertEqual(upload.json()["error"], "maintenance")

        health = self.client.get("/health")
        self.assertEqual(health.status_code, 200)


class TestBackgroundProcessorEnqueueMaintenance(unittest.TestCase):
    """Issue #549 defect class: enqueue's maintenance read must be off-loop
    and fail open when the read fails, while still refusing under an enabled
    flag."""

    def setUp(self):
        self.temp_fd, self.temp_db_path = tempfile.mkstemp(suffix=".db")
        os.close(self.temp_fd)
        init_db(self.temp_db_path)
        self.pool = SQLiteConnectionPool(self.temp_db_path, max_size=2)
        self.service = MaintenanceService(self.pool)

    def tearDown(self):
        self.pool.close_all()
        if os.path.exists(self.temp_db_path):
            os.remove(self.temp_db_path)

    def _processor(self):
        from app.services.background_tasks import BackgroundProcessor

        return BackgroundProcessor(maintenance_service=self.service)

    def test_enqueue_refuses_while_flag_enabled(self):
        from app.services.document_processor import DocumentProcessingError

        self.service.set_flag(True, "window")
        proc = self._processor()
        with self.assertRaises(DocumentProcessingError):
            import asyncio

            asyncio.run(proc.enqueue(file_path="x.txt", vault_id=1))

    def test_enqueue_fails_open_when_flag_row_is_missing(self):
        conn = self.pool.get_connection()
        try:
            conn.execute("DELETE FROM system_flags WHERE name = 'maintenance'")
            conn.commit()
        finally:
            self.pool.release_connection(conn)
        self.service._invalidate_flag_cache()

        proc = self._processor()
        import asyncio

        # Must complete without raising (fail open) and land on the queue.
        asyncio.run(proc.enqueue(file_path="x.txt", vault_id=1))
        self.assertFalse(proc.queue.empty())

    def test_enqueue_uses_cached_read_not_a_per_call_pool_checkout(self):
        proc = self._processor()
        import asyncio

        asyncio.run(proc.enqueue(file_path="a.txt", vault_id=1))
        calls = {"n": 0}
        original = self.pool.get_connection

        def counting(*args, **kwargs):
            calls["n"] += 1
            return original(*args, **kwargs)

        self.pool.get_connection = counting
        asyncio.run(proc.enqueue(file_path="b.txt", vault_id=1))
        self.assertEqual(
            calls["n"],
            0,
            "enqueue must serve the flag from the cache, not a pooled checkout",
        )



class TestFlagCacheRaceRegression(unittest.TestCase):
    """PR #593 review F-001: a cache-miss read whose SELECT started before a
    concurrent set_flag commit must NOT write the stale value back over the
    invalidation (generation guard). Deterministic interleave via a
    once-only release hook, mirroring the confirmed reproduction probe."""

    def setUp(self):
        self.temp_fd, self.temp_db_path = tempfile.mkstemp(suffix='.db')
        os.close(self.temp_fd)
        init_db(self.temp_db_path)
        self.pool = SQLiteConnectionPool(self.temp_db_path, max_size=2)
        self.service = MaintenanceService(self.pool, flag_cache_ttl_seconds=60.0)

    def tearDown(self):
        self.pool.close_all()
        if os.path.exists(self.temp_db_path):
            os.remove(self.temp_db_path)

    def test_stale_read_cannot_repoison_cache_after_toggle(self):
        import threading

        # Warm + invalidate so the reader is on a cache miss.
        self.assertFalse(self.service.get_flag_cached().enabled)
        self.service._invalidate_flag_cache()

        original_release = self.pool.release_connection
        read_started = threading.Event()
        release_read = threading.Event()
        paused_once = []

        def slow_release(conn):
            # Pause ONLY the reader's first release: its SELECT has read the
            # old (disabled) value; its write-back has not happened yet.
            original_release(conn)
            if not paused_once:
                paused_once.append(True)
                read_started.set()
                release_read.wait(timeout=10)

        self.pool.release_connection = slow_release
        reader_result = {}

        def reader():
            reader_result["flag"] = self.service.get_flag_cached()

        reader_t = threading.Thread(target=reader)
        reader_t.start()
        self.assertTrue(read_started.wait(timeout=5))

        # Toggle commits + invalidates WHILE the reader's stale value is
        # in hand (pre-fix, the reader then re-poisons the cache).
        self.service.set_flag(True, "race regression")
        release_read.set()
        reader_t.join(timeout=10)

        self.assertFalse(
            reader_result["flag"].enabled,
            "reader's DB SELECT predated the toggle, so it read the old value",
        )
        # The generation guard must discard the stale write-back: the very
        # next cached read sees the toggle.
        self.assertTrue(self.service.get_flag_cached().enabled)

    def test_get_flag_async_runs_off_the_calling_thread(self):
        """PR #593 review (fresh-002): pin that get_flag_async dispatches the
        pooled read to a worker thread, not the caller's thread."""
        import threading

        seen_threads = []
        original_get_flag = self.service.get_flag

        def recording_get_flag():
            seen_threads.append(threading.get_ident())
            return original_get_flag()

        self.service.get_flag = recording_get_flag
        import asyncio

        asyncio.run(self.service.get_flag_async())
        self.assertEqual(len(seen_threads), 1)
        self.assertNotEqual(
            seen_threads[0],
            threading.get_ident(),
            "get_flag_async must run the pooled read off the calling thread",
        )


if __name__ == '__main__':
    unittest.main()
