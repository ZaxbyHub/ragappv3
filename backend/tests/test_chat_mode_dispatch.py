"""Tests for Instant/Thinking chat mode dispatch in the RAG engine.

These tests assert that ``RAGEngine.query`` / ``_execute_retrieval`` apply the
two-tier latency policy: Instant mode skips the expensive pre-generation LLM
aux calls (step-back transform, CRAG retrieval evaluation), while Thinking mode
keeps the full-quality pipeline.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.chat_mode import ChatMode
from app.services.embeddings import EmbeddingError
from app.services.rag_engine import RAGEngine, RAGEngineError


def _make_engine_for_retrieval():
    """Construct a RAGEngine with the minimum wiring for _execute_retrieval."""
    engine = RAGEngine.__new__(RAGEngine)
    engine.vector_store = MagicMock()
    engine.vector_store.search = AsyncMock(
        return_value=[
            {
                "id": "chunk-1",
                "file_id": "file-1",
                "text": "Relevant text",
                "_distance": 0.1,
                "metadata": {},
            }
        ]
    )
    engine.vector_store.get_fts_exceptions.return_value = 0
    engine.reranking_service = None
    engine._retrieval_evaluators = {}
    return engine


@pytest.fixture
def mock_settings():
    """Patch rag_engine.settings with retrieval-eval relevant fields."""
    with patch("app.services.rag_engine.settings") as mock:
        mock.retrieval_evaluation_enabled = True
        mock.instant_skip_retrieval_evaluation = True
        mock.context_max_tokens = 0
        mock.retrieval_recency_weight = 0.0
        mock.rrf_legacy_mode = False
        mock.exact_match_promote = False
        mock.reranking_enabled = False
        mock.hybrid_search_enabled = False
        yield mock


class TestRetrievalEvaluationModeGating:
    """CRAG retrieval evaluation must be skipped in Instant mode only."""

    @pytest.mark.asyncio
    async def test_instant_mode_skips_retrieval_evaluation(self, mock_settings):
        """Instant mode must NOT invoke the CRAG retrieval evaluator."""
        engine = _make_engine_for_retrieval()
        instant_client = MagicMock()

        with patch(
            "app.services.rag_engine.RetrievalEvaluator"
        ) as evaluator_cls:
            await engine._execute_retrieval(
                [("original", [0.1, 0.2, 0.3])],
                "question",
                vault_id=1,
                active_client=instant_client,
                mode=ChatMode.INSTANT,
            )

        # Evaluator must never be constructed/called in Instant mode.
        evaluator_cls.assert_not_called()

    @pytest.mark.asyncio
    async def test_thinking_mode_runs_retrieval_evaluation(self, mock_settings):
        """Thinking mode keeps the full pipeline: evaluator IS invoked."""
        engine = _make_engine_for_retrieval()
        thinking_client = MagicMock()
        fake_evaluator = MagicMock()
        fake_evaluator.evaluate = AsyncMock(return_value="CONFIDENT")

        with patch(
            "app.services.rag_engine.RetrievalEvaluator",
            return_value=fake_evaluator,
        ) as evaluator_cls:
            await engine._execute_retrieval(
                [("original", [0.1, 0.2, 0.3])],
                "question",
                vault_id=1,
                active_client=thinking_client,
                mode=ChatMode.THINKING,
            )

        evaluator_cls.assert_called_once_with(thinking_client)
        fake_evaluator.evaluate.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_mode_defaults_to_running_evaluation(self, mock_settings):
        """Back-compat: when mode is unset, evaluation still runs (no skip)."""
        engine = _make_engine_for_retrieval()
        client = MagicMock()
        fake_evaluator = MagicMock()
        fake_evaluator.evaluate = AsyncMock(return_value="CONFIDENT")

        with patch(
            "app.services.rag_engine.RetrievalEvaluator",
            return_value=fake_evaluator,
        ) as evaluator_cls:
            await engine._execute_retrieval(
                [("original", [0.1, 0.2, 0.3])],
                "question",
                vault_id=1,
                active_client=client,
                # mode omitted → defaults to None → not Instant → eval runs
            )

        evaluator_cls.assert_called_once_with(client)
        fake_evaluator.evaluate.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_instant_skip_disabled_runs_evaluation(self, mock_settings):
        """If the operator disables the Instant skip, evaluation runs in Instant too."""
        mock_settings.instant_skip_retrieval_evaluation = False
        engine = _make_engine_for_retrieval()
        client = MagicMock()
        fake_evaluator = MagicMock()
        fake_evaluator.evaluate = AsyncMock(return_value="CONFIDENT")

        with patch(
            "app.services.rag_engine.RetrievalEvaluator",
            return_value=fake_evaluator,
        ) as evaluator_cls:
            await engine._execute_retrieval(
                [("original", [0.1, 0.2, 0.3])],
                "question",
                vault_id=1,
                active_client=client,
                mode=ChatMode.INSTANT,
            )

        evaluator_cls.assert_called_once_with(client)
        fake_evaluator.evaluate.assert_awaited_once()


# ---------------------------------------------------------------------------
# query()-level gating: step-back transform + follow-up rewrite
# ---------------------------------------------------------------------------


class _FailAfterGatesEmbeddingService:
    """Embedding service that raises on the original-query embedding.

    The follow-up-rewrite and step-back gates in ``RAGEngine.query`` run BEFORE
    query embedding. Raising here exits the generator cleanly via
    ``RAGEngineError`` immediately after the gates, so these tests exercise the
    mode gating without driving the deep retrieval tail (which would require a
    real DB pool for indexed-file/supersession lookups and otherwise hangs).
    """

    async def embed_single(self, text):
        raise EmbeddingError("stop after gates")

    async def embed_passage(self, text):
        raise EmbeddingError("stop after gates")

    def get_cache_stats(self):
        return {}

    @property
    def embedding_model(self):
        return "stub-embed"

    @property
    def embeddings_url(self):
        return "http://stub"


class _StubMemoryStore:
    def detect_memory_intent(self, text):
        return None

    def search_memories(self, query, limit=5, vault_id=None, include_global=False):
        return []


class _StubLLMClient:
    async def chat_completion(self, messages, *args, **kwargs):
        return "A sufficiently long synthesized answer for the test harness."

    async def chat_completion_stream(self, messages, *args, **kwargs):
        yield "answer"


def _make_query_engine():
    """Engine wired with stubs and distinct instant/thinking clients."""
    return RAGEngine(
        embedding_service=_FailAfterGatesEmbeddingService(),
        vector_store=MagicMock(),
        memory_store=_StubMemoryStore(),
        llm_client=_StubLLMClient(),
        reranking_service=None,
        instant_client=_StubLLMClient(),
        thinking_client=_StubLLMClient(),
    )


async def _drive_query(engine, *args, **kwargs):
    """Drive query() to completion, swallowing the post-gate RAGEngineError.

    The stub embedding service raises after the gates run, so the generator
    raises RAGEngineError. That is expected and intentional — the gating
    assertions are made by the caller after this returns.
    """
    with pytest.raises(RAGEngineError):
        async for _ in engine.query(*args, **kwargs):
            pass


def _query_settings(mock):
    """Apply a baseline of settings flags needed to drive query() to done."""
    mock.query_transformation_enabled = True
    mock.stepback_enabled = True
    mock.hyde_enabled = False
    mock.retrieval_evaluation_enabled = True
    mock.context_distillation_enabled = False
    mock.context_distillation_synthesis_enabled = False
    mock.memory_retrieval_enabled = False
    mock.parent_retrieval_enabled = False
    mock.kms_enabled = False
    mock.reranking_enabled = False
    mock.hybrid_search_enabled = False
    mock.maintenance_mode = False
    mock.context_max_tokens = 0
    mock.retrieval_recency_weight = 0.0
    mock.rrf_legacy_mode = False
    mock.exact_match_promote = False
    mock.max_distance_threshold = 0.5
    mock.retrieval_top_k = 10
    mock.rag_trace_in_response = False
    mock.default_chat_mode = "thinking"
    # Instant skip flags (defaults under test)
    mock.instant_skip_query_transformation = True
    mock.instant_skip_retrieval_evaluation = True
    mock.instant_skip_distillation_synthesis = True
    mock.instant_skip_followup_rewrite = False
    mock.agentic_rag_enabled = False
    return mock


class TestQueryLevelTransformationGating:
    """query()-level gating: Instant skips step-back transform; Thinking runs it."""

    @pytest.mark.asyncio
    async def test_instant_skips_query_transformation(self):
        engine = _make_query_engine()
        with patch("app.services.rag_engine.settings") as mock_settings, patch(
            "app.services.rag_engine.QueryTransformer"
        ) as qt_cls:
            _query_settings(mock_settings)
            await _drive_query(
                engine,
                "what is the install procedure",
                [],
                stream=False,
                vault_id=1,
                mode=ChatMode.INSTANT,
            )
        # Instant + skip flag True + no history (follow-up never fires)
        # → QueryTransformer never constructed.
        qt_cls.assert_not_called()

    @pytest.mark.asyncio
    async def test_thinking_runs_query_transformation(self):
        engine = _make_query_engine()
        with patch("app.services.rag_engine.settings") as mock_settings, patch(
            "app.services.rag_engine.QueryTransformer"
        ) as qt_cls:
            _query_settings(mock_settings)
            instance = qt_cls.return_value
            instance.transform = AsyncMock(
                return_value=[("original", "what is the install procedure")]
            )
            await _drive_query(
                engine,
                "what is the install procedure",
                [],
                stream=False,
                vault_id=1,
                mode=ChatMode.THINKING,
            )
        qt_cls.assert_called_once()
        instance.transform.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_instant_skip_disabled_runs_transformation(self):
        engine = _make_query_engine()
        with patch("app.services.rag_engine.settings") as mock_settings, patch(
            "app.services.rag_engine.QueryTransformer"
        ) as qt_cls:
            _query_settings(mock_settings)
            mock_settings.instant_skip_query_transformation = False
            instance = qt_cls.return_value
            instance.transform = AsyncMock(
                return_value=[("original", "what is the install procedure")]
            )
            await _drive_query(
                engine,
                "what is the install procedure",
                [],
                stream=False,
                vault_id=1,
                mode=ChatMode.INSTANT,
            )
        qt_cls.assert_called_once()


class _OkEmbeddingService:
    """Embedding stub that succeeds (lets query() run to the done message)."""

    async def embed_single(self, text):
        return [0.1, 0.2, 0.3]

    async def embed_passage(self, text):
        return [0.1, 0.2, 0.3]


class _OkVectorStore:
    """Vector store fake serving one relevant chunk per search."""

    def __init__(self):
        self.search_calls = 0

    async def search(self, embedding, limit, vault_id=None, query_text="",
                     hybrid=True, hybrid_alpha=0.5, filter_expr=None, **kw):
        self.search_calls += 1
        return [{
            "id": "1_0",
            "file_id": "1",
            "text": "Paris is the capital of France.",
            "_distance": 0.1,
            "metadata": {},
        }]

    def get_fts_exceptions(self):
        return 0

    def is_connected(self):
        return True


class _AnswerLLMClient:
    base_url = "answer-stub"
    model = "answer-stub"

    def __init__(self):
        self.last_metrics = {}

    async def chat_completion(self, messages, **kw):
        return "The capital of France is Paris, a city known for its landmarks."

    async def chat_completion_stream(self, messages, **kw):
        yield "The capital of France is Paris, a city known for its landmarks."


class _FakePlanner:
    """QueryPlanner stub with an injected plan."""

    plan_result: list = []

    def __init__(self, client):
        self.client = client

    async def plan(self, query):
        return list(_FakePlanner.plan_result)


class TestInstantFusedEvaluationSkip:
    """Issue #510 RAG-005: Instant + instant_skip_retrieval_evaluation must
    skip the retrieval evaluator on the FUSED multi-sub-query path too.
    """

    def _settings(self, mock, *, skip_eval):
        _query_settings(mock)
        mock.instant_initial_retrieval_top_k = 10
        mock.instant_reranker_top_n = 5
        mock.instant_memory_context_top_k = 5
        mock.instant_max_tokens = 512
        mock.instant_skip_retrieval_evaluation = skip_eval
        # Window expansion would need get_chunks_by_uid on the fake store.
        mock.retrieval_window = 0
        return mock

    def _make_engine(self):
        return RAGEngine(
            embedding_service=_OkEmbeddingService(),
            vector_store=_OkVectorStore(),
            memory_store=_StubMemoryStore(),
            llm_client=_AnswerLLMClient(),
            reranking_service=None,
            instant_client=_AnswerLLMClient(),
            thinking_client=_AnswerLLMClient(),
        )

    async def _drive_to_done(self, engine):
        done = None
        async for chunk in engine.query(
            "compare the capital and population of france",
            [],
            stream=False,
            vault_id=None,
            mode=ChatMode.INSTANT,
        ):
            if chunk.get("type") == "done":
                done = chunk
        assert done is not None
        return done

    @pytest.mark.asyncio
    async def test_instant_multi_subquery_plan_skips_fused_evaluation(self):
        """INSTANT + skip=True + multi-subquery plan → ZERO evaluator calls."""
        engine = self._make_engine()
        _FakePlanner.plan_result = [
            "what is the capital of france",
            "what is the population of france",
        ]
        with patch("app.services.rag_engine.settings") as mock_settings, patch(
            "app.services.rag_engine.QueryPlanner", _FakePlanner
        ), patch(
            "app.services.rag_engine.RetrievalEvaluator"
        ) as evaluator_cls, patch(
            "app.services.rag_engine._get_pool",
            side_effect=RuntimeError("no db in test"),
        ):
            self._settings(mock_settings, skip_eval=True)
            done = await self._drive_to_done(engine)

        evaluator_cls.assert_not_called()
        assert done.get("sources"), "test requires the fused path to return sources"

    @pytest.mark.asyncio
    async def test_instant_single_subquery_plan_skips_evaluation(self):
        engine = self._make_engine()
        _FakePlanner.plan_result = ["what is the capital of france"]
        with patch("app.services.rag_engine.settings") as mock_settings, patch(
            "app.services.rag_engine.QueryPlanner", _FakePlanner
        ), patch(
            "app.services.rag_engine.RetrievalEvaluator"
        ) as evaluator_cls, patch(
            "app.services.rag_engine._get_pool",
            side_effect=RuntimeError("no db in test"),
        ):
            self._settings(mock_settings, skip_eval=True)
            await self._drive_to_done(engine)

        evaluator_cls.assert_not_called()

    @pytest.mark.asyncio
    async def test_instant_multi_subquery_with_skip_disabled_invokes_evaluation(self):
        engine = self._make_engine()
        _FakePlanner.plan_result = [
            "what is the capital of france",
            "what is the population of france",
        ]
        fake_evaluator = MagicMock()
        fake_evaluator.evaluate = AsyncMock(return_value="CONFIDENT")
        with patch("app.services.rag_engine.settings") as mock_settings, patch(
            "app.services.rag_engine.QueryPlanner", _FakePlanner
        ), patch(
            "app.services.rag_engine.RetrievalEvaluator",
            return_value=fake_evaluator,
        ) as evaluator_cls, patch(
            "app.services.rag_engine._get_pool",
            side_effect=RuntimeError("no db in test"),
        ):
            self._settings(mock_settings, skip_eval=False)
            await self._drive_to_done(engine)

        evaluator_cls.assert_called_once()
        fake_evaluator.evaluate.assert_awaited_once()


class TestFollowupRewriteModeGating:
    """Instant follow-up rewrite is on by default but operator-skippable."""

    _HISTORY = [
        {"role": "user", "content": "How do I install the CDP server?"},
        {"role": "assistant", "content": "Run the installer as admin."},
    ]

    @pytest.mark.asyncio
    async def test_instant_skip_followup_rewrite_when_flag_set(self):
        engine = _make_query_engine()
        with patch("app.services.rag_engine.settings") as mock_settings, patch(
            "app.services.rag_engine.QueryTransformer"
        ) as qt_cls, patch(
            "app.services.query_transformer.is_followup_query", return_value=True
        ):
            _query_settings(mock_settings)
            # Skip BOTH step-back and follow-up rewrite → QueryTransformer never
            # constructed in Instant even for a follow-up message.
            mock_settings.instant_skip_followup_rewrite = True
            await _drive_query(
                engine,
                "tell me more",
                self._HISTORY,
                stream=False,
                vault_id=1,
                mode=ChatMode.INSTANT,
            )
        qt_cls.assert_not_called()

    @pytest.mark.asyncio
    async def test_instant_runs_followup_rewrite_by_default(self):
        engine = _make_query_engine()
        with patch("app.services.rag_engine.settings") as mock_settings, patch(
            "app.services.rag_engine.QueryTransformer"
        ) as qt_cls, patch(
            "app.services.query_transformer.is_followup_query", return_value=True
        ):
            _query_settings(mock_settings)
            # Default: follow-up rewrite ON in Instant; step-back transform skipped.
            mock_settings.instant_skip_followup_rewrite = False
            instance = qt_cls.return_value
            instance.rewrite_followup = AsyncMock(return_value="install the CDP server")
            await _drive_query(
                engine,
                "tell me more",
                self._HISTORY,
                stream=False,
                vault_id=1,
                mode=ChatMode.INSTANT,
            )
        qt_cls.assert_called_once()
        instance.rewrite_followup.assert_awaited_once()
