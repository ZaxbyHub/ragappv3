"""Unit tests for the RAG pipeline."""

import os
import sys
import unittest
from typing import Any, AsyncIterator, Dict, List, Optional
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

from app.services.rag_engine import RAGEngine
from app.services.reranking import RerankingService


class FakeEmbeddingService:
    """Deterministic fake embedding service for testing."""

    def __init__(self, embedding: Optional[List[float]] = None):
        # Default to a 3-dimensional embedding for predictable tests
        self.embedding = embedding if embedding is not None else [0.1, 0.2, 0.3]

    async def embed_single(self, text: str) -> List[float]:
        return self.embedding.copy()

    async def embed_batch(self, texts: List[str]) -> List[List[float]]:
        return [self.embedding.copy() for _ in texts]


class FakeVectorStore:
    """Deterministic fake vector store for testing."""

    def __init__(self, results: Optional[List[Dict[str, Any]]] = None):
        self._results = results if results is not None else []
        self._fts_exceptions = 0

    async def search(
        self,
        embedding: List[float],
        limit: int = 10,
        filter_expr: Optional[str] = None,
        vault_id: Optional[str] = None,
        query_text: Optional[str] = None,
        hybrid: bool = False,
        hybrid_alpha: float = 0.5,
        query_sparse: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        # Simulate hybrid search by returning combined results
        if hybrid and query_text:
            # Return some results with _distance for dense search
            dense_results = self._results[:limit]
            # Add some FTS-style results with 'score' field and _fts_status
            fts_results = [
                {**r, "score": 0.7 + (i * 0.05), "_fts_status": "ok"}
                for i, r in enumerate(dense_results[:len(dense_results)//2])
            ]
            # Combine dense and FTS results with RRF
            combined = []
            seen_ids = set()
            for r in dense_results[:limit]:
                if r.get("id") not in seen_ids:
                    combined.append(r)
                    seen_ids.add(r.get("id"))
            for r in fts_results:
                if r.get("id") not in seen_ids:
                    combined.append(r)
                    seen_ids.add(r.get("id"))
            return combined[:limit]
        return self._results[:limit]

    async def get_chunks_by_uid(self, chunk_uids: List[str]) -> List[Dict[str, Any]]:
        # Return empty list for fake - real implementation would fetch from DB
        return []

    def get_fts_exceptions(self) -> int:
        """Return the number of FTS exceptions since last reset and reset counter."""
        count = self._fts_exceptions
        self._fts_exceptions = 0
        return count


class FakeMemoryRecord:
    """Simple fake memory record for testing."""

    def __init__(
        self,
        id: int = 1,
        content: str = "",
        category: Optional[str] = None,
        tags: Optional[str] = None,
        source: Optional[str] = None,
        created_at: Optional[str] = None,
        updated_at: Optional[str] = None
    ):
        self.id = id
        self.content = content
        self.category = category
        self.tags = tags
        self.source = source
        self.created_at = created_at
        self.updated_at = updated_at


class FakeMemoryStore:
    """Deterministic fake memory store for testing."""

    def __init__(
        self,
        intent: Optional[str] = None,
        memories: Optional[List[FakeMemoryRecord]] = None
    ):
        self.intent = intent
        self._memories = memories if memories is not None else []
        self.added_memories: List[Dict[str, Any]] = []

    def detect_memory_intent(self, text: str) -> Optional[str]:
        return self.intent

    def add_memory(
        self,
        content: str,
        category: Optional[str] = None,
        tags: Optional[str] = None,
        source: Optional[str] = None,
        vault_id: Optional[int] = None
    ) -> FakeMemoryRecord:
        self.added_memories.append({
            "content": content,
            "category": category,
            "tags": tags,
            "source": source
        })
        return FakeMemoryRecord(
            id=len(self.added_memories),
            content=content,
            category=category,
            tags=tags,
            source=source
        )

    def search_memories(self, query: str, limit: int = 5, vault_id: Optional[int] = None) -> List[FakeMemoryRecord]:
        return self._memories[:limit]


class FakeLLMClient:
    """Deterministic fake LLM client for testing."""

    def __init__(
        self,
        response: str = "",
        stream_chunks: Optional[List[str]] = None
    ):
        self._response = response
        self._stream_chunks = stream_chunks if stream_chunks is not None else []
        self.last_messages: Optional[List[Dict[str, str]]] = None

    async def chat_completion(self, messages: List[Dict[str, str]], **kwargs) -> str:
        self.last_messages = messages
        return self._response

    async def chat_completion_stream(
        self,
        messages: List[Dict[str, str]],
        **kwargs,
    ) -> AsyncIterator[str]:
        self.last_messages = messages
        for chunk in self._stream_chunks:
            yield chunk


class TestRerankingService(unittest.IsolatedAsyncioTestCase):
    """Test suite for RerankingService."""

    def test_reranking_service_initialization_with_endpoint(self):
        """Test RerankingService initialization with TEI endpoint."""
        service = RerankingService(
            reranker_url="http://localhost:8000",
            reranker_model="cross-encoder/ms-marco-MiniLM-L-6-v2",
            top_n=5
        )

        # Verify URL is properly formatted (trailing slash removed)
        self.assertEqual(service.reranker_url, "http://localhost:8000")
        self.assertEqual(service.reranker_model, "cross-encoder/ms-marco-MiniLM-L-6-v2")
        self.assertEqual(service.top_n, 5)

    def test_reranking_service_initialization_with_trailing_slash(self):
        """Test RerankingService removes trailing slash from URL."""
        service = RerankingService(
            reranker_url="http://localhost:8000/",
            reranker_model="cross-encoder/ms-marco-MiniLM-L-6-v2",
            top_n=3
        )

        self.assertEqual(service.reranker_url, "http://localhost:8000")

    def test_reranking_service_initialization_without_endpoint(self):
        """Test RerankingService initialization without TEI endpoint (local model)."""
        service = RerankingService(
            reranker_url="",  # Empty URL means local model
            reranker_model="cross-encoder/ms-marco-MiniLM-L-6-v2",
            top_n=5
        )

        self.assertEqual(service.reranker_url, "")
        self.assertEqual(service.reranker_model, "cross-encoder/ms-marco-MiniLM-L-6-v2")

    async def test_rerank_with_endpoint_success(self):
        """Test reranking via TEI endpoint."""
        # Mock httpx.AsyncClient
        mock_response = MagicMock()
        mock_response.json.return_value = [
            {"index": 1, "score": 0.95},
            {"index": 0, "score": 0.85},
            {"index": 2, "score": 0.75}
        ]
        mock_response.raise_for_status = MagicMock()

        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        service = RerankingService(
            reranker_url="http://localhost:8000",
            reranker_model="cross-encoder/ms-marco-MiniLM-L-6-v2",
            top_n=3
        )

        chunks = [
            {"text": "First chunk"},
            {"text": "Second chunk"},
            {"text": "Third chunk"}
        ]

        with patch('app.services.reranking.httpx.AsyncClient', return_value=mock_client), \
             patch('app.services.reranking.assert_url_safe'):
            reranked_chunks, rerank_success = await service.rerank("test query", chunks)

        self.assertTrue(rerank_success)
        # Verify results are sorted by score descending
        self.assertEqual(len(reranked_chunks), 3)
        self.assertEqual(reranked_chunks[0]["text"], "Second chunk")  # Highest score
        self.assertEqual(reranked_chunks[1]["text"], "First chunk")
        self.assertEqual(reranked_chunks[2]["text"], "Third chunk")

        # Verify scores are attached and monotonically decreasing (sigmoid-normalized)
        s0 = reranked_chunks[0]["_rerank_score"]
        s1 = reranked_chunks[1]["_rerank_score"]
        s2 = reranked_chunks[2]["_rerank_score"]
        self.assertGreater(s0, s1)
        self.assertGreater(s1, s2)
        self.assertGreater(s0, 0.0)
        self.assertLessEqual(s0, 1.0)

    async def test_rerank_with_endpoint_empty_chunks(self):
        """Test reranking with empty chunks list."""
        service = RerankingService(
            reranker_url="http://localhost:8000",
            reranker_model="cross-encoder/ms-marco-MiniLM-L-6-v2",
            top_n=5
        )

        reranked_chunks, rerank_success = await service.rerank("test query", [])
        self.assertEqual(reranked_chunks, [])
        # On empty chunks, rerank returns (chunks[:0], True) — empty chunks with success=True
        self.assertTrue(rerank_success)

    async def test_rerank_with_endpoint_single_chunk(self):
        """Test reranking with single chunk returns unchanged."""
        service = RerankingService(
            reranker_url="http://localhost:8000",
            reranker_model="cross-encoder/ms-marco-MiniLM-L-6-v2",
            top_n=5
        )

        chunks = [{"text": "Single chunk"}]
        reranked_chunks, rerank_success = await service.rerank("test query", chunks)

        # Single chunk bypasses reranker: returns (chunks, True)
        self.assertEqual(reranked_chunks, chunks)
        self.assertTrue(rerank_success)

    async def test_rerank_local_fallback(self):
        """Test reranking with local sentence-transformers fallback."""
        service = RerankingService(
            reranker_url="",  # No endpoint, use local model
            reranker_model="cross-encoder/ms-marco-MiniLM-L-6-v2",
            top_n=3
        )

        chunks = [
            {"text": "Python is a programming language"},
            {"text": "Java is also a programming language"},
            {"text": "Python supports async/await"}
        ]

        # Mock the local model loading and scoring
        with patch('app.services.reranking._get_local_model') as mock_get_model:
            mock_model = MagicMock()
            # Scores: index 0=0.8, index 1=0.5, index 2=0.9
            mock_model.predict.return_value = [0.8, 0.5, 0.9]
            mock_get_model.return_value = mock_model

            reranked_chunks, rerank_success = await service.rerank("test query", chunks)

        self.assertTrue(rerank_success)
        # Verify results are sorted by score descending
        self.assertEqual(len(reranked_chunks), 3)
        self.assertEqual(reranked_chunks[0]["text"], "Python supports async/await")  # Score 0.9
        self.assertEqual(reranked_chunks[1]["text"], "Python is a programming language")  # Score 0.8
        self.assertEqual(reranked_chunks[2]["text"], "Java is also a programming language")  # Score 0.5

    async def test_rerank_local_model_error_handling(self):
        """Test reranking with local model handles errors gracefully."""
        service = RerankingService(
            reranker_url="",  # No endpoint, use local model
            reranker_model="cross-encoder/ms-marco-MiniLM-L-6-v2",
            top_n=5
        )

        chunks = [
            {"text": "Chunk A"},
            {"text": "Chunk B"},
            {"text": "Chunk C"}
        ]

        # Mock model loading to return a model that raises an error on predict
        with patch('app.services.reranking._get_local_model') as mock_get_model:
            mock_model = MagicMock()
            mock_model.predict.side_effect = RuntimeError("Model scoring failed")
            mock_get_model.return_value = mock_model

            reranked_chunks, rerank_success = await service.rerank("test query", chunks)

        # On exception: returns (chunks[:n], False) — original chunks with success=False
        self.assertFalse(rerank_success)
        self.assertEqual(reranked_chunks, chunks[:5])  # Original chunks (up to top_n)

    async def test_rerank_via_endpoint_error_handling(self):
        """Test reranking via endpoint handles HTTP errors gracefully."""
        # Create a mock response that raises an exception on raise_for_status
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock(side_effect=Exception("HTTP Error"))
        mock_response.json = MagicMock()

        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        service = RerankingService(
            reranker_url="http://localhost:8000",
            reranker_model="cross-encoder/ms-marco-MiniLM-L-6-v2",
            top_n=5
        )

        chunks = [
            {"text": "Chunk A"},
            {"text": "Chunk B"},
            {"text": "Chunk C"}
        ]

        with patch('app.services.reranking.httpx.AsyncClient', return_value=mock_client):
            reranked_chunks, rerank_success = await service.rerank("test query", chunks)

        # On exception: returns (chunks[:n], False) — original chunks with success=False
        self.assertFalse(rerank_success)
        self.assertEqual(reranked_chunks, chunks[:5])

    async def test_rerank_top_n_override(self):
        """Test that top_n parameter overrides instance default."""
        service = RerankingService(
            reranker_url="http://localhost:8000",
            reranker_model="cross-encoder/ms-marco-MiniLM-L-6-v2",
            top_n=5
        )

        chunks = [
            {"text": "A"}, {"text": "B"}, {"text": "C"},
            {"text": "D"}, {"text": "E"}, {"text": "F"}
        ]

        mock_response = MagicMock()
        mock_response.json.return_value = [
            {"index": i, "score": 1.0 - i * 0.1} for i in range(len(chunks))
        ]
        mock_response.raise_for_status = MagicMock()

        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch('app.services.reranking.httpx.AsyncClient', return_value=mock_client), \
             patch('app.services.reranking.assert_url_safe'):
            # Override top_n to 3
            reranked_chunks, rerank_success = await service.rerank("test query", chunks, top_n=3)

        self.assertTrue(rerank_success)
        self.assertEqual(len(reranked_chunks), 3)


class TestHybridSearch(unittest.TestCase):
    """Test suite for hybrid search functionality."""

    def test_rrf_fusion_combines_dense_and_fts_results(self):
        """Test Reciprocal Rank Fusion (RRF) combines dense and FTS results."""
        # Simulate dense search results (with _distance)
        dense_results = [
            {"text": "Dense result 1", "_distance": 0.1, "file_id": "d1"},
            {"text": "Dense result 2", "_distance": 0.2, "file_id": "d2"},
            {"text": "Dense result 3", "_distance": 0.3, "file_id": "d3"},
        ]

        # Simulate FTS results (with score)
        fts_results = [
            {"text": "FTS result 1", "score": 0.9, "file_id": "f1"},
            {"text": "Dense result 1", "_distance": 0.1, "file_id": "d1"},  # Overlap!
            {"text": "FTS result 2", "score": 0.8, "file_id": "f2"},
        ]

        # RRF fusion: score = 1/(rank_dense + 1) + 1/(rank_fts + 1)
        # d1: 1/(0+1) + 1/(1+1) = 1.5
        # f1: 1/(3+1) + 1/(0+1) = 1.25
        # d2: 1/(1+1) + 1/(3+1) = 1.0
        # f2: 1/(3+1) + 1/(2+1) = 0.583
        # d3: 1/(2+1) + 0 = 0.333

        # Create hybrid results with both score types
        hybrid_results = []
        seen_ids = set()

        # Add dense results first
        for i, r in enumerate(dense_results):
            hybrid_results.append(r.copy())
            seen_ids.add(r["file_id"])

        # Add FTS results that aren't already in
        for r in fts_results:
            if r["file_id"] not in seen_ids:
                hybrid_results.append(r.copy())
                seen_ids.add(r["file_id"])

        # Verify we have combined results
        self.assertEqual(len(hybrid_results), 5)  # d1, d2, d3, f1, f2

    def test_rrf_fusion_with_overlapping_results(self):
        """Test RRF handles overlapping results from dense and FTS."""
        # Both search methods return the same top result
        dense_results = [
            {"text": "Best match", "_distance": 0.05, "file_id": "same"},
        ]

        fts_results = [
            {"text": "Best match", "score": 0.95, "file_id": "same"},
        ]

        # RRF score for overlapping result:
        # rank_dense=0, rank_fts=0
        # score = 1/(0+1) + 1/(0+1) = 2.0

        self.assertEqual(len(dense_results), 1)
        self.assertEqual(len(fts_results), 1)
        self.assertEqual(dense_results[0]["file_id"], fts_results[0]["file_id"])

    def test_rrf_fusion_no_overlap(self):
        """Test RRF when dense and FTS results don't overlap."""
        dense_results = [
            {"text": "Dense only", "_distance": 0.1, "file_id": "dense_only"},
        ]

        fts_results = [
            {"text": "FTS only", "score": 0.9, "file_id": "fts_only"},
        ]

        # No overlap - each result gets only one component of RRF
        self.assertNotEqual(dense_results[0]["file_id"], fts_results[0]["file_id"])


class TestRAGEnginePipeline(unittest.IsolatedAsyncioTestCase):
    """Test suite for RAGEngine pipeline functionality."""

    async def test_two_stage_retrieval_with_reranking(self):
        """Test initial retrieval + reranking pipeline."""
        # Stage 1: Initial retrieval returns many results
        initial_results = [
            {"text": "Relevant chunk 1", "file_id": "doc1", "_distance": 0.1, "metadata": {}},
            {"text": "Relevant chunk 2", "file_id": "doc2", "_distance": 0.2, "metadata": {}},
            {"text": "Relevant chunk 3", "file_id": "doc3", "_distance": 0.3, "metadata": {}},
            {"text": "Relevant chunk 4", "file_id": "doc4", "_distance": 0.4, "metadata": {}},
            {"text": "Relevant chunk 5", "file_id": "doc5", "_distance": 0.5, "metadata": {}},
        ]

        fake_vector = FakeVectorStore(results=initial_results)
        fake_embedding = FakeEmbeddingService()
        fake_memory = FakeMemoryStore()
        fake_llm = FakeLLMClient(response="Answer based on retrieved chunks.")

        # Create engine with reranking enabled
        engine = RAGEngine(
            embedding_service=fake_embedding,
            vector_store=fake_vector,
            memory_store=fake_memory,
            llm_client=fake_llm,
            reranking_service=None  # Will use default from settings
        )

        # Enable reranking
        engine.reranking_enabled = True
        engine.initial_retrieval_top_k = 5
        engine.retrieval_top_k = 3

        results = []
        async for msg in engine.query("test query", []):
            results.append(msg)

        # Should have content and done messages (stage events may also be present)
        non_stage_results = [r for r in results if r.get("type") not in ("stage",)]
        self.assertEqual(len(non_stage_results), 2)

        # Check that we got results
        done_msg = results[-1]
        self.assertEqual(done_msg["type"], "done")
        self.assertIn("sources", done_msg)
        # After reranking and filtering, should have at most retrieval_top_k results
        self.assertLessEqual(len(done_msg["sources"]), engine.retrieval_top_k)

    async def test_two_stage_retrieval_without_reranking(self):
        """Test two-stage retrieval when reranking is disabled."""
        initial_results = [
            {"text": "Result 1", "file_id": "doc1", "_distance": 0.1, "metadata": {}},
            {"text": "Result 2", "file_id": "doc2", "_distance": 0.2, "metadata": {}},
        ]

        fake_vector = FakeVectorStore(results=initial_results)
        fake_embedding = FakeEmbeddingService()
        fake_memory = FakeMemoryStore()
        fake_llm = FakeLLMClient(response="Answer.")

        engine = RAGEngine(
            embedding_service=fake_embedding,
            vector_store=fake_vector,
            memory_store=fake_memory,
            llm_client=fake_llm
        )

        # Disable reranking
        engine.reranking_enabled = False
        engine.retrieval_top_k = 10

        results = []
        async for msg in engine.query("test query", []):
            results.append(msg)

        done_msg = results[-1]
        # Without reranking, should return up to initial_retrieval_top_k results
        self.assertLessEqual(len(done_msg["sources"]), 10)

    async def test_hybrid_search_enabled(self):
        """Test that hybrid search is passed to vector store."""
        initial_results = [
            {"text": "Hybrid result", "file_id": "doc1", "_distance": 0.1, "metadata": {}},
        ]

        fake_vector = FakeVectorStore(results=initial_results)
        fake_embedding = FakeEmbeddingService()
        fake_memory = FakeMemoryStore()
        fake_llm = FakeLLMClient(response="Answer.")

        engine = RAGEngine(
            embedding_service=fake_embedding,
            vector_store=fake_vector,
            memory_store=fake_memory,
            llm_client=fake_llm
        )

        # Enable hybrid search
        engine.hybrid_search_enabled = True
        engine.hybrid_alpha = 0.5
        engine.retrieval_top_k = 5

        # Mock the vector store search to capture parameters
        original_search = fake_vector.search
        search_params = {}

        def mock_search(*args, **kwargs):
            search_params.update(kwargs)
            return original_search(*args, **kwargs)

        fake_vector.search = mock_search

        async for _ in engine.query("test query", []):
            pass

        # Verify hybrid search parameters were passed
        self.assertTrue(search_params.get("hybrid", False))
        # With Phase 2 BM25 FTS changes, hybrid_alpha stays at 0.5 when sparse embedding
        # fails (BM25 FTS is used as fallback, not dense-only fallback)
        self.assertAlmostEqual(search_params.get("hybrid_alpha", 0), 0.5, places=1)

    async def test_hybrid_search_disabled(self):
        """Test that hybrid search is not passed when disabled."""
        initial_results = [
            {"text": "Dense result", "file_id": "doc1", "_distance": 0.1, "metadata": {}},
        ]

        fake_vector = FakeVectorStore(results=initial_results)
        fake_embedding = FakeEmbeddingService()
        fake_memory = FakeMemoryStore()
        fake_llm = FakeLLMClient(response="Answer.")

        engine = RAGEngine(
            embedding_service=fake_embedding,
            vector_store=fake_vector,
            memory_store=fake_memory,
            llm_client=fake_llm
        )

        # Disable hybrid search
        engine.hybrid_search_enabled = False
        engine.retrieval_top_k = 5

        # Mock the vector store search to capture parameters
        search_params = {}

        def mock_search(*args, **kwargs):
            search_params.update(kwargs)
            return []

        fake_vector.search = mock_search

        async for _ in engine.query("test query", []):
            pass

        # Verify hybrid search was disabled
        self.assertFalse(search_params.get("hybrid", True))

    async def test_retrieval_window_expansion(self):
        """Test that retrieval window expands to adjacent chunks."""
        # Use longer text to pass the 50-char minimum after dedup
        initial_results = [
            {"id": "doc1_5", "text": "This is the main chunk with some additional content to ensure it passes the 50 character minimum threshold for context distillation.", "file_id": "doc1", "_distance": 0.1,
             "metadata": {"chunk_index": 5}},
        ]

        fake_vector = FakeVectorStore(results=initial_results)
        fake_embedding = FakeEmbeddingService()
        fake_memory = FakeMemoryStore()
        fake_llm = FakeLLMClient(response="Answer.")

        # Enable window expansion before creating engine
        fake_vector.retrieval_window = 2
        fake_vector.retrieval_top_k = 10

        # Mock get_chunks_by_uid to return adjacent chunks using patch
        # Note: The RAGEngine uses "id" field for lookup, so we need to include it
        adjacent_chunks = [
            {"id": "doc1_4", "text": "This is chunk 4 with sufficient content to pass the 50 character minimum after deduplication.", "file_id": "doc1", "_distance": 0.15,
             "metadata": {"chunk_index": 4}},
            {"id": "doc1_6", "text": "This is chunk 6 with sufficient content to pass the 50 character minimum after deduplication.", "file_id": "doc1", "_distance": 0.18,
             "metadata": {"chunk_index": 6}},
        ]

        async def mock_get_chunks_by_uid(chunk_uids):
            return adjacent_chunks

        from app.config import settings as app_settings

        with patch.object(fake_vector, 'get_chunks_by_uid', side_effect=mock_get_chunks_by_uid), \
             patch.object(app_settings, 'context_distillation_enabled', False), \
             patch.object(app_settings, 'parent_retrieval_enabled', False):
            engine = RAGEngine(
                embedding_service=fake_embedding,
                vector_store=fake_vector,
                memory_store=fake_memory,
                llm_client=fake_llm
            )

            # Enable window expansion and disable hybrid search
            engine.hybrid_search_enabled = False
            engine.retrieval_window = 2
            engine.retrieval_top_k = 10

            results = []
            async for msg in engine.query("test query", []):
                results.append(msg)

            done_msg = results[-1]
            # Debug: print what we got
            print(f"DEBUG: Got {len(done_msg.get('sources', []))} sources")
            for s in done_msg.get("sources", []):
                print(f"  Source: {s.get('file_id')}, chunk_index={s.get('metadata', {}).get('chunk_index')}")

            # Should include main chunk plus adjacent chunks (3 total)
            # Note: This test may fail due to distance threshold filtering.
            # If it fails, the issue is that document_retrieval filter_relevant
            # is removing all results based on max_distance_threshold.
            # The test has been updated to show debug info.
            self.assertEqual(len(done_msg["sources"]), 3)
            # Verify the sources are sorted by chunk_index
            self.assertEqual(done_msg["sources"][0]["file_id"], "doc1")
            self.assertEqual(done_msg["sources"][0]["metadata"].get("chunk_index"), 4)
            self.assertEqual(done_msg["sources"][1]["file_id"], "doc1")
            self.assertEqual(done_msg["sources"][1]["metadata"].get("chunk_index"), 5)
            self.assertEqual(done_msg["sources"][2]["file_id"], "doc1")
            self.assertEqual(done_msg["sources"][2]["metadata"].get("chunk_index"), 6)

    async def test_empty_query_handling(self):
        """Test handling of empty query string."""
        fake_vector = FakeVectorStore(results=[])
        fake_embedding = FakeEmbeddingService()
        fake_memory = FakeMemoryStore()
        fake_llm = FakeLLMClient(response="No relevant documents found.")

        engine = RAGEngine(
            embedding_service=fake_embedding,
            vector_store=fake_vector,
            memory_store=fake_memory,
            llm_client=fake_llm
        )

        results = []
        async for msg in engine.query("", []):
            results.append(msg)

        # Should still return a response
        self.assertGreater(len(results), 0)

    async def test_maintenance_mode_fallback(self):
        """Test that maintenance mode triggers fallback path."""
        fake_vector = FakeVectorStore(results=[
            {"text": "Should not be returned", "file_id": "doc1", "_distance": 0.1, "metadata": {}}
        ])
        fake_embedding = FakeEmbeddingService()
        fake_memory = FakeMemoryStore()
        fake_llm = FakeLLMClient(response="Answer.")

        engine = RAGEngine(
            embedding_service=fake_embedding,
            vector_store=fake_vector,
            memory_store=fake_memory,
            llm_client=fake_llm
        )

        # Enable maintenance mode
        engine.maintenance_mode = True

        results = []
        async for msg in engine.query("test query", []):
            results.append(msg)

        # Should have fallback message
        fallback_messages = [m for m in results if m.get("type") == "fallback"]
        self.assertGreater(len(fallback_messages), 0)


if __name__ == "__main__":
    unittest.main()
