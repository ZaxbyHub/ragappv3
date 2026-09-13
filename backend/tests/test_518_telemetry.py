"""Issue #518 acceptance checks C3/AC3 + C4/AC4 — telemetry, correlation,
metrics surface, RequestIdFilter registration.

NEW-SURFACE spec (frozen). The implementation must provide an importable
module ``app/services/telemetry.py`` exposing exactly this contract:

* ``PROVIDER_OUTCOMES = ("ok", "partial", "unavailable", "empty")``.
* ``class Telemetry`` with ``__init__(*, enabled: bool = True,
  registry_dir: Path | None = None)`` and:
  - ``record_chat_turn(turn_id) -> None``
  - ``record_stage(turn_id, stage, duration_seconds) -> None``
  - ``record_queue_wait(admission_class, wait_seconds, depth=0) -> None``
  - ``record_first_useful_content(turn_id, seconds) -> None``
  - ``record_provider_call(provider, outcome) -> None`` — raises
    ``ValueError`` for an outcome outside PROVIDER_OUTCOMES
  - ``record_embedding_cache(hit: bool) -> None``
  - ``snapshot() -> dict`` with keys ``chat_turns`` (int),
    ``stage_durations`` ({turn_id: {stage: seconds}}), ``queue_waits``
    ({admission_class: [wait_seconds]}), ``queue_depths``
    ({admission_class: [depth]}), ``first_useful_content``
    ({turn_id: seconds}), ``provider_calls`` ({provider: {outcome: int}}),
    ``embedding_cache_hits`` (int)
  - ``metrics_text() -> str`` — Prometheus text exposition containing the
    metric names ``ragapp_chat_turns_total``, ``ragapp_queue_depth``,
    ``ragapp_queue_wait_seconds``, ``ragapp_provider_calls_total``; when
    ``registry_dir`` is set, aggregates records from every Telemetry
    instance sharing that directory (the multiprocess mechanism)
  - ``reset() -> None``
  Disabled mode (``enabled=False``): recorders are no-ops, snapshot stays
  empty/zero, ``metrics_text()`` carries no ``ragapp_`` samples.
* ``init_telemetry(registry_dir: Path | None = None) -> Telemetry`` —
  (re)binds the singleton to the current ``settings.telemetry_enabled``.
* ``get_telemetry() -> Telemetry``.
* ``set_current_turn(turn_id) -> None`` / ``current_turn_id() -> str|None``.
* ``correlation_headers() -> dict`` — ``{"traceparent": <W3C
  00-<32hex>-<16hex>-0X>, "X-Request-ID": <current turn id>}`` when
  enabled and a turn is set; ``{}`` when disabled (no turn set is also
  allowed to be empty).
* ``register_metrics_route(app) -> None`` — attaches ``GET /metrics``
  (text/plain, Prometheus exposition).

Production wiring asserted here (behavior, not implementation):
* outbound httpx calls made by the REAL EmbeddingService and LLMClient
  carry ``traceparent`` + ``X-Request-ID`` of the current turn when
  telemetry is enabled, and NO correlation headers when disabled;
* the chat SSE terminal ``done`` event carries a non-empty per-turn
  correlation id under the key ``turn_id``;
* RequestIdFilter is registered through the actual logging setup
  (app/lifespan.py) and the same identity flows outbound.
"""

import logging
import re
from unittest.mock import MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services.telemetry import (
    PROVIDER_OUTCOMES,
    Telemetry,
    correlation_headers,
    get_telemetry,
    init_telemetry,
    register_metrics_route,
    set_current_turn,
)

W3C_TRACEPARENT = re.compile(r"^00-[0-9a-f]{32}-[0-9a-f]{16}-0[01]$")

EMBED_RESPONSE = {"data": [{"embedding": [0.1, 0.2, 0.3]}]}
CHAT_RESPONSE = {
    "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}]
}


# ── recording API ────────────────────────────────────────────────────────


def test_telemetry_records_measurements_not_constants():
    tel = Telemetry(enabled=True)
    tel.record_chat_turn("t1")
    tel.record_stage("t1", "retrieval", 0.15)
    tel.record_stage("t1", "generation", 0.42)
    tel.record_queue_wait("chat", 0.05, depth=2)
    tel.record_first_useful_content("t1", 0.30)
    tel.record_provider_call("embeddings", "ok")
    tel.record_provider_call("llm", "unavailable")
    tel.record_provider_call("llm", "empty")
    tel.record_embedding_cache(True)
    tel.record_embedding_cache(True)
    tel.record_embedding_cache(False)

    snap = tel.snapshot()
    assert snap["chat_turns"] == 1
    assert snap["stage_durations"]["t1"]["retrieval"] == pytest.approx(0.15)
    assert snap["stage_durations"]["t1"]["generation"] == pytest.approx(0.42)
    assert snap["queue_waits"]["chat"] == [pytest.approx(0.05)]
    assert snap["queue_depths"]["chat"] == [2]
    assert snap["first_useful_content"]["t1"] == pytest.approx(0.30)
    assert snap["provider_calls"]["embeddings"]["ok"] == 1
    assert snap["provider_calls"]["llm"]["unavailable"] == 1
    assert snap["provider_calls"]["llm"]["empty"] == 1
    assert snap["embedding_cache_hits"] == 2

    text = tel.metrics_text()
    for name in (
        "ragapp_chat_turns_total",
        "ragapp_queue_depth",
        "ragapp_queue_wait_seconds",
        "ragapp_provider_calls_total",
    ):
        assert name in text, (
            f"518-C3 METRIC MISSING: Prometheus exposition lacks {name}"
        )


def test_provider_outcome_enum_is_enforced():
    assert set(PROVIDER_OUTCOMES) >= {"ok", "partial", "unavailable", "empty"}
    tel = Telemetry(enabled=True)
    with pytest.raises(ValueError):
        tel.record_provider_call("llm", "bogus-outcome")


def test_disabled_telemetry_is_inert():
    tel = Telemetry(enabled=False)
    tel.record_chat_turn("t1")
    tel.record_stage("t1", "retrieval", 0.1)
    tel.record_provider_call("llm", "ok")
    snap = tel.snapshot()
    assert snap["chat_turns"] == 0
    assert snap["stage_durations"] == {}
    assert snap["provider_calls"] == {}
    assert "ragapp_" not in tel.metrics_text()


def test_multiprocess_registry_aggregates_across_instances(tmp_path):
    """Two Telemetry instances (simulated worker processes) sharing one
    registry directory must aggregate into a single exposition."""
    worker1 = Telemetry(enabled=True, registry_dir=tmp_path)
    worker2 = Telemetry(enabled=True, registry_dir=tmp_path)
    worker1.record_chat_turn("t1")
    worker2.record_chat_turn("t2")

    totals = []
    for line in worker1.metrics_text().splitlines():
        if line.startswith("ragapp_chat_turns_total"):
            totals.append(float(line.rsplit(" ", 1)[1]))
    assert totals and max(totals) >= 2, (
        "518-C3 NOT MULTIPROCESS-SAFE: two registry-sharing Telemetry "
        f"instances did not aggregate (chat_turns values seen: {totals})"
    )


# ── correlation context + headers ────────────────────────────────────────


def test_correlation_headers_carry_w3c_traceparent_and_request_id():
    with patch("app.config.settings.telemetry_enabled", True):
        init_telemetry()
    set_current_turn("turn-518-correlate")
    headers = correlation_headers()
    assert set(headers) == {"traceparent", "X-Request-ID"}, (
        f"518-C3 CORRELATION HEADERS WRONG: {sorted(headers)}"
    )
    assert W3C_TRACEPARENT.match(headers["traceparent"]), (
        f"518-C3 BAD TRACEPARENT: {headers['traceparent']!r} is not W3C "
        "00-traceid-spanid-flags format"
    )
    assert headers["X-Request-ID"] == "turn-518-correlate"


def test_correlation_headers_empty_when_telemetry_disabled():
    with patch("app.config.settings.telemetry_enabled", False):
        init_telemetry()
        set_current_turn("turn-518-off")
        assert correlation_headers() == {}, (
            "518-C3 TELEMETRY DISABLED MUST BE INERT: correlation headers "
            "were produced with telemetry_enabled=False"
        )


# ── outbound propagation through REAL provider services ─────────────────


def _install_send_interceptor(monkeypatch, body):
    captured = []

    async def fake_send(self, request, **kwargs):
        captured.append(request)
        return httpx.Response(200, json=body, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "send", fake_send)
    return captured


async def test_outbound_httpx_carries_correlation_when_enabled(monkeypatch):
    """The REAL EmbeddingService and LLMClient outbound requests must carry
    the same turn's traceparent + X-Request-ID (correlation across provider
    calls)."""
    embed_captured = _install_send_interceptor(monkeypatch, EMBED_RESPONSE)
    with patch("app.config.settings.telemetry_enabled", True):
        init_telemetry()
        set_current_turn("turn-518-outbound")
        with patch("app.services.embeddings.assert_url_safe"):
            from app.services.embeddings import EmbeddingService

            service = EmbeddingService()
        try:
            vec = await service.embed_single("correlation probe text")
        finally:
            try:
                await service._client.aclose()
            except Exception:
                pass
    assert vec == [0.1, 0.2, 0.3]
    assert embed_captured, "518-C3 HARNESS: embedding call never sent"
    for request in embed_captured:
        tp = request.headers.get("traceparent", "")
        assert W3C_TRACEPARENT.match(tp), (
            f"518-C3 NO OUTBOUND CORRELATION: embedding request to "
            f"{request.url} carried traceparent={tp!r} (W3C format required)"
        )
        assert request.headers.get("X-Request-ID") == "turn-518-outbound", (
            "518-C3 NO OUTBOUND CORRELATION: embedding request carried "
            f"X-Request-ID={request.headers.get('X-Request-ID')!r}"
        )

    chat_captured = _install_send_interceptor(monkeypatch, CHAT_RESPONSE)
    with patch("app.config.settings.telemetry_enabled", True):
        init_telemetry()
        set_current_turn("turn-518-outbound")
        with patch("app.services.llm_client.assert_url_safe"):
            from app.services.llm_client import LLMClient

            client = LLMClient()
        try:
            content = await client.chat_completion(
                [{"role": "user", "content": "hi"}]
            )
        finally:
            try:
                await client.aclose()
            except Exception:
                pass
    assert content == "hi"
    assert chat_captured, "518-C3 HARNESS: LLM call never sent"
    for request in chat_captured:
        assert W3C_TRACEPARENT.match(
            request.headers.get("traceparent", "")
        ), (
            f"518-C3 NO OUTBOUND CORRELATION: LLM request to {request.url} "
            "carried no valid traceparent"
        )
        assert request.headers.get("X-Request-ID") == "turn-518-outbound", (
            "518-C3 CORRELATION NOT SHARED ACROSS PROVIDERS: LLM request "
            "did not carry the same X-Request-ID as the embedding request"
        )


async def test_outbound_httpx_adds_no_headers_when_disabled(monkeypatch):
    captured = _install_send_interceptor(monkeypatch, EMBED_RESPONSE)
    with patch("app.config.settings.telemetry_enabled", False):
        init_telemetry()
        with patch("app.services.embeddings.assert_url_safe"):
            from app.services.embeddings import EmbeddingService

            service = EmbeddingService()
        try:
            await service.embed_single("disabled mode probe")
        finally:
            try:
                await service._client.aclose()
            except Exception:
                pass
    assert captured, "518-C3 HARNESS: embedding call never sent"
    for request in captured:
        assert "traceparent" not in request.headers, (
            "518-C3 TELEMETRY DISABLED MUST BE INERT: outbound request "
            f"to {request.url} carried a traceparent header"
        )
        assert "X-Request-ID" not in request.headers, (
            "518-C3 TELEMETRY DISABLED MUST BE INERT: outbound request "
            f"to {request.url} carried an X-Request-ID header"
        )


# ── chat SSE turn correlation ────────────────────────────────────────────


def _build_stream_app():
    from app.api.deps import get_rag_engine, get_vector_store
    from app.api.routes import chat as chat_module
    from app.api.routes.chat import get_stream_auth

    async def fake_query(*args, **kwargs):
        yield {"type": "content", "content": "answer"}
        yield {
            "type": "done", "sources": [], "memories_used": [],
            "wiki_used": [], "kms_used": [],
        }

    class _Engine:
        query = fake_query

    ready_vs = MagicMock()
    ready_vs._ready = True

    app = FastAPI()
    app.include_router(chat_module.router, prefix="/api")
    app.dependency_overrides[get_rag_engine] = lambda: _Engine()
    app.dependency_overrides[get_stream_auth] = lambda: {
        "id": "u1", "username": "u", "email": "u@e", "role": "admin",
    }
    app.dependency_overrides[get_vector_store] = lambda: ready_vs
    return app


def test_chat_stream_done_carries_turn_correlation_id():
    with patch("app.config.settings.telemetry_enabled", True):
        init_telemetry()
        app = _build_stream_app()
        client = TestClient(app)
        response = client.post(
            "/api/chat/stream",
            json={"messages": [{"role": "user", "content": "hi"}]},
        )
    assert response.status_code == 200, response.text[:400]
    dones = []
    for line in response.text.splitlines():
        if line.startswith("data: "):
            import json as _json

            payload = _json.loads(line[len("data: "):])
            if payload.get("type") == "done":
                dones.append(payload)
    assert dones, "518-C3 HARNESS: no done event on the SSE stream"
    for done in dones:
        turn_id = done.get("turn_id")
        assert isinstance(turn_id, str) and turn_id, (
            "518-C3 NO TURN CORRELATION ON SSE DONE: terminal done event "
            f"carried turn_id={turn_id!r} — per-turn correlation id required"
        )
    app.dependency_overrides.clear()


# ── /metrics surface ─────────────────────────────────────────────────────


def test_metrics_endpoint_exposes_required_families():
    tel = get_telemetry()
    tel.reset()
    tel.record_chat_turn("t1")
    tel.record_queue_wait("chat", 0.05, depth=1)
    tel.record_provider_call("llm", "ok")

    app = FastAPI()
    register_metrics_route(app)
    client = TestClient(app)
    response = client.get("/metrics")
    assert response.status_code == 200, response.text[:200]
    assert response.headers["content-type"].startswith("text/plain")
    for name in (
        "ragapp_chat_turns_total",
        "ragapp_queue_depth",
        "ragapp_queue_wait_seconds",
        "ragapp_provider_calls_total",
    ):
        assert name in response.text, (
            f"518-C3 METRIC MISSING: /metrics lacks {name}"
        )


# ── C4 / AC4 — RequestIdFilter registration + shared identity ───────────


def test_request_id_filter_registered_through_logging_setup():
    """The actual logging setup in app/lifespan.py must attach
    RequestIdFilter to the root handlers (both the fresh-handler and
    existing-handler branches)."""
    from pathlib import Path

    lifespan_src = (
        Path(__file__).resolve().parents[1] / "app" / "lifespan.py"
    ).read_text(encoding="utf-8")
    assert "addFilter(RequestIdFilter())" in lifespan_src, (
        "518-C4 REQUEST_ID_FILTER NOT REGISTERED: app/lifespan.py logging "
        "setup no longer attaches RequestIdFilter to root handlers"
    )
    assert lifespan_src.count("RequestIdFilter") >= 2


def test_request_id_filter_identity_flows_outbound():
    """The request id stamped by RequestIdFilter from ``request_id_var`` is
    the same identity that flows outbound on httpx (via the telemetry
    correlation headers)."""
    from app.utils.request_context import RequestIdFilter, request_id_var

    token = request_id_var.set("req-518-identity")
    try:
        record = logging.makeLogRecord(
            {"name": "t", "level": logging.INFO, "msg": "m"}
        )
        assert RequestIdFilter().filter(record) is True
        assert record.request_id == "req-518-identity", (
            "518-C4 FILTER BROKEN: RequestIdFilter did not stamp "
            f"record.request_id from request_id_var (got {record.request_id!r})"
        )
        with patch("app.config.settings.telemetry_enabled", True):
            init_telemetry()
        set_current_turn("req-518-identity")
        headers = correlation_headers()
        assert headers.get("X-Request-ID") == "req-518-identity", (
            "518-C4 IDENTITY NOT SHARED OUTBOUND: the outbound "
            f"X-Request-ID ({headers.get('X-Request-ID')!r}) differs from "
            "the request id stamped by RequestIdFilter"
        )
    finally:
        request_id_var.reset(token)


def test_telemetry_module_exposes_singleton():
    with patch("app.config.settings.telemetry_enabled", True):
        first = init_telemetry()
    assert isinstance(first, Telemetry)
    assert get_telemetry() is first
