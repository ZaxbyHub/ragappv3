import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "reconcile_lancedb_sqlite.py"
SPEC = importlib.util.spec_from_file_location("reconcile_lancedb_sqlite", SCRIPT_PATH)
reconcile = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = reconcile
SPEC.loader.exec_module(reconcile)


class FakeArrowTable:
    def __init__(self, rows):
        self._rows = rows

    def to_pylist(self):
        return list(self._rows)


class FakePandasRows:
    def __init__(self, rows):
        self._rows = rows

    def to_dict(self, orient):
        assert orient == "records"
        return list(self._rows)


class FakePandasArrowTable:
    def __init__(self, rows):
        self._rows = rows

    def to_pandas(self):
        return FakePandasRows(self._rows)


class FakeIterableArrowTable:
    def __init__(self, rows):
        self._rows = rows

    def __iter__(self):
        return iter(self._rows)


class FakeBatchReader:
    def __init__(self, batches):
        self._batches = list(batches)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._batches:
            raise StopAsyncIteration
        return self._batches.pop(0)


class FakeQuery:
    def __init__(self, batches):
        self.batches = batches
        self.selected_columns = None

    def select(self, columns):
        self.selected_columns = columns
        return self

    async def to_batches(self, max_batch_length=None):
        assert max_batch_length == reconcile.LANCEDB_SCAN_BATCH_SIZE
        return FakeBatchReader(self.batches)


class FakeQueryTable:
    def __init__(self, batches):
        self.query_obj = FakeQuery(batches)

    def query(self):
        return self.query_obj

    async def to_arrow(self):
        raise AssertionError("query projection path should be used")


class FakeTable:
    def __init__(
        self,
        rows,
        indices=None,
        create_index_error=None,
        count_rows_error=None,
        list_indices_error=None,
    ):
        self.rows = list(rows)
        self.indices = list(indices or [])
        self.create_index_error = create_index_error
        self.count_rows_error = count_rows_error
        self.list_indices_error = list_indices_error
        self.deleted_filters = []
        self.optimized = False
        self.dropped_indices = []
        self.created_indices = []
        self.waited_indices = []

    async def to_arrow(self):
        return FakeArrowTable(self.rows)

    async def count_rows(self, filter_expr=None):
        if self.count_rows_error and not filter_expr:
            raise self.count_rows_error
        if not filter_expr:
            return len(self.rows)
        return sum(1 for row in self.rows if self._matches(row, filter_expr))

    async def delete(self, filter_expr):
        self.deleted_filters.append(filter_expr)
        self.rows = [row for row in self.rows if not self._matches(row, filter_expr)]

    async def optimize(self):
        self.optimized = True
        return {"optimized": True}

    async def list_indices(self):
        if self.list_indices_error:
            raise self.list_indices_error
        return self.indices

    async def drop_index(self, name):
        self.dropped_indices.append(name)
        self.indices = [index for index in self.indices if index.name != name]

    async def create_index(self, **kwargs):
        if self.create_index_error:
            raise self.create_index_error
        self.created_indices.append(kwargs)

    async def wait_for_index(self, index_names):
        self.waited_indices.append(list(index_names))

    def _matches(self, row, filter_expr):
        if filter_expr.startswith("vault_id = "):
            return row["vault_id"] == filter_expr.split("'", 2)[1]
        if filter_expr.startswith("chunk_scale != "):
            return row["chunk_scale"] != filter_expr.split("'", 2)[1]
        if filter_expr.startswith("file_id IN ("):
            raw_values = filter_expr.removeprefix("file_id IN (").removesuffix(")")
            values = {value.strip().strip("'") for value in raw_values.split(",")}
            return row["file_id"] in values
        raise AssertionError(f"unexpected filter: {filter_expr}")


def create_sqlite_db(tmp_path, rows):
    db_path = tmp_path / "app.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE files (
                id INTEGER PRIMARY KEY,
                vault_id INTEGER NOT NULL,
                file_name TEXT NOT NULL,
                chunk_count INTEGER DEFAULT 0,
                status TEXT NOT NULL
            )
            """
        )
        conn.executemany(
            """
            INSERT INTO files (id, vault_id, file_name, chunk_count, status)
            VALUES (?, ?, ?, ?, ?)
            """,
            rows,
        )
    return db_path


def run_main(monkeypatch, tmp_path, sqlite_rows, lancedb_rows, argv):
    db_path = create_sqlite_db(tmp_path, sqlite_rows)
    table = FakeTable(lancedb_rows)

    async def open_chunks_table(lancedb_path):
        assert lancedb_path == tmp_path / "lancedb"
        return table

    monkeypatch.setattr(reconcile, "_open_chunks_table", open_chunks_table)
    exit_code = reconcile.main(
        [
            "--sqlite-path",
            str(db_path),
            "--lancedb-path",
            str(tmp_path / "lancedb"),
            *argv,
        ]
    )
    return exit_code, table


def run_main_with_table(monkeypatch, tmp_path, sqlite_rows, table, argv):
    db_path = create_sqlite_db(tmp_path, sqlite_rows)

    async def open_chunks_table(lancedb_path):
        assert lancedb_path == tmp_path / "lancedb"
        return table

    monkeypatch.setattr(reconcile, "_open_chunks_table", open_chunks_table)
    exit_code = reconcile.main(
        [
            "--sqlite-path",
            str(db_path),
            "--lancedb-path",
            str(tmp_path / "lancedb"),
            *argv,
        ]
    )
    return exit_code, table


def parse_report(capsys):
    output = capsys.readouterr()
    return json.loads(output.out), output


class FakeIndex:
    def __init__(self, name):
        self.name = name


class FakeIvfPq:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


def test_dry_run_reports_vault_mismatch(capsys, monkeypatch, tmp_path):
    exit_code, table = run_main(
        monkeypatch,
        tmp_path,
        sqlite_rows=[(10, 10, "valid.txt", 2, "indexed")],
        lancedb_rows=[
            {
                "id": "10_0",
                "file_id": "10",
                "vault_id": "10",
                "chunk_scale": "default",
            },
            {
                "id": "10_1",
                "file_id": "10",
                "vault_id": "5",
                "chunk_scale": "default",
            },
        ],
        argv=["--dry-run"],
    )

    report, output = parse_report(capsys)

    assert exit_code == 0, output.err
    assert table.deleted_filters == []
    assert report["sqlite_indexed_files_by_vault_id"] == {"10": 1}
    assert report["lancedb_chunks_by_vault_id"] == {"10": 1, "5": 1}
    assert report["vault_ids_present_in_lancedb_but_not_sqlite_indexed"] == {"5": 1}
    assert report["vault_id_mismatches_between_sqlite_files_and_lancedb_chunks"] == {
        "10": {
            "sqlite_vault_id": "10",
            "lancedb_vault_ids": ["10", "5"],
            "mismatched_lancedb_vault_ids": ["5"],
            "row_count": 1,
        }
    }


def test_dry_run_reports_indexed_sqlite_file_missing_lancedb_rows(
    capsys, monkeypatch, tmp_path
):
    exit_code, table = run_main(
        monkeypatch,
        tmp_path,
        sqlite_rows=[
            (10, 10, "missing.txt", 3, "indexed"),
            (11, 10, "zero.txt", 0, "indexed"),
        ],
        lancedb_rows=[],
        argv=["--dry-run"],
    )

    report, output = parse_report(capsys)

    assert exit_code == 0, output.err
    assert table.deleted_filters == []
    assert report["indexed_sqlite_files_missing_lancedb_rows"] == {
        "10": {
            "vault_id": "10",
            "file_name": "missing.txt",
            "sqlite_chunk_count": 3,
        }
    }
    assert report["totals"]["indexed_files_missing_lancedb_rows"] == 1


def test_dry_run_reports_empty_corpus(capsys, monkeypatch, tmp_path):
    exit_code, table = run_main(
        monkeypatch,
        tmp_path,
        sqlite_rows=[],
        lancedb_rows=[],
        argv=["--dry-run"],
    )

    report, output = parse_report(capsys)

    assert exit_code == 0, output.err
    assert table.deleted_filters == []
    assert report["sqlite_indexed_files_by_vault_id"] == {}
    assert report["lancedb_chunks_by_vault_id"] == {}
    assert report["file_ids_present_in_lancedb_but_not_sqlite_indexed"] == []
    assert report["vault_ids_present_in_lancedb_but_not_sqlite_indexed"] == {}
    assert report["totals"] == {
        "indexed_files_missing_lancedb_rows": 0,
        "lancedb_chunks": 0,
        "orphan_file_ids": 0,
        "orphan_vault_ids": 0,
        "sqlite_indexed_files": 0,
        "stale_multiscale_rows": 0,
        "vault_mismatched_files": 0,
    }


def test_missing_sqlite_path_returns_error(capsys, tmp_path):
    exit_code = reconcile.main(
        [
            "--sqlite-path",
            str(tmp_path / "missing.db"),
            "--lancedb-path",
            str(tmp_path / "lancedb"),
            "--dry-run",
        ]
    )

    output = capsys.readouterr()

    assert exit_code == 1
    assert "SQLite database not found" in output.err
    assert output.out == ""


def test_delete_orphan_vault_requires_confirm(capsys, monkeypatch, tmp_path):
    exit_code, table = run_main(
        monkeypatch,
        tmp_path,
        sqlite_rows=[(10, 10, "valid.txt", 2, "indexed")],
        lancedb_rows=[
            {
                "id": "10_0",
                "file_id": "10",
                "vault_id": "10",
                "chunk_scale": "default",
            },
            {
                "id": "10_1",
                "file_id": "10",
                "vault_id": "5",
                "chunk_scale": "default",
            },
        ],
        argv=["--delete-orphan-vault", "5"],
    )

    report, output = parse_report(capsys)

    assert exit_code == 2
    assert "Re-run with --confirm" in output.err
    assert table.deleted_filters == []
    assert report["cleanup_plan"]["requires_confirm"] is True
    assert report["cleanup_plan"]["planned_delete_counts"] == {"orphan_vault_rows": 1}


def test_delete_orphan_vault_with_confirm_removes_only_requested_vault(
    capsys, monkeypatch, tmp_path
):
    exit_code, table = run_main(
        monkeypatch,
        tmp_path,
        sqlite_rows=[(10, 10, "valid.txt", 2, "indexed")],
        lancedb_rows=[
            {
                "id": "10_0",
                "file_id": "10",
                "vault_id": "10",
                "chunk_scale": "default",
            },
            {
                "id": "10_1",
                "file_id": "10",
                "vault_id": "5",
                "chunk_scale": "default",
            },
        ],
        argv=["--delete-orphan-vault", "5", "--confirm"],
    )

    report, output = parse_report(capsys)

    assert exit_code == 0, output.err
    assert table.deleted_filters == ["vault_id = '5'"]
    assert table.rows == [
        {
            "id": "10_0",
            "file_id": "10",
            "vault_id": "10",
            "chunk_scale": "default",
        }
    ]
    assert report["cleanup_result"]["deleted_counts"] == {"orphan_vault_rows": 1}
    assert report["cleanup_result"]["remaining_after_delete_counts"] == {
        "orphan_vault_rows": 0
    }


def test_delete_stale_multiscale_only_when_requested_and_confirmed(
    capsys, monkeypatch, tmp_path
):
    exit_code, table = run_main(
        monkeypatch,
        tmp_path,
        sqlite_rows=[(10, 10, "valid.txt", 2, "indexed")],
        lancedb_rows=[
            {
                "id": "10_default_0",
                "file_id": "10",
                "vault_id": "10",
                "chunk_scale": "default",
            },
            {
                "id": "10_512_0",
                "file_id": "10",
                "vault_id": "10",
                "chunk_scale": "512",
            },
        ],
        argv=[
            "--multi-scale-indexing-enabled",
            "false",
            "--delete-stale-multiscale",
            "--confirm",
        ],
    )

    report, output = parse_report(capsys)

    assert exit_code == 0, output.err
    assert table.deleted_filters == ["chunk_scale != 'default'"]
    assert table.rows == [
        {
            "id": "10_default_0",
            "file_id": "10",
            "vault_id": "10",
            "chunk_scale": "default",
        }
    ]
    assert report["stale_multiscale_rows_when_disabled"] == {
        "multi_scale_indexing_enabled": False,
        "row_count": 1,
        "by_chunk_scale": {"512": 1},
    }
    assert report["cleanup_result"]["deleted_counts"] == {
        "stale_multiscale_rows": 1
    }
    assert report["cleanup_result"]["remaining_after_delete_counts"] == {
        "stale_multiscale_rows": 0
    }


def test_delete_orphan_file_ids_removes_only_rows_without_indexed_sqlite_file(
    capsys, monkeypatch, tmp_path
):
    exit_code, table = run_main(
        monkeypatch,
        tmp_path,
        sqlite_rows=[
            (10, 10, "valid.txt", 1, "indexed"),
            (11, 10, "pending.txt", 0, "pending"),
        ],
        lancedb_rows=[
            {
                "id": "10_0",
                "file_id": "10",
                "vault_id": "10",
                "chunk_scale": "default",
            },
            {
                "id": "11_0",
                "file_id": "11",
                "vault_id": "10",
                "chunk_scale": "default",
            },
            {
                "id": "99_0",
                "file_id": "99",
                "vault_id": "10",
                "chunk_scale": "default",
            },
        ],
        argv=["--delete-orphan-file-ids", "--confirm"],
    )

    report, output = parse_report(capsys)

    assert exit_code == 0, output.err
    assert report["file_ids_present_in_lancedb_but_not_sqlite_indexed"] == [
        "11",
        "99",
    ]
    assert report["cleanup_result"]["deleted_counts"] == {
        "orphan_file_id_rows": 2
    }
    assert report["cleanup_result"]["remaining_after_delete_counts"] == {
        "orphan_file_id_rows": 0
    }
    assert [row["file_id"] for row in table.rows] == ["10"]


def test_delete_stale_multiscale_is_noop_when_multiscale_enabled(
    capsys, monkeypatch, tmp_path
):
    exit_code, table = run_main(
        monkeypatch,
        tmp_path,
        sqlite_rows=[(10, 10, "valid.txt", 2, "indexed")],
        lancedb_rows=[
            {
                "id": "10_default_0",
                "file_id": "10",
                "vault_id": "10",
                "chunk_scale": "default",
            },
            {
                "id": "10_512_0",
                "file_id": "10",
                "vault_id": "10",
                "chunk_scale": "512",
            },
        ],
        argv=[
            "--multi-scale-indexing-enabled",
            "true",
            "--delete-stale-multiscale",
            "--confirm",
        ],
    )

    report, output = parse_report(capsys)

    assert exit_code == 0, output.err
    assert table.deleted_filters == []
    assert [row["chunk_scale"] for row in table.rows] == ["default", "512"]
    assert report["stale_multiscale_rows_when_disabled"] == {
        "multi_scale_indexing_enabled": True,
        "row_count": 0,
        "by_chunk_scale": {},
    }
    assert "cleanup_result" not in report


def test_optimize_after_cleanup_drops_embedding_index_below_threshold(
    capsys, monkeypatch, tmp_path
):
    table = FakeTable(
        [
            {
                "id": "10_0",
                "file_id": "10",
                "vault_id": "10",
                "chunk_scale": "default",
            }
        ],
        indices=[FakeIndex("embedding_idx")],
    )

    exit_code, table = run_main_with_table(
        monkeypatch,
        tmp_path,
        sqlite_rows=[(10, 10, "valid.txt", 1, "indexed")],
        table=table,
        argv=["--optimize-after-cleanup", "--confirm"],
    )

    report, output = parse_report(capsys)

    assert exit_code == 0, output.err
    assert table.optimized is True
    assert table.dropped_indices == ["embedding_idx"]
    assert table.created_indices == []
    assert report["optimize_after_cleanup"]["index_action"] == (
        "dropped_embedding_idx_below_threshold"
    )


def test_optimize_after_cleanup_rebuilds_and_waits_for_large_table(
    capsys, monkeypatch, tmp_path
):
    monkeypatch.setattr(reconcile, "IvfPq", FakeIvfPq)
    rows = [
        {
            "id": f"10_{index}",
            "file_id": "10",
            "vault_id": "10",
            "chunk_scale": "default",
        }
        for index in range(reconcile.VECTOR_INDEX_MIN_ROWS)
    ]
    table = FakeTable(rows)

    exit_code, table = run_main_with_table(
        monkeypatch,
        tmp_path,
        sqlite_rows=[(10, 10, "valid.txt", len(rows), "indexed")],
        table=table,
        argv=["--optimize-after-cleanup", "--confirm"],
    )

    report, output = parse_report(capsys)

    assert exit_code == 0, output.err
    assert table.optimized is True
    assert len(table.created_indices) == 1
    assert table.created_indices[0]["column"] == "embedding"
    assert table.created_indices[0]["replace"] is True
    assert table.waited_indices == [["embedding_idx"]]
    assert report["optimize_after_cleanup"]["index_action"] == "rebuilt_embedding_idx"
    assert report["optimize_after_cleanup"]["waited_for_index"] is True


def test_optimize_after_cleanup_requires_confirm(capsys, monkeypatch, tmp_path):
    table = FakeTable(
        [
            {
                "id": "10_0",
                "file_id": "10",
                "vault_id": "10",
                "chunk_scale": "default",
            }
        ]
    )

    exit_code, table = run_main_with_table(
        monkeypatch,
        tmp_path,
        sqlite_rows=[(10, 10, "valid.txt", 1, "indexed")],
        table=table,
        argv=["--optimize-after-cleanup"],
    )

    report, output = parse_report(capsys)

    assert exit_code == 2
    assert "Re-run with --confirm" in output.err
    assert table.optimized is False
    assert "optimize_after_cleanup" not in report
    assert report["cleanup_plan"]["requires_confirm"] is True


def test_optimize_after_cleanup_reports_index_rebuild_failure(
    capsys, monkeypatch, tmp_path
):
    monkeypatch.setattr(reconcile, "IvfPq", FakeIvfPq)
    rows = [
        {
            "id": f"10_{index}",
            "file_id": "10",
            "vault_id": "10",
            "chunk_scale": "default",
        }
        for index in range(reconcile.VECTOR_INDEX_MIN_ROWS)
    ]
    table = FakeTable(rows, create_index_error=RuntimeError("training failed"))

    exit_code, table = run_main_with_table(
        monkeypatch,
        tmp_path,
        sqlite_rows=[(10, 10, "valid.txt", len(rows), "indexed")],
        table=table,
        argv=["--optimize-after-cleanup", "--confirm"],
    )

    report, output = parse_report(capsys)

    assert exit_code == 0, output.err
    assert table.optimized is True
    assert table.waited_indices == []
    assert report["optimize_after_cleanup"]["index_action"] == (
        "rebuild_embedding_idx_failed"
    )
    assert report["optimize_after_cleanup"]["index_error"] == "training failed"


def test_optimize_after_cleanup_reports_count_rows_probe_failure(
    capsys, monkeypatch, tmp_path
):
    table = FakeTable(
        [
            {
                "id": "10_0",
                "file_id": "10",
                "vault_id": "10",
                "chunk_scale": "default",
            }
        ],
        count_rows_error=RuntimeError("count failed"),
    )

    exit_code, table = run_main_with_table(
        monkeypatch,
        tmp_path,
        sqlite_rows=[(10, 10, "valid.txt", 1, "indexed")],
        table=table,
        argv=["--optimize-after-cleanup", "--confirm"],
    )

    report, output = parse_report(capsys)

    assert exit_code == 0, output.err
    assert table.optimized is True
    assert report["optimize_after_cleanup"]["index_action"] == (
        "index_maintenance_skipped"
    )
    assert report["optimize_after_cleanup"]["index_error"] == (
        "count_rows failed: count failed"
    )


def test_optimize_after_cleanup_reports_list_indices_probe_failure(
    capsys, monkeypatch, tmp_path
):
    table = FakeTable(
        [
            {
                "id": "10_0",
                "file_id": "10",
                "vault_id": "10",
                "chunk_scale": "default",
            }
        ],
        list_indices_error=RuntimeError("index listing failed"),
    )

    exit_code, table = run_main_with_table(
        monkeypatch,
        tmp_path,
        sqlite_rows=[(10, 10, "valid.txt", 1, "indexed")],
        table=table,
        argv=["--optimize-after-cleanup", "--confirm"],
    )

    report, output = parse_report(capsys)

    assert exit_code == 0, output.err
    assert table.optimized is True
    assert report["optimize_after_cleanup"]["index_action"] == (
        "index_maintenance_skipped"
    )
    assert report["optimize_after_cleanup"]["index_error"] == (
        "list_indices failed: index listing failed"
    )


async def test_load_lancedb_chunks_uses_projected_batches():
    table = FakeQueryTable(
        [
            FakeArrowTable(
                [
                    {
                        "id": "10_0",
                        "file_id": "10",
                        "vault_id": "10",
                        "chunk_scale": "default",
                        "embedding": [1, 2, 3],
                    }
                ]
            ),
            FakeArrowTable(
                [
                    {
                        "id": "11_0",
                        "file_id": "11",
                        "vault_id": "11",
                        "chunk_scale": None,
                        "text": "not needed",
                    }
                ]
            ),
        ]
    )

    chunks = await reconcile._load_lancedb_chunks(table)

    assert table.query_obj.selected_columns == list(reconcile.LANCEDB_RECONCILE_COLUMNS)
    assert chunks == [
        reconcile.ChunkRow("10_0", "10", "10", "default"),
        reconcile.ChunkRow("11_0", "11", "11", "default"),
    ]


async def test_load_lancedb_chunks_supports_pandas_fallback():
    class PandasTable:
        async def to_arrow(self):
            return FakePandasArrowTable(
                [
                    {
                        "id": "10_0",
                        "file_id": "10",
                        "vault_id": "10",
                        "chunk_scale": "512",
                    }
                ]
            )

    chunks = await reconcile._load_lancedb_chunks(PandasTable())

    assert chunks == [reconcile.ChunkRow("10_0", "10", "10", "512")]


async def test_load_lancedb_chunks_supports_iterable_fallback():
    class IterableTable:
        async def to_arrow(self):
            return FakeIterableArrowTable(
                [
                    {
                        "id": "10_0",
                        "file_id": "10",
                        "vault_id": "10",
                        "chunk_scale": "",
                    }
                ]
            )

    chunks = await reconcile._load_lancedb_chunks(IterableTable())

    assert chunks == [reconcile.ChunkRow("10_0", "10", "10", "default")]
