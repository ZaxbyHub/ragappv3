"""
Issue #494 acceptance check — AC15 (CONFIG-005): retrieval_window=0 is
rejected by the write-path validator even though zero is a documented,
meaningful value.

Root cause (verified at base a543361):
- settings.py SettingsUpdate.validate_retrieval_window rejects v <= 0
  ("must be a positive integer"), so PUT {"retrieval_window": 0} fails 422.
- The retrieval UI documents the range 0-3 (zero = no context expansion),
  and document_retrieval.py treats 0 as a valid no-op:
  ``if self.retrieval_window > 0: sources = await self.expand_window(...)``
  — i.e. zero explicitly disables window expansion.

Decision (recorded per the fix plan): zero = disables expansion, consistent
across UI, validator, and retrieval engine. Boundary 3 stays accepted and -1
stays rejected.

Check: PUT {"retrieval_window": 0} -> 200 (accepted, applied);
PUT {"retrieval_window": 3} -> 200; PUT {"retrieval_window": -1} -> 422.
At the pre-fix base the zero case returns 422 and this check fails.
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

from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402


class TestRetrievalWindowZeroAccepted(unittest.TestCase):
    """AC15 — DISCRIMINATING: retrieval_window=0 must be a valid no-op value."""

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
        self._orig_window = settings.retrieval_window

    def tearDown(self):
        app.dependency_overrides.pop(self._get_db, None)
        settings.users_enabled = self._orig_users_enabled
        settings.retrieval_window = self._orig_window
        self._test_pool.close_all()

    def test_ac15_retrieval_window_zero_accepted_boundary_three_and_neg_rejected(self):
        """0 accepted (disables expansion), 3 accepted (UI max), -1 rejected."""
        resp_zero = self.client.put("/api/settings", json={"retrieval_window": 0})

        print("AC15 CHECK: FAIL")
        self.assertEqual(
            resp_zero.status_code,
            200,
            f"retrieval_window=0 must be accepted (documented range 0-3; "
            f"document_retrieval.py treats 0 as 'expansion disabled'). Got "
            f"{resp_zero.status_code}: {resp_zero.text[:300]}",
        )
        self.assertEqual(resp_zero.json()["retrieval_window"], 0)
        self.assertEqual(settings.retrieval_window, 0)

        resp_three = self.client.put("/api/settings", json={"retrieval_window": 3})
        self.assertEqual(
            resp_three.status_code,
            200,
            f"retrieval_window=3 (UI upper bound) must stay accepted. Got "
            f"{resp_three.status_code}",
        )

        resp_neg = self.client.put("/api/settings", json={"retrieval_window": -1})
        self.assertEqual(
            resp_neg.status_code,
            422,
            f"retrieval_window=-1 must stay rejected. Got "
            f"{resp_neg.status_code}",
        )


if __name__ == "__main__":
    unittest.main()
