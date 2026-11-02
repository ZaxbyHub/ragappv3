"""End-to-end maintenance recovery flow (#731; review PRR-012).

Composes the pieces the frozen checks cover individually into the real user
journey: a ``must_change_password`` user authenticates and completes the
forced password change DURING a maintenance window (the recovery exit the
change-password exemption exists for) while a non-exempt mutating route
stays blocked by the same window. Non-frozen; complements frozen C16 (the
middleware-bypass harness), C18 (sign-in survives a window) and C19 (the
flagged-user 403 gate).

The harness mirrors test_change_password_must_change_flag.py (real app,
temp-DB pool, get_db + csrf_protect dependency overrides) plus f02's
maintenance wiring (``app.state.maintenance_service``); registration happens
BEFORE the window opens because register is deliberately NOT exempt.

No network access; no production code modified by this module.
"""

import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Stub missing optional dependencies (mirrors test_f02_short_circuit_logging.py)
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

try:
    from unstructured.partition.auto import partition  # noqa: F401
except ImportError:
    import types

    _unstructured = types.ModuleType("unstructured")
    _unstructured.__path__ = []
    _unstructured.partition = types.ModuleType("unstructured.partition")
    _unstructured.partition.__path__ = []
    _unstructured.partition.auto = types.ModuleType("unstructured.partition.auto")
    _unstructured.partition.auto.partition = lambda *args, **kwargs: []
    _unstructured.documents = types.ModuleType("unstructured.documents")
    _unstructured.documents.elements = types.ModuleType(
        "unstructured.documents.elements"
    )
    _unstructured.documents.elements.Element = type("Element", (), {})
    sys.modules["unstructured"] = _unstructured
    sys.modules["unstructured.partition"] = _unstructured.partition
    sys.modules["unstructured.partition.auto"] = _unstructured.partition.auto
    sys.modules["unstructured.documents"] = _unstructured.documents
    sys.modules["unstructured.documents.elements"] = _unstructured.documents.elements

from fastapi.testclient import TestClient  # noqa: E402

from app.config import settings  # noqa: E402
from app.models.database import (  # noqa: E402
    SQLiteConnectionPool,
    init_db,
    run_migrations,
)


class TestFlaggedUserRecoversDuringWindow(unittest.TestCase):
    """The composed journey the frozen checks cover piecewise."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "f04-recovery.db")
        init_db(self.db_path)
        run_migrations(self.db_path)

        self._saved = (
            settings.jwt_secret_key,
            settings.users_enabled,
            settings.app_root_path,
        )
        settings.jwt_secret_key = "test-secret-key-for-testing-at-least-32-chars-long"
        settings.users_enabled = True
        settings.app_root_path = ""

        self.pool = SQLiteConnectionPool(self.db_path, max_size=5)

        from app.api.deps import get_db
        from app.main import app as main_app
        from app.security import csrf_protect
        from app.services.maintenance import MaintenanceService

        self.app = main_app
        self._saved_overrides = dict(main_app.dependency_overrides)
        self._saved_service = getattr(main_app.state, "maintenance_service", None)

        def get_test_db():
            conn = self.pool.get_connection()
            try:
                yield conn
            finally:
                self.pool.release_connection(conn)

        main_app.dependency_overrides[get_db] = get_test_db
        main_app.dependency_overrides[csrf_protect] = lambda: "test-csrf-token"
        self._service = MaintenanceService(self.pool)
        # The window is NOT enabled here: registration is deliberately NOT
        # maintenance-exempt, so the test body opens the window only after
        # the user exists (see the flow below).
        main_app.state.maintenance_service = self._service
        self.client = TestClient(main_app)

    def tearDown(self):
        (
            settings.jwt_secret_key,
            settings.users_enabled,
            settings.app_root_path,
        ) = self._saved
        self.app.dependency_overrides.clear()
        self.app.dependency_overrides.update(self._saved_overrides)
        self.app.state.maintenance_service = self._saved_service
        self._service.set_flag(False, "f04 window closed")
        self.pool.close_all()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _get_user_id(self, username: str) -> int:
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT id FROM users WHERE username = ?", (username,)
            ).fetchone()
            return row[0]
        finally:
            conn.close()

    def _set_must_change_password(self, user_id: int, value: int) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "UPDATE users SET must_change_password = ? WHERE id = ?",
                (value, user_id),
            )
            conn.commit()
        finally:
            conn.close()

    def _get_must_change_password(self, user_id: int) -> int:
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT must_change_password FROM users WHERE id = ?", (user_id,)
            ).fetchone()
            return int(row[0])
        finally:
            conn.close()

    def test_flagged_user_completes_forced_change_during_window(self):
        """Registration happens while the window is off (register is not
        exempt); the window then blocks a non-exempt mutating route, the
        flagged user logs in (exempt) and completes the forced change on the
        real route (the recovery exit), and the flag clears — the full #731
        recovery journey end to end."""
        reg = self.client.post(
            "/api/auth/register",
            json={"username": "f04recover", "password": "OldPass123"},
        )
        self.assertIn(reg.status_code, (200, 201), reg.text)

        user_id = self._get_user_id("f04recover")
        self._set_must_change_password(user_id, 1)

        # NOW open the window: a non-exempt mutating route is blocked.
        self._service.set_flag(True, "f04 recovery window")
        blocked = self.client.post("/api/documents/upload")
        self.assertEqual(blocked.status_code, 503, blocked.text)

        # Login is maintenance-exempt: the flagged user authenticates.
        login = self.client.post(
            "/api/auth/login",
            json={"username": "f04recover", "password": "OldPass123"},
        )
        self.assertEqual(login.status_code, 200, login.text)
        token = login.json()["access_token"]

        # The recovery exit: the real change-password route passes the window
        # (any non-503 must be an actual 200 here — this closes review
        # PRR-012 and sharpens frozen C16's != 503 on the stub route).
        resp = self.client.post(
            "/api/auth/change-password",
            headers={"Authorization": f"Bearer {token}"},
            json={"current_password": "OldPass123", "new_password": "NewPass456"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIn("access_token", resp.json())
        self.assertEqual(self._get_must_change_password(user_id), 0)


if __name__ == "__main__":
    unittest.main()
