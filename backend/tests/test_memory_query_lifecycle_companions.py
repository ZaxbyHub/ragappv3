"""Non-frozen companion tests for the memory-query lifecycle fixes (#939).

The frozen checks (backend/tests/test_memory_query_lifecycle.py, checkpoint
manifest for the 939-940-941 trace) prove the AC-level behavior; these
companions pin the MECHANICS the frozen checks cannot assert on their own
(plan-critic round 1, finding 6):

* budget fire -> the hung embed actually observes cancellation (not just
  turn completion + WARNING);
* rejected admission -> ZERO embed calls and the FTS search still runs with
  ``query_embedding=None`` (occupancy, not admission counting);
* the embed runs while the EMBEDDING lease is held (active-lease recorder);
* a blocked admission (never-yielding acquire) still completes FTS-only
  within the budget (the total budget bounds the acquire too);
* the memories ROUTE precompute (``_perform_memory_search``) — hanging-embed
  cancellation via the route timeout, rejected-admission degrade, and the
  FTS-only kwarg.

Not a frozen file — edits here need no manifest AMEND.
"""

import asyncio
import logging
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# pytest (rootdir=backend, no tests package) puts this directory on sys.path,
# so the frozen suite's doubles are importable — one source of truth for the
# harness shapes, and the frozen file's bytes stay untouched.
from test_memory_query_lifecycle import (  # noqa: E402
    _CountingAdmissionController,
    _engine_patches,
    _FakeMemoryStore,
    _make_engine,
    _memory_admission_patches,
    _pin_embed_budget,
)

from app.api.routes import memories as memories_routes  # noqa: E402
from app.services.admission import AdmissionClass  # noqa: E402


class _LeaseTrackingController(_CountingAdmissionController):
    """Records whether the embed callback ran while an EMBEDDING lease was
    held (active-lease occupancy, not admission counting)."""

    def __init__(self):
        super().__init__()
        self._active = False
        self.lease_held_during_embed = None

    def admit(self, admission_class):
        if admission_class is not AdmissionClass.EMBEDDING:
            return super().admit(admission_class)
        controller = self

        class _Lease:
            async def __aenter__(self):
                controller.embedding_admits += 1
                controller._active = True
                return self

            async def __aexit__(self, exc_type, exc, tb):
                controller._active = False
                return False

        return _Lease()

    def note_embed_ran(self):
        self.lease_held_during_embed = self._active


class _BlockingAdmissionController(_CountingAdmissionController):
    """admit(EMBEDDING) never yields — the unbounded-acquire shape with no
    default admission deadline (plan-critic round 1, finding 2)."""

    def admit(self, admission_class):
        if admission_class is not AdmissionClass.EMBEDDING:
            return super().admit(admission_class)
        controller = self

        class _Stuck:
            async def __aenter__(self):
                controller.embedding_admits += 1
                await asyncio.Future()  # never completes
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

        return _Stuck()


class _LeaseNotingStore(_FakeMemoryStore):
    """_FakeMemoryStore whose embed_query reports into a lease-tracking
    controller (the embed-inside-held-lease recorder)."""

    def __init__(self, controller):
        super().__init__(hang=False)
        self._controller = controller

    async def embed_query(self, query):
        self._controller.note_embed_ran()
        return await super().embed_query(query)


class _RouteStore(_FakeMemoryStore):
    """_FakeMemoryStore shape the route helper can drive: the sync search
    records its kwargs so the FTS-only leg can assert on them."""

    def search_memories(self, query, limit=5, **kwargs):
        self.search_calls.append({"query": query, "kwargs": dict(kwargs)})
        return []


def _fts_kwargs(store):
    return [entry["kwargs"] for entry in store.search_calls]


def _warned_mechanism(caplog):
    return any(
        "Memory query embedding" in r.getMessage()
        for r in caplog.records
        if r.levelno >= logging.WARNING
    )


async def test_budget_fire_cancels_the_hung_embed(caplog, monkeypatch):
    """C1 mechanics: when the total budget fires, the embed task observes
    asyncio.CancelledError (the store double's cancelled flag), not merely a
    completed turn + WARNING."""
    _pin_embed_budget(monkeypatch)  # 0.3s at head
    store = _FakeMemoryStore(hang=True)
    engine = _make_engine(store)
    controller = _CountingAdmissionController()
    with caplog.at_level(logging.WARNING), _memory_admission_patches(controller):
        agen = engine.query("quarterly roadmap status", [], stream=True)
        try:
            chunks = await asyncio.wait_for(_consume(agen), 10.0)
        finally:
            await _drain_leftover_tasks()

    observed_cancel = await asyncio.wait_for(store.cancelled_seen.wait(), 5.0)
    assert observed_cancel and store.cancelled is True, (
        "the budget must CANCEL the in-flight embed, not just complete the "
        "turn around it"
    )
    assert chunks, "the turn must still complete FTS-only"
    assert _warned_mechanism(caplog)


async def test_rejected_admission_runs_zero_embeds_and_fts_kwarg(
    caplog, monkeypatch
):
    """C4 mechanics: a saturated budget means ZERO embed calls and the FTS
    search still runs with query_embedding=None (the attempted-but-failed
    encoding), not just a completed turn + WARNING."""
    _pin_embed_budget(monkeypatch)
    store = _FakeMemoryStore(hang=False)
    engine = _make_engine(store)
    saturated = _CountingAdmissionController(reject_embedding=True)
    with caplog.at_level(logging.WARNING), _memory_admission_patches(saturated):
        agen = engine.query("quarterly roadmap status", [], stream=True)
        try:
            chunks = await asyncio.wait_for(_consume(agen), 10.0)
        finally:
            await _drain_leftover_tasks()

    assert chunks, "the turn must still complete (FTS-only)"
    assert not store.embed_calls, (
        f"a rejected admission must run ZERO embeds; got {store.embed_calls}"
    )
    assert store.search_calls, "the FTS search must still run"
    assert all(
        kw.get("query_embedding") is None for kw in _fts_kwargs(store)
    ), (
        "the degraded search must pass the attempted-but-failed encoding "
        f"(query_embedding=None); got {_fts_kwargs(store)}"
    )
    assert _warned_mechanism(caplog)


async def test_embed_runs_inside_the_active_lease(monkeypatch):
    """C4 mechanics: the embed executes while the EMBEDDING lease is held —
    occupancy, not admission counting (an admit followed by an embed outside
    the async-with would still count)."""
    _pin_embed_budget(monkeypatch)
    controller = _LeaseTrackingController()
    store = _LeaseNotingStore(controller)
    engine = _make_engine(store)
    with _memory_admission_patches(controller):
        agen = engine.query("quarterly roadmap status", [], stream=True)
        try:
            await asyncio.wait_for(_consume(agen), 10.0)
        finally:
            await _drain_leftover_tasks()

    assert controller.embedding_admits == 1
    assert controller.lease_held_during_embed is True, (
        "the embed must run while the EMBEDDING lease is held; an admit "
        "count alone does not pin occupancy"
    )


async def test_blocked_admission_still_completes_within_budget(
    caplog, monkeypatch
):
    """Finding 2: the total budget must bound the admission ACQUIRE too —
    a controller whose admit never yields cannot stall the turn."""
    _pin_embed_budget(monkeypatch)
    store = _FakeMemoryStore(hang=False)
    engine = _make_engine(store)
    stuck = _BlockingAdmissionController()
    with caplog.at_level(logging.WARNING), _memory_admission_patches(stuck):
        agen = engine.query("quarterly roadmap status", [], stream=True)
        try:
            chunks = await asyncio.wait_for(_consume(agen), 10.0)
        finally:
            await _drain_leftover_tasks()

    assert chunks, "a never-yielding admission must still complete FTS-only"
    assert not store.embed_calls, "no embed may run through a stuck acquire"
    assert _warned_mechanism(caplog)


# ---------------------------------------------------------------------------
# Route-side legs (F4b): _perform_memory_search gets the same wrapper.
# ---------------------------------------------------------------------------


async def test_route_precompute_times_out_and_cancels_hung_embed(monkeypatch):
    _pin_embed_budget(monkeypatch)
    store = _RouteStore(hang=True)
    with _engine_patches():
        with patch(
            "app.api.routes.memories.get_admission_controller",
            lambda: _CountingAdmissionController(),
            create=True,
        ):
            started = asyncio.get_running_loop().time()
            rows = await asyncio.wait_for(
                memories_routes._perform_memory_search(
                    store, "quarterly roadmap", limit=5, vault_id=1
                ),
                10.0,
            )
            elapsed = asyncio.get_running_loop().time() - started
    assert rows == []
    assert store.cancelled is True, (
        "the route precompute timeout must cancel the hung embed"
    )
    assert elapsed < 9.0, (
        "the route helper must degrade within the configured budget, not "
        "the check's ceiling"
    )


async def test_route_precompute_rejected_admission_degrades_fts(monkeypatch):
    _pin_embed_budget(monkeypatch)
    store = _RouteStore(hang=False)
    saturated = _CountingAdmissionController(reject_embedding=True)
    with _engine_patches():
        with patch(
            "app.api.routes.memories.get_admission_controller",
            lambda: saturated,
            create=True,
        ):
            rows = await asyncio.wait_for(
                memories_routes._perform_memory_search(
                    store, "quarterly roadmap", limit=5, vault_id=1
                ),
                10.0,
            )
    assert rows == []
    assert not store.embed_calls, "rejected admission must run zero embeds"
    assert store.search_calls, "the FTS search must still run"
    assert all(kw.get("query_embedding") is None for kw in _fts_kwargs(store))


async def test_route_precompute_happy_path_passes_embedding(monkeypatch):
    """Control: with admission available and a healthy embedder, the route
    helper still passes the precomputed embedding (no normal-path change)."""
    _pin_embed_budget(monkeypatch)
    store = _RouteStore(hang=False)
    with _engine_patches():
        with patch(
            "app.api.routes.memories.get_admission_controller",
            lambda: _CountingAdmissionController(),
            create=True,
        ):
            await asyncio.wait_for(
                memories_routes._perform_memory_search(
                    store, "quarterly roadmap", limit=5, vault_id=1
                ),
                10.0,
            )
    assert store.embed_calls == ["quarterly roadmap"]
    assert all(
        kw.get("query_embedding") == [1.0, 0.0] for kw in _fts_kwargs(store)
    )


# ---------------------------------------------------------------------------
# Local loop helpers.
# ---------------------------------------------------------------------------


async def _consume(agen):
    out = []
    async for chunk in agen:
        out.append(chunk)
    return out


async def _drain_leftover_tasks():
    for task in list(asyncio.all_tasks()):
        if task is not asyncio.current_task() and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
