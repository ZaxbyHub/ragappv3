"""Unit tests for the RAG pipeline."""

import os
import sys
import unittest
from typing import Any, Dict, List, Optional, cast
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
    _unstructured.partition = types.ModuleType('unstructured.partition')
    _unstructured.partition.auto = types.ModuleType('unstructured.partition.auto')
    _unstructured.partition.auto.partition = lambda *args, **kwargs: []
    _unstructured.chunking = types.ModuleType('unstructured.chunking')
    _unstructured.chunking.title = types.ModuleType('unstructured.chunking.title')
    _unstructured.chunking.title.chunk_by_title = lambda *args, **kwargs: []
    _unstructured.documents = types.ModuleType('unstructured.documents')
    _unstructured.documents.elements = types.ModuleType('unstructured.documents.elements')
    _unstructured.documents.elements.Element = type('Element', (), {})
    sys.modules['unstructured'] = _unstructured
    sys.modules['unstructured.partition'] = _unstructured.partition
    sys.modules['unstructured.partition.auto'] = _unstructured.partition.auto
    sys.modules['unstructured.chunking'] = _unstructured.chunking
    sys.modules['unstructured.chunking.title'] = _unstructured.chunking.title
    sys.modules['unstructured.documents'] = _unstructured.documents
    sys.modules['unstructured.documents.elements'] = _unstructured.documents.elements

from app.services.embeddings import EmbeddingService
from app.services.llm_client import LLMClient, LLMError
from app.services.memory_store import MemoryRecord, MemoryStore
from app.services.rag_engine import EmbeddingError, RAGEngine, RAGEngineError
from app.services.vector_store import VectorStore


@pytest.fixture(autouse=True)
def _patch_ssrf():
    """Prevent SSRF guard from blocking service construction in tests."""
    with patch('app.services.embeddings.assert_url_safe'), \
         patch('app.services.llm_client.assert_url_safe'):
        yield


class FakeEmbeddingService:
    def __init__(self, embedding: List[float]):
        self.embedding = embedding

    async def embed_single(self, text: str) -> List[float]:
        return self.embedding

    async def embed_passage(self, text: str) -> List[float]:
        return self.embedding


class FakeVectorStore:
    def __init__(self, results: List[Dict]):
        self._results = results

    async def search(self, embedding: List[float], limit: int = 10, filter_expr=None, vault_id=None, query_text=None, hybrid=False, **kwargs):
        results = self._results
        if vault_id is not None:
            target = str(vault_id)
            results = [r for r in results if str(r.get("vault_id")) == target]
        return results[:limit]

    def get_chunks_by_uid(self, chunk_uids: List[str]):
        # Return empty list for fake - real implementation would fetch from DB
        return []

    def get_fts_exceptions(self) -> int:
        # Match real VectorStore interface — no FTS exceptions in fake
        return 0


class FakeMemoryStore:
    def __init__(self, intent: Optional[str] = None, memories: Optional[List[MemoryRecord]] = None):
        self.intent = intent
        self._memories = memories or []
        self.added: List[str] = []

    def detect_memory_intent(self, text: str):
        return self.intent

    def add_memory(self, content: str, category=None, tags=None, source=None, vault_id=None):
        self.added.append(content)
        return MemoryRecord(id=1, content=content, category=category, tags=tags, source=source, created_at=None, updated_at=None)

    def search_memories(self, query: str, limit: int = 5, vault_id=None, include_global: bool = False):
        # include_global is accepted (issue #404) but ignored by this fake —
        # the global-exclusion behavior is covered by test_memory_global_authz.py
        # and test_rag_engine_memory_scope.py against the real MemoryStore.
        return self._memories[:limit]


class FakeLLMClient:
    def __init__(self, response: str, stream_chunks: Optional[List[str]] = None, raise_llm_error: bool = False):
        self._response = response
        self._stream_chunks = stream_chunks or []
        self._raise_llm_error = raise_llm_error

    async def chat_completion(self, messages, **kwargs):
        return self._response

    async def chat_completion_stream(self, messages, **kwargs):
        if self._raise_llm_error:
            raise LLMError("Simulated LLM streaming error")
        for chunk in self._stream_chunks:
            yield chunk


class RAGEngineTests(unittest.IsolatedAsyncioTestCase):
    async def test_detects_memory_intent_and_returns_confirmation(self):
        memory_store = FakeMemoryStore(intent="remember that you are helpful")
        engine = RAGEngine()
        engine.embedding_service = cast(EmbeddingService, FakeEmbeddingService([0.1, 0.2]))
        engine.vector_store = cast(VectorStore, FakeVectorStore([]))
        engine.memory_store = cast(MemoryStore, memory_store)
        engine.llm_client = cast(LLMClient, FakeLLMClient(response=""))
        results = [msg async for msg in engine.query("remember that foo", [], stream=False, can_write_memory=True)]
        content_msgs = [r for r in results if r.get("type") == "content"]
        self.assertGreaterEqual(len(content_msgs), 1)
        self.assertIn("Memory stored", content_msgs[0]["content"])
        self.assertEqual(["remember that you are helpful"], memory_store.added)

    async def test_query_returns_sources_and_memories(self):
        memory = MemoryRecord(id=1, content="Important fact", category=None, tags=None, source="test", created_at=None, updated_at=None)
        vector_results = [
            {"text": "chunk one", "file_id": "file1", "metadata": {"source_file": "doc.md"}, "score": 0.9},
        ]
        engine = RAGEngine()
        engine.embedding_service = cast(EmbeddingService, FakeEmbeddingService([0.1, 0.2]))
        engine.vector_store = cast(VectorStore, FakeVectorStore(vector_results))
        engine.memory_store = cast(MemoryStore, FakeMemoryStore(memories=[memory]))
        # Response cites [M1] so that memories_used contains the cited memory.
        engine.llm_client = cast(LLMClient, FakeLLMClient(response="Important fact [M1]"))
        results = [msg async for msg in engine.query("query", [], stream=False)]
        content_msgs = [r for r in results if r.get("type") == "content"]
        self.assertEqual(1, len(content_msgs))
        done = results[-1]
        self.assertEqual("done", done["type"])
        # memories_used must contain only the cited memory as a structured dict.
        self.assertEqual(1, len(done["memories_used"]))
        self.assertEqual("M1", done["memories_used"][0]["memory_label"])
        self.assertEqual(memory.content, done["memories_used"][0]["content"])

    async def test_streaming_response_yields_chunks(self):
        engine = RAGEngine()
        engine.embedding_service = cast(EmbeddingService, FakeEmbeddingService([0.1, 0.2]))
        engine.vector_store = cast(
            VectorStore,
            FakeVectorStore([
                {"text": "chunk", "file_id": "file1", "metadata": {}, "score": 0.5}
            ]),
        )
        engine.memory_store = cast(MemoryStore, FakeMemoryStore())
        engine.llm_client = cast(LLMClient, FakeLLMClient(response="", stream_chunks=["part1", "part2"]))
        stream = [msg async for msg in engine.query("query", [], stream=True)]
        content_msgs = [s for s in stream if s.get("type") == "content"]
        self.assertEqual(2, len(content_msgs))
        self.assertEqual("part1", content_msgs[0]["content"])
        self.assertEqual("done", stream[-1]["type"])

    async def test_filter_relevant_filters_scores_below_threshold(self):
        """Test that _distance field is used (lower distance = better match)."""
        engine = RAGEngine()
        engine.max_distance_threshold = 0.5
        results = [
            {"text": "close", "file_id": "f1", "metadata": {}, "_distance": 0.3},
            {"text": "at_threshold", "file_id": "f2", "metadata": {}, "_distance": 0.5},
            {"text": "far", "file_id": "f3", "metadata": {}, "_distance": 0.8},
        ]
        filtered = await engine._filter_relevant(results)
        # Keep distances <= threshold (0.3 <= 0.5, 0.5 <= 0.5), filter 0.8 > 0.5
        self.assertEqual(2, len(filtered))
        self.assertEqual("close", filtered[0].text)
        self.assertEqual("at_threshold", filtered[1].text)

    async def test_filter_relevant_includes_scores_equal_to_threshold(self):
        """Test that distances equal to threshold are included (distance <= threshold)."""
        engine = RAGEngine()
        engine.max_distance_threshold = 0.5
        results = [
            {"text": "at_threshold", "file_id": "f1", "metadata": {}, "_distance": 0.5},
            {"text": "above", "file_id": "f2", "metadata": {}, "_distance": 0.6},
        ]
        filtered = await engine._filter_relevant(results)
        # Distances <= threshold are included: 0.5 <= 0.5 (keep), 0.6 > 0.5 (skip)
        self.assertEqual(1, len(filtered))
        self.assertEqual("at_threshold", filtered[0].text)

    async def test_filter_relevant_handles_none_score_as_default(self):
        engine = RAGEngine()
        engine.relevance_threshold = 0.5
        results = [
            {"text": "none_score", "file_id": "f1", "metadata": {}, "score": None},
            {"text": "low", "file_id": "f2", "metadata": {}, "score": 0.4},
        ]
        filtered = await engine._filter_relevant(results)
        self.assertEqual(1, len(filtered))
        self.assertEqual("none_score", filtered[0].text)
        self.assertEqual(1.0, filtered[0].score)

    async def test_filter_relevant_with_mixed_scores(self):
        """Test filtering with mixed distances - keep distances <= threshold."""
        engine = RAGEngine()
        engine.max_distance_threshold = 0.5
        results = [
            {"text": "a", "file_id": "f1", "metadata": {"s": 1}, "_distance": 0.4},
            {"text": "b", "file_id": "f2", "metadata": {"s": 2}, "_distance": 0.5},
            {"text": "c", "file_id": "f3", "metadata": {"s": 3}, "_distance": 0.6},
            {"text": "d", "file_id": "f4", "metadata": {"s": 4}, "_distance": 0.3},
            {"text": "e", "file_id": "f5", "metadata": {"s": 5}, "_distance": 0.8},
        ]
        filtered = await engine._filter_relevant(results)
        # Distances <= 0.5: a(0.4), b(0.5), d(0.3) = 3 results
        self.assertEqual(3, len(filtered))
        self.assertEqual("a", filtered[0].text)
        self.assertEqual(0.4, filtered[0].score)
        self.assertEqual(1, filtered[0].metadata.get("s"))
        self.assertEqual("b", filtered[1].text)
        self.assertEqual(0.5, filtered[1].score)
        self.assertEqual(2, filtered[1].metadata.get("s"))
        self.assertEqual("d", filtered[2].text)
        self.assertEqual(0.3, filtered[2].score)
        self.assertEqual(4, filtered[2].metadata.get("s"))

    def test_build_messages_with_empty_context(self):
        engine = RAGEngine()
        messages = engine._build_messages("my question", [], [], [])
        self.assertEqual(2, len(messages))
        self.assertEqual("system", messages[0]["role"])
        self.assertEqual("user", messages[1]["role"])
        self.assertEqual("No relevant documents found for this query.\n\nQuestion: <user_query>my question</user_query>", messages[1]["content"])

    async def test_filter_relevant_filters_by_distance_with_lancedb_results(self):
        """Test that _distance field from LanceDB is used correctly (lower = better)."""
        engine = RAGEngine()
        engine.max_distance_threshold = 0.5  # Distance threshold
        results = [
            {"text": "close match", "file_id": "f1", "metadata": {}, "_distance": 0.2},  # Keep (0.2 <= 0.5)
            {"text": "at threshold", "file_id": "f2", "metadata": {}, "_distance": 0.5},  # Keep (0.5 <= 0.5)
            {"text": "far match", "file_id": "f3", "metadata": {}, "_distance": 0.8},   # Skip (0.8 > 0.5)
        ]
        filtered = await engine._filter_relevant(results)
        self.assertEqual(2, len(filtered))
        self.assertEqual("close match", filtered[0].text)
        self.assertEqual("at threshold", filtered[1].text)

    async def test_no_fallback_injection_when_all_chunks_filtered(self):
        """Test that when all chunks are filtered by threshold, no garbage fallback is injected."""
        # All results have distance > threshold (should all be filtered)
        vector_results = [
            {"text": "irrelevant1", "file_id": "f1", "metadata": {}, "_distance": 0.9},
            {"text": "irrelevant2", "file_id": "f2", "metadata": {}, "_distance": 0.95},
        ]
        engine = RAGEngine()
        engine.embedding_service = cast(EmbeddingService, FakeEmbeddingService([0.1, 0.2]))
        engine.vector_store = cast(VectorStore, FakeVectorStore(vector_results))
        engine.memory_store = cast(MemoryStore, FakeMemoryStore())
        engine.llm_client = cast(LLMClient, FakeLLMClient(response="answer"))

        results = [msg async for msg in engine.query("query", [], stream=False)]
        done = results[-1]
        self.assertEqual("done", done["type"])
        # Sources should be empty since all chunks were filtered
        self.assertEqual(0, len(done["sources"]))
        # The LLM response should just be "answer" - no fallback injection
        content_msgs = [r for r in results if r.get("type") == "content"]
        self.assertEqual("answer", content_msgs[0]["content"])

    def test_build_system_prompt_contains_knowledgevault_and_cite_sources(self):
        engine = RAGEngine()
        prompt = engine._build_system_prompt()
        self.assertIn("KnowledgeVault", prompt)
        self.assertIn("cite", prompt.lower())

    @pytest.mark.xfail(
        reason="_format_chunk is a stale backward-compat wrapper; "
               "PromptBuilderService.format_chunk now requires source_index",
        strict=True,
    )
    def test_format_chunk_defaults_to_document_when_metadata_missing(self):
        engine = RAGEngine()
        from app.services.rag_engine import RAGSource
        chunk = RAGSource(text="some text", file_id="f1", score=0.8, metadata={})
        formatted = engine._format_chunk(chunk)
        self.assertIn("document", formatted)
        self.assertIn("some text", formatted)

    async def test_rag_engine_vault_isolation(self):
        """FakeVectorStore and engine-level query must filter results by vault_id."""
        vector_results = [
            {"text": "shared topic", "file_id": "f1", "metadata": {}, "score": 0.9, "vault_id": 1},
            {"text": "shared topic", "file_id": "f2", "metadata": {}, "score": 0.8, "vault_id": 1},
            {"text": "shared topic", "file_id": "f3", "metadata": {}, "score": 0.7, "vault_id": 2},
        ]
        vector_store = FakeVectorStore(vector_results)

        vault1_results = await vector_store.search([0.1, 0.2], limit=10, vault_id=1)
        self.assertEqual(2, len(vault1_results))
        self.assertTrue(
            all(str(r.get("vault_id")) == "1" for r in vault1_results),
            "vault_id=1 search returned cross-vault results",
        )

        vault2_results = await vector_store.search([0.1, 0.2], limit=10, vault_id=2)
        self.assertEqual(1, len(vault2_results))
        self.assertEqual("2", str(vault2_results[0].get("vault_id")))
        self.assertTrue(
            all(str(r.get("vault_id")) == "2" for r in vault2_results),
            "vault_id=2 search returned cross-vault results",
        )

        # Engine-level vault isolation: verify engine.query() respects vault_id
        engine = RAGEngine()
        engine.embedding_service = cast(EmbeddingService, FakeEmbeddingService([0.1, 0.2]))
        engine.vector_store = cast(VectorStore, FakeVectorStore(vector_results))
        engine.memory_store = cast(MemoryStore, FakeMemoryStore())
        engine.llm_client = cast(LLMClient, FakeLLMClient(response="answer"))

        from app.config import settings
        with patch.object(settings, 'query_transformation_enabled', False):
            vault1_msgs = [msg async for msg in engine.query("shared topic", [], stream=False, vault_id=1)]

        done1 = [m for m in vault1_msgs if isinstance(m, dict) and m.get("type") == "done"]
        self.assertTrue(len(done1) > 0, "Expected a done message for vault_id=1 query")
        vault1_sources = done1[0].get("sources", [])
        self.assertTrue(
            len(vault1_sources) > 0,
            "Expected sources for vault_id=1 query",
        )
        self.assertTrue(
            all(s.get("file_id") in ("f1", "f2") for s in vault1_sources),
            f"vault_id=1 query returned cross-vault sources: {[s.get('file_id') for s in vault1_sources]}",
        )

        with patch.object(settings, 'query_transformation_enabled', False):
            vault2_msgs = [msg async for msg in engine.query("shared topic", [], stream=False, vault_id=2)]

        done2 = [m for m in vault2_msgs if isinstance(m, dict) and m.get("type") == "done"]
        self.assertTrue(len(done2) > 0, "Expected a done message for vault_id=2 query")
        vault2_sources = done2[0].get("sources", [])
        self.assertTrue(
            len(vault2_sources) > 0,
            "Expected sources for vault_id=2 query",
        )
        self.assertTrue(
            all(s.get("file_id") == "f3" for s in vault2_sources),
            f"vault_id=2 query returned cross-vault sources: {[s.get('file_id') for s in vault2_sources]}",
        )

    async def test_followup_rewrite_modifies_retrieval_query(self):
        """Followup rewrite must change the query used for retrieval."""
        rewritten_query = "What are the security features of the authentication system?"

        class SpyEmbeddingService:
            def __init__(self):
                self.embedded_texts = []

            async def embed_single(self, text):
                self.embedded_texts.append(text)
                return [0.1, 0.2]

            async def embed_passage(self, text):
                self.embedded_texts.append(text)
                return [0.1, 0.2]

        chat_history = [
            {"role": "user", "content": "How does the authentication system work?"},
            {"role": "assistant", "content": "The system uses JWT tokens with RS256 signing..."},
        ]

        engine = RAGEngine()
        spy_embedding = SpyEmbeddingService()
        engine.embedding_service = cast(EmbeddingService, spy_embedding)
        engine.vector_store = cast(VectorStore, FakeVectorStore([
            {"text": "chunk", "file_id": "f1", "metadata": {}, "score": 0.9}
        ]))
        engine.memory_store = cast(MemoryStore, FakeMemoryStore())
        engine.llm_client = cast(LLMClient, FakeLLMClient(response=rewritten_query))

        from app.config import settings
        with patch.object(settings, 'query_transformation_enabled', False):
            results = [msg async for msg in engine.query("tell me more", chat_history, stream=False)]

        self.assertTrue(
            any(rewritten_query in t for t in spy_embedding.embedded_texts),
            f"Expected rewritten query '{rewritten_query}' in embedded texts, got: {spy_embedding.embedded_texts}",
        )
        self.assertFalse(
            any("tell me more" == t for t in spy_embedding.embedded_texts),
            "Original followup text was used for retrieval instead of rewritten query",
        )

    async def test_reranker_exception_falls_back_to_unreranked(self):
        """Reranker exception should fall back to unreranked results with score_type=distance."""
        class FailingReranker:
            async def rerank(self, *args, **kwargs):
                raise RuntimeError("Reranker service unavailable")

        engine = RAGEngine()
        engine.embedding_service = cast(EmbeddingService, FakeEmbeddingService([0.1, 0.2]))
        engine.vector_store = cast(VectorStore, FakeVectorStore([
            {"text": "chunk content", "file_id": "f1", "metadata": {}, "score": 0.9}
        ]))
        engine.memory_store = cast(MemoryStore, FakeMemoryStore())
        engine.llm_client = cast(LLMClient, FakeLLMClient(response="LLM answer"))
        engine.reranking_enabled = True
        engine.reranking_service = cast(Any, FailingReranker())

        from app.config import settings
        with patch.object(settings, 'query_transformation_enabled', False):
            results = [msg async for msg in engine.query("test query", [], stream=False)]

        # Engine should return results despite reranker failure
        self.assertGreater(len(results), 0, "Engine should return results despite reranker exception")

        # The done message must have score_type="distance" (not "rerank")
        done = [r for r in results if isinstance(r, dict) and r.get("type") == "done"]
        self.assertTrue(len(done) > 0, "Expected a done message")
        self.assertEqual("distance", done[0]["score_type"],
                         "Reranker exception should fall back to distance score_type")

        # Original vector results must survive the reranker fallback (not cleared)
        self.assertGreater(
            len(done[0].get("sources", [])), 0,
            "Reranker fallback should preserve original vector results in sources",
        )

    async def test_streaming_llm_error_yields_error_chunk(self):
        """LLM error during streaming should yield an error chunk before the generator completes."""
        engine = RAGEngine()
        engine.embedding_service = cast(EmbeddingService, FakeEmbeddingService([0.1, 0.2]))
        engine.vector_store = cast(VectorStore, FakeVectorStore([
            {"text": "chunk", "file_id": "f1", "metadata": {}, "score": 0.9}
        ]))
        engine.memory_store = cast(MemoryStore, FakeMemoryStore())
        engine.llm_client = cast(
            LLMClient,
            FakeLLMClient(response="", stream_chunks=["part1"], raise_llm_error=True),
        )

        from app.config import settings
        with patch.object(settings, 'query_transformation_enabled', False):
            results = [msg async for msg in engine.query("query", [], stream=True)]

        error_chunks = [
            r for r in results
            if isinstance(r, dict) and r.get("type") == "error"
        ]
        self.assertTrue(
            len(error_chunks) > 0,
            "Expected at least one error chunk in streaming results",
        )
        self.assertEqual("LLM_ERROR", error_chunks[0]["code"])

    async def test_streaming_original_embedding_failure_yields_error_chunk(self):
        """When original query embedding fails with stream=True, yield exactly one error chunk."""

        class FailingEmbeddingService:
            def __init__(self):
                self.call_count = 0

            async def embed_single(self, text):
                self.call_count += 1
                raise EmbeddingError("Original query embed failed")

            async def embed_passage(self, text):
                # HyDE / passage variants should not be called when original fails
                raise EmbeddingError("Unexpected passage embed call")

        engine = RAGEngine()
        engine.embedding_service = cast(EmbeddingService, FailingEmbeddingService())
        engine.vector_store = cast(VectorStore, FakeVectorStore([]))
        engine.memory_store = cast(MemoryStore, FakeMemoryStore())
        engine.llm_client = cast(LLMClient, FakeLLMClient(response=""))

        from app.config import settings
        with patch.object(settings, 'query_transformation_enabled', False):
            results = [msg async for msg in engine.query("test", [], stream=True)]

        error_chunks = [r for r in results if isinstance(r, dict) and r.get("type") == "error"]
        self.assertEqual(1, len(error_chunks), f"Expected exactly 1 error chunk, got: {results}")
        self.assertEqual("EMBEDDING_ERROR", error_chunks[0]["code"])
        self.assertIn("Original query embedding failed", error_chunks[0]["message"])

    async def test_query_require_vault_raises_without_vault_id(self):
        """require_vault=True with vault_id=None must raise ValueError before any async work."""
        engine = RAGEngine()
        engine.embedding_service = cast(EmbeddingService, FakeEmbeddingService([0.1, 0.2]))
        engine.vector_store = cast(VectorStore, FakeVectorStore([]))
        engine.memory_store = cast(MemoryStore, FakeMemoryStore())
        engine.llm_client = cast(LLMClient, FakeLLMClient(response=""))

        gen = engine.query("test", [], require_vault=True, vault_id=None)
        with pytest.raises(ValueError, match="vault_id is required"):
            async for _ in gen:
                pass

    async def test_query_require_vault_allows_with_vault_id(self):
        """require_vault=True with a vault_id must NOT raise the vault guard ValueError."""
        engine = RAGEngine()
        engine.embedding_service = cast(EmbeddingService, FakeEmbeddingService([0.1, 0.2]))
        engine.vector_store = cast(VectorStore, FakeVectorStore([]))
        engine.memory_store = cast(MemoryStore, FakeMemoryStore())
        engine.llm_client = cast(LLMClient, FakeLLMClient(response=""))

        gen = engine.query("test", [], require_vault=True, vault_id=42)
        vault_error_raised = False
        try:
            async for _ in gen:
                pass
        except ValueError as e:
            if "vault_id is required" in str(e):
                vault_error_raised = True
        # Other errors (e.g. missing embedding service) are acceptable;
        # the vault guard itself must NOT have fired.
        self.assertFalse(vault_error_raised, "ValueError about vault_id should not be raised when vault_id is provided")

    async def test_non_streaming_original_embedding_failure_raises(self):
        """When original query embedding fails with stream=False, raise RAGEngineError."""
        class FailingEmbeddingService:
            def __init__(self):
                self.call_count = 0

            async def embed_single(self, text):
                self.call_count += 1
                raise EmbeddingError("Original query embed failed")

            async def embed_passage(self, text):
                raise EmbeddingError("Unexpected passage embed call")

        engine = RAGEngine()
        engine.embedding_service = cast(EmbeddingService, FailingEmbeddingService())
        engine.vector_store = cast(VectorStore, FakeVectorStore([]))
        engine.memory_store = cast(MemoryStore, FakeMemoryStore())
        engine.llm_client = cast(LLMClient, FakeLLMClient(response=""))

        from app.config import settings
        with patch.object(settings, 'query_transformation_enabled', False):
            with self.assertRaises(RAGEngineError) as ctx:
                async def consume():
                    async for _ in engine.query("test", [], stream=False):
                        pass
                await consume()

        self.assertIn("Original query embedding failed", str(ctx.exception))


class TestVariantFailureHandling:
    """Tests for variant failure handling and variants_dropped tracking."""

    @pytest.mark.asyncio
    async def test_original_query_failure_propagates(self):
        """When original query embedding fails, RAGEngineError should propagate."""
        class FailingEmbeddingService:
            def __init__(self):
                self.call_count = 0
            async def embed_single(self, text):
                self.call_count += 1
                raise EmbeddingError("Original query embed failed")
            async def embed_passage(self, text):
                return [0.1]  # Should not be called for original

        class FailingVectorStore:
            def __init__(self):
                self.call_count = 0
            def search(self, embedding, limit=10, **kwargs):
                self.call_count += 1
                return [{"id": "chunk1", "text": "test", "score": 0.9, "metadata": {}}]

        engine = RAGEngine(
            embedding_service=FailingEmbeddingService(),
            vector_store=FailingVectorStore(),
            memory_store=FakeMemoryStore(),
            llm_client=None,
            reranking_service=None,
        )

        # Patch settings to enable transformation
        with patch("app.services.rag_engine.settings") as mock_settings:
            mock_settings.query_transformation_enabled = True
            mock_settings.hyde_enabled = False
            mock_settings.stepback_enabled = False
            mock_settings.agentic_rag_enabled = False
            mock_settings.context_distillation_enabled = False
            mock_settings.context_max_tokens = 6000

            # The query should fail because original embedding fails
            with pytest.raises(RAGEngineError) as exc_info:
                async def run():
                    async for _ in engine.query("test query", [], vault_id=1):
                        pass
                await run()

            assert "Original query embedding failed" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_hyde_failure_adds_to_variants_dropped(self):
        """When HyDE embedding fails, variants_dropped should include 'hyde'."""
        class PartialFailingEmbeddingService:
            def __init__(self):
                self.embed_count = 0
            async def embed_single(self, text):
                return [0.1] * 384  # Return valid embedding
            async def embed_passage(self, text):
                # HyDE fails but original succeeds
                raise EmbeddingError("HyDE embed failed")
            def get_cache_stats(self):
                return {}
            @property
            def embedding_model(self):
                return "test-embedding-model"
            @property
            def embeddings_url(self):
                return "http://test"

        class MockVectorStore:
            def __init__(self):
                self.search_results = [{"id": "chunk1", "text": "test", "score": 0.9, "metadata": {"processed_at": "2024-01-01"}}]
            async def search(self, embedding, limit=10, **kwargs):
                return self.search_results
            def get_fts_exceptions(self):
                return 0

        # LLM that generates valid responses (> 20 chars)
        class MockLLMClient:
            def __init__(self):
                pass
            async def chat_completion(self, messages, **kwargs):
                return "A longer response for the LLM query."
            async def chat_completion_stream(self, messages, **kwargs):
                yield "test"

        engine = RAGEngine(
            embedding_service=PartialFailingEmbeddingService(),
            vector_store=MockVectorStore(),
            memory_store=FakeMemoryStore(),
            llm_client=MockLLMClient(),
        )

        # Patch settings to enable transformation with hyde
        with patch("app.services.rag_engine.settings") as mock_settings:
            mock_settings.query_transformation_enabled = True
            mock_settings.hyde_enabled = True
            mock_settings.stepback_enabled = True  # HyDE requires stepback_enabled
            mock_settings.context_distillation_enabled = False
            mock_settings.context_max_tokens = 6000
            mock_settings.retrieval_top_k = 10
            mock_settings.max_distance_threshold = 0.5
            mock_settings.relevance_threshold = 0.5
            mock_settings.embedding_model = "test-model"
            mock_settings.embedding_url = "http://test"
            mock_settings.ollama_embedding_url = "http://test"
            mock_settings.retrieval_evaluation_enabled = False
            mock_settings.reranking_enabled = False
            mock_settings.hybrid_search_enabled = False
            mock_settings.chunk_size_chars = 8192
            mock_settings.chunk_overlap_chars = 200
            mock_settings.retrieval_window = 0
            mock_settings.vector_top_k = None
            mock_settings.maintenance_mode = False
            mock_settings.retrieval_recency_weight = 0.0
            mock_settings.agentic_rag_enabled = False

            # Patch the QueryTransformer to return specific variants without LLM
            async def mock_transform(query):
                return [
                    ('original', 'test query'),
                    ('step_back', 'broader version of test query'),
                    ('hyde', 'hyde passage text that answers the question')
                ]

            with patch("app.services.rag_engine.QueryTransformer") as MockQueryTransformer:
                instance = MockQueryTransformer.return_value
                instance.transform = mock_transform

                results = [msg async for msg in engine.query("test query", [], stream=False, vault_id=1)]

        # Verify the query succeeded (no exception raised)
        done = [r for r in results if r.get("type") == "done"]
        assert len(done) == 1
        assert "retrieval_debug" in done[0]
        # Assert that variants_dropped contains 'hyde'
        assert "hyde" in done[0]["retrieval_debug"]["variants_dropped"]


if __name__ == "__main__":
    unittest.main()
