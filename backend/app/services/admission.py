"""Shared inference admission control (issue #518, Workstream E3).

Per-endpoint/per-device admission budgets shared by chat, Instant, embedding,
reranking, vision and background workers — across processes/replicas when a
shared ``AdmissionStore`` (e.g. Redis) is configured, process-local otherwise.

Guarantees implemented here:
* one shared budget per device class — controllers sharing a store share the
  budget AND one waiter hub, so a slot released by one coordinator wakes the
  next queued waiter on any coordinator (UPLOAD-DEEP-02/08: no more
  independent per-caller semaphores);
* bounded queues with immediate ``queue_full`` rejection (no unbounded retry
  amplification against the shared store);
* foreground preference over background work, WITHOUT starving background —
  when a foreground request is blocked and only background holders occupy the
  budget, one local background holder is *logically evicted* (its task keeps
  running; its later release is a no-op). Work is never cancelled;
* deadline propagation — a queued request whose deadline expires is rejected,
  never executed;
* cancellation — a disconnected waiter frees its queue slot (and any slot
  acquired on its behalf after cancellation is released back);
* dead-holder recovery — holders past their TTL are swept on acquire. Live
  holders renew (heartbeat every ttl/3) so a long generation never loses its
  slot; the TTL only reaps holders whose process died without releasing;
* graceful shutdown — in-flight slots are released and new admits rejected;
* fail-open degradation — an unreachable shared store never blocks requests
  (``controller.degraded`` reports the condition).

``admission_enabled=False`` (or ``AdmissionController(enabled=False)``) is a
zero-store-call pass-through: current behavior is completely preserved.

Boundary (documented for operators): foreground preemption classifies holders
by the controller's own lease registry, so it evicts only holders admitted by
THIS process. Remote background holders are recovered by TTL sweeps instead.
With the in-memory store the waiter hub is process-local by construction; a
Redis store shares the *budget counters* across processes while queued-wakeup
remains per-process (documented scale-out behavior; see docs/operations.md).
"""

import asyncio
import logging
import os
import secrets
import socket
import time
import weakref
from abc import abstractmethod
from collections import deque
from contextlib import asynccontextmanager
from enum import Enum
from typing import Any, Callable, Deque, Dict, Optional, Set

logger = logging.getLogger(__name__)


class AdmissionClass(str, Enum):
    """Inference device/endpoint classes sharing admission budgets."""

    CHAT = "chat"
    INSTANT = "instant"
    EMBEDDING = "embedding"
    RERANKING = "reranking"
    VISION = "vision"
    BACKGROUND = "background"


class AdmissionRejected(Exception):
    """Admission was refused. ``reason`` is one of the closed set below."""

    REASONS = ("queue_full", "deadline_exceeded", "shutdown")

    def __init__(self, reason: str, message: Optional[str] = None):
        if reason not in self.REASONS:
            raise ValueError(f"unknown AdmissionRejected reason: {reason!r}")
        self.reason = reason
        super().__init__(message or f"admission rejected: {reason}")


class AdmissionStore:
    """Async store seam shared by coordinators (the cross-process boundary).

    ``try_acquire`` registers ``holder`` against ``key`` for ``ttl_seconds``
    and returns True (False when the holder is already registered);
    ``occupancy`` counts live holders (stale ones are swept); ``release``
    removes a holder; ``sweep_expired`` drops holders past their TTL and
    returns how many were removed.
    """

    @abstractmethod
    async def try_acquire(self, key: str, holder: str, ttl_seconds: float) -> bool:
        ...

    @abstractmethod
    async def release(self, key: str, holder: str) -> None:
        ...

    @abstractmethod
    async def occupancy(self, key: str) -> int:
        ...

    @abstractmethod
    async def sweep_expired(self, key: str) -> int:
        ...

    async def refresh(self, key: str, holder: str, ttl_seconds: float) -> bool:
        """Extend a live holder's TTL. Returns False when the holder is no
        longer registered (already swept/released) — the caller must then
        treat its slot as lost.

        Concrete default for stores without per-holder expiry bookkeeping
        (e.g. test doubles): report the holder as persistent. Stores that
        track expiry epochs override this (both shipped stores do).
        """
        return True


class MemoryAdmissionStore(AdmissionStore):
    """In-process reference store. Each key maps holder -> expiry epoch."""

    def __init__(self, clock: Optional[Callable[[], float]] = None):
        self._clock = clock or time.monotonic
        self._holders: Dict[str, Dict[str, float]] = {}

    def _live(self, key: str, now: float) -> Dict[str, float]:
        entry = self._holders.get(key)
        if entry is None:
            return {}
        stale = [h for h, exp in entry.items() if exp <= now]
        for holder in stale:
            del entry[holder]
        if not entry:
            self._holders.pop(key, None)
        return entry

    async def try_acquire(self, key: str, holder: str, ttl_seconds: float) -> bool:
        now = self._clock()
        entry = self._live(key, now)
        if holder in entry:
            return False
        entry[holder] = now + max(ttl_seconds, 0.0)
        self._holders[key] = entry
        return True

    async def release(self, key: str, holder: str) -> None:
        entry = self._holders.get(key)
        if entry is not None:
            entry.pop(holder, None)
            if not entry:
                self._holders.pop(key, None)

    async def occupancy(self, key: str) -> int:
        return len(self._live(key, self._clock()))

    async def sweep_expired(self, key: str) -> int:
        now = self._clock()
        entry = self._holders.get(key)
        if not entry:
            return 0
        stale = [h for h, exp in entry.items() if exp <= now]
        for holder in stale:
            del entry[holder]
        if not entry:
            self._holders.pop(key, None)
        return len(stale)

    async def refresh(self, key: str, holder: str, ttl_seconds: float) -> bool:
        now = self._clock()
        entry = self._live(key, now)
        if holder not in entry:
            return False
        entry[holder] = now + max(ttl_seconds, 0.0)
        return True


class RedisAdmissionStore(AdmissionStore):
    """Cross-process store backed by Redis hashes (one hash per budget key).

    Holder field values are absolute expiry epochs (synchronized-clock best
    effort). Operations are single-key atomic commands.
    """

    # KEYS[1] = admission hash field key, ARGV[1] = holder, ARGV[2] = new
    # absolute expiry epoch. Extends the expiry ONLY if the holder is still
    # registered, so a swept/dead holder can never be resurrected by a late
    # renewal.
    _REFRESH_LUA = """
if redis.call('HEXISTS', KEYS[1], ARGV[1]) == 1 then
  redis.call('HSET', KEYS[1], ARGV[1], ARGV[2])
  return 1
end
return 0
"""

    def __init__(self, url: str):
        import redis.asyncio as aioredis  # optional at call time

        self._redis = aioredis.from_url(url, decode_responses=True)

    @staticmethod
    def _field(key: str) -> str:
        return f"admission:{key}"

    async def try_acquire(self, key: str, holder: str, ttl_seconds: float) -> bool:
        now = time.time()
        field = self._field(key)
        await self._sweep(field, now)
        added = await self._redis.hsetnx(field, holder, now + max(ttl_seconds, 0.0))
        return bool(added)

    async def release(self, key: str, holder: str) -> None:
        await self._redis.hdel(self._field(key), holder)

    async def occupancy(self, key: str) -> int:
        await self._sweep(self._field(key), time.time())
        return int(await self._redis.hlen(self._field(key)))

    async def sweep_expired(self, key: str) -> int:
        return await self._sweep(self._field(key), time.time())

    async def refresh(self, key: str, holder: str, ttl_seconds: float) -> bool:
        # Atomic check-and-set: only a holder still registered gets its
        # expiry extended — a swept holder is never resurrected.
        return bool(
            await self._redis.eval(
                self._REFRESH_LUA, 1, self._field(key), holder,
                time.time() + max(ttl_seconds, 0.0),
            )
        )

    async def _sweep(self, field: str, now: float) -> int:
        entry = await self._redis.hgetall(field)
        stale = [h for h, exp in entry.items() if float(exp) <= now]
        if stale:
            await self._redis.hdel(field, *stale)
        return len(stale)

    async def close(self) -> None:
        await self._redis.aclose()


class _Waiter:
    __slots__ = (
        "controller",
        "future",
        "deadline",
        "holder",
        "foreground",
        "seq",
        "gone",
        "task",
    )

    def __init__(
        self,
        controller: "AdmissionController",
        future: "asyncio.Future",
        deadline: Optional[float],
        holder: str,
        foreground: bool,
        seq: int,
        task: Optional["asyncio.Task"],
    ):
        self.controller = controller
        self.future = future
        self.deadline = deadline
        self.holder = holder
        self.foreground = foreground
        self.seq = seq
        self.gone = False
        self.task = task


class _Hub:
    """Shared waiter lanes + pump for every controller using one store.

    A slot released by ANY controller pumps the hub, so queued waiters on all
    coordinators sharing the store get their wakeup (in-process; see the
    module docstring for the Redis scale-out boundary).
    """

    def __init__(self) -> None:
        self.fg: Dict[AdmissionClass, Deque[_Waiter]] = {
            cls: deque() for cls in AdmissionClass
        }
        self.bg: Dict[AdmissionClass, Deque[_Waiter]] = {
            cls: deque() for cls in AdmissionClass
        }
        self.lock: Optional[asyncio.Lock] = None
        self.pump_scheduled = False

    def lane(self, admission_class: AdmissionClass, foreground: bool):
        return (
            self.fg[admission_class] if foreground else self.bg[admission_class]
        )

    def depth(self, admission_class: AdmissionClass) -> int:
        return len(self.fg[admission_class]) + len(self.bg[admission_class])

    def schedule_pump(self) -> None:
        if self.pump_scheduled:
            return
        self.pump_scheduled = True
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            self.pump_scheduled = False
            return
        loop.create_task(self.pump())

    async def pump(self) -> None:
        try:
            if self.lock is None:
                self.lock = asyncio.Lock()
            async with self.lock:
                for admission_class in AdmissionClass:
                    for lane in (self.fg[admission_class], self.bg[admission_class]):
                        while lane:
                            waiter = lane[0]
                            if waiter.gone or waiter.future.done():
                                lane.popleft()
                                continue
                            admitted = await waiter.controller._admit_waiter(
                                admission_class, lane, waiter
                            )
                            if not admitted:
                                break
        finally:
            self.pump_scheduled = False


_HUBS: "weakref.WeakKeyDictionary[AdmissionStore, _Hub]" = weakref.WeakKeyDictionary()


def _hub_for(store: AdmissionStore) -> _Hub:
    hub = _HUBS.get(store)
    if hub is None:
        hub = _Hub()
        _HUBS[store] = hub
    return hub


class _Lease:
    """An admitted slot. ``release()`` is idempotent."""

    def __init__(
        self,
        controller: "AdmissionController",
        admission_class: AdmissionClass,
        holder: str,
        key: str,
        *,
        store_bound: bool,
        reentrant: bool = False,
    ):
        self._controller = controller
        self.admission_class = admission_class
        self.holder = holder
        self.key = key
        self._store_bound = store_bound
        self._reentrant = reentrant
        self.task: Optional["asyncio.Task"] = None
        self.revoked = False
        self._released = False
        self._renewer: Optional["asyncio.Task"] = None

    def _start_renewal(self) -> None:
        """Heartbeat the store TTL so a live holder outlives ttl_seconds.

        Without renewal, any generation longer than the (crash-recovery) TTL
        silently loses its budget slot and the device oversubscribes
        (swarm review F-003). Renewal runs every ttl/3 while the lease is
        held and stops on release/shutdown.
        """
        if not self._store_bound:
            return
        interval = max(self._controller.ttl_seconds / 3.0, 0.05)
        self._renewer = asyncio.get_running_loop().create_task(
            self._renew_loop(interval)
        )

    async def _renew_loop(self, interval: float) -> None:
        controller = self._controller
        while True:
            await asyncio.sleep(interval)
            try:
                alive = await controller.store.refresh(
                    self.key, self.holder, controller.ttl_seconds
                )
            except Exception:  # noqa: BLE001 — store outage: keep trying
                controller._mark_degraded()
                continue
            if not alive:
                # Slot was swept while we were still running (store-side
                # race). Do not resurrect it: mark revoked so release()
                # skips the store release, and surface the anomaly.
                controller._mark_degraded()
                self.revoked = True
                logger.warning(
                    "admission lease %s/%s lost its slot mid-flight "
                    "(refresh miss); device may be oversubscribed",
                    self.key,
                    self.holder,
                )
                return

    async def _stop_renewal(self) -> None:
        if self._renewer is not None:
            self._renewer.cancel()
            try:
                await self._renewer
            except asyncio.CancelledError:
                pass
            self._renewer = None

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        await self._stop_renewal()
        if self._reentrant:
            return
        controller = self._controller
        controller._forget_lease(self)
        if self._store_bound and not self.revoked:
            try:
                await controller.store.release(self.key, self.holder)
            except Exception:  # noqa: BLE001 — store outage must not leak slots
                controller._mark_degraded()
        controller.hub.schedule_pump()


def _default_instance_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}-{secrets.token_hex(4)}"


class AdmissionController:
    """Coordinates admission against per-device budgets over a shared store.

    ``budgets`` maps budget-key -> limit; ``class_budgets`` maps each
    AdmissionClass to its budget key (several classes may share one key —
    e.g. one hardware-wide inference budget).
    """

    def __init__(
        self,
        budgets: Dict[str, int],
        class_budgets: Dict[AdmissionClass, str],
        queue_max_size: int = 64,
        store: Optional[AdmissionStore] = None,
        enabled: bool = True,
        ttl_seconds: float = 30.0,
        default_deadline: Optional[float] = None,
        instance_id: Optional[str] = None,
    ):
        self.budgets = dict(budgets)
        self.class_budgets = dict(class_budgets)
        self.queue_max_size = queue_max_size
        self.store: AdmissionStore = store or MemoryAdmissionStore()
        self.enabled = enabled
        self.ttl_seconds = ttl_seconds
        self.default_deadline = default_deadline
        self.instance_id = instance_id or _default_instance_id()
        self.hub = _hub_for(self.store)
        self._seq = 0
        self._shutdown = False
        self._degraded = False
        self._local_actives: Set[tuple] = set()
        self._active_leases: Dict[tuple, _Lease] = {}

    # ------------------------------------------------------------------ API

    @property
    def degraded(self) -> bool:
        return self._degraded

    def _mark_degraded(self) -> None:
        """Flip the degraded flag, logging the transition once (the flag
        previously existed but nothing ever read/alerted on it — swarm
        review LOW finding)."""
        if not self._degraded:
            logger.warning(
                "admission store degraded: failing open (see docs/operations.md)"
            )
        self._degraded = True

    def budget_for(self, admission_class: AdmissionClass) -> int:
        key = self.class_budgets[admission_class]
        return int(self.budgets.get(key, 1))

    async def queue_depth(self, admission_class: AdmissionClass) -> int:
        return self.hub.depth(admission_class)

    async def shutdown(self) -> None:
        """Release in-flight slots and reject all future admissions."""
        self._shutdown = True
        # Go through the lease objects (not raw store.release) so renewal
        # heartbeats are cancelled too — otherwise a live renewer would
        # resurrect the slot this shutdown just released.
        for lease in list(self._active_leases.values()):
            try:
                await lease.release()
            except Exception:  # noqa: BLE001 — best-effort release on shutdown
                self._mark_degraded()
        self._local_actives.clear()
        self._reject_own_waiters("shutdown")
        self.hub.schedule_pump()

    @asynccontextmanager
    async def admit(
        self,
        admission_class: AdmissionClass,
        *,
        deadline: Optional[float] = None,
        foreground: bool = True,
    ):
        if not self.enabled:
            yield _Lease(
                self, admission_class, "", "", store_bound=False, reentrant=True
            )
            return
        if self._shutdown:
            raise AdmissionRejected("shutdown")
        lease = await self._acquire(admission_class, deadline, foreground)
        try:
            yield lease
        finally:
            await lease.release()

    @classmethod
    def from_settings(cls, settings: Any) -> "AdmissionController":
        budgets = {
            "chat": max(1, int(getattr(settings, "admission_chat_budget", 8))),
            "instant": max(1, int(getattr(settings, "admission_instant_budget", 4))),
            "embedding": max(
                1, int(getattr(settings, "admission_embedding_budget", 4))
            ),
            "reranking": max(
                1, int(getattr(settings, "admission_reranking_budget", 4))
            ),
            "vision": max(1, int(getattr(settings, "admission_vision_budget", 2))),
            "background": max(
                1, int(getattr(settings, "admission_background_budget", 2))
            ),
        }
        class_budgets = {
            AdmissionClass.CHAT: "chat",
            AdmissionClass.INSTANT: "instant",
            AdmissionClass.EMBEDDING: "embedding",
            AdmissionClass.RERANKING: "reranking",
            AdmissionClass.VISION: "vision",
            AdmissionClass.BACKGROUND: "background",
        }
        store_url = str(getattr(settings, "admission_store_url", "") or "")
        store: Optional[AdmissionStore] = None
        if store_url:
            store = RedisAdmissionStore(store_url)
        deadline = getattr(settings, "admission_deadline_seconds", None)
        return cls(
            budgets=budgets,
            class_budgets=class_budgets,
            queue_max_size=max(
                1, int(getattr(settings, "admission_queue_max_size", 64))
            ),
            store=store,
            enabled=bool(getattr(settings, "admission_enabled", True)),
            default_deadline=(float(deadline) if deadline is not None else None),
        )

    # ------------------------------------------------------------- internals

    async def _acquire(
        self,
        admission_class: AdmissionClass,
        deadline: Optional[float],
        foreground: bool,
    ) -> _Lease:
        key = self.class_budgets[admission_class]
        holder = f"{self.instance_id}:{admission_class.value}:{secrets.token_hex(6)}"
        effective_deadline = (
            deadline if deadline is not None else self.default_deadline
        )

        # Queue bound: in-flight holders (shared-store occupancy for this
        # budget key) + queued waiters for the class. At or over the bound a
        # new request is rejected IMMEDIATELY — bounded overload response,
        # never an unbounded queue or retry storm against the store.
        try:
            key_occupancy = await self.store.occupancy(key)
        except Exception:  # noqa: BLE001 — degraded store: fail open
            self._mark_degraded()
            key_occupancy = -1
        if (
            key_occupancy >= 0
            and key_occupancy + self.hub.depth(admission_class)
            >= self.queue_max_size
        ):
            raise AdmissionRejected("queue_full")

        self._seq += 1
        future: "asyncio.Future" = asyncio.get_running_loop().create_future()
        waiter = _Waiter(
            self,
            future,
            effective_deadline,
            holder,
            foreground,
            self._seq,
            asyncio.current_task(),
        )
        self.hub.lane(admission_class, foreground).append(waiter)

        # Foreground preference: interactive classes (chat/instant/embedding/
        # reranking/vision) may evict a local background holder when blocked;
        # background work never preempts anything — that is the fairness half
        # of "foreground preference without starvation".
        if foreground and admission_class is not AdmissionClass.BACKGROUND:
            await self._maybe_preempt(key)
        self.hub.schedule_pump()
        # Give the scheduled pump a chance to run before parking on the future.
        await asyncio.sleep(0)

        try:
            if effective_deadline is None:
                lease = await future
            else:
                lease = await asyncio.wait_for(future, timeout=effective_deadline)
        except asyncio.TimeoutError:
            self._unregister(admission_class, waiter)
            await self._release_orphan_lease(admission_class, waiter)
            raise AdmissionRejected("deadline_exceeded") from None
        except asyncio.CancelledError:
            self._unregister(admission_class, waiter)
            await self._release_orphan_lease(admission_class, waiter)
            raise
        except AdmissionRejected:
            self._unregister(admission_class, waiter)
            raise
        return lease

    async def _release_orphan_lease(
        self, admission_class: AdmissionClass, waiter: _Waiter
    ) -> None:
        """Release a lease the pump delivered in the race window between the
        waiter's timeout/cancel and the pump's set_result (swarm review
        PRR-009): the awaiting task is dying and would never release the
        delivered lease, so the slot would sit until TTL. If the pump has
        not created the lease yet, waiter.gone (set by _unregister) makes
        its post-acquire check release the slot instead — either ordering
        frees the slot."""
        key = self.class_budgets[admission_class]
        lease = self._active_leases.get((key, waiter.holder))
        if lease is not None and lease.holder == waiter.holder:
            await lease.release()

    async def _admit_waiter(
        self,
        admission_class: AdmissionClass,
        lane: "Deque[_Waiter]",
        waiter: "_Waiter",
    ) -> bool:
        """Try to admit one hub waiter on behalf of its controller. Returns
        False when the budget blocks further admission for this key."""
        key = self.class_budgets[admission_class]
        degraded_store = False
        try:
            occupancy = await self.store.occupancy(key)
        except Exception:  # noqa: BLE001 — degraded store: fail open
            self._mark_degraded()
            degraded_store = True
            occupancy = -1
        budget = self.budgets.get(key, 1)
        if occupancy >= budget > 0:
            return False
        if not degraded_store:
            try:
                acquired = await self.store.try_acquire(
                    key, waiter.holder, self.ttl_seconds
                )
            except Exception:  # noqa: BLE001 — degraded store: fail open
                self._mark_degraded()
                degraded_store = True
                acquired = True
            if not acquired:
                return False
        try:
            lane.remove(waiter)
        except ValueError:
            if not degraded_store:
                try:
                    await self.store.release(key, waiter.holder)
                except Exception:  # noqa: BLE001
                    self._mark_degraded()
            return True
        if waiter.gone or waiter.future.done():
            # The waiter vanished (deadline/cancel) while we acquired on its
            # behalf — give the slot straight back.
            if not degraded_store:
                try:
                    await self.store.release(key, waiter.holder)
                except Exception:  # noqa: BLE001
                    self._mark_degraded()
            return True
        lease = _Lease(
            self,
            admission_class,
            waiter.holder,
            key,
            store_bound=not degraded_store,
        )
        lease.task = waiter.task
        self._local_actives.add((key, waiter.holder))
        self._active_leases[(key, waiter.holder)] = lease
        lease._start_renewal()
        if not waiter.future.done():
            waiter.future.set_result(lease)
        return True

    async def _maybe_preempt(self, key: str) -> None:
        """Foreground preference: logically evict ONE local background holder
        when the budget is saturated by background work. The evicted task is
        never cancelled; its later release is a no-op."""
        try:
            if await self.store.occupancy(key) < self.budgets.get(key, 1):
                return
        except Exception:  # noqa: BLE001 — degraded store: fail open
            self._mark_degraded()
            return
        for active_key, holder in list(self._local_actives):
            if active_key != key:
                continue
            lease = self._active_leases.get((active_key, holder))
            if lease is None or lease.admission_class != AdmissionClass.BACKGROUND:
                continue
            lease.revoked = True
            self._forget_lease(lease)
            try:
                await self.store.release(active_key, holder)
            except Exception:  # noqa: BLE001 — best-effort eviction
                self._mark_degraded()
            return

    def _forget_lease(self, lease: _Lease) -> None:
        self._local_actives.discard((lease.key, lease.holder))
        self._active_leases.pop((lease.key, lease.holder), None)

    def _unregister(self, admission_class: AdmissionClass, waiter: _Waiter) -> None:
        waiter.gone = True
        for lane in (self.hub.fg[admission_class], self.hub.bg[admission_class]):
            try:
                lane.remove(waiter)
            except ValueError:
                pass

    def _reject_own_waiters(self, reason: str) -> None:
        for admission_class in AdmissionClass:
            for lane in (self.hub.fg[admission_class], self.hub.bg[admission_class]):
                for waiter in list(lane):
                    if waiter.controller is self:
                        waiter.gone = True
                        lane.remove(waiter)
                        if not waiter.future.done():
                            waiter.future.set_exception(AdmissionRejected(reason))


# Route-level chat gate marker (explicit no-nesting contract).
#
# The chat route holds the CHAT budget for the whole stream; the engine's
# generation-phase gate SKIPS its own acquire when this marker is set in the
# current task context, so route+engine never double-acquire the same key.
# This is deliberately explicit rather than transparent reentrancy: a second
# admit for a held key in the same task must queue (independent callers), so
# nesting is avoided structurally instead of detected.
from contextvars import ContextVar  # noqa: E402

_chat_gate_var: ContextVar[bool] = ContextVar("ragapp_chat_gate_held", default=False)


def mark_chat_gate():
    """Mark the current task context as already holding the chat gate."""
    return _chat_gate_var.set(True)


def reset_chat_gate(token) -> None:
    """Restore the previous chat-gate marker state."""
    _chat_gate_var.reset(token)


def chat_gate_held() -> bool:
    """True when the current request already holds the route-level chat gate."""
    return _chat_gate_var.get()


_controller: Optional[AdmissionController] = None


def get_admission_controller() -> AdmissionController:
    """Process singleton. Built from settings on first use."""
    global _controller
    if _controller is None:
        from app.config import settings

        _controller = AdmissionController.from_settings(settings)
    return _controller


def reset_admission_controller() -> None:
    """Test/teardown hook — drop the singleton."""
    global _controller
    _controller = None
