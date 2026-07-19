"""Runtime rate-limit tests for POST/PUT /settings (issue #389 F-PRE-004).

These tests close the gap between the source-scan tests in
``test_rate_limiting.py::TestRateLimitingDecoratorsSettings`` (which prove the
``@limiter.limit`` decorator is PRESENT in source) and the actual HTTP behavior
the limiter is supposed to enforce. They drive the real POST/PUT ``/settings``
routes through ``TestClient`` with a fully-wired app (auth + DB + middleware)
and assert that exceeding ``admin_rate_limit`` returns HTTP 429.

They also guard the trailing-slash quota-normalization fix in
``WhitelistLimiter._check_request_limit`` (PRR-001): a client alternating
between ``/api/settings`` and ``/api/settings/`` must share ONE bucket, not
get a 2x effective limit from two independent buckets.
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

# Stub missing optional dependencies (mirrors test_vaults.py bootstrap).
try:
    import lancedb  # noqa: F401
except ImportError:
    import types
    sys.modules["lancedb"] = types.ModuleType("lancedb")
try:
    import pyarrow  # noqa: F401
except ImportError:
    import types
    sys.modules["pyarrow"] = types.ModuleType("pyarrow")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient

from app.api.deps import get_current_active_user, get_db
from app.config import settings
from app.limiter import limiter
from app.main import app
from app.models.database import _pool_cache, _pool_cache_lock, init_db
from app.security import csrf_protect


class _SettingsRateLimitHarness(unittest.TestCase):
    """Shared setup: fresh temp DB, admin auth override, csrf override, and a
    limiter reset before/after each test (mirrors the conftest autouse
    fixture but explicit here so this file is runnable standalone)."""

    @classmethod
    def setUpClass(cls):
        with _pool_cache_lock:
            for pool in list(_pool_cache.values()):
                pool.close_all()
            _pool_cache.clear()

    def setUp(self):
        limiter.reset()

        self._temp_dir = tempfile.mkdtemp()
        self._db_path = str(Path(self._temp_dir) / "app.db")
        init_db(self._db_path)

        self._original_data_dir = settings.data_dir
        settings.data_dir = Path(self._temp_dir)

        # Seed an admin user so require_role("admin") passes.
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO users (id, username, hashed_password, role, is_active) "
            "VALUES (?, ?, ?, ?, 1)",
            (1, "admin", "x", "admin"),
        )
        conn.commit()
        conn.close()

        app.dependency_overrides[get_current_active_user] = lambda: {
            "id": 1,
            "username": "admin",
            "full_name": "Admin",
            "role": "admin",
            "is_active": True,
            "must_change_password": False,
        }
        app.dependency_overrides[csrf_protect] = lambda: "test-csrf-token"

        # Provide a get_db override backed by a per-test pool so the handler's
        # _persist_settings / _compute_effective_sources can read/write.
        import _db_pool

        self._pool = _db_pool.SimpleConnectionPool(self._db_path)

        def override_get_db():
            c = self._pool.get_connection()
            try:
                yield c
            finally:
                self._pool.release_connection(c)

        app.dependency_overrides[get_db] = override_get_db

        self.client = TestClient(app)

    def _admin_rate_limit_count(self) -> int:
        """Parse the configured admin_rate_limit ("N/window") into N."""
        spec = settings.admin_rate_limit or "10/minute"
        return int(spec.split("/")[0])

    def tearDown(self):
        app.dependency_overrides.pop(get_current_active_user, None)
        app.dependency_overrides.pop(csrf_protect, None)
        app.dependency_overrides.pop(get_db, None)
        settings.data_dir = self._original_data_dir
        if hasattr(self, "_pool"):
            self._pool.close_all()
        with _pool_cache_lock:
            for pool in list(_pool_cache.values()):
                pool.close_all()
            _pool_cache.clear()
        limiter.reset()
        import shutil
        shutil.rmtree(self._temp_dir, ignore_errors=True)

    @classmethod
    def tearDownClass(cls):
        with _pool_cache_lock:
            for pool in list(_pool_cache.values()):
                pool.close_all()
            _pool_cache.clear()


class TestSettingsRateLimitRuntime(_SettingsRateLimitHarness):
    """Runtime 429 enforcement for POST/PUT /settings (PRR-003)."""

    def test_post_settings_returns_429_after_limit(self):
        """The (N+1)th POST /settings within the window returns 429.

        Uses an empty SettingsUpdate body. Each call returns 400 ("No valid
        fields provided for update") because the handler rejects an empty
        update — but slowapi increments the rate-limit counter BEFORE the
        handler body runs (the @limiter.limit wrapper wraps the handler), so
        every call counts toward admin_rate_limit regardless of the 400. The
        (N+1)th call is blocked by the limiter before the handler is invoked
        and returns 429.
        """
        n = self._admin_rate_limit_count()
        for i in range(n):
            resp = self.client.post("/api/settings", json={})
            self.assertNotEqual(
                resp.status_code, 429,
                f"call {i + 1}/{n} returned 429 too early",
            )
        resp = self.client.post("/api/settings", json={})
        self.assertEqual(
            resp.status_code, 429,
            f"call {n + 1} should return 429 (rate limited), got {resp.status_code}",
        )

    def test_put_settings_returns_429_after_limit(self):
        """The (N+1)th PUT /settings within the window returns 429."""
        n = self._admin_rate_limit_count()
        for i in range(n):
            resp = self.client.put("/api/settings", json={})
            self.assertNotEqual(resp.status_code, 429,
                                f"call {i + 1}/{n} returned 429 too early")
        resp = self.client.put("/api/settings", json={})
        self.assertEqual(resp.status_code, 429,
                         f"call {n + 1} should return 429, got {resp.status_code}")

    def test_post_and_put_share_one_bucket(self):
        """POST and PUT /settings share a single admin_rate_limit bucket
        (PRR-002, intentional behavior): exhausting the limit via POST leaves
        no quota for a subsequent PUT in the same window."""
        n = self._admin_rate_limit_count()
        for i in range(n):
            self.client.post("/api/settings", json={})
        # Bucket exhausted — PUT must now be rate-limited too.
        resp = self.client.put("/api/settings", json={})
        self.assertEqual(
            resp.status_code, 429,
            f"PUT after exhausting POST quota should be 429 (shared bucket), "
            f"got {resp.status_code}",
        )


class TestTrailingSlashRateLimitNormalization(_SettingsRateLimitHarness):
    """PRR-001: trailing-slash and non-slash variants of the SAME route share
    one rate-limit bucket (no quota-doubling via path alternation)."""

    def test_alternating_slash_and_non_slash_shares_bucket(self):
        """A client alternating POST /api/settings and POST /api/settings/ must
        exhaust a single admin_rate_limit bucket, not get 2x the limit.

        Pre-fix, slowapi keyed on the raw request path, so /api/settings and
        /api/settings/ were independent buckets. WhitelistLimiter now
        normalizes the scope path (rstrip '/') before slowapi reads it.
        """
        n = self._admin_rate_limit_count()
        # Alternate between non-slash and slash, doubling the request count.
        # If buckets were independent we'd see all 2n succeed; with the fix,
        # the (n+1)th request regardless of slash variant returns 429.
        seen_429 = False
        for i in range(2 * n):
            path = "/api/settings" if i % 2 == 0 else "/api/settings/"
            resp = self.client.post(path, json={})
            if resp.status_code == 429:
                seen_429 = True
                break
        self.assertTrue(
            seen_429,
            "expected a 429 within 2*limit alternating requests (shared bucket); "
            "if no 429, the trailing-slash quota-doubling regression returned",
        )


if __name__ == "__main__":
    unittest.main()
