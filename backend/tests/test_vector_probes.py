"""Preserving regression pins for the FTS probe and the parent-window probe
(issue #512 VECTOR-002 / VECTOR-003).

Registered ports of the frozen issue-tracer checks C8/C9 — these behaviors
were already correct at HEAD and must stay correct:

- C8: a second init_table run must NOT recreate the FTS index (the probe
  detects the existing 'fts_text' index), no FTS-unavailable warnings are
  emitted, and the fake FTS query still returns the ingested row.
- C9: has_parent_window_text_sample() returns True only when some row's
  metadata contains a parent_window_text; plain metadata and empty tables
  return False.
"""

import json
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import settings  # noqa: E402
from app.services.vector_store import VectorStore  # noqa: E402

EMBED_DIM = 8
FTS_NEEDLE = "zephyrquokka"


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


class FakeIndex:
    def __init__(self, name):
        self.name = name


class FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    async def to_list(self):
        return list(self._rows)


class FakeSearchBuilder:
    """search().where(expr, prefilter=True).limit(n) chain with a substring
    LIKE over the metadata column (frozen-check idiom)."""

    def __init__(self, table, query=None):
        self._table = table
        self._query = query
        self._predicate = None

    def where(self, expr, prefilter=None):
        self._predicate = expr
        return self

    async def limit(self, n):
        rows = self._table.rows
        pred = self._predicate
        if pred and "like" in str(pred).lower() and "metadata" in str(pred).lower():
            inner = str(pred)
            if "'%" in inner and "%'" in inner:
                start = inner.index("'%") + 2
                end = inner.rindex("%'")
                needle = inner[start:end]
                rows = [r for r in rows if needle in str(r.get("metadata", ""))]
        return FakeCursor(rows[:n])


class FakeArrowLikeField:
    def __init__(self, name, type=None, nullable=True):
        self.name = name
        self.type = type
        self.nullable = nullable


class FakeArrowLikeSchema:
    def __init__(self, names):
        self._fields = [FakeArrowLikeField(n) for n in names]
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


class FakeFTSTable:
    """Fake async lancedb table with an FTS-like search() surface."""

    def __init__(self, name, schema, rows=None):
        self.name = name
        self._schema = schema
        self.rows = list(rows or [])
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
        self.rows.extend(dict(r) for r in records)
        self.calls["add"].append(len(records))
        return None

    async def delete(self, where):
        self.calls["delete"].append(where)
        return None

    async def optimize(self):
        return None

    async def head(self, n):
        return self.rows[:n]

    def search(self, query=None, **kwargs):
        return FakeSearchBuilder(self, query)


class FakeDB:
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
        table = FakeFTSTable(name, schema)
        table.rows = rows
        self._tables[name] = table
        return table


def _make_schema():
    return FakeArrowLikeSchema(
        [
            "id", "text", "file_id", "vault_id", "chunk_index", "chunk_scale",
            "sparse_embedding", "metadata",
            "parent_doc_id", "parent_window_start", "parent_window_end",
            "chunk_position", "embedding",
        ]
    )


class _LogCollector(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.records = []

    def emit(self, record):
        self.records.append(record.getMessage())


class TestFTSProbeNoDuplicateReinit(unittest.IsolatedAsyncioTestCase):
    """C8 pin: init → ingest → re-init must create the FTS index exactly once."""

    async def test_reinit_does_not_recreate_fts_index(self):
        tmp = tempfile.mkdtemp(prefix="probe_c8_")

        # Keep record_embedding_metadata writes inside the temp dir.
        conn_db = Path(tmp) / "app.db"
        import sqlite3

        conn = sqlite3.connect(str(conn_db))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS settings_kv ("
            "key TEXT PRIMARY KEY, value TEXT, updated_at TIMESTAMP)"
        )
        conn.commit()
        conn.close()
        old_data_dir = settings.data_dir
        settings.data_dir = Path(tmp)

        fake_db = FakeDB()
        store = VectorStore(db_path=Path(tmp) / "lancedb")
        store.db = fake_db

        collector = _LogCollector()
        vs_logger = logging.getLogger("app.services.vector_store")
        vs_logger.addHandler(collector)
        try:
            # First init through the production path.
            await store.init_table(embedding_dim=EMBED_DIM)

            # Ingest one row through the production write path.
            record = {
                "id": "f1_0",
                "text": f"the {FTS_NEEDLE} contains the searchable body",
                "file_id": "f1",
                "vault_id": "1",
                "chunk_index": 0,
                "metadata": "{}",
                "parent_doc_id": "f1",
                "parent_window_start": 0,
                "parent_window_end": 40,
                "chunk_position": 0,
                "embedding": [0.05] * EMBED_DIM,
            }
            await store.add_chunks([record])

            # Reinit: a second init_table run must not recreate the FTS index.
            await store.init_table(embedding_dim=EMBED_DIM)
        finally:
            vs_logger.removeHandler(collector)
            settings.data_dir = old_data_dir

        table = fake_db._tables.get("chunks")
        self.assertIsNotNone(table)
        fts_creates = [
            c
            for c in (table.calls["create_index"] if table else [])
            if c["column"] == "text"
        ]
        self.assertEqual(
            len(fts_creates),
            1,
            f"create_index(column='text') called {len(fts_creates)} time(s) "
            f"across init+reinit (expected exactly 1)",
        )
        unavailable_warnings = [
            m
            for m in collector.records
            if "unavailable" in m.lower() or "fts index creation failed" in m.lower()
        ]
        self.assertEqual(unavailable_warnings, [])

        cursor = await table.search(FTS_NEEDLE).limit(5)
        rows = await cursor.to_list()
        self.assertTrue(rows, "FTS query must return the ingested row")


class TestParentWindowProbe(unittest.IsolatedAsyncioTestCase):
    """C9 pin: has_parent_window_text_sample() detects parent-window metadata."""

    async def test_probe_true_when_parent_window_text_present(self):
        tmp = tempfile.mkdtemp(prefix="probe_c9_")
        table = FakeFTSTable(
            "chunks",
            _make_schema(),
            [
                {
                    "id": "f1_0",
                    "text": "chunk text",
                    "metadata": json.dumps(
                        {
                            "parent_window_text": "broader parent context",
                            "source_file": "doc.pdf",
                        }
                    ),
                    "embedding": [0.1] * 8,
                }
            ],
        )
        store = VectorStore(db_path=Path(tmp) / "lancedb")
        store.table = table
        self.assertIs(await store.has_parent_window_text_sample(), True)

    async def test_probe_false_for_plain_metadata(self):
        tmp = tempfile.mkdtemp(prefix="probe_c9_")
        table = FakeFTSTable(
            "chunks",
            _make_schema(),
            [
                {
                    "id": "f2_0",
                    "text": "plain chunk",
                    "metadata": json.dumps({"source_file": "doc2.pdf"}),
                    "embedding": [0.2] * 8,
                }
            ],
        )
        store = VectorStore(db_path=Path(tmp) / "lancedb")
        store.table = table
        self.assertIs(await store.has_parent_window_text_sample(), False)

    async def test_probe_false_for_empty_table(self):
        tmp = tempfile.mkdtemp(prefix="probe_c9_")
        table = FakeFTSTable("chunks", _make_schema(), [])
        store = VectorStore(db_path=Path(tmp) / "lancedb")
        store.table = table
        self.assertIs(await store.has_parent_window_text_sample(), False)


if __name__ == "__main__":
    unittest.main()
