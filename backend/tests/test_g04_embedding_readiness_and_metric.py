"""G04 (issues #736/#833) arm's-length acceptance checks — embedding
readiness, upload mixing, metric-aware thresholds, and the vault-scoped
reindex gate lift.

Four contractual checks (names are contractual, do not rename):

- ``test_embedding_model_put_gates_model_ready`` (AC1, expected GREEN at
  base): a VALIDATED-ready real VectorStore (real LanceDB table + matching
  sidecar identity, proven via ``validate_schema``) must be taken out of
  readiness by PUT /api/settings changing ``embedding_model``, so the
  ``require_model_ready`` gate answers 503 (issue #695 behavior).
- ``test_upload_after_embedding_model_change_is_not_mixed`` (AC2, expected
  RED at base): after the settings identity change (no reindex), an upload
  must not write new-model vectors into the old-model table — the upload is
  either refused (409/503) or persists zero new vectors.
- ``test_l2_metric_keeps_calibrated_centre_result`` (AC3, expected RED at
  base): with ``settings.vector_metric = 'l2'`` and the calibrated
  ``max_distance_threshold`` 0.75, a result at ``_distance`` 1.22 (unit
  vectors — squarely inside the relevant band under l2 semantics) must
  survive ``filter_relevant``; a cosine control at 0.61 must also survive,
  making the check self-calibrating.
- ``test_vault_scoped_reindex_does_not_lift_gate_over_mixed_index`` (AC7,
  expected RED at base): after the identity-change readiness flip, a
  VAULT-SCOPED reindex driven through the REAL completion path
  (``BackgroundProcessor._process_reindex_job`` -> ``_reindex_embed_all``)
  must not lift the gate while a second vault still holds old-model
  vectors. Base marks ready unconditionally on completion.

Offline and deterministic: every embedding HTTP call is faked with the
in-repo fake services; all state lives in per-test temp dirs and every
settings singleton mutation is restored in ``finally`` blocks.
"""

import asyncio
import contextlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

CSRF_TEST_POLICY = "naive"  # harness sends no token header; declared for tests/conftest.py

# lancedb / pyarrow are REQUIRED for these tests (real-store checks) — import
# them early, exactly like test_embedding_model_versioning.py.
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
from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from test_integration import FakeLLMClient, FakeMemoryStore, setup_app_state  # noqa: E402

from app.api.deps import require_model_ready  # noqa: E402
from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402
from app.services.vector_store import VectorStore  # noqa: E402

_VAULT_SCOPE = 2    # the vault the scoped reindex covers
_VAULT_OTHER = 3    # the vault that still holds old-model vectors


# ---------------------------------------------------------------------------
# Shared helpers (patterns copied from test_b06_runtime_model_change_readiness
# and test_embedding_model_versioning — no new harness styles invented).
# ---------------------------------------------------------------------------

def _gate_status(store) -> int:
    """Evaluate the readiness gate exactly as a request-scoped dependency
    would (test_b06 pattern): call ``require_model_ready`` on the live store
    and fold any HTTPException into its status code (200 means admitted)."""
    try:
        require_model_ready(store)
    except HTTPException as exc:
        return exc.status_code
    return 200


def _reset_pools() -> None:
    """Close and forget every cached SQLite pool (test_embedding_model_
    versioning setUp/tearDown pattern)."""
    from app.models.database import _pool_cache, _pool_cache_lock

    with _pool_cache_lock:
        for _path, _pool in list(_pool_cache.items()):
            _pool.close_all()
        _pool_cache.clear()


async def _validate_schema_with_async_handles(vs, uri: str, **validate_kwargs):
    """Run validate_schema against REAL lancedb's async connection API
    (copied verbatim from test_embedding_model_versioning)."""
    vs.db = await lancedb.connect_async(uri)
    vs.table = await vs.db.open_table("chunks")
    return await vs.validate_schema(**validate_kwargs)


async def _rebind_async_handles(vs, uri: str):
    """Rebind vs to lancedb's ASYNC connection API. Production awaits
    ``table.schema()`` / ``db.table_names()`` (async shapes); the sync
    connect() handles expose plain values, and ``await sync_table.schema()``
    raises TypeError — the same rebind _validate_schema_with_async_handles
    performs."""
    vs.db = await lancedb.connect_async(uri)
    vs.table = await vs.db.open_table("chunks")


def _write_sidecar(db_path: str, model_id, dim: int, prefix_hash) -> None:
    """Persist the embedding-identity sidecar rows into settings_kv."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO settings_kv (key, value) VALUES (?, ?)",
            ("embedding_model_id", model_id),
        )
        conn.execute(
            "INSERT OR REPLACE INTO settings_kv (key, value) VALUES (?, ?)",
            ("embedding_dim", str(dim)),
        )
        conn.execute(
            "INSERT OR REPLACE INTO settings_kv (key, value) VALUES (?, ?)",
            ("embedding_prefix_hash", prefix_hash),
        )
        conn.commit()
    finally:
        conn.close()


def _make_real_lancedb_store(tmp_dir: str):
    """Real VectorStore over a real LanceDB table on a temp dir
    (TestValidateSchemaMismatch pattern). Returns (vs, db, table)."""
    lancedb_path = settings.lancedb_path
    lancedb_path.mkdir(parents=True, exist_ok=True)

    schema = pa.schema([
        ("id", pa.string()),
        ("text", pa.string()),
        ("file_id", pa.string()),
        ("vault_id", pa.string()),
        ("chunk_index", pa.int32()),
        ("embedding", pa.list_(pa.float32(), 384)),
    ])
    db = lancedb.connect(str(lancedb_path))
    table = db.create_table("chunks", schema=schema, exist_ok=True)

    vs = VectorStore()
    vs.db = db
    vs.table = table
    return vs, db, table


@contextlib.contextmanager
def _isolated_env():
    """Temp data_dir + fresh DB + snapshotted settings singleton, restored
    on exit (merges the test_b06 snapshot loop with the versioning-suite
    pool/init pattern)."""
    snapshot = {
        name: getattr(settings, name) for name in type(settings).model_fields
    }
    tmp_dir = tempfile.mkdtemp(prefix="g04_ac_")
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
        for _name, _value in snapshot.items():
            setattr(settings, _name, _value)
        shutil.rmtree(tmp_dir, ignore_errors=True)


@contextlib.contextmanager
def _app_state_store(store):
    """Install ``store`` as the live app.state.vector_store, restore after
    (test_b06 pattern)."""
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
    managers BackgroundProcessor expects (copied from
    test_embedding_model_versioning)."""

    def __init__(self, pool):
        self._pool = pool

    def connection(self):
        return contextlib.closing(self._pool.get_connection())

    def connection_async(self):
        return _AsyncClosingConnection(self._pool.get_connection())


class _AsyncClosingConnection:
    """Async mirror of contextlib.closing (copied from
    test_embedding_model_versioning)."""

    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        self._conn.close()
        return False


# ---------------------------------------------------------------------------
# AC1 — validated-ready store, PUT flips the gate (expected GREEN at base).
# ---------------------------------------------------------------------------

def test_embedding_model_put_gates_model_ready():
    """AC1: a VALIDATED-ready real VectorStore must gate with 503 after PUT
    /api/settings changes embedding_model (issue #695 contract; the store was
    proven ready by validate_schema over a real matching sidecar first)."""
    with _isolated_env() as (tmp_dir, db_path):
        vs, _db, _table = _make_real_lancedb_store(tmp_dir)
        # Sidecar matches the CURRENT configured identity -> validate_schema
        # must confirm ready (the validated-ready precondition, not assumed).
        _write_sidecar(
            db_path,
            settings.embedding_model,
            384,
            vs._compute_embedding_prefix_hash(),
        )
        result = asyncio.run(
            _validate_schema_with_async_handles(
                vs, str(settings.lancedb_path),
                embedding_model_id=settings.embedding_model, embedding_dim=384,
            )
        )
        assert vs._ready is True, (
            "precondition: validate_schema must confirm the store ready on a "
            f"matching sidecar; got _ready={vs._ready!r}, result={result!r}"
        )

        orig_users_enabled = settings.users_enabled
        settings.users_enabled = False
        with _app_state_store(vs):
            client = TestClient(app)
            client.headers.update(
                {"Authorization": f"Bearer {settings.admin_secret_token}"}
            )
            old_model = settings.embedding_model
            new_model = old_model + "-g04"
            try:
                resp = client.put(
                    "/api/settings", json={"embedding_model": new_model}
                )
                assert resp.status_code == 200, resp.text

                status = _gate_status(app.state.vector_store)
                assert status == 503, (
                    "AC1: after PUT /api/settings changed embedding_model "
                    f"{old_model!r} -> {new_model!r}, the readiness gate must "
                    f"answer 503 until an admin reindex; require_model_ready "
                    f"returned {status} with _ready={vs._ready!r}"
                )
                assert vs._ready is False, (
                    "AC1: the validated-ready store itself must carry "
                    f"_ready=False after the embedding model change; got "
                    f"_ready={vs._ready!r}"
                )
            finally:
                settings.users_enabled = orig_users_enabled


# ---------------------------------------------------------------------------
# AC2 — upload after the identity change must not mix (expected RED at base).
# ---------------------------------------------------------------------------

class _G04EmbeddingService:
    """Embedding fake matching the REAL ``embed_batch`` call contract
    (fail_fast=False returns (embeddings, failed_indices)) — the
    test_issue258_e2e_real_worker WorkerEmbeddingService pattern."""

    def __init__(self):
        self.dim = 768
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
    ingest path uses (test_issue258_e2e_real_worker RecordingVectorStore
    pattern), plus the readiness/draining surface the #695/#696 hooks touch
    so the flip behaves exactly as it does on a real store."""

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
        self.add_chunks_calls.append(len(records))
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
        return "g04-prefix-hash"

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


_G04_SQL_CONTENT = (
    b"CREATE TABLE g04_upload_probe (\n"
    b"    id INTEGER PRIMARY KEY,\n"
    b"    marker TEXT NOT NULL\n"
    b");\n"
)


async def _drain_queue(bp, timeout: float = 60.0) -> None:
    """Run the REAL worker loop until the ingestion queue is drained
    (test_issue258_e2e_real_worker pattern)."""
    worker = asyncio.create_task(bp._worker_loop())
    try:
        await asyncio.wait_for(bp.queue.join(), timeout=timeout)
    finally:
        bp.shutdown_event.set()
        await asyncio.wait_for(worker, timeout=10.0)


def test_upload_after_embedding_model_change_is_not_mixed():
    """AC2: after PUT /api/settings changes embedding_model (no reindex), an
    upload through the real API + real background worker must not write
    new-model vectors into the old-model index: either the upload is refused
    (409/503) or zero new vectors are persisted. Base silently mixes — the
    upload succeeds and the worker embeds under the NEW model into the OLD
    table."""
    from app.services.background_tasks import BackgroundProcessor

    orig_data_dir = settings.data_dir
    orig_lease_flag = settings.ingestion_job_lease_enabled
    orig_users_enabled = settings.users_enabled
    settings.users_enabled = False
    settings.ingestion_job_lease_enabled = False
    store = _G04RecordingVectorStore()
    embedding = _G04EmbeddingService()
    cleanup = setup_app_state(
        app,
        vector_store=store,
        embedding_service=embedding,
        llm_client=FakeLLMClient(),
        memory_store=FakeMemoryStore(),
    )
    orig_bp = getattr(app.state, "background_processor", None)
    had_bp = hasattr(app.state, "background_processor")
    try:
        db_path = app.state._test_db_path
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute(
                "INSERT OR IGNORE INTO vaults (id, name) VALUES (?, ?)",
                (1, "g04-default-vault"),
            )
            conn.commit()
        finally:
            conn.close()

        bp = BackgroundProcessor(
            retry_delay=0,
            vector_store=store,
            embedding_service=embedding,
            pool=app.state.db_pool,
            llm_client=FakeLLMClient(),
            maintenance_service=app.state.maintenance_service,
        )
        app.state.background_processor = bp

        client = TestClient(app)
        client.headers.update(
            {"Authorization": f"Bearer {settings.admin_secret_token}"}
        )

        old_model = settings.embedding_model
        new_model = old_model + "-g04"
        put_resp = client.put("/api/settings", json={"embedding_model": new_model})
        assert put_resp.status_code == 200, put_resp.text
        assert store._ready is False, (
            "precondition: the settings save must have flipped the recording "
            f"store not-ready (_ready={store._ready!r})"
        )

        vectors_before = len(store.stored_chunks)
        upload_resp = client.post(
            "/api/documents/upload?vault_id=1",
            files={
                "file": (
                    "g04_upload_probe.sql",
                    BytesIO(_G04_SQL_CONTENT),
                    "text/plain",
                )
            },
        )
        upload_status = upload_resp.status_code

        if upload_status == 200:
            # Deterministically drain the real in-process worker.
            asyncio.run(_drain_queue(bp))
        vectors_after = len(store.stored_chunks)
        new_vectors = vectors_after - vectors_before
        refused = upload_status in (409, 503)
        not_mixed = refused or new_vectors == 0

        assert not_mixed is True, (
            "AC2: upload after the embedding-model change "
            f"({old_model!r} -> {new_model!r}, no reindex) mixed the index: "
            f"upload status={upload_status}, new vectors written to the "
            f"old-model table={new_vectors}. The upload must be refused "
            "(409/503) or persist zero new vectors until the index is "
            "rebuilt under the new identity."
        )
    finally:
        if had_bp:
            app.state.background_processor = orig_bp
        else:
            try:
                del app.state.background_processor
            except AttributeError:
                pass
        cleanup()
        settings.data_dir = orig_data_dir
        settings.ingestion_job_lease_enabled = orig_lease_flag
        settings.users_enabled = orig_users_enabled
        _reset_pools()


# ---------------------------------------------------------------------------
# AC3 — l2 metric must not drop calibrated-centre results (expected RED).
# ---------------------------------------------------------------------------

async def test_l2_metric_keeps_calibrated_centre_result():
    """AC3: with settings.vector_metric='l2' and the calibrated
    max_distance_threshold 0.75, a unit-vector result at _distance 1.22
    (the calibrated relevant band under l2 semantics — l2 distances for
    unit vectors live on a sqrt-scaled 0..2 range) must survive
    filter_relevant. The cosine control (same record, _distance 0.61,
    metric 'cosine') must also survive, proving the harness and the
    threshold polarity are correct."""
    from app.services.document_retrieval import DocumentRetrievalService

    orig_metric = settings.vector_metric
    orig_threshold = settings.max_distance_threshold
    try:
        settings.vector_metric = "l2"
        settings.max_distance_threshold = 0.75
        service = DocumentRetrievalService(
            max_distance_threshold=0.75,
            retrieval_top_k=10,
            retrieval_window=0,  # no neighbor expansion: unit seam under test
        )
        l2_record = {
            "text": "l2 metric calibrated-centre result",
            "file_id": "file1",
            "_distance": 1.22,
        }
        out_l2 = await service.filter_relevant([dict(l2_record)])
        kept = len(out_l2) == 1 and out_l2[0].text == l2_record["text"]

        # Cosine control: identical record shape at the cosine-equivalent
        # 0.61 under metric 'cosine' must be kept (self-calibration).
        settings.vector_metric = "cosine"
        control_service = DocumentRetrievalService(
            max_distance_threshold=0.75,
            retrieval_top_k=10,
            retrieval_window=0,
        )
        cosine_record = {
            "text": "cosine control calibrated-centre result",
            "file_id": "file1",
            "_distance": 0.61,
        }
        out_cos = await control_service.filter_relevant([dict(cosine_record)])
        kept_control = (
            len(out_cos) == 1 and out_cos[0].text == cosine_record["text"]
        )
        assert kept_control is True, (
            "AC3 self-calibration: the cosine control (_distance 0.61 under "
            "metric 'cosine', threshold 0.75) must be kept; "
            f"got {len(out_cos)} result(s) — the harness or the threshold "
            "polarity is broken"
        )

        assert kept is True, (
            "AC3: under vector_metric='l2' with max_distance_threshold=0.75, "
            "a unit-vector result at _distance 1.22 (calibrated relevant "
            "band under l2 semantics; cosine-equivalent ~0.61) was dropped — "
            "filter_relevant compares the raw l2 distance against the "
            "cosine-calibrated threshold and must be metric-aware"
        )
    finally:
        settings.vector_metric = orig_metric
        settings.max_distance_threshold = orig_threshold


# ---------------------------------------------------------------------------
# AC7 — vault-scoped reindex must not lift the gate (expected RED at base).
# ---------------------------------------------------------------------------

def test_vault_scoped_reindex_does_not_lift_gate_over_mixed_index():
    """AC7: the reviewer-verified trap at the completion seam. After the
    identity-change readiness flip (driven through the REAL settings-save
    hook ``_invalidate_vector_store_readiness``), a VAULT-SCOPED reindex for
    vault 2 runs to completion through the REAL job path
    (``BackgroundProcessor._process_reindex_job`` -> ``_reindex_embed_all``,
    legacy transport pinned like test_embedding_model_versioning) while vault
    3 still holds old-model vectors. The completion block must NOT lift the
    readiness gate over the mixed index — base marks ready unconditionally
    once no mid-run identity change is detected."""
    from app.api.routes.settings import _invalidate_vector_store_readiness
    from app.services.background_tasks import BackgroundProcessor

    orig_lease_flag = settings.reindex_job_lease_enabled
    settings.reindex_job_lease_enabled = False  # legacy transport (pinned)
    with _isolated_env() as (tmp_dir, db_path):
        vs, _db, table = _make_real_lancedb_store(tmp_dir)
        # Production awaits the async lancedb API shapes (table.schema());
        # rebind the store to async handles over the same on-disk table.
        asyncio.run(_rebind_async_handles(vs, str(settings.lancedb_path)))
        # NOTE: the sidecar intentionally carries NO embedding_model_id —
        # the documented ``_embedding_identity_changed`` False branch ("no
        # recorded identity ... routing stays dimension-only"), which is
        # exactly the state where the vault-scoped staged-path guard cannot
        # fire and the completion block owns the decision under test.

        pool = SimpleConnectionPool(db_path)
        try:
            conn = pool.get_connection()
            try:
                conn.execute("PRAGMA foreign_keys = ON")
                conn.execute(
                    "INSERT OR IGNORE INTO vaults (id, name) VALUES (?, ?)",
                    (_VAULT_SCOPE, "g04-scoped-vault"),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO vaults (id, name) VALUES (?, ?)",
                    (_VAULT_OTHER, "g04-old-model-vault"),
                )
                # One file per vault: vault 2 (the reindex scope) and
                # vault 3 (untouched by the scoped job).
                conn.execute(
                    "INSERT INTO files (file_path, vault_id, status, phase, file_name, file_size) "
                    "VALUES (?, ?, 'indexed', 'complete', ?, 100)",
                    ("/test/scoped.txt", _VAULT_SCOPE, "scoped.txt"),
                )
                conn.execute(
                    "INSERT INTO files (file_path, vault_id, status, phase, file_name, file_size) "
                    "VALUES (?, ?, 'indexed', 'complete', ?, 100)",
                    ("/test/oldmodel.txt", _VAULT_OTHER, "oldmodel.txt"),
                )
                conn.commit()
            finally:
                pool.release_connection(conn)

            # Vault 3 physically holds an old-model vector in the live table.
            table.add([
                {
                    "id": "g04_old_0",
                    "text": "old-model vector still resident in vault 3",
                    "file_id": "oldmodel",
                    "vault_id": str(_VAULT_OTHER),
                    "chunk_index": 0,
                    "embedding": [0.1] * 384,
                }
            ])

            with _app_state_store(vs):
                # The identity-change flip, driven through the REAL
                # settings-save hook (the same call the PUT /settings
                # handler makes — it reads app.state.vector_store, so the
                # store must be live on the app first).
                prior_config = {
                    "model": str(settings.embedding_model or ""),
                    "url": "",
                    "doc_prefix": "",
                    "query_prefix": "",
                }
                _invalidate_vector_store_readiness(app, prior_config)
                assert vs._ready is False, (
                    "precondition: the settings-save readiness hook must have "
                    f"flipped the store not-ready (_ready={vs._ready!r})"
                )

            # Vault-scoped job fixture — the exact row POST /documents/reindex
            # writes for {"vault_id": 2} (documents.py _create_job).
            conn = pool.get_connection()
            try:
                cur = conn.execute(
                    "INSERT INTO document_reindex_jobs "
                    "(vault_id, trigger_type, trigger_id, status, input_json) "
                    "VALUES (?, 'api', 'g04', 'pending', ?)",
                    (_VAULT_SCOPE, json.dumps({"vault_id": _VAULT_SCOPE})),
                )
                conn.commit()
                job_id = cur.lastrowid
            finally:
                pool.release_connection(conn)

            # REAL BackgroundProcessor; only the per-file embed step is
            # doubled (AsyncMock success — the completion decision, not the
            # embedder, is the unit under test), exactly like
            # TestReindexJobCompletionOrdering.
            processor = BackgroundProcessor(vector_store=vs)
            processor.processor.pool = _PoolConnectionWrapper(pool)
            processor.processor.process_existing_file = AsyncMock(
                return_value=MagicMock(file_id=1)
            )

            with _app_state_store(vs):
                asyncio.run(processor._process_reindex_job(job_id))

                # What the run actually did (evidence for the failure text).
                conn = pool.get_connection()
                try:
                    row = conn.execute(
                        "SELECT status FROM document_reindex_jobs WHERE id = ?",
                        (job_id,),
                    ).fetchone()
                    job_status = row[0] if row is not None else "missing"
                finally:
                    pool.release_connection(conn)
                old_model_vectors_remaining = len(table.to_arrow())

                gate_still_refuses = _gate_status(vs) == 503 and vs._ready is False

                assert gate_still_refuses is True, (
                    "AC7: a vault-scoped reindex (vault "
                    f"{_VAULT_SCOPE}) that ran to completion "
                    f"(job status={job_status!r}) lifted the readiness gate "
                    f"(_ready={vs._ready!r}) while vault {_VAULT_OTHER} "
                    f"still holds {old_model_vectors_remaining} old-model "
                    "vector(s) in the live table — the gate must stay down "
                    "until an ALL-VAULT reindex completes"
                )
                assert old_model_vectors_remaining >= 1, (
                    "AC7 context: vault 3's old-model vector must still be "
                    "resident (a vault-scoped job never touches it)"
                )
        finally:
            try:
                pool.close_all()
            except Exception:
                pass
            settings.reindex_job_lease_enabled = orig_lease_flag
