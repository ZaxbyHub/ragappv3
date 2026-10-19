"""Acceptance checks for issue #908 (C15): the variant_type sub-query leak.

``RAGEngine._execute_retrieval`` binds ``variant_type = query_embeddings[i][0]``,
which on the multi-sub-query orchestrator path is the RAW SUB-QUERY STRING,
and logs it untruncated at WARNING when ``vector_store.search`` raises. Same
defect class as #710 (#287 contract: no raw user-derived text at default log
level), different site — the name ``variant_type`` hides its user-text nature
from the name-based AST guard.

- AC1 (C4): no WARNING-or-higher record of ``app.services.rag_engine`` may
  contain the sub-query text when a variant's search raises. RED at base,
  GREEN after the fix.
- AC2 (C5): the failure WARNING must keep the variant INDEX and the exception
  text so operators keep the operational signal. GREEN at base, GREEN after
  the fix.

Fake services and the settings fixture mirror
``tests/test_c05_subquery_log_redaction.py`` /
``tests/test_multi_sub_query_aggregate_status.py``. The exception text
deliberately carries a different marker (C10ERR) than the sub-query text
(C10VT) so a leak can only be attributed to the sub-query itself.
"""

import logging
import os
import sys
from contextlib import contextmanager
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

# Add parent directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Stub optional dependencies same as other test files
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

try:
    from unstructured.partition.auto import partition
except ImportError:
    import types

    _unstructured = types.ModuleType("unstructured")
    _unstructured.partition = types.ModuleType("unstructured.partition")
    _unstructured.partition.auto = types.ModuleType("unstructured.partition.auto")
    _unstructured.partition.auto.partition = lambda *args, **kwargs: []
    sys.modules["unstructured"] = _unstructured
    sys.modules["unstructured.partition"] = _unstructured.partition
    sys.modules["unstructured.partition.auto"] = _unstructured.partition.auto

from app.services.rag_engine import RAGEngine, RAGEngineError

RAG_ENGINE_LOGGER = "app.services.rag_engine"

# Markers: the sub-query text carries C10VT (the leak), the raised exception
# carries C10ERR (the preserved operational signal). Neither contains the other.
SUB_QUERY_MARKER = "C10VT"
EXCEPTION_MARKER = "C10ERR"


class _ListHandler(logging.Handler):
    """Collect every record at or above its level; no assertion semantics."""

    def __init__(self, level: int):
        super().__init__(level=level)
        self.records = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@contextmanager
def capture_records(logger_name: str, level: int = logging.INFO):
    """Capture records of ``logger_name`` at ``level``+ without failing when
    the logger stays silent (unlike ``assertLogs``)."""
    lg = logging.getLogger(logger_name)
    handler = _ListHandler(level)
    previous_level = lg.level
    lg.setLevel(level)
    lg.addHandler(handler)
    try:
        yield handler
    finally:
        lg.removeHandler(handler)
        lg.setLevel(previous_level)


class FakeEmbeddingService:
    """Returns a deterministic embedding based on call count."""

    def __init__(self):
        self._counter = 0

    async def embed_single(self, text: str) -> List[float]:
        self._counter += 1
        base = [0.1, 0.2, 0.3, 0.4]
        return [b + self._counter * 0.01 for b in base]

    async def embed_passage(self, text: str) -> List[float]:
        return await self.embed_single(text)


class FailingVectorStore:
    """Every search raises a non-semaphore RuntimeError (carries C10ERR, never
    C10VT) so every sub-query variant takes the warning-and-degrade path."""

    def __init__(self):
        self.search_calls = 0

    async def search(
        self,
        embedding: List[float],
        limit: int = 10,
        vault_id=None,
        query_text=None,
        hybrid=False,
        **kwargs,
    ) -> List[Dict[str, Any]]:
        self.search_calls += 1
        raise RuntimeError(f"vector backend unavailable ({EXCEPTION_MARKER})")

    def get_fts_exceptions(self) -> int:
        return 0


class FakeMemoryStore:
    def detect_memory_intent(self, query: str) -> Optional[str]:
        return None

    async def search_memories(
        self, query: str, top_k: int, vault_id=None, include_global: bool = False
    ) -> List:
        return []


class FakeLLMClient:
    """Returns a canned JSON list for every chat_completion call."""

    def __init__(self, plan: List[str]):
        self._plan = plan

    async def chat_completion(self, messages, max_tokens=None, temperature=None):
        import json

        return json.dumps(self._plan)


def _make_engine(
    embedding_service: Any,
    vector_store: Any,
    llm_client: Any,
    memory_store: Any,
) -> RAGEngine:
    with patch("app.services.embeddings.assert_url_safe"), patch(
        "app.services.llm_client.assert_url_safe"
    ):
        engine = RAGEngine(
            embedding_service=embedding_service,
            vector_store=vector_store,
            memory_store=memory_store,
            llm_client=llm_client,
        )
    engine.retrieval_top_k = 10
    engine.vector_store = vector_store
    engine._get_indexed_file_ids = MagicMock(return_value=None)
    return engine


@pytest.fixture
def patched_settings_fixture():
    """Patch settings for isolated testing (mirrors the c05 suite)."""
    mock_settings = MagicMock()
    mock_settings.agentic_rag_enabled = False
    mock_settings.query_transformation_enabled = False
    mock_settings.memory_retrieval_enabled = False
    mock_settings.context_distillation_enabled = False
    mock_settings.parent_retrieval_enabled = False
    mock_settings.retrieval_evaluation_enabled = False
    mock_settings.context_max_tokens = 0
    mock_settings.rag_relevance_threshold = None
    mock_settings.max_distance_threshold = None
    mock_settings.rrf_legacy_mode = True
    mock_settings.multi_query_rrf_k = 60
    mock_settings.rrf_weight_original = 1.0
    mock_settings.rrf_weight_stepback = 1.0
    mock_settings.rrf_weight_hyde = 1.0
    mock_settings.retrieval_recency_weight = 0.0
    mock_settings.exact_match_promote = False
    mock_settings.hybrid_search_enabled = False
    mock_settings.reranking_enabled = False
    mock_settings.parent_retrieval_max_sources = 10
    mock_settings.distillation_similarity_threshold = 0.0
    mock_settings.chunk_size_chars = 512
    mock_settings.chunk_overlap_chars = 128
    mock_settings.retrieval_window = 0
    mock_settings.instant_skip_followup_rewrite = False
    mock_settings.instant_skip_query_transformation = False
    mock_settings.instant_skip_retrieval_evaluation = False
    mock_settings.wiki_retrieval_enabled = False
    mock_settings.kms_enabled = False
    mock_settings.maintenance_mode = False
    mock_settings.rag_trace_in_response = False

    with patch("app.services.rag_engine.settings", mock_settings):
        yield mock_settings


async def _run_failing_orchestration() -> None:
    """Drive the multi-sub-query orchestrator with an always-failing vector
    store: both plan items embed fine, then every per-sub-query retrieval
    takes the warning-and-degrade branch inside ``_execute_retrieval``.

    The all-variants-failed fallback then retries as the hardcoded
    ``'original'`` variant, whose search failure legitimately re-raises
    RAGEngineError (unchanged degradation semantics, explicitly out of #908
    scope) — so that terminal exception is caught here: the checks below
    assert on the per-sub-query WARNING records emitted before it, and must
    stay decidable by the marker assertion alone.
    """
    plan = ["plain question", f"{SUB_QUERY_MARKER} facet"]
    engine = _make_engine(
        FakeEmbeddingService(),
        FailingVectorStore(),
        FakeLLMClient(plan),
        FakeMemoryStore(),
    )
    try:
        await engine._orchestrate_sub_query_retrieval(
            plan=plan,
            vault_id=1,
            user_input="test question",
            effective_alpha=0.6,
            variants_dropped=[],
            effective_initial_top_k=10,
            effective_reranker_top_n=10,
            active_client=None,
            mode=None,
        )
    except RAGEngineError:
        pass


async def test_variant_type_not_logged_on_search_failure(patched_settings_fixture):
    """AC1: no WARNING+ rag_engine record carries the sub-query text.

    RED at base (the ``vector search variant=%d (%s) failed: %s`` WARNING
    interpolates ``variant_type``, which holds the raw sub-query string on
    this path), GREEN after the fix.
    """
    with capture_records(RAG_ENGINE_LOGGER, logging.INFO) as captured:
        await _run_failing_orchestration()

    leaks = [
        r for r in captured.records if SUB_QUERY_MARKER in r.getMessage()
    ]
    assert leaks == [], (
        f"{len(leaks)} INFO+ record(s) of {RAG_ENGINE_LOGGER} carry the raw "
        f"sub-query text via the variant label (violates the #287/#710/#908 "
        f"contract): {[r.getMessage() for r in leaks]}"
    )


async def test_failure_warning_still_carries_index_and_error(patched_settings_fixture):
    """AC2: the failure WARNING keeps the variant index and exception text.

    GREEN at base, GREEN after the fix — redaction must not silence the
    operational signal. Each per-sub-query ``_execute_retrieval`` call sees a
    single-element variant list, so the variant index inside that call is 0.
    """
    with capture_records(RAG_ENGINE_LOGGER, logging.WARNING) as captured:
        await _run_failing_orchestration()

    warnings = [r.getMessage() for r in captured.records]
    assert any(
        "variant=0" in message and EXCEPTION_MARKER in message
        for message in warnings
    ), (
        "a failed vector-search variant must still log a WARNING carrying the "
        f"variant index and the exception text; captured: {warnings}"
    )
