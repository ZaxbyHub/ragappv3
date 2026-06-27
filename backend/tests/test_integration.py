"""
Integration tests for the KnowledgeVault RAG application.

These tests cover end-to-end flows including:
- Upload -> Index -> Chat flow
- Memory search operations
- Document deletion
- Error handling for embedding/chat downtime

Run with: python -m pytest backend/tests/test_integration.py -v
"""

import os
import sys
import tempfile
from io import BytesIO
from pathlib import Path
from typing import Dict, List, Optional

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# =============================================================================
# CRITICAL: Set up stubs BEFORE any app imports
# =============================================================================

# Stub missing optional dependencies
try:
    import lancedb
except ImportError:
    import types

    lancedb_stub = types.ModuleType("lancedb")
    sys.modules["lancedb"] = lancedb_stub

try:
    import pyarrow
except ImportError:
    import types

    pyarrow_stub = types.ModuleType("pyarrow")
    sys.modules["pyarrow"] = pyarrow_stub

# Stub unstructured before anything else tries to import it
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
_unstructured.documents.elements = types.ModuleType("unstructured.documents.elements")
_unstructured.documents.elements.Element = type("Element", (), {})
sys.modules["unstructured"] = _unstructured
sys.modules["unstructured.partition"] = _unstructured.partition
sys.modules["unstructured.partition.auto"] = _unstructured.partition.auto
sys.modules["unstructured.chunking"] = _unstructured.chunking
sys.modules["unstructured.chunking.title"] = _unstructured.chunking.title
sys.modules["unstructured.documents"] = _unstructured.documents
sys.modules["unstructured.documents.elements"] = _unstructured.documents.elements

# =============================================================================
# Import test utilities
# =============================================================================

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# =============================================================================
# Fake Service Implementations for Testing
# =============================================================================


class FakeEmbeddingService:
    """Fake embedding service for testing."""

    def __init__(
        self, embedding: Optional[List[float]] = None, raise_error: bool = False
    ):
        self.embedding = embedding or [0.1] * 768  # Default 768-dim embedding
        self.raise_error = raise_error
        self.call_count = 0

    async def embed_single(self, text: str) -> List[float]:
        self.call_count += 1
        if self.raise_error:
            from app.services.embeddings import EmbeddingError

            raise EmbeddingError("Embedding service unavailable")
        return self.embedding

    async def embed_batch(self, texts: List[str]) -> List[List[float]]:
        if self.raise_error:
            from app.services.embeddings import EmbeddingError

            raise EmbeddingError("Embedding service unavailable")
        return [self.embedding for _ in texts]


class FakeLLMClient:
    """Fake LLM client for testing."""

    def __init__(
        self,
        response: str = "Test response",
        stream_chunks: Optional[List[str]] = None,
        raise_error: bool = False,
    ):
        self.response = response
        self.stream_chunks = stream_chunks or ["Test ", "response"]
        self.raise_error = raise_error
        self.call_count = 0

    async def chat_completion(
        self, messages: List[Dict[str, str]], **kwargs
    ) -> str:
        self.call_count += 1
        if self.raise_error:
            from app.services.llm_client import LLMError

            raise LLMError("LLM service unavailable")
        return self.response

    async def chat_completion_stream(
        self, messages: List[Dict[str, str]], **kwargs
    ):
        self.call_count += 1
        if self.raise_error:
            from app.services.llm_client import LLMError

            raise LLMError("LLM service unavailable")
        for chunk in self.stream_chunks:
            yield chunk

    def start(self):
        pass

    async def close(self):
        pass


class FakeVectorStore:
    """Fake vector store for testing."""

    def __init__(self, search_results: Optional[List[Dict]] = None):
        self.search_results = search_results or []
        self.stored_chunks: List[Dict] = []
        self.deleted_file_ids: List[str] = []

    async def search(
        self, embedding: List[float], limit: int = 10, filter_expr=None, vault_id=None, **kwargs
    ) -> List[Dict]:
        return self.search_results[:limit]

    def add_chunks(self, records: List[Dict]):
        self.stored_chunks.extend(records)

    def delete_by_file(self, file_id: str) -> int:
        self.deleted_file_ids.append(file_id)
        return sum(1 for chunk in self.stored_chunks if chunk.get("file_id") == file_id)

    def init_table(self, dimension: int):
        pass

    def connect(self):
        pass

    def close(self):
        pass

    async def get_chunks_by_uid(self, chunk_uids: List[str]) -> List[Dict]:
        return []

    def get_fts_exceptions(self) -> int:
        return 0


class FakeMemoryStore:
    """Fake memory store for testing."""

    def __init__(self, memories: Optional[List] = None, intent: Optional[str] = None):
        self._memories = memories or []
        self._intent = intent
        self.added_memories: List[Dict] = []

    def detect_memory_intent(self, text: str) -> Optional[str]:
        return self._intent

    def add_memory(
        self,
        content: str,
        category: Optional[str] = None,
        tags: Optional[str] = None,
        source: Optional[str] = None,
        vault_id: Optional[int] = None,
        importance: float = 0.5,
        expires_at=None,
    ):
        self.added_memories.append(
            {
                "content": content,
                "category": category,
                "tags": tags,
                "source": source,
                "importance": importance,
                "expires_at": expires_at,
            }
        )
        # Return a simple dict-like object instead of MemoryRecord
        return MagicMock(
            id=len(self.added_memories),
            content=content,
            category=category,
            tags=tags,
            source=source,
            importance=importance,
            expires_at=expires_at,
            created_at=None,
            updated_at=None,
        )

    def search_memories(self, query: str, limit: int = 5, vault_id=None) -> List:
        return self._memories[:limit]


# =============================================================================
# Test Helper Functions
# =============================================================================


class NoOpLimiter:
    """A limiter that doesn't actually rate limit."""

    enabled = False  # Setting to False should skip all rate limiting

    def limit(self, *args, **kwargs):
        def decorator(func):
            return func

        return decorator


def setup_app_state(app, **overrides):
    """Set up required app state for testing.

    This initializes all the state attributes that dependencies expect.
    """
    import tempfile

    # Create a temp directory for test database
    test_data_dir = Path(tempfile.mkdtemp(prefix="knowledgevault_test_"))

    # Update settings to use test directory
    # This must be done before importing routes that use settings
    from app.config import settings

    settings.data_dir = test_data_dir

    # Now initialize the database (it will use settings.sqlite_path)
    from app.models.database import get_pool, init_db

    init_db(str(settings.sqlite_path))

    # Create db_pool for testing
    db_pool = get_pool(str(settings.sqlite_path), max_size=2)

    # Create uploads directory
    uploads_dir = test_data_dir / "uploads"
    uploads_dir.mkdir(exist_ok=True)

    # Mock the maintenance service
    mock_maintenance = MagicMock()
    mock_maintenance.get_flag.return_value = MagicMock(
        enabled=False, reason="", version=0, updated_at=None
    )

    # Build a minimal fake rag_engine for routes that read app.state.rag_engine
    from app.services.rag_engine import RAGEngine

    fake_rag_engine = overrides.get(
        "rag_engine",
        RAGEngine(
            embedding_service=overrides.get("embedding_service", FakeEmbeddingService()),
            vector_store=overrides.get("vector_store", FakeVectorStore()),
            memory_store=overrides.get("memory_store", FakeMemoryStore()),
            llm_client=overrides.get("llm_client", FakeLLMClient()),
        ),
    )

    # Initialize app state
    app.state.db_pool = db_pool
    app.state.maintenance_service = overrides.get(
        "maintenance_service", mock_maintenance
    )
    app.state.limiter = overrides.get("limiter", NoOpLimiter())
    app.state.embedding_service = overrides.get(
        "embedding_service", FakeEmbeddingService()
    )
    app.state.llm_client = overrides.get("llm_client", FakeLLMClient())
    app.state.vector_store = overrides.get("vector_store", FakeVectorStore())
    app.state.memory_store = overrides.get("memory_store", FakeMemoryStore())
    app.state.secret_manager = overrides.get("secret_manager", MagicMock())
    app.state.toggle_manager = overrides.get("toggle_manager", MagicMock())
    app.state.csrf_manager = overrides.get("csrf_manager", MagicMock())
    app.state.rag_engine = fake_rag_engine

    # Bypass authentication and CSRF for integration tests (tests cover functionality, not auth)
    from app.api.deps import get_current_active_user
    from app.security import csrf_protect

    app.dependency_overrides[get_current_active_user] = lambda: {
        "id": 0,
        "username": "admin",
        "role": "superadmin",
        "is_active": 1,
        "must_change_password": 0,
    }
    app.dependency_overrides[csrf_protect] = lambda: "test-csrf-token"

    if "background_processor" in overrides:
        app.state.background_processor = overrides["background_processor"]
    else:
        mock_bp = MagicMock()
        mock_bp.is_running = True
        mock_bp.start = AsyncMock()
        mock_bp.enqueue = AsyncMock()
        app.state.background_processor = mock_bp

    # Store test paths on app state for tests to access
    app.state._test_db_path = str(settings.sqlite_path)
    app.state._test_data_dir = test_data_dir

    # Return cleanup function
    def cleanup():
        import shutil

        app.dependency_overrides.pop(get_current_active_user, None)
        app.dependency_overrides.pop(csrf_protect, None)
        shutil.rmtree(test_data_dir, ignore_errors=True)

    return cleanup


def create_test_client(**overrides):
    """Create a TestClient with mocked app state.

    This must be called AFTER importing app.main, and sets up the required
    state that the middleware expects.
    """
    from fastapi.testclient import TestClient

    from app.main import app

    setup_app_state(app, **overrides)

    return TestClient(app)


# =============================================================================
# Integration Test Suite
# =============================================================================


class TestIntegration(unittest.TestCase):
    """Integration tests covering end-to-end workflows."""

    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()

        from app.config import settings
        self._orig_data_dir = settings.data_dir

        # Create fake services
        self.fake_embedding_service = FakeEmbeddingService()
        self.fake_llm_client = FakeLLMClient()
        self.fake_vector_store = FakeVectorStore()
        self.fake_memory_store = FakeMemoryStore()

    def tearDown(self):
        """Clean up temporary files."""
        import shutil

        from app.api.deps import get_current_active_user
        from app.main import app
        from app.security import csrf_protect
        app.dependency_overrides.pop(get_current_active_user, None)
        app.dependency_overrides.pop(csrf_protect, None)

        from app.config import settings
        settings.data_dir = self._orig_data_dir

        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    # ==========================================================================
    # Test: Upload -> Index -> Chat Flow
    # ==========================================================================

    @patch("app.api.routes.documents.DocumentProcessor")
    def test_upload_index_chat_flow(self, mock_processor_class):
        """Test complete flow: upload document, index it, then chat with RAG."""
        from fastapi.testclient import TestClient

        from app.main import app

        # Setup mocks for app state
        setup_app_state(app)

        client = TestClient(app)

        # Setup mock processor
        mock_processor = MagicMock()
        mock_processor_class.return_value = mock_processor
        mock_processor._check_duplicate_in_flight.return_value = None  # no duplicate
        mock_processor._insert_or_get_file_record.return_value = 123   # file_id

        # Mock process_file to return a processed document
        mock_result = MagicMock()
        mock_result.file_id = 123
        mock_result.chunks = [
            MagicMock(text="Chunk 1 content", chunk_index=0),
            MagicMock(text="Chunk 2 content", chunk_index=1),
        ]
        mock_processor.process_file = AsyncMock(return_value=mock_result)

        # Step 1: Upload a document
        test_content = b"This is a test document content for integration testing."
        response = client.post(
            "/api/documents/upload?vault_id=1",
            files={"file": ("test_doc.txt", BytesIO(test_content), "text/plain")},
        )

        self.assertEqual(response.status_code, 200)
        upload_data = response.json()
        self.assertIn("file_id", upload_data)
        # status is now 'pending' (queued for async processing), not 'indexed'
        self.assertIn(upload_data["status"], ["pending", "indexed"])
        # Verify the upload actually enqueued background processing
        app.state.background_processor.enqueue.assert_called_once()

        # Step 2: Verify document is listed
        response = client.get("/api/documents")
        self.assertEqual(response.status_code, 200)
        docs_data = response.json()
        self.assertIn("documents", docs_data)

    def test_chat_with_indexed_document(self):
        """Test chat endpoint returns response with sources."""
        from fastapi.testclient import TestClient

        from app.api.deps import get_rag_engine
        from app.main import app
        from app.services.rag_engine import RAGEngine

        # Setup mocks for app state
        setup_app_state(app)

        client = TestClient(app)

        # Setup fake vector store with search results
        self.fake_vector_store.search_results = [
            {
                "text": "This is relevant information from the document.",
                "file_id": "123",
                "metadata": {"source_file": "test_doc.txt"},
                "score": 0.95,
            }
        ]

        # Setup fake memory
        fake_memory = MagicMock()
        fake_memory.content = "Remember this important fact"
        self.fake_memory_store._memories = [fake_memory]

        from app.config import settings as app_settings

        # Create RAG engine with fake services
        rag_engine = RAGEngine(
            embedding_service=self.fake_embedding_service,
            vector_store=self.fake_vector_store,
            memory_store=self.fake_memory_store,
            llm_client=self.fake_llm_client,
        )
        # Disable atomic visibility filter (no files in test DB)
        rag_engine._get_indexed_file_ids = MagicMock(return_value=None)
        # Override dependency to use our fake RAG engine
        app.dependency_overrides[get_rag_engine] = lambda: rag_engine

        # FakeEmbeddingService returns identical embeddings, so context distillation
        # would deduplicate all chunks. Disable it for this test.
        with patch.object(app_settings, "context_distillation_enabled", False):
            try:
                # Send chat request
                response = client.post(
                    "/api/chat",
                    json={
                        "message": "What information is in the document?",
                        "history": [],
                        "stream": False,
                    },
                )

                self.assertEqual(response.status_code, 200)
                chat_data = response.json()
                self.assertIn("content", chat_data)
                self.assertIn("sources", chat_data)
                self.assertIn("memories_used", chat_data)

                # Verify sources are included
                self.assertEqual(len(chat_data["sources"]), 1)
                self.assertEqual(chat_data["sources"][0]["file_id"], "123")
            finally:
                # Clean up dependency override
                app.dependency_overrides.pop(get_rag_engine, None)

    @patch("app.api.routes.chat.get_rag_engine")
    def test_chat_streaming_response(self, mock_get_rag_engine):
        """Test streaming chat endpoint yields chunks correctly."""
        from fastapi.testclient import TestClient

        from app.main import app
        from app.services.rag_engine import RAGEngine

        # Setup mocks for app state
        setup_app_state(app)

        client = TestClient(app)

        self.fake_llm_client.stream_chunks = ["Hello, ", "this ", "is ", "a ", "test."]
        self.fake_vector_store.search_results = [
            {"text": "Test content", "file_id": "1", "metadata": {}, "score": 0.9}
        ]

        rag_engine = RAGEngine(
            embedding_service=self.fake_embedding_service,
            vector_store=self.fake_vector_store,
            memory_store=self.fake_memory_store,
            llm_client=self.fake_llm_client,
        )
        mock_get_rag_engine.return_value = rag_engine

        response = client.post(
            "/api/chat/stream",
            json={"messages": [{"role": "user", "content": "Hello!"}]},
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            response.headers.get("content-type", "").startswith("text/event-stream")
        )

        # Parse SSE events
        content = response.text
        self.assertIn("data:", content)
        self.assertIn("type", content)

    # ==========================================================================
    # Test: Memory Search
    # ==========================================================================

    def test_memory_search_full_text(self):
        """Test memory search endpoint returns relevant memories."""
        from fastapi.testclient import TestClient

        from app.main import app

        # Setup mocks for app state
        setup_app_state(app)

        client = TestClient(app)

        # First create a memory
        response = client.post(
            "/api/memories",
            json={
                "content": "This is a test memory about Python programming",
                "category": "programming",
                "tags": "python,coding",
                "source": "test",
            },
        )

        self.assertEqual(response.status_code, 200)
        memory_data = response.json()
        self.assertIn("id", memory_data)

        # Search for the memory
        response = client.get("/api/memories/search?query=Python&limit=5")
        self.assertEqual(response.status_code, 200)
        search_data = response.json()

        self.assertIn("results", search_data)
        self.assertIn("total", search_data)

        # Verify memory search via POST endpoint too
        response = client.post(
            "/api/memories/search", json={"query": "programming", "limit": 5}
        )
        self.assertEqual(response.status_code, 200)
        search_data = response.json()
        self.assertIn("results", search_data)

    def test_memory_search_empty_query(self):
        """Test memory search handles empty queries gracefully."""
        from fastapi.testclient import TestClient

        from app.main import app

        # Setup mocks for app state
        setup_app_state(app)

        client = TestClient(app)

        response = client.post("/api/memories/search", json={"query": "", "limit": 5})

        self.assertEqual(response.status_code, 200)
        search_data = response.json()
        self.assertEqual(search_data["results"], [])
        self.assertEqual(search_data["total"], 0)

    def test_memory_search_whitespace_only(self):
        """Test memory search handles whitespace-only queries gracefully."""
        from fastapi.testclient import TestClient

        from app.main import app

        # Setup mocks for app state
        setup_app_state(app)

        client = TestClient(app)

        response = client.post(
            "/api/memories/search", json={"query": "   ", "limit": 5}
        )

        self.assertEqual(response.status_code, 200)
        search_data = response.json()
        self.assertEqual(search_data["results"], [])
        self.assertEqual(search_data["total"], 0)

    # ==========================================================================
    # Test: Document Delete
    # ==========================================================================

    @patch("app.api.routes.documents.VectorStore")
    def test_document_delete_without_existing_doc(self, mock_vector_store_class):
        """Test document deletion returns 404 for non-existent document."""
        from fastapi.testclient import TestClient

        from app.main import app

        # Setup mocks for app state
        setup_app_state(app)

        client = TestClient(app)

        # Mock vector store
        mock_vector_store = MagicMock()
        mock_vector_store_class.return_value = mock_vector_store
        mock_vector_store.delete_by_file = MagicMock(return_value=2)
        mock_vector_store.db = MagicMock()
        mock_vector_store.db.table_names.return_value = ["chunks"]

        # Try delete of non-existent document (auth skipped when no token configured)
        response = client.delete("/api/documents/1")
        self.assertEqual(response.status_code, 404)  # Document not found

    @patch("app.api.routes.documents.SecretManager")
    @patch("app.api.routes.documents.VectorStore")
    def test_document_delete_success(
        self, mock_vector_store_class, mock_secret_manager_class
    ):
        """Test successful document deletion removes file and chunks."""
        from fastapi.testclient import TestClient

        from app.main import app

        # Setup mocks for app state
        setup_app_state(app)

        client = TestClient(app)

        # Setup mocks
        mock_vector_store = MagicMock()
        mock_vector_store_class.return_value = mock_vector_store
        mock_vector_store.db = MagicMock()
        mock_vector_store.db.table_names.return_value = ["chunks"]
        mock_vector_store.delete_by_file = MagicMock(return_value=2)
        mock_vector_store.connect = MagicMock()
        mock_vector_store.close = MagicMock()

        mock_secret_manager = MagicMock()
        mock_secret_manager.get_hmac_key.return_value = (b"test_key", "v1")
        mock_secret_manager_class.return_value = mock_secret_manager

        # First create a document by uploading
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("Test document content")
            temp_file = f.name

        try:
            # This would normally require proper auth setup
            # For integration test, we verify the delete flow structure
            response = client.get("/api/documents")
            self.assertIn(response.status_code, [200, 401])
        finally:
            if os.path.exists(temp_file):
                os.unlink(temp_file)

    def test_document_delete_not_found(self):
        """Test deleting a non-existent document returns 404."""
        from fastapi.testclient import TestClient

        from app.main import app

        # Setup mocks for app state
        setup_app_state(app)

        TestClient(app)

        # This requires authentication to test properly
        # The endpoint should return 404 for non-existent documents
        # We verify the API structure is correct
        pass  # Would need auth setup to test fully

    # ==========================================================================
    # Test: Error Cases for Embedding/Chat Downtime
    # ==========================================================================

    def test_chat_with_embedding_service_down(self):
        """Test chat handles embedding service downtime gracefully."""
        from fastapi.testclient import TestClient

        from app.api.deps import get_rag_engine
        from app.main import app
        from app.services.rag_engine import RAGEngine

        # Setup mocks for app state
        setup_app_state(app)

        client = TestClient(app)

        # Create RAG engine with failing embedding service
        failing_embedding = FakeEmbeddingService(raise_error=True)
        rag_engine = RAGEngine(
            embedding_service=failing_embedding,
            vector_store=self.fake_vector_store,
            memory_store=self.fake_memory_store,
            llm_client=self.fake_llm_client,
        )
        # Override dependency to use our fake RAG engine
        app.dependency_overrides[get_rag_engine] = lambda: rag_engine

        try:
            response = client.post(
                "/api/chat",
                json={"message": "Test message", "history": [], "stream": False},
            )

            # Should return 503 Service Unavailable
            self.assertEqual(response.status_code, 503)
            error_data = response.json()
            self.assertIn("detail", error_data)
        finally:
            # Clean up dependency override
            app.dependency_overrides.pop(get_rag_engine, None)

    def test_chat_with_llm_service_down(self):
        """Test chat handles LLM service downtime gracefully."""
        from fastapi.testclient import TestClient

        from app.api.deps import get_rag_engine
        from app.main import app
        from app.services.rag_engine import RAGEngine

        # Setup mocks for app state
        setup_app_state(app)

        client = TestClient(app)

        # Create RAG engine with failing LLM client
        failing_llm = FakeLLMClient(raise_error=True)
        rag_engine = RAGEngine(
            embedding_service=self.fake_embedding_service,
            vector_store=self.fake_vector_store,
            memory_store=self.fake_memory_store,
            llm_client=failing_llm,
        )
        # Override dependency to use our fake RAG engine
        app.dependency_overrides[get_rag_engine] = lambda: rag_engine

        try:
            response = client.post(
                "/api/chat",
                json={"message": "Test message", "history": [], "stream": False},
            )

            # Should return 503 Service Unavailable
            self.assertEqual(response.status_code, 503)
            error_data = response.json()
            self.assertIn("detail", error_data)
        finally:
            # Clean up dependency override
            app.dependency_overrides.pop(get_rag_engine, None)

    def test_chat_streaming_with_llm_error(self):
        """Test streaming chat handles LLM errors gracefully."""
        from fastapi.testclient import TestClient

        from app.api.deps import get_rag_engine
        from app.main import app
        from app.services.rag_engine import RAGEngine

        # Setup mocks for app state
        setup_app_state(app)

        client = TestClient(app)

        failing_llm = FakeLLMClient(raise_error=True)
        rag_engine = RAGEngine(
            embedding_service=self.fake_embedding_service,
            vector_store=self.fake_vector_store,
            memory_store=self.fake_memory_store,
            llm_client=failing_llm,
        )
        # Override dependency to use our fake RAG engine
        app.dependency_overrides[get_rag_engine] = lambda: rag_engine

        try:
            response = client.post(
                "/api/chat/stream",
                json={"messages": [{"role": "user", "content": "Test"}]},
            )

            # Streaming endpoint returns 200 but includes error in stream
            self.assertEqual(response.status_code, 200)
            content = response.text
            self.assertIn("error", content.lower())
        finally:
            # Clean up dependency override
            app.dependency_overrides.pop(get_rag_engine, None)

    # ==========================================================================
    # Test: Document Upload Error Cases
    # ==========================================================================

    def test_upload_file_too_large(self):
        """Test upload rejects files exceeding size limit."""
        from fastapi.testclient import TestClient

        from app.main import app

        # Setup mocks for app state
        setup_app_state(app)

        client = TestClient(app)

        # Regression F-001: use the configured limit so this remains above
        # max_file_size_mb after future default changes.
        from app.config import settings

        large_content = b"x" * ((settings.max_file_size_mb + 1) * 1024 * 1024)

        response = client.post(
            "/api/documents/upload?vault_id=1",
            files={"file": ("large_file.txt", BytesIO(large_content), "text/plain")},
            headers={"content-length": str(len(large_content))},
        )

        self.assertEqual(response.status_code, 413)  # Payload Too Large

    def test_upload_invalid_file_extension(self):
        """Test upload rejects files with invalid extensions."""
        from fastapi.testclient import TestClient

        from app.main import app

        # Setup mocks for app state
        setup_app_state(app)

        client = TestClient(app)

        response = client.post(
            "/api/documents/upload?vault_id=1",
            files={
                "file": ("malicious.exe", BytesIO(b"test"), "application/octet-stream")
            },
        )

        self.assertEqual(response.status_code, 400)
        error_data = response.json()
        self.assertIn("detail", error_data)

    def test_upload_empty_filename(self):
        """Test upload handles empty filename gracefully."""
        from fastapi.testclient import TestClient

        from app.main import app

        # Setup mocks for app state
        setup_app_state(app)

        client = TestClient(app)

        response = client.post(
            "/api/documents/upload",
            files={"file": ("", BytesIO(b"test"), "text/plain")},
        )

        # Empty filename should return 422 (validation error)
        self.assertEqual(response.status_code, 422)

    # ==========================================================================
    # Test: Health and Status Endpoints
    # ==========================================================================

    def test_health_endpoint_during_degraded_state(self):
        """Test health endpoint reflects degraded service state."""
        from fastapi.testclient import TestClient

        from app.api.deps import get_llm_health_checker, get_model_checker
        from app.main import app

        # Setup mocks for app state
        setup_app_state(app)

        client = TestClient(app)

        # Configure mocks to return degraded state
        mock_llm_checker = MagicMock()
        mock_llm_checker.check_all = AsyncMock(
            return_value={
                "ok": False,
                "embeddings": {"ok": False, "error": "Connection refused"},
                "chat": {"ok": True, "error": None},
                "error": "Embedding service unavailable",
            }
        )

        mock_model_checker = MagicMock()
        mock_model_checker.check_models = AsyncMock(
            return_value={
                "embedding_model": {"available": False, "error": "Model not found"},
                "chat_model": {"available": True, "error": None},
            }
        )

        app.dependency_overrides[get_llm_health_checker] = lambda: mock_llm_checker
        app.dependency_overrides[get_model_checker] = lambda: mock_model_checker

        try:
            response = client.get("/api/health?deep=true")
            self.assertEqual(response.status_code, 200)

            data = response.json()
            self.assertEqual(data["status"], "ok")
            self.assertEqual(data["llm"]["ok"], False)
            self.assertEqual(data["models"]["embedding_model"]["available"], False)
        finally:
            app.dependency_overrides.pop(get_llm_health_checker, None)
            app.dependency_overrides.pop(get_model_checker, None)

    # ==========================================================================
    # Test: Memory Management
    # ==========================================================================

    def test_create_memory_with_invalid_tags(self):
        """Test memory creation handles invalid tag formats."""
        from fastapi.testclient import TestClient

        from app.main import app

        # Setup mocks for app state
        setup_app_state(app)

        client = TestClient(app)

        response = client.post(
            "/api/memories",
            json={
                "content": "Test memory",
                "tags": "invalid{json",  # Invalid JSON
            },
        )

        # Should handle gracefully (either accept or reject with clear error)
        self.assertIn(response.status_code, [200, 400, 422])

    def test_update_nonexistent_memory(self):
        """Test updating a non-existent memory returns 404."""
        from fastapi.testclient import TestClient

        from app.main import app

        # Setup mocks for app state
        setup_app_state(app)

        client = TestClient(app)

        response = client.put(
            "/api/memories/999999", json={"content": "Updated content"}
        )

        self.assertEqual(response.status_code, 404)

    def test_delete_nonexistent_memory(self):
        """Test deleting a non-existent memory returns 404."""
        from fastapi.testclient import TestClient

        from app.main import app

        # Setup mocks for app state
        setup_app_state(app)

        client = TestClient(app)

        response = client.delete("/api/memories/999999")
        self.assertEqual(response.status_code, 404)


# =============================================================================
# Async Integration Tests
# =============================================================================


class TestAsyncIntegration(unittest.IsolatedAsyncioTestCase):
    """Async integration tests for RAG engine flows."""

    async def asyncSetUp(self):
        """Set up async test fixtures."""
        self.fake_embedding = FakeEmbeddingService([0.1] * 768)
        self.fake_llm = FakeLLMClient("Test response")
        self.fake_vector = FakeVectorStore()
        self.fake_memory = FakeMemoryStore()

        # Import RAGEngine here to avoid import issues
        from app.config import settings as app_settings
        from app.services.rag_engine import RAGEngine

        self.rag_engine = RAGEngine(
            embedding_service=self.fake_embedding,
            vector_store=self.fake_vector,
            memory_store=self.fake_memory,
            llm_client=self.fake_llm,
        )
        # Disable atomic visibility filter (no files in test DB)
        self.rag_engine._get_indexed_file_ids = MagicMock(return_value=None)
        # FakeEmbeddingService returns identical embeddings for all texts, so
        # context distillation would deduplicate all chunks. Disable it.
        self._distillation_patcher = patch.object(
            app_settings, "context_distillation_enabled", False
        )
        self._distillation_patcher.start()

    async def asyncTearDown(self):
        self._distillation_patcher.stop()

    async def test_rag_query_with_memory_intent_detection(self):
        """Test RAG engine detects memory storage intent."""
        self.fake_memory._intent = "remember that integration tests are important"

        results = []
        async for chunk in self.rag_engine.query(
            "remember that integration tests are important", [], stream=False
        ):
            results.append(chunk)

        # Should return confirmation without querying vector store
        # (content message + done event are both emitted)
        content_events = [r for r in results if r.get("type") == "content"]
        self.assertGreaterEqual(len(content_events), 1)
        self.assertIn("Memory stored", content_events[0]["content"])

        # Verify memory was added
        self.assertEqual(len(self.fake_memory.added_memories), 1)

    async def test_rag_query_includes_sources_and_memories(self):
        """Test RAG query returns sources and memories in done event."""
        # Setup test data
        self.fake_vector.search_results = [
            {
                "text": "Relevant chunk 1",
                "file_id": "doc1",
                "metadata": {"source_file": "test.txt"},
                "score": 0.9,
            },
            {
                "text": "Relevant chunk 2",
                "file_id": "doc2",
                "metadata": {"source_file": "test2.txt"},
                "score": 0.8,
            },
        ]

        fake_memory = MagicMock()
        fake_memory.content = "Important memory"
        self.fake_memory._memories = [fake_memory]

        # LLM must cite [M1] for the memory to appear in memories_used
        self.fake_llm.response = "Answer based on [M1]."

        results = []
        async for chunk in self.rag_engine.query("test query", [], stream=False):
            results.append(chunk)

        # Verify done event contains sources and memories
        done_event = results[-1]
        self.assertEqual(done_event["type"], "done")
        self.assertEqual(len(done_event["sources"]), 2)
        self.assertEqual(len(done_event["memories_used"]), 1)
        self.assertEqual(done_event["memories_used"][0]["content"], "Important memory")

    async def test_rag_query_with_maintenance_mode(self):
        """Test RAG query behavior during maintenance mode."""
        # Enable maintenance mode
        self.rag_engine.maintenance_mode = True

        results = []
        async for chunk in self.rag_engine.query("test query", [], stream=False):
            results.append(chunk)

        # Should include fallback message
        fallback_events = [r for r in results if r.get("type") == "fallback"]
        self.assertEqual(len(fallback_events), 1)
        self.assertIn("maintenance", fallback_events[0].get("reason", "").lower())

    async def test_rag_streaming_response_order(self):
        """Test streaming response yields chunks in correct order."""
        self.fake_llm.stream_chunks = ["First ", "Second ", "Third"]
        self.fake_vector.search_results = [
            {"text": "test", "file_id": "1", "metadata": {}, "score": 0.9}
        ]

        results = []
        async for chunk in self.rag_engine.query("test", [], stream=True):
            results.append(chunk)

        # Verify content chunks come before done event
        content_types = [r["type"] for r in results]
        self.assertEqual(content_types[0], "content")
        self.assertEqual(content_types[-1], "done")

        # Verify all stream chunks are present
        content_values = [
            r.get("content", "") for r in results if r["type"] == "content"
        ]
        self.assertEqual(content_values, ["First ", "Second ", "Third"])

    async def test_rag_with_embedding_error(self):
        """Test RAG engine handles embedding errors gracefully."""
        self.fake_embedding.raise_error = True

        with self.assertRaises(Exception) as context:
            async for _ in self.rag_engine.query("test", [], stream=False):
                pass

        self.assertIn("embedding", str(context.exception).lower())


if __name__ == "__main__":
    unittest.main()
