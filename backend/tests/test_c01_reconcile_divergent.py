"""C01 acceptance check (issue #706, vector-store migration safety).

DIVERGENT canonical/staging reconciliation must not silently drop rows.

Scenario: the canonical ``chunks`` table holds NEWER fully-migrated data
(2 rows, ``post-1``/``post-2`` — every column populated, ``vault_id``
non-null) while a leftover ``chunks_rebuild`` staging table holds OLDER
data (3 rows, ``orig-1``/``orig-2``/``orig-3``). Row count alone cannot
tell which side is authoritative: the canonical table legitimately holds
fewer rows after vault deletions. At base,
``_reconcile_chunks_rebuild_state`` treats "canonical rows < staging
rows" as "canonical incomplete", drops the canonical table and rebuilds
it from the stale staging copy — ``post-1``/``post-2`` vanish with no
CRITICAL log and no exception raised: silent data loss.

Acceptance: after ``store.migrate_add_vault_id()`` either every row from
BOTH tables is still present in the final ``chunks`` table, or the loss
was detected loudly (a CRITICAL record logged AND an exception raised).
Silently keeping only staging's rows is a failure.

Fake idiom: ``FakeDB``/``FakeTable`` from ``tests/test_vector_recovery.py``
(exact shapes, including ``schema()`` so the migration's column check
works).
"""

import asyncio
import logging
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _hermetic_env() -> None:
    os.environ["ADMIN_SECRET_TOKEN"] = "test-secret"
    os.environ["USERS_ENABLED"] = "false"
    os.environ["JWT_SECRET_KEY"] = "test-jwt-secret-key-for-testing-only"
    os.environ["REDIS_URL"] = ""


_hermetic_env()

from app.config import settings  # noqa: E402
from app.services.vector_store import CHUNKS_STAGING_TABLE, VectorStore  # noqa: E402

EMBED_DIM = 8

FULL_NEW_FORMAT_FIELDS = [
    "id", "text", "file_id", "vault_id", "chunk_index", "chunk_scale",
    "sparse_embedding", "metadata",
    "parent_doc_id", "parent_window_start", "parent_window_end",
    "chunk_position", "embedding",
]


# ---------------------------------------------------------------------------
# Faithful fake lancedb (frozen-check idiom, tests/test_vector_recovery.py)
# ---------------------------------------------------------------------------


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
    """Async lancedb table with a mutable stored schema (frozen idiom)."""

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
        return None

    async def to_pandas(self):
        import pandas as pd

        cols = _schema_names(self._schema)
        data = [[row.get(c) for c in cols] for row in self.rows]
        return pd.DataFrame(data, columns=cols)

    async def add(self, records):
        schema_fields = set(_schema_names(self._schema))
        for rec in records:
            for key in rec:
                if key not in schema_fields:
                    raise ValueError(
                        f"cannot append field {key!r}: not in table schema"
                    )
        self.rows.extend(dict(rec) for rec in records)
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
    """Async lancedb connection with call recording."""

    def __init__(self, tables=None):
        self._tables = dict(tables or {})
        self.calls = {"open_table": [], "drop_table": [], "create_table": []}

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
            schema = FakeArrowLikeSchema(list(data.columns))
            rows = data.to_dict("records")
        else:
            rows = []
        table = FakeTable(name, schema)
        table.rows = rows
        self._tables[name] = table
        return table


# ---------------------------------------------------------------------------
# Scenario
# ---------------------------------------------------------------------------


def _full_row(chunk_id: str, text: str) -> dict:
    """A fully-populated current-schema row (every migration's no-op shape:
    vault_id / chunk_scale / parent columns all non-null)."""
    return {
        "id": chunk_id,
        "text": text,
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
        "embedding": [0.1] * EMBED_DIM,
    }


def _isolate_settings_data_dir(tmp_path):
    """Keep settings_kv / journal writes inside the test's temp dir (idiom
    from tests/test_vector_recovery.py)."""
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


async def _table_ids(fake_db, name):
    table = await fake_db.open_table(name)
    df = await table.to_pandas()
    return set(df["id"].tolist()) if len(df) else set()


def test_divergent_canonical_and_staging_not_silently_dropped(
    tmp_path, caplog
) -> None:
    import tempfile

    tmp = tempfile.mkdtemp(prefix="c01div_", dir=str(tmp_path))

    # Canonical: NEWER data, 2 fully-migrated rows.
    canonical = FakeTable("chunks", FakeArrowLikeSchema(FULL_NEW_FORMAT_FIELDS))
    canonical.rows = [
        _full_row("post-1", "newer canonical row one"),
        _full_row("post-2", "newer canonical row two"),
    ]
    # Staging: OLDER data, 3 rows — divergent, not a strict subset story.
    staging = FakeTable(
        CHUNKS_STAGING_TABLE, FakeArrowLikeSchema(FULL_NEW_FORMAT_FIELDS)
    )
    staging.rows = [
        _full_row("orig-1", "older staging row one"),
        _full_row("orig-2", "older staging row two"),
        _full_row("orig-3", "older staging row three"),
    ]
    fake_db = FakeDB({"chunks": canonical, CHUNKS_STAGING_TABLE: staging})
    store = VectorStore(db_path=Path(tmp) / "lancedb")
    store.db = fake_db
    store._embedding_dim = EMBED_DIM

    restore = _isolate_settings_data_dir(tmp)
    raised: BaseException | None = None
    try:
        with caplog.at_level(logging.CRITICAL, logger="app.services.vector_store"):
            try:
                asyncio.run(store.migrate_add_vault_id())
            except BaseException as exc:  # noqa: BLE001
                raised = exc
    finally:
        restore()

    final_ids = asyncio.run(_table_ids(fake_db, "chunks"))
    all_ids = {"post-1", "post-2", "orig-1", "orig-2", "orig-3"}
    all_present = all_ids.issubset(final_ids)
    critical_logged = any(
        record.levelno >= logging.CRITICAL for record in caplog.records
    )
    ok = all_present or (critical_logged and raised is not None)
    assert ok is True
