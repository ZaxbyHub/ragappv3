"""B07 acceptance checks (issue #696) — a same-dimension embedding-MODEL
change is still an identity change and must flow through the staged rebuild.

``BackgroundProcessor._reindex_embed_all`` opens the staged rebuild
(``begin_dimension_rebuild`` / ``commit_dimension_rebuild`` /
``abort_dimension_rebuild``) only when the dimension PROBE differs from the
live table dim. A same-dimension embedding-model swap therefore rewrites the
LIVE table in place (``reprocess_kwargs = {}``, "Same-dimension reindex:
nothing to swap") with no abort-on-failure path: a mid-reindex failure leaves
the live index a mix of old-model and new-model vectors while every ``files``
row still says ``status='indexed'``.

Separately, ``require_model_ready`` (app.api.deps) answers 503 whenever
``_ready`` is False, with no notion that an OPEN staged rebuild leaves the
previous generation fully queryable — so a long rebuild takes chat/retrieval
down for its whole duration even though the old index is intact.

These checks are RED at master (HEAD 51cf9409):

1. A full reindex whose embedding identity changed (model id, prefix hash)
   while dims stayed equal (probe 4 == live 4) must OPEN a staged rebuild.
   RED: ``assert 0 == 1`` on ``begin_dimension_rebuild.await_count``.
2. Every per-file re-embed must be routed to the staged target — no
   live-table writes. RED: ``assert 0 == 2`` staged writes.
3. A per-file failure mid-rebuild must ABORT the staged table and never
   commit, keeping the prior generation intact. RED: ``assert 0 == 1`` on
   ``abort_dimension_rebuild.await_count``.
4. While a staged rebuild is open on the real VectorStore (previous
   generation fully queryable), ``require_model_ready`` must admit requests
   instead of 503ing. RED: ``assert 503 == 200``.

The stub store carries BOTH identity-change signals (not-ready flag, recorded
old-model metadata) with equal dims, so the fixture stays robust to whichever
signal the eventual fix consults.
"""

import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.api.deps import require_model_ready
from app.config import settings
from app.models.database import SQLiteConnectionPool, run_migrations
from app.services.background_tasks import BackgroundProcessor
from app.services.vector_store import VectorStore

LIVE_DIM = 4
PROBE_DIM = 4
OLD_MODEL = "old-model"
NEW_MODEL = "new-model"
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
        "VALUES ('b07_fixture.txt', ?, ?, 'indexed', 32)",
        (file_path, vault_id),
    )
    conn.commit()


class _ContractFaithfulEmbeddingService:
    """Mirrors EmbeddingService.embed_batch's real return contract."""

    def __init__(self):
        self.calls: list[tuple[tuple[str, ...], bool]] = []

    async def embed_batch(self, texts, batch_size=None, fail_fast=True):
        self.calls.append((tuple(texts), bool(fail_fast)))
        vectors = [[float(i)] * PROBE_DIM for i, _ in enumerate(texts)]
        if fail_fast:
            return vectors
        return vectors, []


class _SameDimModelChangeStore:
    """Live-table stub carrying BOTH same-dimension identity-change signals.

    The live table sits at dim 4 (== the probe dim) while its recorded
    identity is the OLD model and the store is not-ready — so whichever
    signal a staged-rebuild decision consults, this fixture reads as
    "identity changed, dimension did not".
    """

    def __init__(self):
        self._ready = False
        self.begin_dimension_rebuild = AsyncMock()
        self.commit_dimension_rebuild = AsyncMock()
        self.abort_dimension_rebuild = AsyncMock()
        self.record_embedding_metadata = AsyncMock()
        self.mark_ready = AsyncMock()

    async def get_live_embedding_dim(self):
        return LIVE_DIM

    async def get_embedding_metadata(self):
        return {
            "embedding_model_id": OLD_MODEL,
            "embedding_dim": LIVE_DIM,
            "embedding_prefix_hash": OLD_PREFIX_HASH,
        }


class _ProcessRecorder:
    """Stands in for DocumentProcessor.process_existing_file; records calls.

    ``fail_on_call=N`` raises RuntimeError on the Nth call so a mid-rebuild
    per-file failure can be injected (the seeded paths do not exist on disk,
    so the real processor is never able to run).
    """

    def __init__(self, fail_on_call: int | None = None):
        self.calls: list[dict] = []
        self._fail_on_call = fail_on_call

    async def __call__(self, file_id, file_path, vault_id, **kwargs):
        self.calls.append(
            {
                "file_id": file_id,
                "file_path": file_path,
                "vault_id": vault_id,
                "kwargs": kwargs,
            }
        )
        if self._fail_on_call is not None and len(self.calls) >= self._fail_on_call:
            raise RuntimeError(f"injected re-embed failure for file_id={file_id}")


# ---------------------------------------------------------------------------
# Minimal fake lancedb (self-contained copy of the test_issue513 shape —
# only the surface init_table / begin / abort consume)
# ---------------------------------------------------------------------------


class _FakeIndex:
    def __init__(self, name, columns=None, index_type=None):
        self.name = name
        self.columns = columns if columns is not None else []
        self.index_type = index_type if index_type is not None else ""


class _FakeTable:
    """Async lancedb table: index surface used by init/begin."""

    def __init__(self, name, schema):
        self.name = name
        self._schema = schema
        self.indices: list[_FakeIndex] = []

    async def list_indices(self):
        return list(self.indices)

    async def create_index(self, column=None, config=None, replace=False):
        # Shape-complete entry: index detection is by column+type (#557).
        kind = "FTS" if column == "text" else "IvfPq"
        self.indices.append(_FakeIndex(f"{column}_idx", [column], kind))
        return None


class _FakeDB:
    """Async lancedb connection: table_names/open/drop/create."""

    def __init__(self):
        self._tables: dict[str, _FakeTable] = {}

    async def table_names(self):
        return list(self._tables.keys())

    async def open_table(self, name):
        if name not in self._tables:
            raise RuntimeError(f"table {name} not found")
        return self._tables[name]

    async def drop_table(self, name):
        self._tables.pop(name, None)

    async def create_table(self, name, schema=None, data=None, mode="create"):
        table = _FakeTable(name, schema)
        self._tables[name] = table
        return table


@pytest.fixture()
def same_dim_harness(tmp_path):
    """Tests 1-3 harness — the b02 asyncSetUp body as a pytest fixture.

    Migrated DB + pool + BackgroundProcessor; the wrapped DocumentProcessor's
    services swapped for the contract-faithful embed service and the
    same-dim identity-change store; ``settings.embedding_model`` overridden
    (snapshot + restore) so identity differs while dims are equal; two
    indexed files seeded in vault 1 (paths intentionally absent on disk —
    ``process_existing_file`` is replaced by a recorder in each test).
    """
    db_path = tmp_path / "app.db"
    run_migrations(str(db_path))
    conn = _connect(str(db_path))
    pool = SQLiteConnectionPool(str(db_path), max_size=4)
    processor = BackgroundProcessor(pool=pool, retry_delay=0.05)
    emb = _ContractFaithfulEmbeddingService()
    store = _SameDimModelChangeStore()
    # _reindex_embed_all reads the wrapped DocumentProcessor's services.
    processor.processor.embedding_service = emb
    processor.processor.vector_store = store
    # Identity differs while dims are equal: the live table's recorded model
    # is OLD_MODEL (see _SameDimModelChangeStore) and the probe embeds at
    # dim 4 == live dim 4 — only the model id changed.
    orig_embedding_model = settings.embedding_model
    settings.embedding_model = NEW_MODEL
    _insert_indexed_file(conn, 1, "does/not/exist-b07-a.txt")
    _insert_indexed_file(conn, 1, "does/not/exist-b07-b.txt")
    try:
        yield SimpleNamespace(
            conn=conn,
            pool=pool,
            processor=processor,
            emb=emb,
            store=store,
        )
    finally:
        settings.embedding_model = orig_embedding_model
        pool.close_all()
        conn.close()


async def test_same_dim_model_change_opens_staged_rebuild(same_dim_harness):
    """A full reindex whose embedding identity changed (dims equal) must
    open a staged rebuild instead of rewriting the live table in place."""
    recorder = _ProcessRecorder()
    same_dim_harness.processor.processor.process_existing_file = recorder

    await same_dim_harness.processor._reindex_embed_all(  # noqa: SLF001
        1, vault_id=None
    )

    assert same_dim_harness.store.begin_dimension_rebuild.await_count == 1


async def test_same_dim_model_change_writes_only_to_staged_target(same_dim_harness):
    """Every per-file re-embed must carry ``vector_target`` — the staged
    rebuild handle — so the live (old-generation) table is never written
    mid-reindex."""
    recorder = _ProcessRecorder()
    same_dim_harness.processor.processor.process_existing_file = recorder

    await same_dim_harness.processor._reindex_embed_all(  # noqa: SLF001
        1, vault_id=None
    )

    staged_writes = sum(
        1
        for call in recorder.calls
        if call["kwargs"].get("vector_target") is not None
    )
    assert staged_writes == 2


async def test_same_dim_failure_aborts_and_keeps_prior_generation(same_dim_harness):
    """A per-file failure mid-reindex must ABORT the staged table (prior
    generation kept fully intact) and never commit the partial rebuild."""
    recorder = _ProcessRecorder(fail_on_call=2)
    same_dim_harness.processor.processor.process_existing_file = recorder

    await same_dim_harness.processor._reindex_embed_all(  # noqa: SLF001
        1, vault_id=None
    )

    assert same_dim_harness.store.abort_dimension_rebuild.await_count == 1
    assert same_dim_harness.store.commit_dimension_rebuild.await_count == 0


async def test_gate_serves_previous_generation_during_rebuild(tmp_path, monkeypatch):
    """The readiness gate must serve the previous generation while a staged
    rebuild is open (real VectorStore + fake lancedb — the test_issue513
    Rev-5a pattern); a not-ready flag with an open rebuild is not an outage."""
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "multi_scale_indexing_enabled", False)
    # A migrated app DB at the patched sqlite_path so the init-time
    # settings_kv metadata write stays hermetic.
    run_migrations(str(tmp_path / "app.db"))

    vs = VectorStore(db_path=tmp_path / "lancedb")
    vs.db = _FakeDB()
    await vs.init_table(8)

    # Not-ready (model identity changed) with a staged rebuild open: the
    # previous generation is fully queryable, so the gate must admit.
    await vs.mark_ready(False)
    handle = await vs.begin_dimension_rebuild(8)
    try:
        gate_status = 200
        try:
            require_model_ready(vs)
        except HTTPException as exc:
            gate_status = exc.status_code
        assert gate_status == 200
    finally:
        await vs.abort_dimension_rebuild(handle)
