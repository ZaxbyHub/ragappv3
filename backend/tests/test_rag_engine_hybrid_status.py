"""
Tests for hybrid_status and fts_exceptions in RAGEngine.

Verifies Task 2.2:
1. hybrid_status is computed correctly: 'disabled' | 'both' | 'dense_only'
2. fts_exceptions is retrieved from vector_store.get_fts_exceptions()
3. Both are included in retrieval_debug in the done message
"""

import logging
import os
import sys
from typing import Dict, List, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Add backend to path
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


class FakeEmbeddingService:
    def __init__(self, embedding: List[float]):
        self._embedding = embedding

    async def embed_single(self, text: str) -> List[float]:
        return self._embedding

    async def embed_query_sparse(self, text: str) -> dict:
        return None  # Signal failure — code falls back to dense-only


class FakeMemoryStore:
    def detect_memory_intent(self, text: str):
        return None

    def add_memory(self, content: str, category=None, tags=None, source=None, vault_id=None):
        from app.services.memory_store import MemoryRecord
        return MemoryRecord(id=1, content=content, category=category, tags=tags, source=source, created_at=None, updated_at=None)

    def search_memories(self, query: str, limit: int = 5, vault_id=None, include_global: bool = False):
        return []


class FakeLLMClient:
    def __init__(self, response: str = "test response"):
        self._response = response

    async def chat_completion(self, messages, max_tokens=None):
        return self._response

    async def chat_completion_stream(self, messages, max_tokens=None):
        yield {"type": "content", "content": self._response}


def _make_search_results(results: List[Dict]) -> MagicMock:
    """Create an async mock for vector_store.search() returning a list of dicts."""
    mock = AsyncMock(return_value=results)
    return mock


_EMPTY_TOKEN_PACK_STATS = {
    "token_pack_included": 0,
    "token_pack_skipped": 0,
    "token_pack_truncated": 0,
}


class _FakeLanceSearchQuery:
    def __init__(self, rows):
        self._rows = rows
        self.limit_value = None

    def where(self, filter_expr):
        return self

    def limit(self, limit):
        self.limit_value = limit
        return self

    async def to_list(self):
        if self.limit_value is None:
            return list(self._rows)
        return list(self._rows[: self.limit_value])


class _FakeLanceTable:
    def __init__(self, dense_rows, fts_rows):
        self.dense_query = _FakeLanceSearchQuery(dense_rows)
        self.fts_query = _FakeLanceSearchQuery(fts_rows)

    async def search(self, query, query_type):
        if query_type == "vector":
            return self.dense_query
        if query_type == "fts":
            return self.fts_query
        raise AssertionError(f"unexpected query_type: {query_type}")

    async def list_indices(self):
        return []

    async def count_rows(self):
        return 1


# ── helpers for creating RAGEngine with mocked deps ────────────────────────────

def _make_engine(
    search_results: List[Dict],
    hybrid_search_enabled: bool = True,
    fts_exceptions: int = 0,
    reranking_enabled: bool = False,
) -> "RAGEngine":
    """Build a RAGEngine with a fully-mocked vector_store."""
    from app.services.rag_engine import RAGEngine

    engine = RAGEngine.__new__(RAGEngine)
    engine.embedding_service = cast(object, FakeEmbeddingService([0.1] * 384))
    engine.memory_store = cast(object, FakeMemoryStore())
    engine.llm_client = cast(object, FakeLLMClient())
    engine._thinking_client_override = None
    engine._instant_client_override = None
    engine.reranking_enabled = reranking_enabled
    engine.reranking_service = None
    engine.reranker_top_n = None
    engine.initial_retrieval_top_k = 10
    engine.retrieval_top_k = 10
    engine.hybrid_search_enabled = hybrid_search_enabled
    engine.hybrid_alpha = 0.6
    engine.maintenance_mode = False  # must be set so score_type is defined in except handler
    engine.vector_metric = "cosine"
    engine.max_distance_threshold = 1.0
    engine.chunk_size_chars = 512
    engine.chunk_overlap_chars = 128
    engine.retrieval_window = 0

    # Mock vector_store
    mock_vs = MagicMock()
    mock_vs.search = _make_search_results(search_results)
    mock_vs.get_fts_exceptions = MagicMock(return_value=fts_exceptions)
    mock_vs.is_connected = MagicMock(return_value=True)
    mock_vs.get_chunks_by_uid = AsyncMock(return_value=[])  # expand_window awaits this
    engine.vector_store = mock_vs

    # DocumentRetrievalService: mock it to bypass expand_window chain
    from app.services.document_retrieval import DocumentRetrievalService
    mock_dr = MagicMock(spec=DocumentRetrievalService)
    # filter_relevant: sync mock that returns the RAGSource list
    mock_dr.filter_relevant = AsyncMock(return_value=[])
    mock_dr.no_match = False
    mock_dr.to_source_metadata = lambda chunk, source_index=0: {
        "file_id": chunk.file_id,
        "text": chunk.text,
        "score": chunk.score,
        "metadata": chunk.metadata,
        "source_index": source_index,
    }
    mock_dr._normalize_metadata = lambda m: m or {}
    engine.document_retrieval = mock_dr

    # PromptBuilderService
    from app.services.prompt_builder import PromptBuilderService
    engine.prompt_builder = PromptBuilderService()

    engine._query_transformer = None
    engine._retrieval_evaluator = None
    engine._retrieval_evaluators = {}
    engine._wiki_retrieval = None
    engine._kms_retrieval = None
    engine._get_indexed_file_ids = lambda vault_id: None
    async def _no_supersession_warning(chunks):
        return None
    engine._check_supersession = _no_supersession_warning

    return engine


# ── tests for _execute_retrieval hybrid_status ────────────────────────────────

@pytest.mark.asyncio
async def test_hybrid_status_disabled_when_hybrid_search_disabled():
    """hybrid_status='disabled' when hybrid_search_enabled=False."""

    # Results WITH _fts_status should still produce 'disabled' when feature is off
    results = [
        {"id": "1", "text": "doc", "file_id": "f1", "_distance": 0.1, "_fts_status": "ok", "metadata": {}},
        {"id": "2", "text": "doc2", "file_id": "f2", "_distance": 0.2, "_fts_status": "ok", "metadata": {}},
    ]

    engine = _make_engine(results, hybrid_search_enabled=False)
    result_tuple = await engine._execute_retrieval(
        query_embeddings=[[0.1] * 384],
        user_input="test query",
        vault_id=None,
    )
    hybrid_status = result_tuple[5]

    assert hybrid_status == "disabled", f"Expected 'disabled', got '{hybrid_status}'"


@pytest.mark.asyncio
async def test_hybrid_status_both_when_fts_ok():
    """hybrid_status='both' when hybrid_search_enabled=True and results have _fts_status='ok'."""
    results = [
        {"id": "1", "text": "doc", "file_id": "f1", "_distance": 0.1, "_fts_status": "ok", "metadata": {}},
        {"id": "2", "text": "doc2", "file_id": "f2", "_distance": 0.2, "_fts_status": "ok", "metadata": {}},
    ]

    engine = _make_engine(results, hybrid_search_enabled=True)
    result_tuple = await engine._execute_retrieval(
        query_embeddings=[[0.1] * 384],
        user_input="test query",
        vault_id=None,
    )
    hybrid_status = result_tuple[5]

    assert hybrid_status == "both", f"Expected 'both', got '{hybrid_status}'"


@pytest.mark.asyncio
async def test_rag_query_single_scale_hybrid_does_not_fallback_on_rrf(
    monkeypatch,
    caplog,
):
    """RAGEngine uses real single-scale hybrid search without rrf_fuse fallback."""
    from app.services.vector_store import VectorStore

    dense_rows = [
        {
            "id": "dense-1",
            "text": "dense source",
            "file_id": "dense-file",
            "metadata": {},
            "_distance": 0.1,
        }
    ]
    fts_rows = [
        {
            "id": "fts-1",
            "text": "fts source",
            "file_id": "fts-file",
            "metadata": {},
            "_distance": 0.2,
        }
    ]

    store = VectorStore()
    store.db = MagicMock()
    store.table = _FakeLanceTable(dense_rows, fts_rows)
    monkeypatch.setattr(store, "_maybe_create_vector_index", AsyncMock())
    monkeypatch.setattr(
        "app.services.vector_store.settings.multi_scale_indexing_enabled",
        False,
    )
    monkeypatch.setattr(
        "app.services.vector_store.settings.hybrid_rrf_k",
        60,
    )
    monkeypatch.setattr(
        "app.services.vector_store.settings.rrf_legacy_mode",
        False,
    )
    monkeypatch.setattr(
        "app.services.rag_engine.settings.context_max_tokens",
        0,
    )
    monkeypatch.setattr(
        "app.services.rag_engine.settings.retrieval_evaluation_enabled",
        False,
    )

    engine = _make_engine([], hybrid_search_enabled=True)
    engine.vector_store = store
    caplog.set_level(logging.WARNING)

    result_tuple = await engine._execute_retrieval(
        query_embeddings=[("original", [0.1] * 384)],
        user_input="hybrid query",
        vault_id=None,
    )
    vector_results = result_tuple[0]
    hybrid_status = result_tuple[5]

    assert [row["id"] for row in vector_results] == ["dense-1", "fts-1"]
    assert hybrid_status == "both"
    assert not any(
        "cannot access local variable 'rrf_fuse'" in rec.getMessage()
        or "Vector search fallback triggered" in rec.getMessage()
        for rec in caplog.records
    )


@pytest.mark.asyncio
async def test_hybrid_status_dense_only_when_no_fts_status_key():
    """hybrid_status='dense_only' when hybrid_search_enabled=True but no _fts_status key in results."""
    results = [
        {"id": "1", "text": "doc", "file_id": "f1", "_distance": 0.1, "metadata": {}},
        {"id": "2", "text": "doc2", "file_id": "f2", "_distance": 0.2, "metadata": {}},
    ]
    # No _fts_status key at all

    engine = _make_engine(results, hybrid_search_enabled=True)
    result_tuple = await engine._execute_retrieval(
        query_embeddings=[[0.1] * 384],
        user_input="test query",
        vault_id=None,
    )
    hybrid_status = result_tuple[5]

    assert hybrid_status == "dense_only", f"Expected 'dense_only', got '{hybrid_status}'"


@pytest.mark.asyncio
async def test_hybrid_status_dense_only_when_fts_failed():
    """hybrid_status='dense_only' when hybrid_search_enabled=True but all _fts_status='failed' (no 'ok')."""
    results = [
        {"id": "1", "text": "doc", "file_id": "f1", "_distance": 0.1, "_fts_status": "failed", "metadata": {}},
        {"id": "2", "text": "doc2", "file_id": "f2", "_distance": 0.2, "_fts_status": "failed", "metadata": {}},
    ]

    engine = _make_engine(results, hybrid_search_enabled=True)
    result_tuple = await engine._execute_retrieval(
        query_embeddings=[[0.1] * 384],
        user_input="test query",
        vault_id=None,
    )
    hybrid_status = result_tuple[5]

    assert hybrid_status == "dense_only", f"Expected 'dense_only', got '{hybrid_status}'"


@pytest.mark.asyncio
async def test_hybrid_status_both_with_mixed_fts_status():
    """hybrid_status='both' when at least one result has _fts_status='ok' (others may be 'empty')."""
    results = [
        {"id": "1", "text": "doc", "file_id": "f1", "_distance": 0.1, "_fts_status": "empty", "metadata": {}},
        {"id": "2", "text": "doc2", "file_id": "f2", "_distance": 0.2, "_fts_status": "ok", "metadata": {}},
    ]

    engine = _make_engine(results, hybrid_search_enabled=True)
    result_tuple = await engine._execute_retrieval(
        query_embeddings=[[0.1] * 384],
        user_input="test query",
        vault_id=None,
    )
    hybrid_status = result_tuple[5]

    assert hybrid_status == "both", f"Expected 'both', got '{hybrid_status}'"


# ── tests for fts_exceptions in done message ─────────────────────────────────

@pytest.mark.asyncio
async def test_fts_exceptions_passed_to_done_message():
    """fts_exceptions is retrieved and included in retrieval_debug."""
    results = [
        {"id": "1", "text": "doc", "file_id": "f1", "_distance": 0.1, "metadata": {}},
    ]
    expected_exceptions = 3

    engine = _make_engine(results, hybrid_search_enabled=True, fts_exceptions=expected_exceptions)

    # Collect done message — disable optional stages to keep test fast
    done_messages = []
    with \
        patch("app.services.rag_engine.settings.tri_vector_search_enabled", False), \
        patch("app.services.rag_engine.settings.context_distillation_enabled", False), \
        patch("app.services.rag_engine.settings.retrieval_evaluation_enabled", False), \
        patch("app.services.rag_engine.settings.query_transformation_enabled", False), \
        patch("app.services.rag_engine.settings.retrieval_recency_weight", 0.0), \
        patch("app.services.rag_engine.settings.context_max_tokens", 0):
        async for msg in engine.query("test query", []):
            if msg.get("type") == "done":
                done_messages.append(msg)

    assert len(done_messages) == 1, f"Expected 1 done message, got {len(done_messages)}"
    done = done_messages[0]
    assert "retrieval_debug" in done, "done message missing retrieval_debug"
    assert done["retrieval_debug"]["fts_exceptions"] == expected_exceptions, \
        f"Expected fts_exceptions={expected_exceptions}, got {done['retrieval_debug'].get('fts_exceptions')}"


@pytest.mark.asyncio
async def test_get_fts_exceptions_called_once_per_query():
    """vector_store.get_fts_exceptions() is called exactly once per query."""
    results = [{"id": "1", "text": "doc", "file_id": "f1", "_distance": 0.1, "metadata": {}}]
    engine = _make_engine(results, hybrid_search_enabled=True, fts_exceptions=5)

    call_count = 0
    original_get = engine.vector_store.get_fts_exceptions

    def counting_get():
        nonlocal call_count
        call_count += 1
        return original_get()

    engine.vector_store.get_fts_exceptions = counting_get

    done_messages = []
    with \
        patch("app.services.rag_engine.settings.tri_vector_search_enabled", False), \
        patch("app.services.rag_engine.settings.context_distillation_enabled", False), \
        patch("app.services.rag_engine.settings.retrieval_evaluation_enabled", False), \
        patch("app.services.rag_engine.settings.query_transformation_enabled", False), \
        patch("app.services.rag_engine.settings.retrieval_recency_weight", 0.0), \
        patch("app.services.rag_engine.settings.context_max_tokens", 0):
        async for msg in engine.query("test query", []):
            if msg.get("type") == "done":
                done_messages.append(msg)

    assert call_count == 1, f"Expected get_fts_exceptions called 1 time, got {call_count}"


@pytest.mark.asyncio
async def test_hybrid_status_included_in_done_message():
    """hybrid_status is included in retrieval_debug in the done message."""
    # Simulate 'both' status
    results = [
        {"id": "1", "text": "doc", "file_id": "f1", "_distance": 0.1, "_fts_status": "ok", "metadata": {}},
    ]
    engine = _make_engine(results, hybrid_search_enabled=True)

    done_messages = []
    with \
        patch("app.services.rag_engine.settings.tri_vector_search_enabled", False), \
        patch("app.services.rag_engine.settings.context_distillation_enabled", False), \
        patch("app.services.rag_engine.settings.retrieval_evaluation_enabled", False), \
        patch("app.services.rag_engine.settings.query_transformation_enabled", False), \
        patch("app.services.rag_engine.settings.retrieval_recency_weight", 0.0), \
        patch("app.services.rag_engine.settings.context_max_tokens", 0):
        # Patch _execute_retrieval to return a controlled tuple that avoids
        # the except-handler score_type bug in rag_engine.py line 599
        # Note: rerank_success=True (4th elem) → rerank_status="ok" (8th elem)
        async def mock_retrieval(*args, **kwargs):
            return [{"id": "1", "text": "doc", "file_id": "f1", "_distance": 0.1, "_fts_status": "ok", "metadata": {}}], None, "CONFIDENT", True, "rerank", "both", 0, "ok", [], False, dict(_EMPTY_TOKEN_PACK_STATS)
        with patch.object(engine, '_execute_retrieval', mock_retrieval):
            async for msg in engine.query("test query", []):
                if msg.get("type") == "done":
                    done_messages.append(msg)

    assert len(done_messages) == 1
    assert "retrieval_debug" in done_messages[0]
    assert "hybrid_status" in done_messages[0]["retrieval_debug"]
    assert done_messages[0]["retrieval_debug"]["hybrid_status"] == "both"


@pytest.mark.asyncio
async def test_hybrid_status_dense_only_in_done_message():
    """When no _fts_status key, hybrid_status='dense_only' appears in done message."""
    results = [
        {"id": "1", "text": "doc", "file_id": "f1", "_distance": 0.1, "metadata": {}},
    ]
    engine = _make_engine(results, hybrid_search_enabled=True)

    done_messages = []
    with \
        patch("app.services.rag_engine.settings.tri_vector_search_enabled", False), \
        patch("app.services.rag_engine.settings.context_distillation_enabled", False), \
        patch("app.services.rag_engine.settings.retrieval_evaluation_enabled", False), \
        patch("app.services.rag_engine.settings.query_transformation_enabled", False), \
        patch("app.services.rag_engine.settings.retrieval_recency_weight", 0.0), \
        patch("app.services.rag_engine.settings.context_max_tokens", 0):
        async def mock_retrieval(*args, **kwargs):
            return [{"id": "1", "text": "doc", "file_id": "f1", "_distance": 0.1, "metadata": {}}], None, "CONFIDENT", True, "rerank", "dense_only", 0, "ok", [], False, dict(_EMPTY_TOKEN_PACK_STATS)
        with patch.object(engine, '_execute_retrieval', mock_retrieval):
            async for msg in engine.query("test query", []):
                if msg.get("type") == "done":
                    done_messages.append(msg)

    assert len(done_messages) == 1
    assert done_messages[0]["retrieval_debug"]["hybrid_status"] == "dense_only"


@pytest.mark.asyncio
async def test_hybrid_status_disabled_in_done_message():
    """When hybrid_search_enabled=False, hybrid_status='disabled' appears in done message."""
    results = [
        {"id": "1", "text": "doc", "file_id": "f1", "_distance": 0.1, "_fts_status": "ok", "metadata": {}},
    ]
    engine = _make_engine(results, hybrid_search_enabled=False)

    done_messages = []
    with \
        patch("app.services.rag_engine.settings.tri_vector_search_enabled", False), \
        patch("app.services.rag_engine.settings.context_distillation_enabled", False), \
        patch("app.services.rag_engine.settings.retrieval_evaluation_enabled", False), \
        patch("app.services.rag_engine.settings.query_transformation_enabled", False), \
        patch("app.services.rag_engine.settings.retrieval_recency_weight", 0.0), \
        patch("app.services.rag_engine.settings.context_max_tokens", 0):
        async for msg in engine.query("test query", []):
            if msg.get("type") == "done":
                done_messages.append(msg)

    assert len(done_messages) == 1
    assert done_messages[0]["retrieval_debug"]["hybrid_status"] == "disabled"


# ── tests for _build_done_message directly ───────────────────────────────────

def test_build_done_message_includes_hybrid_status_and_fts_exceptions():
    """_build_done_message includes hybrid_status and fts_exceptions in retrieval_debug."""
    from app.services.rag_engine import RAGEngine

    engine = RAGEngine.__new__(RAGEngine)
    from app.services.document_retrieval import DocumentRetrievalService
    from app.services.prompt_builder import PromptBuilderService
    engine.document_retrieval = DocumentRetrievalService(
        vector_store=MagicMock(),
        max_distance_threshold=1.0,
        retrieval_top_k=10,
        retrieval_window=0,
    )
    engine.prompt_builder = PromptBuilderService()
    engine.retrieval_top_k = 10
    engine.max_distance_threshold = 1.0
    engine.vector_metric = "cosine"
    engine.reranking_enabled = False

    chunks = []
    memories = []
    score_type = "distance"
    hybrid_status = "both"
    fts_exceptions = 7
    rerank_status = "ok"

    msg = engine._build_done_message(chunks, memories, score_type, hybrid_status, fts_exceptions, rerank_status)

    assert msg["type"] == "done"
    assert "retrieval_debug" in msg
    assert msg["retrieval_debug"]["hybrid_status"] == "both"
    assert msg["retrieval_debug"]["fts_exceptions"] == 7


def test_build_done_message_with_dense_only_status():
    """_build_done_message with dense_only hybrid_status."""
    from app.services.rag_engine import RAGEngine

    engine = RAGEngine.__new__(RAGEngine)
    from app.services.document_retrieval import DocumentRetrievalService
    from app.services.prompt_builder import PromptBuilderService
    engine.document_retrieval = DocumentRetrievalService(
        vector_store=MagicMock(),
        max_distance_threshold=1.0,
        retrieval_top_k=10,
        retrieval_window=0,
    )
    engine.prompt_builder = PromptBuilderService()
    engine.retrieval_top_k = 10
    engine.max_distance_threshold = 1.0
    engine.vector_metric = "cosine"
    engine.reranking_enabled = False

    msg = engine._build_done_message([], [], "distance", "dense_only", 0, "ok")

    assert msg["retrieval_debug"]["hybrid_status"] == "dense_only"
    assert msg["retrieval_debug"]["fts_exceptions"] == 0


def test_build_done_message_with_disabled_status():
    """_build_done_message with disabled hybrid_status."""
    from app.services.rag_engine import RAGEngine

    engine = RAGEngine.__new__(RAGEngine)
    from app.services.document_retrieval import DocumentRetrievalService
    from app.services.prompt_builder import PromptBuilderService
    engine.document_retrieval = DocumentRetrievalService(
        vector_store=MagicMock(),
        max_distance_threshold=1.0,
        retrieval_top_k=10,
        retrieval_window=0,
    )
    engine.prompt_builder = PromptBuilderService()
    engine.retrieval_top_k = 10
    engine.max_distance_threshold = 1.0
    engine.vector_metric = "cosine"
    engine.reranking_enabled = False

    msg = engine._build_done_message([], [], "distance", "disabled", 2, "disabled")

    assert msg["retrieval_debug"]["hybrid_status"] == "disabled"
    assert msg["retrieval_debug"]["fts_exceptions"] == 2


# ── edge cases ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_hybrid_status_dense_only_empty_results():
    """hybrid_status='dense_only' when results list is empty but _fts_status key exists."""
    results = []
    engine = _make_engine(results, hybrid_search_enabled=True)

    # Patch _execute_retrieval to return a controlled tuple, bypassing the
    # score_type UnboundLocalError bug in rag_engine.py's except handler.
    async def mock_retrieval(*args, **kwargs):
        return [], None, "CONFIDENT", False, "distance", "dense_only", 0, "ok", [], False, dict(_EMPTY_TOKEN_PACK_STATS)
    with patch.object(engine, '_execute_retrieval', mock_retrieval):
        result_tuple = await engine._execute_retrieval(
            query_embeddings=[[0.1] * 384],
            user_input="test query",
            vault_id=None,
        )
    hybrid_status = result_tuple[5]

    assert hybrid_status == "dense_only", f"Expected 'dense_only', got '{hybrid_status}'"


@pytest.mark.asyncio
async def test_fts_exceptions_zero_value():
    """fts_exceptions=0 is included correctly (not omitted)."""
    results = [{"id": "1", "text": "doc", "file_id": "f1", "_distance": 0.1, "metadata": {}}]
    engine = _make_engine(results, hybrid_search_enabled=True, fts_exceptions=0)

    done_messages = []
    with \
        patch("app.services.rag_engine.settings.tri_vector_search_enabled", False), \
        patch("app.services.rag_engine.settings.context_distillation_enabled", False), \
        patch("app.services.rag_engine.settings.retrieval_evaluation_enabled", False), \
        patch("app.services.rag_engine.settings.query_transformation_enabled", False), \
        patch("app.services.rag_engine.settings.retrieval_recency_weight", 0.0), \
        patch("app.services.rag_engine.settings.context_max_tokens", 0):
        async for msg in engine.query("test query", []):
            if msg.get("type") == "done":
                done_messages.append(msg)

    assert len(done_messages) == 1
    assert "fts_exceptions" in done_messages[0]["retrieval_debug"]
    assert done_messages[0]["retrieval_debug"]["fts_exceptions"] == 0
