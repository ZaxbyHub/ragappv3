"""Acceptance check (C16, C5) — chat message size cap at the API boundary.

Freezes the acceptance criterion that POST /api/chat must reject an
oversized ``message`` payload BEFORE any generation work happens:

- the ``chat_message_max_chars`` setting must exist, and
- a message of ``cap + 1`` characters must answer 413/422 while the RAG
  engine's generation entry point is never invoked.

DISCRIMINATING: at the pre-fix base the setting does not exist, so the
first assertion fails (RED for the right reason).

Route-test harness copied from test_chat_history_role_validation.py:
module-level stubs for lancedb/pyarrow/unstructured, a setUpModule temp
SQLite via app.models.database.init_db, and dependency overrides for
get_current_active_user / get_db / get_rag_engine.
"""

import sys
import types
import unittest
from typing import Any, Dict
from unittest.mock import MagicMock

# Stub optional dependencies (same pattern as other chat tests)
_stubs = [
    ("lancedb",),
    ("pyarrow",),
]
for pkg_info in _stubs:
    pkg = pkg_info[0]
    if pkg not in sys.modules:
        sys.modules[pkg] = types.ModuleType(pkg)

_unstructured_stub = types.ModuleType("unstructured")
_unstructured_stub.__path__ = []
_unstructured_stub.partition = types.ModuleType("unstructured.partition")
_unstructured_stub.partition.__path__ = []
_unstructured_stub.partition.auto = types.ModuleType("unstructured.partition.auto")
_unstructured_stub.partition.auto.partition = lambda *a, **kw: []
sys.modules["unstructured"] = _unstructured_stub
sys.modules["unstructured.partition"] = _unstructured_stub.partition
sys.modules["unstructured.partition.auto"] = _unstructured_stub.partition.auto

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.usefixtures("ready_vector_store")

# Temporary test database (same pattern as test_chat_history_role_validation.py).
TEST_DB_PATH = None


def setUpModule():
    """Create and initialize the shared test database before any test runs."""
    global TEST_DB_PATH
    import tempfile
    from pathlib import Path

    from app.models.database import init_db

    test_data_dir = tempfile.mkdtemp(prefix="c16-chat-message-cap-test-")
    TEST_DB_PATH = Path(test_data_dir) / "test.db"
    init_db(str(TEST_DB_PATH))


class TestChatMessageCapAPI(unittest.TestCase):
    """API-level test for the chat message size cap.

    The engine's generation entry point is the async-generator ``query``
    method the non-stream route iterates; every invocation is recorded so
    the test can prove an oversized payload never reaches generation.
    """

    def setUp(self):
        # Import here so stubs are in place first
        from app.api.deps import get_current_active_user, get_db, get_rag_engine
        from app.main import app

        self.app = app
        self.client = TestClient(app)
        self._get_rag_engine = get_rag_engine
        self._get_current_active_user = get_current_active_user

        # DB override: point get_db at the shared TEST_DB_PATH pool (same
        # rationale as test_chat_history_role_validation.py).
        from app.models.database import get_pool

        self._test_pool = get_pool(str(TEST_DB_PATH))

        def override_get_db():
            conn = self._test_pool.get_connection()
            try:
                yield conn
            finally:
                self._test_pool.release_connection(conn)

        self._get_db = get_db
        self.app.dependency_overrides[self._get_db] = override_get_db

        # Auth override
        mock_user = {"id": 1, "username": "testuser", "role": "admin"}
        self.app.dependency_overrides[self._get_current_active_user] = lambda: mock_user

        # RAG engine override: MagicMock engine whose generation entry
        # point (query) records every invocation.
        mock_engine = MagicMock()
        self.query_calls: list = []

        async def mock_query(*args, **kwargs):
            self.query_calls.append((args, kwargs))
            yield {"type": "content", "content": "Hello"}
            yield {
                "type": "done",
                "sources": [],
                "memories_used": [],
                "score_type": "distance",
            }

        mock_engine.query = mock_query
        self.app.dependency_overrides[self._get_rag_engine] = lambda: mock_engine

    def tearDown(self):
        self.app.dependency_overrides.pop(self._get_db, None)
        self.app.dependency_overrides.pop(self._get_rag_engine, None)
        self.app.dependency_overrides.pop(self._get_current_active_user, None)

    def test_oversized_chat_message_rejected_before_generation(self):
        """A message longer than chat_message_max_chars answers 413/422 with
        no generation work."""
        from app.config import settings

        cap = getattr(settings, "chat_message_max_chars", None)
        assert cap is not None, (
            "chat_message_max_chars setting must exist so POST /api/chat can "
            "reject oversized message payloads before any generation work"
        )

        response = self.client.post(
            "/api/chat",
            json={"message": "x" * (cap + 1), "stream": False},
        )
        self.assertIn(
            response.status_code,
            (413, 422),
            f"Expected 413 or 422 for a {cap + 1}-char message, got "
            f"{response.status_code}: {response.text[:200]}",
        )
        self.assertEqual(
            self.query_calls,
            [],
            "the RAG engine's generation entry point must never be invoked "
            "for a payload rejected by the message size cap",
        )


if __name__ == "__main__":
    unittest.main()
