"""Issue #709 sibling coverage: phantom [W#]/[K#] labels (defect class c).

The frozen check (test_c04_phantom_label_enforcement.py, AC6) uses a
phantom memory label [M9] as the representative for the "regex-parsed
citation labels trusted without membership validation" defect class.
The wiki ([W#]) and KMS ([K#]) families shared the identical gap before
the fix; these siblings pin each of them so no family can regress
independently. Harness mirrors the frozen file (and
tests/test_retrieval_mode_dispatch.py's TestCitationModeEnforcement
stubs): one document source offered, no wiki/KMS evidence offered, an
answer whose only citation is a phantom label of the family under test.
"""

from unittest.mock import patch

import pytest

from app.services.rag_engine import RAGEngine


class RecordingVectorStore:
    def __init__(self, text="Paris is the capital of France.", distance=0.1):
        self._record = {
            "id": "1_0",
            "file_id": "1",
            "text": text,
            "_distance": distance,
            "metadata": {},
        }

    async def search(self, embedding, limit, vault_id=None, query_text="",
                     hybrid=True, hybrid_alpha=0.5, filter_expr=None, **kw):
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
    base_url = "stub-answer-client"
    model = "stub-model"

    def __init__(self, answer):
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
    done = None
    async for chunk in engine.query("what are widgets", [], vault_id=None,
                                    **query_kwargs):
        if chunk.get("type") == "done":
            done = chunk
    assert done is not None, "query() never produced a done message"
    return done


class TestPhantomWikiAndKmsLabels:
    @pytest.mark.asyncio
    async def test_phantom_wiki_label_not_satisfied(self):
        """[W7] with zero wiki evidence offered must not satisfy required mode."""
        client = _StubLLMClient(answer="Widgets are blue [W7].")
        engine = _make_engine(client, RecordingVectorStore())
        with patch("app.services.rag_engine.settings") as mock_settings, patch(
            "app.services.rag_engine._get_pool",
            side_effect=RuntimeError("no db in test"),
        ):
            _apply_settings(mock_settings)
            done = await _collect(engine, stream=False, citation_mode="required")

        assert done.get("sources"), "test requires at least one retrieved source"
        assert not done.get("wiki_used"), "test requires zero wiki evidence offered"
        assert (
            done["citation_enforcement"]["status"] == "missing_citations"
        ), done["citation_enforcement"]

    @pytest.mark.asyncio
    async def test_mixed_valid_and_phantom_labels_missing_citations(self):
        """A valid [S1] beside a phantom [M9] must still flip to
        missing_citations with the phantom named in the detail — the
        constraint covers every family that appears, not only
        phantom-only answers (review blocker 2)."""
        client = _StubLLMClient(
            answer="Widgets are blue [S1]. Also [M9] is green."
        )
        engine = _make_engine(client, RecordingVectorStore())
        with patch("app.services.rag_engine.settings") as mock_settings, patch(
            "app.services.rag_engine._get_pool",
            side_effect=RuntimeError("no db in test"),
        ):
            _apply_settings(mock_settings)
            done = await _collect(engine, stream=False, citation_mode="required")

        enforcement = done["citation_enforcement"]
        assert enforcement["status"] == "missing_citations"
        # Payload shape pinned: satisfied has no detail; missing does.
        assert set(enforcement) == {"mode", "status", "detail"}
        assert "M9" in enforcement["detail"]
        assert "[S1]" in "Widgets are blue [S1]."

    @pytest.mark.asyncio
    async def test_phantom_kms_label_not_satisfied(self):
        """[K4] with zero KMS evidence offered must not satisfy required mode."""
        client = _StubLLMClient(answer="Widgets are blue [K4].")
        engine = _make_engine(client, RecordingVectorStore())
        with patch("app.services.rag_engine.settings") as mock_settings, patch(
            "app.services.rag_engine._get_pool",
            side_effect=RuntimeError("no db in test"),
        ):
            _apply_settings(mock_settings)
            done = await _collect(engine, stream=False, citation_mode="required")

        assert done.get("sources"), "test requires at least one retrieved source"
        assert not done.get("kms_used"), "test requires zero KMS evidence offered"
        assert (
            done["citation_enforcement"]["status"] == "missing_citations"
        ), done["citation_enforcement"]
