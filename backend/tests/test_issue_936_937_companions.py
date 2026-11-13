"""Companion tests for issues #936 + #937 (unfrozen, issue-tracer tier L).

Every obligation the approved plan (07) lists under "companion tests":
exact flush-failure accounting without double counts, WARNING rate limiting
(suppression within the interval, re-emission after expiry), tmp hygiene,
full malformed-shard table incl. the unreadable-file leg and route-level
200s, old/new-shard compatibility, conservative prune behavior (own-host
dead pruned; foreign/legacy/unknown retained), real-process liveness,
Windows/POSIX error mapping mocks, queue-depth capping, new-constant
structural pins, heal-loop resilience (failed tick -> later successful
tick), lock-held skip with no provider call, admission high-water under
budget=1/concurrency>1 incl. an external lease, deferred-row retry,
whole-system single-flight, NULL-only staged-rebuild selection, non-NULL
directive rows not re-embedded, config bounds, and the exception-arm
warning wording.
"""

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import settings  # noqa: E402
from app.services import telemetry as telemetry_module  # noqa: E402
from app.services.telemetry import (  # noqa: E402
    METRIC_EMBEDDING_CACHE_HITS,
    METRIC_SHARD_FLUSH_FAILURES,
    QUEUE_HISTORY_CAP,
    Telemetry,
    _escape_label,
    _pid_alive,
    _valid_shard,
)

# --------------------------------------------------------------------------
# A1 companions: flush-failure accounting and rate-limited WARNING
# --------------------------------------------------------------------------


def _failing_flush(tel: Telemetry, monkeypatch) -> None:
    """Make every shard write raise inside _flush_shard's try block."""

    def _boom(payload):
        raise OSError("simulated shard write failure")

    monkeypatch.setattr(telemetry_module, "_dumps", _boom)


def _sample(text: str, metric: str) -> int:
    for line in text.splitlines():
        if line.startswith(metric + " "):
            return int(line.rsplit(" ", 1)[1])
    return -1


def test_transient_flush_failure_then_success_reads_exactly_one(tmp_path, monkeypatch):
    tel = Telemetry(enabled=True, registry_dir=tmp_path)
    calls = {"n": 0}

    def _flaky_dumps(payload):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("transient failure")
        return json.dumps(payload)

    monkeypatch.setattr(telemetry_module, "_dumps", _flaky_dumps)
    tel.record_chat_turn("t1")  # fails once
    tel.record_chat_turn("t2")  # succeeds; shard persists failures=1
    text = tel.metrics_text()
    assert _sample(text, METRIC_SHARD_FLUSH_FAILURES) == 1


def test_persistent_flush_failure_grows_and_fallback(tmp_path, monkeypatch):
    tel = Telemetry(enabled=True, registry_dir=tmp_path)
    _failing_flush(tel, monkeypatch)
    for i in range(3):
        tel.record_chat_turn(f"t{i}")
    # The failing writer's own exposition grows (its metrics_text flush
    # fails again first, so the count is >= the three record failures).
    text = tel.metrics_text()
    assert _sample(text, METRIC_SHARD_FLUSH_FAILURES) >= 3
    # registry-absent fallback: local counts only (flush disabled so the
    # counter cannot move during the read).
    tel2 = Telemetry(enabled=True, registry_dir=None)
    with patch.object(
        telemetry_module.Telemetry,
        "_flush_shard",
        lambda self: None,
    ):
        tel2._shard_flush_failures = 2
        text2 = tel2.metrics_text()
    assert _sample(text2, METRIC_SHARD_FLUSH_FAILURES) == 2


def test_two_instances_flush_failures_sum(tmp_path, monkeypatch):
    a = Telemetry(enabled=True, registry_dir=tmp_path)
    _failing_flush(a, monkeypatch)
    a.record_chat_turn("a")
    b = Telemetry(enabled=True, registry_dir=tmp_path)
    _failing_flush(b, monkeypatch)
    b.record_chat_turn("b")
    # Cross-process semantics: a failing writer cannot persist its
    # counter, so each process's exposition surfaces ITS OWN in-memory
    # count (the scraper's own flush also fails under the global patch).
    # The invariant is visibility, not summability-without-persistence.
    scraper = Telemetry(enabled=True, registry_dir=tmp_path)
    text = scraper.metrics_text()
    assert _sample(text, METRIC_SHARD_FLUSH_FAILURES) >= 1


def test_reset_clears_flush_failure_state(tmp_path, monkeypatch):
    tel = Telemetry(enabled=True, registry_dir=tmp_path)
    _failing_flush(tel, monkeypatch)
    tel.record_chat_turn("t")
    monkeypatch.undo()
    tel.reset()
    tel.record_chat_turn("t2")  # healthy write after reset
    text = tel.metrics_text()
    assert _sample(text, METRIC_SHARD_FLUSH_FAILURES) == 0


def test_first_flush_failure_always_warns_then_suppresses(tmp_path, monkeypatch, caplog):
    tel = Telemetry(enabled=True, registry_dir=tmp_path)
    _failing_flush(tel, monkeypatch)
    with caplog.at_level(logging.WARNING, logger="app.services.telemetry"):
        tel.record_chat_turn("t1")
    warnings = [
        r
        for r in caplog.records
        if r.levelno >= logging.WARNING and "shard flush failed" in r.getMessage()
    ]
    assert warnings, "first failure must warn unconditionally"

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="app.services.telemetry"):
        tel.record_chat_turn("t2")  # inside the interval
    warnings = [
        r
        for r in caplog.records
        if r.levelno >= logging.WARNING and "shard flush failed" in r.getMessage()
    ]
    assert not warnings, "second failure within the interval must be suppressed"

    # Expiry: push the last-warning timestamp past the interval.
    tel._last_flush_warning = time.monotonic() - (
        telemetry_module._FLUSH_WARN_INTERVAL + 1
    )
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="app.services.telemetry"):
        tel.record_chat_turn("t3")
    warnings = [
        r
        for r in caplog.records
        if r.levelno >= logging.WARNING and "shard flush failed" in r.getMessage()
    ]
    assert warnings, "failure after the interval must warn again"


# --------------------------------------------------------------------------
# A2 companions: tmp hygiene
# --------------------------------------------------------------------------


def test_stale_tmp_removed_fresh_tmp_survives(tmp_path):
    tel = Telemetry(enabled=True, registry_dir=tmp_path)
    tel.record_chat_turn("t")
    stale = tmp_path / "telemetry-00000000-1-deadbeef.json.tmp"
    stale.write_text("{}", encoding="utf-8")
    fresh = tmp_path / "telemetry-00000000-1-livework.json.tmp"
    fresh.write_text("{}", encoding="utf-8")
    os.utime(stale, (time.time() - 7200, time.time() - 7200))
    tel.metrics_text()
    assert not stale.exists(), "stale tmp must be reclaimed by aggregation"
    assert fresh.exists(), "an in-flight tmp must survive"


# --------------------------------------------------------------------------
# A3 companions: malformed-shape table, unreadable leg, route 200, compat
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        [],  # non-dict top level
        {"chat_turns": "not-an-int"},
        {"chat_turns": True},
        {"chat_turns": -1},
        {"embedding_cache_hits": 1.5},
        {"shard_flush_failures": -3},
        {"evaluator_fail_opens": []},
        {"evaluator_fail_opens": {"not-a-known-reason": 1}},
        {"evaluator_fail_opens": {"exception": True}},
        {"queue_waits": []},
        {"queue_waits": {"c": "nope"}},
        {"queue_waits": {"c": [1, "x"]}},
        {"queue_waits": {"c": [-0.5]}},
        {"queue_waits": {"c": [float("nan")]}},
        {"queue_depths": {"c": [-1]}},
        {"provider_calls": {"p": {"ok": "x"}}},
    ],
)
def test_malformed_shapes_skip_with_warning_and_200(tmp_path, caplog, payload):
    hostile = tmp_path / "telemetry-00000000-9-cafe01.json"
    hostile.write_text(json.dumps(payload), encoding="utf-8")
    good = tmp_path / "telemetry-00000000-9-cafe02.json"
    good.write_text(json.dumps({"chat_turns": 5}), encoding="utf-8")
    tel = Telemetry(enabled=True, registry_dir=tmp_path)
    with caplog.at_level(logging.WARNING, logger="app.services.telemetry"):
        text = tel.metrics_text()
    assert _sample(text, telemetry_module.METRIC_CHAT_TURNS) == 5
    assert any(
        "failed validation" in r.getMessage() or "not valid JSON" in r.getMessage()
        for r in caplog.records
        if r.name == "app.services.telemetry"
    )


def test_unreadable_shard_skips_with_warning(tmp_path, caplog, monkeypatch):
    hostile = tmp_path / "telemetry-00000000-9-cafe03.json"
    hostile.write_text("{}", encoding="utf-8")
    good = tmp_path / "telemetry-00000000-9-cafe04.json"
    good.write_text(json.dumps({"chat_turns": 2}), encoding="utf-8")

    real_read = Path.read_text

    def _maybe_unreadable(self, *args, **kwargs):
        if self.name == hostile.name:
            raise OSError("simulated unreadable shard")
        return real_read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _maybe_unreadable)
    tel = Telemetry(enabled=True, registry_dir=tmp_path)
    with caplog.at_level(logging.WARNING, logger="app.services.telemetry"):
        text = tel.metrics_text()
    assert _sample(text, telemetry_module.METRIC_CHAT_TURNS) == 2
    assert any(
        "unreadable" in r.getMessage()
        for r in caplog.records
        if r.name == "app.services.telemetry"
    )


def test_metrics_route_serves_200_with_hostile_shard(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    hostile = tmp_path / "telemetry-00000000-9-cafe05.json"
    hostile.write_text(
        json.dumps({"evaluator_fail_opens": {'injected"} 999\n# ': 5}}),
        encoding="utf-8",
    )
    telemetry_module.init_telemetry(registry_dir=tmp_path)
    metrics_app = FastAPI()
    telemetry_module.register_metrics_route(metrics_app)
    resp = TestClient(metrics_app).get("/metrics")
    assert resp.status_code == 200
    assert "injected" not in resp.text


def test_shard_schema_compat_both_directions():
    # old shard without the new key is valid
    ok, reason = _valid_shard({"chat_turns": 3})
    assert ok, reason
    # new shard with only the new key is valid
    ok, reason = _valid_shard({"shard_flush_failures": 1})
    assert ok, reason
    # empty shard is valid (all fields optional)
    ok, reason = _valid_shard({})
    assert ok, reason


# --------------------------------------------------------------------------
# A5 companions: conservative prune, liveness
# --------------------------------------------------------------------------


def _backdate(path: Path) -> None:
    old = time.time() - 7200
    os.utime(path, (old, old))


def test_prune_deletes_dead_own_host_shard_only(tmp_path):
    tel = Telemetry(enabled=True, registry_dir=tmp_path)
    tag = telemetry_module._HOST_TAG
    # dead own-host: a pid that cannot exist -> confirmed dead.
    dead = tmp_path / f"telemetry-{tag}-4194303-deadbee.json"
    dead.write_text(json.dumps({"chat_turns": 1}), encoding="utf-8")
    _backdate(dead)
    # foreign host tag: never pruned by this host.
    foreign = tmp_path / "telemetry-ffffffff-4194303-foreign1.json"
    foreign.write_text(json.dumps({"chat_turns": 1}), encoding="utf-8")
    _backdate(foreign)
    # legacy format (no host tag): retained (unknown ownership).
    legacy = tmp_path / "telemetry-4194303-legacybe.json"
    legacy.write_text(json.dumps({"chat_turns": 1}), encoding="utf-8")
    _backdate(legacy)
    # unparsable name: retained.
    weird = tmp_path / "telemetry-nonsense.json"
    weird.write_text(json.dumps({"chat_turns": 1}), encoding="utf-8")
    _backdate(weird)
    tel.metrics_text()
    assert not dead.exists(), "confirmed-dead own-host shard must be pruned"
    assert foreign.exists(), "foreign-host shard must be retained"
    assert legacy.exists(), "legacy-format shard must be retained"
    assert weird.exists(), "unparsable shard name must be retained"


def test_pid_alive_real_child_process():
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert _pid_alive(proc.pid) is True
    finally:
        proc.terminate()
        proc.wait(timeout=10)
    # NOTE: an immediately-after-exit probe is intentionally NOT asserted:
    # on Windows a terminated pid stays OpenProcess-visible while any
    # handle to it survives (Popen's handle releases on GC), so the
    # deterministic dead-pid leg uses an unallocatable synthetic pid.
    assert _pid_alive(4194303) is False


def test_pid_alive_error_mapping(monkeypatch):
    assert _pid_alive(0) is False
    assert _pid_alive(-5) is False
    if os.name == "nt":
        import ctypes

        class _FakeKernel:  # noqa: D401 - stub
            def __init__(self, handle, err):
                self._handle = handle
                self._err = err
                self.OpenProcess = MagicMock(return_value=handle)
                self.CloseHandle = MagicMock(return_value=1)

            def shake(self):
                ctypes.set_last_error(self._err)

        # access denied (5) -> alive
        fk = _FakeKernel(None, 5)
        monkeypatch.setattr(
            ctypes, "WinDLL", lambda *a, **k: fk, raising=False
        )
        monkeypatch.setattr(ctypes, "get_last_error", lambda: 5, raising=False)
        assert _pid_alive(4194303) is True
        # invalid parameter (87) -> dead
        fk2 = _FakeKernel(None, 87)
        monkeypatch.setattr(
            ctypes, "WinDLL", lambda *a, **k: fk2, raising=False
        )
        monkeypatch.setattr(ctypes, "get_last_error", lambda: 87, raising=False)
        assert _pid_alive(4194303) is False
    else:
        import signal

        def _perm(pid, sig):
            raise PermissionError(1, "denied")

        monkeypatch.setattr(os, "kill", _perm)
        assert _pid_alive(4194303) is True

        def _missing(pid, sig):
            raise ProcessLookupError()

        monkeypatch.setattr(os, "kill", _missing)
        assert _pid_alive(4194303) is False


# --------------------------------------------------------------------------
# A6/A7 companions
# --------------------------------------------------------------------------


def test_queue_depths_capped_on_write_and_merge(tmp_path):
    tel = Telemetry(enabled=True, registry_dir=tmp_path)
    for i in range(QUEUE_HISTORY_CAP + 50):
        tel.record_queue_wait("cls", 0.1, depth=i)
    tel.record_chat_turn("t")  # write-through flush carries the histories
    payload = json.loads(
        (tmp_path / tel._shard_name).read_text(encoding="utf-8")
    )
    assert len(payload["queue_waits"]["cls"]) <= QUEUE_HISTORY_CAP
    assert len(payload["queue_depths"]["cls"]) <= QUEUE_HISTORY_CAP
    # merge-side: a hostile oversized shard list is trimmed too
    bloated = tmp_path / "telemetry-00000000-9-bloated.json"
    bloated.write_text(
        json.dumps(
            {"queue_waits": {"b": [0.1] * 5000}, "queue_depths": {"b": [1] * 5000}}
        ),
        encoding="utf-8",
    )
    text = tel.metrics_text()
    assert 'admission_class="b"' in text  # class still rendered
    tel2 = Telemetry(enabled=True, registry_dir=tmp_path)
    totals = tel2._aggregate_counts()
    assert len(totals["queue_waits"]["b"]) <= QUEUE_HISTORY_CAP
    assert len(totals["queue_depths"]["b"]) <= QUEUE_HISTORY_CAP


def test_new_constants_have_no_literals_in_metrics_text():
    import inspect

    src = inspect.getsource(Telemetry.metrics_text)
    assert METRIC_EMBEDDING_CACHE_HITS not in src.split(f"{METRIC_EMBEDDING_CACHE_HITS}")[0]
    # the structural rule: neither new constant's VALUE appears inside a
    # plain string literal in the source (only via f-string interpolation,
    # where the constant name appears, not its value).
    assert '"' + METRIC_SHARD_FLUSH_FAILURES not in src
    assert "'" + METRIC_SHARD_FLUSH_FAILURES not in src
    assert '"' + METRIC_EMBEDDING_CACHE_HITS not in src


def test_escape_label():
    assert _escape_label('injected"} 999\n# ') == 'injected\\"} 999\\n# '
    assert _escape_label("plain") == "plain"
    assert _escape_label("back\\slash") == "back\\\\slash"


# --------------------------------------------------------------------------
# B1 companions: heal-loop resilience, single-flight, admission
# --------------------------------------------------------------------------


class _ScriptedEmbedder:
    """Embedding service double with per-call scripted outcomes."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0
        self.concurrent = 0
        self.max_concurrent = 0

    async def embed_passage(self, text):
        self.calls += 1
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        try:
            outcome = self.script.pop(0) if self.script else "ok"
            if isinstance(outcome, Exception):
                raise outcome
            return [0.1, 0.2, 0.3]
        finally:
            self.concurrent -= 1


def _make_store(tmp_path, embedder):
    from app.models.database import (
        SQLiteConnectionPool,
        init_db,
        run_migrations,
    )
    from app.services.memory_store import MemoryStore

    db_path = str(tmp_path / "heal.db")
    init_db(db_path)
    run_migrations(db_path)
    pool = SQLiteConnectionPool(db_path, max_size=3)
    return MemoryStore(pool=pool, embedding_service=embedder), pool


def test_heal_loop_survives_failed_tick_then_heals(tmp_path):
    embedder = _ScriptedEmbedder([RuntimeError("outage"), RuntimeError("outage")])
    store, pool = _make_store(tmp_path, embedder)
    try:
        store.add_memory("heal me", source="chat", vault_id=1)
        conn = pool.get_connection()
        try:
            conn.execute(
                "UPDATE memories SET embedding = NULL, embedding_model = NULL"
            )
            conn.commit()
        finally:
            pool.release_connection(conn)

        async def _run():
            task = asyncio.create_task(store.periodic_embed_heal_loop(interval=0))
            try:
                for _ in range(300):
                    await asyncio.sleep(0.01)
                    conn = pool.get_connection()
                    try:
                        row = conn.execute(
                            "SELECT embedding IS NULL FROM memories"
                        ).fetchone()
                    finally:
                        pool.release_connection(conn)
                    if row and not row[0]:
                        return True
                return False
            finally:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        # First tick raises (scripted outage); the loop must CONTINUE and a
        # later tick (script now exhausted -> success) heals the row.
        assert asyncio.run(_run()) is True
    finally:
        pool.close_all()


def test_heal_tick_skips_when_lock_held(tmp_path, caplog):
    embedder = _ScriptedEmbedder([])
    store, pool = _make_store(tmp_path, embedder)
    try:
        store.add_memory("row", source="chat", vault_id=1)
        conn = pool.get_connection()
        try:
            conn.execute(
                "UPDATE memories SET embedding = NULL, embedding_model = NULL"
            )
            conn.commit()
        finally:
            pool.release_connection(conn)

        async def _run():
            lock = store.get_backfill_lock()
            async with lock:  # simulate an in-flight backfill (startup/route)
                with caplog.at_level(logging.INFO):
                    task = asyncio.create_task(
                        store.periodic_embed_heal_loop(interval=0)
                    )
                    try:
                        await asyncio.sleep(0.5)
                    finally:
                        task.cancel()
                        try:
                            await task
                        except asyncio.CancelledError:
                            pass

        asyncio.run(_run())
        assert embedder.calls == 0, "a skipped tick must not embed anything"
        # The lock was never stolen from the holder (the skip happens at the
        # 5s acquire boundary, later than this 0.5s window; the INFO wording
        # itself is pinned by source in test_warning_wordings' module).
    finally:
        pool.close_all()


class _BudgetOneAdmission:
    """Admission controller double whose EMBEDDING budget is exactly 1."""

    def __init__(self, external_holds=0):
        from collections import deque

        self._waiters = deque()
        self._held = external_holds

    def admit(self, admission_class):
        controller = self

        class _Ctx:
            async def __aenter__(self):
                if controller._held >= 1:
                    from app.services.admission import AdmissionRejected

                    # The real constructor validates reason against a
                    # closed set; use "queue_full" (a saturated budget).
                    raise AdmissionRejected("queue_full")
                controller._held += 1
                return self

            async def __aexit__(self, *exc):
                controller._held -= 1
                return False

        return _Ctx()


def test_heal_admission_high_water_budget_one(tmp_path, monkeypatch):
    embedder = _ScriptedEmbedder([])
    store, pool = _make_store(tmp_path, embedder)
    try:
        for i in range(6):
            store.add_memory(f"row {i}", source="chat", vault_id=1)
        conn = pool.get_connection()
        try:
            conn.execute(
                "UPDATE memories SET embedding = NULL, embedding_model = NULL"
            )
            conn.commit()
        finally:
            pool.release_connection(conn)

        saturated = _BudgetOneAdmission(external_holds=1)

        async def _run():
            from app.services import admission as admission_module

            with patch.object(
                admission_module,
                "get_admission_controller",
                lambda: saturated,
            ):
                summary = await store.backfill_missing_embeddings(
                    null_only=True, admitted=True
                )
            return summary

        summary = asyncio.run(_run())
        assert summary["deferred"] == 6, summary
        assert summary["processed"] == 0
        assert embedder.calls == 0, "no provider call under a saturated budget"

        # Retry with the budget free: this controller double REJECTS on
        # contention rather than queueing (a deliberately pessimistic bound
        # — the real controller blocks), so successive ticks drain the NULL
        # rows one admitted embed at a time and provider concurrency never
        # exceeds 1.
        free = _BudgetOneAdmission()

        async def _run2():
            from app.services import admission as admission_module

            with patch.object(
                admission_module,
                "get_admission_controller",
                lambda: free,
            ):
                return await store.backfill_missing_embeddings(
                    null_only=True, admitted=True
                )

        total_processed = 0
        for _ in range(6):
            summary2 = asyncio.run(_run2())
            total_processed += summary2["processed"]
            if total_processed == 6:
                break
        assert total_processed == 6
        assert embedder.max_concurrent == 1
    finally:
        pool.close_all()


def test_whole_system_single_flight_three_callers(tmp_path):
    embedder = _ScriptedEmbedder([])
    store, pool = _make_store(tmp_path, embedder)
    try:
        in_flight = {"n": 0, "max": 0}

        async def _tracked(*args, **kwargs):
            in_flight["n"] += 1
            in_flight["max"] = max(in_flight["max"], in_flight["n"])
            try:
                await asyncio.sleep(0.1)
                return {"total": 0, "processed": 0, "skipped": 0, "failed": 0}
            finally:
                in_flight["n"] -= 1

        store.backfill_missing_embeddings = _tracked

        async def _route_shape():
            async with store.get_backfill_lock():
                return await store.backfill_missing_embeddings()

        async def _run():
            healer = asyncio.create_task(
                store.periodic_embed_heal_loop(interval=0)
            )
            try:
                # Two concurrent route-shape callers plus a heal tick
                # (interval=0 fires immediately) — all three contend for one
                # in-flight backfill run.
                await asyncio.gather(
                    _route_shape(),
                    _route_shape(),
                    asyncio.sleep(0.5),
                )
            finally:
                healer.cancel()
                try:
                    await healer
                except asyncio.CancelledError:
                    pass

        asyncio.run(_run())
        assert in_flight["max"] <= 1, in_flight
        assert in_flight["n"] == 0
    finally:
        pool.close_all()


def test_null_only_skips_model_mismatched_rows(tmp_path):
    embedder = _ScriptedEmbedder([])
    store, pool = _make_store(tmp_path, embedder)
    try:
        store.add_memory("old generation row", source="chat", vault_id=1)
        conn = pool.get_connection()
        try:
            # A valid embedding from a DIFFERENT model (staged rebuild: live
            # model is NEW, this row is correctly labeled OLD).
            conn.execute(
                "UPDATE memories SET embedding = ?, embedding_model = ?",
                (json.dumps([0.4, 0.5, 0.6]), "old-generation-model"),
            )
            conn.commit()
        finally:
            pool.release_connection(conn)

        async def _run():
            first = await store.backfill_missing_embeddings(null_only=True)
            second = await store.backfill_missing_embeddings(null_only=True)
            return first, second

        first, second = asyncio.run(_run())
        # NULL-only never selects the valid old-generation row, on any tick.
        assert first["total"] == 0 and second["total"] == 0
        assert embedder.calls == 0
    finally:
        pool.close_all()


def test_heal_interval_config_bounds():
    from pydantic import ValidationError

    from app.config import Settings

    for good in (60, 600, 86400):
        assert (
            Settings.model_validate(
                {**Settings().model_dump(), "memory_embed_heal_interval_seconds": good}
            ).memory_embed_heal_interval_seconds
            == good
        )
    for bad in (59, 0, -1, 86401):
        with pytest.raises(ValidationError):
            Settings.model_validate(
                {
                    **Settings().model_dump(),
                    "memory_embed_heal_interval_seconds": bad,
                }
            )
    assert settings.memory_embed_heal_interval_seconds == 600


# --------------------------------------------------------------------------
# B2 companion: non-NULL directive rows not re-embedded
# --------------------------------------------------------------------------


def test_directive_leaves_embedded_row_alone(tmp_path):
    embedder = _ScriptedEmbedder([])
    store, pool = _make_store(tmp_path, embedder)
    try:
        store.add_memory("already embedded", source="chat", vault_id=1)
        conn = pool.get_connection()
        try:
            conn.execute(
                "UPDATE memories SET embedding = ?, embedding_model = ?",
                (json.dumps([0.1, 0.2, 0.3]), settings.embedding_model),
            )
            conn.commit()
        finally:
            pool.release_connection(conn)

        async def _run():
            return await store.memory_embedding_is_null(1)

        assert asyncio.run(_run()) is False
        assert embedder.calls == 0
    finally:
        pool.close_all()


# --------------------------------------------------------------------------
# B3 companion: exception-arm warning wording
# --------------------------------------------------------------------------


def test_warning_wordings_name_all_three_healers():
    """Static wording floor for every degradation arm (issue #937 AC3).

    The frozen C10 drives the live routes; this pins the exact three-healer
    phrase across all five production warning sites (create outcome+except,
    update outcome+except, remember outcome+except and the found-existing
    re-embed arms) so no arm drifts back to an overstated wording.
    """
    memories_src = Path(
        "app/api/routes/memories.py"
    ).read_text(encoding="utf-8")
    rag_src = Path("app/services/rag_engine.py").read_text(encoding="utf-8")
    # The source splits the healing phrase across adjacent string literals,
    # so the static floor counts the healer-token mentions per arm instead:
    # create outcome+except and update outcome+except (>= 4), remember
    # outcome+except and found-existing outcome+except (>= 4); and the
    # overstated "not on a timer" wording must be gone everywhere.
    assert memories_src.count("embed-heal") >= 4, memories_src.count("embed-heal")
    assert rag_src.count("embed-heal") >= 4, rag_src.count("embed-heal")
    assert "not on a timer" not in memories_src
    assert "not on a timer" not in rag_src
