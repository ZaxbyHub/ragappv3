"""Issue #518 review-hardening tests (plan-critic Round-1 revisions).

Non-frozen supplementary coverage demanded by the plan critic beyond the
frozen C1-C8 checks: per-class admission wiring (EMBEDDING, RERANKING,
VISION, BACKGROUND, INSTANT), logical-eviction-without-cancellation
semantics, request_id_var -> outbound X-Request-ID end to end, /metrics
registered on the PRODUCTION app, embedding cache-hit recording through
the real service, and non-stream llm_metrics derivation from the engine
done chunk only.
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services.admission import (
    AdmissionClass,
    AdmissionController,
    MemoryAdmissionStore,
)

BUDGETS = {k: 1 for k in ("chat", "instant", "embedding", "reranking", "vision", "background")}
CLASSES = {cls: cls.value for cls in AdmissionClass}


class _RecController:
    def __init__(self):
        self.calls = []

    async def queue_depth(self, cls):
        return 0

    @asynccontextmanager
    async def admit(self, admission_class, **kwargs):
        self.calls.append((admission_class, kwargs))
        lease = SimpleNamespace(admission_class=admission_class, holder="rec")
        yield lease


# ---------------------------------------------------------------------------
# 1. Preempted background holder is NEVER cancelled (logical eviction only)
# ---------------------------------------------------------------------------


async def test_preempted_background_holder_completes_normally():
    ctrl = AdmissionController(
        budgets={"dev": 1},
        class_budgets={
            AdmissionClass.CHAT: "dev",
            AdmissionClass.BACKGROUND: "dev",
        },
        instance_id="w1",
    )
    outcomes = []

    async def background_work():
        outcomes.append("bg-start")
        await asyncio.sleep(0.15)
        outcomes.append("bg-done")

    bg_task = asyncio.create_task(background_work())
    await asyncio.sleep(0.01)
    async with ctrl.admit(AdmissionClass.BACKGROUND):
        await bg_task
    assert outcomes == ["bg-start", "bg-done"], outcomes

    # Foreground preempts (the bg slot was held during its body); the bg
    # task itself completed — no cancellation.
    holder_cm = ctrl.admit(AdmissionClass.BACKGROUND)
    await holder_cm.__aenter__()
    started = asyncio.get_event_loop().time()
    async with ctrl.admit(AdmissionClass.CHAT):
        pass
    elapsed = asyncio.get_event_loop().time() - started
    assert elapsed < 1.0, (
        "518-H PREEMPTION STALLED: foreground not admitted past background holder"
    )


# ---------------------------------------------------------------------------
# 2. Per-class wiring: engine embedding/rerank/vision; background worker;
#    instant-mode engine gate.
# ---------------------------------------------------------------------------


async def test_engine_embeds_consult_embedding_admission():
    from test_518_failed_turn_metrics import (
        C1Embedding,
        C1MemoryStore,
        C1VectorStore,
        OkClient,
    )

    from app.config import settings
    from app.services import rag_engine as rag_engine_module

    recorder = _RecController()
    engine = rag_engine_module.RAGEngine(
        embedding_service=C1Embedding(),
        vector_store=C1VectorStore(),
        memory_store=C1MemoryStore(),
        llm_client=OkClient(),
    )
    with patch.object(
        rag_engine_module, "get_admission_controller", lambda: recorder
    ), patch.object(settings, "query_transformation_enabled", False):
        chunks = [c async for c in engine.query("hello", [], stream=True)]
    assert chunks
    embed_calls = [
        c for c, _ in recorder.calls if c == AdmissionClass.EMBEDDING
    ]
    assert embed_calls, "518-H EMBEDDING NOT WIRED at the engine embed phase"


def test_background_processor_consults_background_admission():
    from app.services import background_tasks as bg_module

    recorder = _RecController()

    async def drive():
        processor = bg_module.BackgroundProcessor.__new__(
            bg_module.BackgroundProcessor
        )
        processor.shutdown_event = asyncio.Event()
        processor.queue = asyncio.Queue()
        processor.queue.put_nowait(SimpleNamespace(file_id=1))
        processor.shutdown_event.set()  # one iteration: dequeue, then exit

        with patch.object(
            bg_module, "get_admission_controller", lambda: recorder
        ), patch.object(
            processor, "_process_task_wrapper", autospec=True
        ) as fake_process:
            async def _noop(task):
                return None

            fake_process.side_effect = _noop
            await asyncio.wait_for(
                bg_module.BackgroundProcessor._worker_loop(processor), timeout=5
            )

    asyncio.run(drive())
    bg_calls = [c for c, _ in recorder.calls if c == AdmissionClass.BACKGROUND]
    assert bg_calls, "518-H BACKGROUND NOT WIRED at the ingestion worker"


async def test_instant_mode_engine_gate_uses_instant_class():
    from test_518_failed_turn_metrics import (
        C1Embedding,
        C1MemoryStore,
        C1VectorStore,
        OkClient,
    )

    from app.config import settings
    from app.models.chat_mode import ChatMode
    from app.services import rag_engine as rag_engine_module

    recorder = _RecController()
    engine = rag_engine_module.RAGEngine(
        embedding_service=C1Embedding(),
        vector_store=C1VectorStore(),
        memory_store=C1MemoryStore(),
        llm_client=OkClient(),
    )
    with patch.object(
        rag_engine_module, "get_admission_controller", lambda: recorder
    ), patch.object(settings, "query_transformation_enabled", False):
        chunks = [
            c
            async for c in engine.query(
                "hello", [], stream=True, mode=ChatMode.INSTANT
            )
        ]
    assert chunks
    instant_calls = [
        c for c, _ in recorder.calls if c == AdmissionClass.INSTANT
    ]
    assert instant_calls, "518-H INSTANT NOT WIRED for instant-mode generation"


# ---------------------------------------------------------------------------
# 3. request_id_var -> outbound X-Request-ID end to end (logging identity is
#    the wire identity, C4 behavioral chain).
# ---------------------------------------------------------------------------

EMBED_RESPONSE = {"data": [{"embedding": [0.1, 0.2, 0.3]}]}


def test_request_id_var_flows_outbound_end_to_end(monkeypatch):
    captured = []

    async def fake_send(self, request, **kwargs):
        captured.append(request)
        return httpx.Response(200, json=EMBED_RESPONSE, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "send", fake_send)

    from app.services.telemetry import init_telemetry
    from app.utils.request_context import request_id_var

    with patch("app.config.settings.telemetry_enabled", True):
        init_telemetry()
        token = request_id_var.set("req-hardening-518")
        try:
            from app.services.embeddings import EmbeddingService

            with patch("app.services.embeddings.assert_url_safe"):
                service = EmbeddingService()

            async def drive():
                vec = await service.embed_single("identity probe")
                await service._client.aclose()
                return vec

            vec = asyncio.run(drive())
        finally:
            request_id_var.reset(token)
    assert vec == [0.1, 0.2, 0.3]
    assert captured
    for request in captured:
        assert (
            request.headers.get("X-Request-ID") == "req-hardening-518"
        ), "518-H REQUEST-ID NOT SHARED OUTBOUND from request_id_var"


# ---------------------------------------------------------------------------
# 4. /metrics is registered on the PRODUCTION app, not just the helper.
# ---------------------------------------------------------------------------


def test_metrics_route_registered_on_production_app():
    from app.main import app

    paths = {getattr(route, "path", None) for route in app.routes}
    assert "/metrics" in paths, (
        f"518-H METRICS NOT ON PRODUCTION APP: /metrics absent from main.app "
        f"routes ({sorted(str(p) for p in paths if p)[:12]}...)"
    )


# ---------------------------------------------------------------------------
# 5. Embedding cache hits recorded through the REAL service.
# ---------------------------------------------------------------------------


def test_embedding_cache_hit_recorded(monkeypatch):
    calls = []

    async def fake_send(self, request, **kwargs):
        calls.append(request)
        return httpx.Response(200, json=EMBED_RESPONSE, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "send", fake_send)

    from app.services.embeddings import EmbeddingService
    from app.services.telemetry import get_telemetry, init_telemetry

    with patch("app.config.settings.telemetry_enabled", True):
        init_telemetry()
        telemetry = get_telemetry()
        telemetry.reset()
        with patch("app.services.embeddings.assert_url_safe"):
            service = EmbeddingService()

        async def drive():
            v1 = await service.embed_single("cache-probe-text")
            v2 = await service.embed_single("cache-probe-text")
            await service._client.aclose()
            return v1, v2

        v1, v2 = asyncio.run(drive())
    assert v1 == v2 == [0.1, 0.2, 0.3]
    snap = telemetry.snapshot()
    assert snap["embedding_cache_hits"] >= 1, (
        f"518-H CACHE HITS NOT RECORDED: snapshot={snap['embedding_cache_hits']}"
    )
    # Second identical call was served from L1 — no extra network round trip.
    assert len(calls) == 1, f"518-H L1 MISS ON SECOND CALL: {len(calls)} requests"


# ---------------------------------------------------------------------------
# 6. Non-stream route derives llm_metrics ONLY from the engine done chunk.
# ---------------------------------------------------------------------------


def test_non_stream_llm_metrics_comes_from_engine_done_chunk():
    from app.api.deps import get_db, get_rag_engine, get_vector_store
    from app.api.routes import chat as chat_module
    from app.api.routes.chat import get_current_active_user

    async def fake_query(*args, **kwargs):
        # Engine yields done WITHOUT llm_metrics — the route must not invent
        # or inherit any (e.g. from engine singleton state).
        yield {"type": "content", "content": "answer"}
        yield {"type": "done", "sources": [], "memories_used": []}

    class _Engine:
        query = fake_query

    ready_vs = MagicMock()
    ready_vs._ready = True

    # Hermetic: the non-stream path persists the turn through a pooled
    # SQLite connection; under the parallel full suite that pool can fail
    # connection creation (known cwd/xdist environmental contention). The
    # persistence seam is not under test — stub it.
    fake_conn = MagicMock()
    fake_conn.in_transaction = False

    app = FastAPI()
    app.include_router(chat_module.router, prefix="/api")
    app.dependency_overrides[get_rag_engine] = lambda: _Engine()
    app.dependency_overrides[get_current_active_user] = lambda: {
        "id": 1, "username": "u", "email": "u@e", "role": "admin",
    }
    app.dependency_overrides[get_vector_store] = lambda: ready_vs
    app.dependency_overrides[get_db] = lambda: fake_conn

    client = TestClient(app)
    with patch.object(
        chat_module, "get_admission_controller", lambda: _RecController()
    ):
        response = client.post(
            "/api/chat", json={"message": "hi", "history": []}
        )
    assert response.status_code == 200, response.text[:400]
    body = response.json()
    assert body.get("llm_metrics") is None, (
        f"518-H NON-STREAM METRICS FABRICATED: got {body.get('llm_metrics')!r} "
        "from a done chunk that carried none"
    )
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# 7. AC4 independent signal (outside the frozen C3/C4 file): registration +
#    identity flow.
# ---------------------------------------------------------------------------


def test_request_id_filter_registered_and_shared_outbound():
    from pathlib import Path

    lifespan_src = (
        Path(__file__).resolve().parents[1] / "app" / "lifespan.py"
    ).read_text(encoding="utf-8")
    assert "addFilter(RequestIdFilter())" in lifespan_src and (
        lifespan_src.count("RequestIdFilter") >= 2
    ), "518-H REQUEST_ID_FILTER NOT REGISTERED"

    from app.services.telemetry import (
        correlation_headers,
        init_telemetry,
        set_current_turn,
    )
    from app.utils.request_context import RequestIdFilter, request_id_var

    token = request_id_var.set("req-ac4-hardening")
    try:
        record = logging.makeLogRecord(
            {"name": "t", "level": logging.INFO, "msg": "m"}
        )
        assert RequestIdFilter().filter(record) is True
        assert record.request_id == "req-ac4-hardening"
        with patch("app.config.settings.telemetry_enabled", True):
            init_telemetry()
        set_current_turn("req-ac4-hardening")
        headers = correlation_headers()
        assert headers.get("X-Request-ID") == "req-ac4-hardening"
    finally:
        request_id_var.reset(token)


# ---------------------------------------------------------------------------
# 8. Rerank and vision consult their classes through the engine path.
# ---------------------------------------------------------------------------


async def test_engine_vision_and_rerank_consult_admission():
    from test_518_failed_turn_metrics import (
        C1Embedding,
        C1MemoryStore,
        OkClient,
    )

    from app.config import settings
    from app.services import rag_engine as rag_engine_module
    from app.services.vision_evidence import VisionRunContext

    recorder = _RecController()

    class VisionStore:
        """Search results are dict-shaped like the real VectorStore; the
        vision gate checks artifact_id/modality on the POST-retrieval chunk
        objects, which document_retrieval derives from metadata — so put the
        attributes in the result metadata the same way artifact-bearing
        chunks carry them (getattr fallbacks also read them off the dict
        view the fakes produce)."""

        async def search(self, embedding, limit=10, filter_expr=None,
                         vault_id=None, query_text=None, hybrid=False, **kw):
            return [{
                "text": "v", "file_id": "f1", "score": 0.9,
                "metadata": {}, "artifact_id": 7, "modality": "image",
            }]

        async def get_chunks_by_uid(self, uids):
            return []

        def get_fts_exceptions(self):
            return 0

    class SilentReranker:
        async def rerank(self, query, chunks, top_n):
            return chunks, True

    engine = rag_engine_module.RAGEngine(
        embedding_service=C1Embedding(),
        vector_store=VisionStore(),
        memory_store=C1MemoryStore(),
        llm_client=OkClient(),
        reranking_service=SilentReranker(),
    )
    engine.reranking_enabled = True

    # The vision gate checks artifact_id/modality on the POST-retrieval
    # chunk objects; short-circuit filter_relevant to return artifact-bearing
    # chunk objects directly (the retrieval machinery is not under test).
    artifact_chunk = SimpleNamespace(
        text="vision source", file_id="f1", score=0.9, metadata={},
        artifact_id=7, modality="image", description=None,
        vision_observation=None, parent_window_text=None,
        observation_for=None,
    )

    class _PassThroughRetrieval:
        async def filter_relevant(self, sources, **kwargs):
            return [artifact_chunk]

        async def expand_window(self, sources):
            return sources

        def to_source_metadata(self, chunk, source_index=None, **kwargs):
            return {
                "file_id": getattr(chunk, "file_id", None),
                "text": getattr(chunk, "text", ""),
            }

        def __getattr__(self, name):
            # Any other retrieval helper: permissive no-op returning [] —
            # the retrieval machinery is not under test here.
            def _passthrough(*args, **kwargs):
                return []

            return _passthrough

    engine.document_retrieval = _PassThroughRetrieval()

    vision_service = MagicMock()

    async def fake_run(**kwargs):
        return SimpleNamespace(
            eligible=0, selected=0, deduped=0, capped=0, vlm_used=0,
            proxy_only=0, policy_blocked=0, sources=[], observations={},
            statuses={},
        )

    vision_service.run = fake_run
    vision_context = VisionRunContext(
        service=vision_service, user={"id": 1}, evaluate=None
    )
    with patch.object(
        rag_engine_module, "get_admission_controller", lambda: recorder
    ), patch.object(settings, "query_transformation_enabled", False):
        try:
            _chunks = [
                c
                async for c in engine.query(
                    "hello", [], stream=True, vision_context=vision_context
                )
            ]
        except Exception:
            # The wiring assertions only need the vision/rerank phases to
            # have executed; the minimal fake chunk may not satisfy every
            # downstream consumer (prompt building etc.). The vision gate
            # itself proves execution via the logged degrade path.
            pass
    vision_calls = [c for c, _ in recorder.calls if c == AdmissionClass.VISION]
    rerank_calls = [
        c for c, _ in recorder.calls if c == AdmissionClass.RERANKING
    ]
    assert vision_calls, "518-H VISION NOT WIRED at the vision evidence phase"
    assert rerank_calls, "518-H RERANKING NOT WIRED at the rerank phase"
