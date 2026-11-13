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

# The exception-arm route drives mention csrf only via the dependency
# override; enforcement is not under test here (repo classifier contract).
CSRF_TEST_POLICY = "naive"

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
        {"queue_waits": {"c": [True]}},  # bool queue sample (r2 TI-3)
        {"chat_turns": 1.5},  # float counter (r2 TI-3)
        {"evaluator_fail_opens": {"exception": -5}},  # negative count (r2 TI-3)
        {"provider_calls": {"p": "not-a-dict"}},  # non-dict map (r2 TI-3)
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


def test_binary_corrupt_shard_skipped_without_500(tmp_path, caplog):
    """Implementation-review F1: non-UTF-8 shard bytes raise
    UnicodeDecodeError (a ValueError subclass) at the READ leg — the skip
    must happen there, never propagate out of metrics_text. RED at the
    pre-fix head (executed by the reviewer), GREEN after the read-leg fix.
    """
    import logging as _logging

    binary = tmp_path / "telemetry-00000000-9-cafe06.json"
    binary.write_bytes(b"\xff\xfeinvalid")
    good = tmp_path / "telemetry-00000000-9-cafe07.json"
    good.write_text(json.dumps({"chat_turns": 4}), encoding="utf-8")
    tel = Telemetry(enabled=True, registry_dir=tmp_path)
    with caplog.at_level(_logging.WARNING, logger="app.services.telemetry"):
        text = tel.metrics_text()
    assert _sample(text, telemetry_module.METRIC_CHAT_TURNS) == 4
    assert any(
        "unreadable" in r.getMessage()
        for r in caplog.records
        if r.name == "app.services.telemetry"
    )


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
    # merge-side (final-critic finding 1): each SHARD's list is capped at
    # merge-ENTRY, but the CROSS-SHARD aggregate keeps every shard's
    # retained window — a merged-list trim would let the last-sorting shard
    # monopolize the queue-wait average.
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


def test_two_capped_shards_keep_true_cross_shard_mean(tmp_path):
    """Final-critic finding 1 (executed probes): worker A holds 200 samples
    of 10.0s, worker B holds 200 samples of 0.0s — the true mean is 5.0. A
    merged-list trim reported 0.0 or 10.0 (whichever shard sorted last
    won); per-shard capping keeps the honest aggregate.
    """
    tel = Telemetry(enabled=True, registry_dir=tmp_path)
    for _ in range(QUEUE_HISTORY_CAP):
        tel.record_queue_wait("cls", 10.0, depth=1)
    tel.record_chat_turn("t")  # write-through flush persists the histories
    other = tmp_path / "telemetry-00000000-1-zzzzzzzz.json"
    other.write_text(
        json.dumps(
            {
                "queue_waits": {"cls": [0.0] * QUEUE_HISTORY_CAP},
                "queue_depths": {"cls": [0] * QUEUE_HISTORY_CAP},
            }
        ),
        encoding="utf-8",
    )
    scraper = Telemetry(enabled=True, registry_dir=tmp_path)
    text = scraper.metrics_text()
    for line in text.splitlines():
        if line.startswith("ragapp_queue_wait_seconds{"):
            avg = float(line.rsplit(" ", 1)[1])
            assert avg == pytest.approx(5.0), line
            break
    else:
        raise AssertionError("queue wait gauge missing")


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
                        # Hold past the heal loop's bounded 5s acquire so the
                        # skip path (TimeoutError -> INFO -> continue) really
                        # executes (final-critic finding 5).
                        await asyncio.sleep(6.5)
                    finally:
                        task.cancel()
                        try:
                            await task
                        except asyncio.CancelledError:
                            pass

        asyncio.run(_run())
        assert embedder.calls == 0, "a skipped tick must not embed anything"
        assert any(
            "skipped: backfill in flight" in r.getMessage()
            for r in caplog.records
        ), "the skip INFO must actually fire"
    finally:
        pool.close_all()


class _BudgetOneAdmission:
    """Admission controller double whose EMBEDDING budget is exactly 1."""

    def __init__(self, external_holds=0):
        from collections import deque

        self._waiters = deque()
        self._held = external_holds

    def admit(self, admission_class, *, foreground: bool = True):
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

    Final-critic finding 2: a token count alone is too weak — the
    BEHAVIORAL drives in test_exception_arm_wordings_name_the_healers are
    the load-bearing fix (each raised arm must log its own three-healer
    message, asserted per logger and per message). This static floor stays
    as the cheap tripwire: every degradation arm's literal carries the
    periodic-healer token (create outcome+except, update outcome+except in
    memories.py; remember outcome+except + found-existing outcome+except
    in rag_engine.py => >= 4 each), and the overstated wording is gone.
    """
    memories_src = Path(
        "app/api/routes/memories.py"
    ).read_text(encoding="utf-8")
    rag_src = Path("app/services/rag_engine.py").read_text(encoding="utf-8")
    assert memories_src.count("embed-heal") >= 4, memories_src.count("embed-heal")
    assert rag_src.count("embed-heal") >= 4, rag_src.count("embed-heal")
    assert "not on a timer" not in memories_src
    assert "not on a timer" not in rag_src


def test_exception_arm_wordings_name_the_healers(tmp_path, caplog):
    """Final-critic findings 2 + round-2 finding 1 (both executed): a
    reverted failure-arm wording survived the whole battery. Every RAISED
    exception arm is driven with a raising embed_and_store and its message
    asserted MESSAGE-SPECIFICALLY per logger: the update-route arm
    ("Could not recompute embedding"), the create-route arm ("after create
    failed"), and the remember-directive newly-inserted arm ("after
    remember-directive failed"). An embedder outage alone does NOT reach
    these arms (it is converted to the "failed" OUTCOME by
    _embed_text_with_outcome), so the drive replaces embed_and_store
    itself — the arms' real trigger.
    """
    import logging as _logging

    phrase = (
        "healed by the startup backfill task, the periodic embed-heal "
        "task, or the superadmin backfill route"
    )

    class _RaisingEmbedder:
        async def embed_passage(self, text):
            raise RuntimeError("provider exploded")

    # The route's EXCEPTION arm fires only when embed_and_store itself
    # raises (an embedder outage is converted to the "failed" OUTCOME by
    # _embed_text_with_outcome's best-effort contract) — so the drive
    # replaces the store's embed_and_store with a raising coroutine, which
    # is precisely the arm's trigger.
    async def _raising_embed_and_store(memory_id, content):
        raise RuntimeError("embed_and_store exploded")

    # -- update-route exception arm via the app (a04 _RouteEnv pattern)
    from _a04_env import RouteEnv, app  # noqa: F401  (app import first)
    from _db_pool import SimpleConnectionPool  # noqa: F401

    from app.models.database import SQLiteConnectionPool, init_db, run_migrations

    base = tmp_path / "exc"
    db_path = str(base / "routes.db")
    init_db(db_path)
    run_migrations(db_path)
    store_pool = SQLiteConnectionPool(db_path, max_size=2)
    from app.services.memory_store import MemoryStore

    store = MemoryStore(pool=store_pool, embedding_service=_RaisingEmbedder())
    store.embed_and_store = _raising_embed_and_store
    pool = SimpleConnectionPool(db_path)
    from fastapi.testclient import TestClient

    client = TestClient(app)
    from app.api.deps import (
        get_current_active_user,
        get_db,
        get_evaluate_policy,
        get_memory_store,
    )
    from app.security import csrf_protect

    originals = {}
    try:
        originals[get_db] = app.dependency_overrides.get(get_db)
        originals[get_memory_store] = app.dependency_overrides.get(get_memory_store)
        originals[get_current_active_user] = app.dependency_overrides.get(
            get_current_active_user
        )
        originals[get_evaluate_policy] = app.dependency_overrides.get(
            get_evaluate_policy
        )
        app.dependency_overrides[get_db] = lambda: pool.get_connection()
        app.dependency_overrides[get_memory_store] = lambda: store
        app.dependency_overrides[get_current_active_user] = lambda: {
            "id": 1,
            "role": "admin",
        }
        async def _allow(user, resource, rid, action):
            return True

        app.dependency_overrides[get_evaluate_policy] = lambda: _allow
        # Seed a row via the store directly (create arm fires its own
        # warnings; the update arm is the one under drive).
        record = store.add_memory(
            "exception arm wording row", source="user input", vault_id=1
        )
        with caplog.at_level(_logging.WARNING):
            resp = client.put(
                f"/api/memories/{record.id}",
                json={"content": "exception arm wording row CHANGED"},
            )
            assert resp.status_code == 200, resp.text
    finally:
        for dep, orig in originals.items():
            if orig is not None:
                app.dependency_overrides[dep] = orig
            else:
                app.dependency_overrides.pop(dep, None)
        pool.close_all()
        store_pool.close_all()

    update_exc = [
        r.getMessage()
        for r in caplog.records
        if r.name == "app.api.routes.memories"
        and "Could not recompute embedding" in r.getMessage()
    ]
    assert update_exc, "update exception arm did not log"
    assert any(phrase in m for m in update_exc), update_exc[0][:120]


def test_exception_arm_wordings_create_and_remember(tmp_path, caplog):
    """Final-critic round-2 finding 1 (executed): reverting the REMEMBER
    exception-arm wording survived all 52 tests. These drives pin the
    create-route and remember-directive exception arms message-specifically
    with a raising embed_and_store on a real RAGEngine / real store.
    """
    import logging as _logging

    phrase = (
        "healed by the startup backfill task, the periodic embed-heal "
        "task, or the superadmin backfill route"
    )
    from unittest.mock import MagicMock, patch

    from app.models.database import SQLiteConnectionPool, init_db, run_migrations
    from app.services import rag_engine as rag_engine_module
    from app.services.memory_store import MemoryStore

    class _SilentEmbedder:
        async def embed_passage(self, text):
            return [0.1, 0.2, 0.3]

    db_path = str(tmp_path / "arms.db")
    init_db(db_path)
    run_migrations(db_path)
    pool = SQLiteConnectionPool(db_path, max_size=3)
    try:
        # -- remember-directive newly-inserted exception arm
        rem_store = MemoryStore(pool=pool, embedding_service=_SilentEmbedder())

        async def _raising_remember_embed(memory_id, content):
            raise RuntimeError("embed exploded")

        rem_store.embed_and_store = _raising_remember_embed
        engine = rag_engine_module.RAGEngine(
            embedding_service=MagicMock(),
            vector_store=MagicMock(),
            memory_store=rem_store,
            llm_client=MagicMock(),
            db_path=db_path,
        )

        class _Permissive:
            def admit(self, admission_class):
                class _Ctx:
                    async def __aenter__(self):
                        return self

                    async def __aexit__(self, *exc):
                        return False

                return _Ctx()

        async def _drive():
            chunks = []
            with patch.object(
                rag_engine_module,
                "get_admission_controller",
                lambda: _Permissive(),
            ):
                async for chunk in engine.query(
                    "remember that the round3 arm wording fires",
                    [],
                    stream=True,
                    vault_id=1,
                    can_write_memory=True,
                ):
                    chunks.append(chunk)
            return chunks

        with caplog.at_level(_logging.WARNING):
            chunks = asyncio.run(_drive())
        assert chunks, "harness: remember-directive produced no chunks"
        remember_exc = [
            r.getMessage()
            for r in caplog.records
            if r.name == "app.services.rag_engine"
            and "after remember-directive failed" in r.getMessage()
        ]
        assert remember_exc, "remember exception arm did not log"
        assert any(
            phrase in m for m in remember_exc
        ), remember_exc[0][:120]

        # -- create-route exception arm
        caplog.clear()
        create_store = MemoryStore(pool=pool, embedding_service=_SilentEmbedder())

        async def _raising_create_embed(memory_id, content):
            raise RuntimeError("embed exploded")

        create_store.embed_and_store = _raising_create_embed
        from _db_pool import SimpleConnectionPool
        from fastapi.testclient import TestClient

        from app.main import app

        simple_pool = SimpleConnectionPool(db_path)
        from app.api.deps import (
            get_current_active_user,
            get_db,
            get_evaluate_policy,
            get_memory_store,
        )

        originals = {}
        try:
            for dep in (get_db, get_memory_store, get_current_active_user,
                        get_evaluate_policy):
                originals[dep] = app.dependency_overrides.get(dep)

            async def _allow(user, resource, rid, action):
                return True

            app.dependency_overrides[get_db] = lambda: simple_pool.get_connection()
            app.dependency_overrides[get_memory_store] = lambda: create_store
            app.dependency_overrides[get_current_active_user] = lambda: {
                "id": 1,
                "role": "admin",
            }
            app.dependency_overrides[get_evaluate_policy] = lambda: _allow
            with caplog.at_level(_logging.WARNING):
                resp = TestClient(app).post(
                    "/api/memories",
                    json={"content": "round3 create arm wording row", "vault_id": 1},
                )
                assert resp.status_code == 200, resp.text
        finally:
            for dep, orig in originals.items():
                if orig is not None:
                    app.dependency_overrides[dep] = orig
                else:
                    app.dependency_overrides.pop(dep, None)
            simple_pool.close_all()

        create_exc = [
            r.getMessage()
            for r in caplog.records
            if r.name == "app.api.routes.memories"
            and "after create failed" in r.getMessage()
        ]
        assert create_exc, "create exception arm did not log"
        assert any(phrase in m for m in create_exc), create_exc[0][:120]
    finally:
        pool.close_all()


def test_outcome_arm_wordings_message_specific(tmp_path, caplog):
    """Delta-review probe 4c (executed): reverting the create-route OUTCOME
    arm wording survived all 53 tests (C10 pools create+update messages
    under one substring; the exception-arm drives do not reach outcome
    arms). This matrix isolates EVERY degradation arm message-specifically:
    create outcome, update outcome, remember newly-inserted outcome,
    remember found-existing outcome, and the found-existing exception arm.
    Outcome arms are reached with an embedder whose embed_passage returns
    None (provider None -> outcome "failed" per _embed_text_with_outcome),
    NOT with a raise.
    """
    import logging as _logging
    from unittest.mock import MagicMock, patch

    from app.models.database import SQLiteConnectionPool, init_db, run_migrations
    from app.services import rag_engine as rag_engine_module
    from app.services.memory_store import MemoryStore

    phrase = (
        "healed by the startup backfill task, the periodic embed-heal "
        "task, or the superadmin backfill route"
    )

    class _NoneEmbedder:
        """Provider that returns None -> outcome 'failed' (outcome arm)."""

        async def embed_passage(self, text):
            return None

    db_path = str(tmp_path / "outcome-arms.db")
    init_db(db_path)
    run_migrations(db_path)
    pool = SQLiteConnectionPool(db_path, max_size=3)

    class _Permissive:
        def admit(self, admission_class):
            class _Ctx:
                async def __aenter__(self):
                    return self

                async def __aexit__(self, *exc):
                    return False

            return _Ctx()

    def _route_msgs():
        return [
            r.getMessage()
            for r in caplog.records
            if r.name == "app.api.routes.memories"
            and r.levelno >= _logging.WARNING
        ]

    def _engine_msgs():
        return [
            r.getMessage()
            for r in caplog.records
            if r.name == "app.services.rag_engine"
            and r.levelno >= _logging.WARNING
        ]

    try:
        from _db_pool import SimpleConnectionPool
        from fastapi.testclient import TestClient

        from app.main import app

        store = MemoryStore(pool=pool, embedding_service=_NoneEmbedder())
        simple_pool = SimpleConnectionPool(db_path)
        from app.api.deps import (
            get_current_active_user,
            get_db,
            get_evaluate_policy,
            get_memory_store,
        )

        async def _allow(user, resource, rid, action):
            return True

        originals = {}
        try:
            for dep in (get_db, get_memory_store, get_current_active_user,
                        get_evaluate_policy):
                originals[dep] = app.dependency_overrides.get(dep)
            app.dependency_overrides[get_db] = lambda: simple_pool.get_connection()
            app.dependency_overrides[get_memory_store] = lambda: store
            app.dependency_overrides[get_current_active_user] = lambda: {
                "id": 1,
                "role": "admin",
            }
            app.dependency_overrides[get_evaluate_policy] = lambda: _allow
            client = TestClient(app)

            # -- create outcome arm + update outcome arm via the routes
            with caplog.at_level(_logging.WARNING):
                created = client.post(
                    "/api/memories",
                    json={"content": "outcome arms row", "vault_id": 1},
                )
                assert created.status_code == 200, created.text
                mid = int(created.json()["id"])
                updated = client.put(
                    f"/api/memories/{mid}",
                    json={"content": "outcome arms row CHANGED"},
                )
                assert updated.status_code == 200, updated.text
            msgs = _route_msgs()
            create_outcome = [
                m for m in msgs if "Memory stored without embedding" in m
            ]
            assert create_outcome, "create outcome arm did not log"
            assert any(phrase in m for m in create_outcome), create_outcome[0][:110]
            update_outcome = [
                m for m in msgs
                if "Memory update left the row without an embedding" in m
            ]
            assert update_outcome, "update outcome arm did not log"
            assert any(phrase in m for m in update_outcome), update_outcome[0][:110]
        finally:
            for dep, orig in originals.items():
                if orig is not None:
                    app.dependency_overrides[dep] = orig
                else:
                    app.dependency_overrides.pop(dep, None)
            simple_pool.close_all()

        # -- remember newly-inserted outcome arm (real engine, None provider)
        caplog.clear()
        rem_store = MemoryStore(pool=pool, embedding_service=_NoneEmbedder())
        engine = rag_engine_module.RAGEngine(
            embedding_service=MagicMock(),
            vector_store=MagicMock(),
            memory_store=rem_store,
            llm_client=MagicMock(),
            db_path=db_path,
        )

        async def _drive_remember(query):
            chunks = []
            with patch.object(
                rag_engine_module,
                "get_admission_controller",
                lambda: _Permissive(),
            ):
                async for chunk in engine.query(
                    query, [], stream=True, vault_id=1, can_write_memory=True
                ):
                    chunks.append(chunk)
            return chunks

        with caplog.at_level(_logging.WARNING):
            chunks = asyncio.run(
                _drive_remember("remember that the outcome arms probe fires")
            )
        assert chunks, "harness: remember-directive produced no chunks"
        msgs = _engine_msgs()
        new_outcome = [
            m for m in msgs if "Memory stored without embedding" in m
        ]
        assert new_outcome, "remember newly-inserted outcome arm did not log"
        assert any(phrase in m for m in new_outcome), new_outcome[0][:110]

        # -- found-existing outcome arm (re-send the same directive)
        caplog.clear()
        with caplog.at_level(_logging.WARNING):
            chunks = asyncio.run(
                _drive_remember("remember that the outcome arms probe fires")
            )
        assert chunks, "harness: re-sent directive produced no chunks"
        msgs = _engine_msgs()
        resent_outcome = [
            m for m in msgs if "Memory re-sent without embedding" in m
        ]
        assert resent_outcome, "found-existing outcome arm did not log"
        assert any(phrase in m for m in resent_outcome), resent_outcome[0][:110]

        # -- found-existing exception arm (raising embed_and_store)
        caplog.clear()

        async def _raising_embed(memory_id, content):
            raise RuntimeError("embed exploded")

        rem_store.embed_and_store = _raising_embed
        with caplog.at_level(_logging.WARNING):
            chunks = asyncio.run(
                _drive_remember("remember that the outcome arms probe fires")
            )
        assert chunks, "harness: exception-arm directive produced no chunks"
        msgs = _engine_msgs()
        found_exc = [
            m
            for m in msgs
            if "Memory re-embed after re-sent remember-directive failed" in m
        ]
        assert found_exc, "found-existing exception arm did not log"
        assert any(phrase in m for m in found_exc), found_exc[0][:110]
    finally:
        pool.close_all()


def test_deeply_nested_and_bigint_shards_skip_with_200(tmp_path, caplog):
    """Swarm-review L2R-1 + L2R-2 (both executed at HEAD and base):
    a shard whose JSON nests deeply raises RecursionError out of
    json.loads (RuntimeError subclass, missed by the old ValueError leg),
    and a shard carrying an arbitrarily large int sample raises
    OverflowError out of math.isfinite — both previously escaped
    metrics_text as a 500. After the fix both are skipped with the
    rate-limited WARNING and the exposition still serves the good shard.
    """
    import logging as _logging

    deep = tmp_path / "telemetry-00000000-1-deep0001.json"
    deep.write_text("[" * 1500 + "]" * 1500, encoding="utf-8")
    bigint = tmp_path / "telemetry-00000000-1-big00001.json"
    bigint.write_text(
        json.dumps({"queue_waits": {"x": [10 ** 400]}}), encoding="utf-8"
    )
    good = tmp_path / "telemetry-00000000-1-good0001.json"
    good.write_text(json.dumps({"chat_turns": 7}), encoding="utf-8")
    tel = Telemetry(enabled=True, registry_dir=tmp_path)
    with caplog.at_level(_logging.WARNING, logger="app.services.telemetry"):
        text = tel.metrics_text()
    assert _sample(text, telemetry_module.METRIC_CHAT_TURNS) == 7
    msgs = [r.getMessage() for r in caplog.records]
    assert any("not valid JSON" in m for m in msgs), msgs[:2]
    assert any("failed validation" in m for m in msgs), msgs[:2]


def test_sum_overflow_shards_skip_with_200(tmp_path, caplog):
    """Final-critic delta finding 1 (executed at 61fce0bd and 202e284c):
    two 10**308 int samples each convert to a finite float (so the L2R-2
    isfinite guard passes them), but their int SUM plus any float in the
    merged list overflows at avg = sum(waits)/len(waits) -> /metrics 500.
    The _MAX_QUEUE_SAMPLE ceiling rejects each sample at validation, so
    the hostile shard skips with the shape WARNING and the good shard
    still serves.
    """
    import logging as _logging

    hostile = tmp_path / "telemetry-00000000-1-ovf00001.json"
    hostile.write_text(
        json.dumps({"queue_waits": {"x": [10 ** 308, 10 ** 308, 0.5]}}),
        encoding="utf-8",
    )
    good = tmp_path / "telemetry-00000000-1-good0002.json"
    good.write_text(json.dumps({"chat_turns": 9}), encoding="utf-8")
    tel = Telemetry(enabled=True, registry_dir=tmp_path)
    with caplog.at_level(_logging.WARNING, logger="app.services.telemetry"):
        text = tel.metrics_text()
    assert _sample(text, telemetry_module.METRIC_CHAT_TURNS) == 9
    assert any(
        "failed validation" in r.getMessage()
        for r in caplog.records
        if r.name == "app.services.telemetry"
    )


# ==========================================================================
# r2 swarm-pr-review feedback round (FB-001..FB-011) + external-review and
# final-critic round-2 deltas (FB-012..FB-018) — regression pins
# ==========================================================================

from pathlib import Path as _Path

_REPO_ROOT = _Path(__file__).resolve().parents[1]


def test_pid_alive_never_raises_on_oversized_pid(monkeypatch):
    """FB-001 leg 1 (r2 review F-001 = external review F-01, critic-confirmed
    blocker): on POSIX os.kill() converts the pid via _Py_PARSE_PID and
    raises OverflowError (an ArithmeticError, NOT an OSError) above INT_MAX
    — the exception escaped the prune's except-OSError guard and 500ed
    unauthenticated /metrics on every scrape while the poison shard existed.
    _pid_alive must therefore NEVER raise: an unrepresentable pid is treated
    as alive (retention over deletion). Simulated POSIX leg (a Windows box
    cannot execute the real os.kill conversion); no Path objects are touched
    inside the patched window (os.name patching would break pathlib's
    flavor selection mid-test).
    """

    def _posix_kill(pid, sig):
        if pid > telemetry_module._MAX_PID:
            raise OverflowError("Python int too large to convert to C long")
        raise ProcessLookupError(pid)

    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(os, "kill", _posix_kill)
    # Oversized pid: caught defensively -> alive (conservative retention).
    assert telemetry_module._pid_alive(2**40) is True
    # A real-range dead pid still maps to dead.
    assert telemetry_module._pid_alive(2**31 - 5) is False


def test_prune_never_probes_oversized_pid(tmp_path, monkeypatch):
    """FB-001 leg 2: the prune's name gate must never probe a pid segment
    above _MAX_PID (int() is arbitrary precision, so a 20+ digit pid part
    passes the digit checks; probing it is what raised on POSIX). Retained
    shards are the conservative direction. Discriminating on every
    platform: with the _MAX_PID gate removed, the probe spy records the
    oversized pid and the assertion fails.
    """
    hostile_pid = 2**40
    hostile = tmp_path / (
        f"telemetry-{telemetry_module._HOST_TAG}-{hostile_pid}-0ff00001.json"
    )
    # Empty contents: retention and merge are independent — this shard is
    # valid-but-empty, so the count assertion below isolates the good shard.
    hostile.write_text(json.dumps({}), encoding="utf-8")
    good = tmp_path / "telemetry-00000000-1-good0002.json"
    good.write_text(json.dumps({"chat_turns": 9}), encoding="utf-8")
    os.utime(hostile, (time.time() - 7200, time.time() - 7200))

    probed = []
    real_alive = telemetry_module._pid_alive

    def _spy(pid):
        probed.append(pid)
        return real_alive(pid)

    monkeypatch.setattr(telemetry_module, "_pid_alive", _spy)
    tel = Telemetry(enabled=True, registry_dir=tmp_path)
    text = tel.metrics_text()
    assert _sample(text, telemetry_module.METRIC_CHAT_TURNS) == 9
    assert hostile.exists(), (
        "an unrepresentable-pid shard must be retained (unknown ownership)"
    )
    assert probed == [], (
        f"the prune must never probe an oversized pid (probed: {probed})"
    )


def test_over_ceiling_counter_shard_skips(tmp_path, caplog):
    """FB-007 (r2 review parity fix, user-approved; also closes external
    review F-03's int-to-str 500): a counter above the magnitude ceiling is
    a hostile shape — without the gate a 10**401 counter renders verbatim
    and a float64 scraper errors the WHOLE scrape (200 preserved app-side).
    Isolated dir so the rendered digits cannot hide inside a merged total
    (the first draft of this pin survived the ceiling mutant because
    11 + 10**401 shifted the digit string — lesson recorded).
    """
    import logging as _logging

    hostile = tmp_path / "telemetry-00000000-7-bigc0001.json"
    hostile.write_text(
        json.dumps({"chat_turns": 10 ** 401}), encoding="utf-8"
    )
    tel = Telemetry(enabled=True, registry_dir=tmp_path)
    with caplog.at_level(_logging.WARNING, logger="app.services.telemetry"):
        text = tel.metrics_text()
    msgs = [r.getMessage() for r in caplog.records
            if r.name == "app.services.telemetry"]
    assert any("failed validation" in m for m in msgs), msgs[:3]
    assert str(10 ** 401) not in text, (
        "an over-ceiling counter must never render into the exposition"
    )


def test_unallowlisted_outcome_shard_skips(tmp_path, caplog):
    """FB-008 (r2 review parity fix, user-approved): an unallowlisted
    provider-outcome key is a hostile shape — the write side rejects it, so
    a shard carrying one must skip with the shape WARNING and never merge a
    fabricated series into the exposition.
    """
    import logging as _logging

    fabricated = tmp_path / "telemetry-00000000-7-fabc0002.json"
    fabricated.write_text(
        json.dumps({"provider_calls": {"ghost": {"fabricated_outcome": 7}}}),
        encoding="utf-8",
    )
    tel = Telemetry(enabled=True, registry_dir=tmp_path)
    with caplog.at_level(_logging.WARNING, logger="app.services.telemetry"):
        text = tel.metrics_text()
    msgs = [r.getMessage() for r in caplog.records
            if r.name == "app.services.telemetry"]
    assert any("failed validation" in m for m in msgs), msgs[:3]
    assert "fabricated_outcome" not in text, (
        "an unallowlisted outcome must never render into the exposition"
    )


def test_windows_liveness_never_uses_os_kill(monkeypatch):
    """FB-003 (r2 review F-003 = external review TINF-001, HIGH_IMPACT test
    gap): the nt liveness leg must use OpenProcess and NEVER os.kill
    (TerminateProcess would kill the probed worker). Portable simulation:
    force the nt branch with a stubbed kernel32 and plant an os.kill
    tripwire — reintroducing os.kill on the nt path fails this test on ANY
    platform, including CI's ubuntu runners.
    """
    import ctypes

    def _kill_tripwire(*args, **kwargs):
        raise AssertionError(
            "os.kill was called on the Windows liveness path — the #936 "
            "contract forbids os.kill on Windows (TerminateProcess kills "
            "the probed worker)"
        )

    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(os, "kill", _kill_tripwire)

    class _FakeKernel32:
        def __init__(self):
            self.OpenProcess = lambda access, inherit, pid: None
            self.CloseHandle = lambda handle: 1

    # raising=False (final critic round 2, FB-014): ctypes.WinDLL and
    # ctypes.get_last_error exist ONLY under `if _os.name == "nt"` inside
    # ctypes itself, so on the ubuntu CI runners these attributes are
    # absent and a default setattr raises AttributeError at setup — the
    # test must CREATE the missing attributes, not just replace them.
    monkeypatch.setattr(
        ctypes, "WinDLL", lambda *a, **k: _FakeKernel32(), raising=False
    )
    monkeypatch.setattr(
        ctypes, "get_last_error", lambda: 87, raising=False
    )

    assert telemetry_module._pid_alive(999999) is False
    assert telemetry_module._pid_alive(os.getpid()) is True


def test_warn_ledger_bounded_and_pruned_shard_forgotten(tmp_path, caplog):
    """FB-011 (r2 review RP-2/PO-2): the warning-suppression ledger must be
    bounded and must drop a pruned shard's keys instead of keeping the name
    immortal in the map.
    """
    import logging as _logging

    tel = Telemetry(enabled=True, registry_dir=tmp_path)
    # Cap: fill the ledger past _WARN_STATE_MAX via _rate_limited_warning.
    with caplog.at_level(_logging.WARNING, logger="app.services.telemetry"):
        for i in range(telemetry_module._WARN_STATE_MAX + 8):
            tel._rate_limited_warning(f"shape:shard-{i}.json", "w %d", i)
    assert len(tel._warn_state) <= telemetry_module._WARN_STATE_MAX

    # Prune-coupled eviction: warn for a shard, then forget it.
    tel._rate_limited_warning("read:gone.json", "w")
    assert "read:gone.json" in tel._warn_state
    tel._forget_shard_warnings("gone.json")
    assert "read:gone.json" not in tel._warn_state
    assert "parse:gone.json" not in tel._warn_state
    assert "shape:gone.json" not in tel._warn_state


def test_skip_warning_rate_limited_across_scrapes(tmp_path, caplog):
    """FB-010/TF-4: the read-side skip-WARNING rate limiter's suppression
    half — the same hostile shard scraped repeatedly must warn only once
    inside the interval (previously only the flush-side limiter was pinned;
    disabling the suppression half kept the whole suite green).
    """
    import logging as _logging

    hostile = tmp_path / "telemetry-00000000-5-supr0001.json"
    hostile.write_text(
        json.dumps({"queue_waits": {"c": "nope"}}), encoding="utf-8"
    )
    tel = Telemetry(enabled=True, registry_dir=tmp_path)
    with caplog.at_level(_logging.WARNING, logger="app.services.telemetry"):
        tel.metrics_text()
        tel.metrics_text()
        tel.metrics_text()
    shape_warns = [
        r
        for r in caplog.records
        if r.name == "app.services.telemetry"
        and "failed validation" in r.getMessage()
    ]
    assert len(shape_warns) == 1, (
        f"expected exactly 1 rate-limited WARNING, got {len(shape_warns)}"
    )


def test_embed_semaphore_reused_per_loop(tmp_path):
    """FB-010/TF-6/TI-2: the store-level embed semaphore must be reused on
    the same loop (minting a fresh semaphore per call kept the suite green
    via the 296 constructor-bound assert).
    """

    async def _main():
        embedder = _ScriptedEmbedder([])
        store, pool = _make_store(tmp_path, embedder)
        try:
            s1 = store._embed_batch_semaphore()
            s2 = store._embed_batch_semaphore()
            return s1, s2
        finally:
            pool.close_all()

    s1, s2 = asyncio.run(_main())
    assert s1 is s2


def test_heal_loop_call_kwargs_pinned(tmp_path):
    """FB-002 + FB-005 (r2 review TF-2 HIGH_IMPACT + IA-1): the heal loop's
    scheduler seam must pass null_only=True, admitted=True and the per-tick
    row cap (dropping any of them previously kept the whole suite green,
    re-opening the staged-rebuild re-embed storm and the #939 budget
    bypass), and the admitted arm must take the lease on the BACKGROUND
    lane like every other background processor.
    """

    async def _main():
        embedder = _ScriptedEmbedder([])
        store, pool = _make_store(tmp_path, embedder)
        calls = []

        async def _spy(**kwargs):
            calls.append(kwargs)
            return {"total": 0, "processed": 0, "skipped": 0, "failed": 0}

        orig = store.backfill_missing_embeddings
        store.backfill_missing_embeddings = _spy
        task = asyncio.create_task(store.periodic_embed_heal_loop(interval=0))
        try:
            for _ in range(200):
                await asyncio.sleep(0.01)
                if calls:
                    return calls
            return calls
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            store.backfill_missing_embeddings = orig
            pool.close_all()

    calls = asyncio.run(_main())
    assert calls, "the heal loop must call backfill_missing_embeddings"
    assert calls[0].get("null_only") is True, calls[0]
    assert calls[0].get("admitted") is True, calls[0]
    assert calls[0].get("max_rows") == 200, calls[0]
    # Line-shape pin (Stage B reviewer: a bare substring check was
    # satisfied by the comment's own phrase "passes foreground=False
    # here", so deleting the kwarg kept the pin green) — only the actual
    # kwarg line "foreground=False," matches.
    import re as _re
    ms_src = (_REPO_ROOT / "app" / "services" / "memory_store.py").read_text(
        encoding="utf-8"
    )
    marker = "Background-class lane (r2 review F-005)"
    assert marker in ms_src
    admit_window = ms_src[ms_src.index(marker):]
    assert _re.search(r"^\s*foreground=False,\s*$", admit_window[:1200], _re.M), (
        "the heal tick's admitted arm must pass foreground=False on the "
        "admit() call itself, not only in a comment"
    )


def test_heal_tick_row_cap(tmp_path):
    """FB-006 (r2 review F-006): max_rows bounds one run's work — rows
    beyond the cap stay NULL and are picked up by later ticks, which bounds
    how long the single-flight lock is held per tick.
    """
    embedder = _ScriptedEmbedder([])
    store, pool = _make_store(tmp_path, embedder)
    try:
        for i in range(3):
            store.add_memory(f"row {i}", source="chat", vault_id=1)
        conn = pool.get_connection()
        try:
            conn.execute(
                "UPDATE memories SET embedding = NULL, embedding_model = NULL"
            )
            conn.commit()
        finally:
            pool.release_connection(conn)

        async def _run():
            return await store.backfill_missing_embeddings(
                null_only=True, admitted=False, max_rows=2
            )

        summary = asyncio.run(_run())
        assert summary["total"] == 2, summary
        assert summary["processed"] == 2, summary
        conn = pool.get_connection()
        try:
            null_count = conn.execute(
                "SELECT COUNT(*) FROM memories WHERE embedding IS NULL"
            ).fetchone()[0]
        finally:
            pool.release_connection(conn)
        assert null_count == 1, (
            "the row beyond the cap must stay NULL for later ticks"
        )
    finally:
        pool.close_all()


def test_heal_rotation_reaches_rows_behind_persistent_failures(tmp_path):
    """FB-015 (final critic round 2): a plain prefix cap lets 200
    persistently failing NULL rows monopolize every tick — a healthy row
    behind them would NEVER be attempted (critic-executed probe: three
    ticks attempted the same 200 ids). The rotating window must advance so
    the healthy row is reached on a later tick.
    """
    embedder = _ScriptedEmbedder([])
    store, pool = _make_store(tmp_path, embedder)
    try:
        failing = []
        healthy = None
        for i in range(201):
            rec = store.add_memory(
                "healthy" if i == 200 else f"failing {i}",
                source="chat",
                vault_id=1,
            )
            if i == 200:
                healthy = rec.id
            else:
                failing.append(rec.id)
        conn = pool.get_connection()
        try:
            conn.execute(
                "UPDATE memories SET embedding = NULL, embedding_model = NULL"
            )
            conn.commit()
        finally:
            pool.release_connection(conn)
        failing_ids = set(failing)

        orig_embed = store.embed_and_store

        async def _failing_embed_for_prefix(mid, text):
            if mid in failing_ids:
                raise RuntimeError("persistent provider failure")
            return await orig_embed(mid, text)

        store.embed_and_store = _failing_embed_for_prefix

        async def _run_ticks(ticks):
            for _ in range(ticks):
                await store.backfill_missing_embeddings(
                    null_only=True, admitted=False, max_rows=200
                )
            conn = pool.get_connection()
            try:
                row = conn.execute(
                    "SELECT embedding IS NULL FROM memories WHERE id = ?",
                    (healthy,),
                ).fetchone()
            finally:
                pool.release_connection(conn)
            return bool(row and not row[0])

        # Tick 1 covers the failing prefix; tick 2's window starts at the
        # cursor (offset 200) and reaches the healthy row.
        healed = asyncio.run(_run_ticks(2))
        store.embed_and_store = orig_embed
        assert healed is True, (
            "the rotating window must reach rows behind persistently "
            "failing ones within two ticks"
        )
    finally:
        pool.close_all()


def test_deferred_landed_write_counted_processed(tmp_path):
    """FB-009 (r2 review CS-3/CON-4, probe-proven): asyncio.timeout can
    expire while the to_thread DB write is still in flight — the worker
    thread commits after the awaiting coroutine was cancelled, so the row
    is EMBEDDED even though the coroutine saw the timeout. The deferred
    arm must re-check the row instead of counting a landed write as
    deferred (and the 'a later run retries it' log must stay true).
    """
    from app.config import settings as _settings

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

        orig_budget = _settings.memory_embed_budget_seconds
        orig_embed = store.embed_and_store
        _settings.memory_embed_budget_seconds = 0.1

        async def _landed_write_embed(mid, text):
            # Mimic the real embed_and_store shape: the DB write runs on a
            # worker thread and LANDS, then the coroutine is still awaiting
            # (provider latency) when the budget expires.
            await asyncio.to_thread(
                store._store_embedding,
                mid,
                [0.1, 0.2, 0.3],
                text,
                _settings.embedding_model,
            )
            await asyncio.sleep(0.5)
            return "stored"

        store.embed_and_store = _landed_write_embed
        try:

            async def _run():
                return await store.backfill_missing_embeddings(
                    null_only=True, admitted=True
                )

            summary = asyncio.run(_run())
        finally:
            _settings.memory_embed_budget_seconds = orig_budget
            store.embed_and_store = orig_embed
        assert summary["processed"] == 1, summary
        assert summary["deferred"] == 0, summary
    finally:
        pool.close_all()


def test_resend_branch_embed_is_admitted_and_budgeted():
    """FB-004 (r2 review F-004 = external review A-06/CON-005): the re-send
    directive's NULL-embed branch must run inside the admit(EMBEDDING) +
    budget-window pattern (a bare inline embed previously pinned the SSE
    content frame and a CHAT admission slot for up to the provider's 60s
    timeout). Static tripwire over the branch the way the AC3 wording
    tripwire pins message shape.
    """
    import re as _re

    src = (
        _REPO_ROOT / "app" / "services" / "rag_engine.py"
    ).read_text(encoding="utf-8")
    marker = "Admitted + budgeted (r2 review F-004)"
    assert marker in src, "the re-send branch lost its admitted-embed shape"
    window = src[src.index(marker):]
    branch = window[: window.index("yield {")]
    assert _re.search(r"asyncio\.timeout\(", branch), branch[:400]
    assert "AdmissionClass.EMBEDDING" in branch
    assert "deferred:budget" in branch and "deferred:admission" in branch


def test_lifespan_heal_task_wiring_pinned():
    """FB-010/TF-3: the production activation of the healer (lifespan
    creating the periodic task from the settings knob and cancelling it at
    shutdown) must stay wired — deleting the create_task call previously
    kept the whole suite green because no test enters the app lifespan.
    Static tripwire: each mutation (create -> None, cancel removed) breaks
    its assertion.
    """
    import re as _re

    src = (_REPO_ROOT / "app" / "lifespan.py").read_text(encoding="utf-8")
    assert _re.search(
        r"memory_embed_heal_task\s*=\s*asyncio\.create_task", src
    ), "lifespan no longer creates the periodic embed-heal task"
    assert _re.search(r"memory_embed_heal_task\.cancel\(\)", src), (
        "lifespan no longer cancels the periodic embed-heal task"
    )
    assert "memory_embed_heal_interval_seconds" in src, (
        "the heal task must be scheduled from the settings knob"
    )


def test_lone_surrogate_key_shard_skips_with_200(tmp_path, caplog):
    """FB-012 (external review F-02, critic-confirmed): a lone surrogate in
    a label key parses from JSON, passes naive str checks, and then raises
    UnicodeEncodeError at RESPONSE ENCODING time — 500ing the
    unauthenticated /metrics scrape and falsifying the release note's
    never-500 claim. The validator must reject unencodable label keys so
    the shard skips with a WARNING and the scrape stays 200 (executed probe
    at head pre-fix: _valid_shard returned (True, '') and response encoding
    raised).
    """
    import logging as _logging

    surrogate_key = "\ud800"
    hostile = tmp_path / "telemetry-00000000-3-surr0001.json"
    hostile.write_text(
        json.dumps({"provider_calls": {surrogate_key: {"ok": 1}}}),
        encoding="utf-8",
    )
    good = tmp_path / "telemetry-00000000-3-surr0002.json"
    good.write_text(json.dumps({"chat_turns": 4}), encoding="utf-8")
    tel = Telemetry(enabled=True, registry_dir=tmp_path)
    with caplog.at_level(_logging.WARNING, logger="app.services.telemetry"):
        text = tel.metrics_text()
    assert _sample(text, telemetry_module.METRIC_CHAT_TURNS) == 4
    msgs = [r.getMessage() for r in caplog.records
            if r.name == "app.services.telemetry"]
    assert any("failed validation" in m for m in msgs), msgs[:3]
    assert surrogate_key not in text


def test_heal_loop_survives_failed_tick_then_heals(tmp_path, caplog):
    """FB-010/TF-7 = external review F-05/TEST-004 (probe-proven theater):
    the previous version scripted a PROVIDER outage, but
    _embed_text_with_outcome swallows every provider exception into a
    "failed" outcome — the tick completed normally and the loop's keep-alive
    except arm never executed; a `raise`-after-warning mutant kept 46
    green. Drive a REAL tick exception instead (backfill itself raises,
    e.g. a DB outage) and assert BOTH the keep-alive WARNING and a later
    tick healing the row.
    """
    import logging as _logging

    embedder = _ScriptedEmbedder([])
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

        orig_backfill = store.backfill_missing_embeddings
        state = {"failed_once": False}

        async def _flaky_backfill(**kwargs):
            if not state["failed_once"]:
                state["failed_once"] = True
                raise RuntimeError("simulated tick failure (db outage)")
            return await orig_backfill(**kwargs)

        store.backfill_missing_embeddings = _flaky_backfill

        async def _run():
            task = asyncio.create_task(
                store.periodic_embed_heal_loop(interval=0)
            )
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

        with caplog.at_level(
            _logging.WARNING, logger="app.services.memory_store"
        ):
            healed = asyncio.run(_run())
        store.backfill_missing_embeddings = orig_backfill
        assert healed is True, "the loop must survive the failing tick and heal"
        assert any(
            "Periodic memory embed-heal failed" in r.getMessage()
            for r in caplog.records
            if r.name == "app.services.memory_store"
        ), "the keep-alive WARNING arm must actually execute"
    finally:
        pool.close_all()


def test_two_instances_flush_failures_sum(tmp_path):
    # Recovered-sibling exact count (external review F-04, TEST-001): a
    # sibling that failed N flushes and then RECOVERED persists its N, and
    # the scraper must expose exactly that sibling contribution. The old
    # `>= 1` assertion was satisfied by the scraper's own failing flush, so
    # a mutant dropping persisted sibling totals kept every test green.
    b = Telemetry(enabled=True, registry_dir=tmp_path)
    b._shard_flush_failures = 2
    b.record_chat_turn("b")  # flush succeeds -> persists shard_flush_failures=2
    scraper = Telemetry(enabled=True, registry_dir=tmp_path)
    text = scraper.metrics_text()
    assert _sample(text, METRIC_SHARD_FLUSH_FAILURES) == 2
