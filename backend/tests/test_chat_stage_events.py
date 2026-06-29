"""
Tests for FR-015 stage events (Searching/Reading/Drafting) in chat SSE stream.

Verifies:
1. RAGEngine.query() yields stage events at the correct pipeline transitions.
2. SSE forwarding in chat.py emits the correct `data: {"type": "stage", ...}` format.
3. Existing mode/content/error/fallback/done events are unchanged.
"""
import asyncio
import json
import os
import sys
import unittest
from typing import Any, AsyncIterator, Dict, List, Optional

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# Stub missing optional dependencies (same pattern as test_chat_streaming.py)
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

from app.main import app
from app.services.eval_adapter import STAGE_DRAFTING, STAGE_READING, STAGE_SEARCHING
from app.services.rag_engine import RAGEngine

# ---------------------------------------------------------------------------
# Fake services (same pattern as test_rag_pipeline.py)
# ---------------------------------------------------------------------------

class FakeEmbeddingService:
    """Deterministic fake embedding service for testing."""

    def __init__(self, embedding: Optional[List[float]] = None):
        self.embedding = embedding if embedding is not None else [0.1] * 768

    async def embed_single(self, text: str) -> List[float]:
        return self.embedding.copy()

    async def embed_passage(self, text: str) -> List[float]:
        return self.embedding.copy()


class FakeVectorStore:
    """Deterministic fake vector store for testing."""

    def __init__(self, results: Optional[List[Dict[str, Any]]] = None):
        self._results = results if results is not None else []
        self._fts_exceptions = 0
        self.is_connected = lambda: True

    async def search(
        self,
        embedding: List[float],
        limit: int = 10,
        filter_expr: Optional[str] = None,
        vault_id: Optional[str] = None,
        query_text: Optional[str] = None,
        hybrid: bool = False,
        hybrid_alpha: float = 0.5,
        query_sparse: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        return self._results[:limit]

    async def get_chunks_by_uid(self, chunk_uids: List[str]) -> List[Dict[str, Any]]:
        return []

    def get_fts_exceptions(self) -> int:
        count = self._fts_exceptions
        self._fts_exceptions = 0
        return count


class FakeMemoryRecord:
    def __init__(
        self,
        id: int = 1,
        content: str = "",
        category: Optional[str] = None,
        tags: Optional[str] = None,
        source: Optional[str] = None,
        created_at: Optional[str] = None,
        updated_at: Optional[str] = None,
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
        memories: Optional[List[FakeMemoryRecord]] = None,
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
        vault_id: Optional[int] = None,
    ) -> FakeMemoryRecord:
        self.added_memories.append({
            "content": content,
            "category": category,
            "tags": tags,
            "source": source,
        })
        return FakeMemoryRecord(
            id=len(self.added_memories),
            content=content,
            category=category,
            tags=tags,
            source=source,
        )

    def search_memories(
        self, query: str, limit: int = 5, vault_id: Optional[int] = None
    ) -> List[FakeMemoryRecord]:
        return self._memories[:limit]


class FakeLLMClient:
    """Deterministic fake LLM client for testing."""

    def __init__(
        self,
        response: str = "",
        stream_chunks: Optional[List[str]] = None,
    ):
        self._response = response
        self._stream_chunks = stream_chunks if stream_chunks is not None else []
        self.last_messages: Optional[List[Dict[str, str]]] = None

    async def chat_completion(
        self, messages: List[Dict[str, str]], **kwargs
    ) -> str:
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


class FakeDocumentRetrieval:
    """Fake document retrieval service."""

    def __init__(self, chunks: Optional[List[Dict[str, Any]]] = None):
        self._chunks = chunks if chunks is not None else []
        self.no_match = False

    async def filter_relevant(
        self,
        chunks: List[Dict[str, Any]],
        reranked: bool = False,
        indexed_file_ids: Optional[Any] = None,
    ) -> List[Dict[str, Any]]:
        return self._chunks


class FakePromptBuilder:
    """Fake prompt builder service."""

    def build_messages(
        self,
        user_input: str,
        chat_history: List[Dict[str, Any]],
        relevant_chunks: List[Dict[str, Any]],
        memories: List[Any],
        relevance_hint: Optional[str] = None,
        wiki_evidence: Optional[Any] = None,
        kms_evidence: Optional[Any] = None,
        system_prompt_override: Optional[str] = None,
    ) -> List[Dict[str, str]]:
        return [{"role": "user", "content": user_input}]


def make_engine(
    embedding_service=None,
    vector_store=None,
    memory_store=None,
    llm_client=None,
    document_retrieval=None,
    prompt_builder_service=None,
    reranking_service=None,
):
    """Factory for a RAGEngine with fully-faked dependencies."""
    return RAGEngine(
        embedding_service=embedding_service or FakeEmbeddingService(),
        vector_store=vector_store or FakeVectorStore(),
        memory_store=memory_store or FakeMemoryStore(),
        llm_client=llm_client or FakeLLMClient(response="Test answer."),
        reranking_service=reranking_service,
        document_retrieval_service=document_retrieval or FakeDocumentRetrieval(),
        prompt_builder_service=prompt_builder_service or FakePromptBuilder(),
    )


# ---------------------------------------------------------------------------
# Tests: RAGEngine query() stage events
# ---------------------------------------------------------------------------

class TestRAGEngineStageEvents(unittest.IsolatedAsyncioTestCase):
    """Test that RAGEngine.query() emits stage events at correct transitions."""

    async def test_stage_searching_yielded_before_retrieval(self):
        """STAGE_SEARCHING must be the first stage event yielded, before retrieval begins."""
        engine = make_engine()

        stages = []
        async for chunk in engine.query("test query", []):
            if chunk.get("type") == "stage":
                stages.append(chunk["stage"])

        self.assertIn(STAGE_SEARCHING, stages)
        searching_idx = stages.index(STAGE_SEARCHING)
        self.assertEqual(searching_idx, 0, "STAGE_SEARCHING must be the first stage event")

    async def test_stage_reading_yielded_after_retrieval(self):
        """STAGE_READING must appear after retrieval/distillation, before LLM generation."""
        engine = make_engine()

        stages = []
        async for chunk in engine.query("test query", []):
            if chunk.get("type") == "stage":
                stages.append(chunk["stage"])

        self.assertIn(STAGE_READING, stages)
        # STAGE_READING must appear after STAGE_SEARCHING
        searching_idx = stages.index(STAGE_SEARCHING)
        reading_idx = stages.index(STAGE_READING)
        self.assertGreater(reading_idx, searching_idx, "STAGE_READING must appear after STAGE_SEARCHING")

    async def test_stage_drafting_yielded_before_llm_generation(self):
        """STAGE_DRAFTING must appear before the first content token from LLM."""
        engine = make_engine(
            llm_client=FakeLLMClient(stream_chunks=["Answer."])
        )

        events = []
        async for chunk in engine.query("test query", [], stream=True):
            events.append(chunk)

        # Find the first content event and the STAGE_DRAFTING event
        first_content_idx = next(
            (i for i, e in enumerate(events) if e.get("type") == "content"),
            None,
        )
        drafting_idx = next(
            (
                i
                for i, e in enumerate(events)
                if e.get("type") == "stage" and e.get("stage") == STAGE_DRAFTING
            ),
            None,
        )

        self.assertIsNotNone(drafting_idx, "STAGE_DRAFTING must be emitted")
        self.assertIsNotNone(
            first_content_idx, "content events must be emitted after STAGE_DRAFTING"
        )
        self.assertLess(
            drafting_idx,
            first_content_idx,
            "STAGE_DRAFTING must appear before the first content token",
        )

    async def test_all_three_stages_emitted_in_order(self):
        """Exactly three stage events must be emitted in order: Searching → Reading → Drafting."""
        engine = make_engine(
            llm_client=FakeLLMClient(stream_chunks=["Answer."])
        )

        stages = []
        async for chunk in engine.query("test query", [], stream=True):
            if chunk.get("type") == "stage":
                stages.append(chunk["stage"])

        self.assertEqual(len(stages), 3, "Exactly 3 stage events must be emitted")
        self.assertEqual(stages[0], STAGE_SEARCHING)
        self.assertEqual(stages[1], STAGE_READING)
        self.assertEqual(stages[2], STAGE_DRAFTING)

    async def test_stage_events_are_non_blocking_yields(self):
        """Stage events must not delay the pipeline — they are plain yield points, not separate calls."""
        engine = make_engine(
            llm_client=FakeLLMClient(stream_chunks=["Answer."])
        )

        all_chunks = []
        async for chunk in engine.query("test query", [], stream=True):
            all_chunks.append(chunk)

        # All stage events should have type="stage" and a known stage value
        stage_events = [e for e in all_chunks if e.get("type") == "stage"]
        self.assertEqual(len(stage_events), 3)
        emitted_stages = {e["stage"] for e in stage_events}
        self.assertEqual(
            emitted_stages, {STAGE_SEARCHING, STAGE_READING, STAGE_DRAFTING},
        )

    async def test_content_and_done_still_emitted_after_stage_events(self):
        """Stage events are additive — content and done must still be emitted normally."""
        engine = make_engine(
            llm_client=FakeLLMClient(stream_chunks=["Hello world."])
        )

        events = []
        async for chunk in engine.query("test query", [], stream=True):
            events.append(chunk)

        content_events = [e for e in events if e.get("type") == "content"]
        done_events = [e for e in events if e.get("type") == "done"]

        self.assertTrue(len(content_events) > 0, "content events must still be emitted")
        self.assertTrue(len(done_events) > 0, "done event must still be emitted")
        # Stage events should appear before content
        stage_indices = [
            i for i, e in enumerate(events) if e.get("type") == "stage"
        ]
        first_content_idx = next(
            i for i, e in enumerate(events) if e.get("type") == "content"
        )
        self.assertLess(
            max(stage_indices) if stage_indices else -1,
            first_content_idx,
        )


# ---------------------------------------------------------------------------
# Tests: SSE forwarding
# ---------------------------------------------------------------------------

from unittest.mock import MagicMock


class TestChatSSEStageForwarding(unittest.TestCase):
    """Test that chat.py correctly forwards stage events as SSE."""

    def setUp(self):
        self.client = TestClient(app)

    def tearDown(self):
        from app.api.deps import get_current_active_user, get_rag_engine
        from app.main import app

        app.dependency_overrides.pop(get_rag_engine, None)
        app.dependency_overrides.pop(get_current_active_user, None)
        if hasattr(app.state, '_test_services'):
            for key in app.state._test_services:
                try:
                    delattr(app.state, key)
                except KeyError:
                    pass
            delattr(app.state, '_test_services')

    def _set_mock_rag_engine(self, mock_query_fn):
        from app.api.deps import get_current_active_user, get_rag_engine
        from app.main import app

        mock_engine = MagicMock()
        mock_engine.query = mock_query_fn
        app.dependency_overrides[get_rag_engine] = lambda: mock_engine

        mock_user = {
            "id": "test-user-1",
            "username": "testuser",
            "email": "testuser@example.com",
            "role": "admin",
        }
        app.dependency_overrides[get_current_active_user] = lambda: mock_user

        if not hasattr(app.state, '_test_services'):
            app.state._test_services = []
        app.state._test_services.extend([
            'embedding_service', 'vector_store', 'memory_store', 'llm_client',
        ])

        if not hasattr(app.state, 'embedding_service'):
            app.state.embedding_service = MagicMock()
        if not hasattr(app.state, 'vector_store'):
            app.state.vector_store = MagicMock()
        if not hasattr(app.state, 'memory_store'):
            app.state.memory_store = MagicMock()
        if not hasattr(app.state, 'llm_client'):
            app.state.llm_client = MagicMock()

    def _parse_sse_events(self, response_text: str) -> list:
        """Parse SSE response text into list of event dicts."""
        events = []
        for block in response_text.strip().split('\n\n'):
            if not block:
                continue
            event_data = {}
            data_lines = []
            for line in block.split('\n'):
                if line.startswith('data:'):
                    prefix_len = 6 if line.startswith('data: ') else 5
                    data_lines.append(line[prefix_len:])
                elif line.startswith('event:'):
                    prefix_len = 7 if line.startswith('event: ') else 6
                    event_data['event_type'] = line[prefix_len:]
            if data_lines:
                full_data = '\n'.join(data_lines)
                event_data['data'] = json.loads(full_data)
                events.append(event_data)
        return events

    def test_stage_event_forwarded_as_sse_data(self):
        """When RAGEngine yields a stage event, it must appear in SSE as a data event."""
        async def mock_query(*args, **kwargs):
            yield {"type": "stage", "stage": STAGE_SEARCHING}
            yield {"type": "content", "content": "Answer."}
            yield {"type": "done", "sources": [], "memories_used": []}

        self._set_mock_rag_engine(mock_query)

        response = self.client.post(
            "/api/chat/stream",
            json={"messages": [{"role": "user", "content": "test"}]},
        )

        self.assertEqual(response.status_code, 200)
        events = self._parse_sse_events(response.text)
        stage_events = [
            e['data'] for e in events
            if e.get('data', {}).get("type") == "stage"
        ]

        self.assertEqual(len(stage_events), 1)
        self.assertEqual(stage_events[0]["stage"], STAGE_SEARCHING)

    def test_all_three_stage_events_forwarded_in_order(self):
        """All three stage events (Searching/Reading/Drafting) must appear in SSE in order."""
        async def mock_query(*args, **kwargs):
            yield {"type": "stage", "stage": STAGE_SEARCHING}
            yield {"type": "stage", "stage": STAGE_READING}
            yield {"type": "stage", "stage": STAGE_DRAFTING}
            yield {"type": "content", "content": "Answer."}
            yield {"type": "done", "sources": [], "memories_used": []}

        self._set_mock_rag_engine(mock_query)

        response = self.client.post(
            "/api/chat/stream",
            json={"messages": [{"role": "user", "content": "test"}]},
        )

        self.assertEqual(response.status_code, 200)
        events = self._parse_sse_events(response.text)
        stage_events = [
            e['data'] for e in events
            if e.get('data', {}).get("type") == "stage"
        ]

        self.assertEqual(len(stage_events), 3)
        self.assertEqual(stage_events[0]["stage"], STAGE_SEARCHING)
        self.assertEqual(stage_events[1]["stage"], STAGE_READING)
        self.assertEqual(stage_events[2]["stage"], STAGE_DRAFTING)

    def test_stage_sse_format_correct(self):
        """Stage SSE events must use the format: data: {"type": "stage", "stage": "..."}"""
        async def mock_query(*args, **kwargs):
            yield {"type": "stage", "stage": STAGE_SEARCHING}
            yield {"type": "content", "content": "Answer."}
            yield {"type": "done", "sources": [], "memories_used": []}

        self._set_mock_rag_engine(mock_query)

        response = self.client.post(
            "/api/chat/stream",
            json={"messages": [{"role": "user", "content": "test"}]},
        )

        text = response.text
        # Must contain "data: " prefix
        self.assertIn("data: ", text)
        # Must contain the stage type key
        self.assertIn('"type": "stage"', text)
        # Must contain the stage value
        self.assertIn(f'"stage": "{STAGE_SEARCHING}"', text)
        # Must end with double newline (SSE message separator)
        self.assertIn(f'"stage": "{STAGE_SEARCHING}"}}\n\n', text)

    def test_content_and_done_still_work_with_stage_events(self):
        """Adding stage events must not break existing content/done SSE events."""
        async def mock_query(*args, **kwargs):
            yield {"type": "stage", "stage": STAGE_SEARCHING}
            yield {"type": "stage", "stage": STAGE_READING}
            yield {"type": "content", "content": "Hello world"}
            yield {
                "type": "done",
                "sources": [{"file_id": "doc1"}],
                "memories_used": [],
            }

        self._set_mock_rag_engine(mock_query)

        response = self.client.post(
            "/api/chat/stream",
            json={"messages": [{"role": "user", "content": "test"}]},
        )

        self.assertEqual(response.status_code, 200)
        events = self._parse_sse_events(response.text)

        content_events = [
            e['data'] for e in events
            if e.get('data', {}).get("type") == "content"
        ]
        done_events = [
            e['data'] for e in events
            if e.get('data', {}).get("type") == "done"
        ]

        self.assertEqual(len(content_events), 1)
        self.assertEqual(content_events[0]["content"], "Hello world")
        self.assertEqual(len(done_events), 1)
        self.assertEqual(done_events[0]["sources"], [{"file_id": "doc1"}])

    def test_error_event_still_works_with_stage_events(self):
        """Error events must still be emitted correctly even when stage events are present."""
        async def mock_query(*args, **kwargs):
            yield {"type": "stage", "stage": STAGE_SEARCHING}
            yield {"type": "error", "message": "Search failed", "code": "SEARCH_ERROR"}
            yield {
                "type": "done",
                "sources": [],
                "memories_used": [],
                "wiki_used": [],
                "kms_used": [],
                "score_type": "distance",
            }

        self._set_mock_rag_engine(mock_query)

        response = self.client.post(
            "/api/chat/stream",
            json={"messages": [{"role": "user", "content": "test"}]},
        )

        self.assertEqual(response.status_code, 200)
        events = self._parse_sse_events(response.text)
        error_events = [
            e['data'] for e in events
            if e.get('data', {}).get("type") == "error"
        ]

        self.assertEqual(len(error_events), 1)
        self.assertEqual(error_events[0]["code"], "SEARCH_ERROR")

    def test_fallback_event_re_emitted_as_content(self):
        """Fallback events are re-emitted as content events (existing behavior)."""
        async def mock_query(*args, **kwargs):
            yield {"type": "stage", "stage": STAGE_SEARCHING}
            yield {"type": "fallback", "content": "Using cached result."}
            yield {"type": "done", "sources": [], "memories_used": []}

        self._set_mock_rag_engine(mock_query)

        response = self.client.post(
            "/api/chat/stream",
            json={"messages": [{"role": "user", "content": "test"}]},
        )

        self.assertEqual(response.status_code, 200)
        events = self._parse_sse_events(response.text)
        # Fallback content is re-emitted as a content event (existing behavior)
        content_events = [
            e['data'] for e in events
            if e.get('data', {}).get("type") == "content"
        ]

        self.assertEqual(len(content_events), 1)
        self.assertEqual(content_events[0]["content"], "Using cached result.")


if __name__ == "__main__":
    unittest.main()
