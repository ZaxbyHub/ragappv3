"""
Chat streaming endpoint tests using unittest and TestClient.

Tests SSE format, content accumulation, and done event structure.
"""
import json
import os
import sys
import unittest
from unittest.mock import MagicMock

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

from app.main import app


class TestChatStreaming(unittest.TestCase):
    """Test suite for chat streaming endpoint."""

    def setUp(self):
        """Set up test client."""
        self.client = TestClient(app)

    def tearDown(self):
        from app.api.deps import get_current_active_user, get_rag_engine
        from app.main import app
        app.dependency_overrides.pop(get_rag_engine, None)
        app.dependency_overrides.pop(get_current_active_user, None)
        # Clean up app.state services
        if hasattr(app.state, '_test_services'):
            for key in app.state._test_services:
                try:
                    delattr(app.state, key)
                except KeyError:
                    pass
            delattr(app.state, '_test_services')

    def _set_mock_rag_engine(self, mock_query_fn):
        """Helper to override get_rag_engine with a mock that uses the given query function."""
        from app.api.deps import get_current_active_user, get_rag_engine
        from app.main import app

        mock_engine = MagicMock()
        mock_engine.query = mock_query_fn
        app.dependency_overrides[get_rag_engine] = lambda: mock_engine

        # Mock authentication to return a test user with admin access
        mock_user = {
            "id": "test-user-1",
            "username": "testuser",
            "email": "testuser@example.com",
            "role": "admin",
        }
        app.dependency_overrides[get_current_active_user] = lambda: mock_user

        # Set up app.state services that might be needed
        if not hasattr(app.state, '_test_services'):
            app.state._test_services = []
        app.state._test_services.append('embedding_service')
        app.state._test_services.append('vector_store')
        app.state._test_services.append('memory_store')
        app.state._test_services.append('llm_client')

        # Create simple mocks for services
        if not hasattr(app.state, 'embedding_service'):
            app.state.embedding_service = MagicMock()
        if not hasattr(app.state, 'vector_store'):
            app.state.vector_store = MagicMock()
        if not hasattr(app.state, 'memory_store'):
            app.state.memory_store = MagicMock()
        if not hasattr(app.state, 'llm_client'):
            app.state.llm_client = MagicMock()

    def _parse_sse_events(self, response_text: str) -> list:
        """Parse SSE response text into list of event data."""
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
                elif line.startswith('retry:'):
                    pass
            if data_lines:
                full_data = '\n'.join(data_lines)
                event_data['data'] = json.loads(full_data)
                events.append(event_data)
        return events

    def test_stream_chat_returns_sse_format(self):
        """Test streaming chat returns SSE format with data: lines."""
        # Mock RAGEngine to yield deterministic chunks
        async def mock_query(*args, **kwargs):
            yield {"type": "content", "content": "Hello"}
            yield {"type": "content", "content": " world"}
            yield {"type": "done", "sources": [], "memories_used": []}

        self._set_mock_rag_engine(mock_query)

        response = self.client.post(
            "/api/chat/stream",
            json={"messages": [{"role": "user", "content": "test"}]}
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "text/event-stream; charset=utf-8")

        # Verify SSE format: each line starts with "data: "
        text = response.text
        for line in text.strip().split('\n\n'):
            self.assertTrue(line.startswith("data: "), f"Line does not start with 'data: ': {line}")

    def test_stream_chat_accumulates_content(self):
        """Test streaming chat accumulates content chunks correctly."""
        async def mock_query(*args, **kwargs):
            yield {"type": "content", "content": "First"}
            yield {"type": "content", "content": " second"}
            yield {"type": "content", "content": " third"}
            yield {"type": "done", "sources": [], "memories_used": []}

        self._set_mock_rag_engine(mock_query)

        response = self.client.post(
            "/api/chat/stream",
            json={"messages": [{"role": "user", "content": "test"}]}
        )

        events = self._parse_sse_events(response.text)

        # Filter content events
        content_events = [e['data'] for e in events if e.get('data', {}).get("type") == "content"]
        self.assertEqual(len(content_events), 3)

        # Verify content accumulation
        full_content = "".join(e.get("content", "") for e in content_events)
        self.assertEqual(full_content, "First second third")

    def test_stream_chat_done_event_has_sources(self):
        """Test done event includes sources array."""
        expected_sources = [
            {"file_id": "doc1.txt", "score": 0.95, "metadata": {"source_file": "doc1.txt"}},
            {"file_id": "doc2.txt", "score": 0.87, "metadata": {"source_file": "doc2.txt"}}
        ]

        async def mock_query(*args, **kwargs):
            yield {"type": "content", "content": "Response"}
            yield {"type": "done", "sources": expected_sources, "memories_used": []}

        self._set_mock_rag_engine(mock_query)

        response = self.client.post(
            "/api/chat/stream",
            json={"messages": [{"role": "user", "content": "test"}]}
        )

        events = self._parse_sse_events(response.text)
        done_events = [e['data'] for e in events if e.get('data', {}).get("type") == "done"]

        self.assertEqual(len(done_events), 1)
        done_event = done_events[0]
        self.assertIn("sources", done_event)
        self.assertEqual(done_event["sources"], expected_sources)

    def test_stream_chat_done_event_has_score_type(self):
        """Done event must propagate score_type from the engine so the frontend
        can interpret source scores with the correct polarity and thresholds.
        """
        async def mock_query(*args, **kwargs):
            yield {"type": "content", "content": "Response"}
            yield {
                "type": "done",
                "sources": [{"file_id": "a", "score": 0.2}],
                "memories_used": [],
                "score_type": "rerank",
            }

        self._set_mock_rag_engine(mock_query)

        response = self.client.post(
            "/api/chat/stream",
            json={"messages": [{"role": "user", "content": "test"}]}
        )

        events = self._parse_sse_events(response.text)
        done_events = [e['data'] for e in events if e.get('data', {}).get("type") == "done"]

        self.assertEqual(len(done_events), 1)
        self.assertIn("score_type", done_events[0])
        self.assertEqual(done_events[0]["score_type"], "rerank")

    def test_stream_chat_done_event_score_type_defaults_to_distance(self):
        """If the engine omits score_type, the route must default to 'distance'
        so the frontend never sees an undefined value.
        """
        async def mock_query(*args, **kwargs):
            yield {"type": "content", "content": "Response"}
            # Intentionally no score_type key
            yield {"type": "done", "sources": [], "memories_used": []}

        self._set_mock_rag_engine(mock_query)

        response = self.client.post(
            "/api/chat/stream",
            json={"messages": [{"role": "user", "content": "test"}]}
        )

        events = self._parse_sse_events(response.text)
        done_events = [e['data'] for e in events if e.get('data', {}).get("type") == "done"]

        self.assertEqual(len(done_events), 1)
        self.assertEqual(done_events[0].get("score_type"), "distance")

    def test_stream_chat_done_event_has_memories_used(self):
        """Test done event includes memories_used array."""
        expected_memories = ["User likes Python", "User prefers dark mode"]

        async def mock_query(*args, **kwargs):
            yield {"type": "content", "content": "Response"}
            yield {"type": "done", "sources": [], "memories_used": expected_memories}

        self._set_mock_rag_engine(mock_query)

        response = self.client.post(
            "/api/chat/stream",
            json={"messages": [{"role": "user", "content": "test"}]}
        )

        events = self._parse_sse_events(response.text)
        done_events = [e['data'] for e in events if e.get('data', {}).get("type") == "done"]

        self.assertEqual(len(done_events), 1)
        done_event = done_events[0]
        self.assertIn("memories_used", done_event)
        self.assertEqual(done_event["memories_used"], expected_memories)

    def test_stream_chat_with_history(self):
        """Test streaming chat accepts history parameter."""
        captured_history = None

        async def mock_query(message, history, stream=False, **kwargs):
            nonlocal captured_history
            captured_history = history
            yield {"type": "content", "content": "Response"}
            yield {"type": "done", "sources": [], "memories_used": []}

        self._set_mock_rag_engine(mock_query)

        messages = [
            {"role": "user", "content": "Previous question"},
            {"role": "assistant", "content": "Previous answer"},
            {"role": "user", "content": "test"}
        ]

        response = self.client.post(
            "/api/chat/stream",
            json={"messages": messages}
        )

        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(captured_history)
        self.assertEqual(len(captured_history), 2)

        # Assert history content is passed correctly
        self.assertEqual(captured_history[0]["role"], "user")
        self.assertEqual(captured_history[0]["content"], "Previous question")
        self.assertEqual(captured_history[1]["role"], "assistant")
        self.assertEqual(captured_history[1]["content"], "Previous answer")

    def test_stream_chat_empty_content_chunks(self):
        """Test streaming handles empty content chunks gracefully."""
        async def mock_query(*args, **kwargs):
            yield {"type": "content", "content": ""}
            yield {"type": "content", "content": "Actual content"}
            yield {"type": "done", "sources": [], "memories_used": []}

        self._set_mock_rag_engine(mock_query)

        response = self.client.post(
            "/api/chat/stream",
            json={"messages": [{"role": "user", "content": "test"}]}
        )

        events = self._parse_sse_events(response.text)
        content_events = [e['data'] for e in events if e.get('data', {}).get("type") == "content"]

        # Should include empty content chunk
        self.assertEqual(len(content_events), 2)
        self.assertEqual(content_events[0].get("content"), "")
        self.assertEqual(content_events[1].get("content"), "Actual content")

    def test_stream_chat_single_chunk_response(self):
        """Test streaming with single content chunk and done event."""
        async def mock_query(*args, **kwargs):
            yield {"type": "content", "content": "Complete response"}
            yield {"type": "done", "sources": [], "memories_used": []}

        self._set_mock_rag_engine(mock_query)

        response = self.client.post(
            "/api/chat/stream",
            json={"messages": [{"role": "user", "content": "test"}]}
        )

        events = self._parse_sse_events(response.text)

        # mode event is emitted first, then content, then done
        non_mode = [e for e in events if e.get('data', {}).get('type') != 'mode']
        self.assertEqual(len(non_mode), 2)
        self.assertEqual(non_mode[0]['data'].get("type"), "content")
        self.assertEqual(non_mode[0]['data'].get("content"), "Complete response")
        self.assertEqual(non_mode[1]['data'].get("type"), "done")

    def test_sse_parser_handles_multiline_data(self):
        """Test SSE parser handles multi-line data fields."""
        # Simulate SSE with multi-line data - newlines must be escaped in JSON
        sse_text = """data: {"type": "content", "content": "Line 1\\nLine 2"}

"""
        events = self._parse_sse_events(sse_text)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['data']['content'], "Line 1\nLine 2")

    def test_sse_parser_handles_data_without_space(self):
        """Test SSE parser handles 'data:' without space after colon."""
        sse_text = """data:{"type": "content", "content": "test"}

"""
        events = self._parse_sse_events(sse_text)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['data']['content'], "test")

    def test_sse_parser_handles_event_field(self):
        """Test SSE parser captures event type field."""
        sse_text = """event: message
data: {"type": "content", "content": "test"}

"""
        events = self._parse_sse_events(sse_text)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['event_type'], "message")
        self.assertEqual(events[0]['data']['content'], "test")

    def test_sse_parser_ignores_retry_field(self):
        """Test SSE parser ignores retry field as per spec."""
        sse_text = """retry: 5000
data: {"type": "content", "content": "test"}

"""
        events = self._parse_sse_events(sse_text)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['data']['content'], "test")
        # Retry field should not appear in parsed event
        self.assertNotIn('retry', events[0])

    def test_stream_chat_newline_encoding_in_data(self):
        """Test streaming handles newline characters in content data."""
        async def mock_query(*args, **kwargs):
            yield {"type": "content", "content": "Line 1\nLine 2\nLine 3"}
            yield {"type": "done", "sources": [], "memories_used": []}

        self._set_mock_rag_engine(mock_query)

        response = self.client.post(
            "/api/chat/stream",
            json={"messages": [{"role": "user", "content": "test"}]}
        )

        events = self._parse_sse_events(response.text)
        content_events = [e['data'] for e in events if e.get('data', {}).get("type") == "content"]

        self.assertEqual(len(content_events), 1)
        self.assertEqual(content_events[0].get("content"), "Line 1\nLine 2\nLine 3")

    def test_stream_error_does_not_leak_exception_details(self):
        """DD-A008: Exception details must NOT be sent to the client.

        When rag_engine.query raises an exception, the SSE error event must
        contain only the generic message 'An error occurred during chat processing'
        and code 'INTERNAL_ERROR'. The exception type name and exception message
        must NOT appear anywhere in the error event (they are server-side logged only).
        """
        # Use a distinctive exception type and message to make verification strict
        async def mock_query_that_raises(*args, **kwargs):
            raise ValueError("Database connection failed — host unreachable")
            yield  # unreachable, but makes this an async generator  # pragma: no cover

        self._set_mock_rag_engine(mock_query_that_raises)

        response = self.client.post(
            "/api/chat/stream",
            json={"messages": [{"role": "user", "content": "test"}]}
        )

        self.assertEqual(response.status_code, 200)

        events = self._parse_sse_events(response.text)
        error_events = [e['data'] for e in events if e.get('data', {}).get("type") == "error"]

        self.assertEqual(len(error_events), 1, "Expected exactly one error event")
        error_event = error_events[0]

        # Assert 1: exact generic message
        self.assertEqual(
            error_event.get("message"),
            "An error occurred during chat processing",
            "Error message must be the generic client-safe message",
        )

        # Assert 2: error code
        self.assertEqual(
            error_event.get("code"),
            "INTERNAL_ERROR",
            "Error code must be INTERNAL_ERROR",
        )

        # Assert 3: message does NOT contain exception type name
        error_message = error_event.get("message", "")
        self.assertNotIn(
            "ValueError",
            error_message,
            "Error message must NOT contain exception type name",
        )

        # Assert 4: message does NOT contain exception message
        self.assertNotIn(
            "Database connection failed",
            error_message,
            "Error message must NOT contain exception details",
        )
        self.assertNotIn(
            "host unreachable",
            error_message,
            "Error message must NOT contain exception details",
        )

    def test_stream_error_does_not_leak_generic_exception(self):
        """Verify the fix also works for non-ValueError exceptions (e.g. RuntimeError, KeyError)."""
        async def mock_query_that_raises(*args, **kwargs):
            raise RuntimeError("Secret internal token: abc123")
            yield  # unreachable, but makes this an async generator  # pragma: no cover

        self._set_mock_rag_engine(mock_query_that_raises)

        response = self.client.post(
            "/api/chat/stream",
            json={"messages": [{"role": "user", "content": "test"}]}
        )

        self.assertEqual(response.status_code, 200)

        events = self._parse_sse_events(response.text)
        error_events = [e['data'] for e in events if e.get('data', {}).get("type") == "error"]
        self.assertEqual(len(error_events), 1)

        error_message = error_events[0].get("message", "")

        # The generic message must be present
        self.assertEqual(error_message, "An error occurred during chat processing")
        # Exception details must NOT be present
        self.assertNotIn("RuntimeError", error_message)
        self.assertNotIn("Secret internal token", error_message)
        self.assertNotIn("abc123", error_message)
        # Code must be INTERNAL_ERROR
        self.assertEqual(error_events[0].get("code"), "INTERNAL_ERROR")

    def test_stream_chat_done_event_has_citation_validation_when_invalid(self):
        """Done event must include citation_validation and repaired_content when
        the assembled content contains a citation that has no matching source.

        The mock yields content with [S1] but the sources list has no source_label
        key — therefore source_count=0 and valid_s={}.  The validator marks S1
        invalid, sets invalid_stripped=True, and returns repaired_content with the
        hallucinated [S1] marker stripped.
        """
        async def mock_query(*args, **kwargs):
            yield {"type": "content", "content": "Answer based on [S1] source."}
            yield {
                "type": "done",
                # No source_label key → _max_index returns 0 → valid_s is {}.
                # [S1] in content is therefore invalid.
                "sources": [{"file_id": "doc1.txt", "score": 0.9}],
                "memories_used": [],
            }

        self._set_mock_rag_engine(mock_query)

        response = self.client.post(
            "/api/chat/stream",
            json={"messages": [{"role": "user", "content": "test"}]}
        )

        self.assertEqual(response.status_code, 200)

        events = self._parse_sse_events(response.text)
        done_events = [
            e["data"] for e in events
            if e.get("data", {}).get("type") == "done"
        ]
        self.assertEqual(len(done_events), 1, "Expected exactly one done event")
        done_event = done_events[0]

        # citation_validation must be present and have S1 listed as invalid
        self.assertIn("citation_validation", done_event)
        cv = done_event["citation_validation"]
        self.assertIn("S1", cv["invalid"])
        self.assertNotIn("S1", cv["valid"])

        # repaired_content must also be present (invalid citations were stripped)
        self.assertIn("repaired_content", done_event)
        repaired = done_event["repaired_content"]
        self.assertIsNotNone(repaired)
        self.assertNotIn("[S1]", repaired)

    def test_stream_chat_done_event_no_citation_validation_when_valid(self):
        """Done event must NOT include citation_validation or repaired_content when
        all citations in the generated content match available sources.

        The mock yields content with [S1] and the sources list includes a
        source_label key with value "S1" — therefore source_count=1, [S1] is
        valid, and no repair is needed.
        """
        async def mock_query(*args, **kwargs):
            yield {"type": "content", "content": "Answer based on [S1] source."}
            yield {
                "type": "done",
                "sources": [{"source_label": "S1", "file_id": "doc1.txt", "score": 0.9}],
                "memories_used": [],
            }

        self._set_mock_rag_engine(mock_query)

        response = self.client.post(
            "/api/chat/stream",
            json={"messages": [{"role": "user", "content": "test"}]}
        )

        self.assertEqual(response.status_code, 200)

        events = self._parse_sse_events(response.text)
        done_events = [
            e["data"] for e in events
            if e.get("data", {}).get("type") == "done"
        ]
        self.assertEqual(len(done_events), 1, "Expected exactly one done event")
        done_event = done_events[0]

        # citation_validation must NOT be present when all citations are valid
        self.assertNotIn("citation_validation", done_event)

        # repaired_content must NOT be present when no citations were repaired
        self.assertNotIn("repaired_content", done_event)


if __name__ == "__main__":
    unittest.main()
