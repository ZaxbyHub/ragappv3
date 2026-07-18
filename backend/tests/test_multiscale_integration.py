"""Tests for multi-scale RRF fusion with recency scoring in rag_engine.py."""

import os
import sys
import unittest
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, cast
from unittest.mock import patch

import pytest

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

from app.config import settings
from app.services.embeddings import EmbeddingService
from app.services.llm_client import LLMClient
from app.services.memory_store import MemoryRecord, MemoryStore
from app.services.rag_engine import RAGEngine
from app.services.vector_store import VectorStore


@pytest.fixture(autouse=True)
def _patch_ssrf():
    with patch("app.services.embeddings.assert_url_safe"), \
         patch("app.services.llm_client.assert_url_safe"):
        yield


class FakeEmbeddingService:
    """Fake embedding service that returns a fixed embedding."""

    def __init__(self, embedding: List[float]):
        self.embedding = embedding

    async def embed_single(self, text: str) -> List[float]:
        return self.embedding


class FakeVectorStore:
    """Fake vector store that returns configurable results."""

    def __init__(self, results: List[Dict]):
        self._results = results
        self._fts_exceptions = 0
        self.search_call_count = 0

    def get_fts_exceptions(self) -> int:
        return self._fts_exceptions

    async def search(
        self,
        embedding: List[float],
        limit: int = 10,
        filter_expr=None,
        vault_id=None,
        query_text: str = "",
        hybrid: bool = False,
        hybrid_alpha: float = 0.5,
    ) -> List[Dict[str, Any]]:
        self.search_call_count += 1
        return self._results[:limit]


class FakeMemoryStore:
    """Fake memory store."""

    def __init__(
        self,
        intent: Optional[str] = None,
        memories: Optional[List[MemoryRecord]] = None,
    ):
        self.intent = intent
        self._memories = memories or []

    def detect_memory_intent(self, text: str):
        return self.intent

    def add_memory(
        self, content: str, category=None, tags=None, source=None, vault_id=None
    ):
        return MemoryRecord(
            id=1,
            content=content,
            category=category,
            tags=tags,
            source=source,
            created_at=None,
            updated_at=None,
        )

    def search_memories(self, query: str, limit: int = 5, vault_id=None, include_global: bool = False):
        return self._memories[:limit]


class FakeLLMClient:
    """Fake LLM client that returns a fixed response."""

    def __init__(self, response: str = "test response"):
        self._response = response

    async def chat_completion(self, messages, max_tokens=None):
        return self._response

    async def chat_completion_stream(self, messages):
        yield {"type": "content", "content": "chunk"}


class TestRecencyFusionIntegration(unittest.IsolatedAsyncioTestCase):
    """Tests for recency-weighted RRF fusion in _execute_retrieval."""

    def _create_engine_with_results(
        self,
        result_lists: List[List[Dict[str, Any]]],
    ) -> RAGEngine:
        """Create a RAGEngine with mocked vector store returning the given results."""
        engine = RAGEngine()
        # Inject fake services
        engine.embedding_service = cast(
            EmbeddingService, FakeEmbeddingService([0.1] * 384)
        )
        engine.vector_store = cast(VectorStore, FakeVectorStore([]))
        engine.memory_store = cast(MemoryStore, FakeMemoryStore())
        engine.llm_client = cast(LLMClient, FakeLLMClient())

        # Mock vector_store.search to return different results for each call
        call_index = [0]

        async def mock_search(*args, **kwargs):
            idx = call_index[0]
            call_index[0] += 1
            if idx < len(result_lists):
                return result_lists[idx]
            return []

        engine.vector_store.search = mock_search

        return engine

    async def test_recency_scoring_computed_when_weight_positive_and_multiple_queries(
        self,
    ):
        """Recency scoring is computed when retrieval_recency_weight > 0 and multiple result lists."""
        # Create results with different timestamps - needs 2+ query embeddings to get len(all_results) > 1
        now = datetime.now()
        old_time = now - timedelta(days=30)
        recent_time = now

        result_lists = [
            [
                {
                    "id": "doc1",
                    "text": "old doc",
                    "metadata": {},
                    "processed_at": old_time.isoformat(),
                },
                {
                    "id": "doc2",
                    "text": "recent doc",
                    "metadata": {},
                    "processed_at": recent_time.isoformat(),
                },
            ],
            [
                {
                    "id": "doc2",
                    "text": "recent doc",
                    "metadata": {},
                    "processed_at": recent_time.isoformat(),
                },
                {
                    "id": "doc3",
                    "text": "middle doc",
                    "metadata": {},
                    "processed_at": (now - timedelta(days=15)).isoformat(),
                },
            ],
        ]

        engine = self._create_engine_with_results(result_lists)

        # Patch settings for recency
        with patch.object(settings, "retrieval_recency_weight", 0.5):
            # Pass 2 query embeddings to get 2 result lists (len(all_results) = 2)
            results, _, _, _, _, _, _, _, _, exact_match_promoted, token_pack_stats = await engine._execute_retrieval(
                [[0.1] * 384, [0.1] * 384],  # 2 embeddings = 2 result lists
                "test query",
                vault_id=1,
            )

            # Results should be fused using RRF with recency
            self.assertGreater(len(results), 0)
            # doc2 appears in both lists and is most recent, should rank higher
            ids = [r["id"] for r in results]
            # doc2 should be first (appears in both + recency boost)
            self.assertEqual(ids[0], "doc2")

    async def test_recency_scoring_skipped_when_weight_zero(self):
        """Recency scoring is skipped when retrieval_recency_weight = 0."""
        now = datetime.now()
        result_lists = [
            [
                {
                    "id": "doc1",
                    "text": "old doc",
                    "metadata": {},
                    "processed_at": (now - timedelta(days=30)).isoformat(),
                },
                {
                    "id": "doc2",
                    "text": "recent doc",
                    "metadata": {},
                    "processed_at": now.isoformat(),
                },
            ],
            [
                {
                    "id": "doc2",
                    "text": "recent doc",
                    "metadata": {},
                    "processed_at": now.isoformat(),
                },
            ],
        ]

        engine = self._create_engine_with_results(result_lists)

        # Patch settings to use weight=0
        with patch.object(settings, "retrieval_recency_weight", 0.0):
            results, _, _, _, _, _, _, _, _, exact_match_promoted, token_pack_stats = await engine._execute_retrieval(
                [[0.1] * 384, [0.1] * 384],
                "test query",
                vault_id=1,
            )

            # With weight=0, results should be sorted by RRF only (doc appears in 2 lists wins)
            self.assertGreater(len(results), 0)
            ids = [r["id"] for r in results]
            # doc2 appears in both lists, should be first
            self.assertEqual(ids[0], "doc2")

    async def test_recency_scoring_skipped_single_query_result_list(self):
        """Recency scoring is skipped when only 1 result list (single query)."""
        now = datetime.now()
        result_lists = [
            [
                {
                    "id": "doc1",
                    "text": "old doc",
                    "metadata": {},
                    "processed_at": (now - timedelta(days=30)).isoformat(),
                },
                {
                    "id": "doc2",
                    "text": "recent doc",
                    "metadata": {},
                    "processed_at": now.isoformat(),
                },
            ],
        ]

        # With weight > 0 but only 1 result list, recency should be skipped
        engine = self._create_engine_with_results(result_lists)
        with patch.object(settings, "retrieval_recency_weight", 0.5):
            results, _, _, _, _, _, _, _, _, exact_match_promoted, token_pack_stats = await engine._execute_retrieval(
                [[0.1] * 384],  # single embedding = single result list
                "test query",
                vault_id=1,
            )

        # Should return results without recency fusion (single list passthrough)
        self.assertEqual(len(results), 2)

    async def test_recency_scoring_skipped_no_processed_at_metadata(self):
        """Recency scoring is skipped when no docs have processed_at metadata."""
        result_lists = [
            [
                {"id": "doc1", "text": "doc without date", "metadata": {}},
                {"id": "doc2", "text": "another doc", "metadata": {}},
            ],
            [
                {"id": "doc2", "text": "another doc", "metadata": {}},
                {"id": "doc3", "text": "third doc", "metadata": {}},
            ],
        ]

        engine = self._create_engine_with_results(result_lists)
        with patch.object(settings, "retrieval_recency_weight", 0.5):
            results, _, _, _, _, _, _, _, _, exact_match_promoted, token_pack_stats = await engine._execute_retrieval(
                [[0.1] * 384, [0.1] * 384],  # 2 embeddings = 2 result lists
                "test query",
                vault_id=1,
            )

        # Should return results but without recency boosting (dates dict empty -> no recency_scores)
        self.assertGreater(len(results), 0)

    async def test_recency_formula_oldest_zero_newest_one(self):
        """Recency formula: oldest=0.0, newest=1.0, middle=proportional."""
        # For recency fusion, we need 2+ result lists
        now = datetime.now()
        old_time = now - timedelta(days=100)
        middle_time = now - timedelta(days=50)
        new_time = now

        result_lists = [
            [
                {
                    "id": "oldest",
                    "text": "old",
                    "metadata": {},
                    "processed_at": old_time.isoformat(),
                },
                {
                    "id": "middle",
                    "text": "mid",
                    "metadata": {},
                    "processed_at": middle_time.isoformat(),
                },
                {
                    "id": "newest",
                    "text": "new",
                    "metadata": {},
                    "processed_at": new_time.isoformat(),
                },
            ],
            [
                {
                    "id": "newest",
                    "text": "new",
                    "metadata": {},
                    "processed_at": new_time.isoformat(),
                },
                {
                    "id": "middle",
                    "text": "mid",
                    "metadata": {},
                    "processed_at": middle_time.isoformat(),
                },
            ],
        ]

        engine = self._create_engine_with_results(result_lists)
        with patch.object(settings, "retrieval_recency_weight", 1.0):
            results, _, _, _, _, _, _, _, _, exact_match_promoted, token_pack_stats = await engine._execute_retrieval(
                [[0.1] * 384, [0.1] * 384],  # 2 embeddings for fusion
                "test query",
                vault_id=1,
            )

        # With weight=1.0, ranking should be purely by recency
        # newest should be first, oldest last
        ids = [r["id"] for r in results]
        self.assertEqual(ids[0], "newest")
        self.assertEqual(ids[-1], "oldest")

    async def test_recency_invalid_datetime_handled_gracefully(self):
        """Invalid datetime in processed_at is skipped gracefully."""
        result_lists = [
            [
                {
                    "id": "valid1",
                    "text": "valid1",
                    "metadata": {},
                    "processed_at": "2024-01-01T00:00:00",
                },
                {
                    "id": "invalid",
                    "text": "invalid",
                    "metadata": {},
                    "processed_at": "not-a-valid-date",
                },
                {
                    "id": "valid2",
                    "text": "valid2",
                    "metadata": {},
                    "processed_at": "2024-12-31T00:00:00",
                },
            ],
            [
                {
                    "id": "valid2",
                    "text": "valid2",
                    "metadata": {},
                    "processed_at": "2024-12-31T00:00:00",
                },
                {
                    "id": "valid1",
                    "text": "valid1",
                    "metadata": {},
                    "processed_at": "2024-01-01T00:00:00",
                },
            ],
        ]

        engine = self._create_engine_with_results(result_lists)

        # Should not raise an exception
        with patch.object(settings, "retrieval_recency_weight", 0.5):
            results, _, _, _, _, _, _, _, _, exact_match_promoted, token_pack_stats = await engine._execute_retrieval(
                [[0.1] * 384, [0.1] * 384],
                "test query",
                vault_id=1,
            )

        # Should return valid results, skipping invalid date doc
        self.assertGreater(len(results), 0)

    async def test_recency_scores_passed_to_rrf_fuse(self):
        """Normalized recency scores are passed to rrf_fuse function."""
        now = datetime.now()

        result_lists = [
            [
                {
                    "id": "doc1",
                    "text": "old",
                    "metadata": {},
                    "processed_at": (now - timedelta(days=10)).isoformat(),
                },
            ],
            [
                {
                    "id": "doc2",
                    "text": "new",
                    "metadata": {},
                    "processed_at": now.isoformat(),
                },
            ],
        ]

        # Patch rrf_fuse in the rag_engine module where it's imported
        from app.services import rag_engine

        original_rrf_fuse = rag_engine.rrf_fuse
        rrf_fuse_calls = []

        def mock_rrf_fuse(*args, **kwargs):
            rrf_fuse_calls.append(kwargs)
            return original_rrf_fuse(*args, **kwargs)

        with patch.object(rag_engine, "rrf_fuse", mock_rrf_fuse):
            with patch.object(settings, "retrieval_recency_weight", 0.5):
                engine = self._create_engine_with_results(result_lists)
                await engine._execute_retrieval(
                    [[0.1] * 384, [0.1] * 384],
                    "test query",
                    vault_id=1,
                )

        # Verify rrf_fuse was called with recency_scores
        self.assertGreater(len(rrf_fuse_calls), 0)
        last_call = rrf_fuse_calls[-1]
        self.assertIn("recency_scores", last_call)
        self.assertIn("recency_weight", last_call)

        # Verify recency_weight matches setting
        self.assertEqual(last_call["recency_weight"], 0.5)

    async def test_recency_with_processed_at_in_metadata_field(self):
        """Recency scoring works when processed_at is in metadata field."""
        now = datetime.now()

        result_lists = [
            [
                {
                    "id": "doc1",
                    "text": "old doc",
                    "metadata": {
                        "processed_at": (now - timedelta(days=30)).isoformat()
                    },
                },
                {
                    "id": "doc2",
                    "text": "new doc",
                    "metadata": {"processed_at": now.isoformat()},
                },
            ],
            [
                {
                    "id": "doc2",
                    "text": "new doc",
                    "metadata": {"processed_at": now.isoformat()},
                },
            ],
        ]

        engine = self._create_engine_with_results(result_lists)
        with patch.object(settings, "retrieval_recency_weight", 0.5):
            results, _, _, _, _, _, _, _, _, exact_match_promoted, token_pack_stats = await engine._execute_retrieval(
                [[0.1] * 384, [0.1] * 384],
                "test query",
                vault_id=1,
            )

        # Should return results - doc2 should be first due to recency
        self.assertGreater(len(results), 0)
        ids = [r["id"] for r in results]
        self.assertEqual(ids[0], "doc2")

    async def test_recency_single_doc_in_dates_dict_skipped(self):
        """Recency scoring skipped when only 1 doc has valid processed_at."""
        now = datetime.now()

        result_lists = [
            [
                {
                    "id": "doc1",
                    "text": "has date",
                    "metadata": {},
                    "processed_at": now.isoformat(),
                },
                {"id": "doc2", "text": "no date", "metadata": {}},
            ],
            [
                {"id": "doc2", "text": "no date", "metadata": {}},
                {"id": "doc3", "text": "also no date", "metadata": {}},
            ],
        ]

        engine = self._create_engine_with_results(result_lists)

        # Should not raise - recency should be skipped when only 1 valid date
        with patch.object(settings, "retrieval_recency_weight", 0.5):
            results, _, _, _, _, _, _, _, _, exact_match_promoted, token_pack_stats = await engine._execute_retrieval(
                [[0.1] * 384, [0.1] * 384],
                "test query",
                vault_id=1,
            )

        # Should return fused results without recency boosting (dates has only 1 item)
        self.assertGreater(len(results), 0)

    async def test_recency_span_handles_zero_difference(self):
        """When min_ts == max_ts, span defaults to 1.0 to avoid division by zero."""
        # Same timestamp for all docs - need 2 lists for fusion
        now = datetime.now()

        result_lists = [
            [
                {
                    "id": "doc1",
                    "text": "doc1",
                    "metadata": {},
                    "processed_at": now.isoformat(),
                },
                {
                    "id": "doc2",
                    "text": "doc2",
                    "metadata": {},
                    "processed_at": now.isoformat(),
                },
            ],
            [
                {
                    "id": "doc2",
                    "text": "doc2",
                    "metadata": {},
                    "processed_at": now.isoformat(),
                },
            ],
        ]

        engine = self._create_engine_with_results(result_lists)

        # Should not raise - span=1.0 handles zero difference
        with patch.object(settings, "retrieval_recency_weight", 0.5):
            results, _, _, _, _, _, _, _, _, exact_match_promoted, token_pack_stats = await engine._execute_retrieval(
                [[0.1] * 384, [0.1] * 384],
                "test query",
                vault_id=1,
            )

        self.assertGreater(len(results), 0)


if __name__ == "__main__":
    unittest.main()
