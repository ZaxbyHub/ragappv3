"""Issue #705 (Workstream B16) — operator maintenance scripts must agree with
the app's status model.

Acceptance checks (issue-tracer protocol). Every test below encodes the
POST-fix behavior, so each FAILS at the unfixed base tree:

- reconcile_lancedb_sqlite.py: the ``--multi-scale-indexing-enabled auto``
  default must follow the shipped app default (True, config.py), ``partial``
  files own live LanceDB rows, orphan cleanup must re-check SQLite before
  deleting (a file ingested between the two reads must survive), and
  ``build_report`` must not rescan the chunk list once per indexed file.
- scripts/reset_embeddings.py: the reset must cover ``partial`` files, must
  land the ``queued`` phase the startup recovery sweep adopts, and must not
  wipe the LanceDB directory before the SQLite reset succeeds.
- scripts/migrate_memories.py: the pre-migration backup must predate
  ``init_db()``'s schema changes (legacy ``files`` without ``modified_at``
  must be backed up verbatim).
- backend/app/utils/transaction.py: ``db_transaction``'s ``finally`` must not
  release the pooled connection unguarded.
"""

import ast
import importlib.util
import shutil
import sqlite3
import sys
import tempfile
import types
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Module loading — reconcile via importlib (cribbed from
# tests/test_reconcile_lancedb_sqlite.py, under a distinct sys.modules name so
# the sibling module's registration is not clobbered); repo-root scripts via
# the sys.path idiom from tests/test_migrate_memories.py.
# ---------------------------------------------------------------------------

RECONCILE_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "reconcile_lancedb_sqlite.py"
)
RECONCILE_SPEC = importlib.util.spec_from_file_location(
    "reconcile_lancedb_sqlite_b16", RECONCILE_PATH
)
reconcile = importlib.util.module_from_spec(RECONCILE_SPEC)
assert RECONCILE_SPEC.loader is not None
sys.modules[RECONCILE_SPEC.name] = reconcile
RECONCILE_SPEC.loader.exec_module(reconcile)

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.models.database import init_db, run_migrations
from scripts import migrate_memories, reset_embeddings

# ---------------------------------------------------------------------------
# Fake LanceDB harness (cribbed from tests/test_reconcile_lancedb_sqlite.py —
# only the surface the delete paths touch).
# ---------------------------------------------------------------------------


class FakeArrowTable:
    def __init__(self, rows):
        self._rows = rows

    def to_pylist(self):
        return list(self._rows)


class FakeTable:
    def __init__(self, rows):
        self.rows = list(rows)
        self.deleted_filters = []

    async def to_arrow(self):
        return FakeArrowTable(self.rows)

    async def count_rows(self, filter_expr=None):
        if not filter_expr:
            return len(self.rows)
        return sum(1 for row in self.rows if self._matches(row, filter_expr))

    async def delete(self, filter_expr):
        self.deleted_filters.append(filter_expr)
        self.rows = [row for row in self.rows if not self._matches(row, filter_expr)]

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


def run_reconcile_main(monkeypatch, tmp_path, sqlite_rows, lancedb_rows, argv):
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


# ---------------------------------------------------------------------------
# reconcile — stale multi-scale auto default
# ---------------------------------------------------------------------------


def test_stale_multiscale_auto_default_keeps_live_rows(monkeypatch, tmp_path):
    monkeypatch.delenv("MULTI_SCALE_INDEXING_ENABLED", raising=False)
    exit_code, table = run_reconcile_main(
        monkeypatch,
        tmp_path,
        sqlite_rows=[(1, 1, "a.txt", 2, "indexed")],
        lancedb_rows=[
            {"id": "1_768_0", "file_id": "1", "vault_id": "1", "chunk_scale": "768"},
            {"id": "1_1536_0", "file_id": "1", "vault_id": "1", "chunk_scale": "1536"},
        ],
        argv=["--delete-stale-multiscale", "--confirm"],
    )

    remaining = sum(1 for row in table.rows if row["file_id"] == "1")

    assert exit_code == 0
    assert remaining == 2


# ---------------------------------------------------------------------------
# reconcile — 'partial' files own live LanceDB rows
# ---------------------------------------------------------------------------


def test_orphan_cleanup_keeps_partial_file_rows(monkeypatch, tmp_path):
    exit_code, table = run_reconcile_main(
        monkeypatch,
        tmp_path,
        sqlite_rows=[(10, 10, "p.txt", 1, "partial")],
        lancedb_rows=[
            {"id": "10_0", "file_id": "10", "vault_id": "10", "chunk_scale": "default"}
        ],
        argv=["--delete-orphan-file-ids", "--confirm"],
    )

    remaining = sum(1 for row in table.rows if row["file_id"] == "10")

    assert exit_code == 0
    assert remaining == 1


# ---------------------------------------------------------------------------
# reconcile — concurrent ingest between the two reads must survive cleanup
# ---------------------------------------------------------------------------


def test_orphan_cleanup_rechecks_concurrently_ingested_file(monkeypatch, tmp_path):
    db_path = create_sqlite_db(tmp_path, [(1, 1, "a.txt", 1, "indexed")])
    table = FakeTable(
        [{"id": "1_0", "file_id": "1", "vault_id": "1", "chunk_scale": "default"}]
    )

    async def open_chunks_table(lancedb_path):
        assert lancedb_path == tmp_path / "lancedb"
        # A file lands between reconcile's SQLite read and its LanceDB read:
        # the row is live in both stores by the time the delete would run.
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO files (id, vault_id, file_name, chunk_count, status)"
                " VALUES (20, 1, 'late.txt', 1, 'processing')"
            )
        table.rows.append(
            {"id": "20_0", "file_id": "20", "vault_id": "1", "chunk_scale": "default"}
        )
        return table

    monkeypatch.setattr(reconcile, "_open_chunks_table", open_chunks_table)
    exit_code = reconcile.main(
        [
            "--sqlite-path",
            str(db_path),
            "--lancedb-path",
            str(tmp_path / "lancedb"),
            "--delete-orphan-file-ids",
            "--confirm",
        ]
    )

    remaining = sum(1 for row in table.rows if row["file_id"] == "20")

    assert exit_code == 0
    assert remaining == 1


# ---------------------------------------------------------------------------
# reconcile — build_report must be constant-pass over the chunk list
# ---------------------------------------------------------------------------


class CountingList(list):
    """list that counts how many times something iterated over it."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.iterations = 0

    def __iter__(self):
        self.iterations += 1
        return super().__iter__()


def test_build_report_chunk_passes_are_constant():
    files = {
        str(i): reconcile.IndexedFile(
            file_id=str(i), vault_id="1", file_name=f"f{i}.txt", chunk_count=2
        )
        for i in range(200)
    }
    chunks = CountingList(
        reconcile.ChunkRow(
            id=f"c{j}", file_id=str(j // 2), vault_id="1", chunk_scale="default"
        )
        for j in range(400)
    )

    reconcile.build_report(files, chunks, multi_scale_indexing_enabled=True)

    iterations = chunks.iterations
    assert iterations <= 10


# ---------------------------------------------------------------------------
# reset_embeddings — real-schema DB harness
# ---------------------------------------------------------------------------


def build_files_db(db_path, rows):
    """Run the real migrations, then seed files rows.

    rows are (vault_id, file_path, file_name, file_hash, file_size,
    chunk_count, status, phase) tuples.
    """
    run_migrations(str(db_path))
    conn = sqlite3.connect(db_path)
    try:
        conn.executemany(
            """
            INSERT INTO files (vault_id, file_path, file_name, file_hash,
                               file_size, chunk_count, status, phase)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.commit()
    finally:
        conn.close()
    return db_path


def patch_reset_settings(monkeypatch, db_path, lancedb_dir):
    # settings.sqlite_path is a read-only property on the real Settings, so
    # the script's module global is swapped for a plain namespace pointing at
    # the temp stores.
    monkeypatch.setattr(
        reset_embeddings,
        "settings",
        types.SimpleNamespace(sqlite_path=db_path, lancedb_path=lancedb_dir),
    )


def read_file_column(db_path, column, file_name):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            f"SELECT {column} FROM files WHERE file_name = ?", (file_name,)
        ).fetchone()[0]
    finally:
        conn.close()


def test_reset_embeddings_resets_partial_files(tmp_path, monkeypatch):
    db_path = build_files_db(
        tmp_path / "app.db",
        [
            (1, "a.pdf", "a.pdf", "h1", 10, 2, "indexed", None),
            (1, "p.pdf", "p.pdf", "h2", 10, 1, "partial", None),
        ],
    )
    lancedb_dir = tmp_path / "lancedb"
    lancedb_dir.mkdir()
    patch_reset_settings(monkeypatch, db_path, lancedb_dir)

    reset_embeddings.reset_all_embeddings()

    partial_status = read_file_column(db_path, "status", "p.pdf")
    assert partial_status == "pending"


def test_reset_embeddings_does_not_wipe_before_reset_succeeds(tmp_path, monkeypatch):
    db_path = build_files_db(
        tmp_path / "app.db",
        [(1, "a.pdf", "a.pdf", "h1", 10, 2, "indexed", None)],
    )
    lancedb_dir = tmp_path / "lancedb"
    lancedb_dir.mkdir()
    sentinel = lancedb_dir / "sentinel.marker"
    sentinel.write_text("live vectors", encoding="utf-8")
    patch_reset_settings(monkeypatch, db_path, lancedb_dir)

    def locked_db(_path):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(reset_embeddings, "get_db_connection", locked_db)

    try:
        reset_embeddings.reset_all_embeddings()
    except sqlite3.OperationalError:
        pass

    survived = int(sentinel.exists())
    assert survived == 1


def test_reset_embeddings_sets_queued_phase(tmp_path, monkeypatch):
    db_path = build_files_db(
        tmp_path / "app.db",
        [(1, "a.pdf", "a.pdf", "h1", 10, 2, "indexed", None)],
    )
    lancedb_dir = tmp_path / "lancedb"
    lancedb_dir.mkdir()
    patch_reset_settings(monkeypatch, db_path, lancedb_dir)

    reset_embeddings.reset_all_embeddings()

    phase = read_file_column(db_path, "phase", "a.pdf")
    assert phase == "queued"


# ---------------------------------------------------------------------------
# migrate_memories — backup must predate init_db's schema changes
# ---------------------------------------------------------------------------


@pytest.fixture
def scratch_dir():
    root = Path(__file__).resolve().parents[2] / "tmp" / "pytest-b16"
    root.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(dir=root))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def test_migrate_memories_backup_predates_init_db(scratch_dir, tmp_path, monkeypatch):
    # Real current files DDL, minus modified_at — derived by building a fresh
    # schema and dropping the modified_at line.
    template_path = tmp_path / "template.db"
    init_db(str(template_path))
    conn = sqlite3.connect(template_path)
    try:
        ddl = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='files'"
        ).fetchone()[0]
    finally:
        conn.close()
    legacy_ddl = "\n".join(
        line for line in ddl.splitlines() if not line.strip().startswith("modified_at")
    )

    db_path = scratch_dir / "app.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(legacy_ddl)
        conn.execute(
            "INSERT INTO files (vault_id, file_path, file_name, file_hash,"
            " file_size, chunk_count, status)"
            " VALUES (1, 'legacy.txt', 'legacy.txt', 'h1', 10, 0, 'indexed')"
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(migrate_memories.settings, "data_dir", scratch_dir)
    monkeypatch.setattr(migrate_memories, "backup_sqlite", None)
    monkeypatch.setattr(migrate_memories, "HAS_BACKUP_MODULE", False)
    monkeypatch.setattr(migrate_memories, "run_migrations", lambda _path: None)
    monkeypatch.chdir(scratch_dir)

    migrate_memories.migrate(rollback=False, backup=None, retention=30)

    backups = sorted((scratch_dir / "backups").glob("app_backup_*.db"))
    assert backups, "expected a pre-migration backup under scratch/backups"
    backup_conn = sqlite3.connect(backups[0])
    try:
        cols = {
            row[1] for row in backup_conn.execute("PRAGMA table_info(files)").fetchall()
        }
    finally:
        backup_conn.close()

    has_col = int("modified_at" in cols)
    assert has_col == 0


# ---------------------------------------------------------------------------
# transaction.py — db_transaction must not release unguarded in finally
# ---------------------------------------------------------------------------


def finalbody_release_is_unguarded(stmt):
    """True when stmt is (or contains) a bare release_connection call.

    A statement wrapped in its own ast.Try — handler or finalbody — counts as
    guarded; anything else that reaches a release_connection call is not.
    """
    if isinstance(stmt, ast.Try):
        return False
    for node in ast.walk(stmt):
        if isinstance(node, ast.Try):
            return False
    for node in ast.walk(stmt):
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Attribute)
            and node.value.func.attr == "release_connection"
        ):
            return True
    return False


def test_no_unguarded_db_transaction_release():
    transaction_path = (
        Path(__file__).resolve().parents[2] / "backend" / "app" / "utils" / "transaction.py"
    )
    unsafe = False
    if transaction_path.exists():
        tree = ast.parse(transaction_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            if node.name != "db_transaction":
                continue
            for sub in ast.walk(node):
                if isinstance(sub, ast.Try) and sub.finalbody:
                    for stmt in sub.finalbody:
                        if finalbody_release_is_unguarded(stmt):
                            unsafe = True

    unsafe_int = int(unsafe)
    assert unsafe_int == 0
