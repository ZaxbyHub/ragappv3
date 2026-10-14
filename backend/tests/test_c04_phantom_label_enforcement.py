"""Issue #709 acceptance check (C04, AC6): a phantom [M#] label must not
satisfy citation_mode="required".

The required-citation enforcement in ``RAGEngine.query`` validates
label-set membership only for ``[S#]`` document labels. ``cited_memories``
comes straight from ``parse_citations`` with no membership check against
the memories actually offered, so an answer citing a phantom memory label
(e.g. ``[M9]`` when NO memories were retrieved) takes the "satisfied"
branch: the phantom citation launders an otherwise-uncited answer past
the required-citation gate — exactly the hallucination-enabling failure
the mode exists to prevent.

Contract: with citation_mode="required", at least one document source
offered, no memories offered, and an answer whose only citation is a
phantom memory label, the enforcement status must NOT be "satisfied".

Stubs mirror ``tests/test_retrieval_mode_dispatch.py``
(``TestCitationModeEnforcement`` / ``TestCitationModeDisabledPrompt``):
_StubLLMClient answer bot, RecordingVectorStore serving one relevant
chunk, patched ``app.services.rag_engine.settings`` and a raising
``_get_pool`` so no real DB is touched.
"""

from unittest.mock import patch

import pytest

from app.services.rag_engine import RAGEngine

PHANTOM_MEMORY_ANSWER = "Widgets are blue [M9]."


class RecordingVectorStore:
    """Records search() signatures and serves one relevant document chunk."""

    def __init__(self, text="Paris is the capital of France.", distance=0.1):
        self.search_calls = []
        self._record = {
            "id": "1_0",
            "file_id": "1",
            "text": text,
            "_distance": distance,
            "metadata": {},
        }

    async def search(self, embedding, limit, vault_id=None, query_text="",
                     hybrid=True, hybrid_alpha=0.5, filter_expr=None, **kw):
        self.search_calls.append({
            "hybrid": hybrid,
            "hybrid_alpha": hybrid_alpha,
            "query_text": query_text,
            "embedding": embedding,
            "limit": limit,
            "filter_expr": filter_expr,
        })
        return [dict(self._record)]

    def get_fts_exceptions(self):
        return 0

    def is_connected(self):
        return True


class _StubEmbeddingService:
    async def embed_single(self, text):
        return [0.1, 0.2, 0.3]

    async def embed_passage(self, text):
        return [0.1, 0.2, 0.3]


class _StubMemoryStore:
    def detect_memory_intent(self, text):
        return None


class _StubLLMClient:
    """Answer bot: yields a fixed answer; records provider messages."""

    base_url = "stub-answer-client"
    model = "stub-model"

    def __init__(self, answer=PHANTOM_MEMORY_ANSWER):
        self.answer = answer
        self.last_metrics = {"provider_url": self.base_url}
        self.completion_messages = None
        self.stream_messages = None

    async def chat_completion(self, messages, **kw):
        self.completion_messages = messages
        return self.answer

    async def chat_completion_stream(self, messages, **kw):
        self.stream_messages = messages
        yield self.answer


def _make_engine(client, store):
    return RAGEngine(
        embedding_service=_StubEmbeddingService(),
        vector_store=store,
        memory_store=_StubMemoryStore(),
        llm_client=client,
        reranking_service=None,
        instant_client=client,
        thinking_client=client,
    )


def _apply_settings(mock):
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
    mock.hybrid_search_enabled = True
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


async def _collect(engine, **query_kwargs):
    """Drive query() and return (done_message, content_text)."""
    done = None
    content = []
    async for chunk in engine.query("what are widgets", [], vault_id=None,
                                    **query_kwargs):
        if chunk.get("type") == "done":
            done = chunk
        elif chunk.get("type") == "content":
            content.append(chunk.get("content", ""))
    assert done is not None, "query() never produced a done message"
    return done, "".join(content)


class TestPhantomMemoryLabel:
    @pytest.mark.asyncio
    async def test_phantom_memory_label_not_satisfied(self):
        """[M9] with zero memories offered must not satisfy required mode."""
        client = _StubLLMClient(answer=PHANTOM_MEMORY_ANSWER)
        store = RecordingVectorStore()
        engine = _make_engine(client, store)
        with patch("app.services.rag_engine.settings") as mock_settings, patch(
            "app.services.rag_engine._get_pool",
            side_effect=RuntimeError("no db in test"),
        ):
            _apply_settings(mock_settings)
            done, _ = await _collect(engine, stream=False, citation_mode="required")

        # Precondition: one document source was offered (and no memories —
        # the stub memory store offers none and memory retrieval is off).
        assert done.get("sources"), "test requires at least one retrieved source"
        assert not done.get("memories_used"), "test requires zero memories offered"

        satisfied = done["citation_enforcement"]["status"] == "satisfied"
        assert satisfied is False
