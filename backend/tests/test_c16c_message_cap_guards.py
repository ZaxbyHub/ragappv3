"""Guardrail tests for the chat message length cap (#922 EARS1, both routes).

Non-frozen companions to the frozen C5 check: positive controls (a
cap-sized message is ACCEPTED on both routes), the streaming-route cap
(the frozen check pins only POST /api/chat), and the empty-messages guard
({"messages": []} must keep hitting the route's own 400, never a 500).
"""

import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
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

TEST_DB_PATH = None
_TEST_DATA_DIR = None


def setUpModule():
    """Create and initialize the shared test database before any test runs."""
    global TEST_DB_PATH, _TEST_DATA_DIR

    from app.models.database import init_db

    _TEST_DATA_DIR = tempfile.mkdtemp(prefix="c16c-message-cap-")
    TEST_DB_PATH = Path(_TEST_DATA_DIR) / "test.db"
    init_db(str(TEST_DB_PATH))


def tearDownModule():
    """Remove the module's temp dir (review PRR-027b: no leaked mkdtemp)."""
    shutil.rmtree(_TEST_DATA_DIR, ignore_errors=True)


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
        """Positive control: exactly-at-cap is ACCEPTED (200, generation runs).

        Strengthened per review PRR-019/F-006: assertNotEqual(422) passed on
        500/401/404/413 — assert the real success and that generation ran."""
        from app.config import settings

        cap = settings.chat_message_max_chars
        response = self.client.post(
            "/api/chat",
            json={"message": "x" * cap, "stream": False},
        )
        self.assertEqual(
            response.status_code,
            200,
            f"an exactly-{cap}-char message must be accepted: {response.text[:200]}",
        )
        self.assertEqual(
            len(self.query_calls),
            1,
            "the accepted path must reach the engine exactly once",
        )

    def test_oversized_final_message_rejected_on_stream_route(self):
        """The streaming route caps the FINAL message (422, no generation).

        The cap lives in the route body after get_stream_auth and the role
        check (review F-001), so the rejection is authenticated, carries the
        named detail, and does not echo the message list."""
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
        self.assertEqual(
            response.status_code,
            422,
            f"stream route must reject an over-cap final message with 422, got "
            f"{response.status_code}: {response.text[:200]}",
        )
        self.assertIn("chat_message_max_chars", response.text)
        self.assertNotIn("x" * 100, response.text, "the payload must not be echoed")
        self.assertEqual(
            self.query_calls,
            [],
            "the engine generation entry point must never run for a rejected payload",
        )

    def test_exact_cap_final_message_accepted_on_stream_route(self):
        """Exactly-at-cap final message is accepted on the stream route —
        kills the `>` → `>=` boundary mutant (review F-006)."""
        from app.config import settings

        cap = settings.chat_message_max_chars
        response = self.client.post(
            "/api/chat/stream",
            json={
                "messages": [
                    {"role": "user", "content": "x" * cap},
                ]
            },
        )
        self.assertEqual(
            response.status_code,
            200,
            f"an exactly-{cap}-char final message must be accepted: "
            f"{response.text[:200]}",
        )
        self.assertEqual(len(self.query_calls), 1)

    def test_oversized_non_user_final_message_keeps_role_400(self):
        """An oversized final message with a non-user role gets the route's
        role 400 (error-code ordering preserved by the route-body cap,
        review PRR-026/F-001)."""
        from app.config import settings

        cap = settings.chat_message_max_chars
        response = self.client.post(
            "/api/chat/stream",
            json={
                "messages": [
                    {"role": "assistant", "content": "x" * (cap + 1)},
                ]
            },
        )
        self.assertEqual(
            response.status_code,
            400,
            f"the role check must precede the cap check, got {response.status_code}: "
            f"{response.text[:200]}",
        )
        self.assertIn("must be from the user", response.text)

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
        self.assertEqual(
            response.status_code,
            200,
            f"long prior history must stay accepted, got {response.status_code}: "
            f"{response.text[:200]}",
        )
        self.assertEqual(len(self.query_calls), 1)

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
