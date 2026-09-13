"""Issue #518 acceptance check C2 / AC2 — cross-process admission layer.

NEW-SURFACE spec (frozen). The implementation must provide an importable
module ``app/services/admission.py`` exposing exactly this contract:

* ``AdmissionClass`` — str Enum: CHAT, INSTANT, EMBEDDING, RERANKING,
  VISION, BACKGROUND (values "chat" ... "background").
* ``AdmissionRejected(Exception)`` with attribute ``reason`` — "queue_full",
  "deadline_exceeded", or "shutdown".
* ``AdmissionStore`` — async store ABC: ``try_acquire(key, holder,
  ttl_seconds) -> bool``, ``release(key, holder)``, ``occupancy(key) -> int``,
  ``sweep_expired(key) -> int``. The cross-process seam: controllers sharing
  one store object share budgets (the production Redis store implements the
  same ABC).
* ``MemoryAdmissionStore(AdmissionStore)`` — in-process reference store,
  ``__init__(clock: Callable[[], float] | None = None)``; an acquire against
  a key whose holders are all past TTL must succeed (stale = swept).
* ``AdmissionController`` — kwargs ``budgets: Dict[str, int]`` (budget-key
  -> limit), ``class_budgets: Dict[AdmissionClass, str]``, ``queue_max_size
  = 64``, ``store = None``, ``enabled = True``, ``ttl_seconds = 30.0``,
  ``default_deadline = None``, ``instance_id = None``; async ctx-manager
  ``admit(admission_class, *, deadline=None, foreground=True)`` yielding a
  lease (``.admission_class``/``.holder``, idempotent ``release()``);
  ``queue_depth(cls) -> int``; ``budget_for(cls) -> int``; ``degraded``
  property; ``shutdown()``; ``from_settings(settings)`` classmethod.
* ``get_admission_controller()`` — process singleton accessor.

Behavior: shared budgets across controller instances via the store;
foreground preference over queued background waiters without starving
background; deadline rejection without executing; cancellation releases the
wait; queue-full rejects immediately (no retry storm); failing store
degrades fail-open with ``degraded`` set; shutdown releases in-flight slots
and rejects new admits; stale holders past TTL expire; ``enabled=False``
never touches the store; the chat route and the engine LLM-stream path
consult admission via ``get_admission_controller``.
"""

import asyncio
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services.admission import (  # noqa: F401
    AdmissionClass,
    AdmissionController,
    AdmissionRejected,
    AdmissionStore,
    MemoryAdmissionStore,
    get_admission_controller,
)


class CountingStore(AdmissionStore):
    """Delegating store that counts try_acquire calls (retry-storm guard)."""

    def __init__(self, inner: AdmissionStore):
        self.inner = inner
        self.acquire_attempts = 0

    async def try_acquire(self, key, holder, ttl_seconds):
        self.acquire_attempts += 1
        return await self.inner.try_acquire(key, holder, ttl_seconds)

    async def release(self, key, holder):
        await self.inner.release(key, holder)

    async def occupancy(self, key):
        return await self.inner.occupancy(key)

    async def sweep_expired(self, key):
        return await self.inner.sweep_expired(key)


class ExplodingStore(AdmissionStore):
    """Store whose every operation fails — unreachable shared backend."""

    async def _boom(self, *args, **kwargs):
        raise RuntimeError("admission backend unreachable")

    try_acquire = release = occupancy = sweep_expired = _boom


INFERENCE_BUDGETS = {"dev": 1}
INFERENCE_CLASSES = {
    AdmissionClass.CHAT: "dev",
    AdmissionClass.BACKGROUND: "dev",
}


async def _wait_for(task, timeout=2.0):
    return await asyncio.wait_for(task, timeout)




def _controller(store=None, **kwargs):
    kwargs.setdefault("budgets", INFERENCE_BUDGETS)
    kwargs.setdefault("class_budgets", INFERENCE_CLASSES)
    if store is not None:
        kwargs["store"] = store
    return AdmissionController(**kwargs)


async def test_budget_is_shared_across_controller_instances():
    """Two coordinator instances sharing ONE store must share ONE budget:
    while worker-a holds the only slot, worker-b and worker-c block; after
    the release exactly one of them is admitted."""
    store = MemoryAdmissionStore()
    ctrl_a = _controller(store, instance_id="worker-a")
    ctrl_b = _controller(store, instance_id="worker-b")
    ctrl_c = _controller(store, instance_id="worker-c")

    lease_cm = ctrl_a.admit(AdmissionClass.CHAT)
    lease_a = await lease_cm.__aenter__()
    assert lease_a.admission_class == AdmissionClass.CHAT
    assert lease_a.holder

    admitted = []

    async def contender(controller, tag):
        async with controller.admit(AdmissionClass.CHAT):
            admitted.append(tag)
            await asyncio.sleep(0.02)

    task_b = asyncio.create_task(contender(ctrl_b, "b"))
    task_c = asyncio.create_task(contender(ctrl_c, "c"))
    await asyncio.sleep(0.05)
    assert admitted == [], (
        "518-C2 SHARED BUDGET VIOLATED: a second coordinator instance was "
        "admitted while the only shared slot was held"
    )

    await lease_cm.__aexit__(None, None, None)
    # AMEND (CHECK_WRONG, issue-518 trace): asyncio.wait defaults to
    # ALL_COMPLETED, so sequential b-then-c completion inside the window
    # yields 2 done and the assertion can never hold under any correct
    # auto-admitting implementation. FIRST_COMPLETED captures the assertion's
    # actual intent: at most ONE waiter completes per freed slot.
    done, pending = await asyncio.wait(
        {task_b, task_c}, timeout=2.0, return_when=asyncio.FIRST_COMPLETED
    )
    assert len(done) == 1, (
        f"518-C2 SHARED BUDGET VIOLATED: after release, {len(done)} "
        "waiters were admitted simultaneously under a budget of 1"
    )
    await _wait_for(pending.pop())
    assert sorted(admitted) == ["b", "c"]




async def test_foreground_preference_with_background_fairness():
    """With a 1-slot budget hogged by background work and background waiters
    already queued, a foreground chat request must be admitted first (bounded
    wait), and the background waiters must still all complete (no
    starvation)."""
    ctrl = _controller(queue_max_size=8)
    holder_cm = ctrl.admit(AdmissionClass.BACKGROUND)
    await holder_cm.__aenter__()

    completion_order = []

    async def bg_waiter(tag):
        async with ctrl.admit(AdmissionClass.BACKGROUND):
            completion_order.append(tag)

    bg_tasks = [asyncio.create_task(bg_waiter(f"bg{i}")) for i in range(3)]
    await asyncio.sleep(0.05)
    assert completion_order == [], "518-C2 HARNESS: background waiters ran early"

    async def fg_turn():
        async with ctrl.admit(AdmissionClass.CHAT, foreground=True):
            completion_order.append("fg")

    started = time.monotonic()
    await _wait_for(asyncio.create_task(fg_turn()), timeout=2.0)
    fg_wait = time.monotonic() - started
    assert "fg" in completion_order, (
        "518-C2 NO FOREGROUND PREFERENCE: foreground chat turn was not "
        "admitted within a bounded wait while background work held the slot"
    )
    assert completion_order.index("fg") < completion_order.index("bg0"), (
        f"518-C2 NO FOREGROUND PREFERENCE: foreground turn completed at "
        f"position {completion_order.index('fg')} behind already-queued "
        f"background waiters ({completion_order})"
    )
    assert fg_wait < 2.0

    await holder_cm.__aexit__(None, None, None)
    await _wait_for(asyncio.gather(*bg_tasks), timeout=2.0)
    assert set(completion_order) == {"fg", "bg0", "bg1", "bg2"}, (
        "518-C2 BACKGROUND STARVED: queued background waiters did not all "
        f"complete under sustained foreground load ({completion_order})"
    )




async def test_deadline_exceeded_rejects_without_executing():
    """A queued request whose deadline expires must be REJECTED, never
    executed, and must not consume the slot."""
    ctrl = _controller(default_deadline=5.0)
    holder_cm = ctrl.admit(AdmissionClass.CHAT)
    await holder_cm.__aenter__()

    executed, rejections = [], []

    async def deadline_turn():
        try:
            async with ctrl.admit(AdmissionClass.CHAT, deadline=0.05):
                executed.append("ran")
        except AdmissionRejected as exc:
            rejections.append(exc.reason)

    started = time.monotonic()
    await _wait_for(asyncio.create_task(deadline_turn()), timeout=1.0)
    assert rejections == ["deadline_exceeded"], (
        f"518-C2 DEADLINE NOT PROPAGATED: expected AdmissionRejected with "
        f"reason 'deadline_exceeded', got {rejections}"
    )
    assert time.monotonic() - started < 1.0

    await holder_cm.__aexit__(None, None, None)
    await asyncio.sleep(0.02)
    assert executed == [], (
        "518-C2 DEADLINED REQUEST EXECUTED: the rejected request ran after "
        "the holder released — deadline must reject, not defer execution"
    )
    # Slot must be reusable immediately (rejected waiter holds nothing).
    async with ctrl.admit(AdmissionClass.CHAT):
        pass




async def test_cancelled_waiter_releases_queue_slot():
    """Client disconnect (task cancellation) while queued must free the wait
    so later requests are admitted promptly."""
    ctrl = _controller()
    holder_cm = ctrl.admit(AdmissionClass.CHAT)
    await holder_cm.__aenter__()

    entered = []

    async def disconnected_client():
        async with ctrl.admit(AdmissionClass.CHAT):
            entered.append("ran")

    waiter = asyncio.create_task(disconnected_client())
    await asyncio.sleep(0.05)
    assert await ctrl.queue_depth(AdmissionClass.CHAT) >= 1

    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    await holder_cm.__aexit__(None, None, None)

    assert await ctrl.queue_depth(AdmissionClass.CHAT) == 0, (
        "518-C2 CANCEL LEAKED QUEUE SLOT: queue depth non-zero after the "
        "queued waiter was cancelled"
    )
    async with ctrl.admit(AdmissionClass.CHAT):
        pass




async def test_queue_full_rejects_immediately_without_retry_storm():
    """When the queue is at its bound, a new request is rejected immediately
    (no unbounded retry amplification against the shared store)."""
    store = CountingStore(MemoryAdmissionStore())
    ctrl = _controller(store, queue_max_size=1)
    holder_cm = ctrl.admit(AdmissionClass.CHAT)
    await holder_cm.__aenter__()

    async def queued_waiter():
        with pytest.raises(AdmissionRejected):
            async with ctrl.admit(AdmissionClass.CHAT, deadline=10.0):
                pass

    waiter = asyncio.create_task(queued_waiter())
    await asyncio.sleep(0.05)

    started = time.monotonic()
    with pytest.raises(AdmissionRejected) as excinfo:
        async with ctrl.admit(AdmissionClass.CHAT, deadline=10.0):
            pass
    elapsed = time.monotonic() - started
    assert excinfo.value.reason == "queue_full", (
        f"518-C2 UNBOUNDED OVERLOAD: expected reason 'queue_full', got "
        f"{excinfo.value.reason!r}"
    )
    assert elapsed < 0.5, (
        f"518-C2 UNBOUNDED OVERLOAD: queue-full rejection took {elapsed:.3f}s "
        "(retry storm instead of a bounded, immediate rejection)"
    )
    assert store.acquire_attempts < 25, (
        f"518-C2 RETRY STORM: {store.acquire_attempts} store acquire "
        "attempts for one rejected request — overload must be bounded"
    )
    await holder_cm.__aexit__(None, None, None)
    await _wait_for(waiter, timeout=2.0)




async def test_unreachable_store_degrades_open_and_reports():
    """When the shared admission backend is unreachable, admission fails
    OPEN (requests still admitted) and the controller reports degraded."""
    ctrl = _controller(ExplodingStore())
    assert ctrl.degraded is False
    started = time.monotonic()
    async with ctrl.admit(AdmissionClass.CHAT):
        pass
    assert time.monotonic() - started < 0.5, (
        "518-C2 DEGRADED STORE STALLED: admission blocked on an unreachable "
        "shared backend instead of failing open"
    )
    assert ctrl.degraded is True, (
        "518-C2 DEGRADED STATE NOT REPORTED: controller did not set "
        ".degraded after shared-store failures"
    )




async def test_shutdown_releases_inflight_and_rejects_new():
    """shutdown() must release in-flight slots (a sibling coordinator can
    immediately use the budget) and reject new admissions."""
    store = MemoryAdmissionStore()
    ctrl = _controller(store, instance_id="worker-a")
    sibling = _controller(store, instance_id="worker-b")
    lease_cm = ctrl.admit(AdmissionClass.CHAT)
    await lease_cm.__aenter__()

    await ctrl.shutdown()
    assert store and await store.occupancy("dev") == 0, (
        "518-C2 SHUTDOWN LEFT SLOT HELD: in-flight lease was not released "
        "on shutdown"
    )
    # Sibling proves the shared budget is free again.
    async with sibling.admit(AdmissionClass.CHAT):
        pass
    # New admissions on the shut-down controller are rejected.
    with pytest.raises(AdmissionRejected) as excinfo:
        async with ctrl.admit(AdmissionClass.CHAT):
            pass
    assert excinfo.value.reason == "shutdown"


async def test_stale_holder_past_ttl_is_expired():
    """A holder that died without releasing (past its TTL) must not block
    new admissions — stale holders are swept on acquire."""
    clock = {"now": 0.0}
    store = MemoryAdmissionStore(clock=lambda: clock["now"])
    ctrl_a = _controller(store, ttl_seconds=0.1, instance_id="worker-a")
    ctrl_b = _controller(store, ttl_seconds=0.1, instance_id="worker-b")
    ghost_cm = ctrl_a.admit(AdmissionClass.CHAT)
    await ghost_cm.__aenter__()  # holder then "dies" — never released
    clock["now"] += 5.0  # far past the 0.1s TTL

    started = time.monotonic()
    async with ctrl_b.admit(AdmissionClass.CHAT):
        pass
    assert time.monotonic() - started < 0.5, (
        "518-C2 STALE HOLDER NOT EXPIRED: a holder past its TTL still "
        "blocked admission from a healthy coordinator"
    )




async def test_disabled_mode_is_passthrough_with_zero_store_calls():
    store = CountingStore(MemoryAdmissionStore())
    ctrl = _controller(store, enabled=False)
    started = time.monotonic()
    async with ctrl.admit(AdmissionClass.CHAT):
        pass
    assert time.monotonic() - started < 0.05
    assert store.acquire_attempts == 0, (
        "518-C2 DISABLED MODE NOT PASSTHROUGH: admission consulted the "
        "shared store while disabled"
    )




def test_from_settings_reads_admission_config_keys():
    from app.config import Settings

    s = Settings(
        admission_enabled=True,
        admission_chat_budget=3,
        admission_background_budget=7,
        admission_queue_max_size=11,
    )
    ctrl = AdmissionController.from_settings(s)
    assert ctrl.budget_for(AdmissionClass.CHAT) == 3
    assert ctrl.budget_for(AdmissionClass.BACKGROUND) == 7
    for cls in AdmissionClass:
        assert ctrl.budget_for(cls) >= 1

    disabled = AdmissionController.from_settings(
        Settings(admission_enabled=False)
    )
    assert disabled.enabled is False
    assert isinstance(get_admission_controller(), AdmissionController)
    assert get_admission_controller() is get_admission_controller()




class _RecController:
    """Records admit() consultations; stands in for the singleton."""

    def __init__(self):
        self.calls = []

    @asynccontextmanager
    async def admit(self, admission_class, **kwargs):
        self.calls.append((admission_class, kwargs))
        yield SimpleNamespace(admission_class=admission_class, holder="rec")


async def test_engine_stream_path_consults_admission():
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
    with patch.object(rag_engine_module, "get_admission_controller",
                      lambda: recorder), \
            patch.object(settings, "query_transformation_enabled", False):
        chunks = [c async for c in engine.query("hello", [], stream=True)]
    assert chunks, "518-C2 HARNESS: engine query produced no chunks"
    chat_calls = [c for c, _ in recorder.calls if c == AdmissionClass.CHAT]
    assert chat_calls, (
        "518-C2 ENGINE NOT WIRED: the RAG engine stream path never "
        "consulted admission for AdmissionClass.CHAT via "
        "get_admission_controller()"
    )


def test_chat_stream_route_consults_admission():
    from app.api.deps import get_rag_engine, get_vector_store
    from app.api.routes import chat as chat_module
    from app.api.routes.chat import get_stream_auth

    recorder = _RecController()

    async def fake_query(*args, **kwargs):
        yield {"type": "content", "content": "ok"}
        yield {"type": "done", "sources": [], "memories_used": []}

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

    client = TestClient(app)
    with patch.object(chat_module, "get_admission_controller",
                      lambda: recorder):
        response = client.post(
            "/api/chat/stream",
            json={"messages": [{"role": "user", "content": "hi"}]},
        )
    assert response.status_code == 200, response.text[:400]
    chat_calls = [c for c, _ in recorder.calls if c == AdmissionClass.CHAT]
    assert chat_calls, (
        "518-C2 ROUTE NOT WIRED: POST /api/chat/stream never consulted "
        "admission for AdmissionClass.CHAT via get_admission_controller()"
    )
    app.dependency_overrides.clear()
