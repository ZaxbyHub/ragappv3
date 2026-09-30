"""Issue #687 regression guards — behaviors added by the fix that the frozen
acceptance checks do not already pin, plus the defect-class structural
guardrail.

Covers (per the approved plan):
- F2 pump re-latch termination (no runaway pumps; release mid-pump settles)
- F3 MemoryAdmissionStore atomic budget enforcement across shared controllers
- F4 from_settings mixed-load semantics: realizable sum, queueing under
  saturation, preemption at shipped defaults, and the cross-class queue bound
- F7 client disconnect while queued frees the waiter (no leak, no orphan)
- Structural guardrail: the await-shape invariants of the defect class
  (cancellation-scoped lane registration, dirty re-pump latch, budget-carrying
  store seam, preempt-stops-renewal) asserted over admission.py's source so a
  revert of any leg fails loudly.
"""

import asyncio
import inspect
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from app.api.routes import chat as chat_module
from app.api.routes.chat import ChatRequest
from app.services import admission as admission_module
from app.services.admission import (
    AdmissionClass,
    AdmissionController,
    AdmissionRejected,
    MemoryAdmissionStore,
)

CHAT = AdmissionClass.CHAT
BACKGROUND = AdmissionClass.BACKGROUND

_SOURCE = Path(inspect.getsourcefile(admission_module)).read_text(encoding="utf-8")


def _default_settings(**overrides):
    base = dict(
        admission_enabled=True,
        admission_chat_budget=8,
        admission_instant_budget=4,
        admission_embedding_budget=4,
        admission_reranking_budget=4,
        admission_vision_budget=2,
        admission_background_budget=2,
        admission_queue_max_size=64,
        admission_store_url="",
        admission_deadline_seconds=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


async def _settle(ticks: int = 12) -> None:
    for _ in range(ticks):
        await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# F2 — the dirty re-pump latch terminates and admits the parked waiter
# ---------------------------------------------------------------------------


async def test_pump_re_latch_settles_without_runaway_pumps():
    """Repeated schedule_pump calls with nothing pannable settle to a quiet
    hub: no pump stays scheduled, no dirty flag remains, no runaway chain."""
    ctrl = AdmissionController(
        budgets={"chat": 1},
        class_budgets={CHAT: "chat"},
        store=MemoryAdmissionStore(),
    )
    for _ in range(50):
        ctrl.hub.schedule_pump()
    await _settle(25)
    assert ctrl.hub.pump_scheduled is False
    assert ctrl.hub.pump_dirty is False


class _ParkAcquireStore(MemoryAdmissionStore):
    """One-shot store whose NEXT ``try_acquire`` captures the live budget
    decision, parks on a gate, and returns the STALE pre-park decision —
    modeling a store round trip suspended while a concurrent release lands
    (the head-only analogue of the frozen C2 dual-shape gate)."""

    def __init__(self) -> None:
        super().__init__()
        self.gate: asyncio.Event | None = None
        self.parked = asyncio.Event()
        self._armed = False

    def arm(self) -> None:
        self.gate = asyncio.Event()
        self.parked = asyncio.Event()
        self._armed = True

    async def try_acquire(self, key, holder, ttl_seconds, budget=None):
        try:
            decision = await super().try_acquire(key, holder, ttl_seconds, budget)
        except TypeError:  # pragma: no cover — head tree always takes 4-arg
            decision = await super().try_acquire(key, holder, ttl_seconds)
        if not self._armed:
            return decision
        self._armed = False  # one-shot: park only the armed call
        self.parked.set()
        assert self.gate is not None
        await self.gate.wait()
        return decision  # stale: the pre-park decision


async def test_release_during_pump_re_pumps_and_admits():
    """A release landing while a pump is SUSPENDED on its store round trip
    must still admit the queued waiter via the dirty re-latch.

    Discriminating by construction (#827 review PRR-004): the pump's armed
    ``try_acquire`` captures the pre-release full-budget decision (False) and
    parks; ``L1.release()`` lands mid-park (its ``schedule_pump`` only marks
    the hub dirty); on gate-open the pump resumes with the stale False and
    breaks — WITHOUT the dirty re-latch nothing re-pumps and the waiter stays
    parked (mutation-probed: reverting the latch fails this test). With the
    latch, the teardown re-pump re-reads the freed budget and admits.
    """
    store = _ParkAcquireStore()
    ctrl = AdmissionController(
        budgets={"chat": 1},
        class_budgets={CHAT: "chat"},
        store=store,
    )
    l1 = await ctrl._acquire(CHAT, None, True)  # noqa: SLF001
    entered = []

    async def waiter():
        async with ctrl.admit(CHAT):
            entered.append(True)

    w = asyncio.create_task(waiter())
    await _settle(25)  # W queued; its pump saw occupancy 1 and broke out
    assert entered == []

    store.arm()  # park the NEXT try_acquire (the fresh pump's round trip)
    ctrl.hub.schedule_pump()
    await store.parked.wait()  # pump suspended with the stale full decision
    await l1.release()  # lands mid-park: schedule_pump can only mark dirty
    store.gate.set()  # pump resumes, breaks on the stale False
    await _settle(50)

    try:
        assert entered == [True]  # dirty re-latch admitted W with no further event
        assert await store.occupancy("chat") == 0
    finally:
        if not w.done():
            w.cancel()
            try:
                await w
            except asyncio.CancelledError:
                pass


# ---------------------------------------------------------------------------
# F3 — MemoryAdmissionStore enforces the budget atomically across controllers
# ---------------------------------------------------------------------------


async def test_memory_store_budget_binds_shared_controllers():
    """Two controllers sharing one MemoryAdmissionStore admit at most
    `budget` holders between them (the in-process half of the atomic seam)."""
    store = MemoryAdmissionStore()
    a = AdmissionController(
        budgets={"chat": 1}, class_budgets={CHAT: "chat"}, store=store
    )
    b = AdmissionController(
        budgets={"chat": 1}, class_budgets={CHAT: "chat"}, store=store
    )
    lease_a = await a._acquire(CHAT, None, True)  # noqa: SLF001
    entered = []

    async def second():
        async with b.admit(CHAT, deadline=0.2):
            entered.append(True)

    task = asyncio.create_task(second())
    await asyncio.sleep(0.4)
    try:
        assert entered == []  # budget 1 is held by controller A's lease
        assert await store.occupancy("chat") == 1
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, AdmissionRejected):
            pass
        await lease_a.release()
    assert await store.occupancy("chat") == 0


# ---------------------------------------------------------------------------
# F4 — from_settings mixed-load semantics on the shared llm key
# ---------------------------------------------------------------------------


async def test_from_settings_llm_key_admits_realizable_sum():
    """Defaults (chat 8, background 2 -> llm budget 8): a workload of six
    chat holders plus two background holders is admitted in full (6+2 = 8)."""
    ctrl = AdmissionController.from_settings(_default_settings())
    leases = []
    try:
        for _ in range(6):
            leases.append(await ctrl._acquire(CHAT, None, True))  # noqa: SLF001
        for _ in range(2):
            leases.append(
                await ctrl._acquire(BACKGROUND, None, True)  # noqa: SLF001
            )
        assert await ctrl.store.occupancy("llm") == 8
    finally:
        for lease in leases:
            await lease.release()


async def test_from_settings_background_queues_under_full_chat():
    """Defaults: with all 8 llm slots held by chat, a background admit queues
    (it is not rejected, not admitted) until chat pressure subsides."""
    ctrl = AdmissionController.from_settings(_default_settings())
    leases = []
    try:
        for _ in range(8):
            leases.append(await ctrl._acquire(CHAT, None, True))  # noqa: SLF001
        entered = []

        async def bg():
            async with ctrl.admit(BACKGROUND, deadline=0.3):
                entered.append(True)

        task = asyncio.create_task(bg())
        await asyncio.sleep(0.15)
        assert entered == []  # queued behind the saturated llm budget
        assert await ctrl.queue_depth(BACKGROUND) == 1
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, AdmissionRejected):
            pass
        await _settle(20)
        assert await ctrl.queue_depth(BACKGROUND) == 0  # waiter unregistered
    finally:
        for lease in leases:
            await lease.release()


async def test_from_settings_queue_bound_counts_cross_class_holders():
    """The queue bound is key occupancy + class depth: with queue_max 4 and
    4 chat holders on the shared llm key, a background arrival is rejected
    immediately (queue_full) — the documented #687 tightening."""
    ctrl = AdmissionController.from_settings(
        _default_settings(admission_queue_max_size=4)
    )
    leases = []
    try:
        for _ in range(4):
            leases.append(await ctrl._acquire(CHAT, None, True))  # noqa: SLF001
        try:
            await asyncio.wait_for(
                ctrl._acquire(BACKGROUND, None, True), timeout=0.5  # noqa: SLF001
            )
            raised = None
        except AdmissionRejected as exc:
            raised = exc
        assert raised is not None and raised.reason == "queue_full"
    finally:
        for lease in leases:
            await lease.release()


# ---------------------------------------------------------------------------
# F7 — client disconnect while queued leaves nothing behind
# ---------------------------------------------------------------------------


async def test_disconnect_while_queued_frees_waiter(monkeypatch):
    """Closing the non-durable stream generator mid-heartbeat-wait (client
    disconnect) unregisters the waiter: no queued depth, no orphan lease, no
    surviving renewer."""
    from unittest.mock import MagicMock

    ctrl = AdmissionController(
        budgets={"chat": 1},
        class_budgets={CHAT: "chat"},
        store=MemoryAdmissionStore(),
    )
    monkeypatch.setattr(
        chat_module, "get_admission_controller", lambda: ctrl
    )
    monkeypatch.setattr(chat_module, "CHAT_HEARTBEAT_INTERVAL", 0.02)

    holder_cm = ctrl.admit(CHAT)
    await holder_cm.__aenter__()  # hold the only chat slot
    await _settle()

    engine = MagicMock()
    engine.llm_client = None
    response = chat_module.stream_chat_response("q", [], engine)
    stream = response.body_iterator

    # Consume at least one queued heartbeat, then disconnect.
    saw_heartbeat = False
    for _ in range(20):
        try:
            chunk = await asyncio.wait_for(stream.__anext__(), 0.1)
        except asyncio.TimeoutError:
            continue
        if chunk.startswith(":"):
            # TST-004 (#827 review): a queued heartbeat is a bare SSE comment
            # — it must never carry an id: line that could advance any
            # client's last-event-id.
            assert "id:" not in chunk
            saw_heartbeat = True
            break
    assert saw_heartbeat, "harness: queued stream never heartbeated"
    await stream.aclose()  # client disconnect mid-queue

    await _settle(25)
    assert await ctrl.queue_depth(CHAT) == 0  # waiter unregistered
    # Only the holder's own lease remains — the queued stream left no
    # orphan lease or renewer behind.
    assert len(ctrl._active_leases) == 1  # noqa: SLF001
    await holder_cm.__aexit__(None, None, None)
    assert await ctrl.store.occupancy("chat") == 0


# ---------------------------------------------------------------------------
# Structural guardrail — the defect-class invariants, asserted over source
# ---------------------------------------------------------------------------


def test_guardrail_admission_await_shapes():
    """The async lifecycle invariants of the #687 defect class, pinned over
    admission.py's source so a revert of any leg fails loudly:

    1. the waiter's lane registration sits INSIDE the cancellation-scoped
       try of ``_acquire`` (registration before the try is the AC1 leak);
    2. the pump latch has the dirty re-pump path (its absence is the AC2
       lost wakeup);
    3. every concrete ``try_acquire`` implementation carries the budget
       parameter (a 3-arg seam is the AC3 TOCTOU);
    4. preemption stops the evicted lease's renewal (its absence is the AC6
       noise) and renews quietly via the revoked early-exit.
    """
    # (1) registration is inside the try: the cancellation-scoped try (the
    # LAST try before the timeout handler — the queue-bound read's own try
    # sits earlier in the method and must not mask this anchor) must precede
    # the append inside _acquire's body.
    acquire_src = _acquire_source(_SOURCE)
    assert acquire_src, "guardrail: could not isolate _acquire source"
    handler_at = acquire_src.index("except asyncio.TimeoutError:")
    try_at = acquire_src.rfind("try:", 0, handler_at)
    append_at = acquire_src.index(".append(waiter)")
    assert try_at != -1 and try_at < append_at < handler_at, (
        "guardrail (#687 T1-28-S-01): the waiter lane append must live "
        "inside the cancellation-scoped try of _acquire"
    )
    assert "except asyncio.CancelledError:" in acquire_src

    # (2) the dirty re-pump latch exists on both sides of the pump.
    assert "self.pump_dirty = True" in _SOURCE, (
        "guardrail (#687 T1-28-S-02): schedule_pump must mark the hub dirty "
        "while a pump runs"
    )
    assert re.search(
        r"finally:\s*\n\s*self\.pump_scheduled = False\s*\n\s*if self\.pump_dirty",
        _SOURCE,
    ), "guardrail (#687 T1-28-S-02): pump teardown must re-schedule on dirty"

    # (3) every concrete try_acquire carries the budget parameter.
    for match in re.finditer(r"async def try_acquire\(([^)]*)\)", _SOURCE):
        params = match.group(1)
        if "self, key: str, holder: str, ttl_seconds: float" in params:
            assert "budget" in params, (
                "guardrail (#687 T1-28-S2-10): concrete try_acquire must "
                f"carry the budget parameter (got: {params})"
            )

    # (4) preemption stops the evicted lease's renewal, and the renew loop
    # exits quietly for deliberately revoked leases.
    preempt_src = _method_source(_SOURCE, "_maybe_preempt")
    assert preempt_src and "_stop_renewal" in preempt_src, (
        "guardrail (#687 T1-28-K-08): _maybe_preempt must stop the evicted "
        "lease's renewal"
    )
    renew_src = _method_source(_SOURCE, "_renew_loop")
    assert renew_src and "if self.revoked:" in renew_src, (
        "guardrail (#687 T1-28-K-08): _renew_loop must exit quietly for "
        "deliberately revoked leases"
    )

    # (5) the shipped Redis script BODIES are bound, not just their ARGV
    # plumbing: the in-test fake re-implements script semantics from
    # substring dispatch, so a regression that deletes enforcement from the
    # Lua itself would otherwise pass every behavioral check (found by the
    # implementation reviewer's mutation probe — deleting the budget block
    # from _ACQUIRE_LUA left C3/C4 green).
    acquire_lua = admission_module.RedisAdmissionStore._ACQUIRE_LUA
    assert "redis.call('TIME')" in acquire_lua, (
        "guardrail (#687 T1-28-S-12): the acquire script must take expiry "
        "authority from the Redis server clock"
    )
    budget_at = acquire_lua.index("live >= budget")
    hsetnx_at = acquire_lua.index("HSETNX")
    assert 0 <= budget_at < hsetnx_at, (
        "guardrail (#687 T1-28-S2-10): the acquire script must enforce the "
        "budget comparison BEFORE the HSETNX"
    )
    refresh_lua = admission_module.RedisAdmissionStore._REFRESH_LUA
    assert "redis.call('TIME')" in refresh_lua and "HEXISTS" in refresh_lua, (
        "guardrail (#687): refresh must stay server-clocked and must not "
        "resurrect a swept holder (HEXISTS gate)"
    )
    for name in ("_OCCUPANCY_LUA", "_SWEEP_LUA"):
        script = getattr(admission_module.RedisAdmissionStore, name)
        assert "redis.call('TIME')" in script, (
            f"guardrail (#687 T1-28-S-12): {name} must take expiry authority "
            "from the Redis server clock"
        )


def _acquire_source(source: str) -> str:
    return _method_source(source, "_acquire")


def _method_source(source: str, name: str) -> str:
    match = re.search(
        rf"(    (?:async )?def {name}\(|    (?:async )?def {name}\b[^:]*:\n)(.*?)(?=\n    (?:async )?def |\n\nclass |\Z)",
        source,
        re.DOTALL,
    )
    return match.group(0) if match else ""


# ---------------------------------------------------------------------------
# #827 feedback pins — F-006 (the HTTPException passthrough is load-bearing)
# and PRR-011 (unforeseen pre-park exceptions still clean up the waiter)
# ---------------------------------------------------------------------------


class _AdmittingController:
    """Admits immediately; stands in for get_admission_controller()."""

    from contextlib import asynccontextmanager as _acm

    @_acm
    async def admit(self, admission_class, **kwargs):
        yield SimpleNamespace(admission_class=admission_class, holder="pin")


async def test_nonstream_httpexception_body_failures_stay_unlogged(monkeypatch, caplog):
    """F-006 (#827 review): the ``except HTTPException: raise`` clause ahead
    of the broad handler is load-bearing for failures raised INSIDE the try
    body — an HTTPException from ``non_stream_chat_response`` (e.g. the
    rag_engine-missing 503) must reach FastAPI without the
    "[chat] UNHANDLED EXCEPTION" traceback. Mutation: deleting the clause
    routes the 503 through ``except Exception`` and this test fails on the
    caplog count."""
    import logging as _logging

    caplog.set_level(_logging.INFO)
    monkeypatch.setattr(
        chat_module, "get_admission_controller", lambda: _AdmittingController()
    )

    async def allow_evaluate(*args, **kwargs):
        return True

    with pytest.raises(HTTPException) as exc_info:
        await chat_module.chat.__wrapped__(
            request=MagicMock(),
            body=ChatRequest(message="hello"),
            rag_engine=None,  # non_stream_chat_response raises 503 (unavailable)
            user={"id": 1, "username": "u", "role": "superadmin"},
            evaluate=allow_evaluate,
            _csrf_token="test-csrf",
            _=True,
        )

    assert exc_info.value.status_code == 503
    unhandled = [
        record
        for record in caplog.records
        if "UNHANDLED EXCEPTION" in record.getMessage()
    ]
    assert len(unhandled) == 0


async def test_unexpected_preempt_exception_still_unregisters_waiter(monkeypatch):
    """PRR-011 (#827 review): an unforeseen exception from the pre-park awaits
    must not orphan the lane-registered waiter — the cancellation-scoped try
    cleans up (unregister + orphan-lease release) and re-raises, so the
    caller sees the real failure and the queue is empty afterwards."""
    store = MemoryAdmissionStore()
    ctrl = AdmissionController(
        budgets={"chat": 1},
        class_budgets={CHAT: "chat"},
        store=store,
    )

    async def explode(self, key):
        raise RuntimeError("injected pre-park failure")

    monkeypatch.setattr(AdmissionController, "_maybe_preempt", explode)

    with pytest.raises(RuntimeError, match="injected pre-park failure"):
        await ctrl._acquire(CHAT, None, True)  # noqa: SLF001

    await _settle(25)
    assert await ctrl.queue_depth(CHAT) == 0  # waiter unregistered
    assert await store.occupancy("chat") == 0  # no orphan lease
