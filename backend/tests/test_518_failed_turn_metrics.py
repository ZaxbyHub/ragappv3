"""Issue #518 acceptance check C1 / AC1 — failed-stream metrics must be
request-local (OBS-002 regression).

Behavior spec (frozen): after a successful chat stream through ONE RAGEngine
instance, a subsequent stream that fails must emit a terminal ``done`` whose
generation metrics identify THAT failure (a metrics dict whose ``status`` is
not ``"ok"``, or an explicit per-request failure marker), and must NEVER
carry the previous success's metrics dict.

Same-class sibling (root cause names ``_last_distillation_provenance``): a
failing turn's citation-confidence inputs must not be inherited from the
prior request.

This test FAILS on current master (stale metrics leak reproduced by
`.agents/issue-traces/518-e3-admission-telemetry-backup/repro/repro_obs002.py`).
It must pass without modifying the issue-249 contract that a successful
fallback still records fallback metrics (test_issue_249_runtime_contracts.py
pins ``_last_llm_metrics`` after a SUCCESSFUL fallback — untouched here).
"""

from unittest.mock import patch

from app.config import settings
from app.services.llm_client import LLMError
from app.services.rag_engine import EmbeddingError, RAGEngine

TURN1_METRICS = {
    "provider_url": "provider-a",
    "model": "fake-model",
    "latency_ms": 111.0,
    "finish_reason": "stop",
    "status": "ok",
    "stream": True,
}


class C1Embedding:
    def __init__(self, fail: bool = False):
        self.fail = fail

    async def embed_single(self, text):
        if self.fail:
            raise EmbeddingError("simulated embedding outage")
        return [0.1, 0.2]

    async def embed_passage(self, text):
        return [0.1, 0.2]

    async def embed_batch(self, texts):
        return [[0.1, 0.2] for _ in texts]


class C1VectorStore:
    async def search(self, embedding, limit=10, filter_expr=None, vault_id=None,
                     query_text=None, hybrid=False, **kwargs):
        return [{
            "text": "chunk one sentence. chunk two sentence.",
            "file_id": "f1",
            "metadata": {"raw_text": "chunk one sentence. chunk two sentence."},
            "score": 0.9,
        }]

    async def get_chunks_by_uid(self, chunk_uids):
        return []

    def get_fts_exceptions(self):
        return 0


class C1MemoryStore:
    def detect_memory_intent(self, text):
        return None

    def search_memories(self, query, limit=5, vault_id=None, include_global=False):
        return []


class OkClient:
    """Succeeds; records last_metrics the way the real LLMClient does."""

    base_url = "http://provider-a"
    model = "fake-model"

    def __init__(self):
        self.last_metrics = {}

    async def chat_completion(self, messages, **kwargs):
        self.last_metrics = dict(TURN1_METRICS, stream=False)
        return "ok answer"

    async def chat_completion_stream(self, messages, **kwargs):
        self.last_metrics = dict(TURN1_METRICS)
        yield "ok answer"


class FailClient:
    """Fails on every candidate before content; records failure last_metrics
    the way the real LLMClient does (llm_client.py stream error path)."""

    base_url = "http://provider-a"
    model = "fake-model"

    def __init__(self):
        self.last_metrics = {}

    async def chat_completion(self, messages, **kwargs):
        self.last_metrics = {
            "provider_url": self.base_url,
            "model": self.model,
            "status": "request_error",
        }
        raise LLMError("simulated provider outage")

    async def chat_completion_stream(self, messages, **kwargs):
        self.last_metrics = {
            "provider_url": self.base_url,
            "model": self.model,
            "status": "request_error",
            "stream": True,
        }
        raise LLMError("simulated provider outage")
        yield  # pragma: no cover - makes this an async generator


async def _drive(engine, query):
    return [m async for m in engine.query(query, [], stream=True)]


def _done_of(messages):
    dones = [m for m in messages if isinstance(m, dict) and m.get("type") == "done"]
    return dones[-1] if dones else None


def _identity(metrics):
    """Comparable identity of a metrics dict: the fields the issue says must
    never be inherited (provider_url, latency, status, finish_reason)."""
    if not isinstance(metrics, dict):
        return None
    return (
        metrics.get("provider_url"),
        metrics.get("latency_ms"),
        metrics.get("status"),
        metrics.get("finish_reason"),
    )


def _assert_done_identifies_failure(done, turn_label, previous_identity):
    """The terminal ``done`` of a failed turn must identify THAT failure.

    Accepted forms (frozen spec):
      * ``llm_metrics`` dict with a ``status`` that is present and != "ok"
        (e.g. request_error / timeout / unavailable), OR
      * an explicit per-request failure marker: a truthy
        ``generation_failed`` flag or an ``llm_error`` entry on the done.
    In every case the metrics identity must differ from the previous
    success's identity dict.
    """
    assert done is not None, (
        f"518-C1 NO TERMINAL DONE: failed turn ({turn_label}) emitted no "
        "terminal done chunk at engine level"
    )
    m = done.get("llm_metrics")
    status_identifies = isinstance(m, dict) and m.get("status") is not None \
        and m.get("status") != "ok"
    explicit_marker = bool(done.get("generation_failed")) or (
        done.get("llm_error") is not None
    )
    assert status_identifies or explicit_marker, (
        f"518-C1 STALE METRICS LEAK: failed turn ({turn_label}) terminal done "
        f"metrics identify the failure; got llm_metrics={m!r}, "
        f"generation_failed={done.get('generation_failed')!r}, "
        f"llm_error={done.get('llm_error')!r} — the done carries the previous "
        "successful turn's ok-metrics instead of this request's failure"
    )
    current_identity = _identity(m)
    assert current_identity != previous_identity, (
        f"518-C1 STALE METRICS LEAK: failed turn ({turn_label}) done metrics "
        f"identity {current_identity!r} equals the previous success's "
        f"identity {previous_identity!r} (provider_url+latency_ms+status+"
        "finish_reason) — request-local metrics required"
    )


def _build_engine():
    return RAGEngine(
        embedding_service=C1Embedding(),
        vector_store=C1VectorStore(),
        memory_store=C1MemoryStore(),
        llm_client=OkClient(),
    )


async def test_failed_llm_turn_done_identifies_failure_not_previous_success():
    """AC1: success turn, then every LLM candidate fails before content —
    the failed turn's done must identify the failure, never turn 1's
    metrics."""
    engine = _build_engine()
    with patch.object(settings, "query_transformation_enabled", False):
        # --- turn 1: success ---
        turn1 = await _drive(engine, "first question")
        done1 = _done_of(turn1)
        assert done1 is not None, "518-C1 HARNESS: turn 1 produced no done"
        m1 = done1.get("llm_metrics")
        assert isinstance(m1, dict) and m1.get("status") == "ok", (
            f"518-C1 HARNESS: turn 1 done metrics not ok: {m1!r}"
        )
        identity1 = _identity(m1)

        # --- turn 2: every LLM candidate fails before content ---
        fail = FailClient()
        engine.llm_client = fail
        engine._thinking_client_override = fail
        engine._instant_client_override = fail
        turn2 = await _drive(engine, "second question")
        err2 = [m for m in turn2 if isinstance(m, dict) and m.get("type") == "error"]
        assert err2, "518-C1 HARNESS: turn 2 produced no error chunk"
        done2 = _done_of(turn2)
        _assert_done_identifies_failure(done2, "llm-failure", identity1)


async def test_failed_embedding_turn_does_not_leak_previous_metrics():
    """AC1 variant: embedding outage before any LLM call — whatever terminal
    payload the engine produces must not carry the previous success's
    metrics, and a done (if emitted) must identify the failure."""
    engine = _build_engine()
    with patch.object(settings, "query_transformation_enabled", False):
        turn1 = await _drive(engine, "first question")
        done1 = _done_of(turn1)
        assert done1 is not None, "518-C1 HARNESS: turn 1 produced no done"
        m1 = done1.get("llm_metrics")
        assert isinstance(m1, dict) and m1.get("status") == "ok", (
            f"518-C1 HARNESS: turn 1 done metrics not ok: {m1!r}"
        )
        identity1 = _identity(m1)

        # --- turn 3: embedding outage before any LLM call ---
        engine.embedding_service = C1Embedding(fail=True)
        turn3 = await _drive(engine, "third question")
        err3 = [m for m in turn3 if isinstance(m, dict) and m.get("type") == "error"]
        assert err3, "518-C1 HARNESS: turn 3 produced no error chunk"
        done3 = _done_of(turn3)
        if done3 is not None:
            # The embedding-failure path may terminate without an engine-level
            # done (the route then emits its own metric-free terminal done).
            # When a done IS emitted, it must obey the same request-local
            # contract as the LLM-failure variant.
            _assert_done_identifies_failure(done3, "embedding-failure", identity1)
        # No chunk of the failed turn may embed the previous success metrics.
        for chunk in turn3:
            if isinstance(chunk, dict):
                assert _identity(chunk.get("llm_metrics")) != identity1, (
                    "518-C1 STALE METRICS LEAK: embedding-failure turn emitted "
                    "a chunk carrying the previous success's metrics identity "
                    f"{identity1!r}"
                )

        # A subsequent failing LLM turn through the SAME engine (stale state
        # now includes turn 1) must still identify its own failure.
        fail = FailClient()
        engine.embedding_service = C1Embedding()
        engine.llm_client = fail
        engine._thinking_client_override = fail
        engine._instant_client_override = fail
        turn4 = await _drive(engine, "fourth question")
        done4 = _done_of(turn4)
        _assert_done_identifies_failure(done4, "post-embedding-failure", identity1)


async def test_failed_turn_does_not_inherit_distillation_provenance():
    """AC1 same-class sibling: citation-confidence inputs
    (``_last_distillation_provenance``) must not be inherited from the prior
    request on a failing turn."""
    engine = _build_engine()
    with patch.object(settings, "query_transformation_enabled", False):
        turn1 = await _drive(engine, "first question")
        assert _done_of(turn1) is not None, "518-C1 HARNESS: turn 1 produced no done"
        prov1 = list(getattr(engine, "_last_distillation_provenance", []) or [])

        fail = FailClient()
        engine.llm_client = fail
        engine._thinking_client_override = fail
        engine._instant_client_override = fail
        await _drive(engine, "second question")
        prov_after = list(getattr(engine, "_last_distillation_provenance", []) or [])

        assert prov1 == [] or prov_after != prov1, (
            "518-C1 PROVENANCE LEAK: failing turn kept the previous request's "
            f"citation-confidence inputs (_last_distillation_provenance "
            f"{len(prov1)} entries still present after the failed turn) — "
            "provenance must be request-local"
        )
