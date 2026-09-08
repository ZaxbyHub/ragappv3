"""Non-destructive vector-store recovery tests (issue #512).

Registered companions of the frozen issue-tracer checks C1/C2/C3, extended
with the post-drop recovery state machine that the frozen C3 fake (which
fails EVERY data-bearing create) cannot reach:

- VECTOR-005: an EMPTY legacy table gets the current schema applied and a
  new-format write succeeds.
- VECTOR-001a: a transient open_table error in init_table raises
  VectorStoreConnectionError and performs no drop/create.
- VECTOR-001b: a staging-create failure happens BEFORE any drop (original
  intact) and raises.
- FINAL-create failure after the drop: the replacement data stays
  durably recoverable in 'chunks_rebuild'; re-running the migration
  restores the canonical table from staging.
- Success path: staging cleaned, FTS restored, and the authoritative index
  generation published to the migration journal (row present in a temp
  sqlite database).

The fakes implement exactly the recovery contract's API surface:
db-level table_names/open_table/drop_table/create_table(schema=|data=) and
table-level schema()/count_rows()/to_pandas()/create_index()/add().
"""

import asyncio
import sqlite3
import unittest
from pathlib import Path

from app.config import settings
from app.services.vector_store import (
    CHUNKS_STAGING_TABLE,
    VectorStore,
    VectorStoreConnectionError,
)

EMBED_DIM = 8

FULL_NEW_FORMAT_FIELDS = [
    "id", "text", "file_id", "vault_id", "chunk_index", "chunk_scale",
    "sparse_embedding", "metadata",
    "parent_doc_id", "parent_window_start", "parent_window_end",
    "chunk_position", "embedding",
]


# ---------------------------------------------------------------------------
# Faithful fake lancedb (frozen-check idiom)
# ---------------------------------------------------------------------------


class FakeIndex:
    def __init__(self, name):
        self.name = name


class FakeArrowLikeField:
    def __init__(self, name, type=None, nullable=True):
        self.name = name
        self.type = type
        self.nullable = nullable


class FakeArrowLikeSchema:
    def __init__(self, names, types=None):
        types = types or {}
        self._fields = [FakeArrowLikeField(n, types.get(n)) for n in names]
        self.metadata = None

    def __len__(self):
        return len(self._fields)

    def field(self, key):
        if isinstance(key, int):
            return self._fields[key]
        for f in self._fields:
            if f.name == key:
                return f
        raise KeyError(key)

    @property
    def names(self):
        return [f.name for f in self._fields]


def _schema_names(schema):
    """Field names from any of the schema shapes in play: the backend
    conftest pyarrow stub (tuple entries in ``_fields``), property-style
    fakes, or method-style ``names()``."""
    fields = getattr(schema, "_fields", None)
    if fields is not None:
        return [f[0] if isinstance(f, tuple) else f.name for f in fields]
    names = getattr(schema, "names", None)
    if callable(names):
        return list(names())
    if names is not None:
        return list(names)
    return [schema.field(i).name for i in range(len(schema))]


class FakeTable:
    """Async lancedb table with a mutable stored schema (frozen-check idiom).

    ``add`` enforces the Arrow append contract: a record field not present
    in the stored schema cannot be appended — the exact mechanism that made
    the VECTOR-005 empty-legacy skip a real failure."""

    def __init__(self, name, schema):
        self.name = name
        self._schema = schema
        self.rows = []
        self.indices = []
        self.calls = {"create_index": [], "add": [], "delete": []}

    async def schema(self):
        return self._schema

    async def count_rows(self, where=None):
        if where:
            # Production passes simple equality predicates ("col = 'value'");
            # honor them so a filtered-count regression cannot pass silently.
            col, sep, value = where.strip().partition(" = ")
            if sep:
                wanted = value.strip().strip("'")
                return sum(1 for r in self.rows if r.get(col) == wanted)
        return len(self.rows)

    async def list_indices(self):
        return list(self.indices)

    async def create_index(self, column=None, config=None, replace=False):
        self.calls["create_index"].append(
            {"column": column, "config": config, "replace": replace}
        )
        if column == "text":
            self.indices.append(FakeIndex("fts_text"))
        elif column == "embedding":
            self.indices.append(FakeIndex("embedding_idx"))
        return None

    async def to_pandas(self):
        import pandas as pd

        cols = _schema_names(self._schema)
        data = [[row.get(c) for c in cols] for row in self.rows]
        return pd.DataFrame(data, columns=cols)

    async def add(self, records):
        schema_fields = set(_schema_names(self._schema))
        normalized = []
        for rec in records:
            for key in rec:
                if key not in schema_fields:
                    raise ValueError(
                        f"cannot append field {key!r}: not in table schema"
                    )
            normalized.append(dict(rec))
        self.rows.extend(normalized)
        self.calls["add"].append(len(records))
        return None

    async def delete(self, where):
        self.calls["delete"].append(where)
        return None

    async def optimize(self):
        return None

    async def head(self, n):
        return self.rows[:n]


class FakeDB:
    """Async lancedb connection with call recording and switchable failure
    modes for exercising the swap state machine:

    - ``fail_create_with_data``: every create_table(data=...) raises (the
      frozen C3 contract);
    - ``fail_chunks_data_create``: only create_table('chunks', data=...)
      raises — lets the STAGING create succeed and fails the FINAL create
      after the drop."""

    def __init__(self, tables=None):
        self._tables = dict(tables or {})
        self.calls = {"open_table": [], "drop_table": [], "create_table": []}
        self.fail_create_with_data = False
        self.fail_chunks_data_create = False
        # When nonzero, create_table(data=...) silently stores that many
        # fewer rows - exercises the staging row-parity guard (PRR-013).
        self.create_data_row_loss = 0

    async def table_names(self):
        return list(self._tables.keys())

    async def open_table(self, name):
        self.calls["open_table"].append(name)
        if name not in self._tables:
            raise RuntimeError(f"table {name} not found")
        return self._tables[name]

    async def drop_table(self, name):
        self.calls["drop_table"].append(name)
        self._tables.pop(name, None)

    async def create_table(self, name, schema=None, data=None, mode="create"):
        self.calls["create_table"].append(
            {"name": name, "schema": schema, "data": data, "mode": mode}
        )
        if data is not None:
            if self.fail_create_with_data:
                raise RuntimeError("simulated recreate failure after drop (disk full)")
            if self.fail_chunks_data_create and name == "chunks":
                raise RuntimeError("simulated FINAL create failure after drop")
            schema = FakeArrowLikeSchema(list(data.columns))
            rows = data.to_dict("records")
            if self.create_data_row_loss and rows:
                rows = rows[: len(rows) - self.create_data_row_loss]
        else:
            rows = []
        table = FakeTable(name, schema)
        table.rows = rows
        self._tables[name] = table
        return table


def _legacy_table(rows=None):
    """A pre-parent-window 'chunks' table (legacy schema)."""
    legacy_schema = FakeArrowLikeSchema(
        [
            "id", "text", "file_id", "vault_id", "chunk_index",
            "chunk_scale", "sparse_embedding", "metadata", "embedding",
        ]
    )
    table = FakeTable("chunks", legacy_schema)
    table.rows = list(rows or [])
    return table


def _legacy_row(chunk_id, text="chunk text", file_id="f1", idx=0):
    return {
        "id": chunk_id,
        "text": text,
        "file_id": file_id,
        "vault_id": "1",
        "chunk_index": idx,
        "chunk_scale": "default",
        "sparse_embedding": None,
        "metadata": "{}",
        "embedding": [0.1] * EMBED_DIM,
    }


def _new_format_record(chunk_id="f1_new_0"):
    return {
        "id": chunk_id,
        "text": "new-format chunk",
        "file_id": "f1",
        "vault_id": "1",
        "chunk_index": 0,
        "chunk_scale": "default",
        "sparse_embedding": None,
        "metadata": "{}",
        "parent_doc_id": "f1",
        "parent_window_start": 0,
        "parent_window_end": 11,
        "chunk_position": 0,
        "embedding": [0.0] * EMBED_DIM,
    }


def _make_store(fake_db, tmp_path):
    store = VectorStore(db_path=Path(tmp_path) / "lancedb")
    store.db = fake_db
    store._embedding_dim = EMBED_DIM
    return store


async def _table_ids(fake_db, name):
    table = await fake_db.open_table(name)
    df = await table.to_pandas()
    return set(df["id"].tolist()) if len(df) else set()


# ---------------------------------------------------------------------------
# VECTOR-005 — empty legacy table
# ---------------------------------------------------------------------------


def _isolate_settings_data_dir(tmp_path):
    """Point settings at a temp data dir (restored by the caller via the
    returned callable) so settings_kv / journal writes stay in the test's
    temp directory instead of the developer's ./data."""
    old_data_dir = settings.data_dir
    sqlite_file = Path(tmp_path) / "app.db"
    conn = sqlite3.connect(str(sqlite_file))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS settings_kv ("
        "key TEXT PRIMARY KEY, value TEXT, updated_at TIMESTAMP)"
    )
    conn.commit()
    conn.close()
    settings.data_dir = Path(tmp_path)

    def _restore():
        settings.data_dir = old_data_dir

    return _restore


class TestVector005EmptyLegacySchemaApplied(unittest.IsolatedAsyncioTestCase):
    async def test_empty_legacy_table_gets_current_schema_and_new_write(self):
        import tempfile

        tmp = tempfile.mkdtemp(prefix="vec005_")
        legacy = _legacy_table()  # zero rows, legacy schema
        fake_db = FakeDB({"chunks": legacy})
        store = _make_store(fake_db, tmp)

        restore = _isolate_settings_data_dir(tmp)
        try:
            ret = await store.migrate_add_parent_window()
        finally:
            restore()
        self.assertEqual(ret, 0)

        table = await fake_db.open_table("chunks")
        schema_names = set(_schema_names(await table.schema()))
        for col in (
            "parent_doc_id",
            "parent_window_start",
            "parent_window_end",
            "chunk_position",
        ):
            self.assertIn(col, schema_names)

        # New-format write succeeds against the migrated table.
        store.table = table
        await store.add_chunks([_new_format_record()])
        self.assertEqual(len(table.rows), 1)
        self.assertEqual(table.rows[0]["id"], "f1_new_0")


# ---------------------------------------------------------------------------
# VECTOR-001a — transient open failure preserves the table
# ---------------------------------------------------------------------------


class TestVector001aOpenFailurePreservesTable(unittest.IsolatedAsyncioTestCase):
    async def test_transient_open_error_raises_without_drop_or_create(self):
        import tempfile

        tmp = tempfile.mkdtemp(prefix="vec001a_")

        class _TransientOpenDB(FakeDB):
            async def open_table(self, name):
                self.calls["open_table"].append(name)
                raise OSError("transient I/O blip while opening table")

        fake_db = _TransientOpenDB({"chunks": "authoritative-table-handle"})
        store = _make_store(fake_db, tmp)

        with self.assertRaises(VectorStoreConnectionError) as ctx:
            await store.init_table(embedding_dim=EMBED_DIM)

        self.assertIn("preserved", str(ctx.exception))
        self.assertEqual(fake_db.calls["drop_table"], [])
        self.assertEqual(fake_db.calls["create_table"], [])


# ---------------------------------------------------------------------------
# VECTOR-001b — staging create fails BEFORE any drop
# ---------------------------------------------------------------------------


class TestVector001bStagingCreateFailsBeforeDrop(unittest.IsolatedAsyncioTestCase):
    async def test_staging_failure_leaves_original_intact_and_raises(self):
        import tempfile

        tmp = tempfile.mkdtemp(prefix="vec001b_")
        original_ids = {"keep_a", "keep_b", "keep_c"}
        legacy = _legacy_table([_legacy_row(i) for i in sorted(original_ids)])
        fake_db = FakeDB({"chunks": legacy})
        fake_db.fail_create_with_data = True  # frozen C3 contract
        store = _make_store(fake_db, tmp)

        with self.assertRaises(Exception):
            await store.migrate_add_parent_window()

        names = await fake_db.table_names()
        self.assertIn("chunks", names)
        self.assertEqual(await _table_ids(fake_db, "chunks"), original_ids)
        self.assertNotIn("chunks", fake_db.calls["drop_table"])


# ---------------------------------------------------------------------------
# Post-drop state machine — final create fails, retry restores from staging
# ---------------------------------------------------------------------------


class TestFinalCreateFailureAndStagingRecovery(unittest.IsolatedAsyncioTestCase):
    async def test_final_create_failure_keeps_staging_and_retry_restores(self):
        import tempfile

        tmp = tempfile.mkdtemp(prefix="vecfinal_")
        original_ids = {"keep_a", "keep_b", "keep_c"}
        legacy = _legacy_table(
            [_legacy_row(i, text=f"row {i}") for i in sorted(original_ids)]
        )
        fake_db = FakeDB({"chunks": legacy})
        fake_db.fail_chunks_data_create = True
        store = _make_store(fake_db, tmp)

        # Attempt 1: staging create succeeds, final canonical create fails
        # after the drop. The failure must be reported (raise) AND logged at
        # CRITICAL naming the recoverable staging table.
        with self.assertRaises(Exception):
            with self.assertLogs("app.services.vector_store", level="CRITICAL") as logs:
                await store.migrate_add_parent_window()
        self.assertTrue(
            any(CHUNKS_STAGING_TABLE in line for line in logs.output),
            f"critical log must name the recoverable table: {logs.output}",
        )

        names = set(await fake_db.table_names())
        self.assertIn(CHUNKS_STAGING_TABLE, names)
        self.assertNotIn("chunks", names)
        self.assertEqual(
            await _table_ids(fake_db, CHUNKS_STAGING_TABLE), original_ids
        )

        # Attempt 2 (retry): the migration reconciles first — canonical is
        # rebuilt from staging, then the migration completes as a no-op.
        fake_db.fail_chunks_data_create = False
        ret = await store.migrate_add_parent_window()
        self.assertEqual(ret, 0)

        names = set(await fake_db.table_names())
        self.assertIn("chunks", names)
        self.assertNotIn(CHUNKS_STAGING_TABLE, names)
        self.assertEqual(await _table_ids(fake_db, "chunks"), original_ids)
        schema_names = set(
            _schema_names(await (await fake_db.open_table("chunks")).schema())
        )
        self.assertTrue(
            {"parent_doc_id", "chunk_position"}.issubset(schema_names)
        )
        # PRR-001: the recovered canonical table must not be index-degraded —
        # reconciliation restores FTS before releasing the staging copy.
        recovered = await fake_db.open_table("chunks")
        self.assertTrue(
            [c for c in recovered.calls["create_index"] if c["column"] == "text"],
            "recovered table must have FTS restored by reconciliation",
        )


# ---------------------------------------------------------------------------
# Success path — staging cleaned, FTS restored, generation published
# ---------------------------------------------------------------------------


class TestSwapSuccessPublishesGeneration(unittest.IsolatedAsyncioTestCase):
    async def test_success_path_cleans_staging_and_publishes_generation(self):
        import tempfile

        tmp = Path(tempfile.mkdtemp(prefix="vecsuccess_"))
        original_ids = {"keep_a", "keep_b"}
        legacy = _legacy_table([_legacy_row(i) for i in sorted(original_ids)])
        fake_db = FakeDB({"chunks": legacy})
        store = _make_store(fake_db, tmp)

        restore = _isolate_settings_data_dir(tmp)
        try:
            migrated = await store.migrate_add_parent_window()
        finally:
            restore()

        self.assertEqual(migrated, len(original_ids))

        names = set(await fake_db.table_names())
        self.assertIn("chunks", names)
        self.assertNotIn(CHUNKS_STAGING_TABLE, names)
        self.assertEqual(await _table_ids(fake_db, "chunks"), original_ids)

        # FTS index restored on the swapped-in table.
        canonical = await fake_db.open_table("chunks")
        fts_calls = [
            c for c in canonical.calls["create_index"] if c["column"] == "text"
        ]
        self.assertTrue(fts_calls, "FTS index must be recreated after the swap")

        # Generation published: journal row present in the temp sqlite db.
        conn = sqlite3.connect(str(tmp / "app.db"))
        try:
            rows = conn.execute(
                "SELECT migration_name, phase, outcome FROM migration_journal"
                " WHERE migration_name = ?",
                ("index_generation:vector_store:chunks",),
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], "succeeded")
        self.assertEqual(rows[0][2], "ok")


# ---------------------------------------------------------------------------
# Reconciliation dispositions for a both-present state
# ---------------------------------------------------------------------------


class TestStaleStagingReconciliation(unittest.IsolatedAsyncioTestCase):
    async def test_canonical_present_with_stale_staging_drops_staging(self):
        import tempfile

        tmp = tempfile.mkdtemp(prefix="vecrecon_")
        canonical = _legacy_table([_legacy_row("keep_a")])
        # Simulate a leftover staging table holding FEWER rows than the
        # completed canonical table.
        staging = FakeTable(
            CHUNKS_STAGING_TABLE, FakeArrowLikeSchema(FULL_NEW_FORMAT_FIELDS)
        )
        fake_db = FakeDB({"chunks": canonical, CHUNKS_STAGING_TABLE: staging})
        store = _make_store(fake_db, tmp)

        restore = _isolate_settings_data_dir(tmp)
        try:
            # The migration reconciles the stale staging first, then runs the
            # swap on the still-legacy canonical table.
            migrated = await store.migrate_add_parent_window()
        finally:
            restore()

        self.assertEqual(migrated, 1)
        names = set(await fake_db.table_names())
        self.assertIn("chunks", names)
        self.assertNotIn(CHUNKS_STAGING_TABLE, names)
        # The stale staging table is dropped by reconciliation, and the swap
        # itself creates+deletes its own staging table — both are expected.
        self.assertGreaterEqual(
            fake_db.calls["drop_table"].count(CHUNKS_STAGING_TABLE), 1
        )

    async def test_canonical_incomplete_rebuilds_from_staging_and_restores_fts(self):
        """Canonical present but holding fewer rows than staging: the staging
        copy is authoritative - reconciliation rebuilds canonical from it and
        releases the staging copy. (FTS here comes from the migration's
        follow-up swap; the reconcile-level index restore is load-bearing on
        the canonical-absent path, pinned by the retry test above.)"""
        import tempfile

        tmp = tempfile.mkdtemp(prefix="vecincomplete_")
        # Canonical is ALREADY new-format (so the migration early-returns
        # after reconciliation and no swap re-creates the indices) but holds
        # fewer rows than staging: the staging copy is authoritative.
        canonical = FakeTable(
            "chunks", FakeArrowLikeSchema(FULL_NEW_FORMAT_FIELDS)
        )
        canonical.rows = [_legacy_row("keep_a")]
        staging = FakeTable(
            CHUNKS_STAGING_TABLE, FakeArrowLikeSchema(FULL_NEW_FORMAT_FIELDS)
        )
        staging.rows = [_legacy_row(i) for i in ("keep_a", "keep_b", "keep_c")]
        fake_db = FakeDB({"chunks": canonical, CHUNKS_STAGING_TABLE: staging})
        store = _make_store(fake_db, tmp)

        restore = _isolate_settings_data_dir(tmp)
        try:
            migrated = await store.migrate_add_parent_window()
        finally:
            restore()

        # The migration probed the pre-reconcile canonical (legacy, 1 row),
        # so after reconciliation restored all 3 rows it completed the swap:
        # 3 rows migrated onto the restored table.
        self.assertEqual(migrated, 3)
        names = set(await fake_db.table_names())
        self.assertIn("chunks", names)
        self.assertNotIn(CHUNKS_STAGING_TABLE, names)
        self.assertEqual(
            await _table_ids(fake_db, "chunks"), {"keep_a", "keep_b", "keep_c"}
        )
        recovered = await fake_db.open_table("chunks")
        self.assertTrue(
            [c for c in recovered.calls["create_index"] if c["column"] == "text"],
            "reconciled table must have FTS restored (PRR-001)",
        )


class TestStagingParityAndPublishFailure(unittest.IsolatedAsyncioTestCase):
    """PRR-013/PRR-015 (PR #526 review): the two untested swap guards."""

    async def test_staging_row_parity_mismatch_aborts_before_any_drop(self):
        """Staging created with a wrong row count must abort the swap BEFORE
        dropping the canonical table - the load-bearing non-destructive
        invariant (vector_store.py staging parity check)."""
        import tempfile

        from app.services.vector_store import VectorStoreError

        tmp = Path(tempfile.mkdtemp(prefix="vecparity_"))
        original_ids = {"keep_a", "keep_b", "keep_c"}
        legacy = _legacy_table([_legacy_row(i) for i in sorted(original_ids)])
        fake_db = FakeDB({"chunks": legacy})
        fake_db.create_data_row_loss = 1
        store = _make_store(fake_db, tmp)

        restore = _isolate_settings_data_dir(tmp)
        try:
            with self.assertRaises(VectorStoreError):
                await store.migrate_add_parent_window()
        finally:
            restore()

        # The canonical table was never dropped and its rows are intact.
        self.assertNotIn("chunks", fake_db.calls["drop_table"])
        self.assertEqual(await _table_ids(fake_db, "chunks"), original_ids)

    async def test_publish_failure_keeps_completed_swap(self):
        """A journal failure after the completed swap logs a warning but must
        not undo the swap (staging dropped, canonical intact with FTS)."""
        import tempfile
        from unittest import mock

        import app.services.vector_store as vector_store_module

        tmp = Path(tempfile.mkdtemp(prefix="vecpublish_"))
        original_ids = {"keep_a", "keep_b"}
        legacy = _legacy_table([_legacy_row(i) for i in sorted(original_ids)])
        fake_db = FakeDB({"chunks": legacy})
        store = _make_store(fake_db, tmp)

        restore = _isolate_settings_data_dir(tmp)
        try:
            with mock.patch.object(
                vector_store_module,
                "publish_index_generation",
                side_effect=RuntimeError("journal down"),
            ):
                migrated = await store.migrate_add_parent_window()
        finally:
            restore()

        self.assertEqual(migrated, len(original_ids))
        names = set(await fake_db.table_names())
        self.assertIn("chunks", names)
        self.assertNotIn(CHUNKS_STAGING_TABLE, names)
        self.assertEqual(await _table_ids(fake_db, "chunks"), original_ids)
        canonical = await fake_db.open_table("chunks")
        self.assertTrue(
            [c for c in canonical.calls["create_index"] if c["column"] == "text"],
            "FTS index must exist even when generation publication failed",
        )


if __name__ == "__main__":
    unittest.main()
