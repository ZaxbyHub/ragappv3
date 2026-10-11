"""C01 acceptance checks (issue #706, vector-store migration safety).

NO-OP migrations must not load the whole table.

A real index holds millions of rows. ``migrate_add_vault_id`` /
``migrate_add_chunk_scale`` / ``migrate_add_parent_window`` decide "nothing
to migrate" by loading EVERY row into pandas (``_safe_table_to_pandas`` →
``AsyncTable.to_pandas``) just to count nulls — an O(table) whole-table
load on every startup even when the migration is a pure no-op. The
acceptance: with a single fully-populated row in a real LanceDB table,
the migration must return 0 WITHOUT any whole-table load
(``AsyncTable.to_pandas`` class-level call count == 0; a count/null probe
that reads only what it needs is fine, a full-materialize is not).

Counting is done at the ``lancedb.table.AsyncTable.to_pandas`` CLASS
level because the migration opens its own table handle via
``db.open_table("chunks")`` — instance-level table patches are invisible
to it.
"""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _hermetic_env() -> None:
    os.environ["ADMIN_SECRET_TOKEN"] = "test-secret"
    os.environ["USERS_ENABLED"] = "false"
    os.environ["JWT_SECRET_KEY"] = "test-jwt-secret-key-for-testing-only"
    os.environ["REDIS_URL"] = ""


_hermetic_env()

import lancedb.table as lancedb_table_module  # noqa: E402

from app.config import settings  # noqa: E402
from app.services.vector_store import VectorStore  # noqa: E402

EMBED_DIM = 8


async def _seed_record(store: VectorStore) -> None:
    """Seed ONE fully-populated row — every backfilled column non-null so
    each migration under test is a genuine no-op."""
    record = {
        "id": "c01_noop_0",
        "text": "fully populated chunk",
        "file_id": "1",
        "vault_id": "0",
        "embedding": [0.01] * EMBED_DIM,
        "chunk_scale": "default",
        "parent_doc_id": "1",
        "parent_window_start": 0,
        "parent_window_end": 7,
        "chunk_position": 0,
        "chunk_index": 0,
        "sparse_embedding": None,
        "metadata": "{}",
    }
    await store.add_chunks([record])


async def _assert_seeded(store: VectorStore, expected: int) -> None:
    """Precondition: the seed really landed (sibling-harness idiom)."""
    seeded = await store.count_by_file("1")
    if seeded != expected:
        raise RuntimeError(f"harness invalid: seeded index has {seeded} rows")


async def _run_noop_migration(tmp_path, monkeypatch, migration: str):
    """Harness: real LanceDB store, one fully-populated row, counters
    installed around the migration call only.

    Returns (migration_return_value, whole_table_loads, guarded_loads)
    where whole_table_loads is the CLASS-level ``AsyncTable.to_pandas``
    call count and guarded_loads is the instance-level
    ``_safe_table_to_pandas`` call count (the fallback probe must not be
    needed when the server-side count answers).
    """
    monkeypatch.setattr(settings, "data_dir", tmp_path)

    store = VectorStore(db_path=str(Path(tmp_path) / "lancedb"))
    await store.connect()
    await store.init_table(EMBED_DIM)
    await _seed_record(store)
    await _assert_seeded(store, 1)

    # Counters installed AFTER seeding: only the migration's loads count.
    loads = {"n": 0}
    original_to_pandas = lancedb_table_module.AsyncTable.to_pandas

    async def _counting_to_pandas(self, *args, **kwargs):  # noqa: ANN001, ANN202
        loads["n"] += 1
        return await original_to_pandas(self, *args, **kwargs)

    monkeypatch.setattr(
        lancedb_table_module.AsyncTable, "to_pandas", _counting_to_pandas
    )

    safe_calls = {"n": 0}
    original_safe = store._safe_table_to_pandas

    async def _counting_safe(table, operation_name):  # noqa: ANN001, ANN202
        safe_calls["n"] += 1
        return await original_safe(table, operation_name)

    store._safe_table_to_pandas = _counting_safe

    migration_method = getattr(store, f"migrate_add_{migration}")
    ret = await migration_method()
    return ret, loads["n"], safe_calls["n"]


def test_vault_id_noop_does_not_load_table(tmp_path, monkeypatch) -> None:
    ret, whole_table_loads, guarded_loads = asyncio.run(
        _run_noop_migration(tmp_path, monkeypatch, "vault_id")
    )
    assert ret == 0
    assert whole_table_loads == 0
    # The server-side count answered; the guarded full-load fallback of
    # ``_count_null_rows`` was never needed on the no-op path.
    assert guarded_loads == 0


def test_chunk_scale_noop_does_not_load_table(tmp_path, monkeypatch) -> None:
    ret, whole_table_loads, guarded_loads = asyncio.run(
        _run_noop_migration(tmp_path, monkeypatch, "chunk_scale")
    )
    assert ret == 0
    assert whole_table_loads == 0
    assert guarded_loads == 0


def test_parent_window_noop_does_not_load_table(tmp_path, monkeypatch) -> None:
    ret, whole_table_loads, guarded_loads = asyncio.run(
        _run_noop_migration(tmp_path, monkeypatch, "parent_window")
    )
    assert ret == 0
    assert whole_table_loads == 0
    assert guarded_loads == 0


# ---------------------------------------------------------------------------
# Non-null branch: the server-side probe must SEE the nulls and the
# backfill must run (PRR-018). Real LanceDB only.
# ---------------------------------------------------------------------------


async def _run_null_backfill_migration(tmp_path, monkeypatch):
    """Harness: real LanceDB store, ONE row whose ``vault_id`` is NULL.

    A class-level ``AsyncTable.count_rows`` spy (installed after seeding,
    removed by monkeypatch at test end) records every ``filter=`` probe the
    migration makes and the count real LanceDB answered, so the test can
    assert the server-side null probe ran AND saw the null — not just that
    the end state is filled. Returns
    ``(migration_return_value, probe_saw_nulls, remaining_nulls,
    vault_values_after)``.
    """
    monkeypatch.setattr(settings, "data_dir", tmp_path)

    store = VectorStore(db_path=str(Path(tmp_path) / "lancedb"))
    await store.connect()
    await store.init_table(EMBED_DIM)
    record = {
        "id": "c01_null_0",
        "text": "chunk with a null vault_id",
        "file_id": "1",
        "vault_id": None,  # <- the NULL the probe must see
        "embedding": [0.25] * EMBED_DIM,
        "chunk_scale": "default",
        "parent_doc_id": "1",
        "parent_window_start": 0,
        "parent_window_end": 7,
        "chunk_position": 0,
        "chunk_index": 0,
        "sparse_embedding": None,
        "metadata": "{}",
    }
    await store.add_chunks([record])
    await _assert_seeded(store, 1)

    probes: dict = {}
    original_count_rows = lancedb_table_module.AsyncTable.count_rows

    async def _spying_count_rows(self, *args, **kwargs):  # noqa: ANN001, ANN202
        result = await original_count_rows(self, *args, **kwargs)
        flt = kwargs.get("filter")
        if flt is not None:
            probes[flt] = result
        return result

    monkeypatch.setattr(
        lancedb_table_module.AsyncTable, "count_rows", _spying_count_rows
    )

    ret = await store.migrate_add_vault_id()
    probe_saw_nulls = probes.get("vault_id IS NULL")

    # Post-state (spy still installed; re-reading only adds new evidence).
    table = await store.db.open_table("chunks")
    remaining_nulls = await table.count_rows(filter="vault_id IS NULL")
    df = await store.table.to_pandas()
    vault_values_after = df["vault_id"].tolist()
    return ret, probe_saw_nulls, remaining_nulls, vault_values_after


def test_vault_id_null_row_is_seen_by_probe_and_backfilled(
    tmp_path, monkeypatch
) -> None:
    """The server-side ``count_rows(filter="vault_id IS NULL")`` probe must
    answer > 0 against real LanceDB on a nullable column, and the migration
    must take the BACKFILL path (return > 0, fill the column) — pinning the
    non-zero branch of the probe (PRR-018)."""
    ret, probe_saw_nulls, remaining_nulls, vault_values_after = asyncio.run(
        _run_null_backfill_migration(tmp_path, monkeypatch)
    )
    # Real LanceDB answered the filter server-side and SAW the null.
    assert probe_saw_nulls == 1, (
        "the server-side null probe must run against real LanceDB and "
        f"return > 0; got {probe_saw_nulls!r}"
    )
    # The backfill path ran: the migration reports the migrated row...
    assert ret == 1
    # ...and the row's vault_id is actually filled afterwards.
    assert remaining_nulls == 0
    assert vault_values_after == [""]
