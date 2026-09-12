"""
Issue #494 acceptance check — AC27 (API-001): the custom
RequestValidationError handler copies the raw error['input'] into a
JSONResponse and blows up on bytes payloads.

Root cause (verified at base a543361):
- documents.py ``validation_exception_handler`` (registered app-wide in
  main.py) builds error dicts containing ``"input": error.get("input")`` and
  returns ``JSONResponse(status_code=422, content={"detail": error_dicts})``.
- When a client sends a non-JSON body (e.g. text/plain) to a JSON endpoint,
  FastAPI/Spectre validation reports the RAW BYTES as ``input``; the handler
  copies the bytes into the JSON response, json serialization fails inside
  the handler, and the client receives a 500 instead of the documented 422
  envelope.

Check: POST a text/plain body to a JSON endpoint on the real app; the
response must be 422 with a ``detail`` list of objects each carrying
loc/msg/type keys (the documented envelope). At the pre-fix base the
response is a 500 and this check fails.
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


class TestValidationErrorEnvelopeForRawBodies(unittest.TestCase):
    """AC27 — DISCRIMINATING: text/plain body -> documented 422 envelope."""

    def setUp(self):
        self._orig_users_enabled = settings.users_enabled
        settings.users_enabled = False
        # raise_server_exceptions=False so a broken handler surfaces as the
        # HTTP 500 the client actually receives instead of raising in-process.
        self.client = TestClient(app, raise_server_exceptions=False)
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

    def tearDown(self):
        app.dependency_overrides.pop(self._get_db, None)
        settings.users_enabled = self._orig_users_enabled
        self._test_pool.close_all()

    def test_ac27_text_plain_body_yields_422_envelope_not_500(self):
        """POST text/plain to a JSON endpoint must return the 422 envelope."""
        resp = self.client.post(
            "/api/settings",
            content=b"this is a plain-text body, not JSON",
            headers={"Content-Type": "text/plain"},
        )

        print("AC27 CHECK: FAIL")
        self.assertEqual(
            resp.status_code,
            422,
            f"validation of a text/plain body against a JSON endpoint must "
            f"return the documented 422 envelope. Got {resp.status_code} "
            f"(the RequestValidationError handler fails to serialize the raw "
            f"bytes 'input' and the request surfaces as a server error)",
        )

        body = resp.json()
        self.assertIn("detail", body)
        detail = body["detail"]
        self.assertIsInstance(detail, list)
        self.assertGreater(len(detail), 0, "detail envelope must list errors")
        for item in detail:
            self.assertIsInstance(item, dict)
            for key in ("loc", "msg", "type"):
                self.assertIn(
                    key,
                    item,
                    f"every detail entry must carry {key!r} (documented "
                    f"envelope). Entry: {item!r}",
                )


if __name__ == "__main__":
    unittest.main()
