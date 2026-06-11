"""
Backend API route tests using unittest and FastAPI TestClient.

Tests cover health, settings, memories, documents, and chat endpoints.
All external services (LLM, vector store) are mocked for deterministic tests.
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

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

from _db_pool import SimpleConnectionPool
from fastapi.testclient import TestClient

from app.api.deps import get_llm_health_checker, get_model_checker

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


class TestHealthEndpoint(unittest.TestCase):
    """Tests for the /api/health endpoint."""

    def setUp(self):
        self.client = TestClient(app)

    def test_health_check_success(self):
        """Test health check returns ok status with mocked services."""
        # Mock LLMHealthChecker
        mock_llm_checker = MagicMock()
        mock_llm_checker.check_all = AsyncMock(
            return_value={
                "ok": True,
                "embeddings": {"ok": True, "error": None},
                "chat": {"ok": True, "error": None},
                "error": None,
            }
        )

        # Mock ModelChecker
        mock_model_checker = MagicMock()
        mock_model_checker.check_models = AsyncMock(
            return_value={
                "embedding_model": {"available": True, "error": None},
                "chat_model": {"available": True, "error": None},
            }
        )

        app.dependency_overrides[get_llm_health_checker] = lambda: mock_llm_checker
        app.dependency_overrides[get_model_checker] = lambda: mock_model_checker

        try:
            response = self.client.get("/api/health?deep=true")

            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertEqual(data["status"], "ok")
            self.assertIn("llm", data)
            self.assertIn("models", data)
            self.assertTrue(data["llm"]["ok"])
        finally:
            app.dependency_overrides.pop(get_llm_health_checker, None)
            app.dependency_overrides.pop(get_model_checker, None)

    def test_health_check_llm_failure(self):
        """Test health check handles LLM service failure."""
        mock_llm_checker = MagicMock()
        mock_llm_checker.check_all = AsyncMock(
            return_value={
                "ok": False,
                "embeddings": {"ok": False, "error": "Embedding service error"},
                "chat": {"ok": True, "error": None},
                "error": "Embedding service error",
            }
        )

        mock_model_checker = MagicMock()
        mock_model_checker.check_models = AsyncMock(
            return_value={
                "embedding_model": {"available": True, "error": None},
                "chat_model": {"available": True, "error": None},
            }
        )

        app.dependency_overrides[get_llm_health_checker] = lambda: mock_llm_checker
        app.dependency_overrides[get_model_checker] = lambda: mock_model_checker

        try:
            response = self.client.get("/api/health?deep=true")

            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertEqual(data["status"], "ok")
            self.assertFalse(data["llm"]["ok"])
        finally:
            app.dependency_overrides.pop(get_llm_health_checker, None)
            app.dependency_overrides.pop(get_model_checker, None)


class TestSettingsEndpoints(unittest.TestCase):
    """Tests for the /api/settings GET and POST endpoints."""

    def setUp(self):
        self.client = TestClient(app)
        # Store original values
        self._original_chunk_size = settings.chunk_size
        self._original_rag_threshold = settings.rag_relevance_threshold
        # Override get_db to use a pool that allows cross-thread usage
        from app.api.deps import get_current_active_user, get_db
        from app.models.database import get_pool

        self._test_pool = get_pool(str(TEST_DB_PATH))

        def override_get_db():
            conn = self._test_pool.get_connection()
            try:
                yield conn
            finally:
                self._test_pool.release_connection(conn)

        from app.security import csrf_protect

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_active_user] = lambda: {
            "id": 0, "username": "admin", "role": "superadmin",
            "is_active": 1, "must_change_password": 0,
        }
        app.dependency_overrides[csrf_protect] = lambda: "test-csrf-token"
        self._get_db = get_db
        self._get_current_active_user = get_current_active_user
        self._csrf_protect = csrf_protect

    def tearDown(self):
        # Restore original values
        settings.chunk_size = self._original_chunk_size
        settings.rag_relevance_threshold = self._original_rag_threshold
        # Restore get_db dependency
        app.dependency_overrides.pop(self._get_db, None)
        app.dependency_overrides.pop(self._get_current_active_user, None)
        app.dependency_overrides.pop(self._csrf_protect, None)

    def test_get_settings(self):
        """Test GET /api/settings returns current settings."""
        response = self.client.get("/api/settings")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("chunk_size", data)
        self.assertIn("rag_relevance_threshold", data)
        self.assertIn("embedding_batch_size", data)

    def test_post_settings_valid(self):
        """Test POST /api/settings with valid settings updates values."""
        payload = {
            "chunk_size": 1024,
            "rag_relevance_threshold": 0.5,
            "embedding_batch_size": 64,  # max is 128
        }

        response = self.client.post("/api/settings", json=payload)

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["chunk_size"], 1024)
        self.assertEqual(data["rag_relevance_threshold"], 0.5)
        self.assertEqual(data["embedding_batch_size"], 64)

    def test_post_settings_invalid_chunk_size(self):
        """Test POST /api/settings with invalid chunk_size returns 422."""
        payload = {"chunk_size": 0}

        response = self.client.post("/api/settings", json=payload)

        self.assertEqual(response.status_code, 422)

    def test_post_settings_invalid_rag_threshold_low(self):
        """Test POST /api/settings with rag_relevance_threshold < 0 returns 422."""
        payload = {"rag_relevance_threshold": -0.1}

        response = self.client.post("/api/settings", json=payload)

        self.assertEqual(response.status_code, 422)

    def test_post_settings_invalid_rag_threshold_high(self):
        """Test POST /api/settings with rag_relevance_threshold > 1 returns 422."""
        payload = {"rag_relevance_threshold": 1.5}

        response = self.client.post("/api/settings", json=payload)

        self.assertEqual(response.status_code, 422)

    def test_post_settings_no_valid_fields(self):
        """Test POST /api/settings with no valid fields returns 400."""
        payload = {}

        response = self.client.post("/api/settings", json=payload)

        self.assertEqual(response.status_code, 400)
        data = response.json()
        self.assertIn("No valid fields provided", data["detail"])


class TestMemoriesEndpoints(unittest.TestCase):
    """Tests for the /api/memories CRUD endpoints."""

    def setUp(self):
        self.client = TestClient(app)
        self._temp_dir = tempfile.mkdtemp()

        # Initialize test database
        from app.models.database import init_db

        db_path = str(Path(self._temp_dir) / "test.db")
        init_db(db_path)

        # Create a real MemoryStore pointing to test DB
        from app.models.database import SQLiteConnectionPool
        from app.services.memory_store import MemoryStore

        self.test_pool = SQLiteConnectionPool(db_path, max_size=2)
        test_store = MemoryStore(pool=self.test_pool)

        # Override get_db to use a pool that allows cross-thread usage
        # Create a simple pool for testing
        from app.api.deps import get_db, get_memory_store

        self._connection_pool = SimpleConnectionPool(db_path)

        def override_get_db():
            conn = self._connection_pool.get_connection()
            try:
                yield conn
            finally:
                self._connection_pool.release_connection(conn)

        from app.api.deps import get_current_active_user
        from app.security import csrf_protect

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_memory_store] = lambda: test_store
        app.dependency_overrides[get_current_active_user] = lambda: {
            "id": 0, "username": "admin", "role": "superadmin",
            "is_active": 1, "must_change_password": 0,
        }
        app.dependency_overrides[csrf_protect] = lambda: "test-csrf-token"
        self._test_store = test_store
        self._db_path = db_path
        self._get_db = get_db
        self._get_memory_store = get_memory_store
        self._get_current_active_user = get_current_active_user
        self._csrf_protect = csrf_protect

    def tearDown(self):
        app.dependency_overrides.pop(self._get_db, None)
        app.dependency_overrides.pop(self._get_memory_store, None)
        app.dependency_overrides.pop(self._get_current_active_user, None)
        app.dependency_overrides.pop(self._csrf_protect, None)
        if hasattr(self, "test_pool"):
            self.test_pool.close_all()
        if hasattr(self, "_connection_pool"):
            self._connection_pool.close_all()
        import shutil

        shutil.rmtree(self._temp_dir, ignore_errors=True)

    def test_list_memories_empty(self):
        """Test GET /api/memories returns empty list when no memories."""
        response = self.client.get("/api/memories")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["memories"], [])

    def test_create_memory(self):
        """Test POST /api/memories creates a new memory."""
        payload = {
            "content": "Test memory content",
            "category": "test",
            "tags": '["tag1", "tag2"]',
            "source": "test_source",
        }

        response = self.client.post("/api/memories", json=payload)

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["content"], "Test memory content")
        self.assertEqual(data["metadata"]["category"], "test")
        self.assertEqual(data["id"], "1")  # id is now a string

    def test_create_memory_invalid_empty_content(self):
        """Test POST /api/memories with empty content returns 422."""
        payload = {"content": "", "category": "test"}

        response = self.client.post("/api/memories", json=payload)

        self.assertEqual(response.status_code, 422)

    def test_create_and_list_memory(self):
        """Test creating a memory and then listing it."""
        # Create a memory
        create_payload = {
            "content": "Integration test memory",
            "category": "integration",
            "tags": '["test"]',
        }

        create_response = self.client.post("/api/memories", json=create_payload)
        self.assertEqual(create_response.status_code, 200)
        created = create_response.json()
        self.assertEqual(created["content"], "Integration test memory")

        # List memories
        list_response = self.client.get("/api/memories")
        self.assertEqual(list_response.status_code, 200)
        data = list_response.json()
        self.assertEqual(len(data["memories"]), 1)
        self.assertEqual(data["memories"][0]["content"], "Integration test memory")

    def test_update_memory(self):
        """Test PUT /api/memories/{id} updates a memory."""
        # Create a memory first
        create_payload = {"content": "Original content", "category": "original"}
        create_response = self.client.post("/api/memories", json=create_payload)
        self.assertEqual(create_response.status_code, 200)
        memory_id = create_response.json()["id"]

        # Update the memory
        update_payload = {"content": "Updated content", "category": "updated"}
        update_response = self.client.put(
            f"/api/memories/{memory_id}", json=update_payload
        )

        self.assertEqual(update_response.status_code, 200)
        data = update_response.json()
        self.assertEqual(data["content"], "Updated content")
        self.assertEqual(data["metadata"]["category"], "updated")

    def test_update_memory_not_found(self):
        """Test PUT /api/memories/{id} returns 404 for non-existent memory."""
        update_payload = {"content": "Updated content"}

        response = self.client.put("/api/memories/99999", json=update_payload)

        self.assertEqual(response.status_code, 404)

    def test_delete_memory(self):
        """Test DELETE /api/memories/{id} deletes a memory."""
        # Create a memory first
        create_payload = {"content": "Memory to delete"}
        create_response = self.client.post("/api/memories", json=create_payload)
        self.assertEqual(create_response.status_code, 200)
        memory_id = create_response.json()["id"]

        # Delete the memory
        delete_response = self.client.delete(f"/api/memories/{memory_id}")

        self.assertEqual(delete_response.status_code, 200)
        data = delete_response.json()
        self.assertIn("deleted successfully", data["message"])

        # Verify it's gone
        list_response = self.client.get("/api/memories")
        self.assertEqual(len(list_response.json()["memories"]), 0)

    def test_delete_memory_not_found(self):
        """Test DELETE /api/memories/{id} returns 404 for non-existent memory."""
        response = self.client.delete("/api/memories/99999")

        self.assertEqual(response.status_code, 404)


class TestDocumentsEndpoints(unittest.TestCase):
    """Tests for the /api/documents endpoints."""

    def setUp(self):
        self.client = TestClient(app)
        self._temp_dir = tempfile.mkdtemp()
        db_path = str(Path(self._temp_dir) / "test.db")

        from app.models.database import init_db

        init_db(db_path)

        # Override get_db to use a pool that allows cross-thread usage
        # Create a simple pool for testing
        from app.api.deps import get_current_active_user, get_db, get_evaluate_policy

        self._connection_pool = SimpleConnectionPool(db_path)

        def override_get_db():
            conn = self._connection_pool.get_connection()
            try:
                yield conn
            finally:
                self._connection_pool.release_connection(conn)

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_active_user] = lambda: {
            "id": 1,
            "username": "test-admin",
            "role": "admin",
        }

        async def allow_policy(user, resource_type, resource_id, action):
            return True

        app.dependency_overrides[get_evaluate_policy] = lambda: allow_policy
        self._db_path = db_path
        self._get_db = get_db
        self._get_current_active_user = get_current_active_user
        self._get_evaluate_policy = get_evaluate_policy

    def tearDown(self):
        app.dependency_overrides.pop(self._get_db, None)
        app.dependency_overrides.pop(self._get_current_active_user, None)
        app.dependency_overrides.pop(self._get_evaluate_policy, None)
        if hasattr(self, "_connection_pool"):
            self._connection_pool.close_all()
        import shutil

        shutil.rmtree(self._temp_dir, ignore_errors=True)

    def test_get_document_stats_empty(self):
        """Test GET /api/documents/stats returns success with zero counts."""
        response = self.client.get("/api/documents/stats")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["total_files"], 0)
        self.assertEqual(data["total_chunks"], 0)
        self.assertEqual(data["status"], "success")

    def test_list_documents_empty(self):
        """Test GET /api/documents returns empty list when no documents."""
        response = self.client.get("/api/documents/")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["documents"], [])

    def test_list_documents_returns_phase_progress_and_error_fields(self):
        """GET /api/documents includes list-row phase, progress, and error detail."""
        conn = self._connection_pool.get_connection()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO vaults (id, name, description) VALUES (1, 'Vault 1', '')"
            )
            conn.execute(
                "INSERT OR IGNORE INTO vaults (id, name, description) VALUES (2, 'Vault 2', '')"
            )
            cur = conn.execute(
                """INSERT INTO files
                   (vault_id, file_path, file_name, file_size, status,
                    error_message, phase, phase_message, progress_percent,
                    processed_units, total_units, unit_label,
                    phase_started_at, processing_started_at)
                   VALUES (1, '/uploads/failed.txt', 'failed.txt', 100,
                           'error', 'Parser could not read the file',
                           'parsing', 'Parsing failed', 25.0,
                           1, 4, 'pages', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
            )
            file_id = cur.lastrowid
            leaked_cur = conn.execute(
                """INSERT INTO files
                   (vault_id, file_path, file_name, file_size, status,
                    error_message, phase, phase_message, progress_percent)
                   VALUES (2, '/uploads/leaked.txt', 'leaked.txt', 100,
                           'processing', 'should not leak',
                           'embedding', 'Embedding hidden vault', 50.0)""",
            )
            inaccessible_file_id = leaked_cur.lastrowid
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)

        app.dependency_overrides[self._get_current_active_user] = lambda: {
            "id": 42,
            "username": "vault-one-reader",
            "role": "member",
        }

        async def allow_only_vault_one(user, resource_type, resource_id, action):
            return resource_type == "vault" and resource_id == 1 and action == "read"

        app.dependency_overrides[self._get_evaluate_policy] = lambda: allow_only_vault_one

        response = self.client.get("/api/documents/?vault_id=1")

        self.assertEqual(response.status_code, 200, response.text)
        docs = response.json()["documents"]
        self.assertNotIn(inaccessible_file_id, {item["id"] for item in docs})
        doc = next(item for item in docs if item["id"] == file_id)
        self.assertEqual(doc["status"], "error")
        self.assertEqual(doc["error_message"], "Parser could not read the file")
        self.assertEqual(doc["phase"], "parsing")
        self.assertEqual(doc["phase_message"], "Parsing failed")
        self.assertEqual(doc["progress_percent"], 25.0)
        self.assertEqual(doc["processed_units"], 1)
        self.assertEqual(doc["total_units"], 4)
        self.assertEqual(doc["unit_label"], "pages")
        self.assertEqual(doc["metadata"]["error_message"], "Parser could not read the file")
        self.assertEqual(doc["metadata"]["phase_message"], "Parsing failed")
        self.assertEqual(doc["metadata"]["progress_percent"], 25.0)


class TestChatEndpoint(unittest.TestCase):
    """Tests for the /api/chat endpoint with mocked RAGEngine."""

    def setUp(self):
        self.client = TestClient(app)
        from app.api.deps import get_current_active_user, get_evaluate_policy
        from app.security import csrf_protect

        app.dependency_overrides[get_current_active_user] = lambda: {
            "id": 0, "username": "admin", "role": "superadmin",
            "is_active": 1, "must_change_password": 0,
        }
        app.dependency_overrides[csrf_protect] = lambda: "test-csrf-token"

        async def allow_policy(user, resource_type, resource_id, action):
            return True

        app.dependency_overrides[get_evaluate_policy] = lambda: allow_policy
        self._get_current_active_user = get_current_active_user
        self._csrf_protect = csrf_protect
        self._get_evaluate_policy = get_evaluate_policy

    def tearDown(self):
        from app.api.deps import get_rag_engine

        app.dependency_overrides.pop(get_rag_engine, None)
        app.dependency_overrides.pop(self._get_current_active_user, None)
        app.dependency_overrides.pop(self._csrf_protect, None)
        app.dependency_overrides.pop(self._get_evaluate_policy, None)

    def _set_mock_rag_engine(self, mock_query_fn):
        """Helper to override get_rag_engine with a mock that uses the given query function."""
        from app.api.deps import get_rag_engine

        mock_engine = MagicMock()
        mock_engine.query = mock_query_fn
        app.dependency_overrides[get_rag_engine] = lambda: mock_engine

    def test_chat_non_streaming(self):
        """Test POST /api/chat non-streaming returns content and sources."""

        async def mock_query(user_input, chat_history, stream=False, **kwargs):
            """Mock async generator for RAG query."""
            yield {"type": "content", "content": "This is a test response."}
            yield {
                "type": "done",
                "sources": [
                    {
                        "file_id": "1",
                        "score": 0.95,
                        "metadata": {"source_file": "test.txt"},
                    },
                    {
                        "file_id": "2",
                        "score": 0.85,
                        "metadata": {"source_file": "test2.txt"},
                    },
                ],
                "memories_used": [
                    {
                        "id": "1",
                        "memory_label": "M1",
                        "content": "Memory 1",
                    }
                ],
            }

        self._set_mock_rag_engine(mock_query)

        payload = {"message": "What is the test?", "history": [], "stream": False}

        response = self.client.post("/api/chat", json=payload)

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["content"], "This is a test response.")
        self.assertEqual(len(data["sources"]), 2)
        self.assertEqual(data["sources"][0]["file_id"], "1")
        self.assertEqual(len(data["memories_used"]), 1)
        # score_type default must survive when the engine omits it, so the
        # frontend never falls back to an undefined polarity.
        self.assertEqual(data.get("score_type"), "distance")

    def test_chat_non_streaming_propagates_score_type(self):
        """POST /api/chat must include score_type from the engine's done event."""

        async def mock_query(user_input, chat_history, stream=False, **kwargs):
            yield {"type": "content", "content": "ok"}
            yield {
                "type": "done",
                "sources": [{"file_id": "1", "score": 0.42}],
                "memories_used": [],
                "score_type": "rerank",
            }

        self._set_mock_rag_engine(mock_query)

        response = self.client.post(
            "/api/chat",
            json={"message": "q", "history": [], "stream": False},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json().get("score_type"), "rerank")

    def test_chat_non_streaming_with_history(self):
        """Test POST /api/chat with chat history."""

        async def mock_query(user_input, chat_history, stream=False, **kwargs):
            """Mock async generator that verifies history is passed."""
            # Verify history is passed
            self.assertEqual(len(chat_history), 1)
            self.assertEqual(chat_history[0]["role"], "user")
            self.assertEqual(chat_history[0]["content"], "Previous message")

            yield {"type": "content", "content": "Response with history."}
            yield {"type": "done", "sources": [], "memories_used": []}

        self._set_mock_rag_engine(mock_query)

        payload = {
            "message": "Follow-up question",
            "history": [{"role": "user", "content": "Previous message"}],
            "stream": False,
        }

        response = self.client.post("/api/chat", json=payload)

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["content"], "Response with history.")

    def test_chat_empty_sources(self):
        """Test POST /api/chat handles empty sources gracefully."""

        async def mock_query(user_input, chat_history, stream=False, **kwargs):
            yield {"type": "content", "content": "No relevant sources found."}
            yield {"type": "done", "sources": [], "memories_used": []}

        self._set_mock_rag_engine(mock_query)

        payload = {"message": "Unknown query", "history": [], "stream": False}

        response = self.client.post("/api/chat", json=payload)

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["content"], "No relevant sources found.")
        self.assertEqual(data["sources"], [])
        self.assertEqual(data["memories_used"], [])


class TestBasicHealthEndpoint(unittest.TestCase):
    """Tests for the basic /health endpoint."""

    def setUp(self):
        self.client = TestClient(app)

    def test_basic_health_check(self):
        """Test GET /health returns ok status."""
        response = self.client.get("/health")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "ok")


if __name__ == "__main__":
    unittest.main()
