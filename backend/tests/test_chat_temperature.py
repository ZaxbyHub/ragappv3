"""Per-request temperature forwarding through RAGEngine.query (issue #510 CHAT-003).

An explicit ``temperature`` passed to ``query()`` must reach the provider call
(``chat_completion`` / ``chat_completion_stream``) for BOTH the streaming and
non-streaming paths; ``temperature=0.0`` must be forwarded as 0.0 (not dropped
as falsy), and omitting it keeps the provider default.
"""

from unittest.mock import patch

import pytest

from app.services.rag_engine import RAGEngine

ANSWER = "The capital of France is Paris, a city known for its landmarks."


class RecordingLLMClient:
    """Fake LLM client that records the effective temperature of every call.

    The signature default (0.7) mirrors the provider default: when the engine
    omits ``temperature`` the recorded value is 0.7.
    """

    base_url = "fake-temperature-client"
    model = "fake-model"

    def __init__(self, answer: str = ANSWER):
        self.answer = answer
        self.last_metrics = {"provider_url": self.base_url}
        self.completion_calls = []
        self.stream_calls = []

    async def chat_completion(self, messages, max_tokens=32768, temperature=0.7, **kw):
        self.completion_calls.append(
            {"messages": messages, "max_tokens": max_tokens, "temperature": temperature}
        )
        return self.answer

    async def chat_completion_stream(self, messages, max_tokens=32768, temperature=0.7, **kw):
        self.stream_calls.append(
            {"messages": messages, "max_tokens": max_tokens, "temperature": temperature}
        )
        yield self.answer


class _StubEmbeddingService:
    async def embed_single(self, text):
        return [0.1, 0.2, 0.3]

    async def embed_passage(self, text):
        return [0.1, 0.2, 0.3]


class _StubMemoryStore:
    def detect_memory_intent(self, text):
        return None


class _RecordingVectorStore:
    def __init__(self):
        self.search_calls = []

    async def search(self, embedding, limit, vault_id=None, query_text="",
                     hybrid=True, hybrid_alpha=0.5, filter_expr=None, **kw):
        self.search_calls.append({"embedding": embedding, "limit": limit})
        return [
            {
                "id": "1_0",
                "file_id": "1",
                "text": "Paris is the capital of France.",
                "_distance": 0.1,
                "metadata": {},
            }
        ]

    def get_fts_exceptions(self):
        return 0


def _make_engine(client):
    return RAGEngine(
        embedding_service=_StubEmbeddingService(),
        vector_store=_RecordingVectorStore(),
        memory_store=_StubMemoryStore(),
        llm_client=client,
        reranking_service=None,
        instant_client=client,
        thinking_client=client,
    )


def _apply_settings(mock):
    """Baseline settings that drive query() to the done message with stubs."""
    mock.agentic_rag_enabled = False
    mock.default_chat_mode = "thinking"
    mock.query_transformation_enabled = False
    mock.memory_retrieval_enabled = False
    mock.retrieval_evaluation_enabled = False
    mock.context_distillation_enabled = False
    mock.context_distillation_synthesis_enabled = False
    mock.context_max_tokens = 0
    mock.parent_retrieval_enabled = False
    mock.retrieval_recency_weight = 0.0
    mock.rrf_legacy_mode = False
    mock.exact_match_promote = False
    mock.reranking_enabled = False
    mock.hybrid_search_enabled = False
    mock.hybrid_alpha = 0.6
    mock.maintenance_mode = False
    mock.max_distance_threshold = 1.0
    mock.rag_relevance_threshold = 0.5
    mock.retrieval_top_k = 10
    mock.retrieval_window = 0
    mock.initial_retrieval_top_k = 10
    mock.reranker_top_n = 5
    mock.thinking_max_tokens = 1024
    mock.rag_trace_in_response = False
    mock.kms_enabled = False
    return mock


async def _collect_done(engine, **query_kwargs):
    """Drive query() and return its done message."""
    done = None
    async for chunk in engine.query("what is the capital", [], vault_id=None,
                                    **query_kwargs):
        if chunk.get("type") == "done":
            done = chunk
    assert done is not None, "query() never produced a done message"
    return done


class TestTemperatureForwarding:
    @pytest.mark.asyncio
    async def test_nonstream_forwards_temperature_1_2(self):
        client = RecordingLLMClient()
        engine = _make_engine(client)
        with patch("app.services.rag_engine.settings") as mock_settings, patch(
            "app.services.rag_engine._get_pool",
            side_effect=RuntimeError("no db in test"),
        ):
            _apply_settings(mock_settings)
            await _collect_done(engine, stream=False, temperature=1.2)
        assert len(client.completion_calls) == 1
        assert client.completion_calls[0]["temperature"] == 1.2
        assert client.stream_calls == []

    @pytest.mark.asyncio
    async def test_nonstream_forwards_temperature_zero(self):
        client = RecordingLLMClient()
        engine = _make_engine(client)
        with patch("app.services.rag_engine.settings") as mock_settings, patch(
            "app.services.rag_engine._get_pool",
            side_effect=RuntimeError("no db in test"),
        ):
            _apply_settings(mock_settings)
            await _collect_done(engine, stream=False, temperature=0.0)
        assert client.completion_calls[0]["temperature"] == 0.0

    @pytest.mark.asyncio
    async def test_nonstream_omitted_temperature_uses_provider_default(self):
        client = RecordingLLMClient()
        engine = _make_engine(client)
        with patch("app.services.rag_engine.settings") as mock_settings, patch(
            "app.services.rag_engine._get_pool",
            side_effect=RuntimeError("no db in test"),
        ):
            _apply_settings(mock_settings)
            await _collect_done(engine, stream=False)
        assert client.completion_calls[0]["temperature"] == 0.7

    @pytest.mark.asyncio
    async def test_stream_forwards_temperature_1_2(self):
        client = RecordingLLMClient()
        engine = _make_engine(client)
        with patch("app.services.rag_engine.settings") as mock_settings, patch(
            "app.services.rag_engine._get_pool",
            side_effect=RuntimeError("no db in test"),
        ):
            _apply_settings(mock_settings)
            await _collect_done(engine, stream=True, temperature=1.2)
        assert len(client.stream_calls) == 1
        assert client.stream_calls[0]["temperature"] == 1.2
        assert client.completion_calls == []

    @pytest.mark.asyncio
    async def test_stream_forwards_temperature_zero(self):
        client = RecordingLLMClient()
        engine = _make_engine(client)
        with patch("app.services.rag_engine.settings") as mock_settings, patch(
            "app.services.rag_engine._get_pool",
            side_effect=RuntimeError("no db in test"),
        ):
            _apply_settings(mock_settings)
            await _collect_done(engine, stream=True, temperature=0.0)
        assert client.stream_calls[0]["temperature"] == 0.0

    @pytest.mark.asyncio
    async def test_stream_omitted_temperature_uses_provider_default(self):
        client = RecordingLLMClient()
        engine = _make_engine(client)
        with patch("app.services.rag_engine.settings") as mock_settings, patch(
            "app.services.rag_engine._get_pool",
            side_effect=RuntimeError("no db in test"),
        ):
            _apply_settings(mock_settings)
            await _collect_done(engine, stream=True)
        assert client.stream_calls[0]["temperature"] == 0.7
