"""scripts/migrate_embeddings.py CLI refusal exit codes (issue #694,
Workstream B PR 5) — main()-level and refusal-surface pins for the new
refusal contract, extended by the PR #831 review round.

The run_migration-level behavior (ordering, predicate widening, repair,
settings refusal, writer refusal leaving the index intact) is pinned by the
frozen issue-tracer checks in test_b05_migrate_embeddings_safety.py; this
module pins the CLI surface around them: ``main()`` maps the settings
refusal to exit code 4 and the ``MigrationRefused`` refusals — the writer
lock at the probe, a lock acquired between the probe and the status reset,
and an unreadable/corrupt database at the probe (``MigrationRefused`` is an
ordinary Exception so programmatic callers can catch it with ``except
Exception``) — to exit code 5.

Lives in its own file so the frozen test files stay byte-identical to the
checkpoint manifest. The engine-gated skipTest guards only fire on machines
without the real lancedb/pyarrow packages (a local fallback) — CI installs
both for real (docs/engineering/testing.md), matching the sibling test
modules.
"""

import importlib.machinery
import importlib.util
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "migrate_embeddings.py"

sys.path.insert(0, str(_REPO_ROOT))

_spec = importlib.util.spec_from_file_location("migrate_embeddings_refusals_test", _SCRIPT)
script = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(script)

from app.config import settings  # noqa: E402


def _load_real_module(name, deps=None, purge_prefixes=()):
    """Load the REAL installed package even when backend/conftest.py has
    installed its test stub into sys.modules (same approach as the sibling
    test modules; see test_migrate_embeddings_dim.py for the rationale)."""
    current = sys.modules.get(name)
    if current is not None and getattr(current, "__file__", None):
        return current
    real_spec = importlib.machinery.PathFinder.find_spec(name)
    if real_spec is None:
        return None
    module = importlib.util.module_from_spec(real_spec)
    saved_self = sys.modules.get(name)
    saved_deps = {d: sys.modules.get(d) for d in (deps or {})}
    purged = {
        k: v
        for k, v in sys.modules.items()
        if any(k == p or k.startswith(p + ".") for p in purge_prefixes)
    }
    sys.modules[name] = module
    for dep_name, dep_module in (deps or {}).items():
        sys.modules[dep_name] = dep_module
    for purged_name in purged:
        if purged_name != name:
            del sys.modules[purged_name]
    try:
        real_spec.loader.exec_module(module)
    except Exception:
        return None
    finally:
        sys.modules[name] = saved_self
        for dep_name, dep_module in saved_deps.items():
            if dep_module is not None:
                sys.modules[dep_name] = dep_module
            else:
                sys.modules.pop(dep_name, None)
        for purged_name, purged_module in purged.items():
            sys.modules[purged_name] = purged_module
    return module


_REAL_PYARROW = _load_real_module("pyarrow")
_REAL_LANCEDB = None
if _REAL_PYARROW is not None:
    _REAL_LANCEDB = _load_real_module(
        "lancedb", deps={"pyarrow": _REAL_PYARROW}, purge_prefixes=("lancedb",)
    )


def _make_sqlite(sqlite_path, status="indexed"):
    conn = sqlite3.connect(str(sqlite_path))
    conn.executescript(
        """
        CREATE TABLE files (
            id INTEGER PRIMARY KEY,
            file_name TEXT,
            status TEXT,
            chunk_count INTEGER,
            processed_at TIMESTAMP,
            modified_at TIMESTAMP
        );
        """
    )
    conn.execute(
        "INSERT INTO files (id, file_name, status, chunk_count)"
        " VALUES (1, 'doc.txt', ?, 3)",
        (status,),
    )
    conn.commit()
    conn.close()


def _files_status(sqlite_path):
    conn = sqlite3.connect(str(sqlite_path))
    status = conn.execute("SELECT status FROM files WHERE id = 1").fetchone()[0]
    conn.close()
    return status


def _make_real_table(lancedb_path, dim):
    pa = _REAL_PYARROW
    db = _REAL_LANCEDB.connect(str(lancedb_path))
    schema = pa.schema(
        [
            ("id", pa.string()),
            ("text", pa.string()),
            ("metadata", pa.string()),
            ("embedding", pa.list_(pa.float32(), dim)),
        ]
    )
    table = db.create_table("chunks", schema=schema)
    table.add(
        [
            {
                "id": "f1_0",
                "text": "chunk text",
                "metadata": "{}",
                "embedding": [0.01] * dim,
            }
        ]
    )
    return table


class _RealEngineTestCase(unittest.TestCase):
    def _real_engine(self):
        return patch.dict(
            sys.modules, {"lancedb": _REAL_LANCEDB, "pyarrow": _REAL_PYARROW}
        )

    def _require_real_engine(self):
        if _REAL_LANCEDB is None or _REAL_PYARROW is None:
            self.skipTest("real lancedb/pyarrow not installed (CI stub environment)")


class TestSettingsFailureRefusesWithExit4(_RealEngineTestCase):
    def test_settings_failure_refuses_with_exit_4(self):
        """A settings import failure against a dimension-MATCHING index must
        refuse at the CLI level (exit 4) without wiping — the destructive
        path requires --force whenever the configured dim is unreadable."""
        self._require_real_engine()
        tmp = Path(tempfile.mkdtemp(prefix="dimexit4_"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        lancedb_path = tmp / "lancedb"
        sqlite_path = tmp / "app.db"
        _make_sqlite(sqlite_path)
        with self._real_engine():
            _make_real_table(lancedb_path, int(settings.embedding_dim))
            with patch.dict(sys.modules, {"app.config": None}):
                with patch.object(
                    sys,
                    "argv",
                    [
                        "migrate_embeddings.py",
                        "--lancedb-path",
                        str(lancedb_path),
                        "--sqlite-path",
                        str(sqlite_path),
                    ],
                ):
                    with self.assertRaises(SystemExit) as ctx:
                        script.main()
            self.assertEqual(ctx.exception.code, 4)
            self.assertEqual(
                script._detect_stored_dim(lancedb_path), int(settings.embedding_dim)
            )
        self.assertEqual(_files_status(sqlite_path), "indexed")


class TestActiveWriterRefusesWithExit5(_RealEngineTestCase):
    def test_active_writer_refuses_with_exit_5(self):
        """A held BEGIN IMMEDIATE write lock (a running app) must refuse at
        the CLI level (exit 5) with the LanceDB index intact."""
        self._require_real_engine()
        tmp = Path(tempfile.mkdtemp(prefix="dimexit5_"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        lancedb_path = tmp / "lancedb"
        sqlite_path = tmp / "app.db"
        stored_dim = int(settings.embedding_dim) + 8
        _make_sqlite(sqlite_path)
        holder = sqlite3.connect(str(sqlite_path))
        try:
            holder.execute("BEGIN IMMEDIATE")
            holder.execute("UPDATE files SET status='indexed' WHERE id=1")
            with self._real_engine():
                _make_real_table(lancedb_path, stored_dim)
                with patch.object(
                    sys,
                    "argv",
                    [
                        "migrate_embeddings.py",
                        "--lancedb-path",
                        str(lancedb_path),
                        "--sqlite-path",
                        str(sqlite_path),
                    ],
                ):
                    with self.assertRaises(SystemExit) as ctx:
                        script.main()
                self.assertEqual(ctx.exception.code, 5)
                self.assertEqual(script._detect_stored_dim(lancedb_path), stored_dim)
        finally:
            holder.close()
        self.assertEqual(_files_status(sqlite_path), "indexed")


class TestRepairModeDryRun(unittest.TestCase):
    def test_repair_mode_dry_run_reports_and_writes_nothing(self):
        """The interrupted-run repair (present-but-empty index + rows
        claiming vectors) in --dry-run mode reports the stranded rows
        without touching them."""
        tmp = Path(tempfile.mkdtemp(prefix="dimrepairdry_"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        lancedb_path = tmp / "lancedb"
        sqlite_path = tmp / "app.db"
        lancedb_path.mkdir()  # present but empty: the post-crash re-run state
        _make_sqlite(sqlite_path)  # one 'indexed' row claiming 3 chunks
        ret = script.run_migration(
            lancedb_path=lancedb_path,
            sqlite_path=sqlite_path,
            dry_run=True,
            force=False,
        )
        self.assertEqual(ret, 1)  # the stranded row would be reset
        self.assertEqual(_files_status(sqlite_path), "indexed")
        self.assertFalse(any(lancedb_path.iterdir()), "dry run must not wipe")


class TestCorruptSqliteRefuses(unittest.TestCase):
    """Issue #694 implementation-review finding: an unreadable SQLite file
    is a clean refusal inside the documented exit contract, never a bare
    traceback (exit 1) and never the misleading stop-the-app advice."""

    def _corrupt_db(self, sqlite_path):
        sqlite_path.write_bytes(b"this is not a sqlite database at all" * 8)

    def test_corrupt_db_in_repair_detection_refuses_with_exit_6(self):
        """Corrupt sqlite + present-but-empty index: the repair-detection
        read refuses with exit code 6 (restore-from-backup message), not an
        uncaught DatabaseError."""
        tmp = Path(tempfile.mkdtemp(prefix="dimcorrupt6_"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        lancedb_path = tmp / "lancedb"
        sqlite_path = tmp / "app.db"
        lancedb_path.mkdir()
        self._corrupt_db(sqlite_path)
        with self.assertRaises(SystemExit) as ctx:
            script.run_migration(
                lancedb_path=lancedb_path,
                sqlite_path=sqlite_path,
                dry_run=False,
                force=False,
            )
        self.assertEqual(ctx.exception.code, 6)
        self.assertFalse(any(lancedb_path.iterdir()),
                         "index must remain present (nothing wiped)")

    def test_corrupt_db_with_force_names_the_real_cause(self):
        """Corrupt sqlite + --force: the writer pre-flight refuses
        (MigrationRefused -> main exit 5) with an unreadable/corrupt
        message, NOT the stop-the-application advice."""
        tmp = Path(tempfile.mkdtemp(prefix="dimcorrupt5_"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        lancedb_path = tmp / "lancedb"
        sqlite_path = tmp / "app.db"
        self._corrupt_db(sqlite_path)
        with self.assertRaises(script.MigrationRefused) as ctx:
            script._assert_no_active_writer(sqlite_path)
        self.assertIn("unreadable or corrupt", str(ctx.exception))
        self.assertNotIn("docker compose stop", str(ctx.exception))


class TestResetPostProbeLock(_RealEngineTestCase):
    def test_lock_acquired_after_probe_refuses_from_reset(self):
        """PRR-001: a writer that acquires the lock in the probe-to-reset
        window surfaces as ``MigrationRefused`` (-> exit 5 via ``main()``),
        not a bare OperationalError traceback — and the wipe never runs."""
        self._require_real_engine()
        tmp = Path(tempfile.mkdtemp(prefix="dimtoctou_"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        lancedb_path = tmp / "lancedb"
        sqlite_path = tmp / "app.db"
        stored_dim = int(settings.embedding_dim) + 8
        _make_sqlite(sqlite_path)
        holder = sqlite3.connect(str(sqlite_path))
        try:
            holder.execute("BEGIN IMMEDIATE")
            holder.execute("UPDATE files SET status='indexed' WHERE id=1")
            with self._real_engine():
                _make_real_table(lancedb_path, stored_dim)
                # Simulate the window: the probe already passed (no-op), so
                # the reset itself meets the held lock.
                with patch.object(script, "_assert_no_active_writer", lambda p: None):
                    with self.assertRaises(script.MigrationRefused) as ctx:
                        script.run_migration(
                            lancedb_path=lancedb_path,
                            sqlite_path=sqlite_path,
                            dry_run=False,
                            force=False,
                        )
            self.assertIn("write lock", str(ctx.exception))
            self.assertEqual(script._detect_stored_dim(lancedb_path), stored_dim)
        finally:
            holder.close()
        self.assertEqual(_files_status(sqlite_path), "indexed")


class TestCountProbeLockBranch(unittest.TestCase):
    def test_operationalerror_in_count_probe_raises_migration_refused(self):
        """PRR-015(c): a lock error during the repair-detection read follows
        the same ``MigrationRefused`` contract as the writer probe. A plain
        read rarely blocks (only EXCLUSIVE does), so the branch is exercised
        through the documented error type rather than a real lock state."""
        locked_conn = MagicMock()
        locked_conn.execute.side_effect = sqlite3.OperationalError(
            "database is locked"
        )
        tmp = Path(tempfile.mkdtemp(prefix="dimcountlock_"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        sqlite_path = tmp / "app.db"
        sqlite_path.write_bytes(b"placeholder")  # exists; never really opened
        with patch.object(script.sqlite3, "connect", return_value=locked_conn):
            with self.assertRaises(script.MigrationRefused) as ctx:
                script._count_rows_claiming_vectors(sqlite_path)
        self.assertIn("write lock", str(ctx.exception))
        locked_conn.close.assert_called_once()


class TestForceOverridesSettingsRefusal(_RealEngineTestCase):
    def test_force_proceeds_when_settings_fail(self):
        """PRR-015(a): ``--force`` overrides the exit-4 settings refusal —
        the documented escape hatch proceeds with reset + wipe."""
        self._require_real_engine()
        tmp = Path(tempfile.mkdtemp(prefix="dimforce4_"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        lancedb_path = tmp / "lancedb"
        sqlite_path = tmp / "app.db"
        _make_sqlite(sqlite_path)
        with self._real_engine():
            _make_real_table(lancedb_path, int(settings.embedding_dim))
            with patch.dict(sys.modules, {"app.config": None}):
                ret = script.run_migration(
                    lancedb_path=lancedb_path,
                    sqlite_path=sqlite_path,
                    dry_run=False,
                    force=True,
                )
        self.assertEqual(ret, 1)
        self.assertIsNone(script._detect_stored_dim(lancedb_path))
        self.assertEqual(_files_status(sqlite_path), "pending")


class TestCorruptDbExit5ViaMain(_RealEngineTestCase):
    def test_corrupt_db_at_probe_maps_to_exit_5(self):
        """PRR-015(b): the corrupt-database ``MigrationRefused`` maps to CLI
        exit code 5 through ``main()``, same as the writer-lock refusal."""
        self._require_real_engine()
        tmp = Path(tempfile.mkdtemp(prefix="dimcorruptmain_"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        lancedb_path = tmp / "lancedb"
        sqlite_path = tmp / "app.db"
        stored_dim = int(settings.embedding_dim) + 8
        sqlite_path.write_bytes(b"this is not a sqlite database at all" * 8)
        with self._real_engine():
            _make_real_table(lancedb_path, stored_dim)
            with patch.object(
                sys,
                "argv",
                [
                    "migrate_embeddings.py",
                    "--lancedb-path",
                    str(lancedb_path),
                    "--sqlite-path",
                    str(sqlite_path),
                ],
            ):
                with self.assertRaises(SystemExit) as ctx:
                    script.main()
        self.assertEqual(ctx.exception.code, 5)
        self.assertEqual(script._detect_stored_dim(lancedb_path), stored_dim)


class TestDryRunNeverRefusesOnWriter(_RealEngineTestCase):
    def test_dry_run_completes_while_writer_holds_lock(self):
        """PRR-015(d): ``--dry-run`` never runs the writer probe, so it
        reports instead of refusing even when a writer holds the database."""
        self._require_real_engine()
        tmp = Path(tempfile.mkdtemp(prefix="dimdrywriter_"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        lancedb_path = tmp / "lancedb"
        sqlite_path = tmp / "app.db"
        stored_dim = int(settings.embedding_dim) + 8
        _make_sqlite(sqlite_path)
        holder = sqlite3.connect(str(sqlite_path))
        try:
            holder.execute("BEGIN IMMEDIATE")
            holder.execute("UPDATE files SET status='indexed' WHERE id=1")
            with self._real_engine():
                _make_real_table(lancedb_path, stored_dim)
                ret = script.run_migration(
                    lancedb_path=lancedb_path,
                    sqlite_path=sqlite_path,
                    dry_run=True,
                    force=False,
                )
            self.assertEqual(ret, 1)
            self.assertEqual(script._detect_stored_dim(lancedb_path), stored_dim)
        finally:
            holder.close()
        self.assertEqual(_files_status(sqlite_path), "indexed")


class TestMainExit1UnresolvedPaths(unittest.TestCase):
    def test_unresolved_paths_with_failing_settings_exit_1(self):
        """PRR-015(e): without explicit paths and with settings unloadable,
        ``main()`` exits 1 with the pass-the-paths guidance (distinct from
        the exit-4 in-migration settings refusal, which requires explicit
        paths to reach)."""
        with patch.dict(sys.modules, {"app.config": None}):
            with patch.object(sys, "argv", ["migrate_embeddings.py"]):
                with self.assertRaises(SystemExit) as ctx:
                    script.main()
        self.assertEqual(ctx.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
