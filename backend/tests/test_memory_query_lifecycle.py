"""Issue #939 acceptance checks: the memory-query precompute must be a
bounded, cancellable, budget-accounted step of a chat turn.

DISCRIMINATING suite — every check is RED at the base tree and GREEN after
the fix, each with its base signature named in the test docstring: AC1
budget (TimeoutError from the check's own wait_for), AC2 (``assert 200 ==
422``), AC3 (generator-close leg ``assert False is True``; the
admission-rejected leg is already fixed and must stay green), AC4
(``assert 0 == 1``). All doubles keep the turn on-loop: no network.
"""

import asyncio
import logging
import os
import sys
import types
from contextlib import asynccontextmanager, contextmanager
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

for _mod in ("lancedb", "pyarrow"):
    if _mod not in sys.modules:
        try:
            __import__(_mod)
        except ImportError:
            sys.modules[_mod] = types.ModuleType(_mod)

if "lancedb.index" not in sys.modules:
    _ldb_idx = types.ModuleType("lancedb.index")
    _ldb_idx.FTS = type("FTS", (), {})
    _ldb_idx.IvfPq = type("IvfPq", (), {})
    sys.modules["lancedb.index"] = _ldb_idx

try:
    from unstructured.partition.auto import partition  # noqa: F401
except ImportError:
    for _sub in ("partition", "partition.auto", "chunking", "chunking.title",
                 "documents", "documents.elements"):
        sys.modules.setdefault(f"unstructured.{_sub}", types.ModuleType(f"unstructured.{_sub}"))
    sys.modules.setdefault("unstructured", types.ModuleType("unstructured"))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import (
    get_current_active_user,
    get_db,
    get_evaluate_policy,
    get_memory_store,
    require_model_ready,
)
from app.api.routes import memories as memories_routes
from app.config import settings
from app.services.admission import AdmissionClass, AdmissionRejected
from app.services.memory_store import MemoryRecord
from app.services.rag_engine import STAGE_SEARCHING, RAGEngine

# ----------------------------------------------------------------- doubles


class _OkEmbedding:
    """Engine embedding service: always succeeds (not under test here)."""

    async def embed_single(self, text):
        return [0.1, 0.2]

    async def embed_passage(self, text):
        return [0.1, 0.2]

    async def embed_batch(self, texts):
        return [[0.1, 0.2] for _ in texts]


class _PassThroughVectorStore:
    """Vector-store double returning one fixture chunk from every search."""

    async def search(self, embedding, limit=10, **kwargs):
        text = "chunk one sentence. chunk two sentence."
        return [{"text": text, "file_id": "f1",
                 "metadata": {"raw_text": text}, "score": 0.9}]

    async def get_chunks_by_uid(self, chunk_uids):
        return []

    def get_fts_exceptions(self) -> int:
        return 0


class _OkClient:
    """LLM client double: succeeds on both completion shapes."""

    base_url = "http://provider-a"
    model = "fake-model"

    async def chat_completion(self, messages, **kwargs) -> str:
        return "ok answer"

    async def chat_completion_stream(self, messages, **kwargs):
        yield "ok answer"


def _memory_record() -> MemoryRecord:
    return MemoryRecord(id=1, content="quarterly roadmap planning notes",
                        category="task", tags=None, source=None, vault_id=1,
                        score=1.0, score_type="fts")


class _FakeMemoryStore:
    """Memory-store double for the engine path; its ``embed_query`` is the
    thing that hangs. ``hang=True``: awaits a never-completing future until
    cancelled, recording whether it observed asyncio.CancelledError
    (AC1/AC3). ``hang=False``: returns promptly (AC4 — the admission
    budget, not the timeout, is under test)."""

    def __init__(self, hang: bool = True):
        self.embedding_service = SimpleNamespace()  # engine gates on presence
        self.hang = hang
        self.started = asyncio.Event()
        self.cancelled_seen = asyncio.Event()
        self.cancelled = False
        self.embed_calls: List[str] = []
        self.search_calls: List[Dict[str, Any]] = []

    def detect_memory_intent(self, text):
        return None

    async def embed_query(self, query):
        self.embed_calls.append(query)
        if not self.hang:
            return [1.0, 0.0]
        hang = asyncio.get_running_loop().create_future()
        self.started.set()
        try:
            await hang
        except asyncio.CancelledError:
            self.cancelled = True
            self.cancelled_seen.set()
            raise
        return [1.0, 0.0]

    def search_memories(self, query, limit=5, **kwargs):
        self.search_calls.append({"query": query, "kwargs": dict(kwargs)})
        return [_memory_record()]


class _CountingAdmissionController:
    """Admission double counting EMBEDDING admissions; rejects them all
    when ``reject_embedding`` (otherwise a permissive recorder, isolating
    tests from the process singleton)."""

    def __init__(self, reject_embedding: bool = False):
        self.embedding_admits = 0
        self._reject_embedding = reject_embedding

    @asynccontextmanager
    async def admit(self, admission_class, **kwargs):
        if admission_class is AdmissionClass.EMBEDDING:
            self.embedding_admits += 1
            if self._reject_embedding:
                raise AdmissionRejected("queue_full")
        yield SimpleNamespace(admission_class=admission_class, holder="test")


class _RejectVariantEmbedsController:
    """Admission double that rejects the VARIANT-embed EMBEDDING admission
    only. The memory-path admission (whenever the fix adds one) runs inside
    the memory overlap TASK, so it is recognized via the current task's
    coroutine name and let through; the variant admission waits until the
    memory embed is deterministically in flight, then rejects — the early
    exit fires exactly when the memory task is cancellable."""

    def __init__(self, memory_started: asyncio.Event):
        self._memory_started = memory_started

    @asynccontextmanager
    async def admit(self, admission_class, **kwargs):
        if admission_class is AdmissionClass.EMBEDDING and (
            not _current_task_is_memory_retrieve()
        ):
            await asyncio.wait_for(self._memory_started.wait(), 5.0)
            raise AdmissionRejected("queue_full")
        yield SimpleNamespace(admission_class=admission_class, holder="test")


# ----------------------------------------------------------------- helpers


def _current_task_is_memory_retrieve() -> bool:
    task = asyncio.current_task()
    code = getattr(task.get_coro(), "cr_code", None) if task else None
    return code is not None and code.co_name == "_memory_retrieve"


def _make_engine(memory_store) -> RAGEngine:
    engine = RAGEngine(
        embedding_service=_OkEmbedding(),
        vector_store=_PassThroughVectorStore(),
        memory_store=memory_store,
        llm_client=_OkClient(),
    )
    # A developer's backend/data/app.db must not filter fixture chunks.
    engine._get_indexed_file_ids = lambda vault_id=None: None
    return engine


def _live_memory_tasks() -> List[asyncio.Task]:
    """Alive, not-yet-cancelled tasks running query()'s _memory_retrieve."""
    live: List[asyncio.Task] = []
    for task in asyncio.all_tasks():
        code = getattr(task.get_coro(), "cr_code", None)
        if code is None or code.co_name != "_memory_retrieve":
            continue
        if not task.done() and not task.cancelling():
            live.append(task)
    return live


async def _cancel_leftover_memory_tasks() -> None:
    """Deterministic teardown so no pending memory task outlives the test."""
    leftovers = _live_memory_tasks()
    for task in leftovers:
        task.cancel()
    if leftovers:
        await asyncio.gather(*leftovers, return_exceptions=True)


async def _close_and_cleanup(agen) -> None:
    try:
        await agen.aclose()
    except Exception:  # noqa: BLE001 — teardown only
        pass
    await _cancel_leftover_memory_tasks()



async def _collect(agen) -> List[Dict[str, Any]]:
    return [chunk async for chunk in agen]


def _warning_messages(caplog) -> List[str]:
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]


async def _poll_until(event: asyncio.Event, timeout: float) -> bool:
    """Bounded window (short sleeps — the base failure must be the plain
    assert, never a raising wait_for TimeoutError)."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if event.is_set():
            return True
        await asyncio.sleep(0.02)
    return event.is_set()


@contextmanager
def _engine_patches():
    """Common settings for every engine-driving test in this suite."""
    with patch.object(settings, "memory_retrieval_enabled", True), \
            patch.object(settings, "query_transformation_enabled", False):
        yield


def _pin_embed_budget(monkeypatch) -> None:
    """Set the suggested budget knob ONLY once the fix has introduced it:
    the pre-fix pydantic Settings rejects unknown attributes (a plain
    setattr raises — verified at base), so at base this is a no-op and at
    head it pins a 0.3 s budget, far under the 10 s check ceiling."""
    if hasattr(settings, "memory_embed_budget_seconds"):
        monkeypatch.setattr(settings, "memory_embed_budget_seconds", 0.3)


@contextmanager
def _memory_admission_patches(controller):
    """AC4 turn patches: both possible admission seams point at the same
    recorder (rag_engine today; embeddings.py too, created on demand, in
    case the fix lands the admission inside EmbeddingService), and raw
    document RAG is off so every EMBEDDING admission observed is
    attributable to the memory precompute."""
    with patch(
        "app.services.rag_engine.get_admission_controller", return_value=controller,
    ), patch(
        "app.services.embeddings.get_admission_controller",
        lambda: controller,
        create=True,
    ), patch(
        "app.services.rag_engine._raw_rag_required",
        lambda query_type, wiki_evidence: False,
    ), _engine_patches():
        yield


async def test_memory_precompute_degrades_to_fts_within_budget(caplog, monkeypatch):
    """AC1 (#939), DISCRIMINATING. RED at base: nothing bounds the memory
    precompute's embed await anywhere on the chain (the engine awaits the
    memory task unconditionally; the embedding client's 60 s timeout is per
    HTTP operation, not per turn), so the turn driven here never completes
    and the check's own asyncio.wait_for(..., 10.0) fires with TimeoutError.
    GREEN after the fix: the turn completes FTS-only within the configured
    budget and the degradation WARNING is asserted by name on the memory
    path ("Memory query embedding ..."). The knob
    ``memory_embed_budget_seconds`` is set only when present: inert at
    base, 0.3 s at head (see ``_pin_embed_budget``)."""
    store = _FakeMemoryStore(hang=True)
    engine = _make_engine(store)
    _pin_embed_budget(monkeypatch)
    with caplog.at_level(logging.WARNING), _engine_patches():
        agen = engine.query("quarterly roadmap status", [], stream=True)
        try:
            chunks = await asyncio.wait_for(_collect(agen), 10.0)
        finally:
            await _close_and_cleanup(agen)

    assert chunks, "the turn must produce chunks once it completes (head leg)"
    warning_msgs = _warning_messages(caplog)
    assert any("Memory query embedding" in m for m in warning_msgs), (
        "a memory embed that exceeds the total budget must surface a WARNING "
        f"on the memory path (fragment 'Memory query embedding'); got {warning_msgs}"
    )
    assert store.search_calls, (
        "the memory leg must still complete FTS-only after the budget "
        "degradation — the FTS search has to run"
    )


def test_memory_search_query_length_is_capped():
    """AC2 (#939), DISCRIMINATING. RED at base with ``assert 200 == 422``:
    the memories search route declares ``Query(..., min_length=1)`` with no
    max_length (while memory content is capped at 10000), so the oversized
    query is accepted, embedded, and answered. GREEN after the fix: 422
    before any embedding work runs. The control leg (normal query, 200
    with results and one embed) proves only the oversized input is rejected."""
    class _RecordingRouteStore:
        def __init__(self):
            self.embedding_service = SimpleNamespace()  # route gates on it
            self.embed_calls: List[str] = []

        async def embed_query(self, query):
            self.embed_calls.append(query)
            return [1.0, 0.0]

        def search_memories(self, query, limit=5, **kwargs):
            return [_memory_record()]

    async def _evaluate(_user, _kind, _vault_id, _action):
        return True

    store = _RecordingRouteStore()
    app = FastAPI()
    app.include_router(memories_routes.router, prefix="/api")
    app.dependency_overrides.update({
        get_db: lambda: None,
        get_memory_store: lambda: store,
        get_current_active_user: lambda: {"id": 1, "role": "member", "username": "u"},
        get_evaluate_policy: lambda: _evaluate,
        require_model_ready: lambda: None,
    })
    client = TestClient(app)
    try:
        # Control: a normal query is served with results (and one embed).
        control = client.get("/api/memories/search",
                             params={"query": "roadmap", "vault_id": 1})
        assert control.status_code == 200, control.text[:400]
        assert control.json()["total"] >= 1, (
            "control leg: the normal query must still be served with results")
        assert len(store.embed_calls) == 1, (
            f"control leg: exactly one embed; got {len(store.embed_calls)}")

        # Oversized: rejected before any embedding work runs.
        response = client.get("/api/memories/search",
                              params={"query": "q" * 20001, "vault_id": 1})
        assert response.status_code == 422, (
            "a memory search query longer than the configured maximum must be "
            "rejected with 422 before any embedding work runs; got "
            f"{response.status_code}: {response.text[:200]}")
        assert len(store.embed_calls) == 1, (
            "no embed_query call may happen for the rejected oversized "
            f"query; recorded {len(store.embed_calls) - 1} extra call(s)")
    finally:
        app.dependency_overrides.clear()


async def test_memory_task_cancelled_on_early_exit():
    """AC3 (#939), DISCRIMINATING via leg (b). Leg (a) — admission-rejected
    stream turn: the EMBEDDING admission rejects while the memory embed is
    deterministically in flight; the exit must cancel the memory task so
    its embed observes asyncio.CancelledError. This leg is already GREEN at
    base (the AdmissionRejected branch calls _cancel_overlap_tasks(),
    pinned by test_c08_admission_rejected_cleanup.py) and must STAY green.
    Leg (b) — generator close mid-stream: consume to the Searching stage,
    let the embed start, then ``aclose()`` while it is in flight. RED at
    base (``assert False is True``): no try/finally exists between task
    creation and its await, so the close orphans the task and the embed
    never observes cancellation. GREEN after the fix."""
    # ---- leg (a): admission-rejected turn ------------------------------
    store_a = _FakeMemoryStore(hang=True)
    engine_a = _make_engine(store_a)
    controller = _RejectVariantEmbedsController(store_a.started)
    saw_rejection = False
    with patch(
        "app.services.rag_engine.get_admission_controller", return_value=controller,
    ), _engine_patches():
        agen = engine_a.query("test query", [], stream=True)
        try:
            async for chunk in agen:
                if chunk.get("type") == "error" and (
                    chunk.get("code") == "ADMISSION_REJECTED"
                ):
                    saw_rejection = True
                    break
        finally:
            await _close_and_cleanup(agen)
        assert saw_rejection, "harness: the ADMISSION_REJECTED chunk never arrived"
        observed_a = await _poll_until(store_a.cancelled_seen, 5.0)
        assert observed_a and store_a.cancelled is True, (
            "leg (a) admission-rejected exit: the in-flight memory embed must "
            "observe asyncio.CancelledError (green at base via "
            "_cancel_overlap_tasks and must stay green)")
        await _cancel_leftover_memory_tasks()

    # ---- leg (b): generator close mid-stream ---------------------------
    store_b = _FakeMemoryStore(hang=True)
    engine_b = _make_engine(store_b)
    with patch(
        "app.services.rag_engine.get_admission_controller",
        return_value=_CountingAdmissionController(),
    ), _engine_patches():
        agen = engine_b.query("test query", [], stream=True)
        try:
            # Advance to the Searching stage chunk (emitted after the memory
            # overlap task is created) — a deterministic suspend point.
            it = agen.__aiter__()
            while True:
                chunk = await it.__anext__()
                if chunk.get("type") == "stage" and chunk.get("stage") == STAGE_SEARCHING:
                    break
            # The memory embed is deterministically in flight now.
            await asyncio.wait_for(store_b.started.wait(), 5.0)
            await agen.aclose()
            await _poll_until(store_b.cancelled_seen, 5.0)
            assert store_b.cancelled is True, (
                "leg (b) generator close: aclose() mid-stream must cancel the "
                "in-flight memory task — its embed never observed "
                "asyncio.CancelledError (orphaned task)"
            )
        finally:
            await _cancel_leftover_memory_tasks()


async def test_memory_precompute_uses_embedding_admission_budget(caplog, monkeypatch):
    """AC4 (#939), DISCRIMINATING. Leg 1 is RED at base with
    ``assert 0 == 1``: raw document RAG is disabled for this turn (the
    variant-embed admission block never runs), so the only EMBEDDING
    admission attributable is the memory precompute's — and at base nothing
    in the memory path consults admission (embeddings.py carries zero
    admission references; the precompute runs outside the #518 budget).
    Leg 2 (saturated budget — every EMBEDDING admit rejected) also fails at
    base: the turn completes but no degradation WARNING is emitted, because
    the precompute never consults the budget it would have to be rejected
    by. The count==0 leg is asserted first, so the base failure is
    dominated by leg 1. GREEN after the fix: exactly one EMBEDDING admission
    for the precompute, and a rejection degrades FTS-only with the
    "Memory query embedding" WARNING."""
    _pin_embed_budget(monkeypatch)

    # ---- leg 1: the precompute acquires an EMBEDDING admission slot -----
    store = _FakeMemoryStore(hang=False)
    engine = _make_engine(store)
    controller = _CountingAdmissionController()
    with _memory_admission_patches(controller):
        agen = engine.query("quarterly roadmap status", [], stream=True)
        try:
            await asyncio.wait_for(_collect(agen), 10.0)
        finally:
            await _close_and_cleanup(agen)

    assert controller.embedding_admits == 1, (
        "the memory precompute embed must acquire exactly one EMBEDDING "
        "admission slot per turn (issue #518 budget extended to the one "
        f"embed that bypasses it); observed {controller.embedding_admits}")

    # ---- leg 2: a saturated budget degrades FTS-only with a WARNING -----
    store2 = _FakeMemoryStore(hang=False)
    engine2 = _make_engine(store2)
    saturated = _CountingAdmissionController(reject_embedding=True)
    with caplog.at_level(logging.WARNING), _memory_admission_patches(saturated):
        agen2 = engine2.query("quarterly roadmap status", [], stream=True)
        try:
            chunks2 = await asyncio.wait_for(_collect(agen2), 10.0)
        finally:
            await _close_and_cleanup(agen2)

    assert chunks2, "a saturated budget must still complete the turn (FTS-only)"
    warning_msgs = _warning_messages(caplog)
    assert any("Memory query embedding" in m for m in warning_msgs), (
        "a rejected EMBEDDING admission on the memory precompute must degrade "
        f"FTS-only with a 'Memory query embedding' WARNING; got {warning_msgs}")
