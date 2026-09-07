"""Per-query retrieval/citation controls dispatch (issue #510 UI-004/AC-17).

Covers:
- retrieval_mode semantic/keyword/auto mapping to distinct vector-store
  search signatures (hybrid flag, alpha, query text, embedding, limit);
- citation_mode "required" enforcement status in the done message (stream and
  non-stream), and "disabled" stripping the citation instruction from the
  effective system prompt;
- ChatRequest model validation (422 source) rejecting invalid mode strings;
- currency_warnings parity: present in BOTH the streaming done SSE payload
  and the non-stream done payload when a superseded source is retrieved.
"""

from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from app.api.routes.chat import ChatRequest, ChatStreamRequest
from app.services.rag_engine import RAGEngine

UNCITED_ANSWER = "The capital of France is Paris, a city known for its landmarks."
CITED_ANSWER = "The capital of France is Paris. [S1]"


class RecordingVectorStore:
    """Records every search() signature and serves one relevant chunk."""

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

    def __init__(self, answer=UNCITED_ANSWER):
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
    mock.hybrid_search_enabled = True  # so "auto" differs from "semantic"
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
    async for chunk in engine.query("what is the capital", [], vault_id=None,
                                    **query_kwargs):
        if chunk.get("type") == "done":
            done = chunk
        elif chunk.get("type") == "content":
            content.append(chunk.get("content", ""))
    assert done is not None, "query() never produced a done message"
    return done, "".join(content)


def _hashable(value):
    return tuple(value) if isinstance(value, list) else value


def _signature(store):
    """The (hybrid, alpha, query_text, embedding, limit) tuple of the single search."""
    assert len(store.search_calls) == 1
    call = store.search_calls[0]
    return (
        call["hybrid"],
        call["hybrid_alpha"],
        call["query_text"],
        call["embedding"],
        call["limit"],
    )


class TestRetrievalModeSearchSignatures:
    """semantic/keyword/auto map to pairwise-distinct search signatures."""

    def _engine(self, store):
        engine = RAGEngine.__new__(RAGEngine)
        engine.vector_store = store
        engine.reranking_service = None
        engine._retrieval_evaluators = {}
        return engine

    @pytest.mark.asyncio
    async def test_modes_produce_pairwise_distinct_signatures(self):
        store_by_mode = {}
        with patch("app.services.rag_engine.settings") as mock_settings:
            _apply_settings(mock_settings)
            for mode in ("semantic", "keyword", "auto"):
                store = RecordingVectorStore()
                engine = self._engine(store)
                await engine._execute_retrieval(
                    [("original", [0.1, 0.2, 0.3])],
                    "what is the capital",
                    vault_id=1,
                    retrieval_mode=mode,
                )
                store_by_mode[mode] = _signature(store)

        sem, key, auto = (
            store_by_mode["semantic"],
            store_by_mode["keyword"],
            store_by_mode["auto"],
        )
        # semantic: dense-only (hybrid off), settings alpha preserved.
        assert sem[0] is False
        # keyword: pure lexical — hybrid on with alpha exactly 0.0.
        assert key[0] is True
        assert key[1] == 0.0
        # auto: settings defaults — hybrid on with the configured alpha.
        assert auto[0] is True
        assert auto[1] == 0.6
        # Pairwise distinct signatures overall.
        signatures = {tuple(map(_hashable, sem)), tuple(map(_hashable, key)), tuple(map(_hashable, auto))}
        assert len(signatures) == 3, (
            f"Retrieval modes must be pairwise distinct: {store_by_mode}"
        )
        # Common essentials: real query text, real embedding, sane limit.
        for sig in (sem, key, auto):
            assert sig[2] == "what is the capital"
            assert sig[3] == [0.1, 0.2, 0.3]
            assert sig[4] == 10

    @pytest.mark.asyncio
    async def test_stale_engine_state_attributes_are_ignored(self):
        """PRR-001 regression pin: no per-request engine state exists.

        _execute_retrieval takes controls ONLY from explicit parameters.
        Stale ``engine._active_*`` attributes (as a prior implementation
        would have left behind from another concurrent request) must be
        ignored — the call below runs with engine defaults (settings-driven
        hybrid), not the stale "keyword".
        """
        with patch("app.services.rag_engine.settings") as mock_settings:
            _apply_settings(mock_settings)
            store = RecordingVectorStore()
            engine = self._engine(store)
            engine._active_retrieval_mode = "keyword"  # stale, must be ignored
            await engine._execute_retrieval(
                [("original", [0.1, 0.2, 0.3])],
                "what is the capital",
                vault_id=1,
            )
        sig = _signature(store)
        assert sig[0] is True and sig[1] == 0.6

    @pytest.mark.asyncio
    async def test_none_mode_falls_back_to_settings_hybrid(self):
        with patch("app.services.rag_engine.settings") as mock_settings:
            _apply_settings(mock_settings)
            store = RecordingVectorStore()
            engine = self._engine(store)
            await engine._execute_retrieval(
                [("original", [0.1, 0.2, 0.3])],
                "what is the capital",
                vault_id=1,
                retrieval_mode=None,
            )
        sig = _signature(store)
        assert sig[0] is True and sig[1] == 0.6


class TestCitationModeEnforcement:
    @pytest.mark.asyncio
    async def test_required_uncited_answer_flags_missing_citations_nonstream(self):
        client = _StubLLMClient(answer=UNCITED_ANSWER)
        store = RecordingVectorStore()
        engine = _make_engine(client, store)
        with patch("app.services.rag_engine.settings") as mock_settings, patch(
            "app.services.rag_engine._get_pool",
            side_effect=RuntimeError("no db in test"),
        ):
            _apply_settings(mock_settings)
            done, _ = await _collect(engine, stream=False, citation_mode="required")

        assert done.get("sources"), "test requires at least one retrieved source"
        enforcement = done.get("citation_enforcement")
        assert enforcement == {
            "mode": "required",
            "status": "missing_citations",
            "detail": enforcement["detail"],  # free-form, asserted non-empty below
        }
        assert enforcement["detail"]

    @pytest.mark.asyncio
    async def test_required_uncited_answer_flags_missing_citations_stream(self):
        client = _StubLLMClient(answer=UNCITED_ANSWER)
        store = RecordingVectorStore()
        engine = _make_engine(client, store)
        with patch("app.services.rag_engine.settings") as mock_settings, patch(
            "app.services.rag_engine._get_pool",
            side_effect=RuntimeError("no db in test"),
        ):
            _apply_settings(mock_settings)
            done, _ = await _collect(engine, stream=True, citation_mode="required")

        assert done.get("citation_enforcement", {}).get("status") == "missing_citations"
        assert done["citation_enforcement"]["mode"] == "required"

    @pytest.mark.asyncio
    async def test_required_cited_answer_is_satisfied(self):
        client = _StubLLMClient(answer=CITED_ANSWER)
        store = RecordingVectorStore()
        engine = _make_engine(client, store)
        with patch("app.services.rag_engine.settings") as mock_settings, patch(
            "app.services.rag_engine._get_pool",
            side_effect=RuntimeError("no db in test"),
        ):
            _apply_settings(mock_settings)
            done, _ = await _collect(engine, stream=False, citation_mode="required")

        assert done.get("citation_enforcement") == {
            "mode": "required",
            "status": "satisfied",
        }

    @pytest.mark.asyncio
    async def test_no_enforcement_key_without_required_mode(self):
        client = _StubLLMClient(answer=UNCITED_ANSWER)
        engine = _make_engine(client, RecordingVectorStore())
        with patch("app.services.rag_engine.settings") as mock_settings, patch(
            "app.services.rag_engine._get_pool",
            side_effect=RuntimeError("no db in test"),
        ):
            _apply_settings(mock_settings)
            done, _ = await _collect(engine, stream=False)
        assert "citation_enforcement" not in done


class TestCitationModeDisabledPrompt:
    @pytest.mark.asyncio
    async def test_disabled_strips_citation_instruction_from_system_prompt(self):
        client = _StubLLMClient()
        engine = _make_engine(client, RecordingVectorStore())
        with patch("app.services.rag_engine.settings") as mock_settings, patch(
            "app.services.rag_engine._get_pool",
            side_effect=RuntimeError("no db in test"),
        ):
            _apply_settings(mock_settings)
            await _collect(engine, stream=False, citation_mode="disabled")

        system_prompt = client.completion_messages[0]["content"]
        assert "When answering questions based on the provided context" not in system_prompt
        assert "Document citations: use [S1]" not in system_prompt

    @pytest.mark.asyncio
    async def test_disabled_strips_instruction_on_stream_path(self):
        client = _StubLLMClient()
        engine = _make_engine(client, RecordingVectorStore())
        with patch("app.services.rag_engine.settings") as mock_settings, patch(
            "app.services.rag_engine._get_pool",
            side_effect=RuntimeError("no db in test"),
        ):
            _apply_settings(mock_settings)
            await _collect(engine, stream=True, citation_mode="disabled")

        system_prompt = client.stream_messages[0]["content"]
        assert "When answering questions based on the provided context" not in system_prompt
        assert "Document citations: use [S1]" not in system_prompt

    @pytest.mark.asyncio
    async def test_default_prompt_still_contains_citation_instruction(self):
        client = _StubLLMClient()
        engine = _make_engine(client, RecordingVectorStore())
        with patch("app.services.rag_engine.settings") as mock_settings, patch(
            "app.services.rag_engine._get_pool",
            side_effect=RuntimeError("no db in test"),
        ):
            _apply_settings(mock_settings)
            await _collect(engine, stream=False)

        system_prompt = client.completion_messages[0]["content"]
        assert "When answering questions based on the provided context" in system_prompt
        assert "Document citations: use [S1]" in system_prompt


class TestChatRequestModeValidation:
    """Invalid mode strings are rejected at the request model (422 source)."""

    def test_invalid_retrieval_mode_rejected(self):
        with pytest.raises(ValidationError):
            ChatRequest(message="hi", retrieval_mode="bogus")

    def test_invalid_citation_mode_rejected(self):
        with pytest.raises(ValidationError):
            ChatRequest(message="hi", citation_mode="sometimes")

    def test_valid_modes_accepted(self):
        for mode in ("auto", "semantic", "keyword"):
            ChatRequest(message="hi", retrieval_mode=mode)
        for mode in ("enabled", "disabled", "required"):
            ChatRequest(message="hi", citation_mode=mode)

    def test_stream_request_invalid_retrieval_mode_rejected(self):
        with pytest.raises(ValidationError):
            ChatStreamRequest(
                messages=[{"role": "user", "content": "hi"}],
                retrieval_mode="semantic-ish",
            )

    def test_stream_request_invalid_citation_mode_rejected(self):
        with pytest.raises(ValidationError):
            ChatStreamRequest(
                messages=[{"role": "user", "content": "hi"}],
                citation_mode="forced",
            )


# ---------------------------------------------------------------------------
# Done-message currency_warnings parity (stream + non-stream)
# ---------------------------------------------------------------------------


def _supersession_pool():
    """Mock pool: files table has supersedes_file_id and a newer file exists.

    Cribbed from test_supersession_cache.py's fixture pattern. The
    ``get_connection`` path serves the indexed-file-ids lookup
    (``_get_indexed_file_ids``) so the retrieved chunk (file_id "1") passes
    the atomic-visibility filter and reaches the supersession check.
    """
    mock_pool = MagicMock()
    mock_conn = MagicMock()
    mock_pool.connection.return_value.__enter__ = MagicMock(return_value=mock_conn)
    mock_pool.connection.return_value.__exit__ = MagicMock(return_value=False)

    # _get_indexed_file_ids: SELECT id FROM files WHERE status='indexed' —
    # must include the retrieved chunk's file_id ("1") as dict-like rows.
    indexed_conn = MagicMock()
    indexed_cursor = MagicMock()
    indexed_cursor.fetchall.return_value = [{"id": 1}]
    indexed_conn.execute.return_value = indexed_cursor
    mock_pool.get_connection.return_value = indexed_conn

    pragma_cursor = MagicMock()
    pragma_cursor.fetchall.return_value = [
        (0, "id", "TEXT", 0, None, 0),
        (1, "file_name", "TEXT", 0, None, 0),
        (2, "supersedes_file_id", "TEXT", 0, None, 0),
        (3, "status", "TEXT", 0, None, 0),
    ]
    query_cursor = MagicMock()
    query_cursor.fetchall.return_value = [("report_v2.pdf",)]
    mock_conn.execute.side_effect = [pragma_cursor, query_cursor]
    return mock_pool


class TestCurrencyWarningsDoneParity:
    @pytest.mark.asyncio
    async def test_nonstream_done_payload_carries_currency_warnings(self):
        engine = _make_engine(_StubLLMClient(), RecordingVectorStore())
        with patch("app.services.rag_engine.settings") as mock_settings, patch(
            "app.services.rag_engine._get_pool",
            return_value=_supersession_pool(),
        ):
            _apply_settings(mock_settings)
            done, _ = await _collect(engine, stream=False)

        warnings = done.get("currency_warnings")
        assert warnings, "non-stream done payload must carry currency warnings"
        assert any("superseded" in w for w in warnings)

    @pytest.mark.asyncio
    async def test_stream_done_sse_payload_carries_currency_warnings(self):
        """The stream=True done dict is exactly what the SSE route serializes
        into the terminal event — it must carry the same currency warnings."""
        engine = _make_engine(_StubLLMClient(), RecordingVectorStore())
        with patch("app.services.rag_engine.settings") as mock_settings, patch(
            "app.services.rag_engine._get_pool",
            return_value=_supersession_pool(),
        ):
            _apply_settings(mock_settings)
            done, _ = await _collect(engine, stream=True)

        warnings = done.get("currency_warnings")
        assert warnings, "streaming done payload must carry currency warnings"
        assert any("superseded" in w for w in warnings)
