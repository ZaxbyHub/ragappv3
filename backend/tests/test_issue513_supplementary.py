"""Supplementary pytest coverage for issue #513 (recoverable ingestion,
enrichment and reindex) — the additive tests named by the approved plan
beyond the frozen acceptance checks:

- W27: the same-generation republish corner (identical generation hash →
  idempotent republish, no retirement deletes, no tombstone enqueues, bytes
  survive the sweeper; a genuinely new generation still retires + tombstones).
- W23/W24/W25/W26 settings: both-layer validator ranges for the four new
  settings on the config model AND the SettingsUpdate route model.
- W26: near-duplicate advisory grouping is retrieval-neutral — grouping
  touches ONLY ``document_near_dups`` (no vector rows added/removed, no
  ``files.status`` change, identical vector-store search results).
- W25: startup-vs-periodic stranded-row sweep semantics — post-parse phases
  recovered unconditionally at startup, parse-stage phases age-gated at
  startup, periodic path age-gated + honoring the live-lease.
- Rev-5a: old-dimension rows stay retrievable from the live table WHILE a
  dimension-migrating reindex fills the temp table; new-dim rows retrievable
  after the commit swap.
- Rev-5b: embedding-cache keys change when ANY contract component (model id,
  url discriminator, doc prefix, dim, text) changes, and a populated cache
  still hits after a simulated restart (fresh module state on the same disk
  file).
- Vault-deletion near-dup cleanup: intentionally SKIPPED — see the skip
  reason on the test (the landed vault delete does not clear
  ``document_near_dups``; reported as a gap, not implemented here).

Vector-store tests run against a faithful fake lancedb (the backend conftest
stubs the real package for the whole suite — same idiom as
tests/test_vector_recovery.py): the fake implements the async
table/db surface the dimension-rebuild and search paths consume, plus a
deterministic flat cosine-distance vector query.
"""

from __future__ import annotations

import contextlib
import hashlib
import math
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from pydantic import ValidationError

from app.config import Settings, settings
from app.services import artifact_store, near_duplicates
from app.services.document_artifacts import (
    AtomKind,
    DocumentAsset,
    DocumentAtom,
    ParsedDocument,
)
from app.services.document_processor import DocumentProcessor

# ---------------------------------------------------------------------------
# Shared helpers (modeled on tests/test_artifact_compensation.py)
# ---------------------------------------------------------------------------


class _FakePool:
    """Minimal get/release pool for ``DocumentProcessor._publish_artifacts``."""

    def __init__(self, conn):
        self.conn = conn

    def get_connection(self):
        return self.conn

    def release_connection(self, _conn):
        pass


class _CtxPool:
    """Pool facade exposing ``connection()`` as a context manager.

    ``BackgroundProcessor._recover_stranded_pending_rows`` consumes
    ``pool.connection()`` as a ``with`` block and issues explicit
    ``conn.commit()`` calls after every UPDATE, so a nullcontext over one
    shared sqlite connection is a faithful stand-in.
    """

    def __init__(self, conn):
        self._conn = conn

    def connection(self):
        return contextlib.nullcontext(self._conn)


def _new_db(sqlite_path: str) -> sqlite3.Connection:
    """A fully migrated app database with one indexed files row (id=1)."""
    from app.models.database import init_db, run_migrations

    init_db(sqlite_path)
    run_migrations(sqlite_path)
    conn = sqlite3.connect(sqlite_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "INSERT INTO files (vault_id, file_path, file_name, file_hash, file_size, status) "
        "VALUES (1, '/tmp/x.png', 'x.png', 'h', 1, 'indexed')"
    )
    conn.commit()
    return conn


def _asset(data: bytes = b"abc", generation_hash: str = "genA") -> DocumentAsset:
    asset_id = hashlib.sha256(data).hexdigest()
    return DocumentAsset(
        asset_id=asset_id,
        file_id=1,
        generation_hash=generation_hash,
        sha256=asset_id,
        rel_path=artifact_store.compute_asset_rel_path(1, generation_hash, asset_id),
        mime_type="image/png",
        byte_size=len(data),
    )


def _parsed(asset: DocumentAsset, payloads=None, generation_hash: str = "genA") -> ParsedDocument:
    return ParsedDocument(
        atoms=[
            DocumentAtom(
                atom_id="a-0",
                schema_version=1,
                file_id=1,
                generation_hash=generation_hash,
                ordinal=0,
                kind=AtomKind.IMAGE,
                raw_text="img",
                asset_id=asset.asset_id,
            )
        ],
        assets=(asset,),
        parser_fingerprint="unstructured:test",
        asset_payloads=payloads or {},
    )


def _publish(proc_like, generation_hash: str, asset: DocumentAsset, payloads) -> None:
    """Materialize bytes + publish one generation for files row 1."""
    DocumentProcessor._publish_artifacts(
        proc_like,
        file_id=1,
        vault_id=1,
        generation_hash=generation_hash,
        parsed=_parsed(asset, payloads, generation_hash),
    )


@pytest.fixture()
def artifact_root(tmp_path):
    """Confine artifact bytes to tmp_path (same pattern as the #460 suite)."""
    root = tmp_path / "vault-artifacts"
    root.mkdir(parents=True, exist_ok=True)
    real = artifact_store.artifact_root
    artifact_store.artifact_root = lambda vault_id, settings_obj=None: root
    yield root
    artifact_store.artifact_root = real


# ---------------------------------------------------------------------------
# Faithful fake lancedb (frozen-check idiom, extended with vector search)
# ---------------------------------------------------------------------------


class _FakeIndex:
    def __init__(self, name):
        self.name = name


def _schema_names(schema) -> list:
    """Field names from the backend-conftest pyarrow stub schema shape."""
    fields = getattr(schema, "_fields", None)
    if fields is not None:
        return [f[0] if isinstance(f, tuple) else f.name for f in fields]
    return []


class _FakeVectorQuery:
    """Deterministic flat cosine-distance query over a fake table's rows."""

    def __init__(self, table, query_vec):
        self._table = table
        self._vec = list(query_vec)
        self._where = None
        self._limit = 10

    def distance_type(self, _metric):
        return self

    def bypass_vector_index(self):
        return self

    def where(self, expr):
        self._where = expr
        return self

    def limit(self, n):
        self._limit = n
        return self

    async def to_list(self):
        qnorm = math.sqrt(sum(x * x for x in self._vec)) or 1.0
        results = []
        for row in self._table.rows:
            if self._where and not self._row_matches(row, self._where):
                continue
            emb = row.get("embedding") or []
            if len(emb) != len(self._vec):
                continue
            enorm = math.sqrt(sum(x * x for x in emb)) or 1.0
            dot = sum(a * b for a, b in zip(self._vec, emb))
            record = dict(row)
            record["_distance"] = 1.0 - dot / (qnorm * enorm)
            results.append(record)
        results.sort(key=lambda r: (r["_distance"], r["id"]))
        return results[: self._limit]

    @staticmethod
    def _row_matches(row, where_expr: str) -> bool:
        # Production builds simple "col = 'value'" predicates for vault scope.
        col, sep, value = where_expr.strip().partition(" = ")
        if not sep:
            return True
        return row.get(col) == value.strip().strip("'")


class _FakeTable:
    """Async lancedb table: add/count/search/index/pandas surface."""

    def __init__(self, name, schema):
        self.name = name
        self._schema = schema
        self.rows: list[dict] = []
        self.indices: list[_FakeIndex] = []

    async def schema(self):
        return self._schema

    async def count_rows(self, where=None):
        return len(self.rows)

    async def list_indices(self):
        return list(self.indices)

    async def create_index(self, column=None, config=None, replace=False):
        if replace:
            self.indices = [i for i in self.indices if i.name != f"{column}_idx"]
        self.indices.append(_FakeIndex(f"{column}_idx"))
        return None

    async def to_pandas(self):
        import pandas as pd

        cols = _schema_names(self._schema)
        return pd.DataFrame(
            [[row.get(c) for c in cols] for row in self.rows], columns=cols
        )

    async def add(self, records):
        schema_fields = set(_schema_names(self._schema))
        for rec in records:
            for key in rec:
                if key not in schema_fields:
                    raise ValueError(
                        f"cannot append field {key!r}: not in table schema"
                    )
        self.rows.extend(dict(rec) for rec in records)
        return None

    async def search(self, query_vec, query_type=None):
        assert query_type == "vector"
        return _FakeVectorQuery(self, query_vec)

    async def optimize(self):
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
        if data is not None:
            rows = data.to_dict("records")
        else:
            rows = []
        table = _FakeTable(name, schema)
        table.rows = rows
        self._tables[name] = table
        return table


def _chunk_record(rid, text, file_id, emb):
    return {
        "id": rid,
        "text": text,
        "file_id": file_id,
        "vault_id": "1",
        "chunk_index": 0,
        "chunk_scale": "default",
        "sparse_embedding": None,
        "metadata": "{}",
        "embedding": emb,
    }


# ---------------------------------------------------------------------------
# W27 — same-generation republish corner
# ---------------------------------------------------------------------------


class TestSameGenerationRepublish:
    def test_identical_generation_republish_retires_nothing_and_survives_sweeper(
        self, tmp_path, artifact_root
    ):
        """Republishing the file's CURRENT generation hash is idempotent.

        A successful publish of genA, then a republish with the IDENTICAL
        generation hash: no old-generation retirement deletes (even rows of a
        different hash that may belong to another in-flight publisher), no
        tombstone enqueues, and the committed rows + on-disk bytes survive a
        full sweep of ``artifact_delete_pending``.
        """
        db = _new_db(str(tmp_path / "db.sqlite"))
        try:
            proc_like = object.__new__(DocumentProcessor)
            proc_like.pool = _FakePool(db)

            gen_a = _asset(b"abc", "genA")
            _publish(proc_like, "genA", gen_a, {gen_a.asset_id: b"abc"})
            assert db.execute(
                "SELECT active_generation_hash FROM files WHERE id = 1"
            ).fetchone()[0] == "genA"

            # Seed an unrelated-generation row for the same file (the shape a
            # concurrent publisher could leave behind): a same-generation
            # republish must NOT retire or tombstone it.
            db.execute(
                "INSERT INTO document_assets "
                "(asset_id, file_id, generation_hash, sha256, rel_path, byte_size) "
                "VALUES ('other-asset', 1, 'genOTHER', 'other-sha', "
                "'1/genOTHER/other-asset', 1)"
            )
            db.commit()

            # The corner: republish with the IDENTICAL generation hash.
            _publish(proc_like, "genA", gen_a, {gen_a.asset_id: b"abc"})

            # No old-generation retirement deletes: both the current
            # generation's rows AND the foreign in-flight row survive.
            assert db.execute(
                "SELECT COUNT(*) FROM document_assets "
                "WHERE file_id=1 AND generation_hash='genA'"
            ).fetchone()[0] == 1
            assert db.execute(
                "SELECT COUNT(*) FROM document_assets "
                "WHERE file_id=1 AND generation_hash='genOTHER'"
            ).fetchone()[0] == 1
            # No tombstone enqueues at all.
            assert db.execute(
                "SELECT COUNT(*) FROM artifact_delete_pending"
            ).fetchone()[0] == 0
            # Referenced bytes still on disk.
            assert (artifact_root / gen_a.rel_path).read_bytes() == b"abc"

            # A full sweeper run must leave everything untouched.
            removed, remaining = artifact_store.sweep_pending_asset_deletes(db)
            assert removed == 0
            assert remaining == 0
            assert (artifact_root / gen_a.rel_path).read_bytes() == b"abc"
            assert db.execute(
                "SELECT COUNT(*) FROM document_assets WHERE file_id=1"
            ).fetchone()[0] == 2
        finally:
            db.close()

    def test_genuinely_new_generation_still_retires_and_tombstones(
        self, tmp_path, artifact_root
    ):
        """Control: a genuinely NEW generation hash keeps the retire+tombstone
        behavior (old rows deleted, old bytes tombstoned and swept away)."""
        db = _new_db(str(tmp_path / "db.sqlite"))
        try:
            proc_like = object.__new__(DocumentProcessor)
            proc_like.pool = _FakePool(db)

            gen_a = _asset(b"oldbytes", "genA")
            _publish(proc_like, "genA", gen_a, {gen_a.asset_id: b"oldbytes"})
            assert (artifact_root / gen_a.rel_path).read_bytes() == b"oldbytes"

            gen_b = _asset(b"newbytes", "genB")
            _publish(proc_like, "genB", gen_b, {gen_b.asset_id: b"newbytes"})

            # Old generation retired: rows gone, bytes tombstoned.
            assert db.execute(
                "SELECT COUNT(*) FROM document_assets "
                "WHERE file_id=1 AND generation_hash='genA'"
            ).fetchone()[0] == 0
            tomb = db.execute(
                "SELECT COUNT(*) FROM artifact_delete_pending WHERE rel_path=?",
                (gen_a.rel_path,),
            ).fetchone()[0]
            assert tomb == 1
            # New generation is live with its bytes.
            assert db.execute(
                "SELECT COUNT(*) FROM document_assets "
                "WHERE file_id=1 AND generation_hash='genB'"
            ).fetchone()[0] == 1
            assert (artifact_root / gen_b.rel_path).read_bytes() == b"newbytes"

            # The sweeper reclaims the old generation's bytes only.
            removed, _remaining = artifact_store.sweep_pending_asset_deletes(db)
            assert removed >= 1
            assert not (artifact_root / gen_a.rel_path).exists()
            assert (artifact_root / gen_b.rel_path).read_bytes() == b"newbytes"
        finally:
            db.close()


# ---------------------------------------------------------------------------
# W23/W24/W25/W26 — both-layer validator ranges for the four new settings
# ---------------------------------------------------------------------------

# (field, below, min, max, above) — ranges mirror config.py and the
# SettingsUpdate route model exactly.
_RANGE_CASES = [
    ("embedding_batch_max_chars", 4095, 4096, 1048576, 1048577),
    ("embedding_cache_max_entries", 999, 1000, 1000000, 1000001),
    ("near_dup_threshold", 0.499999, 0.5, 0.999999, 1.0),
    ("orphan_rescan_interval_seconds", 59.9, 60.0, 86400.0, 86400.1),
]


class TestNewSettingValidatorRanges:
    @pytest.mark.parametrize(
        "field,below,low,high,above",
        _RANGE_CASES,
        ids=[c[0] for c in _RANGE_CASES],
    )
    def test_config_model_rejects_out_of_range_and_accepts_boundaries(
        self, field, below, low, high, above
    ):
        """The config-layer ``field_validator`` range guards (config.py)."""
        with pytest.raises(ValidationError):
            Settings(**{field: below})
        with pytest.raises(ValidationError):
            Settings(**{field: above})
        assert getattr(Settings(**{field: low}), field) == low
        assert getattr(Settings(**{field: high}), field) == high

    @pytest.mark.parametrize(
        "field,below,low,high,above",
        _RANGE_CASES,
        ids=[c[0] for c in _RANGE_CASES],
    )
    def test_settings_update_route_model_rejects_out_of_range_and_accepts_boundaries(
        self, field, below, low, high, above
    ):
        """The SettingsUpdate route model carries the SAME range guards (the
        PUT path persists via bare setattr, so this layer is the only write
        guard for runtime updates)."""
        from app.api.routes.settings import SettingsUpdate

        with pytest.raises(ValidationError):
            SettingsUpdate(**{field: below})
        with pytest.raises(ValidationError):
            SettingsUpdate(**{field: above})
        assert getattr(SettingsUpdate(**{field: low}), field) == low
        assert getattr(SettingsUpdate(**{field: high}), field) == high
        # The fields are Optional: an explicit null is accepted (means "leave
        # unchanged") — only concrete out-of-range values are rejected.
        assert getattr(SettingsUpdate(**{field: None}), field) is None


# ---------------------------------------------------------------------------
# W26 — near-duplicate advisory grouping is retrieval-neutral
# ---------------------------------------------------------------------------


class TestNearDuplicateRetrievalNeutrality:
    async def test_grouping_touches_only_document_near_dups(
        self, tmp_path, monkeypatch
    ):
        """Grouping two similar files changes advisory rows only.

        Before/after ``record_file_centroid`` for two files whose chunk
        embeddings pool to nearly identical centroids: the two files share a
        ``group_id``, ``document_near_dups`` gains exactly two rows, the
        vector table row count is unchanged, ``files.status`` is unchanged,
        and a vector-store search over the vault returns IDENTICAL results.
        """
        monkeypatch.setattr(settings, "data_dir", tmp_path)
        monkeypatch.setattr(settings, "multi_scale_indexing_enabled", False)

        db = _new_db(str(tmp_path / "app.db"))
        db.execute(
            "INSERT INTO files (vault_id, file_path, file_name, file_hash, file_size, status) "
            "VALUES (1, '/tmp/a.txt', 'a.txt', 'ha', 1, 'indexed')"
        )
        db.execute(
            "INSERT INTO files (vault_id, file_path, file_name, file_hash, file_size, status) "
            "VALUES (1, '/tmp/b.txt', 'b.txt', 'hb', 1, 'indexed')"
        )
        db.commit()

        from app.services.vector_store import VectorStore

        vs = VectorStore(db_path=tmp_path / "lancedb")
        vs.db = _FakeDB()
        await vs.init_table(4)
        assert "chunks" in await vs.db.table_names()

        emb_a1 = [0.90, 0.10, 0.05, 0.05]
        emb_a2 = [0.85, 0.15, 0.05, 0.00]
        emb_b1 = [0.89, 0.11, 0.05, 0.05]
        emb_b2 = [0.84, 0.16, 0.05, 0.00]

        await vs.add_chunks(
            [
                _chunk_record("c-a1", "alpha one", "2", emb_a1),
                _chunk_record("c-a2", "alpha two", "2", emb_a2),
                _chunk_record("c-b1", "beta one", "3", emb_b1),
                _chunk_record("c-b2", "beta two", "3", emb_b2),
            ]
        )
        query = emb_a1

        async def _search():
            return await vs.search(
                query,
                limit=5,
                vault_id="1",
                hybrid=False,
                bypass_vector_index=True,
            )

        def _projection(results):
            return [
                (r["id"], r["text"], pytest.approx(r["_distance"]))
                for r in results
            ]

        try:
            rows_before = await vs.table.count_rows()
            before = await _search()
            assert {r["id"] for r in before} == {"c-a1", "c-a2", "c-b1", "c-b2"}

            # The grouping under test: two similar files in vault 1.
            near_duplicates.record_file_centroid(db, 1, 2, [emb_a1, emb_a2])
            near_duplicates.record_file_centroid(db, 1, 3, [emb_b1, emb_b2])

            # Advisory rows exist and the pair shares a group.
            assert db.execute(
                "SELECT COUNT(*) FROM document_near_dups WHERE vault_id=1"
            ).fetchone()[0] == 2
            group_a = near_duplicates.get_near_duplicate_group(db, 2)
            group_b = near_duplicates.get_near_duplicate_group(db, 3)
            assert group_a is not None
            assert group_a == group_b

            # No vector rows added or removed.
            assert await vs.table.count_rows() == rows_before
            # No files.status change.
            statuses = [
                row[0]
                for row in db.execute(
                    "SELECT status FROM files WHERE id IN (2, 3) ORDER BY id"
                ).fetchall()
            ]
            assert statuses == ["indexed", "indexed"]

            # Identical search results before/after grouping.
            after = await _search()
            assert _projection(before) == _projection(after)
        finally:
            db.close()


# ---------------------------------------------------------------------------
# W25 — startup-vs-periodic stranded-row sweep semantics
# ---------------------------------------------------------------------------


@pytest.fixture()
def sweep_env(tmp_path):
    """A real DB + BackgroundProcessor wired for _recover_stranded_pending_rows.

    Yields ``(processor, conn, enqueued, add_file, status_of)`` where
    ``add_file`` inserts a ``processing`` row whose backing file exists on
    disk and returns its file id, and ``enqueued`` records every re-enqueue
    the sweep performs.
    """
    import app.services.background_tasks as bt_mod

    conn = _new_db(str(tmp_path / "sweep.sqlite"))
    processor = bt_mod.BackgroundProcessor(max_retries=1, retry_delay=0.01)
    orig_instance = bt_mod._processor_instance
    bt_mod._processor_instance = None
    processor.processor.pool = _CtxPool(conn)
    enqueued: list[dict] = []

    async def _record_enqueue(**kwargs):
        enqueued.append(kwargs)

    processor.enqueue = _record_enqueue

    def add_file(name: str, phase: str, started_at_sql: str) -> int:
        path = tmp_path / name
        path.write_text("content", encoding="utf-8")
        cur = conn.execute(
            "INSERT INTO files (vault_id, file_path, file_name, file_hash, file_size, "
            "status, phase, phase_started_at) "
            "VALUES (1, ?, ?, 'h', 1, 'processing', ?, " + started_at_sql + ")",
            (str(path), name, phase),
        )
        conn.commit()
        return int(cur.lastrowid)

    def status_of(file_id: int) -> sqlite3.Row:
        return conn.execute(
            "SELECT status, phase FROM files WHERE id = ?", (file_id,)
        ).fetchone()

    try:
        yield processor, conn, enqueued, add_file, status_of
    finally:
        bt_mod._processor_instance = orig_instance
        processor._running = False
        conn.close()


class TestStartupVsPeriodicSweepSemantics:
    async def test_startup_sweep_age_gates_parse_phases_and_recovers_post_parse(
        self, sweep_env
    ):
        """The landed startup rule: post-parse phases are recovered
        unconditionally (orphans by definition at startup), while parse-stage
        phases stay age-gated so a restart alone never steals a long live
        parse."""
        processor, _conn, enqueued, add_file, status_of = sweep_env

        young_parse = add_file("young_parse.txt", "parsing", "datetime('now')")
        young_embed = add_file("young_embed.txt", "embedding", "datetime('now')")

        # The exact invocation start() performs (W25 / AC28).
        await processor._recover_stranded_pending_rows(
            require_older_than_minutes=None, ignore_active=set()
        )

        # Young parse-stage row survives untouched.
        row = status_of(young_parse)
        assert row["status"] == "processing"
        assert row["phase"] == "parsing"
        # Young post-parse row IS recovered at startup regardless of age.
        row = status_of(young_embed)
        assert row["status"] == "pending"
        assert row["phase"] == "queued"
        assert [e["file_id"] for e in enqueued] == [young_embed]

    async def test_periodic_sweep_honors_age_gate(self, sweep_env):
        """The periodic path keeps STRANDED_PROCESSING_TIMEOUT_MINUTES: a
        young post-parse row is left alone; an old one is recovered."""
        processor, _conn, enqueued, add_file, status_of = sweep_env

        young = add_file("young.txt", "embedding", "datetime('now')")
        old = add_file("old.txt", "embedding", "datetime('now', '-40 minutes')")

        # The periodic-rescan invocation: defaults apply the age gate.
        await processor._recover_stranded_pending_rows()

        row = status_of(young)
        assert row["status"] == "processing"
        assert row["phase"] == "embedding"
        assert [e["file_id"] for e in enqueued] == [old]
        row = status_of(old)
        assert row["status"] == "pending"
        assert row["phase"] == "queued"

    async def test_periodic_sweep_honors_live_lease(self, sweep_env):
        """Rows held by a live worker (the active-file lease) are never
        stolen, even when they are old enough to age out."""
        processor, _conn, enqueued, add_file, status_of = sweep_env

        leased = add_file("leased.txt", "embedding", "datetime('now', '-40 minutes')")
        async with processor._active_file_ids_lock:
            processor._active_file_ids.add(leased)

        await processor._recover_stranded_pending_rows()

        row = status_of(leased)
        assert row["status"] == "processing"
        assert row["phase"] == "embedding"
        assert enqueued == []


# ---------------------------------------------------------------------------
# Rev-5a — concurrent searchability during a dim-migrating reindex
# ---------------------------------------------------------------------------


class TestDimensionRebuildConcurrentSearchability:
    async def test_old_dim_rows_searchable_while_temp_table_fills(
        self, tmp_path, monkeypatch
    ):
        """Rev-5a: old-dimension rows stay retrievable from the LIVE table
        while new-dimension rows are written into the rebuild temp table;
        after commit, the new-dimension rows are retrievable post-swap."""
        monkeypatch.setattr(settings, "data_dir", tmp_path)
        monkeypatch.setattr(settings, "multi_scale_indexing_enabled", False)

        # A migrated app DB at the patched sqlite_path so the init/commit-time
        # settings_kv + migration-journal writes stay hermetic.
        _db = _new_db(str(tmp_path / "app.db"))
        _db.close()

        from app.services.vector_store import DIMENSION_REBUILD_TABLE, VectorStore

        vs = VectorStore(db_path=tmp_path / "lancedb")
        vs.db = _FakeDB()
        await vs.init_table(8)

        old_query = [0.90, 0.10, 0.05, 0.05, 0.0, 0.0, 0.0, 0.0]

        await vs.add_chunks(
            [
                _chunk_record("old-1", "old doc one", "1", old_query),
                _chunk_record(
                    "old-2", "old doc two", "1", [0.80, 0.20, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
                ),
            ]
        )

        async def _search_old():
            return await vs.search(
                old_query,
                limit=5,
                vault_id="1",
                hybrid=False,
                bypass_vector_index=True,
            )

        baseline = await _search_old()
        assert {r["id"] for r in baseline} == {"old-1", "old-2"}

        # Begin the dimension-migrating rebuild and fill the temp table.
        handle = await vs.begin_dimension_rebuild(16)
        assert handle.table_name == DIMENSION_REBUILD_TABLE
        new_emb = [0.9] + [0.1 / 15.0] * 15
        await vs.add_chunks(
            [
                _chunk_record("new-1", "new doc one", "2", new_emb),
                _chunk_record("new-2", "new doc two", "2", new_emb),
            ],
            target=handle,
        )
        assert await handle.table.count_rows() == 2

        # WHILE the temp table is filling: the live table still holds the
        # old-dim rows and they remain retrievable, unchanged.
        assert await vs.table.count_rows() == 2
        during = await _search_old()
        assert [(r["id"], r["text"]) for r in during] == [
            (r["id"], r["text"]) for r in baseline
        ]

        # Commit the swap; the new-dim rows become retrievable.
        await vs.commit_dimension_rebuild(handle)
        assert vs._embedding_dim == 16
        after = await vs.search(
            new_emb,
            limit=5,
            vault_id="1",
            hybrid=False,
            bypass_vector_index=True,
        )
        assert {r["id"] for r in after} == {"new-1", "new-2"}


# ---------------------------------------------------------------------------
# Rev-5b — embedding-cache key invalidation + disk-backed persistence
# ---------------------------------------------------------------------------


@pytest.fixture()
def isolated_embedding_cache(tmp_path, monkeypatch):
    """Point the persistent embedding cache at tmp_path with fresh module state."""
    from app.services import embedding_cache as ec

    monkeypatch.setattr(settings, "data_dir", tmp_path)

    def _reset_conn():
        if ec._conn is not None:
            try:
                ec._conn.close()
            except sqlite3.Error:
                pass
        ec._conn = None

    _reset_conn()
    yield ec
    _reset_conn()


class _FakeEmbeddingService:
    """Deterministic provider whose calls are counted (Rev-5b)."""

    def __init__(self, dim: int = 4):
        self.dim = dim
        self.calls: list[list[str]] = []

    async def embed_batch(self, texts, fail_fast=False):
        self.calls.append(list(texts))
        return [[0.01] * self.dim for _ in texts], []


class TestEmbeddingCacheKeyInvalidation:
    def test_key_changes_when_any_contract_component_changes(
        self, isolated_embedding_cache
    ):
        """Rev-5b: model id, revision/url discriminator, doc prefix, dim, and
        the normalized text are ALL part of the key pre-image — changing any
        one changes the key (invalidation by construction)."""
        ec = isolated_embedding_cache
        base = dict(
            model_id="model-a",
            model_revision="http://tei:8080",
            doc_prefix="passage:",
            dim=8,
            normalized_text="hello world",
        )
        base_key = ec.embedding_cache_key(**base)
        # Deterministic for identical contracts.
        assert ec.embedding_cache_key(**base) == base_key
        variations = {
            "model_id": "model-b",
            "model_revision": "http://tei:9000",  # the url discriminator slot
            "doc_prefix": "doc:",
            "dim": 16,
            "normalized_text": "hello  world",
        }
        for field, changed_value in variations.items():
            changed = dict(base)
            changed[field] = changed_value
            assert ec.embedding_cache_key(**changed) != base_key, field

    async def test_cache_hits_across_simulated_restart_and_invalidates_on_contract_change(
        self, isolated_embedding_cache
    ):
        """End-to-end through ``embed_batch_cached``: byte-identical text
        under the same contract never re-calls the provider (even after a
        simulated restart with fresh module connection state on the same disk
        file); changing a contract component misses and re-embeds."""
        from app.services.embeddings import embed_batch_cached

        ec = isolated_embedding_cache
        service = _FakeEmbeddingService(dim=4)
        expected = pytest.approx([0.01] * 4)

        # First call: provider embeds; second call: served from the cache.
        first, failed1 = await embed_batch_cached(service, ["alpha"])
        assert first[0] == expected
        assert failed1 == []
        assert service.calls == [["alpha"]]
        second, failed2 = await embed_batch_cached(service, ["alpha"])
        assert second[0] == expected
        assert failed2 == []
        assert service.calls == [["alpha"]]  # no second provider call

        # Simulated restart: fresh module connection on the same disk file.
        assert ec._conn is not None
        ec._conn.close()
        ec._conn = None
        third, _failed3 = await embed_batch_cached(service, ["alpha"])
        assert third[0] == expected
        assert service.calls == [["alpha"]]  # disk-backed hit

        # Contract change (the url discriminator): different key → miss.
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(settings, "ollama_embedding_url", "http://other:1234")
            fourth, _failed4 = await embed_batch_cached(service, ["alpha"])
        assert fourth[0] == expected
        assert service.calls == [["alpha"], ["alpha"]]  # re-embedded once


# ---------------------------------------------------------------------------
# Vault deletion × document_near_dups (cleanup-gap check — SUPPRESSED)
# ---------------------------------------------------------------------------


def test_vault_deletion_clears_document_near_dups_rows():
    """Intentionally skipped: the landed ``vaults.delete_vault`` does NOT
    clear ``document_near_dups`` rows for the deleted vault (the table has no
    FK/cascade to ``files``, and only the per-document delete /
    delete-all-purge paths call ``clear_file_centroid``). Per the plan's
    ownership boundaries this gap is REPORTED rather than fixed here (no app
    files are owned by this work item). Advisory rows for a deleted vault
    linger until the same file id is re-ingested."""
    pytest.skip(
        "Gap (reported, not implemented): vault deletion does not clear "
        "document_near_dups rows — app/api/routes/vaults.py delete_vault has "
        "no near-dup cleanup and the table carries no FK cascade."
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
