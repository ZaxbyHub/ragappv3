"""
Basic API integration tests using unittest and FastAPI TestClient.
"""

import os
import sys

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Stub missing optional dependencies
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

import unittest
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

from app.api.deps import get_db, get_llm_health_checker, get_model_checker
from app.main import app


class TestAPI(unittest.TestCase):
    """Test suite for API endpoints."""

    def setUp(self):
        """Set up test client."""
        self.client = TestClient(app)

    def test_get_health_returns_status_ok(self):
        """Test GET /health returns status ok."""
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "ok")

    def test_get_api_health_returns_status_ok_with_llm_models(self):
        """Test GET /api/health returns status ok with llm and models data."""
        # Configure mock checkers to return deterministic values matching actual service structure
        mock_llm_checker = AsyncMock()
        mock_llm_checker.check_all = AsyncMock(
            return_value={
                "ok": True,
                "embeddings": {"ok": True, "error": None},
                "chat": {"ok": True, "error": None},
                "error": None,
            }
        )
        # check_chat_modes must return a JSON-serializable dict; an un-mocked
        # AsyncMock returns a MagicMock which causes infinite recursion in the
        # FastAPI response encoder.
        mock_llm_checker.check_chat_modes = AsyncMock(
            return_value={"thinking": False, "instant": False}
        )

        mock_model_checker = AsyncMock()
        mock_model_checker.check_models = AsyncMock(
            return_value={
                "embedding_model": {"available": True, "error": None},
                "chat_model": {"available": True, "error": None},
            }
        )

        # Override dependencies with mocks
        app.dependency_overrides[get_llm_health_checker] = lambda: mock_llm_checker
        app.dependency_overrides[get_model_checker] = lambda: mock_model_checker

        try:
            response = self.client.get("/api/health?deep=true")
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertEqual(data["status"], "ok")
            self.assertIn("llm", data)
            self.assertIn("models", data)
            self.assertEqual(data["llm"]["ok"], True)
            self.assertEqual(data["llm"]["embeddings"]["ok"], True)
            self.assertEqual(data["llm"]["chat"]["ok"], True)
            self.assertEqual(data["models"]["embedding_model"]["available"], True)
            self.assertEqual(data["models"]["chat_model"]["available"], True)
        finally:
            app.dependency_overrides.pop(get_llm_health_checker, None)
            app.dependency_overrides.pop(get_model_checker, None)

    def test_get_api_settings_returns_expected_keys(self):
        """Test GET /api/settings returns expected configuration keys."""
        from app.api.deps import get_current_active_user

        app.dependency_overrides[get_current_active_user] = lambda: {
            "id": 0, "username": "admin", "role": "superadmin",
            "is_active": 1, "must_change_password": 0,
        }
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = []
        app.dependency_overrides[get_db] = lambda: mock_conn
        try:
            response = self.client.get("/api/settings")
            self.assertEqual(response.status_code, 200)
            data = response.json()

            expected_keys = [
                "chunk_size",
                "chunk_overlap",
                "max_context_chunks",
                "rag_relevance_threshold",
            ]
            for key in expected_keys:
                self.assertIn(key, data)
        finally:
            app.dependency_overrides.pop(get_current_active_user, None)
            app.dependency_overrides.pop(get_db, None)


if __name__ == "__main__":
    unittest.main()
