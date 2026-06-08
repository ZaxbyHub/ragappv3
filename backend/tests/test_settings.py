"""
Backend settings API tests using unittest and FastAPI TestClient.

Tests cover SettingsResponse fields (reranking, hybrid search) and SettingsUpdate validation.
All external services (LLM, vector store) are mocked for deterministic tests.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# Stub missing optional dependencies
try:
    import lancedb
except ImportError:
    import types
    sys.modules['lancedb'] = types.ModuleType('lancedb')

try:
    import pyarrow
except ImportError:
    import types
    sys.modules['pyarrow'] = types.ModuleType('pyarrow')

try:
    from unstructured.partition.auto import partition
except ImportError:
    import types
    _unstructured = types.ModuleType('unstructured')
    _unstructured.__path__ = []
    _unstructured.partition = types.ModuleType('unstructured.partition')
    _unstructured.partition.__path__ = []
    _unstructured.partition.auto = types.ModuleType('unstructured.partition.auto')
    _unstructured.partition.auto.partition = lambda *args, **kwargs: []
    _unstructured.chunking = types.ModuleType('unstructured.chunking')
    _unstructured.chunking.__path__ = []
    _unstructured.chunking.title = types.ModuleType('unstructured.chunking.title')
    _unstructured.chunking.title.chunk_by_title = lambda *args, **kwargs: []
    _unstructured.documents = types.ModuleType('unstructured.documents')
    _unstructured.documents.__path__ = []
    _unstructured.documents.elements = types.ModuleType('unstructured.documents.elements')
    _unstructured.documents.elements.Element = type('Element', (), {})
    sys.modules['unstructured'] = _unstructured
    sys.modules['unstructured.partition'] = _unstructured.partition
    sys.modules['unstructured.partition.auto'] = _unstructured.partition.auto
    sys.modules['unstructured.chunking'] = _unstructured.chunking
    sys.modules['unstructured.chunking.title'] = _unstructured.chunking.title
    sys.modules['unstructured.documents'] = _unstructured.documents
    sys.modules['unstructured.documents.elements'] = _unstructured.documents.elements

from fastapi.testclient import TestClient

# Create a temporary database for testing
TEST_DB_PATH = None
TEST_DATA_DIR = None


def setup_test_db():
    """Set up a temporary test database."""
    global TEST_DB_PATH, TEST_DATA_DIR
    TEST_DATA_DIR = tempfile.mkdtemp()
    TEST_DB_PATH = Path(TEST_DATA_DIR) / "test.db"

    # Import and initialize the database
    from app.models.database import init_db
    init_db(str(TEST_DB_PATH))
    return str(TEST_DB_PATH)


def get_test_settings():
    """Get test settings with temporary database path."""
    from app.config import Settings

    settings = Settings()
    settings.data_dir = Path(TEST_DATA_DIR)
    return settings


# Set up test database before importing app
setup_test_db()

from app.config import settings
from app.main import app


class TestSettingsResponseFields(unittest.TestCase):
    """Tests for SettingsResponse including reranking and hybrid search fields."""

    def setUp(self):
        self._orig_users_enabled = settings.users_enabled
        settings.users_enabled = False
        self.client = TestClient(app)
        self.client.headers.update(
            {"Authorization": f"Bearer {settings.admin_secret_token}"}
        )
        # Override get_db to use a pool that allows cross-thread usage
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
        # Restore get_db dependency
        app.dependency_overrides.pop(self._get_db, None)
        settings.users_enabled = self._orig_users_enabled

    def test_settings_response_includes_reranker_fields(self):
        """Test GET /api/settings includes reranking configuration fields."""
        response = self.client.get("/api/settings")

        self.assertEqual(response.status_code, 200)
        data = response.json()

        # Verify reranker fields are present
        self.assertIn("reranker_url", data)
        self.assertIn("reranker_model", data)
        self.assertIn("reranking_enabled", data)
        self.assertIn("reranker_top_n", data)
        self.assertIn("initial_retrieval_top_k", data)

        # Verify default values
        self.assertIsInstance(data["reranker_model"], str)
        self.assertIsInstance(data["reranking_enabled"], bool)
        self.assertIsInstance(data["reranker_top_n"], int)
        self.assertIsInstance(data["initial_retrieval_top_k"], int)

    def test_settings_response_includes_hybrid_search_fields(self):
        """Test GET /api/settings includes hybrid search configuration fields."""
        response = self.client.get("/api/settings")

        self.assertEqual(response.status_code, 200)
        data = response.json()

        # Verify hybrid search fields are present
        self.assertIn("hybrid_search_enabled", data)
        self.assertIn("hybrid_alpha", data)

        # Verify default values
        self.assertIsInstance(data["hybrid_search_enabled"], bool)
        self.assertIsInstance(data["hybrid_alpha"], float)
        self.assertTrue(data["hybrid_search_enabled"])  # Default is True
        self.assertEqual(data["hybrid_alpha"], settings.hybrid_alpha)

    def test_settings_response_all_new_fields_present(self):
        """Test GET /api/settings includes all new character-based and config fields."""
        response = self.client.get("/api/settings")

        self.assertEqual(response.status_code, 200)
        data = response.json()

        # New character-based fields
        self.assertIn("chunk_size_chars", data)
        self.assertIn("chunk_overlap_chars", data)
        self.assertIn("retrieval_top_k", data)
        self.assertIn("vector_metric", data)
        self.assertIn("embedding_batch_size", data)

        # Reranker fields
        self.assertIn("reranker_url", data)
        self.assertIn("reranker_model", data)
        self.assertIn("reranking_enabled", data)
        self.assertIn("reranker_top_n", data)
        self.assertIn("initial_retrieval_top_k", data)

        # Hybrid search fields
        self.assertIn("hybrid_search_enabled", data)
        self.assertIn("hybrid_alpha", data)

        # Instant chat mode fields
        self.assertIn("instant_chat_url", data)
        self.assertIn("instant_chat_model", data)
        self.assertIn("default_chat_mode", data)
        self.assertIn("instant_initial_retrieval_top_k", data)
        self.assertIn("instant_reranker_top_n", data)
        self.assertIn("instant_memory_context_top_k", data)
        self.assertIn("instant_max_tokens", data)

        # Instant-mode latency skip flags (operator tuning surface)
        self.assertIn("instant_skip_query_transformation", data)
        self.assertIn("instant_skip_retrieval_evaluation", data)
        self.assertIn("instant_skip_distillation_synthesis", data)
        self.assertIn("instant_skip_followup_rewrite", data)


class TestSettingsUpdateValidation(unittest.TestCase):
    """Tests for SettingsUpdate validation of new fields."""

    def setUp(self):
        self._orig_users_enabled = settings.users_enabled
        settings.users_enabled = False
        self.client = TestClient(app)
        self.client.headers.update(
            {"Authorization": f"Bearer {settings.admin_secret_token}"}
        )
        # Override get_db to use a pool that allows cross-thread usage
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
        # Restore get_db dependency
        app.dependency_overrides.pop(self._get_db, None)
        settings.users_enabled = self._orig_users_enabled

    @patch.dict(os.environ, {"ALLOW_LOCAL_SERVICES": "1"})
    def test_post_settings_valid_reranker_config(self):
        """Test POST /api/settings with valid reranker configuration."""
        payload = {
            "reranker_url": "http://localhost:8000",
            "reranker_model": "BAAI/bge-reranker-v2-m3",
            "reranking_enabled": True,
            "reranker_top_n": 10,
            "initial_retrieval_top_k": 30
        }

        response = self.client.post("/api/settings", json=payload)

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["reranker_url"], "http://localhost:8000")
        self.assertEqual(data["reranker_model"], "BAAI/bge-reranker-v2-m3")
        self.assertEqual(data["reranking_enabled"], True)
        self.assertEqual(data["reranker_top_n"], 10)
        self.assertEqual(data["initial_retrieval_top_k"], 30)

    def test_post_settings_valid_hybrid_search_config(self):
        """Test POST /api/settings with valid hybrid search configuration."""
        payload = {
            "hybrid_search_enabled": False,
            "hybrid_alpha": 0.3
        }

        response = self.client.post("/api/settings", json=payload)

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["hybrid_search_enabled"], False)
        self.assertEqual(data["hybrid_alpha"], 0.3)

    @patch.dict(os.environ, {"ALLOW_LOCAL_SERVICES": "1"})
    def test_post_settings_valid_instant_chat_config(self):
        """Test POST /api/settings with valid instant model configuration."""
        payload = {
            "instant_chat_url": "http://localhost:1234",
            "instant_chat_model": "nvidia/nemotron-3-nano-4b",
            "default_chat_mode": "instant",
            "instant_initial_retrieval_top_k": 8,
            "instant_reranker_top_n": 3,
            "instant_memory_context_top_k": 2,
            "instant_max_tokens": 2048,
        }

        response = self.client.post("/api/settings", json=payload)

        self.assertEqual(response.status_code, 200)
        data = response.json()
        for key, value in payload.items():
            self.assertEqual(data[key], value)

    def test_post_settings_valid_instant_skip_flags(self):
        """Test POST /api/settings accepts and round-trips the instant skip flags."""
        payload = {
            "instant_skip_query_transformation": False,
            "instant_skip_retrieval_evaluation": False,
            "instant_skip_distillation_synthesis": False,
            "instant_skip_followup_rewrite": True,
        }

        response = self.client.post("/api/settings", json=payload)

        self.assertEqual(response.status_code, 200)
        data = response.json()
        for key, value in payload.items():
            self.assertEqual(data[key], value)

    def test_post_settings_invalid_default_chat_mode(self):
        """Test POST /api/settings rejects unknown default chat mode."""
        response = self.client.post("/api/settings", json={"default_chat_mode": "fast"})

        self.assertEqual(response.status_code, 422)

    def test_post_settings_invalid_instant_budget(self):
        """Test POST /api/settings rejects non-positive instant budgets."""
        response = self.client.post(
            "/api/settings",
            json={"instant_initial_retrieval_top_k": 0},
        )

        self.assertEqual(response.status_code, 422)

    def test_post_settings_invalid_hybrid_alpha_low(self):
        """Test POST /api/settings with hybrid_alpha < 0 returns 422."""
        payload = {"hybrid_alpha": -0.1}

        response = self.client.post("/api/settings", json=payload)

        self.assertEqual(response.status_code, 422)

    def test_post_settings_invalid_hybrid_alpha_high(self):
        """Test POST /api/settings with hybrid_alpha > 1 returns 422."""
        payload = {"hybrid_alpha": 1.5}

        response = self.client.post("/api/settings", json=payload)

        self.assertEqual(response.status_code, 422)

    def test_post_settings_invalid_reranker_top_n_zero(self):
        """Test POST /api/settings with reranker_top_n = 0 returns 422."""
        payload = {"reranker_top_n": 0}

        response = self.client.post("/api/settings", json=payload)

        self.assertEqual(response.status_code, 422)

    def test_post_settings_invalid_reranker_top_n_negative(self):
        """Test POST /api/settings with reranker_top_n < 0 returns 422."""
        payload = {"reranker_top_n": -1}

        response = self.client.post("/api/settings", json=payload)

        self.assertEqual(response.status_code, 422)

    def test_post_settings_invalid_initial_retrieval_top_n_zero(self):
        """Test POST /api/settings with initial_retrieval_top_k = 0 returns 422."""
        payload = {"initial_retrieval_top_k": 0}

        response = self.client.post("/api/settings", json=payload)

        self.assertEqual(response.status_code, 422)

    def test_post_settings_valid_embedding_batch_size_boundary(self):
        """Test POST /api/settings with embedding_batch_size at boundaries."""
        # Test minimum valid value
        payload = {"embedding_batch_size": 1}
        response = self.client.post("/api/settings", json=payload)
        self.assertEqual(response.status_code, 200)

        # Test maximum valid value
        payload = {"embedding_batch_size": 128}
        response = self.client.post("/api/settings", json=payload)
        self.assertEqual(response.status_code, 200)

    def test_post_settings_invalid_embedding_batch_size_below_min(self):
        """Test POST /api/settings with embedding_batch_size < 1 returns 422."""
        payload = {"embedding_batch_size": 0}

        response = self.client.post("/api/settings", json=payload)

        self.assertEqual(response.status_code, 422)

    def test_post_settings_invalid_embedding_batch_size_above_max(self):
        """Test POST /api/settings with embedding_batch_size > 128 returns 422."""
        payload = {"embedding_batch_size": 129}

        response = self.client.post("/api/settings", json=payload)

        self.assertEqual(response.status_code, 422)

    @patch.dict(os.environ, {"ALLOW_LOCAL_SERVICES": "1"})
    def test_post_settings_valid_reranker_url(self):
        """Test POST /api/settings with valid reranker URL."""
        # reranker_url is now SSRF-validated at PUT time (like the other model
        # URLs), so it must resolve to an allowed host. Use localhost to match
        # the sibling reranker tests (passes under ALLOW_LOCAL_SERVICES=1); a
        # non-resolving placeholder host would now be rejected by the guard.
        payload = {"reranker_url": "http://localhost:8000"}

        response = self.client.post("/api/settings", json=payload)

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["reranker_url"], "http://localhost:8000")

    def test_post_settings_empty_reranker_url(self):
        """Test POST /api/settings with empty reranker URL is allowed."""
        payload = {"reranker_url": ""}

        response = self.client.post("/api/settings", json=payload)

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["reranker_url"], "")

    @patch.dict(os.environ, {"ALLOW_LOCAL_SERVICES": "1"})
    def test_post_settings_combined_new_fields(self):
        """Test POST /api/settings with multiple new fields in one request."""
        payload = {
            "reranker_url": "http://localhost:8000",
            "reranking_enabled": True,
            "reranker_top_n": 15,
            "hybrid_search_enabled": False,
            "hybrid_alpha": 0.7,
            "embedding_batch_size": 32
        }

        response = self.client.post("/api/settings", json=payload)

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["reranker_url"], "http://localhost:8000")
        self.assertEqual(data["reranking_enabled"], True)
        self.assertEqual(data["reranker_top_n"], 15)
        self.assertEqual(data["hybrid_search_enabled"], False)
        self.assertEqual(data["hybrid_alpha"], 0.7)
        self.assertEqual(data["embedding_batch_size"], 32)


class TestConnectionEndpoint(unittest.TestCase):
    """Tests for the /api/settings/connection endpoint."""

    def setUp(self):
        self._orig_users_enabled = settings.users_enabled
        settings.users_enabled = False
        self.client = TestClient(app)
        self.client.headers.update(
            {"Authorization": f"Bearer {settings.admin_secret_token}"}
        )
        # Override get_db to use a pool that allows cross-thread usage
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
        # Restore get_db dependency
        app.dependency_overrides.pop(self._get_db, None)
        settings.users_enabled = self._orig_users_enabled

    @patch.dict(os.environ, {"ALLOW_LOCAL_SERVICES": "1"})
    @patch("app.api.routes.settings.httpx.AsyncClient")
    def test_connection_endpoint_with_reranker(self, mock_async_client):
        """Test GET /api/settings/connection tests reranker when configured."""
        # Mock the async client context manager
        mock_client_instance = AsyncMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_client_instance.get = AsyncMock(return_value=mock_response)
        mock_async_client.return_value.__aenter__ = AsyncMock(return_value=mock_client_instance)

        # Set reranker URL in settings
        original_reranker_url = settings.reranker_url
        settings.reranker_url = "http://localhost:8000"

        try:
            response = self.client.get("/api/settings/connection")

            self.assertEqual(response.status_code, 200)
            data = response.json()

            # Verify reranker is included in targets
            self.assertIn("reranker", data)
            self.assertEqual(data["reranker"]["url"], "http://localhost:8000")
            self.assertEqual(data["reranker"]["status"], 200)
            self.assertTrue(data["reranker"]["ok"])

            # Verify other endpoints are also tested
            self.assertIn("embeddings", data)
            self.assertIn("chat", data)
        finally:
            settings.reranker_url = original_reranker_url

    @patch.dict(os.environ, {"ALLOW_LOCAL_SERVICES": "1"})
    @patch("app.api.routes.settings.httpx.AsyncClient")
    def test_connection_endpoint_reranker_failure(self, mock_async_client):
        """Test GET /api/settings/connection handles reranker failure."""
        # Mock the async client context manager
        mock_client_instance = AsyncMock()
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_client_instance.get = AsyncMock(return_value=mock_response)
        mock_async_client.return_value.__aenter__ = AsyncMock(return_value=mock_client_instance)

        # Set reranker URL in settings
        original_reranker_url = settings.reranker_url
        settings.reranker_url = "http://localhost:8000"

        try:
            response = self.client.get("/api/settings/connection")

            self.assertEqual(response.status_code, 200)
            data = response.json()

            # Verify reranker failure is reported
            self.assertIn("reranker", data)
            self.assertFalse(data["reranker"]["ok"])
            self.assertEqual(data["reranker"]["status"], 500)
        finally:
            settings.reranker_url = original_reranker_url

    @patch("app.api.routes.settings.httpx.AsyncClient")
    def test_connection_endpoint_reranker_exception(self, mock_async_client):
        """Test GET /api/settings/connection handles reranker connection exception."""
        # Mock the async client context manager
        mock_client_instance = AsyncMock()
        mock_client_instance.get = AsyncMock(side_effect=Exception("Connection refused"))
        mock_async_client.return_value.__aenter__ = AsyncMock(return_value=mock_client_instance)

        # Set reranker URL in settings
        original_reranker_url = settings.reranker_url
        settings.reranker_url = "http://localhost:8000"

        try:
            response = self.client.get("/api/settings/connection")

            self.assertEqual(response.status_code, 200)
            data = response.json()

            # Verify reranker exception is reported
            self.assertIn("reranker", data)
            self.assertFalse(data["reranker"]["ok"])
            self.assertIsNone(data["reranker"]["status"])
            self.assertIn("error", data["reranker"])
        finally:
            settings.reranker_url = original_reranker_url

    def test_connection_endpoint_local_reranker_mode(self):
        """Test GET /api/settings/connection shows local mode when reranker_url not set."""
        # Ensure reranker_url is not set
        original_reranker_url = settings.reranker_url
        settings.reranker_url = ""

        try:
            response = self.client.get("/api/settings/connection")

            self.assertEqual(response.status_code, 200)
            data = response.json()

            # Verify reranker shows local mode
            self.assertIn("reranker", data)
            self.assertEqual(data["reranker"]["url"], "local (sentence-transformers)")
            self.assertTrue(data["reranker"]["ok"])
            self.assertEqual(data["reranker"]["status"], "local")
            self.assertIn("model", data["reranker"])
        finally:
            settings.reranker_url = original_reranker_url


if __name__ == "__main__":
    unittest.main()
