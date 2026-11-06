"""Issue #991 acceptance check — C9 (DISCRIMINATING, backend).

This file is FROZEN for trace 1040-991-health-401-chat-cap-sync.

GET /api/settings (backend/app/api/routes/settings.py:1784-1802, available to
any authenticated user) must expose the live-message cap
``chat_message_max_chars`` (backend/app/config.py:601, default 20000) so the
frontend composer can align its client gate to the enforced server bound
(issue #991 AC5). Two legs:

- DEFAULT: with the shipped default configured, the response carries exactly
  20000 for the field.
- NON-DEFAULT: with ``settings.chat_message_max_chars`` overridden to a
  non-default value (5000), the response carries exactly that value — proving
  the field is wired to the configured setting, not a hardcoded default.

At base ``SettingsResponse`` has no such field and ``_build_settings_dict``
never emits it, so the key is absent from the response on BOTH legs — this
check is RED at base (assertion failure, not an import error) and GREEN after
the fix.

Route-environment idiom mirrors backend/tests/test_a04_memory_update_fields.py
(temp DB + migrations, get_db / get_current_active_user dependency overrides,
TestClient(app)).
"""

import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path

# Add parent directory to path for imports (repo test convention).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

CSRF_TEST_POLICY = "naive"


def _stub_optional_modules() -> None:
    """Stub missing optional heavy deps so importing app.main is cheap."""
    for name in ("lancedb", "pyarrow"):
        if name in sys.modules:
            continue
        try:
            __import__(name)
        except ImportError:
            sys.modules[name] = types.ModuleType(name)
    try:
        from unstructured.partition.auto import partition  # noqa: F401

        return
    except Exception:
        pass
    unstructured = types.ModuleType("unstructured")
    unstructured.__path__ = []
    partition_pkg = types.ModuleType("unstructured.partition")
    partition_pkg.__path__ = []
    auto = types.ModuleType("unstructured.partition.auto")
    auto.partition = lambda *args, **kwargs: []
    chunking = types.ModuleType("unstructured.chunking")
    chunking.__path__ = []
    title = types.ModuleType("unstructured.chunking.title")
    title.chunk_by_title = lambda *args, **kwargs: []
    documents = types.ModuleType("unstructured.documents")
    documents.__path__ = []
    elements = types.ModuleType("unstructured.documents.elements")
    elements.Element = type("Element", (), {})
    unstructured.partition = partition_pkg
    partition_pkg.auto = auto
    chunking.title = title
    documents.elements = elements
    for name, mod in [
        ("unstructured", unstructured),
        ("unstructured.partition", partition_pkg),
        ("unstructured.partition.auto", auto),
        ("unstructured.chunking", chunking),
        ("unstructured.chunking.title", title),
        ("unstructured.documents", documents),
        ("unstructured.documents.elements", elements),
    ]:
        sys.modules[name] = mod


_stub_optional_modules()

from _db_pool import SimpleConnectionPool  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402
from app.models.database import init_db, run_migrations  # noqa: E402

# The shipped default cap (backend/app/config.py). Pinned explicitly so the
# assertion does not depend on environment overrides leaking into the
# settings singleton.
_EXPECTED_DEFAULT_CAP = 20000


class TestSettingsChatCapExposure(unittest.TestCase):
    """C9: authenticated GET /api/settings exposes chat_message_max_chars."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = str(Path(self.temp_dir) / "test.db")
        init_db(self.db_path)
        run_migrations(self.db_path)

        self.pool = SimpleConnectionPool(self.db_path)
        self.client = TestClient(app)

        from app.api.deps import get_current_active_user, get_db

        self._deps = (get_db, get_current_active_user)

        def override_get_db():
            conn = self.pool.get_connection()
            try:
                yield conn
            finally:
                self.pool.release_connection(conn)

        # Any authenticated user may read settings (the route's own contract);
        # a non-admin role also exercises the redaction path, which must NOT
        # strip the cap (it is not an infra/secret field).
        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_active_user] = lambda: {
            "id": 7,
            "username": "member1",
            "role": "member",
            "is_active": 1,
            "must_change_password": 0,
        }

        # Register cleanups BEFORE the mutations (LIFO, repo precedent).
        self.addCleanup(app.dependency_overrides.clear)
        self.addCleanup(self.pool.close_all)
        self.addCleanup(shutil.rmtree, self.temp_dir, True)
        self._original_cap = settings.chat_message_max_chars
        self.addCleanup(self._restore_cap)
        settings.chat_message_max_chars = _EXPECTED_DEFAULT_CAP

    def _restore_cap(self):
        settings.chat_message_max_chars = self._original_cap

    def test_get_settings_exposes_chat_message_max_chars(self):
        response = self.client.get(
            "/api/settings", headers={"Authorization": "Bearer test-token"}
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertIn(
            "chat_message_max_chars",
            body,
            "GET /settings must expose the live-message cap so the composer "
            "gate can match the server bound (issue #991 AC5)",
        )
        self.assertIsInstance(body["chat_message_max_chars"], int)
        self.assertEqual(body["chat_message_max_chars"], settings.chat_message_max_chars)
        self.assertEqual(body["chat_message_max_chars"], _EXPECTED_DEFAULT_CAP)

    def test_get_settings_reflects_configured_non_default_cap(self):
        # A NON-DEFAULT configured cap must flow through verbatim: the field
        # carries the configured value, never a hardcoded default.
        settings.chat_message_max_chars = 5000
        try:
            response = self.client.get(
                "/api/settings", headers={"Authorization": "Bearer test-token"}
            )
            self.assertEqual(response.status_code, 200, response.text)
            body = response.json()
            self.assertIn(
                "chat_message_max_chars",
                body,
                "GET /settings must expose the live-message cap so the composer "
                "gate can match the server bound (issue #991 AC5)",
            )
            self.assertEqual(
                body["chat_message_max_chars"],
                5000,
                "the exposed cap must be the CONFIGURED value (5000), proving "
                "the response field is wired to settings.chat_message_max_chars "
                "rather than a hardcoded default",
            )
            self.assertNotEqual(body["chat_message_max_chars"], _EXPECTED_DEFAULT_CAP)
        finally:
            settings.chat_message_max_chars = _EXPECTED_DEFAULT_CAP


if __name__ == "__main__":
    unittest.main()
