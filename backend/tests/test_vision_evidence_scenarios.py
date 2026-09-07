"""Required multimodal qualification scenarios (issue #462 B3).

Covers the issue's required test/evidence scenarios that had no registered
coverage: capped winner set, mixed partial availability, cancellation,
concurrent batches, old source JSON, artifact-field save/load/fork survival
(through the FastAPI ROUTE layer), stream/non-stream artifact-field parity,
text-only feature-off non-regression, and the equal-proxies identity contract
through the production dedup/rebuild key functions.

All provider/DB interactions are deterministic fakes — no real network, no
real model, no sleeps (cancellation uses asyncio.Event hand-off).
"""

import asyncio
import json
import os
import sqlite3
import sys
import types
from contextlib import ExitStack
from dataclasses import dataclass, field
from unittest.mock import MagicMock, patch

import pytest
from starlette.requests import Request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:  # pragma: no cover
    import lancedb  # noqa: F401
except ImportError:  # pragma: no cover
    sys.modules["lancedb"] = types.ModuleType("lancedb")

from app.api.routes import chat as chat_routes
from app.config import settings
from app.models.database import init_db, run_migrations
from app.services.document_retrieval import RAGSource, source_dedup_key
from app.services.multimodal_enrichment import MultimodalProviderError
from app.services.rag_engine import RAGEngine
from app.services.vision_evidence import (
    VISION_EMPTY_RESPONSE,
    VISION_PROVIDER_UNAVAILABLE,
    VISION_USED,
    VisionEvidenceResult,
    VisionEvidenceService,
    VisionRunContext,
)

# ---------------------------------------------------------------------------
# Shared service-level fakes (deterministic; counters/statuses only)
# ---------------------------------------------------------------------------


@dataclass
class _Src:
    artifact_id: object
    modality: object = "image"
    asset_id: object = "a1"
    text: str = "proxy"
    description: object = None


class _Ctx:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


async def _can_read_always_true(user, evaluate, c, vid):
    return True


def _shared_module_patches():
    """PRR-001 (PR #525 review): module-GLOBAL patches shared by concurrent
    batches must be entered exactly once — concurrent per-coroutine ExitStacks
    double-patching the same globals can restore originals under a still-
    running sibling and leak patched values across tests."""
    return [
        patch.object(settings, "multimodal_query_vision_enabled", True),
        patch("app.services.vision_evidence._conn_ctx", lambda: _Ctx(MagicMock())),
        patch("app.services.vision_evidence._can_read", new=_can_read_always_true),
        patch(
            "app.services.vision_evidence.resolve_confined", lambda *a, **k: object()
        ),
        patch(
            "app.services.vision_evidence._read_bounded",
            lambda path, cap_bytes: b"fake-png-bytes",
        ),
        patch("app.services.vision_evidence.sniff_raster_mime", lambda data: "image/png"),
        patch("app.services.vision_evidence._pixel_dims", lambda data: (10, 10)),
        patch("app.services.vision_evidence.record_security_event"),
    ]


def _svc_patches(svc, *, cap=None, concurrency=None, client_factory=None):
    """Deterministic fake stack: policy gate open, asset pipeline stubbed to a
    valid raster so the REAL _process_one pipeline runs end to end through the
    provider client (matching the TestProcessOneSecurity stubbing pattern).
    Single-batch tests only — concurrent batches must use
    ``_shared_module_patches()`` (see PRR-001)."""
    patches = _shared_module_patches()
    patches.insert(
        1,
        patch.object(
            VisionEvidenceService, "_whole_batch_allowed", lambda self, c, v: None
        ),
    )
    if cap is not None:
        patches.append(patch.object(settings, "multimodal_max_assets_per_batch", cap))
    if concurrency is not None:
        patches.append(patch.object(settings, "multimodal_concurrency", concurrency))
    if client_factory is not None:
        patches.append(patch.object(svc, "_client_factory", client_factory))
    return patches


def _svc_run(sources, *, cap=None, concurrency=None, client_factory=None):
    svc = VisionEvidenceService()
    with ExitStack() as stack:
        for p in _svc_patches(svc, cap=cap, concurrency=concurrency,
                              client_factory=client_factory):
            stack.enter_context(p)
        return asyncio.run(svc.run(query="q", sources=sources, vault_id=1))


class _RecordingClient:
    """Counts concurrent chat_multimodal entries and closes exactly once."""

    def __init__(self):
        self.calls = 0
        self.in_flight = 0
        self.max_in_flight = 0
        self.close_calls = 0

    async def start(self):
        return None

    async def chat_multimodal(self, messages, max_tokens=512):
        self.calls += 1
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(0)
            return "observation"
        finally:
            self.in_flight -= 1

    async def close(self):
        self.close_calls += 1


# ---------------------------------------------------------------------------
# Required scenarios — service level
# ---------------------------------------------------------------------------


def test_capped_winner_set():
    """3 unique eligible winners with cap 2: exactly the first two (rank order)
    are processed, the third is capped out with no status, capped == 1."""
    sources = [_Src("art1"), _Src("art2"), _Src("art3")]

    def _factory(*a, **k):
        return _RecordingClient()

    result = _svc_run(sources, cap=2, client_factory=_factory)
    assert result.eligible == 3
    assert result.selected == 3
    assert result.deduped == 0
    assert result.capped == 1
    assert result.vlm_used == 2
    assert set(result.statuses) == {"art1", "art2"}
    assert all(s == VISION_USED for s in result.statuses.values())
    # Rank/label stability: the source list itself is untouched by the run.
    assert [s.artifact_id for s in sources] == ["art1", "art2", "art3"]


def test_mixed_partial_availability():
    """One batch, three outcomes: used + provider outage + empty response.
    Per-source degradation only — every source keeps rank and identity, valid
    observations and proxy fallbacks coexist."""
    sources = [
        _Src("art-ok", description="marker-ok"),
        _Src("art-outage", description="marker-outage"),
        _Src("art-empty", description="marker-empty"),
    ]

    class _MixedClient:
        async def start(self):
            return None

        async def chat_multimodal(self, messages, max_tokens=512):
            text = json.dumps(messages, default=str)
            if "marker-outage" in text:
                raise MultimodalProviderError(
                    "network", retryable=True, message="boom"
                )
            if "marker-empty" in text:
                return "   "
            return "the chart shows revenue rising"

        async def close(self):
            return None

    result = _svc_run(sources, client_factory=lambda *a, **k: _MixedClient())
    assert result.statuses["art-ok"] == VISION_USED
    assert result.statuses["art-outage"] == VISION_PROVIDER_UNAVAILABLE
    assert result.statuses["art-empty"] == VISION_EMPTY_RESPONSE
    assert result.vlm_used == 1
    assert result.provider_unavailable == 1
    assert result.empty_response == 1
    assert result.observations == {"art-ok": "the chart shows revenue rising"}
    # PRR-006: outcome statuses must not skew the counter accounting — three
    # distinct artifacts (no dedup) and a default-high cap mean deduped/capped
    # stay 0 alongside the mixed statuses.
    assert result.deduped == 0
    assert result.capped == 0
    # Rank/label stability under a mixed batch.
    assert [s.artifact_id for s in sources] == ["art-ok", "art-outage", "art-empty"]


def test_cancellation_propagates_and_closes_client():
    """Cancelling a run whose provider call is blocked must propagate
    CancelledError (never swallow it as a per-source degradation) and still
    close the shared batch client exactly once (run()'s finally)."""
    entered = asyncio.Event()
    never = asyncio.Event()

    class _HangingClient:
        def __init__(self):
            self.close_calls = 0

        async def start(self):
            return None

        async def chat_multimodal(self, messages, max_tokens=512):
            entered.set()
            await never.wait()  # deterministic block; cancelled by the test
            return "unreachable"

        async def close(self):
            self.close_calls += 1

    svc = VisionEvidenceService()
    client = _HangingClient()
    patches = _svc_patches(svc, client_factory=lambda *a, **k: client)
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        asyncio.run(
            _cancel_after_enter(
                svc.run(query="q", sources=[_Src("art1")], vault_id=1), entered
            )
        )
    assert client.close_calls == 1, "finally must close the shared client once"


async def _cancel_after_enter(coro, entered):
    task = asyncio.ensure_future(coro)
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    return None


def test_concurrent_batches_independent():
    """Two concurrent run() batches: each honors its own
    multimodal_concurrency semaphore WITHIN the batch (max in-flight <= cap)
    and both complete with independent counters.

    PRR-001 (PR #525 review): SHARED module-global patches are entered exactly
    ONCE in the sync test body below — concurrent per-coroutine ExitStacks
    double-patching the same globals can restore originals under a still-
    running sibling coroutine and leak patched values across tests. Only
    per-INSTANCE patches (client factory, instance-level policy gate) stay
    inside the concurrent coroutines, because instance attributes cannot race
    between two separate services."""
    client_a = _RecordingClient()
    client_b = _RecordingClient()
    svc_a = VisionEvidenceService()
    svc_b = VisionEvidenceService()
    batch_a = [_Src("a1"), _Src("a2")]
    batch_b = [_Src("b1")]

    async def _run_batch(svc, sources, client):
        # Per-instance patches only — no shared module globals here.
        # NOTE: patch.object on an INSTANCE stores a plain attribute, so the
        # replacement does NOT receive `self` (unlike a class-level patch).
        with ExitStack() as stack:
            for p in (
                patch.object(svc, "_whole_batch_allowed", lambda c, v: None),
                patch.object(svc, "_client_factory", lambda *a, **k: client),
            ):
                stack.enter_context(p)
            return await svc.run(query="q", sources=sources, vault_id=1)

    async def _main():
        return await asyncio.gather(
            _run_batch(svc_a, batch_a, client_a), _run_batch(svc_b, batch_b, client_b)
        )

    # Shared module-global patches: entered exactly once for BOTH batches.
    with ExitStack() as shared:
        for p in (
            *_shared_module_patches(),
            patch.object(settings, "multimodal_concurrency", 1),
        ):
            shared.enter_context(p)
        result_a, result_b = asyncio.run(_main())

    assert result_a.vlm_used == 2
    assert result_b.vlm_used == 1
    # Within-batch semaphore honored (Round-2 required assertion form).
    assert client_a.max_in_flight <= 1
    assert client_b.max_in_flight <= 1
    # Independent counters per batch.
    assert result_a.statuses["a1"] == VISION_USED
    assert result_b.statuses["b1"] == VISION_USED


# ---------------------------------------------------------------------------
# Required scenarios — persistence/route level (route layer, not helpers)
# ---------------------------------------------------------------------------


def _mock_request():
    request = MagicMock(spec=Request)
    request.client.host = "127.0.0.1"
    return request


async def _allow(*args):
    return True


def _connect(db_path):
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


_ARTIFACT_SOURCES = [
    {
        "id": "1_0",
        "file_id": "1",
        "filename": "chart-report.pdf",
        "source_label": "[S1]",
        "snippet": "quarterly revenue",
        "score": 0.9,
        "artifact_id": "atom-123",
        "modality": "chart",
        "asset_id": "abc123def456",
        "bbox": {"x0": 0.1, "y0": 0.2, "x1": 0.8, "y1": 0.9},
        "page_number": 3,
        "vision_status": "used",
        "description": "bar chart of quarterly revenue",
        "metadata": {"page_number": 3},
    }
]

_LEGACY_SOURCES = [
    {
        "id": "2_0",
        "file_id": "2",
        "filename": "old-notes.pdf",
        "source_label": "[S1]",
        "snippet": "plain text only",
        "score": 0.5,
        "metadata": {},
    }
]


def _db_setup(tmp_path):
    db_path = tmp_path / "scenario-chat.db"
    init_db(str(db_path))
    run_migrations(str(db_path))
    conn = _connect(db_path)
    session_id = conn.execute(
        "INSERT INTO chat_sessions (vault_id, user_id, title) VALUES (?, ?, ?)",
        (1, 1, "Scenario"),
    ).lastrowid
    conn.commit()
    return conn, session_id


async def _add_assistant_message(conn, session_id, sources):
    return await chat_routes.add_message(
        _mock_request(),
        session_id,
        chat_routes.AddMessageRequest(
            role="assistant", content="answer", sources=sources
        ),
        conn=conn,
        user={"id": 1},
        evaluate=_allow,
        rag_engine=None,
        _csrf_token="test-token",
    )


def test_old_source_json_round_trips(tmp_path):
    """Pre-multimodal source JSON (no artifact fields) survives the chat
    route's save -> load unchanged, byte-for-byte in structure."""
    conn, session_id = _db_setup(tmp_path)
    try:
        asyncio.run(_add_assistant_message(conn, session_id, _LEGACY_SOURCES))
        detail = asyncio.run(
            chat_routes.get_session(session_id, conn, {"id": 1}, evaluate=_allow)
        )
        saved = detail["messages"][-1]["sources"]
        assert saved == _LEGACY_SOURCES
        assert all("artifact_id" not in s for s in saved)
    finally:
        conn.close()


def test_artifact_fields_survive_save_load(tmp_path):
    """Artifact-rich sources persisted through the add_message ROUTE and
    reloaded through get_session keep every first-class artifact field."""
    conn, session_id = _db_setup(tmp_path)
    try:
        asyncio.run(_add_assistant_message(conn, session_id, _ARTIFACT_SOURCES))
        detail = asyncio.run(
            chat_routes.get_session(session_id, conn, {"id": 1}, evaluate=_allow)
        )
        saved = detail["messages"][-1]["sources"]
        assert saved == _ARTIFACT_SOURCES
        for key in (
            "artifact_id",
            "modality",
            "asset_id",
            "bbox",
            "page_number",
            "vision_status",
            "description",
        ):
            assert saved[0][key] == _ARTIFACT_SOURCES[0][key], key
    finally:
        conn.close()


def test_artifact_fields_survive_fork(tmp_path):
    """Session fork copies artifact-rich sources verbatim (route layer)."""
    conn, session_id = _db_setup(tmp_path)
    try:
        asyncio.run(_add_assistant_message(conn, session_id, _ARTIFACT_SOURCES))
        forked = asyncio.run(
            chat_routes.fork_session(
                _mock_request(),
                session_id,
                chat_routes.ForkSessionRequest(message_index=0),
                conn,
                {"id": 1},
                evaluate=_allow,
            )
        )
        saved = forked["messages"][-1]["sources"]
        assert saved == _ARTIFACT_SOURCES
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Required scenarios — engine level (stream/non-stream parity, text-only)
# ---------------------------------------------------------------------------

ANSWER = "Answer grounded in the retrieved source."


class _RecordingVectorStore:
    """Serves fixed records and records nothing else (deterministic)."""

    def __init__(self, records):
        self._records = records

    async def search(self, embedding, limit, vault_id=None, query_text="",
                     hybrid=True, hybrid_alpha=0.5, filter_expr=None, **kw):
        return [dict(r) for r in self._records]

    def get_fts_exceptions(self):
        return 0

    def is_connected(self):
        return True


class _StubEmbedding:
    async def embed_single(self, text):
        return [0.1, 0.2, 0.3]

    async def embed_passage(self, text):
        return [0.1, 0.2, 0.3]


class _StubMemory:
    def detect_memory_intent(self, text):
        return None


class _StubLLM:
    base_url = "stub-answer-client"
    model = "stub-model"

    def __init__(self, answer=ANSWER):
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


def _make_engine(store, llm=None):
    return RAGEngine(
        embedding_service=_StubEmbedding(),
        vector_store=store,
        memory_store=_StubMemory(),
        llm_client=llm or _StubLLM(),
        reranking_service=None,
        instant_client=llm or _StubLLM(),
        thinking_client=llm or _StubLLM(),
    )


def _apply_engine_settings(mock):
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


_ARTIFACT_RECORD = {
    "id": "7_0",
    "file_id": "7",
    "text": "chart payload text",
    "_distance": 0.1,
    "metadata": {"atom_id": "atom-7", "modality": "image", "asset_id": "asset-7"},
}


async def _collect(engine, **query_kwargs):
    done = None
    content = []
    async for chunk in engine.query("what does the chart say", [], vault_id=1,
                                    **query_kwargs):
        if chunk.get("type") == "done":
            done = chunk
        elif chunk.get("type") == "content":
            content.append(chunk.get("content", ""))
    assert done is not None, "query() never produced a done message"
    return done, "".join(content)


def test_stream_nonstream_artifact_parity():
    """Artifact fields + vision status are IDENTICAL in the streaming done
    payload and the non-stream done payload (same fakes, real construction)."""

    class _UsedVisionService:
        async def run(self, *, query, sources, vault_id, user=None, evaluate=None):
            result = VisionEvidenceResult()
            for s in sources:
                if getattr(s, "artifact_id", None) == "atom-7":
                    result.eligible = 1
                    result.selected = 1
                    result.deduped = 0
                    result.statuses["atom-7"] = VISION_USED
                    result.observations["atom-7"] = "chart shows a spike in Q3"
                    result.vlm_used = 1
            return result

    async def _one(stream):
        engine = _make_engine(_RecordingVectorStore([_ARTIFACT_RECORD]))
        with (
            patch("app.services.rag_engine.settings") as mock_settings,
            patch("app.services.rag_engine._get_pool", return_value=_pool_stub()),
        ):
            _apply_engine_settings(mock_settings)
            done, _ = await _collect(
                engine,
                stream=stream,
                vision_context=VisionRunContext(service=_UsedVisionService()),
            )
        return done

    done_stream = asyncio.run(_one(True))
    done_nonstream = asyncio.run(_one(False))
    src_stream = done_stream.get("sources") or []
    src_nonstream = done_nonstream.get("sources") or []
    assert src_stream, "stream done payload must carry sources"
    assert src_stream == src_nonstream, "artifact source payload must be identical"
    artifact = next(s for s in src_stream if s.get("artifact_id") == "atom-7")
    assert artifact["vision_status"] == "used"
    assert artifact["modality"] == "image"


def test_text_only_feature_off_non_regression():
    """Feature off + artifact chunks: sources carry NO vision_status, the prompt
    carries NO visual observation, and source ORDER matches a text-only corpus
    with the artifact identity stripped."""

    async def _run_variant(with_artifact_metadata):
        record = dict(_ARTIFACT_RECORD)
        if not with_artifact_metadata:
            record = dict(record, metadata={})
        llm = _StubLLM()
        engine = _make_engine(_RecordingVectorStore([record]), llm=llm)
        with (
            patch("app.services.rag_engine.settings") as mock_settings,
            patch("app.services.rag_engine._get_pool", return_value=_pool_stub()),
        ):
            _apply_engine_settings(mock_settings)
            done, _ = await _collect(engine, stream=False, vision_context=None)
        prompt = json.dumps(llm.completion_messages, default=str)
        return done.get("sources") or [], prompt

    artifact_sources, artifact_prompt = asyncio.run(_run_variant(True))
    plain_sources, plain_prompt = asyncio.run(_run_variant(False))
    for s in artifact_sources:
        assert "vision_status" not in s or s.get("vision_status") is None
    # The system prompt MENTIONS the <visual_observation> wrapper in its trust
    # instructions for both variants; a feature-off run must not add any
    # observation ELEMENT beyond that shared baseline (same count as the
    # text-only corpus) and must not leak vision wire fields.
    assert artifact_prompt.count("<visual_observation>") == plain_prompt.count(
        "<visual_observation>"
    )
    assert "vision_status" not in json.dumps(artifact_sources)
    # Ordering identical to the text-only corpus (same retrieval rank path).
    assert [s.get("file_id") for s in artifact_sources] == [
        s.get("file_id") for s in plain_sources
    ]


def test_equal_proxies_identity_contract():
    """Binding identity-contract spec (issue #462 §1): same file + byte-equal
    text + different artifacts -> DISTINCT dedup keys; the packed-rebuild key
    (RAGSource.artifact_identity_key) equals the fusion-map key
    (source_dedup_key) for the same source, so fusion dedup and token-packed
    reconstruction can never diverge on the collapse case."""
    same_text = "byte-identical proxy text"
    key_a = source_dedup_key("file-1", same_text, {"atom_id": "atom-a"})
    key_b = source_dedup_key("file-1", same_text, {"atom_id": "atom-b"})
    assert key_a != key_b
    assert key_a[0] == "artifact" and key_b[0] == "artifact"
    # Plain chunks keep the legacy (file_id, text) collapse.
    assert source_dedup_key("file-1", same_text, {}) == source_dedup_key(
        "file-1", same_text, {}
    )
    # Rebuild-key consistency for BOTH artifact and plain identities.
    artifact_src = RAGSource(
        text=same_text, file_id="file-1", score=0.5,
        metadata={"atom_id": "atom-a"}, artifact_id="atom-a",
    )
    plain_src = RAGSource(
        text=same_text, file_id="file-1", score=0.5, metadata={}
    )
    assert artifact_src.artifact_identity_key() == key_a
    assert plain_src.artifact_identity_key() == source_dedup_key(
        "file-1", same_text, {}
    )


def _pool_stub():
    """Minimal get_pool stand-in for query()'s incidental DB reads."""

    class _P:
        def connection(self):
            return _C()

    class _C:
        def __enter__(self):
            return MagicMock()

        def __exit__(self, *exc):
            return False

    return _P()
