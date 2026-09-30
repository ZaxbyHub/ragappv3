"""Issue #687 acceptance checks (frozen) — admission liveness, atomicity,
clock authority and eviction hygiene.

AC1  test_cancel_during_preempt_await_leaks_no_lease   (T1-28-S-01)
AC2  test_release_during_inflight_pump_is_not_lost     (T1-28-S-02)
AC3  test_cross_replica_budget_is_atomic               (T1-28-S2-10)
AC4  test_clock_skew_does_not_sweep_live_remote_holder (T1-28-S-12)
AC6  test_preempted_lease_stops_renewing_quietly       (T1-28-K-08)

All checks are authored against the UNMODIFIED tree and must fail there for
the defect's own mechanism (see each docstring). No production code is
patched here except the clock authority stub in AC4 (monkeypatched back).
"""

import asyncio
import logging
from typing import Dict, Optional

import pytest

from app.services import admission as admission_module
from app.services.admission import (
    AdmissionClass,
    AdmissionController,
    MemoryAdmissionStore,
    RedisAdmissionStore,
)

CHAT = AdmissionClass.CHAT
BACKGROUND = AdmissionClass.BACKGROUND


async def _settle(ticks: int = 12) -> None:
    """Deterministically let pumps/renewers reach their next suspension."""
    for _ in range(ticks):
        await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# Shared harness: a store whose occupancy() can park mid-read
# ---------------------------------------------------------------------------


class _ParkingOccupancyStore(MemoryAdmissionStore):
    """The NEXT armed store round trip parks on a gate, whichever method it
    is (``occupancy`` or ``try_acquire``), returning the STALE pre-park
    result once the gate opens — reproducing a slow store whose read was in
    flight while other work (release / cancellation) landed.

    Dual-shape (#687 amend, critic round 2): the base tree's pump performs
    its budget decision through the ``occupancy`` pre-check read, the fixed
    tree through ``try_acquire`` — the one-shot gate must park the FIRST
    armed round trip either way or the check hangs on one tree or the other.
    The ``try_acquire`` capture is side-effect-free in the parked (blocked)
    state: the budget is full at capture time, so the captured decision is
    False and nothing is registered. Arming is one-shot: only the designated
    call parks; every later call reads live state.
    """

    def __init__(self) -> None:
        super().__init__()
        self._calls_after_arm = 0
        self._park_at_call: Optional[int] = None
        self.gate: Optional[asyncio.Event] = None
        self.parked = asyncio.Event()
        self.captured: Optional[int] = None

    def arm(self, nth: int) -> None:
        """Park on the ``nth`` store call (of either method) from this arming."""
        self._calls_after_arm = 0
        self._park_at_call = nth
        self.gate = asyncio.Event()
        self.parked = asyncio.Event()
        self.captured = None

    def _should_park(self) -> bool:
        if self._park_at_call is None:
            return False
        self._calls_after_arm += 1
        return self._calls_after_arm == self._park_at_call

    def _disarm(self) -> None:
        self._park_at_call = None

    async def occupancy(self, key: str) -> int:
        if not self._should_park():
            return await super().occupancy(key)
        # One-shot: disarm BEFORE parking so subsequent calls (later pump
        # reads, the final assertion) observe live state.
        self._disarm()
        count = await super().occupancy(key)  # capture the pre-park count
        self.captured = count
        self.parked.set()
        assert self.gate is not None
        await self.gate.wait()
        return count  # stale: what the caller's read saw before the park

    async def try_acquire(self, key, holder, ttl_seconds, budget=None):
        if not self._should_park():
            return await self._plain_acquire(key, holder, ttl_seconds, budget)
        self._disarm()
        try:
            decision = await self._plain_acquire(key, holder, ttl_seconds, budget)
        except TypeError:
            # Pre-#687 seam (3-arg inner store) — the base leg's shape.
            decision = await super().try_acquire(key, holder, ttl_seconds)
        self.captured = -1 if decision else -2  # marker: parked on acquire
        self.parked.set()
        assert self.gate is not None
        await self.gate.wait()
        return decision  # stale pre-park decision

    async def _plain_acquire(self, key, holder, ttl_seconds, budget):
        try:
            return await super().try_acquire(key, holder, ttl_seconds, budget)
        except TypeError:
            return await super().try_acquire(key, holder, ttl_seconds)


def _chat_controller(store, **kwargs) -> AdmissionController:
    kwargs.setdefault("budgets", {"chat": 1})
    kwargs.setdefault("class_budgets", {CHAT: "chat"})
    kwargs.setdefault("store", store)
    return AdmissionController(**kwargs)


# ---------------------------------------------------------------------------
# AC1 — cancellation during the _maybe_preempt store await leaks a lease
# ---------------------------------------------------------------------------


async def test_cancel_during_preempt_await_leaks_no_lease():
    """AC1 (T1-28-S-01): cancelling a queued admit while its foreground
    preemption step is still awaiting the store must remove the waiter so no
    lease is ever created for it and the budget slot returns to free.

    The chat budget is 1 and held by lease L1. The parking store parks on
    the admit's SECOND occupancy call — the ``_maybe_preempt`` read at
    admission.py L738 (the first is the queue-bound read at L599). The
    waiter is appended to the lane at L621 BEFORE that await and OUTSIDE the
    ``try`` whose ``except CancelledError`` (L642-645) unregisters it, so the
    cancellation leaves a live orphan waiter. The pump triggered by L1's
    release then admits the orphan into a renewing lease nobody releases.
    """
    store = _ParkingOccupancyStore()
    ctrl = _chat_controller(store)

    l1_cm = ctrl.admit(CHAT)
    await l1_cm.__aenter__()  # L1 holds the only chat slot
    await _settle()  # L1's acquire pump finished; occupancy calls are quiet

    # Arm right before the second admit; count only calls after arming (the
    # design note: L1's own path consumed occupancy calls too).
    store.arm(2)

    async def queued_chat():
        async with ctrl.admit(CHAT):
            await asyncio.sleep(3600)  # pragma: no cover - cancelled first

    task = asyncio.create_task(queued_chat())
    await store.parked.wait()  # it is suspended in _maybe_preempt's read
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    store.gate.set()  # open the gate (the parked read died with the task)
    await l1_cm.__aexit__(None, None, None)  # release L1 -> schedule_pump
    await asyncio.sleep(0.25)  # let the pump run and admit any orphan

    assert await store.occupancy("chat") == 0


# ---------------------------------------------------------------------------
# AC2 — a release landing while a pump awaits the store is lost
# ---------------------------------------------------------------------------


async def test_release_during_inflight_pump_is_not_lost():
    """AC2 (T1-28-S-02): a lease released while a pump is already running
    and awaiting the store must still admit the next queued waiter without
    any further event.

    Budget 1 held by L1; waiter W queued with no deadline parks on its
    future. The store is armed to park the NEXT store round trip (the
    dual-shape gate: the occupancy pre-check read on the base tree, the
    try_acquire call on the fixed tree) with stale-decision semantics; the
    triggered pump's budget round trip parks. ``await L1.release()`` lands
    during the park — ``schedule_pump`` is a no-op while ``pump_scheduled``
    is set (base L299-302) — then the gate opens and the pump resumes with
    the STALE pre-release decision, breaks out, and (without the fix) no new
    pump is scheduled. W stays parked forever.
    """
    store = _ParkingOccupancyStore()
    ctrl = _chat_controller(store, default_deadline=None)

    l1_cm = ctrl.admit(CHAT)
    await l1_cm.__aenter__()
    await _settle()

    entered = []

    async def waiter_w():
        async with ctrl.admit(CHAT):
            entered.append(True)

    w_task = asyncio.create_task(waiter_w())
    await _settle(25)  # W queued and parked on its future
    assert entered == []
    assert await ctrl.queue_depth(CHAT) == 1

    store.arm(1)  # park the next occupancy call, returning the stale count
    ctrl.hub.schedule_pump()  # the pump's _admit_waiter read parks
    await store.parked.wait()

    await l1_cm.__aexit__(None, None, None)  # release during the parked pump
    store.gate.set()  # pump resumes having "seen" the pre-release count 1
    await asyncio.sleep(1.0)

    try:
        assert w_task.done() is True
    finally:
        if not w_task.done():
            w_task.cancel()
            try:
                await w_task
            except asyncio.CancelledError:
                pass


# ---------------------------------------------------------------------------
# Shared in-test async Redis double (no fakeredis in the repo)
# ---------------------------------------------------------------------------


class _Rendezvous:
    """2-party meet with a timeout fallback (a lone caller proceeds)."""

    def __init__(self, timeout: float = 1.0) -> None:
        self._timeout = timeout
        self._arrived = 0
        self._release = asyncio.Event()

    async def meet(self) -> None:
        self._arrived += 1
        if self._arrived >= 2:
            self._arrived = 0
            ev, self._release = self._release, asyncio.Event()
            ev.set()
            return
        try:
            await asyncio.wait_for(self._release.wait(), self._timeout)
        except asyncio.TimeoutError:
            pass  # fallback: proceed alone after the timeout


class _FakeRedis:
    """Async Redis double over one dict implementing exactly the command
    surface RedisAdmissionStore touches: hsetnx/hgetall/hdel/hlen/eval.

    ``eval`` translates the #687 TIME-based Lua scripts (acquire / occupancy
    / sweep / refresh) against a controllable SERVER clock (``_server_now``)
    — expiry authority is the store, never the caller's host clock. A script
    that is not TIME-based (e.g. a regression to host-clock epochs threaded
    through ARGV) is rejected outright so such a wrong fix fails this check.
    """

    def __init__(self, rendezvous: Optional[_Rendezvous] = None) -> None:
        self._hashes: Dict[str, Dict[str, str]] = {}
        self._rendezvous = rendezvous
        self._server_now = 1_000_000.0

    async def hsetnx(self, field: str, holder: str, value) -> int:
        entry = self._hashes.setdefault(field, {})
        if holder in entry:
            return 0
        entry[holder] = value
        return 1

    async def hgetall(self, field: str) -> Dict[str, str]:
        return dict(self._hashes.get(field, {}))

    async def hdel(self, field: str, *holders: str) -> int:
        entry = self._hashes.get(field)
        if not entry:
            return 0
        removed = 0
        for holder in holders:
            if holder in entry:
                del entry[holder]
                removed += 1
        if not entry:
            self._hashes.pop(field, None)
        return removed

    async def hlen(self, field: str) -> int:
        # Capture-then-meet: both replicas complete their occupancy read
        # (and see this count) before either proceeds to acquire.
        count = len(self._hashes.get(field, {}))
        if self._rendezvous is not None:
            await self._rendezvous.meet()
        return count

    def _sweep(self, field: str):
        """Drop entries expired by the SERVER clock; return (live, removed)."""
        entry = self._hashes.get(field)
        if not entry:
            return 0, 0
        stale = [h for h, exp in entry.items() if float(exp) <= self._server_now]
        for holder in stale:
            del entry[holder]
        if not entry:
            self._hashes.pop(field, None)
        return len(entry), len(stale)

    async def eval(self, script: str, numkeys: int, *args) -> int:
        if "TIME" not in script:
            raise AssertionError(
                "eval script is not TIME-based: expiry authority must be the "
                "Redis server clock, not a caller-supplied epoch (#687)"
            )
        field = args[0]
        if "HSETNX" in script:
            # _ACQUIRE_LUA: ARGV = budget, holder, ttl — sweep + budget
            # check + acquire in one atomic translation. The rendezvous is
            # mirrored here so both replicas' scripts overlap exactly as
            # their legacy hlen reads did (critic round 1: keep it on BOTH
            # paths).
            if self._rendezvous is not None:
                await self._rendezvous.meet()
            budget, holder, ttl = int(args[1]), args[2], float(args[3])
            live, _ = self._sweep(field)
            if budget > 0 and live >= budget:
                return 0
            entry = self._hashes.setdefault(field, {})
            if holder in entry:
                return 0
            entry[holder] = repr(self._server_now + ttl)
            return 1
        if "HEXISTS" in script:
            # _REFRESH_LUA: ARGV = holder, ttl.
            holder, ttl = args[1], float(args[2])
            entry = self._hashes.get(field, {})
            if holder in entry:
                entry[holder] = repr(self._server_now + ttl)
                return 1
            return 0
        live, removed = self._sweep(field)
        if "return removed" in script:
            # _SWEEP_LUA.
            return removed
        # _OCCUPANCY_LUA — return the live count.
        return live


def _fake_backed_store(fake: _FakeRedis) -> RedisAdmissionStore:
    """Build a real RedisAdmissionStore (the constructor does NOT connect)
    and swap its client for the shared in-test fake."""
    store = RedisAdmissionStore("redis://localhost:6379/0")
    store._redis = fake
    return store


# ---------------------------------------------------------------------------
# AC3 — the budget check and the acquire are separate round trips
# ---------------------------------------------------------------------------


async def test_cross_replica_budget_is_atomic():
    """AC3 (T1-28-S2-10): two replicas with separate controllers sharing one
    Redis store admitting CHAT concurrently against a budget of 1 must let
    at most one hold the slot.

    Each ``hlen`` (the occupancy read's terminal command) waits at a 2-party
    rendezvous, so both replicas read occupancy 0 before either acquires.
    ``_admit_waiter`` reads occupancy (L677) and compares to budget (L687) in
    one await, then awaits ``store.try_acquire`` (L691) in a second; the
    Redis acquire is sweep + HSETNX with no budget parameter (L203-208) —
    so both replicas acquire and both hold the slot.
    """
    rendezvous = _Rendezvous(timeout=1.0)
    fake = _FakeRedis(rendezvous=rendezvous)

    ctrl_a = AdmissionController(
        budgets={"chat": 1},
        class_budgets={CHAT: "chat"},
        store=_fake_backed_store(fake),
        instance_id="replica-a",
    )
    ctrl_b = AdmissionController(
        budgets={"chat": 1},
        class_budgets={CHAT: "chat"},
        store=_fake_backed_store(fake),
        instance_id="replica-b",
    )

    holding = []

    async def admit_and_hold(ctrl, tag):
        async with ctrl.admit(CHAT):
            holding.append(tag)
            await asyncio.sleep(0.4)

    task_a = asyncio.create_task(admit_and_hold(ctrl_a, "a"))
    task_b = asyncio.create_task(admit_and_hold(ctrl_b, "b"))
    try:
        await asyncio.sleep(0.2)  # both replicas have run admit to the end
        held = len(fake._hashes.get("admission:chat", {}))
        assert held == 1  # base: both replicas acquire -> assert 2 == 1
        # Amended harness assert (#687, critic round 1): the buggy outcome
        # admitted BOTH replicas; the fixed atomic acquire admits at most
        # one, so the observed holding set is bounded by one entry.
        assert len(holding) <= 1
    finally:
        for task in (task_a, task_b):
            if not task.done():
                task.cancel()
        await asyncio.gather(task_a, task_b, return_exceptions=True)


# ---------------------------------------------------------------------------
# AC4 — holder expiry epochs are host wall-clock values
# ---------------------------------------------------------------------------


class _FixedClock:
    """Clock stub standing in for admission.py's ``time`` module binding."""

    def __init__(self, now: float) -> None:
        self.now = now

    def time(self) -> float:
        return self.now

    def monotonic(self) -> float:
        return self.now


async def test_clock_skew_does_not_sweep_live_remote_holder(monkeypatch):
    """AC4 (T1-28-S-12): a replica whose wall clock runs more than the lease
    TTL ahead of the acquiring replica must not sweep that live holder.

    Replica A acquires with ``admission.time.time`` pinned to T. Replica B
    calls ``occupancy("chat")`` with the clock pinned to T + ttl + 5. Holder
    expiry epochs come from each caller's ``time.time()`` (L204/L226) and
    ``occupancy`` sweeps with the local clock first (L213-215), so B deletes
    A's still-live field and reports 0.
    """
    fake = _FakeRedis()
    store_a = _fake_backed_store(fake)  # replica A's store handle
    store_b = _fake_backed_store(fake)  # replica B's store handle (shared)

    base_time = 1_000_000.0
    ttl = 30.0
    clock = _FixedClock(base_time)
    # Rebind the module's ``time`` reference (admission.py calls
    # ``time.time()`` at call time), so BOTH stores see the patched clock.
    monkeypatch.setattr(admission_module, "time", clock)

    # Dual-shape call (#687 amend): the fixed seam carries the budget; the
    # base seam is 3-arg. Both legs must exercise their own tree's real
    # acquire path (no fail-open masking).
    try:
        acquired = await store_a.try_acquire("chat", "replica-a-holder", ttl, 1)
    except TypeError:
        acquired = await store_a.try_acquire("chat", "replica-a-holder", ttl)
    assert acquired is True

    clock.now = base_time + ttl + 5.0  # replica B's clock runs fast
    # On the fixed tree the fast host clock is inert: expiry authority is
    # the fake's SERVER clock (never advanced), so the holder stays live and
    # occupancy reports 1. On the base tree the monkeypatched host clock
    # sweeps the holder and occupancy reports 0 -> assert 0 == 1.
    occupancy = await store_b.occupancy("chat")

    assert occupancy == 1


# ---------------------------------------------------------------------------
# AC6 — preemption revokes the lease but leaves its renewal running
# ---------------------------------------------------------------------------


async def test_preempted_lease_stops_renewing_quietly(caplog):
    """AC6 (T1-28-K-08): a preempted background lease must stop renewing and
    must NOT log "lost its slot mid-flight".

    The controller has ttl_seconds=0.3 and a SINGLE shared budget key
    (CHAT and BACKGROUND both map to "dev") so foreground preemption CAN
    fire today. A BACKGROUND lease is preempted by a blocked CHAT admit;
    preemption sets ``revoked`` and calls ``_forget_lease`` (L749-750) but
    never stops the renewer — the next ``refresh`` miss makes ``_renew_loop``
    log the "lost its slot" warning (L399-404) blaming the store for a
    deliberate eviction.
    """
    caplog.set_level(logging.WARNING)
    store = MemoryAdmissionStore()
    ctrl = AdmissionController(
        budgets={"dev": 1},
        class_budgets={CHAT: "dev", BACKGROUND: "dev"},
        store=store,
        ttl_seconds=0.3,
    )

    box = {}

    async def bg_work():
        async with ctrl.admit(BACKGROUND) as lease:
            box["lease"] = lease
            await asyncio.Event().wait()  # live task, never cancelled

    bg_task = asyncio.create_task(bg_work())
    for _ in range(100):  # let the background lease acquire
        await asyncio.sleep(0)
        if "lease" in box:
            break
    assert "lease" in box, "harness: background lease never acquired"
    bg_lease = box["lease"]

    async def chat_turn():
        async with ctrl.admit(CHAT):  # foreground; blocked -> preempts bg
            await asyncio.Event().wait()

    chat_task = asyncio.create_task(chat_turn())
    for _ in range(500):  # wait until preemption actually fired
        if bg_lease.revoked:
            break
        await asyncio.sleep(0)
    assert bg_lease.revoked is True, "harness: preemption did not fire"

    # Renewal interval is max(ttl/3, 0.05) = 0.1s; wait well past 2x it so a
    # still-running renewer has ticked and missed its refresh.
    await asyncio.sleep(0.5)

    lost = [
        record
        for record in caplog.records
        if record.levelno >= logging.WARNING
        and "lost its slot" in record.getMessage()
    ]
    try:
        assert len(lost) == 0
    finally:
        for task in (chat_task, bg_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(chat_task, bg_task, return_exceptions=True)
