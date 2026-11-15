"""G04B (issues #736/#833) ingest-gating follow-through — the upload/scan
route gate, the DocumentProcessor write-seam backstop, the epoch-guarded
completion lift, and the production l2 threshold leg.

Names are contractual (do not rename). Harness patterns are reused VERBATIM
from the sibling suites — ``test_g04_embedding_readiness_and_metric.py``
(``_isolated_env``, ``_reset_pools``, ``_app_state_store``,
``_make_real_lancedb_store``, ``_rebind_async_handles``, the recording store
and fake embedder, ``_drain_queue``), ``test_integration.py``
(``setup_app_state`` + TestClient), ``test_embedding_model_versioning.py``
(the reindex-completion processor + pool-wrapper pattern), and
``test_b07_same_dim_staged_rebuild.py`` (staged-target reindex writes).

The 16 checks:

1.  ``test_upload_document_root_alias_gated_when_not_ready`` — POST
    /documents (root alias) after an identity flip answers 503 with the
    mismatch detail (``require_ingest_ready`` wired on the alias).
2.  ``test_upload_refused_during_staged_rebuild`` — both upload aliases on a
    REAL store with ``rebuild_in_progress=True`` (and ``_ready=True``) answer
    503 "Document reindex in progress".
3.  ``test_scan_route_gated_when_not_ready`` — POST /documents/scan with
    ``_ready=False`` answers 503 (setup_app_state/TestClient harness).
4.  ``test_post_change_scan_writes_zero_vectors`` — FileWatcher.scan_once
    discovers a new file with the gate down, the drained processor settles
    the row, and the live table gains ZERO vectors.
5-7. ``test_backstop_*`` — an ordinary queued upload processed with the gate
    closed (by either flag combination) settles to the stable
    ``EMBEDDING_MODEL_MISMATCH:`` error with zero vectors written.
8.  ``test_process_file_legacy_path_gated`` — the file_id=None worker
    selection (process_file) is gated at the same write seam.
9.  ``test_staged_reindex_writes_continue_while_not_ready`` (PRESERVING) —
    reindex-context work with a staged vector_target is exempt.
10. ``test_gate_down_all_vault_reindex_recovers`` (PRESERVING) — the all-vault
    reindex records metadata and lifts a down gate.
11. ``test_scoped_reindex_gate_down_skips_record_and_ready`` — vault-scoped
    job with the gate down skips record/mark_ready and reports
    ``vault_scoped_gate_down``.
12. ``test_scoped_reindex_gate_up_runs_full_sequence`` (PRESERVING) — with
    the gate up the scoped job still runs the full completion sequence.
13. ``test_invalidation_during_metadata_await_blocks_lift`` — a REAL
    settings-save invalidation landing during the completion's
    ``record_embedding_metadata`` await bumps the epoch and suppresses the
    lift (``invalidated_during_completion``).
14. ``test_final_recheck_boundary_race_closed`` — NON-REENTRANT orchestration
    around the short lift critical section: an invalidation arriving from
    OUTSIDE the guard at the final-recheck->mark_ready boundary never leaves
    the store ready-over-mismatch.
15. ``test_production_path_l2_threshold_keeps_centre`` — through the REAL
    RAGEngine sync path, ``l2`` rescales the raw threshold x2.0 (centre and
    upper band kept, beyond-bound dropped) while cosine keeps 0.61 / drops
    0.8 at the same raw threshold.
16. ``test_stable_reason_format_pinned`` — the gate error settles to the
    pinned ``EMBEDDING_MODEL_MISMATCH:`` message.

Offline and deterministic: every embedding call is faked with the in-repo
fake services; all state lives in per-test temp dirs; every settings
singleton mutation is restored in ``finally`` blocks.
"""

import asyncio
import contextlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

CSRF_TEST_POLICY = "naive"  # harness sends no token header; declared for tests/conftest.py

# lancedb / pyarrow are REQUIRED for these tests (real-store checks) — import
# them early, exactly like test_g04_embedding_readiness_and_metric.py.
import lancedb  # noqa: E402
import pyarrow as pa  # noqa: E402

# Stub missing optional dependencies (same defensive pattern as test_settings.py)
try:
    from unstructured.partition.auto import partition
except ImportError:
    import types
    _unstructured = types.ModuleType("unstructured")
    _unstructured.__path__ = []
    _unstructured.partition = types.ModuleType("unstructured.partition")
    _unstructured.partition.__path__ = []
    _unstructured.partition.auto = types.ModuleType("unstructured.partition.auto")
    _unstructured.partition.auto.partition = lambda *args, **kwargs: []
    _unstructured.chunking = types.ModuleType("unstructured.chunking")
    _unstructured.chunking.__path__ = []
    _unstructured.chunking.title = types.ModuleType("unstructured.chunking.title")
    _unstructured.chunking.title.chunk_by_title = lambda *args, **kwargs: []
    _unstructured.documents = types.ModuleType("unstructured.documents")
    _unstructured.documents.__path__ = []
    _unstructured.documents.elements = types.ModuleType("unstructured.documents.elements")
    _unstructured.documents.elements.Element = type("Element", (), {})
    sys.modules["unstructured"] = _unstructured
    sys.modules["unstructured.partition"] = _unstructured.partition
    sys.modules["unstructured.partition.auto"] = _unstructured.partition.auto
    sys.modules["unstructured.chunking"] = _unstructured.chunking
    sys.modules["unstructured.chunking.title"] = _unstructured.chunking.title
    sys.modules["unstructured.documents"] = _unstructured.documents
    sys.modules["unstructured.documents.elements"] = _unstructured.documents.elements

from _db_pool import SimpleConnectionPool  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from test_integration import (  # noqa: E402
    FakeLLMClient,
    FakeMemoryStore,
    setup_app_state,
)

from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402
from app.services.background_tasks import BackgroundProcessor  # noqa: E402
from app.services.document_processor import (  # noqa: E402
    INGEST_ERROR_EMBEDDING_MISMATCH,
    IngestGateClosedError,
    classify_ingest_error,
    format_ingest_error,
)
from app.services.vector_store import VectorStore  # noqa: E402

_VAULT_ID = 1        # default vault for the upload/scan legs
_VAULT_SCOPE = 2     # vault covered by the scoped-reindex completion legs
_EMB_DIM = 768       # matches _G04EmbeddingService below

_MISMATCH_DETAIL = (
    "Embedding model mismatch — admin reindex required before ingesting documents"
)
_REBUILD_DETAIL = "Document reindex in progress — retry when it completes"

_SQL_CONTENT = (
    b"CREATE TABLE g04b_ingest_probe (\n"
    b"    id INTEGER PRIMARY KEY,\n"
    b"    marker TEXT NOT NULL\n"
    b");\n"
)


# ---------------------------------------------------------------------------
# Shared helpers (patterns copied verbatim from
# test_g04_embedding_readiness_and_metric.py, test_integration.py and
# test_embedding_model_versioning.py — no new harness styles invented).
# ---------------------------------------------------------------------------

def _reset_pools() -> None:
    """Close and forget every cached SQLite pool (versioning-suite pattern)."""
    from app.models.database import _pool_cache, _pool_cache_lock

    with _pool_cache_lock:
        for _path, _pool in list(_pool_cache.items()):
            _pool.close_all()
        _pool_cache.clear()


async def _rebind_async_handles(vs, uri: str):
    """Rebind vs to lancedb's ASYNC connection API (g04 sibling pattern):
    production awaits ``table.schema()`` / ``db.table_names()`` and the sync
    connect() handles cannot be awaited."""
    vs.db = await lancedb.connect_async(uri)
    vs.table = await vs.db.open_table("chunks")


def _make_real_lancedb_store(tmp_dir: str, dim: int = _EMB_DIM):
    """Real VectorStore over a real LanceDB table on the current
    settings.lancedb_path (g04 sibling pattern; ``dim`` matches the fake
    embedder so a gate-removed mutant would really write vectors)."""
    lancedb_path = settings.lancedb_path
    lancedb_path.mkdir(parents=True, exist_ok=True)

    schema = pa.schema([
        ("id", pa.string()),
        ("text", pa.string()),
        ("file_id", pa.string()),
        ("vault_id", pa.string()),
        ("chunk_index", pa.int32()),
        ("embedding", pa.list_(pa.float32(), dim)),
    ])
    db = lancedb.connect(str(lancedb_path))
    table = db.create_table("chunks", schema=schema, exist_ok=True)

    vs = VectorStore()
    vs.db = db
    vs.table = table
    return vs, db, table


@contextlib.contextmanager
def _settings_snapshot():
    """Snapshot the whole settings singleton and restore it on exit (g04
    ``_isolated_env`` snapshot loop, lifted so every test can use it)."""
    snapshot = {
        name: getattr(settings, name) for name in type(settings).model_fields
    }
    try:
        yield
    finally:
        for _name, _value in snapshot.items():
            setattr(settings, _name, _value)


@contextlib.contextmanager
def _isolated_env():
    """Temp data_dir + fresh DB + snapshotted settings singleton, restored
    on exit (verbatim from the g04 sibling file)."""
    with _settings_snapshot():
        tmp_dir = tempfile.mkdtemp(prefix="g04b_")
        _reset_pools()
        settings.data_dir = Path(tmp_dir)
        from app.models.database import init_db, run_migrations

        db_path = str(settings.sqlite_path)
        init_db(db_path)
        run_migrations(db_path)
        try:
            yield tmp_dir, db_path
        finally:
            _reset_pools()
            shutil.rmtree(tmp_dir, ignore_errors=True)


@contextlib.contextmanager
def _app_state_store(store):
    """Install ``store`` as the live app.state.vector_store, restore after
    (g04 sibling pattern)."""
    orig = getattr(app.state, "vector_store", None)
    had = hasattr(app.state, "vector_store")
    app.state.vector_store = store
    try:
        yield
    finally:
        if had:
            app.state.vector_store = orig
        else:
            try:
                del app.state.vector_store
            except AttributeError:
                pass


class _PoolConnectionWrapper:
    """Expose SimpleConnectionPool.get_connection() as the sync/async context
    managers BackgroundProcessor expects (versioning-suite pattern)."""

    def __init__(self, pool):
        self._pool = pool

    def connection(self):
        return contextlib.closing(self._pool.get_connection())

    def connection_async(self):
        return _AsyncClosingConnection(self._pool.get_connection())


class _AsyncClosingConnection:
    """Async mirror of contextlib.closing (versioning-suite pattern)."""

    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        self._conn.close()
        return False


class _G04EmbeddingService:
    """Embedding fake matching the REAL ``embed_batch`` call contract
    (fail_fast=False returns (embeddings, failed_indices)) — the g04 sibling
    / issue #258 WorkerEmbeddingService pattern."""

    def __init__(self):
        self.dim = _EMB_DIM
        self.batch_calls = 0

    async def embed_single(self, text: str):
        return [0.1] * self.dim

    async def embed_batch(self, texts, batch_size=None, fail_fast=True):
        self.batch_calls += 1
        if fail_fast:
            return [[0.1] * self.dim for _ in texts]
        return ([[0.1] * self.dim for _ in texts], [])


class _G04RecordingVectorStore:
    """Recording store implementing the REAL VectorStore call surface the
    ingest path uses (g04 sibling pattern; ``add_chunks_calls`` additionally
    records the per-call kwargs so a staged-target write is observable),
    plus the readiness/draining surface so the settings-save flip behaves
    exactly as it does on a real store."""

    def __init__(self):
        self.stored_chunks = []
        self.add_chunks_calls = []
        self._ready = True
        self.rebuild_in_progress = False
        self.draining_configs = []

    # -- ingest surface -------------------------------------------------
    async def init_table(self, dimension, **kwargs):
        return None

    async def add_chunks(self, records, **kwargs):
        self.add_chunks_calls.append((len(records), kwargs))
        self.stored_chunks.extend(records)
        return None

    async def delete_old_generation_by_file(self, file_id, generation, **kwargs):
        return 0

    async def delete_by_file(self, file_id, **kwargs):
        keep = [r for r in self.stored_chunks if r.get("file_id") != file_id]
        removed = len(self.stored_chunks) - len(keep)
        self.stored_chunks = keep
        return removed

    async def count_by_file(self, file_id, **kwargs):
        return sum(1 for r in self.stored_chunks if r.get("file_id") == file_id)

    # -- readiness / identity surface (#695 / #696 hooks) ----------------
    async def mark_ready(self, ready: bool = True) -> None:
        self._ready = ready

    def register_draining_embedding_config(self, config: dict) -> bool:
        self.draining_configs.append(config)
        return True

    async def clear_draining_embedding_config(self) -> None:
        return None

    async def get_live_embedding_dim(self):
        return None

    async def get_embedding_metadata(self):
        return {
            "embedding_model_id": None,
            "embedding_dim": None,
            "embedding_prefix_hash": None,
        }

    def _compute_embedding_prefix_hash(self):
        return "g04b-prefix-hash"

    # -- retrieval/lifecycle no-ops --------------------------------------
    async def search(self, embedding, limit=10, filter_expr=None, vault_id=None, **kwargs):
        return []

    def connect(self):
        return None

    def close(self):
        return None

    def get_fts_exceptions(self):
        return 0

    async def get_chunks_by_uid(self, chunk_uids):
        return []


async def _drain_queue(bp, timeout: float = 120.0) -> None:
    """Run the REAL worker loop until the ingestion queue is drained
    (issue #258 / g04 sibling pattern)."""
    worker = asyncio.create_task(bp._worker_loop())
    try:
        await asyncio.wait_for(bp.queue.join(), timeout=timeout)
    finally:
        bp.shutdown_event.set()
        await asyncio.wait_for(worker, timeout=10.0)


def _prior_config() -> dict:
    """The pre-save embedding config snapshot the REAL settings-save hook
    receives (g04 AC7 shape)."""
    return {
        "model": str(settings.embedding_model or ""),
        "url": "",
        "doc_prefix": "",
        "query_prefix": "",
    }


def _flip_gate_down(vs) -> None:
    """Drive the REAL settings-save readiness hook against the live store
    (requires app.state.vector_store to be the store — g04 AC7 pattern)."""
    from app.api.routes.settings import _invalidate_vector_store_readiness

    _invalidate_vector_store_readiness(app, _prior_config())
    assert vs._ready is False, "precondition: the hook must flip the store not-ready"


def _seed_vault(db_path: str, vault_id: int, name: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT OR IGNORE INTO vaults (id, name) VALUES (?, ?)",
            (vault_id, name),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_indexed_file(db_path: str, vault_id: int, file_path: str) -> int:
    """The exact files-row shape the reindex selection reads (g04 AC7)."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        cur = conn.execute(
            "INSERT INTO files (file_path, vault_id, status, phase, file_name, file_size) "
            "VALUES (?, ?, 'indexed', 'complete', ?, 100)",
            (file_path, vault_id, os.path.basename(file_path)),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def _files_row(db_path: str, file_id: int):
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT status, error_message FROM files WHERE id = ?", (file_id,)
        ).fetchone()
        return None if row is None else (row[0], row[1])
    finally:
        conn.close()


def _files_row_by_path(db_path: str, file_path: str):
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT id, status, error_message FROM files WHERE file_path = ?",
            (file_path,),
        ).fetchone()
        return None if row is None else (row[0], row[1], row[2])
    finally:
        conn.close()


def _job_row(db_path: str, job_id: int):
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT status, result_json, error FROM document_reindex_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        return None if row is None else (row[0], row[1], row[2])
    finally:
        conn.close()


@contextlib.contextmanager
def _real_store_worker_env():
    """setup_app_state + REAL lancedb VectorStore + REAL BackgroundProcessor
    (the g04 AC2 harness with the recording store replaced by a real store —
    the upload/scan legs write through the real surface)."""
    with _settings_snapshot():
        settings.users_enabled = False
        settings.ingestion_job_lease_enabled = False  # legacy in-memory transport
        _reset_pools()
        embedding = _G04EmbeddingService()
        cleanup = setup_app_state(
            app,
            embedding_service=embedding,
            llm_client=FakeLLMClient(),
            memory_store=FakeMemoryStore(),
        )
        try:
            vs, _db, table = _make_real_lancedb_store(app.state._test_data_dir)
            _seed_vault(app.state._test_db_path, _VAULT_ID, "g04b-default-vault")
            bp = BackgroundProcessor(
                retry_delay=0,
                vector_store=vs,
                embedding_service=embedding,
                pool=app.state.db_pool,
                llm_client=FakeLLMClient(),
                maintenance_service=app.state.maintenance_service,
            )
            orig_bp = getattr(app.state, "background_processor", None)
            had_bp = hasattr(app.state, "background_processor")
            app.state.background_processor = bp
            try:
                with _app_state_store(vs):
                    client = TestClient(app)
                    client.headers.update(
                        {"Authorization": f"Bearer {settings.admin_secret_token}"}
                    )
                    yield SimpleNamespace(
                        client=client,
                        bp=bp,
                        vs=vs,
                        table=table,
                        db_path=app.state._test_db_path,
                        data_dir=app.state._test_data_dir,
                        embedding=embedding,
                    )
            finally:
                if had_bp:
                    app.state.background_processor = orig_bp
                else:
                    try:
                        del app.state.background_processor
                    except AttributeError:
                        pass
        finally:
            cleanup()
            _reset_pools()


@contextlib.contextmanager
def _reindex_completion_env(vault_id):
    """The g04 AC7 reindex-completion harness: isolated env + real lancedb
    store on ASYNC handles + migrated pool + one indexed file + one pending
    ``document_reindex_jobs`` row (``vault_id=None`` seeds a global job)."""
    orig_lease = settings.reindex_job_lease_enabled
    settings.reindex_job_lease_enabled = False  # legacy transport (pinned)
    with _isolated_env() as (tmp_dir, db_path):
        vs, _db, _table = _make_real_lancedb_store(tmp_dir)
        # Production awaits the async lancedb API shapes; rebind the store to
        # async handles over the same on-disk table (AC7 pattern).
        asyncio.run(_rebind_async_handles(vs, str(settings.lancedb_path)))

        seeded_vault = _VAULT_SCOPE if vault_id is not None else _VAULT_ID
        _seed_vault(db_path, seeded_vault, f"g04b-vault-{seeded_vault}")
        _seed_indexed_file(
            db_path, seeded_vault, "/test/g04b_reindex_probe.txt"
        )

        pool = SimpleConnectionPool(db_path)
        try:
            conn = pool.get_connection()
            try:
                conn.execute("PRAGMA foreign_keys = ON")
                payload = (
                    json.dumps({"vault_id": vault_id})
                    if vault_id is not None
                    else json.dumps({})
                )
                cur = conn.execute(
                    "INSERT INTO document_reindex_jobs "
                    "(vault_id, trigger_type, trigger_id, status, input_json) "
                    "VALUES (?, 'api', 'g04b', 'pending', ?)",
                    (vault_id, payload),
                )
                conn.commit()
                job_id = int(cur.lastrowid)
            finally:
                pool.release_connection(conn)

            yield SimpleNamespace(
                vs=vs, pool=pool, job_id=job_id, db_path=db_path
            )
        finally:
            try:
                pool.close_all()
            except Exception:
                pass
            settings.reindex_job_lease_enabled = orig_lease


def _make_reindex_processor(vs, pool) -> BackgroundProcessor:
    """REAL BackgroundProcessor; only the per-file embed step is doubled
    (AsyncMock success — the completion decision is the unit under test) —
    the TestReindexJobCompletionOrdering pattern."""
    processor = BackgroundProcessor(vector_store=vs)
    processor.processor.pool = _PoolConnectionWrapper(pool)
    processor.processor.process_existing_file = AsyncMock(
        return_value=MagicMock(file_id=1)
    )
    return processor


def _spy_record_embedding_metadata(vs, calls: list):
    """Wrap record_embedding_metadata with a call-through spy (the same seam
    the failure-injection suites patch)."""
    orig = vs.record_embedding_metadata

    async def _spy(embedding_dim, raise_on_error=False, model_id=None,
                   prefix_hash=None):
        calls.append(
            {
                "embedding_dim": embedding_dim,
                "raise_on_error": raise_on_error,
                "model_id": model_id,
                "prefix_hash": prefix_hash,
            }
        )
        return await orig(
            embedding_dim,
            raise_on_error=raise_on_error,
            model_id=model_id,
            prefix_hash=prefix_hash,
        )

    vs.record_embedding_metadata = _spy


def _spy_mark_ready(vs, calls: list):
    """Wrap mark_ready with a call-through spy."""
    orig = vs.mark_ready

    async def _spy(ready: bool = True):
        calls.append(ready)
        return await orig(ready)

    vs.mark_ready = _spy


# ---------------------------------------------------------------------------
# 1-3 — the route-level ingest gate (require_ingest_ready).
# ---------------------------------------------------------------------------

def test_upload_document_root_alias_gated_when_not_ready():
    """POST /documents (the root alias) must answer 503 with the mismatch
    detail after an identity flip — the alias route carries the same
    require_ingest_ready dependency as the /upload alias (issue #736)."""
    with _settings_snapshot():
        settings.users_enabled = False
        store = _G04RecordingVectorStore()
        cleanup = setup_app_state(
            app,
            vector_store=store,
            embedding_service=_G04EmbeddingService(),
            llm_client=FakeLLMClient(),
            memory_store=FakeMemoryStore(),
        )
        try:
            _seed_vault(app.state._test_db_path, _VAULT_ID, "g04b-root-vault")
            client = TestClient(app)
            client.headers.update(
                {"Authorization": f"Bearer {settings.admin_secret_token}"}
            )

            # The identity flip through the REAL settings save (g04 AC2
            # pattern): PUT /api/settings changes embedding_model, the save
            # hook flips the live store not-ready.
            put_resp = client.put(
                "/api/settings",
                json={"embedding_model": settings.embedding_model + "-g04b"},
            )
            assert put_resp.status_code == 200, put_resp.text
            assert store._ready is False, (
                "precondition: the settings save must have flipped the store "
                f"not-ready (_ready={store._ready!r})"
            )

            resp = client.post(
                f"/api/documents?vault_id={_VAULT_ID}",
                files={
                    "file": (
                        "g04b_root_alias.sql",
                        BytesIO(_SQL_CONTENT),
                        "text/plain",
                    )
                },
            )
            assert resp.status_code == 503, (
                f"root-alias upload must be refused while the identity gate "
                f"is down; got {resp.status_code}: {resp.text[:300]}"
            )
            assert resp.json()["detail"] == _MISMATCH_DETAIL
            assert store.stored_chunks == []
        finally:
            cleanup()
            _reset_pools()


def test_upload_refused_during_staged_rebuild():
    """Both upload aliases on a REAL store with ``rebuild_in_progress=True``
    (and ``_ready=True``) must answer 503 "Document reindex in progress" —
    an upload during a staged rebuild would be discarded at cutover."""
    with _settings_snapshot():
        settings.users_enabled = False
        cleanup = setup_app_state(
            app,
            embedding_service=_G04EmbeddingService(),
            llm_client=FakeLLMClient(),
            memory_store=FakeMemoryStore(),
        )
        try:
            _seed_vault(app.state._test_db_path, _VAULT_ID, "g04b-rebuild-vault")
            vs, _db, _table = _make_real_lancedb_store(app.state._test_data_dir)
            vs.rebuild_in_progress = True  # a staged rebuild is open
            assert vs._ready is True  # precondition: the identity gate is UP

            with _app_state_store(vs):
                client = TestClient(app)
                client.headers.update(
                    {"Authorization": f"Bearer {settings.admin_secret_token}"}
                )
                for route in (
                    f"/api/documents?vault_id={_VAULT_ID}",
                    f"/api/documents/upload?vault_id={_VAULT_ID}",
                ):
                    resp = client.post(
                        route,
                        files={
                            "file": (
                                "g04b_rebuild_probe.sql",
                                BytesIO(_SQL_CONTENT),
                                "text/plain",
                            )
                        },
                    )
                    assert resp.status_code == 503, (
                        f"{route}: upload during a staged rebuild must be "
                        f"refused; got {resp.status_code}: {resp.text[:300]}"
                    )
                    assert resp.json()["detail"] == _REBUILD_DETAIL
        finally:
            cleanup()
            _reset_pools()


def test_scan_route_gated_when_not_ready():
    """POST /documents/scan with ``_ready=False`` must answer 503 with the
    mismatch detail before any directory scanning runs (setup_app_state /
    TestClient harness; admin auth + csrf via the naive policy overrides)."""
    store = _G04RecordingVectorStore()
    store._ready = False
    cleanup = setup_app_state(
        app,
        vector_store=store,
        embedding_service=_G04EmbeddingService(),
        llm_client=FakeLLMClient(),
        memory_store=FakeMemoryStore(),
    )
    try:
        client = TestClient(app)
        resp = client.post("/api/documents/scan")
        assert resp.status_code == 503, (
            f"scan must be refused while the identity gate is down; got "
            f"{resp.status_code}: {resp.text[:300]}"
        )
        assert resp.json()["detail"] == _MISMATCH_DETAIL
    finally:
        cleanup()
        _reset_pools()


# ---------------------------------------------------------------------------
# 4-8 — the DocumentProcessor write-seam backstop.
# ---------------------------------------------------------------------------

def test_post_change_scan_writes_zero_vectors():
    """With the gate down, FileWatcher.scan_once still discovers a new file
    in uploads_dir and enqueues it, and the drained REAL processor settles
    the files row — but the live table gains ZERO vectors for it (the
    processor-seam backstop owns tasks the route gate never saw)."""
    from app.services.file_watcher import FileWatcher

    with _real_store_worker_env() as env:
        _flip_gate_down(env.vs)
        scan_dir = settings.vault_uploads_dir(_VAULT_ID)
        scan_file = scan_dir / "g04b_scan_probe.sql"
        scan_file.write_bytes(_SQL_CONTENT)

        async def _scenario():
            watcher = FileWatcher(env.bp, pool=app.state.db_pool)
            result = await watcher.scan_once()
            assert result.files_enqueued == 1, (
                f"precondition: the scan must discover and enqueue the new "
                f"file (errors={result.errors!r})"
            )
            await _drain_queue(env.bp)

        asyncio.run(_scenario())

        row = _files_row_by_path(env.db_path, str(scan_file.resolve()))
        assert row is not None, "the discovered file must have a files row"
        _file_id, status, error_message = row
        expected = format_ingest_error(INGEST_ERROR_EMBEDDING_MISMATCH)
        assert status == "error", (
            f"the gated scan task must settle to error; got {status!r} "
            f"({error_message!r})"
        )
        assert error_message == expected, (
            f"the persisted message must be the stable mismatch code: "
            f"{error_message!r} != {expected!r}"
        )
        assert len(env.table.to_arrow()) == 0, (
            "the live table must have gained ZERO vectors while the gate "
            "was down"
        )


@contextlib.contextmanager
def _queued_upload_backstop(*, flip_identity: bool, open_rebuild: bool):
    """Upload through the REAL route with the gate open, then close the gate
    (the queued-before-the-flip race window), then drain the REAL worker.
    Yields (env, file_id)."""
    with _real_store_worker_env() as env:
        upload = env.client.post(
            f"/api/documents/upload?vault_id={_VAULT_ID}",
            files={
                "file": (
                    "g04b_backstop_probe.sql",
                    BytesIO(_SQL_CONTENT),
                    "text/plain",
                )
            },
        )
        assert upload.status_code == 200, upload.text
        file_id = upload.json()["file_id"]
        assert env.bp.queue.qsize() == 1, (
            "precondition: the route must have enqueued onto the real queue"
        )

        # The race window under test: the gate closes AFTER the row is
        # queued but BEFORE the worker reaches the write seam.
        if flip_identity:
            _flip_gate_down(env.vs)
        if open_rebuild:
            env.vs.rebuild_in_progress = True

        asyncio.run(_drain_queue(env.bp))
        yield env, file_id


def test_backstop_settles_queued_upload_to_stable_error():
    """An ordinary queued upload processed while ``_ready=False,
    rebuild_in_progress=False`` settles to error with the stable
    EMBEDDING_MODEL_MISMATCH message and zero vectors written."""
    with _queued_upload_backstop(
        flip_identity=True, open_rebuild=False
    ) as (env, file_id):
        expected = format_ingest_error(INGEST_ERROR_EMBEDDING_MISMATCH)
        row = _files_row(env.db_path, file_id)
        assert row is not None
        status, error_message = row
        assert status == "error", (
            f"the queued upload must settle to error; got {status!r} "
            f"({error_message!r})"
        )
        assert error_message == expected, (
            f"the persisted message must be the stable mismatch code: "
            f"{error_message!r} != {expected!r}"
        )
        assert error_message.startswith("EMBEDDING_MODEL_MISMATCH: ")
        assert len(env.table.to_arrow()) == 0, (
            "the write-seam backstop must have written ZERO vectors"
        )


def test_backstop_zero_writes_not_ready_and_rebuild_in_progress():
    """Same as the settle test with BOTH flags set (identity gate down and a
    staged rebuild open) — still zero vectors, still the stable code."""
    with _queued_upload_backstop(
        flip_identity=True, open_rebuild=True
    ) as (env, file_id):
        expected = format_ingest_error(INGEST_ERROR_EMBEDDING_MISMATCH)
        row = _files_row(env.db_path, file_id)
        assert row is not None
        status, error_message = row
        assert status == "error"
        assert error_message == expected
        assert len(env.table.to_arrow()) == 0


def test_backstop_zero_writes_ready_and_rebuild_in_progress():
    """Same as the settle test with ``_ready=True, rebuild_in_progress=True``
    — the mid-rebuild discard window: an upload landing now would be
    discarded at cutover, so the seam refuses it too."""
    with _queued_upload_backstop(
        flip_identity=False, open_rebuild=True
    ) as (env, file_id):
        assert env.vs._ready is True  # precondition: identity gate is UP
        assert env.vs.rebuild_in_progress is True
        expected = format_ingest_error(INGEST_ERROR_EMBEDDING_MISMATCH)
        row = _files_row(env.db_path, file_id)
        assert row is not None
        status, error_message = row
        assert status == "error"
        assert error_message == expected
        assert len(env.table.to_arrow()) == 0


def test_process_file_legacy_path_gated():
    """The file_id=None worker selection (``_run_task_processing`` picks
    ``process_file`` for scan/email-shaped tasks) is gated at the same write
    seam: a scan-shaped task drained through the REAL worker with the gate
    down writes zero vectors and settles the stable error."""
    with _real_store_worker_env() as env:
        _flip_gate_down(env.vs)
        legacy_dir = Path(env.data_dir) / "g04b_legacy"
        legacy_dir.mkdir(parents=True, exist_ok=True)
        legacy_file = legacy_dir / "g04b_legacy_probe.sql"
        legacy_file.write_bytes(_SQL_CONTENT)

        async def _scenario():
            queued = await env.bp.enqueue(
                str(legacy_file), vault_id=_VAULT_ID, source="scan"
            )
            assert queued is True, "precondition: the task must be queued"

            await _drain_queue(env.bp)

        asyncio.run(_scenario())

        row = _files_row_by_path(env.db_path, str(legacy_file.resolve()))
        assert row is not None, "process_file must have created the files row"
        _file_id, status, error_message = row
        expected = format_ingest_error(INGEST_ERROR_EMBEDDING_MISMATCH)
        assert status == "error", (
            f"the legacy-path task must settle to error; got {status!r} "
            f"({error_message!r})"
        )
        assert error_message == expected
        assert len(env.table.to_arrow()) == 0, (
            "the legacy path must have written ZERO live vectors"
        )


# ---------------------------------------------------------------------------
# 9 — reindex-context exemption (PRESERVING).
# ---------------------------------------------------------------------------

def test_staged_reindex_writes_continue_while_not_ready():
    """A reindex-context ``process_existing_file`` with a staged
    vector_target while ``_ready=False`` still performs its writes — the
    backstop exempts reindex work (staged target, never the live table);
    mirrors the test_b07 staged-write expectations."""
    with _isolated_env() as (tmp_dir, db_path):
        store = _G04RecordingVectorStore()
        store._ready = False  # gate down — the exemption must carry the write
        sql_path = Path(tmp_dir) / "g04b_staged_probe.sql"
        sql_path.write_bytes(_SQL_CONTENT)
        _seed_vault(db_path, _VAULT_ID, "g04b-staged-vault")
        file_id = _seed_indexed_file(db_path, _VAULT_ID, str(sql_path))

        # Real SQLiteConnectionPool (the b07 fixture pattern): the full
        # process_existing_file path also routes committing writes through
        # document_progress.write_session -> pool.get_connection_async().
        from app.models.database import SQLiteConnectionPool

        pool = SQLiteConnectionPool(db_path, max_size=4)
        try:
            emb = _G04EmbeddingService()
            bp = BackgroundProcessor(
                retry_delay=0,
                vector_store=store,
                embedding_service=emb,
                pool=pool,
                llm_client=FakeLLMClient(),
            )
            staged_target = SimpleNamespace(table_name="g04b_staged_target")

            async def _scenario():
                await bp.processor.process_existing_file(
                    file_id,
                    str(sql_path),
                    _VAULT_ID,
                    retry_context="reindex",
                    vector_target=staged_target,
                )

            asyncio.run(_scenario())

            assert store.stored_chunks, (
                "reindex-context work with a staged target must still "
                "perform its vector writes while the gate is down"
            )
            staged_writes = [
                kwargs
                for _count, kwargs in store.add_chunks_calls
                if kwargs.get("target") is staged_target
            ]
            assert staged_writes, (
                f"every reindex write must carry the staged target; calls: "
                f"{store.add_chunks_calls!r}"
            )
            row = _files_row(db_path, file_id)
            assert row is not None and row[0] == "indexed", (
                f"the exempted reindex re-embed must complete; row={row!r}"
            )
        finally:
            try:
                pool.close_all()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# 10-14 — the reindex completion lift (vault scoping + epoch guard).
# ---------------------------------------------------------------------------

def test_gate_down_all_vault_reindex_recovers():
    """An all-vault reindex (vault_id None) with the gate down completes,
    records embedding metadata, and lifts readiness to True — the recovery
    path (PRESERVING)."""
    with _reindex_completion_env(vault_id=None) as env:
        with _app_state_store(env.vs):
            _flip_gate_down(env.vs)
            record_calls: list = []
            _spy_record_embedding_metadata(env.vs, record_calls)

            processor = _make_reindex_processor(env.vs, env.pool)
            asyncio.run(processor._process_reindex_job(env.job_id))

        status, result_json, _error = _job_row(env.db_path, env.job_id)
        assert status == "completed", (
            f"the all-vault recovery must complete; row={status!r} "
            f"error={_error!r}"
        )
        assert record_calls, (
            "the all-vault reindex must record embedding metadata at "
            "completion"
        )
        assert env.vs._ready is True, (
            "the all-vault reindex must lift the down gate on completion"
        )
        assert "readiness_skip_reason" not in (json.loads(result_json) or {})


def test_scoped_reindex_gate_down_skips_record_and_ready():
    """A vault-scoped job completing with the gate down must NOT record
    metadata nor lift the gate (other vaults still hold old-model vectors);
    the job still settles completed with readiness_skip_reason
    'vault_scoped_gate_down'."""
    with _reindex_completion_env(vault_id=_VAULT_SCOPE) as env:
        with _app_state_store(env.vs):
            _flip_gate_down(env.vs)
            record_calls: list = []
            mark_calls: list = []
            _spy_record_embedding_metadata(env.vs, record_calls)
            _spy_mark_ready(env.vs, mark_calls)

            processor = _make_reindex_processor(env.vs, env.pool)
            asyncio.run(processor._process_reindex_job(env.job_id))

        status, result_json, _error = _job_row(env.db_path, env.job_id)
        assert status == "completed", (
            f"the scoped job still completes; row={status!r} error={_error!r}"
        )
        assert record_calls == [], (
            "record_embedding_metadata must NOT run for a gate-down "
            "vault-scoped completion"
        )
        assert env.vs._ready is False, (
            "a vault-scoped reindex must not lift a down gate"
        )
        assert True not in mark_calls
        result = json.loads(result_json) or {}
        assert result.get("readiness_skip_reason") == "vault_scoped_gate_down"


def test_scoped_reindex_gate_up_runs_full_sequence():
    """With the gate UP (ready store) a vault-scoped job keeps the full
    completion sequence — record_embedding_metadata and mark_ready(True)
    still run (the failure-injection suites' contract, PRESERVING)."""
    with _reindex_completion_env(vault_id=_VAULT_SCOPE) as env:
        assert env.vs._ready is True  # precondition: the gate is UP
        record_calls: list = []
        mark_calls: list = []
        _spy_record_embedding_metadata(env.vs, record_calls)
        _spy_mark_ready(env.vs, mark_calls)

        with _app_state_store(env.vs):
            processor = _make_reindex_processor(env.vs, env.pool)
            asyncio.run(processor._process_reindex_job(env.job_id))

        status, result_json, _error = _job_row(env.db_path, env.job_id)
        assert status == "completed", (
            f"the scoped job must complete; row={status!r} error={_error!r}"
        )
        assert record_calls, "record_embedding_metadata must still run"
        assert True in mark_calls, "mark_ready(True) must still run"
        assert env.vs._ready is True
        result = json.loads(result_json) or {}
        assert "readiness_skip_reason" not in result


def test_invalidation_during_metadata_await_blocks_lift():
    """Deterministic interleaving: a background thread drives the REAL
    ``_invalidate_vector_store_readiness`` while the completion's
    ``record_embedding_metadata`` await is in flight (the wrapper holds the
    await open until the invalidation landed). The bumped epoch must
    suppress the lift — the job cannot finish with ``_ready=True`` and the
    result carries 'invalidated_during_completion'."""
    with _reindex_completion_env(vault_id=None) as env:
        release_invalidation = threading.Event()
        invalidation_finished = threading.Event()
        prior = _prior_config()

        def _invalidator():
            if not release_invalidation.wait(timeout=10.0):
                return
            # The REAL hook (not a reentrant call): runs on its own thread
            # while the completion's metadata await is suspended.
            from app.api.routes.settings import _invalidate_vector_store_readiness

            _invalidate_vector_store_readiness(app, prior)
            invalidation_finished.set()

        thread = threading.Thread(target=_invalidator, daemon=True)
        thread.start()

        orig_record = env.vs.record_embedding_metadata

        async def _record_with_invalidation_in_flight(
            embedding_dim, raise_on_error=False, model_id=None, prefix_hash=None
        ):
            release_invalidation.set()
            # Hold the completion's metadata await open until the
            # invalidation has landed (bounded so a bug cannot hang the
            # suite; the deadline expiring fails the assertions below).
            deadline = time.monotonic() + 10.0
            while not invalidation_finished.is_set() and (
                time.monotonic() < deadline
            ):
                await asyncio.sleep(0.01)
            return await orig_record(
                embedding_dim,
                raise_on_error=raise_on_error,
                model_id=model_id,
                prefix_hash=prefix_hash,
            )

        env.vs.record_embedding_metadata = _record_with_invalidation_in_flight

        try:
            with _app_state_store(env.vs):
                processor = _make_reindex_processor(env.vs, env.pool)
                asyncio.run(processor._process_reindex_job(env.job_id))
        finally:
            thread.join(timeout=10.0)

        assert invalidation_finished.is_set(), (
            "the invalidation thread must have run during the await"
        )
        assert not thread.is_alive()
        status, result_json, _error = _job_row(env.db_path, env.job_id)
        assert status == "completed", (
            f"the job still completes (epoch-suppressed lift); row={status!r}"
        )
        result = json.loads(result_json) or {}
        assert result.get("readiness_skip_reason") == (
            "invalidated_during_completion"
        )
        assert env.vs._ready is False, (
            "the completion must not lift readiness over the invalidation "
            "that landed during the metadata await"
        )


def test_final_recheck_boundary_race_closed():
    """NON-REENTRANT boundary race: an invalidation arriving at the
    final-recheck->mark_ready window is fired from OUTSIDE the guard (a
    thread released by a mark_ready wrapper; it blocks on the store's
    readiness lock until the completion's critical section releases it —
    never a reentrant call from inside the held guard). The store must never
    end ready-over-mismatch: here the lift runs under the guard and the
    later invalidation wins."""
    with _reindex_completion_env(vault_id=None) as env:
        start_invalidation = threading.Event()
        invalidation_finished = threading.Event()
        prior = _prior_config()

        def _invalidator():
            if not start_invalidation.wait(timeout=10.0):
                return
            # From OUTSIDE the guard: blocks on readiness_guard's lock while
            # the completion holds its critical section, then flips _ready
            # and bumps the epoch the moment it releases.
            from app.api.routes.settings import _invalidate_vector_store_readiness

            _invalidate_vector_store_readiness(app, prior)
            invalidation_finished.set()

        thread = threading.Thread(target=_invalidator, daemon=True)
        thread.start()

        mark_calls: list = []
        orig_mark = env.vs.mark_ready

        async def _mark_ready_releasing_invalidator(ready: bool = True):
            mark_calls.append(ready)
            if ready is True:
                # Fire-and-forget ONLY: waiting for the thread here would be
                # the forbidden re-entrant invalidation inside the held
                # guard (a deadlock by construction).
                start_invalidation.set()
            return await orig_mark(ready)

        env.vs.mark_ready = _mark_ready_releasing_invalidator

        try:
            with _app_state_store(env.vs):
                # Gate down before the run (epoch 1) so the recovery lift is
                # the code under test.
                _flip_gate_down(env.vs)
                epoch_before = env.vs._readiness_epoch

                processor = _make_reindex_processor(env.vs, env.pool)
                asyncio.run(processor._process_reindex_job(env.job_id))
        finally:
            thread.join(timeout=10.0)

        assert invalidation_finished.is_set(), (
            "the boundary invalidation must have landed (blocked on the "
            "guard, then admitted) — a hang here means the guard deadlocked"
        )
        assert not thread.is_alive()
        assert True in mark_calls, (
            "the lift critical section must actually have been entered "
            "(the final-recheck window was exercised)"
        )
        assert env.vs._readiness_epoch >= epoch_before + 1
        assert env.vs._ready is False, (
            "the store must never end ready-over-mismatch at the boundary — "
            "the later invalidation wins"
        )
        status, _result_json, _error = _job_row(env.db_path, env.job_id)
        assert status == "completed"


# ---------------------------------------------------------------------------
# 15-16 — the metric-aware threshold on the production path + format pin.
# ---------------------------------------------------------------------------

async def test_production_path_l2_threshold_keeps_centre():
    """Through the REAL RAGEngine sync path (``_filter_relevant`` ->
    ``_sync_document_retrieval_settings``), ``vector_metric='l2'`` with raw
    ``max_distance_threshold=0.75`` keeps a unit-vector record at
    ``_distance`` 1.22 and one at 1.4 (effective bound 1.5) while dropping
    1.6; cosine at the same raw threshold keeps 0.61 and drops 0.8 exactly
    as before. The engine is constructed under STALE settings so only the
    pre-filter re-sync can carry the live raw threshold in."""
    from app.services.rag_engine import RAGEngine

    orig_metric = settings.vector_metric
    orig_threshold = settings.max_distance_threshold
    orig_window = settings.retrieval_window
    try:
        # Stale construction snapshot (cosine, 0.5): without the re-sync the
        # service would keep filtering at 0.5 and the l2 leg could not pass.
        settings.vector_metric = "cosine"
        settings.max_distance_threshold = 0.5
        settings.retrieval_window = 0
        engine = RAGEngine(
            embedding_service=_G04EmbeddingService(),
            vector_store=_G04RecordingVectorStore(),
            memory_store=FakeMemoryStore(),
            llm_client=FakeLLMClient(),
        )

        # Live raw settings at the calibrated values under l2 semantics.
        settings.vector_metric = "l2"
        settings.max_distance_threshold = 0.75
        l2_records = [
            {"text": "g04b l2 centre band", "file_id": "f1", "_distance": 1.22},
            {"text": "g04b l2 upper band", "file_id": "f2", "_distance": 1.4},
            {"text": "g04b l2 beyond bound", "file_id": "f3", "_distance": 1.6},
        ]
        kept_l2 = await engine._filter_relevant([dict(r) for r in l2_records])
        kept_l2_texts = {s.text for s in kept_l2}
        assert "g04b l2 centre band" in kept_l2_texts, (
            "l2 must keep the calibrated-centre result (raw 1.22 vs the "
            "x2.0 rescaled bound 1.5)"
        )
        assert "g04b l2 upper band" in kept_l2_texts, (
            "the rescale is observable: 1.4 < effective 1.5 must be kept"
        )
        assert "g04b l2 beyond bound" not in kept_l2_texts, (
            "1.6 > effective 1.5 must still be dropped — the rescale is a "
            "bound change, not a filter removal"
        )

        # Cosine control at the same raw threshold: unchanged polarity and
        # bound (0.61 kept, 0.8 dropped).
        settings.vector_metric = "cosine"
        cosine_records = [
            {"text": "g04b cosine kept", "file_id": "f4", "_distance": 0.61},
            {"text": "g04b cosine dropped", "file_id": "f5", "_distance": 0.8},
        ]
        kept_cos = await engine._filter_relevant(
            [dict(r) for r in cosine_records]
        )
        kept_cos_texts = {s.text for s in kept_cos}
        assert "g04b cosine kept" in kept_cos_texts, (
            "cosine at raw threshold 0.75 must keep 0.61 exactly as before"
        )
        assert "g04b cosine dropped" not in kept_cos_texts, (
            "cosine at raw threshold 0.75 must drop 0.8 exactly as before"
        )
    finally:
        settings.vector_metric = orig_metric
        settings.max_distance_threshold = orig_threshold
        settings.retrieval_window = orig_window


def test_stable_reason_format_pinned():
    """The gate error settles to the pinned stable message:
    format_ingest_error(classify_ingest_error(IngestGateClosedError)) equals
    format_ingest_error(INGEST_ERROR_EMBEDDING_MISMATCH) and starts with
    'EMBEDDING_MODEL_MISMATCH: '."""
    settled = format_ingest_error(classify_ingest_error(IngestGateClosedError("x")))
    pinned = format_ingest_error(INGEST_ERROR_EMBEDDING_MISMATCH)
    assert settled == pinned
    assert settled.startswith("EMBEDDING_MODEL_MISMATCH: ")
