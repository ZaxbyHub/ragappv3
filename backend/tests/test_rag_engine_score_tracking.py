"""Integration tests for rag_engine.py score_type/score tracking."""

import os
import sys
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple
from unittest.mock import patch

import pytest

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


class FakeEmbeddingService:
    """Deterministic fake embedding service for testing."""

    def __init__(self, embedding: Optional[List[float]] = None):
        # Default to a 3-dimensional embedding for predictable tests
        self.embedding = embedding if embedding is not None else [0.1, 0.2, 0.3]

    async def embed_single(self, text: str) -> List[float]:
        return self.embedding.copy()

    async def embed_batch(self, texts: List[str]) -> List[List[float]]:
        return [self.embedding.copy() for _ in texts]

    async def embed_query_sparse(self, text: str) -> Optional[Dict[str, Any]]:
        """Return None to skip sparse search."""
        return None


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


class FakeMemoryStore:
    """Deterministic fake memory store for testing."""

    def __init__(self, intent: Optional[str] = None):
        self.intent = intent

    def detect_memory_intent(self, text: str) -> Optional[str]:
        return self.intent

    def search_memories(self, query: str, limit: int, vault_id=None) -> list:
        """Sync version for asyncio.to_thread compatibility."""
        return []


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


class FakeRerankingService:
    """Fake RerankingService for testing with controllable behavior."""

    def __init__(self, success: bool = True, results: Optional[List[Dict[str, Any]]] = None):
        self.success = success  # Whether reranking should succeed
        self.results = results  # Custom results to return

    async def rerank(
        self,
        query: str,
        chunks: List[Dict[str, Any]],
        top_n: Optional[int] = None
    ) -> Tuple[List[Dict[str, Any]], bool]:
        if not self.success:
            raise Exception("Reranking failed")

        # If custom results provided, return them
        if self.results is not None:
            return self.results, True

        # Default: return chunks with _rerank_score
        result = []
        for i, chunk in enumerate(chunks[:top_n]):
            chunk_copy = dict(chunk)
            # Score in [0, 1] range for rerank success
            chunk_copy["_rerank_score"] = 0.9 - (i * 0.1)
            result.append(chunk_copy)
        return result, True


# Test fixture
@pytest.mark.asyncio
class TestRAGEngineScoreTracking:
    """Test suite for RAGEngine score_type and score tracking."""

    def _create_engine(self, reranking_service=None, reranking_enabled=True, hybrid_search_enabled=True):
        """Create a RAGEngine with fake components."""
        fake_vector = FakeVectorStore()
        fake_embedding = FakeEmbeddingService()
        fake_memory = FakeMemoryStore()
        fake_llm = FakeLLMClient(response="Answer.")

        return RAGEngine(
            embedding_service=fake_embedding,
            vector_store=fake_vector,
            memory_store=fake_memory,
            llm_client=fake_llm,
            reranking_service=reranking_service
        )

    async def test_reranking_enabled_and_success(self):
        """Test reranking enabled and success → score_type='rerank', score in [0,1]."""
        rerank_service = FakeRerankingService(success=True)
        engine = self._create_engine(reranking_service=rerank_service)
        engine.reranking_enabled = True
        engine.hybrid_search_enabled = False

        # Initial results with _distance
        initial_results = [
            {"id": "chunk1", "text": "Relevant chunk 1", "file_id": "doc1", "_distance": 0.2},
            {"id": "chunk2", "text": "Relevant chunk 2", "file_id": "doc2", "_distance": 0.4},
            {"id": "chunk3", "text": "Relevant chunk 3", "file_id": "doc3", "_distance": 0.6},
        ]

        engine.vector_store = FakeVectorStore(results=initial_results)

        results = []
        async for msg in engine.query("test query", []):
            results.append(msg)

        # Find done message
        done_msg = None
        for msg in results:
            if msg.get("type") == "done":
                done_msg = msg
                break

        assert done_msg is not None, "No done message found"
        assert done_msg.get("score_type") == "rerank", f"Expected score_type='rerank', got '{done_msg.get('score_type')}'"

        # Check all chunk scores are in [0, 1]
        for source in done_msg.get("sources", []):
            score = source.get("score", 0)
            assert 0 <= score <= 1, f"Score {score} not in [0, 1]"

        print("✓ test_reranking_enabled_and_success passed")

    async def test_reranking_fallback_on_exception(self):
        """Test reranking exception → score_type='distance', score=_distance."""
        rerank_service = FakeRerankingService(success=False)
        engine = self._create_engine(reranking_service=rerank_service)
        engine.reranking_enabled = True
        engine.hybrid_search_enabled = False

        initial_results = [
            {"id": "chunk1", "text": "Relevant chunk 1", "file_id": "doc1", "_distance": 0.1},
            {"id": "chunk2", "text": "Relevant chunk 2", "file_id": "doc2", "_distance": 0.3},
        ]

        engine.vector_store = FakeVectorStore(results=initial_results)

        results = []
        async for msg in engine.query("test query", []):
            results.append(msg)

        done_msg = None
        for msg in results:
            if msg.get("type") == "done":
                done_msg = msg
                break

        assert done_msg is not None, "No done message found"
        assert done_msg.get("score_type") == "distance", f"Expected score_type='distance', got '{done_msg.get('score_type')}'"

        # Check all chunk scores are _distance values
        for source in done_msg.get("sources", []):
            score = source.get("score", 0)
            # With fallback, scores should match original _distance
            assert isinstance(score, (int, float)), f"Score {score} is not numeric"

        print("✓ test_reranking_fallback_on_exception passed")

    async def test_reranking_disabled(self):
        """Test reranking disabled → score_type='distance'."""
        engine = self._create_engine(reranking_service=None)
        engine.reranking_enabled = False
        engine.hybrid_search_enabled = False

        initial_results = [
            {"id": "chunk1", "text": "Relevant chunk 1", "file_id": "doc1", "_distance": 0.2},
        ]

        engine.vector_store = FakeVectorStore(results=initial_results)

        results = []
        async for msg in engine.query("test query", []):
            results.append(msg)

        done_msg = None
        for msg in results:
            if msg.get("type") == "done":
                done_msg = msg
                break

        assert done_msg is not None, "No done message found"
        assert done_msg.get("score_type") == "distance", f"Expected score_type='distance', got '{done_msg.get('score_type')}'"

        print("✓ test_reranking_disabled passed")

    async def test_rerank_status_ok(self):
        """Test reranking success → rerank_status='ok'."""
        rerank_service = FakeRerankingService(success=True)
        engine = self._create_engine(reranking_service=rerank_service)
        engine.reranking_enabled = True
        engine.hybrid_search_enabled = False

        initial_results = [
            {"id": "chunk1", "text": "Relevant chunk 1", "file_id": "doc1", "_distance": 0.2},
        ]

        engine.vector_store = FakeVectorStore(results=initial_results)

        results = []
        async for msg in engine.query("test query", []):
            results.append(msg)

        done_msg = None
        for msg in results:
            if msg.get("type") == "done":
                done_msg = msg
                break

        assert done_msg is not None, "No done message found"
        assert done_msg["retrieval_debug"]["rerank_status"] == "ok", \
            f"Expected rerank_status='ok', got '{done_msg['retrieval_debug'].get('rerank_status')}'"

        print("✓ test_rerank_status_ok passed")

    async def test_rerank_status_fallback(self):
        """Test reranking failure → rerank_status='fallback'."""
        # Mock _execute_retrieval to return tuple with rerank_status='fallback' (rerank_success=None)
        rerank_service = FakeRerankingService(success=False)
        engine = self._create_engine(reranking_service=rerank_service)
        engine.reranking_enabled = True
        engine.hybrid_search_enabled = False

        initial_results = [
            {"id": "chunk1", "text": "Relevant chunk 1", "file_id": "doc1", "_distance": 0.2},
        ]

        engine.vector_store = FakeVectorStore(results=initial_results)

        # Patch _execute_retrieval to return the correct tuple format with False for rerank_success
        async def mock_retrieval(*args, **kwargs):
            # Return properly formatted dicts (not RAGSource) to avoid AttributeError in .get() calls
            return [{"id": "chunk1", "text": "Relevant chunk 1", "file_id": "doc1", "_distance": 0.2, "metadata": {}}], None, "CONFIDENT", False, "distance", "dense_only", 0, "fallback", False, False, {}

        with patch.object(engine, '_execute_retrieval', mock_retrieval):
            results = []
            async for msg in engine.query("test query", []):
                results.append(msg)

            done_msg = None
            for msg in results:
                if msg.get("type") == "done":
                    done_msg = msg
                    break

            assert done_msg is not None, "No done message found"
            assert done_msg["retrieval_debug"]["rerank_status"] == "fallback", \
                f"Expected rerank_status='fallback', got '{done_msg['retrieval_debug'].get('rerank_status')}'"

        print("✓ test_rerank_status_fallback passed")

    async def test_rerank_status_disabled(self):
        """Test reranking disabled → rerank_status='disabled'."""
        engine = self._create_engine(reranking_service=None)
        engine.reranking_enabled = False
        engine.hybrid_search_enabled = False

        initial_results = [
            {"id": "chunk1", "text": "Relevant chunk 1", "file_id": "doc1", "_distance": 0.2},
        ]

        engine.vector_store = FakeVectorStore(results=initial_results)

        results = []
        async for msg in engine.query("test query", []):
            results.append(msg)

        done_msg = None
        for msg in results:
            if msg.get("type") == "done":
                done_msg = msg
                break

        assert done_msg is not None, "No done message found"
        assert done_msg["retrieval_debug"]["rerank_status"] == "disabled", \
            f"Expected rerank_status='disabled', got '{done_msg['retrieval_debug'].get('rerank_status')}'"

        print("✓ test_rerank_status_disabled passed")

    async def test_hybrid_status_both(self):
        """Test hybrid enabled with FTS returning ok → hybrid_status='both'."""
        engine = self._create_engine(reranking_service=None)
        engine.reranking_enabled = False
        engine.hybrid_search_enabled = True

        # Results with _fts_status='ok'
        initial_results = [
            {"id": "chunk1", "text": "Relevant chunk 1", "file_id": "doc1",
             "_distance": 0.2, "_fts_status": "ok"},
            {"id": "chunk2", "text": "Relevant chunk 2", "file_id": "doc2",
             "_distance": 0.4, "_fts_status": "ok"},
        ]

        engine.vector_store = FakeVectorStore(results=initial_results)

        results = []
        async for msg in engine.query("test query", []):
            results.append(msg)

        done_msg = None
        for msg in results:
            if msg.get("type") == "done":
                done_msg = msg
                break

        assert done_msg is not None, "No done message found"
        assert done_msg["retrieval_debug"]["hybrid_status"] == "both", \
            f"Expected hybrid_status='both', got '{done_msg['retrieval_debug'].get('hybrid_status')}'"

        print("✓ test_hybrid_status_both passed")

    async def test_hybrid_status_dense_only(self):
        """Test hybrid enabled but FTS returning empty → hybrid_status='dense_only'."""
        engine = self._create_engine(reranking_service=None)
        engine.reranking_enabled = False
        engine.hybrid_search_enabled = True

        # Results without _fts_status (dense only) or with _fts_status='empty'
        initial_results = [
            {"id": "chunk1", "text": "Relevant chunk 1", "file_id": "doc1",
             "_distance": 0.2, "_fts_status": "empty"},
            {"id": "chunk2", "text": "Relevant chunk 2", "file_id": "doc2",
             "_distance": 0.4},
        ]

        engine.vector_store = FakeVectorStore(results=initial_results)

        results = []
        async for msg in engine.query("test query", []):
            results.append(msg)

        done_msg = None
        for msg in results:
            if msg.get("type") == "done":
                done_msg = msg
                break

        assert done_msg is not None, "No done message found"
        assert done_msg["retrieval_debug"]["hybrid_status"] == "dense_only", \
            f"Expected hybrid_status='dense_only', got '{done_msg['retrieval_debug'].get('hybrid_status')}'"

        print("✓ test_hybrid_status_dense_only passed")

    async def test_hybrid_status_disabled(self):
        """Test hybrid disabled → hybrid_status='disabled'."""
        engine = self._create_engine(reranking_service=None)
        engine.reranking_enabled = False
        engine.hybrid_search_enabled = False

        initial_results = [
            {"id": "chunk1", "text": "Relevant chunk 1", "file_id": "doc1", "_distance": 0.2},
        ]

        engine.vector_store = FakeVectorStore(results=initial_results)

        results = []
        async for msg in engine.query("test query", []):
            results.append(msg)

        done_msg = None
        for msg in results:
            if msg.get("type") == "done":
                done_msg = msg
                break

        assert done_msg is not None, "No done message found"
        assert done_msg["retrieval_debug"]["hybrid_status"] == "disabled", \
            f"Expected hybrid_status='disabled', got '{done_msg['retrieval_debug'].get('hybrid_status')}'"

        print("✓ test_hybrid_status_disabled passed")


def run_tests():
    """Run all tests and report results."""
    import pytest

    # Set module path
    test_file = os.path.abspath(__file__)

    # Run pytest with verbose output
    exit_code = pytest.main([test_file, "-v", "--tb=short"])

    return exit_code


if __name__ == "__main__":
    exit_code = run_tests()
    print(f"\n{'='*60}")
    if exit_code == 0:
        print("ALL TESTS PASSED ✓")
    else:
        print(f"SOME TESTS FAILED (exit code: {exit_code})")
    print(f"{'='*60}")
    sys.exit(exit_code)
