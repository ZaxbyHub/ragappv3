"""Issue #713 (finding T1-32-S2-02): every provider embed call must run
inside the shared EMBEDDING admission budget (issue #518 E3).

At the base commit the only ``AdmissionClass.EMBEDDING`` admit site is
``query()``'s variant gather: ``_build_query_embeddings`` (used by the
retrieval-eval / ``query_retrieve_only`` / ``retrieve_sources`` surfaces) and
``_orchestrate_sub_query_retrieval``'s sub-query embed gather both call the
embedding service with no ``admit(...)`` at all, so those embeds are invisible
to the process-wide device budget.

The checks below assert the DESIRED wiring: RED at the base commit
(465f76d2), GREEN once both embed gathers acquire the EMBEDDING admission.
"""

from contextlib import asynccontextmanager
from typing import Any, Dict, List
from unittest.mock import patch

import pytest

from app.config import settings
from app.services.admission import AdmissionClass
from app.services.rag_engine import RAGEngine


@pytest.fixture(autouse=True)
def _patch_ssrf():
    """Prevent SSRF guard from blocking service construction in tests."""
    with patch("app.services.embeddings.assert_url_safe"), patch(
        "app.services.llm_client.assert_url_safe"
    ):
        yield


def _no_indexed_file_ids(vault_id=None):
    """Visibility filter stub — a developer's backend/data/app.db must not
    filter the fixture's file ids (known poisoning trap)."""
    return None


class _FakeEmbeddingService:
    def __init__(self, embedding: List[float]):
        self._embedding = embedding

    async def embed_single(self, text: str) -> List[float]:
        return self._embedding

    async def embed_passage(self, text: str) -> List[float]:
        return self._embedding


class _FakeLLMClient:
    def __init__(self, response: str):
        self._response = response

    async def chat_completion(self, messages, **kwargs) -> str:
        return self._response


class _NoopMemoryStore:
    def detect_memory_intent(self, text):
        return None

    def search_memories(self, query, limit=5, vault_id=None, include_global=False):
        return []


class _PassThroughVectorStore:
    """Vector-store double returning the fixture chunks from every search."""

    def __init__(self, results: List[Dict[str, Any]]):
        self._results = results

    async def search(
        self,
        embedding,
        limit: int = 10,
        filter_expr=None,
        vault_id=None,
        query_text=None,
        hybrid=False,
        **kwargs,
    ):
        return self._results[:limit]

    async def get_chunks_by_uid(self, chunk_uids):
        # Match the real VectorStore interface — no adjacent chunks in fake.
        return []

    def get_fts_exceptions(self) -> int:
        return 0


class _CountingAdmissionController:
    """Controller double counting admit() entries per AdmissionClass."""

    def __init__(self):
        self.counts: Dict[Any, int] = {}

    @asynccontextmanager
    async def admit(self, admission_class, **kwargs):
        self.counts[admission_class] = self.counts.get(admission_class, 0) + 1
        yield


def _make_engine() -> RAGEngine:
    engine = RAGEngine(
        embedding_service=_FakeEmbeddingService([0.1, 0.2]),
        vector_store=_PassThroughVectorStore(
            [{"text": "chunk", "file_id": "f1", "metadata": {}, "_distance": 0.1}]
        ),
        memory_store=_NoopMemoryStore(),
        llm_client=_FakeLLMClient("answer"),
    )
    engine._get_indexed_file_ids = _no_indexed_file_ids
    return engine


async def test_eval_embed_is_admitted():
    """AC6 (issue #713, T1-32-S2-02): ``retrieve_eval_results`` must acquire at
    least one EMBEDDING admission around its provider embed calls. RED at the
    base commit (zero admissions), GREEN after the fix."""
    controller = _CountingAdmissionController()
    engine = _make_engine()
    with (
        patch(
            "app.services.rag_engine.get_admission_controller",
            return_value=controller,
        ),
        patch.object(settings, "query_transformation_enabled", False),
    ):
        await engine.retrieve_eval_results("test query")

    assert controller.counts.get(AdmissionClass.EMBEDDING, 0) >= 1, (
        "the eval-path query embed must run inside the shared EMBEDDING "
        f"admission budget, saw {controller.counts!r}"
    )


async def test_decomposition_embeds_are_admitted():
    """AC7 (issue #713, T1-32-S2-02): a two-sub-query decomposition plan whose
    sub-queries differ from the already-embedded original must acquire at
    least one EMBEDDING admission around those sub-query embeds. RED at the
    base commit (zero admissions), GREEN after the fix."""
    controller = _CountingAdmissionController()
    engine = _make_engine()
    with patch(
        "app.services.rag_engine.get_admission_controller",
        return_value=controller,
    ):
        await engine._orchestrate_sub_query_retrieval(
            plan=[
                "How is the indexing pipeline deployed?",
                "How is the indexing pipeline monitored?",
            ],
            vault_id=1,
            user_input="How is the indexing pipeline operated in production?",
            effective_alpha=0.6,
            variants_dropped=[],
            effective_initial_top_k=10,
            effective_reranker_top_n=5,
            active_client=None,
            mode=None,
        )

    assert controller.counts.get(AdmissionClass.EMBEDDING, 0) >= 1, (
        "the decomposition sub-query embeds must run inside the shared "
        f"EMBEDDING admission budget, saw {controller.counts!r}"
    )
