"""Issue #518 E3-closure behavioral complements (unit level).

Pins the behavioral paths of the E3 closure work that the frozen contract
checks do not exercise directly:

* the tracer shim layer in ``app.services.telemetry`` (``_NoOpTracer`` /
  ``_NoOpSpan``, ``register_span_middleware``, and the span-context branch
  of ``correlation_headers``);
* ``TelemetrySpanMiddleware`` recording one server span per request with
  ``http.*`` attributes and no user content;
* ``AdmissionController`` degraded-state recovery after a store outage,
  including the lease RENEWAL heartbeat path (a live holder's refresh
  clears a previously-set degraded flag);
* the promote-memory route's BACKGROUND admission gate (503 on
  AdmissionRejected, request proceeds when admitted);
* the Telemetry OTel-observer notification path that feeds the gen_ai
  counter bridge (observer fired with None on each stage record).

The opentelemetry extra is optional; these tests exercise the no-op and
stub paths only. On machines where ``opentelemetry`` happens to be
importable (a namespace-only install also satisfies ``import
opentelemetry``), the shim tests block the import with a ``sys.modules``
None sentinel so ``_resolve_tracer`` still resolves the no-op shim — the
documented air-gapped off-state.
"""

import asyncio
import hashlib
import os
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.api.deps import (  # noqa: E402
    get_current_active_user,
    get_db,
    get_evaluate_policy,
)
from app.api.routes import wiki as wiki_routes  # noqa: E402
from app.middleware.telemetry_span import (  # noqa: E402
    TelemetrySpanMiddleware,
)
from app.security import csrf_protect  # noqa: E402
from app.services import telemetry  # noqa: E402
from app.services.admission import (  # noqa: E402
    AdmissionClass,
    AdmissionController,
    AdmissionRejected,
    MemoryAdmissionStore,
    reset_admission_controller,
)


def _force_noop_tracer_resolution(monkeypatch) -> None:
    """Force ``_resolve_tracer`` down the no-extra path.

    Blocks the ``opentelemetry`` import with a None sentinel in
    ``sys.modules`` (raising ImportError on import attempts) and resets the
    module tracer globals so the next resolution starts clean. The
    monkeypatch fixture restores all three entries afterwards.
    """
    monkeypatch.setitem(sys.modules, "opentelemetry", None)
    monkeypatch.setattr(telemetry, "_tracer", None)
    monkeypatch.setattr(telemetry, "_tracer_resolved", False)


# ---------------------------------------------------------------------------
# 1. Tracer shim
# ---------------------------------------------------------------------------


class TestTracerShim:
    def test_noop_tracer_without_extra(self, monkeypatch):
        _force_noop_tracer_resolution(monkeypatch)
        tracer = telemetry._resolve_tracer()
        assert isinstance(tracer, telemetry._NoOpTracer)
        # The public surface resolves the same cached shim.
        assert telemetry.get_tracer() is tracer
        assert telemetry._tracer_resolved is True

        span = telemetry.start_span("x")
        entered = None
        with span as yielded:  # entering and exiting must be safe
            entered = yielded
        assert entered is span
        # The shimmed span accepts attributes and returns None (otel spans
        # also return None from set_attribute — the shim must match).
        assert span.set_attribute("http.request.method", "GET") is None

    def test_register_span_middleware_is_noop_without_extra(self, monkeypatch):
        _force_noop_tracer_resolution(monkeypatch)
        app = FastAPI()
        before = list(app.user_middleware)
        telemetry.register_span_middleware(app)
        assert app.user_middleware == before
        assert len(app.user_middleware) == 0

    def test_correlation_headers_uses_span_context_when_present(
        self, monkeypatch
    ):
        telemetry.set_current_turn("t-x")
        try:
            # With a live span context, traceparent must carry those EXACT
            # hex ids (W3C format 00-<trace>-<span>-01).
            monkeypatch.setattr(
                telemetry,
                "_active_span_context",
                lambda: ("a" * 32, "b" * 16),
            )
            headers = telemetry.correlation_headers()
            assert headers["traceparent"] == (
                "00-" + "a" * 32 + "-" + "b" * 16 + "-01"
            )
            assert headers["X-Request-ID"] == "t-x"

            # Unpatched fallback: no active span context, so the trace id is
            # the deterministic sha256-derived value — identical across two
            # calls; only the random span id may differ.
            monkeypatch.undo()
            first = telemetry.correlation_headers()
            second = telemetry.correlation_headers()
            f_parts = first["traceparent"].split("-")
            s_parts = second["traceparent"].split("-")
            assert f_parts[0] == "00" and f_parts[3] == "01"
            assert s_parts[0] == "00" and s_parts[3] == "01"
            assert len(f_parts[1]) == 32 and len(s_parts[1]) == 32
            assert len(f_parts[2]) == 16 and len(s_parts[2]) == 16
            assert f_parts[1] == s_parts[1]
            assert f_parts[1] == hashlib.sha256(b"t-x").hexdigest()[:32]
            assert first["X-Request-ID"] == "t-x"
            assert second["X-Request-ID"] == "t-x"
        finally:
            telemetry.set_current_turn(None)


# ---------------------------------------------------------------------------
# 2. Span middleware
# ---------------------------------------------------------------------------


class _RecordingSpan:
    def __init__(self, name: str):
        self.name = name
        self.attributes = {}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def set_attribute(self, key, value):
        self.attributes[key] = value


class _RecordingTracer:
    def __init__(self):
        self.spans = []

    def start_as_current_span(self, name, **kwargs):
        span = _RecordingSpan(name)
        self.spans.append(span)
        return span


class TestSpanMiddleware:
    def test_span_middleware_records_server_span(self, monkeypatch):
        stub = _RecordingTracer()
        monkeypatch.setattr(telemetry, "_tracer", stub)
        monkeypatch.setattr(telemetry, "_tracer_resolved", True)

        app = FastAPI()
        app.add_middleware(TelemetrySpanMiddleware)

        @app.get("/ping")
        async def ping():
            return {"ok": True}

        client = TestClient(app)
        response = client.get("/ping")

        assert response.status_code == 200
        assert response.json() == {"ok": True}
        assert len(stub.spans) == 1, "expected exactly one server span"
        span = stub.spans[0]
        assert span.name == "GET /ping"
        assert span.attributes["http.request.method"] == "GET"
        assert span.attributes["url.path"] == "/ping"
        assert span.attributes["http.response.status_code"] == 200


# ---------------------------------------------------------------------------
# 3. Admission degraded-state recovery (admit path and renew path)
# ---------------------------------------------------------------------------


class FlakyMemoryStore(MemoryAdmissionStore):
    """Memory store whose calls raise while ``failures_remaining`` is > 0."""

    def __init__(self):
        super().__init__()
        self.failures_remaining = 0

    async def _maybe_fail(self):
        if self.failures_remaining > 0:
            self.failures_remaining -= 1
            raise ConnectionError("simulated admission store outage")

    async def occupancy(self, key):
        await self._maybe_fail()
        return await super().occupancy(key)

    async def try_acquire(self, key, holder, ttl_seconds):
        await self._maybe_fail()
        return await super().try_acquire(key, holder, ttl_seconds)

    async def release(self, key, holder):
        await self._maybe_fail()
        return await super().release(key, holder)

    async def refresh(self, key, holder, ttl_seconds):
        await self._maybe_fail()
        return await super().refresh(key, holder, ttl_seconds)


async def _degraded_set_then_cleared_scenario():
    store = FlakyMemoryStore()
    store.failures_remaining = 10  # outage outlives the whole failing admit
    controller = AdmissionController(
        budgets={"chat": 2},
        class_budgets={AdmissionClass.CHAT: "chat"},
        queue_max_size=8,
        store=store,
        enabled=True,
        ttl_seconds=30.0,
    )

    # Outage admit: fail-open (the context is entered) AND degraded flips on.
    degraded_during_outage = None
    async with controller.admit(AdmissionClass.CHAT):
        degraded_during_outage = controller.degraded
    await asyncio.sleep(0.05)  # let any scheduled pump tasks drain
    assert degraded_during_outage is True
    assert controller.degraded is True  # still set right after the outage

    # Store recovers; a subsequent successful admit must CLEAR the flag.
    store.failures_remaining = 0
    async with controller.admit(AdmissionClass.CHAT):
        assert controller.degraded is False
    await asyncio.sleep(0.05)
    assert controller.degraded is False


async def _renewal_heartbeat_scenario():
    # ttl_seconds=0.05 puts the renewal interval at its 0.05s floor. To keep
    # the heartbeat refreshes landing BEFORE the store-side TTL expiry (the
    # real service runs ttl=30s, where ttl/3 is far below the floor), the
    # store clock runs at a tenth of real speed: 0.05 store-seconds of TTL
    # equal 0.5 real seconds, while heartbeats fire every 0.05 real seconds.
    start = time.monotonic()

    def slow_clock():
        return start + (time.monotonic() - start) * 0.1

    store = MemoryAdmissionStore(clock=slow_clock)
    controller = AdmissionController(
        budgets={"chat": 2},
        class_budgets={AdmissionClass.CHAT: "chat"},
        queue_max_size=8,
        store=store,
        enabled=True,
        ttl_seconds=0.05,
    )
    async with controller.admit(AdmissionClass.CHAT) as lease:
        # Simulate a prior outage condition; the renewal heartbeat's
        # successful refresh must clear it (the "recovered" half of the
        # degraded contract, driven through the renew path).
        controller._mark_degraded()
        assert controller.degraded is True
        await asyncio.sleep(0.2)  # several heartbeats (interval 0.05s)
        assert controller.degraded is False
        assert lease.revoked is False  # heartbeat kept the slot alive
    await asyncio.sleep(0.05)
    assert controller.degraded is False


class TestAdmissionRecovery:
    def test_degraded_set_on_outage_and_cleared_after_recovery(self):
        try:
            asyncio.run(_degraded_set_then_cleared_scenario())
        finally:
            reset_admission_controller()

    def test_renewal_heartbeat_keeps_lease_and_clears_degraded(self):
        try:
            asyncio.run(_renewal_heartbeat_scenario())
        finally:
            reset_admission_controller()


# ---------------------------------------------------------------------------
# 4. Promote-memory route admission gate
# ---------------------------------------------------------------------------

_PROMOTE_URL = "/api/wiki/promote-memory"
_PROMOTE_PAYLOAD = {"memory_id": 1, "vault_id": 1}


@dataclass
class _FakeWikiPage:
    """Minimal page object shaped like WikiStore's page dataclass (the
    handler's _page_dict reads .claims, .entities, .lint_findings)."""

    title: str = "Promoted memory"
    status: str = "needs_review"
    claims: list = field(default_factory=list)
    entities: list = field(default_factory=list)
    lint_findings: list = field(default_factory=list)


@asynccontextmanager
async def _rejecting_admit():
    raise AdmissionRejected("queue_full")
    yield  # pragma: no cover — unreachable by construction


class _SaturatedController:
    """Admission stub whose admit() context always rejects (queue_full)."""

    def admit(self, admission_class, **kwargs):
        return _rejecting_admit()


def _build_promote_app() -> FastAPI:
    app = FastAPI()
    app.include_router(wiki_routes.router, prefix="/api")

    async def _evaluate(user, resource_type, resource_id, action):
        return True

    app.dependency_overrides[get_current_active_user] = lambda: {
        "id": 1,
        "role": "admin",
        "username": "u",
    }
    app.dependency_overrides[get_evaluate_policy] = lambda: _evaluate
    app.dependency_overrides[csrf_protect] = lambda: "test-token"
    app.dependency_overrides[get_db] = lambda: MagicMock(name="promote-db")
    return app


class TestPromoteMemoryAdmissionGate:
    def test_rejected_admission_yields_503_admitted_proceeds(self):
        app = _build_promote_app()
        compiler_cls = MagicMock(name="WikiCompiler")
        compiler_cls.return_value.promote_memory.return_value = {
            "page": _FakeWikiPage(),
            "claims": [],
            "entities": [],
            "relations": [],
        }
        admitting = AdmissionController(
            budgets={"background": 64},
            class_budgets={AdmissionClass.BACKGROUND: "background"},
            store=MemoryAdmissionStore(),
            enabled=True,
            ttl_seconds=30.0,
        )
        client = TestClient(app)
        try:
            # Saturated controller: the gate raises AdmissionRejected and the
            # handler is meant to map it to 503 without ever reaching the
            # compiler.
            with patch.object(
                wiki_routes, "get_admission_controller",
                lambda: _SaturatedController(),
            ), patch.object(wiki_routes, "WikiCompiler", compiler_cls):
                response = client.post(_PROMOTE_URL, json=_PROMOTE_PAYLOAD)
            # The 503 passes through the handler's broad except via the
            # explicit ``except HTTPException: raise`` ordering (verified
            # by the implementation reviewer): a saturated gate surfaces
            # as 503 with the "saturated" detail, and the compiler is
            # NEVER reached when admission rejects.
            assert response.status_code == 503, (
                f"unexpected status for rejected admission: "
                f"{response.status_code}"
            )
            assert "saturated" in response.text
            assert compiler_cls.return_value.promote_memory.call_count == 0

            # Permissive controller: the request proceeds past the gate, the
            # patched compiler runs via to_thread, and its dict result is
            # serialized into a 200 response.
            with patch.object(
                wiki_routes, "get_admission_controller", lambda: admitting,
            ), patch.object(wiki_routes, "WikiCompiler", compiler_cls):
                response = client.post(_PROMOTE_URL, json=_PROMOTE_PAYLOAD)
            assert response.status_code == 200
            assert compiler_cls.return_value.promote_memory.call_count == 1
            promote_kwargs = (
                compiler_cls.return_value.promote_memory.call_args.kwargs
            )
            assert promote_kwargs["memory_id"] == 1
            assert promote_kwargs["vault_id"] == 1
            assert promote_kwargs["is_admin"] is True
            body = response.json()
            assert body["page"]["title"] == "Promoted memory"
            assert body["page"]["claims"] == []
            assert body["claims"] == []
        finally:
            app.dependency_overrides.clear()
            reset_admission_controller()


# ---------------------------------------------------------------------------
# 5. Telemetry OTel-observer notification path
# ---------------------------------------------------------------------------


class TestGenAiObserverPath:
    def test_record_stage_notifies_otel_observers(self):
        tel = telemetry.Telemetry(enabled=True)
        calls = []
        tel.attach_otel_observer(lambda none_arg: calls.append(none_arg))
        tel.record_stage("t1", "planning", 0.1)
        # The observer is the seam the OTLP gen_ai counter bridge attaches
        # to; it fires once per stage record, with the documented None arg.
        assert calls == [None]
        # The stage itself is what the bridge aggregates into
        # gen_ai.client.operation.duration.
        assert (
            tel.snapshot()["stage_durations"]["t1"]["planning"] == 0.1
        )

        # A failing observer must never break recording (export isolation).
        tel2 = telemetry.Telemetry(enabled=True)

        def _boom(none_arg):
            raise RuntimeError("export pipeline down")

        tel2.attach_otel_observer(_boom)
        received = []
        tel2.attach_otel_observer(lambda none_arg: received.append(none_arg))
        tel2.record_stage("t2", "generation", 0.2)
        assert received == [None]
        assert tel2.snapshot()["stage_durations"]["t2"]["generation"] == 0.2
