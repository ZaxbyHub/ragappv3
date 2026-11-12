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
from app.services.admission import (  # noqa: E402
    AdmissionClass,
    AdmissionController,
    AdmissionRejected,
    MemoryAdmissionStore,  # noqa: E402
)


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
        started = asyncio.get_running_loop().time()
        agen = engine.query("quarterly roadmap status", [], stream=True)
        try:
            chunks = await asyncio.wait_for(_consume(agen), 10.0)
        finally:
            await _drain_leftover_tasks()
        elapsed = asyncio.get_running_loop().time() - started

    observed_cancel = await asyncio.wait_for(store.cancelled_seen.wait(), 5.0)
    assert observed_cancel and store.cancelled is True, (
        "the budget must CANCEL the in-flight embed, not just complete the "
        "turn around it"
    )
    assert chunks, "the turn must still complete FTS-only"
    assert _warned_mechanism(caplog)
    assert elapsed < 5.0, (
        f"value sensitivity: a 0.3s configured budget must degrade well "
        f"inside the 10s check ceiling (elapsed {elapsed:.2f}s) — a "
        "hard-coded 5s bound would also pass the ceiling assert"
    )


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
    assert store.search_calls, (
        "the FTS search must still run — an empty kwargs list would make "
        "the all() below vacuously true (swarm-review F-004/PRR-007)"
    )
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


# ---------------------------------------------------------------------------
# Implementation-review round 1 additions (finding 1 + questions 1/2).
# ---------------------------------------------------------------------------


async def test_budget_expiry_releases_the_real_admission_slot():
    """Implementation-review finding 1 (real-controller probe): when the
    budget window cancels the holder mid-embed, the EMBEDDING lease MUST be
    released — occupancy returns to zero and a second admit succeeds —
    instead of holding the slot until the store TTL and eroding the shared
    #518 budget on every expiry. Drives a REAL AdmissionController over a
    MemoryAdmissionStore (no controller double can observe this)."""
    store = MemoryAdmissionStore()
    controller = AdmissionController(
        budgets={"embedding": 1},
        class_budgets={AdmissionClass.EMBEDDING: "embedding"},
        store=store,
        instance_id="companion-budget-expiry",
    )

    async def _hold_until_cancelled():
        async with asyncio.timeout(0.3):
            async with controller.admit(AdmissionClass.EMBEDDING):
                await asyncio.Future()  # hang until the budget cancels us

    try:
        await asyncio.wait_for(_hold_until_cancelled(), 10.0)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        pass  # the budget expiry is the expected outcome

    # The release must complete despite the cancellation: poll briefly for
    # occupancy to return to zero (the shielded store release may finish a
    # tick after the cancellation surfaced).
    occupancy = -1
    for _ in range(50):
        occupancy = await store.occupancy("embedding")
        if occupancy == 0:
            break
        await asyncio.sleep(0.02)
    assert occupancy == 0, (
        "budget expiry must release the EMBEDDING slot (occupancy "
        f"{occupancy}); a slot held to the store TTL erodes the shared "
        "budget on every expiry"
    )
    assert not controller._local_actives, (
        f"local lease accounting must forget the cancelled holder; got "
        f"{controller._local_actives}"
    )

    # And the freed slot is immediately usable again.
    async with asyncio.timeout(5.0):
        async with controller.admit(AdmissionClass.EMBEDDING):
            pass


def test_telemetry_binds_json_under_its_own_name():
    """Bounds frozen C13's matcher limitation (implementation-review
    question 2): the frozen seam check matches Call nodes on the literal
    name ``json``. An aliased ``import json as j; j.dumps(...)`` would
    evade it — this pin makes that evasion impossible in telemetry.py by
    requiring the module binds json under its own name (no alias)."""
    import ast as _ast

    import app.services.telemetry as telemetry_module

    src = open(telemetry_module.__file__, encoding="utf-8").read()
    tree = _ast.parse(src)
    json_aliases = set()
    plain_json_import = False
    from_import_offenders = []
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Import):
            for alias in node.names:
                if alias.name == "json":
                    if alias.asname is None:
                        plain_json_import = True
                    else:
                        json_aliases.add(alias.asname)
        elif isinstance(node, _ast.ImportFrom):
            if node.module == "json":
                # `from json import dumps` (aliased or not) puts a bare
                # Name binding outside the frozen matcher's `json.<attr>`
                # shape — flag every spelling of it.
                from_import_offenders.append(
                    ", ".join(a.name + (f" as {a.asname}" if a.asname else "")
                              for a in node.names)
                )
    assert plain_json_import and not json_aliases, (
        "telemetry.py must import json under its own name (no alias) so "
        "the frozen no-direct-json.dumps matcher cannot be evaded by an "
        "aliased binding"
    )
    assert not from_import_offenders, (
        "telemetry.py must not `from json import ...` (offenders: "
        f"{from_import_offenders}) — a bare dumps() Name evades both the "
        "frozen matcher and this pin"
    )

# ---------------------------------------------------------------------------
# Feedback-round pins (swarm review F-001/F-004/F-005 + PRR-007 companion).
# ---------------------------------------------------------------------------


class _RouteRecordingStore:
    """Route-level store double: gates on embedding_service presence,
    records embed calls, serves one memory row."""

    def __init__(self):
        from types import SimpleNamespace

        self.embedding_service = SimpleNamespace()
        self.embed_calls = []

    async def embed_query(self, query):
        self.embed_calls.append(query)
        return [1.0, 0.0]

    def search_memories(self, query, limit=5, **kwargs):
        from test_memory_query_lifecycle import _memory_record

        return [_memory_record()]


def _route_client(store):
    """Fresh FastAPI app mounting the memories router with overrides
    (mirrors the frozen AC2 test's builder so the boundary pins drive the
    real route validation)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.deps import (
        get_current_active_user,
        get_db,
        get_evaluate_policy,
        get_memory_store,
        require_model_ready,
    )

    async def _evaluate(_user, _kind, _vault_id, _action):
        return True

    app = FastAPI()
    app.include_router(memories_routes.router, prefix="/api")
    app.dependency_overrides.update({
        get_db: lambda: None,
        get_memory_store: lambda: store,
        get_current_active_user: lambda: {"id": 1, "role": "member", "username": "u"},
        get_evaluate_policy: lambda: _evaluate,
        require_model_ready: lambda: None,
    })
    return TestClient(app)


def test_post_search_rejects_oversized_query_before_embed():
    """F-004/PRR-002: the POST /memories/search body cap (the
    MemorySearchRequest.query max_length) is claimed in the PR body and the
    release fragment but was asserted by no test — deleting the POST cap
    left every suite green. Pins: an oversized POST body gets 422 with ZERO
    embed calls; a normal POST body still returns 200 with results."""
    store = _RouteRecordingStore()
    client = _route_client(store)
    oversized = client.post(
        "/api/memories/search",
        json={"query": "q" * 10001, "limit": 5, "vault_id": 1},
    )
    assert oversized.status_code == 422, (
        "an over-cap POST query must be rejected with 422 before any "
        f"embedding work; got {oversized.status_code}: {oversized.text[:200]}"
    )
    assert store.embed_calls == [], (
        f"no embed may run for a rejected POST body; got {store.embed_calls}"
    )

    control = client.post(
        "/api/memories/search",
        json={"query": "roadmap", "limit": 5, "vault_id": 1},
    )
    assert control.status_code == 200, control.text[:400]
    assert control.json()["total"] >= 1, (
        "control leg: the normal POST query must still be served")
    assert store.embed_calls == ["roadmap"], (
        f"control leg: exactly one embed; got {store.embed_calls}")


def test_get_search_boundary_pins_the_exact_10000_cap():
    """F-004: the GET 422 boundary was pinned only at 20001 — a mutant
    raising the cap to 15000/20000 stayed green. Pins both sides of the
    exact 10000 boundary: 10001 rejected (zero embeds), 10000 accepted."""
    store = _RouteRecordingStore()
    client = _route_client(store)
    over = client.get(
        "/api/memories/search", params={"query": "q" * 10001, "vault_id": 1}
    )
    assert over.status_code == 422, (
        f"a 10001-char query must be rejected; got {over.status_code}")
    assert store.embed_calls == [], "rejected query must not embed"

    at_cap = client.get(
        "/api/memories/search", params={"query": "q" * 10000, "vault_id": 1}
    )
    assert at_cap.status_code == 200, (
        f"the exactly-at-cap query must be accepted; got "
        f"{at_cap.status_code}: {at_cap.text[:200]}")
    assert store.embed_calls == ["q" * 10000], (
        "the at-cap query is served and embedded (cap is inclusive)")


async def test_cancelled_release_wakeup_is_deferred_until_store_release_completes():
    """F-002 (delta-crit Important): the cancelled path must NOT pump before
    the detached store release completes — an immediate pump reads a
    still-held slot on a latency store and the parked waiter stays parked.
    Drives a real controller (budget 1) over a yielding store: the holder
    exits its body instantly, the cancel lands DURING the shielded release
    (gated on the release-started event, the F-001 pin's deterministic
    position), a waiter is parked meanwhile, and the pinned assertion is
    that the waiter gets ADMITTED afterwards (kills both the immediate-pump
    and the no-wakeup mutants)."""
    store = MemoryAdmissionStore()
    controller = AdmissionController(
        budgets={"embedding": 1},
        class_budgets={AdmissionClass.EMBEDDING: "embedding"},
        store=store,
        instance_id="f002-deferred-pump",
    )
    release_started = asyncio.Event()
    orig_release = store.release

    async def _yielding_release(key, holder):
        release_started.set()
        await asyncio.sleep(0.05)
        await orig_release(key, holder)

    store.release = _yielding_release

    async def _holder():
        async with controller.admit(AdmissionClass.EMBEDDING):
            pass  # the body completes; the cancel lands in release below

    holder_task = asyncio.create_task(_holder())
    await asyncio.wait_for(release_started.wait(), 5.0)

    waiter_got_slot = asyncio.Event()

    async def _waiter():
        async with controller.admit(AdmissionClass.EMBEDDING):
            waiter_got_slot.set()
            await asyncio.sleep(0)

    waiter_task = asyncio.create_task(_waiter())
    # let the waiter park while the detached release is still in flight
    for _ in range(4):
        await asyncio.sleep(0)

    holder_task.cancel()
    with _suppress_cancel():
        await asyncio.wait_for(holder_task, 5.0)

    # head: the deferred done-callback pumps AFTER the detached release
    # finishes -> the parked waiter is admitted. Mutant "immediate pump":
    # pumped while the slot is still held -> waiter never re-woken -> RED.
    # Mutant "no wakeup": never pumped -> RED.
    await asyncio.wait_for(waiter_got_slot.wait(), 2.0)
    # cleanup: the waiter still holds the slot it was admitted into
    waiter_task.cancel()
    with _suppress_cancel():
        await waiter_task


def test_memory_embed_budget_validator_rejects_non_finite():
    """F-005: the knob's gt=0 accepted inf/1e400 (asyncio.timeout(inf)
    never fires — the #939 budget silently disabled). The new
    validate_memory_embed_budget_seconds must reject every non-finite
    value and bound the ceiling, matching the sibling timeout knobs."""
    import pytest

    from app.config import Settings

    for bad in (float("inf"), float("1e400"), float("nan")):
        with pytest.raises(ValueError):
            Settings.validate_memory_embed_budget_seconds(bad)
    assert Settings.validate_memory_embed_budget_seconds(10.0) == 10.0
    assert Settings.validate_memory_embed_budget_seconds(600.0) == 600.0
    with pytest.raises(ValueError):
        Settings.validate_memory_embed_budget_seconds(600.1)
    # The gt=0 bound lives on the Field: pin it at the model level (a
    # non-positive budget would also disable the asyncio.timeout guard).
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        Settings(memory_embed_budget_seconds=-1.0)
    with pytest.raises(pydantic.ValidationError):
        Settings(memory_embed_budget_seconds=float("inf"))


def test_eight_term_cap_lower_bound(tmp_path):
    """F-004: the 8-content-term cap had only an UPPER-side pin (a row with
    only term 9), so a mutant narrowing the cap to [:5] stayed green. A row
    carrying ONLY content term 6 (inside 1..8, outside 1..5) kills it: the
    real cap includes term 6 in the query (match); the narrowed cap drops
    it (no match)."""
    import sys as _sys
    from pathlib import Path as _Path

    _sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
    for _mod in ("lancedb", "pyarrow"):
        if _mod not in _sys.modules:
            try:
                __import__(_mod)
            except ImportError:
                import types as _types

                _sys.modules[_mod] = _types.ModuleType(_mod)
    if "lancedb.index" not in _sys.modules:
        import types as _types

        _idx = _types.ModuleType("lancedb.index")
        _idx.FTS = type("FTS", (), {})
        _idx.IvfPq = type("IvfPq", (), {})
        _sys.modules["lancedb.index"] = _idx

    from app.models.database import SQLiteConnectionPool, init_db
    from app.services.memory_store import MemoryStore

    db_path = _Path(str(tmp_path)) / "cap-lower.db"
    init_db(str(db_path))
    pool = SQLiteConnectionPool(str(db_path), max_size=2)
    store = MemoryStore(pool=pool)
    # Under FTS5's implicit AND, MORE kept query terms = STRICTER match.
    # A row carrying terms 1-6 only:
    #   - head cap [:8]: the query keeps 8 terms -> the row lacks 7,8
    #     -> NO match;
    #   - narrowed-cap mutant ([:5]): the query keeps terms 1-5 -> the row
    #     has all five -> MATCH. Asserting NOT-match therefore kills the
    #     mutant and passes at head (the discriminating direction).
    store.add_memory(
        "term1 term2 term3 term4 term5 term6",
        category="terms-1-6",
    )
    # Sanity rows: the full row matches every cap value; a term-9-only row
    # matches no AND query here.
    store.add_memory(
        "term1 term2 term3 term4 term5 term6 term7 term8 term9 term10",
        category="all",
    )
    try:
        query8 = "term1 term2 term3 term4 term5 term6 term7 term8"
        results = store.search_memories(query8, limit=10)
        contents = [r.content for r in results]
        assert not any(c == "term1 term2 term3 term4 term5 term6" for c in contents), (
            "the 8-term cap must keep all 8 content terms: a row carrying "
            "only terms 1-6 must NOT match the 8-term AND query at the true "
            f"cap (it DOES match a narrowed cap-5 mutant); got {contents}"
        )
        assert any(c.startswith("term1 term2 term3") for c in contents), (
            f"the full row must match the 8-term query; got {contents}"
        )
    finally:
        pool.close_all()


async def test_degrade_arms_name_budget_and_admission_reason(caplog, monkeypatch):
    """F-003 (delta-crit Important): deleting both named arms at both call
    sites left every suite green (the existing oracles match only the shared
    prefix). Pins the arm DISTINCT text: budget expiry names
    memory_embed_budget_seconds; admission saturation names the rejection
    reason — at the engine and the memories-route call sites."""
    _pin_embed_budget(monkeypatch)  # 0.3s at head

    # ---- engine site: budget expiry names the knob ----
    store = _FakeMemoryStore(hang=True)
    engine = _make_engine(store)
    with caplog.at_level(logging.WARNING), _memory_admission_patches(
        _CountingAdmissionController()
    ):
        agen = engine.query("quarterly roadmap status", [], stream=True)
        try:
            await asyncio.wait_for(_consume(agen), 10.0)
        finally:
            await _drain_leftover_tasks()
    expiry_msgs = [
        r.getMessage() for r in caplog.records
        if r.levelno >= logging.WARNING and "memory_embed_budget_seconds" in r.getMessage()
    ]
    assert expiry_msgs, (
        "the budget-expiry WARNING must name memory_embed_budget_seconds "
        "(the arm discriminator); records: "
        + repr([r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING])
    )
    caplog.clear()

    # ---- engine site: saturated budget names the rejection reason ----
    store2 = _FakeMemoryStore(hang=False)
    engine2 = _make_engine(store2)
    saturated = _CountingAdmissionController(reject_embedding=True)
    with caplog.at_level(logging.WARNING), _memory_admission_patches(saturated):
        agen2 = engine2.query("quarterly roadmap status", [], stream=True)
        try:
            await asyncio.wait_for(_consume(agen2), 10.0)
        finally:
            await _drain_leftover_tasks()
    assert any(
        "admission rejected" in r.getMessage() for r in caplog.records
        if r.levelno >= logging.WARNING
    ), "the saturated-budget WARNING must carry the admission rejection reason"
    caplog.clear()

    # ---- route site: budget expiry names the knob ----
    route_store = _RouteStore(hang=True)
    with caplog.at_level(logging.WARNING):
        with patch(
            "app.api.routes.memories.get_admission_controller",
            lambda: _CountingAdmissionController(),
            create=True,
        ):
            await asyncio.wait_for(
                memories_routes._perform_memory_search(
                    route_store, "quarterly roadmap", limit=5, vault_id=1
                ),
                10.0,
            )
    assert any(
        "memory_embed_budget_seconds" in r.getMessage() for r in caplog.records
        if r.levelno >= logging.WARNING
    ), "the route budget-expiry WARNING must name memory_embed_budget_seconds"


async def test_cancel_during_yielding_store_release_keeps_shield_and_reraise():
    """F-001 (swarm-review ship blocker): the cancellation-completes-
    release behaviour must be PINNED, not just implemented — removing
    ``asyncio.shield`` from ``_Lease.release`` left ~760 related tests
    green (Redis-class stores only), and dropping the final
    ``CancelledError`` re-raise swallowed the #827 cancellation contract.
    Drives a REAL AdmissionController over a MemoryAdmissionStore whose
    release YIELDS (event-gated) so the cancel lands deterministically
    INSIDE the shielded await — never in the renewer join (the reviewer's
    Windows warning). Asserts all three legs: the holder task ends
    CANCELLED (re-raise), the after-block code never ran, and the shielded
    detached release still frees the slot (occupancy 0)."""
    store = MemoryAdmissionStore()
    controller = AdmissionController(
        budgets={"embedding": 1},
        class_budgets={AdmissionClass.EMBEDDING: "embedding"},
        store=store,
        instance_id="f001-shield-pin",
    )
    release_started = asyncio.Event()
    orig_release = store.release

    async def _yielding_release(key, holder):
        release_started.set()
        await asyncio.sleep(0.05)  # the cancel arrives inside this window
        await orig_release(key, holder)

    store.release = _yielding_release  # instance override: yields mid-release

    after_block_ran = False

    async def _holder():
        nonlocal after_block_ran
        async with controller.admit(AdmissionClass.EMBEDDING):
            pass
        after_block_ran = True

    holder_task = asyncio.create_task(_holder())
    await asyncio.wait_for(release_started.wait(), 5.0)
    holder_task.cancel()
    with _suppress_cancel():
        await asyncio.wait_for(holder_task, 5.0)

    assert holder_task.cancelled() is True, (
        "the final CancelledError re-raise must surface at the holder "
        "(the #827 cancellation contract); a release that swallows the "
        "cancellation ends the task normally")
    assert after_block_ran is False, (
        "code after the async-with must NOT run when the cancellation "
        "surfaces during release")
    occupancy = -1
    for _ in range(50):
        occupancy = await store.occupancy("embedding")
        if occupancy == 0:
            break
        await asyncio.sleep(0.02)
    assert occupancy == 0, (
        "the shield must keep the detached store release running to "
        f"completion under the cancellation; occupancy {occupancy}")


class _suppress_cancel:
    """contextlib.suppress(asyncio.CancelledError) twin that does NOT
    swallow a CancelledError raised through wait_for's own timeout path
    (wait_for(timeout) raises TimeoutError, which is not suppressed)."""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return exc_type is not None and issubclass(exc_type, asyncio.CancelledError)
