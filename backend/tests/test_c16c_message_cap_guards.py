"""Guardrail tests for the chat message length cap (#922 EARS1, both routes).

Non-frozen companions to the frozen C5 check: positive controls (a
cap-sized message is ACCEPTED on both routes), the streaming-route cap
(the frozen check pins only POST /api/chat), and the empty-messages guard
({"messages": []} must keep hitting the route's own 400, never a 500).
"""

import os
import sys
import types
import unittest
from dataclasses import dataclass, field
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

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

TEST_DB_PATH = None


def setUpModule():
    """Create and initialize the shared test database before any test runs."""
    global TEST_DB_PATH
    import tempfile
    from pathlib import Path

    from app.models.database import init_db

    test_data_dir = tempfile.mkdtemp(prefix="c16c-message-cap-")
    TEST_DB_PATH = Path(test_data_dir) / "test.db"
    init_db(str(TEST_DB_PATH))


@dataclass
class MockRAGSource:
    text: str
    file_id: str
    score: float
    metadata: Dict[str, Any] = field(default_factory=dict)
    parent_window_text: Any = None


class TestChatMessageCapGuardrails(unittest.TestCase):
    """Positive controls and stream-route coverage for the message cap."""

    def setUp(self):
        from app.api.deps import get_current_active_user, get_db, get_rag_engine
        from app.api.routes.chat import get_stream_auth
        from app.main import app

        self.app = app
        self.client = TestClient(app)
        self._get_rag_engine = get_rag_engine
        self._get_current_active_user = get_current_active_user
        self._get_stream_auth = get_stream_auth

        # DB override (same shape as test_chat_history_role_validation.py):
        # point get_db at the shared TEST_DB_PATH pool.
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

        mock_user = {"id": 1, "username": "testuser", "role": "admin"}
        self.app.dependency_overrides[get_current_active_user] = lambda: mock_user
        # Stream-route auth seam (chat.py documents this exact override).
        self.app.dependency_overrides[get_stream_auth] = lambda: dict(mock_user)

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
        self.app.dependency_overrides[get_rag_engine] = lambda: mock_engine

    def tearDown(self):
        self.app.dependency_overrides.pop(self._get_db, None)
        self.app.dependency_overrides.pop(self._get_rag_engine, None)
        self.app.dependency_overrides.pop(self._get_current_active_user, None)
        self.app.dependency_overrides.pop(self._get_stream_auth, None)

    def test_cap_sized_message_accepted_on_chat_route(self):
        """Positive control: exactly-at-cap is accepted (non-stream)."""
        from app.config import settings

        cap = settings.chat_message_max_chars
        response = self.client.post(
            "/api/chat",
            json={"message": "x" * cap, "stream": False},
        )
        self.assertNotEqual(
            response.status_code,
            422,
            f"an exactly-{cap}-char message must not be rejected: {response.text[:200]}",
        )

    def test_oversized_final_message_rejected_on_stream_route(self):
        """The streaming route caps the FINAL message (4xx, no generation)."""
        from app.config import settings

        cap = settings.chat_message_max_chars
        response = self.client.post(
            "/api/chat/stream",
            json={
                "messages": [
                    {"role": "user", "content": "earlier turn"},
                    {"role": "user", "content": "x" * (cap + 1)},
                ]
            },
        )
        self.assertIn(
            response.status_code,
            (413, 422),
            f"stream route must reject an over-cap final message, got "
            f"{response.status_code}: {response.text[:200]}",
        )
        self.assertEqual(
            self.query_calls,
            [],
            "the engine generation entry point must never run for a rejected payload",
        )

    def test_cap_sized_history_not_rejected_on_stream_route(self):
        """Long PRIOR history stays accepted; only the final message is capped."""
        from app.config import settings

        cap = settings.chat_message_max_chars
        response = self.client.post(
            "/api/chat/stream",
            json={
                "messages": [
                    {"role": "assistant", "content": "y" * (cap + 5000)},
                    {"role": "user", "content": "short question"},
                ]
            },
        )
        self.assertNotEqual(
            response.status_code,
            422,
            f"long prior history must stay accepted, got {response.status_code}: "
            f"{response.text[:200]}",
        )

    def test_empty_messages_list_keeps_the_route_400(self):
        """{"messages": []} hits the route's own 400, never a 500."""
        response = self.client.post("/api/chat/stream", json={"messages": []})
        self.assertEqual(
            response.status_code,
            400,
            f"empty messages must stay the route's clean 400, got "
            f"{response.status_code}: {response.text[:200]}",
        )


if __name__ == "__main__":
    unittest.main()
