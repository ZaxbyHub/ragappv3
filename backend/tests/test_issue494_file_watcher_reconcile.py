"""
Issue #494 acceptance check — AC13 (CONFIG-001): file watcher lifecycle is
never reconciled when auto-scan settings are saved.

Root cause (verified at base a543361):
- POST/PUT /api/settings (settings.py post_settings/put_settings) persist and
  apply ``auto_scan_enabled`` / ``auto_scan_interval_minutes`` to the settings
  singleton but contain no file-watcher hook.
- The running FileWatcher never sees the change: it is only started once at
  startup, and the scan interval is captured a single time when the watch
  loop starts (file_watcher.py ``_watch_loop``:
  ``interval_seconds = settings.auto_scan_interval_minutes * 60``).

Check: with the real app and a real FileWatcher instance on
``app.state.file_watcher`` (whose ``scan_once`` is instrumented so no real
directories/DB are touched), saving ``auto_scan_enabled: false`` must stop
the watcher WITHOUT an app restart, and saving
``auto_scan_enabled: true`` + a new interval must have it running again.

The request and the watcher task run on the same event loop: the app is
driven in-process through ``httpx.ASGITransport`` inside an
``IsolatedAsyncioTestCase``, so any (present or fixed) reconciliation inside
the request cycle operates on the same loop the watcher lives on.
"""

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# Stub missing optional dependencies (mirrors the other backend test files)
try:
    import lancedb  # noqa: F401
except ImportError:
    import types
    sys.modules['lancedb'] = types.ModuleType('lancedb')

try:
    import pyarrow  # noqa: F401
except ImportError:
    import types
    sys.modules['pyarrow'] = types.ModuleType('pyarrow')

try:
    from unstructured.partition.auto import partition  # noqa: F401
except ImportError:
    import types
    _unstructured = types.ModuleType('unstructured')
    _unstructured.__path__ = []
    _unstructured.partition = types.ModuleType('unstructured.partition')
    _unstructured.partition.__path__ = []
    _unstructured.partition.auto = types.ModuleType('unstructured.partition.auto')
    _unstructured.partition.auto.partition = lambda *args, **kwargs: []
    sys.modules['unstructured'] = _unstructured
    sys.modules['unstructured.partition'] = _unstructured.partition
    sys.modules['unstructured.partition.auto'] = _unstructured.partition.auto

import httpx  # noqa: E402

TEST_DB_PATH = None
TEST_DATA_DIR = None


def _setup_test_db():
    global TEST_DB_PATH, TEST_DATA_DIR
    TEST_DATA_DIR = tempfile.mkdtemp()
    TEST_DB_PATH = Path(TEST_DATA_DIR) / "test.db"
    from app.models.database import init_db
    init_db(str(TEST_DB_PATH))
    return str(TEST_DB_PATH)


_setup_test_db()

from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402
from app.services.file_watcher import FileWatcher  # noqa: E402

_MISSING = object()


class TestFileWatcherReconcileOnSave(unittest.IsolatedAsyncioTestCase):
    """AC13 — DISCRIMINATING: saving auto-scan settings must reconcile the watcher."""

    async def asyncSetUp(self):
        self._orig_users_enabled = settings.users_enabled
        settings.users_enabled = False

        from app.api.deps import get_db
        from app.models.database import get_pool

        self._test_pool = get_pool(str(TEST_DB_PATH))

        def override_get_db():
            conn = self._test_pool.get_connection()
            try:
                yield conn
            finally:
                self._test_pool.release_connection(conn)

        app.dependency_overrides[get_db] = override_get_db
        self._get_db = get_db

        # Real FileWatcher with an instrumented scan_once: the watch loop
        # still runs for real (event handling, task lifecycle), but no
        # directories or DB are touched.
        self.watcher = FileWatcher(processor=AsyncMock(), pool=self._test_pool)
        self.watcher.scan_once = AsyncMock(return_value=0)
        self._prev_watcher = getattr(app.state, "file_watcher", _MISSING)
        app.state.file_watcher = self.watcher

        # Keep the singleton's auto-scan flags deterministic and restorable.
        self._orig_auto_scan = settings.auto_scan_enabled
        self._orig_interval = settings.auto_scan_interval_minutes
        settings.auto_scan_enabled = True
        settings.auto_scan_interval_minutes = 60  # 3600s — no churn in the loop

        await self.watcher.start()
        self.assertTrue(self.watcher.is_running, "precondition: watcher running")

        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers={"Authorization": f"Bearer {settings.admin_secret_token}"},
        )

    async def asyncTearDown(self):
        await self.client.aclose()
        # At base the watcher is never stopped by the route — clean it up so
        # no watch task leaks out of this test.
        if self.watcher.is_running:
            self.watcher._shutdown_event.set()
            task = self.watcher._watching_task
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            self.watcher._running = False
        settings.auto_scan_enabled = self._orig_auto_scan
        settings.auto_scan_interval_minutes = self._orig_interval
        if self._prev_watcher is _MISSING:
            try:
                del app.state.file_watcher
            except AttributeError:
                pass
        else:
            app.state.file_watcher = self._prev_watcher
        app.dependency_overrides.pop(self._get_db, None)
        settings.users_enabled = self._orig_users_enabled
        self._test_pool.close_all()

    async def test_ac13_save_reconciles_watcher_without_restart(self):
        """POST auto_scan_enabled=false stops the watcher; re-enabling restarts it.

        At the pre-fix base the settings save path has no watcher hook, so the
        watcher state is unchanged after the save and this check fails.
        """
        # ── Disable via the real API ──────────────────────────────────────
        resp = await self.client.post(
            "/api/settings", json={"auto_scan_enabled": False}
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertFalse(resp.json()["auto_scan_enabled"])

        # Give any reconciliation task spawned inside the request cycle a
        # chance to run on this loop.
        await asyncio.sleep(0.25)

        print("AC13 CHECK: FAIL")
        self.assertFalse(
            self.watcher.is_running,
            "FileWatcher still running after saving auto_scan_enabled=false "
            "— the settings save path does not reconcile the watcher (state "
            "only changes after an app restart)",
        )
        task = self.watcher._watching_task
        self.assertTrue(
            task is None or task.done(),
            "FileWatcher watch task still alive after saving "
            "auto_scan_enabled=false",
        )

        # ── Re-enable with a new, smaller interval ────────────────────────
        resp2 = await self.client.post(
            "/api/settings",
            json={"auto_scan_enabled": True, "auto_scan_interval_minutes": 5},
        )
        self.assertEqual(resp2.status_code, 200, resp2.text)
        await asyncio.sleep(0.25)

        self.assertTrue(
            self.watcher.is_running,
            "FileWatcher not running after saving auto_scan_enabled=true with "
            "a new interval — watcher must restart without an app restart",
        )
        task2 = self.watcher._watching_task
        self.assertIsNotNone(task2, "no watch task after re-enable save")
        self.assertFalse(
            task2.done(),
            "watch task after re-enable save is already finished — watcher "
            "not actually running",
        )


if __name__ == "__main__":
    unittest.main()
