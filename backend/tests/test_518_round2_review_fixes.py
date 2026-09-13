"""Round-2 regression pins for the swarm + Copilot review of PR #589 (#518).

Each test names the finding it pins (F-001..F-014 / NEW-* / COPILOT-* from
the 2026-09-13 review round) and fails on the pre-fix head 1451afae.
"""

import asyncio
import json
import os
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.admission import (  # noqa: E402
    AdmissionClass,
    AdmissionController,
    AdmissionStore,
    MemoryAdmissionStore,
    chat_gate_held,
)

ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# F-001 — ADMISSION_DEADLINE_SECONDS="" must not crash Settings at import
# ---------------------------------------------------------------------------


def test_f001_empty_deadline_env_constructs_settings(monkeypatch):
    from app.config import Settings

    monkeypatch.setenv("ADMISSION_DEADLINE_SECONDS", "")
    s = Settings(_env_file=None)
    assert s.admission_deadline_seconds is None, (
        "518-R2 F-001: an empty ADMISSION_DEADLINE_SECONDS env (the compose "
        "long-form injection) must coerce to None, not crash float parsing"
    )


def test_f001_empty_deadline_kwarg_constructs_settings():
    from app.config import Settings

    s = Settings(
        _env_file=None, admission_deadline_seconds=""
    )
    assert s.admission_deadline_seconds is None


def test_f001_compose_uses_short_form_for_typed_deadline():
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "ADMISSION_DEADLINE_SECONDS=${" not in text, (
        "518-R2 F-001: docker-compose.yml must use the short passthrough "
        "form for the typed ADMISSION_DEADLINE_SECONDS key"
    )
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    for line in env_example.splitlines():
        if line.strip().startswith("ADMISSION_DEADLINE_SECONDS="):
            pytest.fail(
                "518-R2 F-001: .env.example ships an ACTIVE empty "
                "ADMISSION_DEADLINE_SECONDS line (cp .env.example .env "
                "must not produce an empty typed value)"
            )


# ---------------------------------------------------------------------------
# F-002 — non-stream chat path must mark the chat gate (no double acquire)
# ---------------------------------------------------------------------------


def _make_request():
    from fastapi import Request

    return Request(
        {
            "type": "http",
            "method": "POST",
            "url": "http://test/api/chat",
            "headers": [],
            "query_string": b"",
            "root_path": "",
            "path": "/api/chat",
        }
    )


async def _call_non_stream_route(chat_module, engine, user=None):
    """Invoke the undecorated non-stream route handler directly."""
    target = getattr(chat_module.chat, "__wrapped__", chat_module.chat)
    body = chat_module.ChatRequest(message="hi")
    return await target(
        _make_request(),
        body,
        engine,
        user or {"id": 1, "username": "u", "email": "u@e", "role": "admin"},
        None,
        "csrf",
        None,
    )


async def test_f002_non_stream_marks_chat_gate():
    """Inside non_stream_chat_response the gate must be held so the engine
    skips its own CHAT acquire (RED on 1451afae: gate never marked)."""
    from app.api.routes import chat as chat_module

    seen = []

    async def fake_response(*args, **kwargs):
        seen.append(chat_gate_held())
        return {"content": "ok", "sources": [], "memories_used": []}

    ctrl = AdmissionController(
        budgets={"chat": 1},
        class_budgets={AdmissionClass.CHAT: "chat"},
        instance_id="r2",
    )
    with (
        patch.object(chat_module, "get_admission_controller", lambda: ctrl),
        patch.object(chat_module, "non_stream_chat_response", fake_response),
    ):
        await _call_non_stream_route(chat_module, object())
    assert seen == [True], (
        "518-R2 F-002: chat_gate_held() was False inside the non-stream "
        "response call — the engine's generation gate would double-acquire "
        f"the CHAT budget (observed {seen})"
    )
    assert await ctrl.store.occupancy("chat") == 0, "slot leaked"


async def test_f002_budget_sized_non_stream_requests_do_not_deadlock():
    """Budget N non-stream requests must all complete: the engine-side gate
    condition (acquire only when the route gate is NOT held) mirrors
    rag_engine.py; pre-fix, budget-sized concurrent requests deadlock."""
    from app.api.routes import chat as chat_module

    ctrl = AdmissionController(
        budgets={"chat": 2},
        class_budgets={AdmissionClass.CHAT: "chat"},
        instance_id="r2b",
    )

    class EngineGate:
        """Replicates the engine's generation-phase admission condition
        against the SAME controller the route uses."""

        async def query(self, *args, **kwargs):
            if not chat_gate_held():
                # Exact production shape (rag_engine generation gate)
                async with ctrl.admit(AdmissionClass.CHAT):
                    await asyncio.sleep(0.01)
            else:
                await asyncio.sleep(0.01)
            yield {"type": "done", "sources": [], "memories_used": []}

    async def fake_response(message, history, engine, **kwargs):
        gen = engine.query(message, history=history)
        async for _ in gen:
            pass
        return {"content": "ok", "sources": [], "memories_used": []}

    with (
        patch.object(chat_module, "get_admission_controller", lambda: ctrl),
        patch.object(chat_module, "non_stream_chat_response", fake_response),
    ):
        results = await asyncio.wait_for(
            asyncio.gather(
                *(_call_non_stream_route(chat_module, EngineGate()) for _ in range(4))
            ),
            timeout=5.0,
        )
    assert all(r["content"] == "ok" for r in results)
    assert await ctrl.store.occupancy("chat") == 0


# ---------------------------------------------------------------------------
# F-003 — live holders renew; only dead holders are swept
# ---------------------------------------------------------------------------


class RefreshRecordingStore(MemoryAdmissionStore):
    def __init__(self):
        super().__init__()
        self.refreshes = 0

    async def refresh(self, key, holder, ttl_seconds):
        self.refreshes += 1
        return await super().refresh(key, holder, ttl_seconds)


async def test_f003_live_holder_renews_past_ttl():
    ctrl = AdmissionController(
        budgets={"chat": 1},
        class_budgets={AdmissionClass.CHAT: "chat"},
        store=RefreshRecordingStore(),
        ttl_seconds=0.09,
        instance_id="r2c",
    )
    assert ctrl.ttl_seconds <= 0.1

    async with ctrl.admit(AdmissionClass.CHAT):
        # Stay admitted well past the TTL: without renewal the 30 s sweep
        # analogue would drop this live holder (F-003).
        await asyncio.sleep(0.16)
        assert await ctrl.store.occupancy("chat") == 1, (
            "518-R2 F-003: a LIVE holder lost its slot to the TTL sweep"
        )
    assert ctrl.store.refreshes >= 2, (
        "518-R2 F-003: no lease renewal heartbeats observed "
        f"({ctrl.store.refreshes})"
    )
    assert await ctrl.store.occupancy("chat") == 0


# ---------------------------------------------------------------------------
# F-004 — poisoned X-Request-ID never reaches outbound headers
# ---------------------------------------------------------------------------


def test_f004_is_safe_request_id_predicate():
    from app.utils.request_context import is_safe_request_id

    assert is_safe_request_id("req-123")
    assert not is_safe_request_id("évil")  # obs-text latin-1 decode
    assert not is_safe_request_id("with space")
    assert not is_safe_request_id("")
    assert not is_safe_request_id("x" * 129)
    assert not is_safe_request_id("tab\tchar")
    assert not is_safe_request_id(None)


async def test_f004_middleware_regenerates_unsafe_request_id():
    from fastapi import FastAPI, Request, Response

    from app.middleware.logging import LoggingMiddleware

    mw = LoggingMiddleware(FastAPI())
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "url": "http://test/api/health",
            "path": "/api/health",
            "root_path": "",
            "headers": [(b"x-request-id", "évil".encode("latin-1"))],
            "query_string": b"",
        }
    )

    async def call_next(req):
        return Response("ok")

    await mw.dispatch(request, call_next)
    rid = request.state.request_id
    assert rid.isascii() and rid.isprintable() and len(rid) <= 128, (
        f"518-R2 F-004: unsafe X-Request-ID propagated: {rid!r}"
    )


async def test_f004_correlation_headers_sanitize_poisoned_id():
    from app.services import telemetry as telemetry_module
    from app.utils.request_context import request_id_var

    telemetry_module.reset_telemetry()
    token = request_id_var.set("évil-\x80")
    try:
        headers = telemetry_module.correlation_headers()
    finally:
        request_id_var.reset(token)
    assert headers, "telemetry enabled: headers expected"
    for name, value in headers.items():
        assert value.isascii(), (
            f"518-R2 F-004: outbound header {name} carries non-ASCII: {value!r}"
        )
    assert headers["X-Request-ID"].startswith("gen-")


# ---------------------------------------------------------------------------
# F-005 — restore rejects escaping paths and unverifiable trees
# ---------------------------------------------------------------------------


class _StaticKeyProvider:
    def __call__(self):
        return (b"k" * 32, "v1")


def _build_backup_set(tmp_path, monkeypatch):
    import sqlite3

    from scripts.backup_set import create_backup_set

    from scripts import backup_set as backup_set_module

    src = tmp_path / "src"
    (src / "lancedb" / "chunks").mkdir(parents=True)
    (src / "lancedb" / "chunks" / "marker").write_bytes(b"lm")
    (src / "vault-7" / "uploads").mkdir(parents=True)
    (src / "vault-7" / "uploads" / "f.txt").write_bytes(b"vault")
    db = tmp_path / "app.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.commit()
    conn.close()

    monkeypatch.setattr(
        backup_set_module, "settings", SimpleNamespace(sqlite_path=str(db))
    )
    out = tmp_path / "set"
    create_backup_set(
        out,
        lancedb_dir=src / "lancedb",
        lancedb_tables={"chunks": _FakeTable()},
        vault_dirs=[src / "vault-7"],
        draft_room_dir=None,
        key_provider=_StaticKeyProvider(),
    )
    return out


class _FakeTable:
    def __init__(self):
        self.tags = []

    def create_tag(self, tag):
        self.tags.append(tag)


def _tamper_manifest(out_dir, mutate):
    path = out_dir / "backup_manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    mutate(manifest)
    path.write_text(json.dumps(manifest), encoding="utf-8")


def test_f005_restore_rejects_escaping_manifest_path(tmp_path, monkeypatch):
    from scripts.restore import RestoreError, restore_backup_set

    out = _build_backup_set(tmp_path, monkeypatch)

    def mutate(m):
        vault = next(i for i in m["items"] if i["kind"] == "vault")
        vault["path"] = "../escape"

    _tamper_manifest(out, mutate)
    with pytest.raises(RestoreError, match="escapes"):
        restore_backup_set(
            out, tmp_path / "dest", key_provider=_StaticKeyProvider()
        )


def test_f005_restore_rejects_absolute_manifest_path(tmp_path, monkeypatch):
    from scripts.restore import RestoreError, restore_backup_set

    out = _build_backup_set(tmp_path, monkeypatch)

    def mutate(m):
        vault = next(i for i in m["items"] if i["kind"] == "vault")
        vault["path"] = str(tmp_path / "elsewhere")

    _tamper_manifest(out, mutate)
    with pytest.raises(RestoreError, match="escapes"):
        restore_backup_set(
            out, tmp_path / "dest", key_provider=_StaticKeyProvider()
        )


def test_f005_restore_rejects_empty_files_map(tmp_path, monkeypatch):
    from scripts.restore import RestoreError, restore_backup_set

    out = _build_backup_set(tmp_path, monkeypatch)

    def mutate(m):
        vault = next(i for i in m["items"] if i["kind"] == "vault")
        vault["files"] = {}

    _tamper_manifest(out, mutate)
    with pytest.raises(RestoreError, match="no file digests"):
        restore_backup_set(
            out, tmp_path / "dest", key_provider=_StaticKeyProvider()
        )


def test_f005_restore_rejects_escaping_files_rel(tmp_path, monkeypatch):
    from scripts.restore import RestoreError, restore_backup_set

    out = _build_backup_set(tmp_path, monkeypatch)

    def mutate(m):
        vault = next(i for i in m["items"] if i["kind"] == "vault")
        first = next(iter(vault["files"]))
        vault["files"]["../outside"] = vault["files"][first]

    _tamper_manifest(out, mutate)
    with pytest.raises(RestoreError, match="escapes the artifact tree"):
        restore_backup_set(
            out, tmp_path / "dest", key_provider=_StaticKeyProvider()
        )


def test_copilot_vault_path_round_trips_under_vaults(tmp_path, monkeypatch):
    """COPILOT-VAULT-PATH: vault artifacts restore under <dest>/vaults/<id>."""
    from scripts.restore import restore_backup_set

    out = _build_backup_set(tmp_path, monkeypatch)
    manifest = json.loads(
        (out / "backup_manifest.json").read_text(encoding="utf-8")
    )
    vault = next(i for i in manifest["items"] if i["kind"] == "vault")
    assert vault["path"] == "vaults/vault-7", (
        "518-R2 COPILOT-VAULT-PATH: manifest vault path must be "
        f"vaults/<id>, got {vault['path']!r}"
    )
    dest = tmp_path / "dest"
    restore_backup_set(out, dest, key_provider=_StaticKeyProvider())
    assert (dest / "vaults" / "vault-7" / "uploads" / "f.txt").read_bytes() == (
        b"vault"
    )


# ---------------------------------------------------------------------------
# NEW-LANCEDB-UNWIRED — the restore CLI actually checks out table tags
# ---------------------------------------------------------------------------


def test_new_lancedb_cli_passes_factory_and_checks_out(tmp_path, monkeypatch):
    from scripts import restore as restore_module

    out = _build_backup_set(tmp_path, monkeypatch)
    manifest = json.loads(
        (out / "backup_manifest.json").read_text(encoding="utf-8")
    )
    lancedb_item = next(i for i in manifest["items"] if i["kind"] == "lancedb")
    expected_tag = lancedb_item["tables"][0]["tag"]

    checkouts = []

    class _FakeTableHandle:
        def checkout(self, tag):
            checkouts.append(tag)

    class _FakeDb:
        def open_table(self, name):
            assert name == "chunks"
            return _FakeTableHandle()

    class _FakeLancedb:
        @staticmethod
        def connect(path):
            assert path.endswith("lancedb"), path
            return _FakeDb()

    fake_module = SimpleNamespace(connect=_FakeLancedb.connect)
    monkeypatch.setitem(sys.modules, "lancedb", fake_module)
    monkeypatch.setattr(
        restore_module, "_default_key_provider", _StaticKeyProvider()
    )
    monkeypatch.setattr(
        sys, "argv", ["restore.py", str(out), "--dest", str(tmp_path / "d")]
    )
    restore_module.main()
    assert checkouts == [expected_tag], (
        "518-R2 NEW-LANCEDB-UNWIRED: the restore CLI never checked out the "
        f"tagged generation (checkouts={checkouts})"
    )


# ---------------------------------------------------------------------------
# F-006 — admission shutdown wired into lifespan AFTER workers drain
# ---------------------------------------------------------------------------


def test_f006_lifespan_wires_admission_shutdown_after_workers():
    src = (ROOT / "backend" / "app" / "lifespan.py").read_text(encoding="utf-8")
    last_stop = src.find("kms_compile_processor.stop()")
    lookup = src.find("get_admission_controller()")
    shutdown_at = src.find("await controller.shutdown()")
    store_close = src.find("await close()")
    assert lookup != -1 and shutdown_at != -1, (
        "518-R2 F-006: lifespan must call admission shutdown"
    )
    assert last_stop < shutdown_at, (
        "518-R2 F-006: admission shutdown must run AFTER background "
        "processors stop (their drains hold background leases)"
    )
    assert store_close != -1 and shutdown_at < store_close, (
        "518-R2 F-006: the shared store connection close must follow the "
        "controller shutdown"
    )


# ---------------------------------------------------------------------------
# F-007 — admission deferral keeps queue.join() bookkeeping balanced
# ---------------------------------------------------------------------------


async def test_f007_admission_deferral_keeps_join_balanced(monkeypatch):
    from app.services import background_tasks as bt

    processor = bt.BackgroundProcessor()
    await processor.start() if hasattr(processor, "start") else None

    ctrl = AdmissionController(
        budgets={"background": 1},
        class_budgets={AdmissionClass.BACKGROUND: "background"},
        queue_max_size=1,
        instance_id="r2d",
    )
    # Saturate the single background slot: the next admit is queue_full.
    holder = ctrl.admit(AdmissionClass.BACKGROUND, foreground=False)
    await holder.__aenter__()

    processed = []

    async def fake_process(task):
        processed.append(task.file_path)

    async def fake_wrapper(task):
        processed.append("wrapped:" + task.file_path)
        processor.queue.task_done()

    monkeypatch.setattr(bt, "get_admission_controller", lambda: ctrl)
    monkeypatch.setattr(processor, "_process_task_wrapper", fake_wrapper)
    monkeypatch.setattr(
        processor, "_mark_task_permanently_failed", lambda *a, **k: None
    )

    item = bt.TaskItem(file_path="doc.pdf", vault_id=1)
    await processor.queue.put(item)

    async def run_loop_until_processed():
        while not processed:
            await asyncio.sleep(0)

    loop_task = asyncio.create_task(processor._worker_loop())
    try:
        # Phase 1: rejection defers the task (task_done + retry scheduler).
        await asyncio.sleep(0.1)
        assert not processed, "unexpectedly processed while saturated"
        # join() must be completable while the item sits in the retry
        # backlog — pre-fix the re-put without task_done wedged it forever.
        join_task = asyncio.create_task(processor.queue.join())
        await asyncio.sleep(0.1)
        assert not join_task.done() or join_task.exception() is None
        # Phase 2: free the slot; the scheduled retry delivers + processes.
        await holder.__aexit__(None, None, None)
        await asyncio.wait_for(run_loop_until_processed(), timeout=5.0)
        assert any(p.startswith("wrapped:") for p in processed), processed
        await asyncio.wait_for(join_task, timeout=5.0)
    finally:
        loop_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await loop_task
        try:
            if processor._retry_scheduler_task is not None:
                processor._retry_scheduler_task.cancel()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# F-008 — multiprocess /metrics reachable via telemetry_registry_dir
# ---------------------------------------------------------------------------


def test_f008_registry_dir_from_settings(tmp_path, monkeypatch):
    from app.config import settings
    from app.services import telemetry as telemetry_module

    telemetry_module.reset_telemetry()
    monkeypatch.setattr(
        settings, "telemetry_registry_dir", str(tmp_path / "shards")
    )
    t = telemetry_module.init_telemetry()
    assert t.registry_dir == tmp_path / "shards", (
        "518-R2 F-008: init_telemetry ignored the settings-configured "
        "registry dir — multiprocess /metrics stays unreachable"
    )
    explicit = telemetry_module.init_telemetry(registry_dir=tmp_path / "x")
    assert explicit.registry_dir == tmp_path / "x"
    telemetry_module.reset_telemetry()


def test_f008_registry_dir_is_a_settings_field():
    from app.config import Settings

    assert "telemetry_registry_dir" in Settings.model_fields, (
        "518-R2 F-008: telemetry_registry_dir missing from Settings"
    )


# ---------------------------------------------------------------------------
# Docs pins — F-009..F-013 (operator-visible honesty claims)
# ---------------------------------------------------------------------------


def test_f013_release_note_carries_migration_sections():
    note = (
        ROOT
        / "docs"
        / "releases"
        / "pending"
        / "518-workstream-e3-admission-telemetry-backup.md"
    ).read_text(encoding="utf-8")
    for section in (
        "## What changed",
        "## Migration",
        "## Breaking changes",
        "## Known caveats",
    ):
        assert section in note, f"518-R2 F-013: release note lacks {section}"


def test_f009_through_f012_operations_doc_honesty_claims():
    text = (ROOT / "docs" / "operations.md").read_text(encoding="utf-8")
    pins = {
        "F-009 plaintext scope": "Encryption scope",
        "F-009 aes key": "AES_KEY",
        "F-010 both ceilings": "raise both together",
        "F-011 new caps": "NEW",
        "F-012 same port": "unauthenticated",
        "F-003 renewal": "renews",
        "F-006 restart": "RESTART",
    }
    for label, needle in pins.items():
        assert needle in text, (
            f"518-R2 {label}: operations.md lost the honesty claim {needle!r}"
        )


def test_degraded_transition_logs_once(caplog):
    ctrl = AdmissionController(
        budgets={"chat": 1},
        class_budgets={AdmissionClass.CHAT: "chat"},
        instance_id="r2e",
    )
    with caplog.at_level("WARNING", logger="app.services.admission"):
        ctrl._mark_degraded()
        ctrl._mark_degraded()
    warnings = [
        r for r in caplog.records if "admission store degraded" in r.message
    ]
    assert len(warnings) == 1, (
        "518-R2 LOW: degraded transition must log exactly once per "
        f"controller (got {len(warnings)})"
    )
    assert ctrl.degraded
