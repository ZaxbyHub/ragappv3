"""Tests for the defense-in-depth require_vault wiring in chat.py.

Verifies that:
- non_stream_chat_response forwards require_vault to RAGEngine.query
- stream_chat_response forwards require_vault to RAGEngine.query
- The /chat route derives require_vault from the user role for non-admin callers
- The /chat/stream route derives require_vault from the user role for non-admin callers
- Admins bypass require_vault (default False path)
"""

import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    import lancedb
except ImportError:
    import types

    sys.modules["lancedb"] = types.ModuleType("lancedb")

try:
    import pyarrow
except ImportError:
    import types

    sys.modules["pyarrow"] = types.ModuleType("pyarrow")

try:
    from unstructured.partition.auto import partition
except ImportError:
    import types

    _unstructured = types.ModuleType("unstructured")
    _unstructured.__path__ = []
    _unstructured.partition = types.ModuleType("unstructured.partition")
    _unstructured.partition.__path__ = []
    _unstructured.partition.auto = types.ModuleType("unstructured.partition.auto")
    _unstructured.partition.auto.partition = lambda *args, **kwargs: []
    _unstructured.chunking = types.ModuleType("unstructured.chunking")
    _unstructured.chunking.__path__ = []
    _unstructured.chunking.title = types.ModuleType("unstructured.chunking.title")
    _unstructured.chunking.title.chunk_by_title = lambda *args, **kwargs: []
    _unstructured.documents = types.ModuleType("unstructured.documents")
    _unstructured.documents.__path__ = []
    _unstructured.documents.elements = types.ModuleType(
        "unstructured.documents.elements"
    )
    _unstructured.documents.elements.Element = type("Element", (), {})
    sys.modules["unstructured"] = _unstructured
    sys.modules["unstructured.partition"] = _unstructured.partition
    sys.modules["unstructured.partition.auto"] = _unstructured.partition.auto
    sys.modules["unstructured.chunking"] = _unstructured.chunking
    sys.modules["unstructured.chunking.title"] = _unstructured.chunking.title
    sys.modules["unstructured.documents"] = _unstructured.documents
    sys.modules["unstructured.documents.elements"] = _unstructured.documents.elements

from fastapi.testclient import TestClient

from app.api.deps import (
    get_current_active_user,
    get_evaluate_policy,
    get_rag_engine,
)
from app.api.routes.chat import (
    non_stream_chat_response,
    stream_chat_response,
)
from app.main import app


class TestRequireVaultWiring(unittest.TestCase):
    """Test suite for require_vault parameter wiring."""

    def _set_route_mocks(self, user_role="member", user_id=1, vault_id=None):
        app.dependency_overrides[get_rag_engine] = lambda: MagicMock()
        app.dependency_overrides[get_current_active_user] = lambda: {
            "id": user_id,
            "username": "testuser",
            "role": user_role,
        }
        app.dependency_overrides[get_evaluate_policy] = lambda: AsyncMock(
            return_value=True
        )
        self._vault_id = vault_id

    def tearDown(self):
        for key in [get_rag_engine, get_current_active_user, get_evaluate_policy]:
            app.dependency_overrides.pop(key, None)

    def test_non_stream_forwards_require_vault_to_query(self):
        captured = {}

        async def fake_query(*args, **kwargs):
            captured["kwargs"] = kwargs
            yield {"type": "done", "sources": [], "memories_used": []}

        mock_engine = MagicMock()
        mock_engine.query = fake_query

        asyncio.run(
            non_stream_chat_response(
                "hello",
                [],
                mock_engine,
                vault_id=None,
                mode=None,
                require_vault=True,
            )
        )

        self.assertTrue(captured["kwargs"].get("require_vault"))
        self.assertEqual(captured["kwargs"].get("require_vault"), True)

    def test_stream_forwards_require_vault_to_query(self):
        captured = {}

        async def fake_query(*args, **kwargs):
            captured["kwargs"] = kwargs
            yield {"type": "done", "sources": [], "memories_used": []}

        mock_engine = MagicMock()
        mock_engine.query = fake_query

        response = stream_chat_response(
            "hello",
            [],
            mock_engine,
            vault_id=None,
            mode=None,
            user_id=1,
            require_vault=True,
        )

        # Consume the streaming response body to trigger event_generator()
        async def consume():
            async for _ in response.body_iterator:
                pass

        asyncio.run(consume())

        self.assertTrue(captured["kwargs"].get("require_vault"))
        self.assertEqual(captured["kwargs"].get("require_vault"), True)

    def test_stream_default_require_vault_false(self):
        captured = {}

        async def fake_query(*args, **kwargs):
            captured["kwargs"] = kwargs
            yield {"type": "done", "sources": [], "memories_used": []}

        mock_engine = MagicMock()
        mock_engine.query = fake_query

        response = stream_chat_response(
            "hello",
            [],
            mock_engine,
            vault_id=None,
            mode=None,
            user_id=1,
        )

        # Consume the streaming response body to trigger event_generator()
        async def consume():
            async for _ in response.body_iterator:
                pass

        asyncio.run(consume())

        self.assertIn("require_vault", captured["kwargs"])
        self.assertFalse(captured["kwargs"]["require_vault"])

    def test_chat_route_sets_require_vault_for_non_admin(self):
        self._set_route_mocks(user_role="member", vault_id=None)
        client = TestClient(app)

        response = client.post(
            "/api/chat",
            json={"message": "hello", "history": [], "vault_id": None},
        )

        self.assertEqual(response.status_code, 403)

    def test_chat_stream_route_sets_require_vault_for_non_admin(self):
        self._set_route_mocks(user_role="member", vault_id=None)
        client = TestClient(app)

        response = client.post(
            "/api/chat/stream",
            json={"messages": [{"role": "user", "content": "hello"}]},
        )

        self.assertEqual(response.status_code, 403)

    def test_chat_route_allows_admin_without_vault(self):
        self._set_route_mocks(user_role="admin", vault_id=None)
        client = TestClient(app)

        response = client.post(
            "/api/chat",
            json={"message": "hello", "history": [], "vault_id": None},
        )

        self.assertEqual(response.status_code, 200)

    def test_chat_stream_route_allows_admin_without_vault(self):
        self._set_route_mocks(user_role="admin", vault_id=None)
        client = TestClient(app)

        response = client.post(
            "/api/chat/stream",
            json={"messages": [{"role": "user", "content": "hello"}]},
        )

        self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
