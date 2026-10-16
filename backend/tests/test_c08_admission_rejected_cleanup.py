"""Issue #713 (finding T1-32-S2-03): an embedding-admission-rejected turn
must not leak background overlap tasks or un-awaited embed coroutines.

At the base commit ``query()``'s ``except AdmissionRejected`` handler yields
the ADMISSION_REJECTED error chunk and returns WITHOUT calling
``_cancel_overlap_tasks()`` — unlike every sibling early exit — so the
memory/KMS overlap tasks outlive the rejected turn, and the ``_embed_one``
coroutines built ahead of the ``admit(...)`` context are dropped un-awaited
(one ``RuntimeWarning: ... was never awaited`` per variant, the issue #565
gate's exact signal).

The checks below assert the DESIRED cleanup: RED at the base commit
(465f76d2), GREEN once the rejection exit cancels the overlap tasks and
leaves no un-awaited embed coroutine.

Both tests drive the streaming generator inside a
``warnings.catch_warnings(record=True)`` block and ``await agen.aclose()``
inside that block, so any never-awaited coroutine warning is captured by the
test itself instead of aborting the session via the #565 conftest gate.
"""

import asyncio
import gc
import threading
import warnings
from contextlib import asynccontextmanager
from typing import Any, Dict, List
from unittest.mock import patch

import pytest

from app.config import settings
from app.services.admission import AdmissionClass, AdmissionRejected
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


class _BlockingMemoryStore:
    """Memory-store double whose search_memories blocks on a gate (bounded by
    a wait timeout) so the memory overlap task is still in flight when the
    ADMISSION_REJECTED error chunk is yielded."""

    def __init__(self, gate: threading.Event):
        self._gate = gate

    def detect_memory_intent(self, text):
        return None

    def search_memories(self, query, limit=5, vault_id=None, include_global=False):
        self._gate.wait(timeout=5.0)
        return []


class _RejectEmbeddingController:
    """Controller double rejecting every EMBEDDING admit.

    ``AdmissionRejected`` validates its reason against the closed set
    ("queue_full", "deadline_exceeded", "shutdown"), so the fake raises with
    one of those literal reasons.
    """

    @asynccontextmanager
    async def admit(self, admission_class, **kwargs):
        if admission_class is AdmissionClass.EMBEDDING:
            raise AdmissionRejected("queue_full")
        yield


def _make_engine(memory_store) -> RAGEngine:
    engine = RAGEngine()
    engine.embedding_service = _FakeEmbeddingService([0.1, 0.2])
    engine.vector_store = _PassThroughVectorStore(
        [{"text": "chunk", "file_id": "f1", "metadata": {}, "_distance": 0.1}]
    )
    engine.memory_store = memory_store
    engine.llm_client = _FakeLLMClient("answer")
    engine._get_indexed_file_ids = _no_indexed_file_ids
    return engine


def _live_memory_tasks() -> List[asyncio.Task]:
    """Tasks running query()'s ``_memory_retrieve`` overlap coroutine that are
    alive and have not been asked to cancel."""
    live: List[asyncio.Task] = []
    for task in asyncio.all_tasks():
        code = getattr(task.get_coro(), "cr_code", None)
        if code is None or code.co_name != "_memory_retrieve":
            continue
        if task.done() or task.cancelling():
            continue
        live.append(task)
    return live


async def _release_overlap_tasks(release: threading.Event) -> None:
    """Deterministic teardown: unblock the memory-store thread, then cancel
    and await whatever overlap tasks are still alive."""
    release.set()
    leftovers = _live_memory_tasks()
    for task in leftovers:
        task.cancel()
    if leftovers:
        await asyncio.gather(*leftovers, return_exceptions=True)


async def test_rejection_cancels_overlap_tasks():
    """AC8 (issue #713, T1-32-S2-03): by the time the ADMISSION_REJECTED error
    chunk is yielded, every overlap task must have had cancellation requested.
    RED at the base commit (the handler returns without
    ``_cancel_overlap_tasks()``), GREEN after the fix."""
    release = threading.Event()
    engine = _make_engine(_BlockingMemoryStore(release))
    controller = _RejectEmbeddingController()
    saw_rejection = False
    leaked: List[asyncio.Task] = []
    with (
        patch(
            "app.services.rag_engine.get_admission_controller",
            return_value=controller,
        ),
        patch.object(settings, "memory_retrieval_enabled", True),
        patch.object(settings, "query_transformation_enabled", False),
    ):
        agen = engine.query("test query", [], stream=True)
        # Drain inside a warning-capture block: the never-awaited embed
        # coroutines this finding is about must be recorded here, not by the
        # session-wide #565 gate (which would abort the run).
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            try:
                async for chunk in agen:
                    if (
                        chunk.get("type") == "error"
                        and chunk.get("code") == "ADMISSION_REJECTED"
                    ):
                        saw_rejection = True
                        leaked = _live_memory_tasks()
                        break
            finally:
                await agen.aclose()
                await _release_overlap_tasks(release)

    assert saw_rejection, "ADMISSION_REJECTED error chunk never arrived"
    assert len(leaked) == 0, (
        "the AdmissionRejected exit must have requested cancellation of "
        f"every overlap task before the error chunk is yielded; "
        f"{len(leaked)} _memory_retrieve task(s) alive and uncancelled"
    )


async def test_rejection_leaves_no_unawaited_embed_coroutines():
    """AC13 (issue #713, T1-32-S2-03): with query transformation disabled (one
    'original' variant), an AdmissionRejected streaming turn must leave zero
    ``RuntimeWarning``s containing "never awaited" after the generator ends
    and ``gc.collect()`` runs. RED at the base commit (the variant's
    ``_embed_one`` coroutine is dropped), GREEN after the fix."""
    release = threading.Event()
    engine = _make_engine(_BlockingMemoryStore(release))
    controller = _RejectEmbeddingController()
    never_awaited: List[warnings.WarningMessage] = []
    with (
        patch(
            "app.services.rag_engine.get_admission_controller",
            return_value=controller,
        ),
        patch.object(settings, "memory_retrieval_enabled", True),
        patch.object(settings, "query_transformation_enabled", False),
    ):
        agen = engine.query("test query", [], stream=True)
        with warnings.catch_warnings(record=True) as recorded:
            warnings.simplefilter("always")
            try:
                async for _chunk in agen:
                    pass
                await agen.aclose()
            finally:
                await _release_overlap_tasks(release)
            gc.collect()
            never_awaited = [
                w
                for w in recorded
                if issubclass(w.category, RuntimeWarning)
                and "never awaited" in str(w.message)
            ]

    assert len(never_awaited) == 0, (
        "the AdmissionRejected exit must not drop un-awaited embed "
        f"coroutines: {[str(w.message) for w in never_awaited]}"
    )
