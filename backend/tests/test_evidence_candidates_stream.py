"""
Tests for the versioned evidence-candidates SSE event.

Verifies:
1. SSE order: stage:reading -> evidence(candidates) -> content -> done, with
   candidate dicts passed through unchanged and no evidence_type key.
2. The emitted wire line is pinned by a shared fixture
   (tests/fixtures/evidence_candidates_sse_line.txt) that the frontend parser
   test reads as the single source of the wire shape.
3. Error path: error + done exactly as before, no evidence event.
4. Empty candidates: evidence event present with an empty list.
5. Heartbeat (CHAT-002) semantics untouched: a fast stream emits no heartbeat
   comment lines (heartbeat coverage lives in test_chat_streaming.py).
"""
import json
import os
import sys
import unittest
from pathlib import Path
from typing import Any, Dict

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# Stub missing optional dependencies (same pattern as test_chat_stage_events.py)
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


from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.main import app
from app.services.eval_adapter import STAGE_DRAFTING, STAGE_READING, STAGE_SEARCHING

# Candidate dicts shaped exactly like document_retrieval.to_source_metadata
# output (no evidence_type — that key is only assigned to final sources).
_CANDIDATE_A: Dict[str, Any] = {
    "id": "42_default_0",
    "file_id": "42",
    "filename": "handbook.pdf",
    "section": "Maintenance",
    "source_label": "S1",
    "page_number": 3,
    "chunk_bbox": None,
    "snippet": "The coolant interval is 500 hours.",
    "score": 0.87,
    "metadata": {"page_number": 3},
}
_CANDIDATE_B: Dict[str, Any] = {
    "id": "43_default_1",
    "file_id": "43",
    "filename": "safety.pdf",
    "section": "Overview",
    "source_label": "S2",
    "page_number": None,
    "chunk_bbox": None,
    "snippet": "Gloves are mandatory in the maintenance bay.",
    "score": 0.71,
    "metadata": {},
}

# Keys every to_source_metadata-shaped candidate must carry.
_REQUIRED_CANDIDATE_KEYS = {
    "id", "file_id", "filename", "section", "source_label",
    "page_number", "snippet", "score", "metadata",
}

_FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "evidence_candidates_sse_line.txt"


class TestEvidenceCandidatesSSE(unittest.TestCase):
    """SSE forwarding of the engine's evidence_candidates chunk."""

    def setUp(self):
        self.client = TestClient(app)

    def tearDown(self):
        from app.api.deps import get_current_active_user, get_rag_engine
        from app.api.routes.chat import get_stream_auth
        from app.main import app

        app.dependency_overrides.pop(get_rag_engine, None)
        app.dependency_overrides.pop(get_current_active_user, None)
        app.dependency_overrides.pop(get_stream_auth, None)
        if hasattr(app.state, '_test_services'):
            for key in app.state._test_services:
                try:
                    delattr(app.state, key)
                except KeyError:
                    pass
            delattr(app.state, '_test_services')

    def _set_mock_rag_engine(self, mock_query_fn):
        from app.api.deps import get_current_active_user, get_rag_engine
        from app.api.routes.chat import get_stream_auth
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
        app.dependency_overrides[get_stream_auth] = lambda: mock_user

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

    def _post_stream(self):
        return self.client.post(
            "/api/chat/stream",
            json={"messages": [{"role": "user", "content": "test"}]},
        )

    def test_evidence_event_between_reading_and_content(self):
        """Order must be stage:reading -> evidence(candidates) -> content -> done."""
        async def mock_query(*args, **kwargs):
            yield {"type": "stage", "stage": STAGE_SEARCHING}
            yield {"type": "stage", "stage": STAGE_READING}
            yield {
                "type": "evidence_candidates",
                "version": 1,
                "candidates": [_CANDIDATE_A, _CANDIDATE_B],
            }
            yield {"type": "content", "content": "Answer."}
            yield {"type": "done", "sources": [], "memories_used": []}

        self._set_mock_rag_engine(mock_query)

        response = self._post_stream()
        self.assertEqual(response.status_code, 200)

        # A fast stream must not emit heartbeat comment lines (CHAT-002).
        self.assertNotIn(": heartbeat", response.text)

        events = self._parse_sse_events(response.text)
        payloads = [e["data"] for e in events]

        def index_of(pred):
            return next((i for i, p in enumerate(payloads) if pred(p)), None)

        reading_idx = index_of(
            lambda p: p.get("type") == "stage" and p.get("stage") == STAGE_READING
        )
        evidence_idx = index_of(lambda p: p.get("type") == "evidence")
        content_idx = index_of(lambda p: p.get("type") == "content")
        done_idx = index_of(lambda p: p.get("type") == "done")

        self.assertIsNotNone(reading_idx, "stage:reading must be forwarded")
        self.assertIsNotNone(evidence_idx, "evidence event must be forwarded")
        self.assertIsNotNone(content_idx, "content event must be forwarded")
        self.assertIsNotNone(done_idx, "done event must be forwarded")
        self.assertLess(reading_idx, evidence_idx)
        self.assertLess(evidence_idx, content_idx)
        self.assertLess(content_idx, done_idx)

        evidence = payloads[evidence_idx]
        self.assertEqual(evidence["type"], "evidence")
        self.assertEqual(evidence["version"], 1)
        self.assertEqual(evidence["phase"], "candidates")
        candidates = evidence["candidates"]
        self.assertEqual(len(candidates), 2)
        self.assertEqual(candidates, [_CANDIDATE_A, _CANDIDATE_B])
        for cand in candidates:
            self.assertTrue(
                _REQUIRED_CANDIDATE_KEYS.issubset(cand.keys()),
                f"candidate missing to_source_metadata keys: {cand.keys()}",
            )
            self.assertNotIn(
                "evidence_type", cand,
                "candidate events must not carry the done-time evidence_type key",
            )

    def test_evidence_sse_line_matches_shared_fixture(self):
        """The generated wire line must equal the shared fixture payload."""
        from app.api.routes.chat import _evidence_sse_line

        fixture_payload = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))

        line = _evidence_sse_line(fixture_payload["candidates"])
        self.assertTrue(line.startswith("data: "), "SSE lines start with 'data: '")
        self.assertTrue(line.endswith("\n\n"), "SSE lines end with a blank line")
        emitted = json.loads(line[len("data: "):])
        self.assertEqual(emitted, fixture_payload)

    def test_error_path_emits_no_evidence_event(self):
        """An error chunk terminates the stream with error + done only."""
        async def mock_query(*args, **kwargs):
            yield {"type": "stage", "stage": STAGE_SEARCHING}
            yield {"type": "error", "message": "Search failed", "code": "SEARCH_ERROR"}
            yield {
                "type": "evidence_candidates",
                "version": 1,
                "candidates": [_CANDIDATE_A],
            }

        self._set_mock_rag_engine(mock_query)

        response = self._post_stream()
        self.assertEqual(response.status_code, 200)

        events = self._parse_sse_events(response.text)
        payloads = [e["data"] for e in events]
        error_events = [p for p in payloads if p.get("type") == "error"]
        done_events = [p for p in payloads if p.get("type") == "done"]
        evidence_events = [p for p in payloads if p.get("type") == "evidence"]

        self.assertEqual(len(error_events), 1)
        self.assertEqual(error_events[0]["code"], "SEARCH_ERROR")
        self.assertEqual(len(done_events), 1)
        self.assertEqual(
            evidence_events, [],
            "no evidence event may be emitted after an error chunk",
        )

    def test_empty_candidates_evidence_event_present(self):
        """A retrieval run with zero candidates still emits the evidence event."""
        async def mock_query(*args, **kwargs):
            yield {"type": "stage", "stage": STAGE_READING}
            yield {"type": "evidence_candidates", "version": 1, "candidates": []}
            yield {"type": "content", "content": "Answer."}
            yield {"type": "done", "sources": [], "memories_used": []}

        self._set_mock_rag_engine(mock_query)

        response = self._post_stream()
        self.assertEqual(response.status_code, 200)

        events = self._parse_sse_events(response.text)
        evidence_events = [
            e["data"] for e in events if e["data"].get("type") == "evidence"
        ]

        self.assertEqual(len(evidence_events), 1)
        self.assertEqual(evidence_events[0]["version"], 1)
        self.assertEqual(evidence_events[0]["phase"], "candidates")
        self.assertEqual(evidence_events[0]["candidates"], [])


class TestRAGEngineEvidenceCandidatesEmission(unittest.IsolatedAsyncioTestCase):
    """The engine itself must emit evidence_candidates between the Reading
    stage and Drafting, with candidates serialized exactly as done.sources
    (minus the done-time evidence_type assignment)."""

    async def test_engine_emits_candidates_matching_done_sources(self):
        from test_chat_stage_events import (
            FakeEmbeddingService,
            FakeLLMClient,
            FakeMemoryStore,
            FakeVectorStore,
        )

        from app.services.rag_engine import RAGEngine

        with patch("app.services.embeddings.assert_url_safe"), patch(
            "app.services.llm_client.assert_url_safe"
        ):
            engine = RAGEngine()
            engine.embedding_service = FakeEmbeddingService()
            engine.vector_store = FakeVectorStore([
                {"text": "chunk one", "file_id": "file1",
                 "metadata": {"source_file": "doc.md"}, "score": 0.9},
            ])
            engine.memory_store = FakeMemoryStore()
            engine.llm_client = FakeLLMClient(
                response="Answer [S1]", stream_chunks=["Answer [S1]"]
            )

            events = [
                chunk async for chunk in engine.query("query", [], stream=True)
            ]

        types = [e.get("type") for e in events]
        self.assertEqual(types.count("evidence_candidates"), 1)
        reading_idx = next(
            i for i, e in enumerate(events)
            if e.get("type") == "stage" and e.get("stage") == STAGE_READING
        )
        candidates_idx = types.index("evidence_candidates")
        drafting_idx = next(
            i for i, e in enumerate(events)
            if e.get("type") == "stage" and e.get("stage") == STAGE_DRAFTING
        )
        self.assertLess(reading_idx, candidates_idx)
        self.assertLess(candidates_idx, drafting_idx)

        emitted = events[candidates_idx]
        self.assertEqual(emitted["version"], 1)
        candidates = emitted["candidates"]
        self.assertTrue(candidates, "non-empty retrieval must yield candidates")
        done = next(e for e in reversed(events) if e.get("type") == "done")
        self.assertEqual(len(done["sources"]), len(candidates))
        for cand, final in zip(candidates, done["sources"]):
            self.assertNotIn("evidence_type", cand)
            self.assertIn("evidence_type", final)
            self.assertEqual(
                cand, {k: v for k, v in final.items() if k != "evidence_type"}
            )


if __name__ == "__main__":
    unittest.main()
