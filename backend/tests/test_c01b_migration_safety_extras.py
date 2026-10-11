"""C01b behavior tests (issue #706): the new migration-safety contracts.

Beyond the frozen test_c01_* acceptance checks, these pin the new behaviors the
fix introduces: dimension-rebuild admission serialization (waiter convergence in
both outcomes, the unchanged uncontended contract, commit-time identity
persistence and its failure path) and the extended reconcile divergence rule
(same-id content conflict, partial overlap, and the >=-branch silent drop).
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _hermetic_env() -> None:
    """Set test env BEFORE any app import (mirrors backend/tests/conftest.py)."""
    os.environ["ADMIN_SECRET_TOKEN"] = "test-secret"
    os.environ["USERS_ENABLED"] = "false"
    os.environ["JWT_SECRET_KEY"] = "test-jwt-secret-key-for-testing-only"
    os.environ["REDIS_URL"] = ""


_hermetic_env()

from app.config import settings  # noqa: E402
from app.services.vector_store import (  # noqa: E402
    CHUNKS_STAGING_TABLE,
    DimensionRebuildHandle,
    VectorStore,
    VectorStoreConnectionError,
    VectorStoreError,
)

OLD_DIM = 4
NEW_DIM = 6


def _record(rec_id: str, dim: int, text: str = "chunk text") -> dict:
    return {
        "id": rec_id,
        "text": text,
        "file_id": "1",
        "vault_id": "0",
        "chunk_index": 0,
        "chunk_scale": "default",
        "sparse_embedding": None,
        "metadata": "{}",
        "parent_doc_id": "1",
        "parent_window_start": 0,
        "parent_window_end": 0,
        "chunk_position": 0,
        "embedding": [((i + 1) * 0.25) for i in range(dim)],
    }


async def _make_store(tmp: Path, dim: int) -> VectorStore:
    store = VectorStore(db_path=tmp / "lancedb")
    await store.connect()
    await store.init_table(dim)
    return store


# ---------------------------------------------------------------------------
# Dimension-rebuild admission contracts
# ---------------------------------------------------------------------------


def test_waiter_converged_returns_none_and_bare_write_lands(tmp_path) -> None:
    """A cross-task waiter whose convergence check passes gets None (#706)."""

    async def scenario() -> None:
        store = await _make_store(tmp_path, OLD_DIM)
        try:
            handle_a = await store.begin_dimension_rebuild(NEW_DIM)
            assert isinstance(handle_a, DimensionRebuildHandle)

            waiter = asyncio.create_task(store.begin_dimension_rebuild(NEW_DIM))
            # Let the waiter enter cond.wait() deterministically.
            for _ in range(20):
                await asyncio.sleep(0)
                if not waiter.done():
                    break
            assert not waiter.done(), "waiter must block while the rebuild is open"

            await store.commit_dimension_rebuild(handle_a)
            result_b = await waiter
            assert result_b is None, "converged waiter must get None"

            live_dim = await store.get_live_embedding_dim()
            assert live_dim == NEW_DIM
            # A bare write against the converged live table succeeds.
            await store.add_chunks([_record("post-conv-1", NEW_DIM)])
            assert await store.count_by_file("1") >= 1
        finally:
            import shutil

            shutil.rmtree(tmp_path / "lancedb", ignore_errors=True)

    asyncio.run(scenario())


def test_waiter_not_converged_opens_own_rebuild(tmp_path) -> None:
    """A waiter whose convergence check fails opens its own rebuild (#706)."""

    async def scenario() -> None:
        store = await _make_store(tmp_path, OLD_DIM)
        try:
            handle_a = await store.begin_dimension_rebuild(NEW_DIM)
            # converged=False models a still-differing embedding IDENTITY —
            # the #696 same-dimension rebuild trigger.
            waiter = asyncio.create_task(
                store.begin_dimension_rebuild(NEW_DIM, converged=lambda: _false())
            )
            for _ in range(20):
                await asyncio.sleep(0)
                if not waiter.done():
                    break
            await store.commit_dimension_rebuild(handle_a)
            handle_b = await waiter
            assert isinstance(handle_b, DimensionRebuildHandle)
            await store.abort_dimension_rebuild(handle_b)
        finally:
            import shutil

            shutil.rmtree(tmp_path / "lancedb", ignore_errors=True)

    async def _false() -> bool:
        return False

    asyncio.run(scenario())


def test_uncontended_same_dim_begin_returns_handle(tmp_path) -> None:
    """The #696 same-dimension identity-rebuild contract is unchanged (#706)."""

    async def scenario() -> None:
        store = await _make_store(tmp_path, 8)
        try:
            handle = await store.begin_dimension_rebuild(8)
            assert isinstance(handle, DimensionRebuildHandle)
            assert handle.dim == 8
            await store.abort_dimension_rebuild(handle)
        finally:
            import shutil

            shutil.rmtree(tmp_path / "lancedb", ignore_errors=True)

    asyncio.run(scenario())


def test_commit_persists_job_start_identity(tmp_path, monkeypatch) -> None:
    """commit_dimension_rebuild writes handle.staged_metadata to the sidecar."""

    async def scenario(monkeypatch) -> dict:
        from app.models.database import init_db

        # sqlite_path is a derived read-only property (data_dir / "app.db"):
        # patch data_dir and init the DB at the derived location.
        monkeypatch.setattr(settings, "data_dir", tmp_path)
        db_path = tmp_path / "app.db"
        init_db(str(db_path))

        store = await _make_store(tmp_path, OLD_DIM)
        handle = await store.begin_dimension_rebuild(
            NEW_DIM,
            staged_metadata={"model_id": "model-at-job-start", "prefix_hash": "feedface"},
        )
        await store.add_chunks([_record("staged-1", NEW_DIM)], target=handle)
        await store.commit_dimension_rebuild(handle)

        conn = sqlite3.connect(str(db_path))
        try:
            rows = dict(
                conn.execute(
                    "SELECT key, value FROM settings_kv WHERE key LIKE 'embedding_%'"
                ).fetchall()
            )
        finally:
            conn.close()
        return rows

    rows = asyncio.run(scenario(monkeypatch))
    assert rows.get("embedding_model_id") == "model-at-job-start"
    assert rows.get("embedding_prefix_hash") == "feedface"
    assert rows.get("embedding_dim") == str(NEW_DIM)


def test_commit_metadata_failure_is_critical_but_commit_succeeds(
    tmp_path, monkeypatch, caplog
) -> None:
    """A sidecar write failure at commit logs CRITICAL and never fails the swap."""

    async def scenario(store, handle):
        await store.commit_dimension_rebuild(handle)

    async def main(monkeypatch, caplog):
        store = await _make_store(tmp_path, OLD_DIM)

        async def _failing_record(*args, **kwargs):
            raise sqlite3.OperationalError("disk simulated full")

        monkeypatch.setattr(store, "record_embedding_metadata", _failing_record)
        handle = await store.begin_dimension_rebuild(
            NEW_DIM, staged_metadata={"model_id": "m", "prefix_hash": "p"}
        )
        await store.add_chunks([_record("staged-1", NEW_DIM)], target=handle)
        with caplog.at_level(logging.CRITICAL, logger="app.services.vector_store"):
            await scenario(store, handle)  # must NOT raise
        return store

    store = asyncio.run(main(monkeypatch, caplog))
    assert any(
        record.levelno >= logging.CRITICAL and "metadata" in record.getMessage().lower()
        for record in caplog.records
    ), "commit-time sidecar failure must log CRITICAL"
    # The swap is durable and the admission slot is released (waiters notified).
    assert store._rebuild_slot_open is False


# ---------------------------------------------------------------------------
# Reconcile divergence contracts (extended beyond the frozen C2 scenario)
# ---------------------------------------------------------------------------


class _FakeTable:
    def __init__(self, rows: list[dict]):
        self.rows = rows

    async def count_rows(self, where=None):
        return len(self.rows)

    async def to_pandas(self):
        import pandas as pd

        return pd.DataFrame(self.rows)


class _FakeDB:
    def __init__(self, tables: dict):
        self.tables = dict(tables)

    async def table_names(self):
        return list(self.tables)

    async def open_table(self, name):
        return self.tables[name]

    async def drop_table(self, name):
        self.tables.pop(name, None)

    async def create_table(self, name, schema=None, data=None, mode="create"):
        self.tables[name] = _FakeTable(data.to_dict("records"))
        return self.tables[name]


def _store_with(db: _FakeDB) -> VectorStore:
    store = object.__new__(VectorStore)
    store.db = db
    store.table = db.tables.get("chunks")
    return store


def _drive_reconcile(tmp_path, monkeypatch, caplog, canonical_rows, staging_rows):
    db = _FakeDB(
        {
            "chunks": _FakeTable(canonical_rows),
            CHUNKS_STAGING_TABLE: _FakeTable(staging_rows),
        }
    )
    store = _store_with(db)
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    raised: list[BaseException] = []
    try:
        asyncio.run(
            store._reconcile_chunks_rebuild_state(migration_name="c01b probe")
        )
    except BaseException as exc:  # noqa: BLE001
        raised.append(exc)
    critical = any(
        record.levelno >= logging.CRITICAL for record in caplog.records
    )
    return db, raised, critical


def test_same_id_content_conflict_is_divergence(tmp_path, monkeypatch, caplog) -> None:
    """Overlapping id with different text must fail closed, tables untouched."""
    with caplog.at_level(logging.CRITICAL, logger="app.services.vector_store"):
        canonical = [
            _record("1_0", OLD_DIM, text="RE-INGESTED newer content"),
            _record("2_0", OLD_DIM),
        ]
        staging = [
            _record("1_0", OLD_DIM, text="stale original content"),
            _record("2_0", OLD_DIM),
            _record("3_0", OLD_DIM),
        ]
        db, raised, critical = _drive_reconcile(
            tmp_path, monkeypatch, caplog, canonical, staging
        )
    assert raised and isinstance(raised[0], VectorStoreError)
    assert critical
    # Neither table modified, both preserved.
    assert {row["id"] for row in db.tables["chunks"].rows} == {"1_0", "2_0"}
    assert CHUNKS_STAGING_TABLE in db.tables


def test_partial_overlap_with_canonical_only_ids_is_divergence(
    tmp_path, monkeypatch, caplog
) -> None:
    """Canonical-only ids must fail closed even with matching overlap content."""
    with caplog.at_level(logging.CRITICAL, logger="app.services.vector_store"):
        canonical = [_record("post-1", OLD_DIM), _record("keep-1", OLD_DIM)]
        staging = [_record("keep-1", OLD_DIM), _record("orig-2", OLD_DIM)]
        db, raised, critical = _drive_reconcile(
            tmp_path, monkeypatch, caplog, canonical, staging
        )
    assert raised and isinstance(raised[0], VectorStoreError)
    assert critical
    assert len(db.tables["chunks"].rows) == 2


def test_ge_branch_staging_only_ids_is_divergence(
    tmp_path, monkeypatch, caplog
) -> None:
    """canonical >= staging rows but staging holds unique ids: fail closed."""
    with caplog.at_level(logging.CRITICAL, logger="app.services.vector_store"):
        canonical = [
            _record("post-1", OLD_DIM),
            _record("post-2", OLD_DIM),
            _record("post-3", OLD_DIM),
        ]
        staging = [_record("orig-1", OLD_DIM), _record("orig-2", OLD_DIM)]
        db, raised, critical = _drive_reconcile(
            tmp_path, monkeypatch, caplog, canonical, staging
        )
    assert raised and isinstance(raised[0], VectorStoreError)
    assert critical
    assert CHUNKS_STAGING_TABLE in db.tables
    assert len(db.tables["chunks"].rows) == 3


def test_ge_branch_safe_drop_when_staging_is_subset(
    tmp_path, monkeypatch, caplog
) -> None:
    """Staging whose ids canonical already holds (content matching) is dropped."""
    with caplog.at_level(logging.INFO, logger="app.services.vector_store"):
        canonical = [
            _record("keep-1", OLD_DIM),
            _record("keep-2", OLD_DIM),
            _record("keep-3", OLD_DIM),
        ]
        staging = [_record("keep-2", OLD_DIM), _record("keep-3", OLD_DIM)]
        db, raised, _ = _drive_reconcile(
            tmp_path, monkeypatch, caplog, canonical, staging
        )
    assert not raised
    assert CHUNKS_STAGING_TABLE not in db.tables
    assert len(db.tables["chunks"].rows) == 3


# ---------------------------------------------------------------------------
# Server-side query() projections (PRR-017): the id-projection fast path and
# the bounded embedding sample must go through query().select().to_pandas()
# when the table offers it, and a projection that cannot answer row identity
# must be refused rather than guessed.
# ---------------------------------------------------------------------------


class _QueryBuilder:
    """Chained ``query()`` builder for ``_QueryingFakeTable``.

    Implements the exact surface the reconcile path drives: ``.where()``,
    ``.select([...])``, ``.to_pandas()`` (plus ``.limit()``/``.to_list()``
    for add_chunks' guard, unused here). Counts which projections were
    actually answered so tests can pin WHICH path ran.
    """

    def __init__(self, rows: list[dict], stats: dict, drop_id: bool = False):
        self._rows = rows
        self._stats = stats
        self._drop_id = drop_id
        self._where = None
        self._columns = None

    def where(self, predicate):  # noqa: ANN001, ANN202
        self._where = predicate
        return self

    def select(self, columns):  # noqa: ANN001, ANN202
        self._columns = list(columns)
        return self

    def limit(self, n):  # noqa: ANN001, ANN202
        return self

    def _filtered(self) -> list[dict]:
        rows = self._rows
        predicate = self._where
        if (
            predicate is not None
            and predicate.startswith("id = '")
            and predicate.endswith("'")
        ):
            wanted = predicate[len("id = '") : -1]
            rows = [row for row in rows if str(row.get("id")) == wanted]
        return rows

    async def to_list(self):  # noqa: ANN202
        return [dict(row) for row in self._filtered()]

    async def to_pandas(self):  # noqa: ANN202
        import pandas as pd

        columns = self._columns
        if columns is None:
            columns = list(self._rows[0].keys()) if self._rows else []
        if self._drop_id:
            columns = [name for name in columns if name != "id"]
        if self._columns == ["id"]:
            self._stats["select_id"] += 1
        elif self._columns == ["embedding"]:
            self._stats["select_embedding"] += 1
        return pd.DataFrame(
            [{c: row.get(c) for c in columns} for row in self._filtered()],
            columns=columns,
        )


class _QueryingFakeTable(_FakeTable):
    """``_FakeTable`` plus a real ``query()`` projection surface."""

    def __init__(self, rows: list[dict]):
        super().__init__(rows)
        self.query_stats = {"select_id": 0, "select_embedding": 0}

    def query(self) -> _QueryBuilder:
        return _QueryBuilder(self.rows, self.query_stats)


class _NoFallbackQueryTable(_QueryingFakeTable):
    """Querying table whose FULL-table load is poison: any use of the
    guarded pandas fallback fails the test outright."""

    async def to_pandas(self):  # noqa: ANN202
        raise AssertionError(
            "guarded full-table fallback used although query() projection "
            "is available"
        )


class _NoIdProjectionTable(_QueryingFakeTable):
    """Querying table whose projection never yields an 'id' column."""

    def query(self) -> _QueryBuilder:
        return _QueryBuilder(self.rows, self.query_stats, drop_id=True)


def _drive_reconcile_db(db: _FakeDB, tmp_path, monkeypatch) -> list:  # noqa: ANN001
    """Run the reconcile against a prebuilt FakeDB; returns raised errors."""
    store = _store_with(db)
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    raised: list[BaseException] = []
    try:
        asyncio.run(
            store._reconcile_chunks_rebuild_state(migration_name="c01b probe")
        )
    except BaseException as exc:  # noqa: BLE001
        raised.append(exc)
    return raised


def test_reconcile_ids_use_query_projection_never_fallback(
    tmp_path, monkeypatch, caplog
) -> None:
    """PRR-017: with query() available the id sets come from the server-side
    projection; the guarded full load is never touched."""
    with caplog.at_level(logging.INFO, logger="app.services.vector_store"):
        canonical = _NoFallbackQueryTable(
            [_record("k-1", OLD_DIM), _record("k-2", OLD_DIM)]
        )
        # Empty staging: a subset with NO overlap, so neither the content
        # check nor the generation check may load a frame — any to_pandas()
        # call above is the fallback being taken (and raises).
        staging = _NoFallbackQueryTable([])
        db = _FakeDB({"chunks": canonical, CHUNKS_STAGING_TABLE: staging})
        raised = _drive_reconcile_db(db, tmp_path, monkeypatch)
    assert not raised
    assert CHUNKS_STAGING_TABLE not in db.tables, "stale staging must be dropped"
    select_id_calls = (
        canonical.query_stats["select_id"] + staging.query_stats["select_id"]
    )
    assert select_id_calls == 2, (
        "both sides' id sets must be read via query().select(['id']) "
        f"(got {select_id_calls} projections)"
    )


def test_reconcile_generation_sample_uses_query_projection(
    tmp_path, monkeypatch, caplog
) -> None:
    """PRR-017/PRR-002: the bounded embedding sample is projected through
    query().where(...).select(['embedding']) — matching generation, staging
    dropped, no divergence raised."""
    with caplog.at_level(logging.INFO, logger="app.services.vector_store"):
        canonical = _QueryingFakeTable(
            [_record("k-1", OLD_DIM), _record("k-2", OLD_DIM)]
        )
        staging = _QueryingFakeTable([_record("k-1", OLD_DIM)])
        db = _FakeDB({"chunks": canonical, CHUNKS_STAGING_TABLE: staging})
        raised = _drive_reconcile_db(db, tmp_path, monkeypatch)
    assert not raised
    assert CHUNKS_STAGING_TABLE not in db.tables
    assert canonical.query_stats["select_id"] == 1
    assert staging.query_stats["select_id"] == 1
    # One overlapping sample id, projected on BOTH sides.
    assert canonical.query_stats["select_embedding"] == 1
    assert staging.query_stats["select_embedding"] == 1


def test_reconcile_refuses_projection_without_id_column(
    tmp_path, monkeypatch, caplog
) -> None:
    """A query()-capable table whose projection cannot answer row identity
    is refused with VectorStoreError — never reconciled by guess."""
    with caplog.at_level(logging.CRITICAL, logger="app.services.vector_store"):
        canonical = _NoIdProjectionTable([_record("k-1", OLD_DIM)])
        staging = _QueryingFakeTable([_record("k-1", OLD_DIM)])
        db = _FakeDB({"chunks": canonical, CHUNKS_STAGING_TABLE: staging})
        raised = _drive_reconcile_db(db, tmp_path, monkeypatch)
    assert raised and isinstance(raised[0], VectorStoreError)
    assert "no 'id' column" in str(raised[0])


# ---------------------------------------------------------------------------
# Commit-time identity snapshot: BOTH or NEITHER (PRR-021)
# ---------------------------------------------------------------------------


def _read_embedding_sidecar(db_path) -> dict:  # noqa: ANN001
    conn = sqlite3.connect(str(db_path))
    try:
        return dict(
            conn.execute(
                "SELECT key, value FROM settings_kv WHERE key LIKE 'embedding_%'"
            ).fetchall()
        )
    finally:
        conn.close()


def test_commit_partial_staged_metadata_skips_sidecar_and_warns(
    tmp_path, monkeypatch, caplog
) -> None:
    """A PARTIAL snapshot (model_id without prefix_hash) must NOT reach the
    settings_kv sidecar — persisting it would mix generations — and the
    skip must be visible as a WARNING (PRR-021)."""

    async def main(monkeypatch, caplog) -> tuple[dict, dict]:
        from app.models.database import init_db

        monkeypatch.setattr(settings, "data_dir", tmp_path)
        db_path = tmp_path / "app.db"
        init_db(str(db_path))

        store = await _make_store(tmp_path, OLD_DIM)
        handle = await store.begin_dimension_rebuild(
            NEW_DIM,
            staged_metadata={"model_id": "m"},  # no prefix_hash: partial
        )
        await store.add_chunks([_record("staged-1", NEW_DIM)], target=handle)
        before = _read_embedding_sidecar(db_path)
        with caplog.at_level(logging.WARNING, logger="app.services.vector_store"):
            await store.commit_dimension_rebuild(handle)  # must NOT raise
        after = _read_embedding_sidecar(db_path)
        return before, after

    before, after = asyncio.run(main(monkeypatch, caplog))
    # No sidecar write happened for the partial snapshot.
    assert after == before, (
        "commit with a partial staged_metadata must leave the embedding "
        f"sidecar untouched (before={before} after={after})"
    )
    assert after.get("embedding_model_id") != "m"
    # The skip is loud.
    assert any(
        record.levelno == logging.WARNING
        and record.name == "app.services.vector_store"
        and "incomplete" in record.getMessage()
        for record in caplog.records
    ), "skipping a partial identity snapshot must log a WARNING"


# ---------------------------------------------------------------------------
# begin_dimension_rebuild failure path: the admission slot must not strand
# waiters when the create fails after the slot was claimed (PRR-020).
# ---------------------------------------------------------------------------


def test_begin_dimension_rebuild_releases_slot_when_create_fails(
    tmp_path, monkeypatch
) -> None:
    """A create-table failure after the slot is claimed releases the slot,
    so a later begin is not stranded."""

    async def scenario() -> None:
        monkeypatch.setattr(settings, "data_dir", tmp_path)
        store = await _make_store(tmp_path, OLD_DIM)
        original_create = store.db.create_table

        async def _failing_create(*args, **kwargs):  # noqa: ANN001, ANN202
            raise RuntimeError("simulated create_table failure")

        store.db.create_table = _failing_create
        raised: BaseException | None = None
        try:
            await store.begin_dimension_rebuild(NEW_DIM)
        except BaseException as exc:  # noqa: BLE001
            raised = exc
        finally:
            store.db.create_table = original_create

        assert raised is not None
        assert isinstance(raised, VectorStoreConnectionError)
        # The slot was released — a later begin is not stranded by it.
        assert store._rebuild_slot_open is False
        assert store._rebuild_owner_task is None
        handle = await store.begin_dimension_rebuild(NEW_DIM)
        assert isinstance(handle, DimensionRebuildHandle)
        await store.abort_dimension_rebuild(handle)
        assert store._rebuild_slot_open is False

    asyncio.run(scenario())
