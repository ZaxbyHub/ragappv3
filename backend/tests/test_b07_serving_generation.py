"""Supplementary regression suite for issue #696 (NOT checkpoint-frozen).

The frozen acceptance checks live in ``test_b07_same_dim_staged_rebuild.py``
(C1-C4); this file pins the serving-generation mechanics around them:
gate/healthz admission semantics, the embedding-service serving funnel,
draining-snapshot lifecycle, the R1/R2 completion guards, commit-failure
artifact preservation, and the capability-probe legacy branches that keep
the stub/MagicMock harnesses of existing suites green.
"""

import asyncio
import inspect
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.api.deps import require_model_ready
from app.config import settings
from app.models.database import SQLiteConnectionPool, run_migrations
from app.services.background_tasks import (
    BackgroundProcessor,
    _embedding_identity_changed,
)
from app.services.embeddings import (
    EmbeddingIdentity,
    EmbeddingService,
    resolve_effective_prefixes,
)
from app.services.vector_store import VectorStore

OLD_MODEL = "old-model"
NEW_MODEL = "new-model"
THIRD_MODEL = "third-model"
OLD_PREFIX_HASH = "0123456789abcdef"


def _connect(db_path):
    conn = sqlite3.connect(db_path, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _insert_indexed_file(conn, vault_id, file_path):
    conn.execute(
        "INSERT INTO vaults (id, name) VALUES (?, ?) ON CONFLICT(id) DO NOTHING",
        (vault_id, f"vault-{vault_id}"),
    )
    conn.execute(
        "INSERT INTO files (file_name, file_path, vault_id, status, file_size) "
        "VALUES ('b07-sg.txt', ?, ?, 'indexed', 32)",
        (file_path, vault_id),
    )
    conn.commit()


class _ContractEmbeddingService:
    """Contract-faithful embed_batch fake (bare list on fail_fast=True)."""

    def __init__(self, dim: int):
        self.dim = dim
        self.calls = []

    async def embed_batch(self, texts, batch_size=None, fail_fast=True):
        self.calls.append((tuple(texts), bool(fail_fast)))
        vectors = [[float(i)] * self.dim for i, _ in enumerate(texts)]
        return vectors if fail_fast else (vectors, [])


class _SidecarStore:
    """Stub store whose sidecar identity is configurable (dims equal)."""

    def __init__(self, sidecar_model=OLD_MODEL, live_dim=4):
        self._sidecar_model = sidecar_model
        self._live_dim = live_dim
        self.begin_dimension_rebuild = AsyncMock()
        self.commit_dimension_rebuild = AsyncMock()
        self.abort_dimension_rebuild = AsyncMock()
        self.record_embedding_metadata = AsyncMock()
        self.mark_ready = AsyncMock()
        self.clear_draining_embedding_config = AsyncMock()

    async def get_live_embedding_dim(self):
        return self._live_dim

    async def get_embedding_metadata(self):
        return {
            "embedding_model_id": self._sidecar_model,
            "embedding_dim": self._live_dim,
            "embedding_prefix_hash": OLD_PREFIX_HASH,
        }


class _Recorder:
    def __init__(self, fail_on_call=None, on_call=None):
        self.calls = []
        self._fail_on_call = fail_on_call
        self._on_call = on_call

    async def __call__(self, file_id, file_path, vault_id, **kwargs):
        self.calls.append(kwargs)
        if self._on_call is not None:
            self._on_call(len(self.calls))
        if self._fail_on_call is not None and len(self.calls) >= self._fail_on_call:
            raise RuntimeError(f"injected failure file_id={file_id}")
        return None


@pytest.fixture()
def job_harness(tmp_path):
    """Reindex-job harness with a settings-identity override."""
    db_path = tmp_path / "app.db"
    run_migrations(str(db_path))
    conn = _connect(str(db_path))
    pool = SQLiteConnectionPool(str(db_path), max_size=4)
    processor = BackgroundProcessor(pool=pool, retry_delay=0.05)
    emb = _ContractEmbeddingService(dim=4)
    store = _SidecarStore()
    processor.processor.embedding_service = emb
    processor.processor.vector_store = store
    orig_model = settings.embedding_model
    settings.embedding_model = NEW_MODEL
    _insert_indexed_file(conn, 1, "does/not/exist-sg-a.txt")
    _insert_indexed_file(conn, 1, "does/not/exist-sg-b.txt")
    try:
        yield SimpleNamespace(
            conn=conn, pool=pool, processor=processor, emb=emb, store=store
        )
    finally:
        settings.embedding_model = orig_model
        pool.close_all()
        conn.close()


# ---------------------------------------------------------------------------
# Gate admission semantics (strict singleton-bool probe)
# ---------------------------------------------------------------------------


def _gate_status(store):
    try:
        require_model_ready(store)
        return 200
    except HTTPException as exc:
        return exc.status_code


def test_gate_503_for_magicmock_store_not_ready():
    """MagicMock auto-attributes must NOT read as an open rebuild (the
    strict singleton-bool probe keeps TestRequireModelReady503 green)."""
    mock_vs = MagicMock()
    mock_vs._ready = False
    assert _gate_status(mock_vs) == 503


def test_gate_503_for_truthy_non_bool_flag():
    store = SimpleNamespace(_ready=False, rebuild_in_progress="yes")
    assert _gate_status(store) == 503


def test_gate_admits_only_while_rebuild_open(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "multi_scale_indexing_enabled", False)
    run_migrations(str(tmp_path / "app.db"))
    vs = VectorStore(db_path=tmp_path / "lancedb")
    vs.db = _FakeDB()

    async def _run():
        await vs.init_table(8)
        await vs.mark_ready(False)
        handle = await vs.begin_dimension_rebuild(8)
        try:
            assert _gate_status(vs) == 200
        finally:
            await vs.abort_dimension_rebuild(handle)
        assert _gate_status(vs) == 503
        # A second begin while closed is allowed again (flag cleared).
        handle2 = await vs.begin_dimension_rebuild(8)
        assert _gate_status(vs) == 200
        # commit clears admission at entry as well.
        await vs.commit_dimension_rebuild(handle2)
        assert vs.rebuild_in_progress is False

    asyncio.run(_run())


def test_second_begin_refused_while_rebuild_open(tmp_path, monkeypatch):
    from app.services.vector_store import VectorStoreError

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "multi_scale_indexing_enabled", False)
    run_migrations(str(tmp_path / "app.db"))
    vs = VectorStore(db_path=tmp_path / "lancedb")
    vs.db = _FakeDB()

    async def _run():
        await vs.init_table(8)
        handle = await vs.begin_dimension_rebuild(8)
        try:
            with pytest.raises(VectorStoreError) as excinfo:
                await vs.begin_dimension_rebuild(8)
            assert "already in progress" in str(excinfo.value)
        finally:
            await vs.abort_dimension_rebuild(handle)

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# healthz parity
# ---------------------------------------------------------------------------


def _healthz_body(store):
    from app.main import app

    keys = ("vector_store", "db_pool", "embedding_service", "migrations_ok")
    # Record ABSENCE distinctly from a None value: restoring a previously
    # absent attribute as None would make it merely present-but-None, which
    # defeats opt-in fixtures that install state only when the attribute is
    # missing (conftest.ready_vector_store installs its ready mock only when
    # app.state lacks vector_store — a None left behind turns later co-worker
    # chat requests into 503 "Vector store is not initialized").
    saved = {k: (hasattr(app.state, k), getattr(app.state, k, None)) for k in keys}
    app.state.vector_store = store
    app.state.db_pool = object()
    app.state.embedding_service = object()
    app.state.migrations_ok = True
    client = TestClient(app)
    try:
        resp = client.get("/api/healthz")
        return resp.status_code, resp.json()
    finally:
        for key, (was_present, value) in saved.items():
            if was_present:
                setattr(app.state, key, value)
            elif hasattr(app.state, key):
                delattr(app.state, key)


def test_healthz_warns_instead_of_degrading_during_rebuild():
    store = SimpleNamespace(
        table=object(),
        _ready=False,
        rebuild_in_progress=True,
        _draining_embedding_config=None,
    )
    status, body = _healthz_body(store)
    assert status == 200
    assert "serving previous embedding generation during staged rebuild" in body.get(
        "warnings", []
    )


def test_healthz_still_degrades_when_not_ready_without_rebuild():
    store = SimpleNamespace(table=object(), _ready=False)
    status, body = _healthz_body(store)
    assert status == 503
    assert any("not ready" in issue for issue in body.get("issues", []))


# ---------------------------------------------------------------------------
# Serving-identity funnel on the embedding service
# ---------------------------------------------------------------------------

_LOOPBACK = "http://127.0.0.1:9999/embed"


@pytest.fixture()
def embed_service(monkeypatch):
    """Funnel-only EmbeddingService (no __init__: the SSRF startup guard
    rejects arbitrary URLs outside a real deployment; the funnel tests
    monkeypatch _embed_with_prefix and never touch HTTP)."""
    monkeypatch.setattr(settings, "ollama_embedding_url", _LOOPBACK)
    monkeypatch.setattr(settings, "embedding_model", NEW_MODEL)
    svc = EmbeddingService.__new__(EmbeddingService)
    svc._resolved_cache = None
    svc._serving_identity_provider = None
    return svc


def test_funnel_pins_single_and_passage_not_batch(embed_service, monkeypatch):
    identity = EmbeddingIdentity(
        model=OLD_MODEL,
        url=_LOOPBACK,
        doc_prefix="old-doc:",
        query_prefix="old-query:",
    )

    async def _provider():
        return identity

    recorded = []

    async def _record_embed(text, prefix, config=None):
        recorded.append((config.model, prefix))
        return [0.0]

    embed_service.set_serving_identity_provider(_provider)
    monkeypatch.setattr(embed_service, "_embed_with_prefix", _record_embed)
    asyncio.run(embed_service.embed_single("q"))
    assert recorded[0] == (OLD_MODEL, "old-query:")
    asyncio.run(embed_service.embed_passage("d"))
    assert recorded[1] == (OLD_MODEL, "old-doc:")
    # embed_batch / embed_probe never consult the provider (source contract:
    # the rebuild must embed under the NEW model; health probes hit the
    # CURRENT endpoint).
    batch_src = inspect.getsource(EmbeddingService.embed_batch)
    probe_src = inspect.getsource(EmbeddingService.embed_probe)
    assert "_serving_identity" not in batch_src
    assert "_serving_identity" not in probe_src
    single_src = inspect.getsource(EmbeddingService.embed_single)
    passage_src = inspect.getsource(EmbeddingService.embed_passage)
    assert "_serving_identity" in single_src and "_serving_identity" in passage_src


def test_funnel_falls_back_to_live_settings_on_none_or_failure(embed_service, monkeypatch):
    calls = {"n": 0}

    async def _flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        raise RuntimeError("provider down")

    recorded = []

    async def _record_embed(text, prefix, config=None):
        # config=None means the LIVE-settings path (no identity override).
        recorded.append(config.model if config is not None else "live-settings")
        return [0.0]

    embed_service.set_serving_identity_provider(_flaky)
    monkeypatch.setattr(embed_service, "_embed_with_prefix", _record_embed)
    asyncio.run(embed_service.embed_single("q"))
    asyncio.run(embed_service.embed_single("q"))
    assert recorded == ["live-settings", "live-settings"]


def test_seam_census_every_query_embed_hits_the_funnel():
    """The retrieval query seams must all call embed_single/embed_passage
    (the funnel covers them); asserted structurally over the source."""
    import re

    repo = Path(__file__).resolve().parents[1]
    seams = {
        "app/services/rag_engine.py": ["embed_single(sq)", "embed_single(user_input)"],
        "app/services/memory_store.py": ["embed_passage(text)", "embed_single(query)"],
        "app/services/agentic_tools.py": ["embed_single(query)"],
        "app/services/image_search.py": ["embed_single(text)"],
    }
    for rel, needles in seams.items():
        src = (repo / rel).read_text(encoding="utf-8")
        for needle in needles:
            assert needle in src, f"{rel} lost seam {needle}"
    src = (repo / "app/api/routes/search.py").read_text(encoding="utf-8")
    assert re.search(r"embed_single\((body\.query|query)", src)


# ---------------------------------------------------------------------------
# Prefix resolution (Qwen) and URL-blind residual
# ---------------------------------------------------------------------------


def test_resolve_effective_prefixes_qwen_rule():
    doc, query = resolve_effective_prefixes("Qwen/Qwen3-Embedding-0.6B", "", "")
    assert doc.startswith("Instruct: Represent this technical documentation")
    assert query.startswith("Instruct: Retrieve relevant technical documentation")
    doc2, query2 = resolve_effective_prefixes(NEW_MODEL, "", "")
    assert doc2 == "" and query2 == ""
    doc3, query3 = resolve_effective_prefixes(
        "Qwen/Qwen3-Embedding-0.6B", "custom-doc:", "custom-query:"
    )
    assert doc3 == "custom-doc:" and query3 == "custom-query:"


def test_prior_embedding_config_snapshots_resolved_prefixes(monkeypatch):
    from app.api.routes.settings import (
        _effective_embedding_identity,
        _prior_embedding_config,
    )

    monkeypatch.setattr(settings, "embedding_model", "Qwen/Qwen3-Embedding-0.6B")
    monkeypatch.setattr(settings, "embedding_doc_prefix", "")
    monkeypatch.setattr(settings, "embedding_query_prefix", "")
    config = _prior_embedding_config()
    assert config["model"].lower().startswith("qwen")
    assert config["doc_prefix"].startswith("Instruct: Represent")
    assert config["query_prefix"].startswith("Instruct: Retrieve")


def test_url_only_change_is_identity_invisible(monkeypatch):
    from app.api.routes.settings import _effective_embedding_identity

    monkeypatch.setattr(settings, "embedding_model", NEW_MODEL)
    monkeypatch.setattr(settings, "ollama_embedding_url", "http://old:8080/embed")
    before = _effective_embedding_identity()
    monkeypatch.setattr(settings, "ollama_embedding_url", "http://new:8081/embed")
    assert _effective_embedding_identity() == before
    # And the routing probe reads a URL change as not-an-identity-change: a
    # store whose sidecar equals live settings (model+hash) stays unchanged.
    async def _run():
        store = _SidecarStore(sidecar_model=NEW_MODEL)
        return await _embedding_identity_changed(store)

    assert asyncio.run(_run()) is False


# ---------------------------------------------------------------------------
# Identity-detection probe branches
# ---------------------------------------------------------------------------


def test_identity_probe_legacy_branches():
    import asyncio

    async def _run():
        # Missing get_embedding_metadata -> legacy False (b02/#229 stubs).
        bare = SimpleNamespace(get_live_embedding_dim=AsyncMock(return_value=4))
        assert await _embedding_identity_changed(bare) is False
        # MagicMock auto-attr returns a Mock (non-dict) -> legacy False.
        assert await _embedding_identity_changed(MagicMock()) is False
        # Non-dict dict-less model id -> False.
        empty = _SidecarStore(sidecar_model="")
        assert await _embedding_identity_changed(empty) is False
        # Recorded old model vs live settings -> True (the frozen-fixture path).
        changed = _SidecarStore(sidecar_model=OLD_MODEL)
        assert await _embedding_identity_changed(changed) is True
        return True

    orig = settings.embedding_model
    settings.embedding_model = NEW_MODEL
    try:
        assert asyncio.run(_run())
    finally:
        settings.embedding_model = orig


# ---------------------------------------------------------------------------
# R1 / R2 guards and routing outcomes
# ---------------------------------------------------------------------------


async def test_r1_identity_move_mid_run_aborts_before_commit(job_harness):
    harness = job_harness

    def flip_on_second_call(n):
        if n >= 1:
            settings.embedding_model = THIRD_MODEL

    recorder = _Recorder(on_call=flip_on_second_call)
    harness.processor.processor.process_existing_file = recorder
    status, result, error = await harness.processor._reindex_embed_all(  # noqa: SLF001
        1, vault_id=None
    )
    assert status == "failed"
    assert "identity changed during reindex" in str(error)
    assert harness.store.abort_dimension_rebuild.await_count == 1
    assert harness.store.commit_dimension_rebuild.await_count == 0
    assert harness.store.record_embedding_metadata.await_count == 0
    assert harness.store.mark_ready.await_count == 0


async def test_r1_completion_guard_on_in_place_path(tmp_path):
    db_path = tmp_path / "app.db"
    run_migrations(str(db_path))
    conn = _connect(str(db_path))
    pool = SQLiteConnectionPool(str(db_path), max_size=4)
    processor = BackgroundProcessor(pool=pool, retry_delay=0.05)
    emb = _ContractEmbeddingService(dim=4)
    # Sidecar matches live settings: identity unchanged -> in-place path.
    store = _SidecarStore(sidecar_model=NEW_MODEL)
    processor.processor.embedding_service = emb
    processor.processor.vector_store = store
    orig = settings.embedding_model
    settings.embedding_model = NEW_MODEL
    _insert_indexed_file(conn, 1, "does/not/exist-r1.txt")

    def flip_after_first(n):
        if n >= 1:
            settings.embedding_model = THIRD_MODEL

    processor.processor.process_existing_file = _Recorder(on_call=flip_after_first)
    try:
        status, result, error = await processor._reindex_embed_all(  # noqa: SLF001
            1, vault_id=None
        )
        assert status == "failed"
        assert error is None  # guard outcome is log-guided, not a raw exception
        assert store.record_embedding_metadata.await_count == 0
        assert store.mark_ready.await_count == 0
    finally:
        settings.embedding_model = orig
        pool.close_all()
        conn.close()


async def test_r2_vault_scoped_identity_refusal(job_harness):
    harness = job_harness
    recorder = _Recorder()
    harness.processor.processor.process_existing_file = recorder
    status, result, error = await harness.processor._reindex_embed_all(  # noqa: SLF001
        1, vault_id=1
    )
    assert status == "failed"
    assert "vault-scoped reindex cannot migrate" in str(error)
    assert "full reindex" in str(error)
    assert harness.store.begin_dimension_rebuild.await_count == 0
    assert recorder.calls == []
    assert harness.emb.calls == [(("dimension_probe",), True)]


async def test_r2_vault_refusal_precedes_probe_none_guidance(job_harness):
    """A vault-scoped job with no embedding service still gets the vault
    message (the semantically correct remediation), not the probe guidance."""
    harness = job_harness
    harness.processor.processor.embedding_service = None
    harness.processor.processor.process_existing_file = _Recorder()
    status, _, error = await harness.processor._reindex_embed_all(  # noqa: SLF001
        1, vault_id=1
    )
    assert status == "failed"
    assert "vault-scoped reindex cannot migrate" in str(error)


async def test_probe_none_identity_changed_full_scope_fails_with_guidance(job_harness):
    harness = job_harness
    harness.processor.processor.embedding_service = None
    recorder = _Recorder()
    harness.processor.processor.process_existing_file = recorder
    status, _, error = await harness.processor._reindex_embed_all(  # noqa: SLF001
        1, vault_id=None
    )
    assert status == "failed"
    assert "embedding service is unavailable" in str(error)
    assert harness.store.begin_dimension_rebuild.await_count == 0
    assert recorder.calls == []


async def test_zero_file_identity_change_keeps_completed_early_return(tmp_path):
    db_path = tmp_path / "app.db"
    run_migrations(str(db_path))
    pool = SQLiteConnectionPool(str(db_path), max_size=4)
    processor = BackgroundProcessor(pool=pool, retry_delay=0.05)
    store = _SidecarStore(sidecar_model=OLD_MODEL)
    processor.processor.embedding_service = _ContractEmbeddingService(dim=4)
    processor.processor.vector_store = store
    orig = settings.embedding_model
    settings.embedding_model = NEW_MODEL
    try:
        status, result, error = await processor._reindex_embed_all(  # noqa: SLF001
            1, vault_id=None
        )
        assert (status, result, error) == ("completed", {"processed": 0, "failed": 0}, None)
        assert store.begin_dimension_rebuild.await_count == 0
    finally:
        settings.embedding_model = orig
        pool.close_all()


async def test_staged_path_metadata_failure_fails_job_without_ready(job_harness):
    """Direct staged-path completion ordering (C7's frozen check becomes
    vacuous under the new routing — its vault-scoped flow now trips the R2
    refusal before the metadata write)."""
    harness = job_harness
    # Make the routing dim-triggered (identity unchanged) so the job reaches
    # the staged completion path.
    harness.store._sidecar_model = NEW_MODEL
    harness.store._live_dim = 4
    harness.emb.dim = 6
    harness.store.record_embedding_metadata = AsyncMock(
        side_effect=RuntimeError("Simulated metadata write failure")
    )
    harness.processor.processor.process_existing_file = _Recorder()
    status, result, error = await harness.processor._reindex_embed_all(  # noqa: SLF001
        1, vault_id=None
    )
    assert status == "failed"
    assert error is not None
    assert harness.store.commit_dimension_rebuild.await_count == 1
    assert harness.store.mark_ready.await_count == 0


async def test_commit_failure_preserves_staged_table(job_harness):
    """A failure raised BY commit must not abort — the temp table is the
    recovery artifact (commit_attempted split)."""
    harness = job_harness
    harness.store.commit_dimension_rebuild = AsyncMock(
        side_effect=RuntimeError("simulated mid-swap failure")
    )
    harness.processor.processor.process_existing_file = _Recorder()
    status, _, error = await harness.processor._reindex_embed_all(  # noqa: SLF001
        1, vault_id=None
    )
    assert status == "failed"
    assert harness.store.abort_dimension_rebuild.await_count == 0


def test_commit_failure_preserves_real_temp_table(tmp_path, monkeypatch):
    """Real-store variant: the staged table survives a mid-swap commit
    failure (recovery requires manual promotion before any re-run — the next
    begin's stale-reclaim drops it)."""
    import asyncio

    from app.services.vector_store import DIMENSION_REBUILD_TABLE

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "multi_scale_indexing_enabled", False)
    run_migrations(str(tmp_path / "app.db"))
    vs = VectorStore(db_path=tmp_path / "lancedb")
    fake = _FakeDB()
    # Simulate the post-drop swap failure: dropping the live table raises.
    fake.fail_drop_chunks = True
    vs.db = fake

    async def _run():
        await vs.init_table(8)
        handle = await vs.begin_dimension_rebuild(8)
        with pytest.raises(RuntimeError):
            await vs.commit_dimension_rebuild(handle)
        return DIMENSION_REBUILD_TABLE in await vs.db.table_names()

    assert asyncio.run(_run()) is True


# ---------------------------------------------------------------------------
# Draining-snapshot lifecycle (two-rule: write-if-absent + clear at cutover)
# ---------------------------------------------------------------------------


def _force_sidecar_model(model: str) -> None:
    """Overwrite the sidecar model id directly (sync, test-only)."""
    conn = sqlite3.connect(str(settings.sqlite_path))
    try:
        conn.execute(
            "INSERT OR REPLACE INTO settings_kv (key, value, updated_at) "
            "VALUES ('embedding_model_id', ?, CURRENT_TIMESTAMP)",
            (model,),
        )
        conn.commit()
    finally:
        conn.close()


def test_draining_snapshot_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    run_migrations(str(tmp_path / "app.db"))
    vs = VectorStore(db_path=tmp_path / "lancedb")
    vs.db = _FakeDB()

    async def _run():
        # Write-if-absent: first flip wins.
        assert vs.register_draining_embedding_config(
            {"model": OLD_MODEL, "url": "http://old:8080/embed",
             "doc_prefix": "", "query_prefix": ""}
        ) is True
        # A second identity flip (or a revert) keeps the existing snapshot.
        assert vs.register_draining_embedding_config(
            {"model": NEW_MODEL, "url": "http://new:8081/embed",
             "doc_prefix": "", "query_prefix": ""}
        ) is False
        # Not read while no rebuild is open.
        assert await vs.serving_embedding_identity() is None
        # During a rebuild: the snapshot is served when the sidecar says the
        # live table was built under the SAME model as the snapshot.
        await vs.init_table(8)
        await vs.mark_ready(False)
        await asyncio.to_thread(_force_sidecar_model, OLD_MODEL)
        handle = await vs.begin_dimension_rebuild(8)
        try:
            identity = await vs.serving_embedding_identity()
            assert identity is not None and identity.model == OLD_MODEL
        finally:
            await vs.abort_dimension_rebuild(handle)
        # Clear at cutover: the next rebuild serves None.
        await vs.clear_draining_embedding_config()
        handle2 = await vs.begin_dimension_rebuild(8)
        try:
            assert await vs.serving_embedding_identity() is None
        finally:
            await vs.abort_dimension_rebuild(handle2)

    asyncio.run(_run())


def test_draining_snapshot_stale_model_refuses_pin(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    run_migrations(str(tmp_path / "app.db"))
    vs = VectorStore(db_path=tmp_path / "lancedb")
    vs.db = _FakeDB()

    async def _run():
        vs.register_draining_embedding_config(
            {"model": OLD_MODEL, "url": "http://old:8080/embed",
             "doc_prefix": "", "query_prefix": ""}
        )
        await vs.init_table(8)
        await vs.mark_ready(False)
        # Sidecar recorded under a DIFFERENT model than the snapshot: the
        # snapshot is stale -> refuse to pin (surfaced unpinned path).
        await asyncio.to_thread(_force_sidecar_model, THIRD_MODEL)
        handle = await vs.begin_dimension_rebuild(8)
        try:
            assert await vs.serving_embedding_identity() is None
        finally:
            await vs.abort_dimension_rebuild(handle)

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Minimal fake lancedb (same shape as the frozen file's)
# ---------------------------------------------------------------------------


class _FakeIndex:
    def __init__(self, name, columns=None, index_type=None):
        self.name = name
        self.columns = columns or []
        self.index_type = index_type or ""


class _FakeTable:
    def __init__(self, name, schema=None):
        self.name = name
        self._schema = schema
        self.indices = []

    async def schema(self):
        return self._schema

    async def list_indices(self):
        return list(self.indices)

    async def create_index(self, column=None, config=None, replace=False):
        kind = "FTS" if column == "text" else "IvfPq"
        self.indices.append(_FakeIndex(f"{column}_idx", [column], kind))
        return None

    async def count_rows(self):
        return 0

    async def to_pandas(self):
        import pandas as pd

        return pd.DataFrame()


class _FakeDB:
    def __init__(self):
        self._tables = {}
        self.fail_drop_chunks = False

    async def table_names(self):
        return list(self._tables.keys())

    async def open_table(self, name):
        if name not in self._tables:
            raise RuntimeError(f"table {name} not found")
        return self._tables[name]

    async def drop_table(self, name):
        if name == "chunks" and self.fail_drop_chunks:
            raise RuntimeError("simulated post-drop swap failure")
        self._tables.pop(name, None)

    async def create_table(self, name, schema=None, data=None, mode="create"):
        table = _FakeTable(name, schema)
        self._tables[name] = table
        return table
