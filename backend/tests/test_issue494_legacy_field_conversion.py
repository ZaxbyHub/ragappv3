"""
Issue #494 acceptance checks — AC14 (CONFIG-004): legacy field conversion is
not applied on the live settings API path.

Root cause (verified at base a543361):
- config.py has mode="before" migration validators on the new fields
  (migrate_chunk_size_chars / migrate_chunk_overlap_chars /
  migrate_retrieval_top_k) that convert the deprecated legacy fields with a
  4x chars-per-token factor (token count * 4 = char count) and a 1x copy for
  vector_top_k -> retrieval_top_k.
- The live API path (settings.py ``_validate_settings_update`` +
  ``_apply_validated_settings``) validates the update and then persists via
  bare ``setattr(settings, field, value)`` with no conversion: PUT
  {"chunk_size": 600} stores the token count 600 on the legacy attribute and
  leaves chunk_size_chars untouched, instead of deriving
  chunk_size_chars = 600 * 4 = 2400.

Verified facts pinned by these nodes:
- The conversion factors in config.py are exactly: chunk_size x4, chunk_overlap
  x4, vector_top_k x1 (copied).
- Construction note: the migration validators run in field-declaration
  order, and the legacy fields (chunk_size etc.) are declared AFTER the new
  fields in Settings, so ``values.data`` does not contain the legacy value
  when the new-field validators run. Constructing ``Settings(chunk_size=512)``
  therefore yields the DEFAULT chunk_size_chars (2000), not 2048 — the
  migration branch is effectively unreachable through plain construction.
  The conversion contract is consequently pinned by invoking the real
  validators with the legacy value in hand (node a) and by the live API
  path (node b).
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

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

from fastapi.testclient import TestClient  # noqa: E402

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

from app.config import Settings, settings  # noqa: E402
from app.main import app  # noqa: E402


class _FakeValidationInfo:
    """Minimal stand-in for pydantic's ValidationInfo (only .data is used)."""

    def __init__(self, data):
        self.data = data


class TestLegacyConversionFactorsPreserved(unittest.TestCase):
    """AC14(a) — PRESERVING: the config.py legacy conversion factors.

    Green at the pre-fix base and must stay green: whatever fixes the live
    API path must reuse these exact factors.
    """

    def test_ac14a_config_legacy_conversion_factors(self):
        """Legacy -> new field conversion: chunk_size x4, chunk_overlap x4,
        vector_top_k x1; an explicit new-field value always passes through."""
        # 4 chars per token: 512 tokens -> 2048 chars
        self.assertEqual(
            Settings.migrate_chunk_size_chars(None, _FakeValidationInfo({"chunk_size": 512})),
            2048,
        )
        # 64 tokens overlap -> 256 chars
        self.assertEqual(
            Settings.migrate_chunk_overlap_chars(None, _FakeValidationInfo({"chunk_overlap": 64})),
            256,
        )
        # vector_top_k is copied 1:1 (same unit: chunk count)
        self.assertEqual(
            Settings.migrate_retrieval_top_k(None, _FakeValidationInfo({"vector_top_k": 7})),
            7,
        )
        # Explicit new-field values are never converted.
        self.assertEqual(
            Settings.migrate_chunk_size_chars(1234, _FakeValidationInfo({"chunk_size": 512})),
            1234,
        )
        self.assertEqual(
            Settings.migrate_retrieval_top_k(9, _FakeValidationInfo({"vector_top_k": 7})),
            9,
        )
        # No legacy value and no new value -> documented defaults.
        self.assertEqual(
            Settings.migrate_chunk_size_chars(None, _FakeValidationInfo({})), 2000
        )
        self.assertEqual(
            Settings.migrate_retrieval_top_k(None, _FakeValidationInfo({})), 12
        )


class TestLegacyConversionOnLivePath(unittest.TestCase):
    """AC14(b) — DISCRIMINATING: the live PUT path must apply the conversion."""

    def setUp(self):
        self._orig_users_enabled = settings.users_enabled
        settings.users_enabled = False
        self.client = TestClient(app)
        self.client.headers.update(
            {"Authorization": f"Bearer {settings.admin_secret_token}"}
        )
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
        self._orig_chunk_size = settings.chunk_size
        self._orig_chunk_size_chars = settings.chunk_size_chars

    def tearDown(self):
        app.dependency_overrides.pop(self._get_db, None)
        settings.users_enabled = self._orig_users_enabled
        settings.chunk_size = self._orig_chunk_size
        settings.chunk_size_chars = self._orig_chunk_size_chars
        self._test_pool.close_all()

    def test_ac14b_put_chunk_size_converts_to_chunk_size_chars(self):
        """PUT {"chunk_size": 600} (chunk_size_chars absent) must leave the
        live settings singleton with chunk_size_chars == 2400 (4x, the
        config.py factor). An explicit chunk_size_chars in the same PUT wins.

        At the pre-fix base chunk_size is setattr'd raw with no conversion, so
        chunk_size_chars keeps its previous value and this check fails.
        """
        # ── Discriminating: legacy-only update must convert ───────────────
        resp = self.client.put("/api/settings", json={"chunk_size": 600})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["chunk_size"], 600)

        print("AC14b CHECK: FAIL")
        self.assertEqual(
            settings.chunk_size_chars,
            2400,
            f"PUT chunk_size=600 did not convert: live singleton has "
            f"chunk_size_chars={settings.chunk_size_chars!r}, expected 2400 "
            f"(600 * 4, the config.py chars-per-token factor)",
        )

        # ── Preserving half: an explicit new field wins over conversion ───
        resp2 = self.client.put(
            "/api/settings", json={"chunk_size": 600, "chunk_size_chars": 1000}
        )
        self.assertEqual(resp2.status_code, 200, resp2.text)
        self.assertEqual(
            settings.chunk_size_chars,
            1000,
            "explicit chunk_size_chars must win over the chunk_size-derived "
            "value",
        )


if __name__ == "__main__":
    unittest.main()
