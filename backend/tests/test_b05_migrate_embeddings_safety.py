"""scripts/migrate_embeddings.py safety checks (issue #694, Workstream B
PR 5) — author-side acceptance checks for the issue-tracer trace.

Each test asserts the POST-FIX behavior, so on the unfixed tree every one
of them fails for a specific, reportable reason:

- the script wipes the LanceDB directory BEFORE resetting SQLite file
  statuses, so a failure between the two strands ``status='indexed'``
  rows over an empty index — and a re-run over the empty index prints
  "Nothing to migrate" and returns 0 without ever consulting SQLite;
- the reset predicate is ``WHERE status='indexed'`` only, so ``partial``
  rows are left pointing at the wiped index;
- a settings-import failure degrades to "Could not detect stored
  dimension. Proceeding with migration." and wipes even a
  dimension-MATCHING index without requiring --force;
- the reset never touches ``files.phase``;
- ``--help`` exits 1 under PYTHONIOENCODING=cp1252 with piped stdout
  (the U+2192 arrow in the argparse description is not encodable).

Tests that need the real lancedb engine are skipped ONLY when the real
package is not importable (backend/conftest.py stubs lancedb/pyarrow when
the real import fails; CI stub environments therefore skip the
engine-dependent checks, while the no-engine checks — the re-run repair
and the --help encoding — run everywhere).
"""

import importlib.machinery
import importlib.util
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "migrate_embeddings.py"

sys.path.insert(0, str(_REPO_ROOT))

_spec = importlib.util.spec_from_file_location("migrate_embeddings_b05_test", _SCRIPT)
script = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(script)

from app.config import settings  # noqa: E402
from app.models.database import run_migrations  # noqa: E402


def _load_real_module(name, deps=None, purge_prefixes=()):
    """Load the REAL installed package even when backend/conftest.py has
    installed its test stub into sys.modules.

    - ``deps`` maps module names to already-loaded real modules that must be
      visible during execution (the real lancedb imports pyarrow at exec
      time);
    - ``purge_prefixes`` are stub submodule prefixes (e.g. the conftest
      ``lancedb.expr`` stub) that must be removed during execution so the
      real package's own ``from .expr import Expr`` resolves to the real
      submodule.

    sys.modules is restored afterwards; the returned module keeps working
    because it was executed under its own name.

    When the real package is ALREADY the active sys.modules entry
    (backend/conftest.py imports lancedb/pyarrow at session start and only
    stubs them when the real import fails), it is returned as-is: purging and
    re-executing the real lancedb would re-initialize its Rust extension,
    which panics on a second initialization in the same process
    (pyo3 PanicException: env_logger::init_from_env should not be called
    after logger initialized: SetLoggerError(()))."""
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


def _make_minimal_sqlite(sqlite_path, rows):
    """Minimal files table for the direct run_migration checks:
    ``rows`` is a list of ``(id, status, chunk_count)`` tuples."""
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
    conn.executemany(
        "INSERT INTO files (id, file_name, status, chunk_count)"
        " VALUES (?, 'doc.txt', ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()


def _files_column(sqlite_path, column, file_id=1):
    conn = sqlite3.connect(str(sqlite_path))
    value = conn.execute(
        f"SELECT {column} FROM files WHERE id = ?", (file_id,)
    ).fetchone()[0]
    conn.close()
    return value


def _indexed_row_count(sqlite_path):
    conn = sqlite3.connect(str(sqlite_path))
    count = conn.execute(
        "SELECT COUNT(*) FROM files WHERE status = 'indexed'"
    ).fetchone()[0]
    conn.close()
    return count


class _RealEngineTestCase(unittest.TestCase):
    """Shared helpers: swap the conftest stubs for the real engines (both
    the fixture writes and the script's own lancedb import need it)."""

    def _real_engine(self):
        return patch.dict(
            sys.modules, {"lancedb": _REAL_LANCEDB, "pyarrow": _REAL_PYARROW}
        )

    def _require_real_engine(self):
        if _REAL_LANCEDB is None or _REAL_PYARROW is None:
            self.skipTest("real lancedb/pyarrow not installed (CI stub environment)")

    def _make_real_table(self, lancedb_path, dim):
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


class TestResetFailureLeavesNoStrandedRows(_RealEngineTestCase):
    def test_reset_failure_leaves_no_indexed_rows_with_empty_index(self):
        """A crash between the wipe and the status reset must not be
        observable as 'indexed rows over an empty index' (wipe-before-reset
        strands them, and a re-run then reports nothing to migrate)."""
        self._require_real_engine()
        tmp = Path(tempfile.mkdtemp(prefix="b05_resetfail_"))
        lancedb_path = tmp / "lancedb"
        sqlite_path = tmp / "app.db"
        _make_minimal_sqlite(sqlite_path, [(1, "indexed", 5)])

        def _locked_reset(path, dry_run):
            raise sqlite3.OperationalError("database is locked")

        with self._real_engine():
            # genuine mismatch: stored dim differs from the configured one
            self._make_real_table(lancedb_path, int(settings.embedding_dim) + 8)
            with patch.object(script, "_reset_file_statuses", _locked_reset):
                try:
                    script.run_migration(
                        lancedb_path=lancedb_path,
                        sqlite_path=sqlite_path,
                        dry_run=False,
                        force=False,
                    )
                except Exception:
                    pass  # the crash between wipe and reset is the scenario
            indexed_row_count = _indexed_row_count(sqlite_path)
            dim = script._detect_stored_dim(lancedb_path)
        inconsistent = int(indexed_row_count > 0 and dim is None)
        assert inconsistent == 0, "indexed rows must never point at an empty index"


class TestRerunRepairsStrandedRows(unittest.TestCase):
    def test_rerun_repairs_indexed_rows_with_empty_index(self):
        """Re-running over an EMPTY index must still consult SQLite and
        repair 'indexed' rows (base: "Nothing to migrate" returns 0 and the
        stranded row is never reset). Needs NO real engine — an empty
        directory detects as None regardless of the engine."""
        tmp = Path(tempfile.mkdtemp(prefix="b05_rerun_"))
        lancedb_path = tmp / "lancedb"
        sqlite_path = tmp / "app.db"
        lancedb_path.mkdir()  # empty index: the post-crash re-run state
        _make_minimal_sqlite(sqlite_path, [(1, "indexed", 5)])
        script.run_migration(
            lancedb_path=lancedb_path,
            sqlite_path=sqlite_path,
            dry_run=False,
            force=False,
        )
        row_status = _files_column(sqlite_path, "status")
        assert row_status == "pending"


class TestResetPredicateCoversPartialRows(_RealEngineTestCase):
    def test_partial_rows_are_reset_on_wipe(self):
        """The wipe applies to the WHOLE index, so the reset predicate may
        not spare 'partial' rows — they point at the same wiped vectors."""
        self._require_real_engine()
        tmp = Path(tempfile.mkdtemp(prefix="b05_partial_"))
        lancedb_path = tmp / "lancedb"
        sqlite_path = tmp / "app.db"
        _make_minimal_sqlite(sqlite_path, [(1, "indexed", 3), (2, "partial", 4)])
        with self._real_engine():
            self._make_real_table(lancedb_path, int(settings.embedding_dim) + 8)
            script.run_migration(
                lancedb_path=lancedb_path,
                sqlite_path=sqlite_path,
                dry_run=False,
                force=False,
            )
        row_status = _files_column(sqlite_path, "status", file_id=2)
        assert row_status == "pending"


class TestSettingsFailureDoesNotWipe(_RealEngineTestCase):
    def test_settings_load_failure_does_not_wipe_without_force(self):
        """A settings-import failure must not degrade to "Proceeding with
        migration" — a dimension-MATCHING index may not be wiped unless
        --force was passed (base: configured_dim=None falls through to the
        wipe)."""
        self._require_real_engine()
        tmp = Path(tempfile.mkdtemp(prefix="b05_settings_"))
        lancedb_path = tmp / "lancedb"
        sqlite_path = tmp / "app.db"
        stored_dim = int(settings.embedding_dim)
        _make_minimal_sqlite(sqlite_path, [(1, "indexed", 3)])
        with self._real_engine():
            self._make_real_table(lancedb_path, stored_dim)
            with patch.dict(sys.modules, {"app.config": None}):
                try:
                    script.run_migration(
                        lancedb_path=lancedb_path,
                        sqlite_path=sqlite_path,
                        dry_run=False,
                        force=False,
                    )
                except SystemExit:
                    pass  # refusing to migrate without a known config is right
            dim = script._detect_stored_dim(lancedb_path) or 0
        assert dim == stored_dim


class TestConcurrentWriterDoesNotCauseWipe(_RealEngineTestCase):
    def test_concurrent_writer_does_not_cause_wipe(self):
        """A SQLite write lock held by a concurrent writer during the reset
        must not mean the index was already wiped (base: the wipe runs
        first, then the locked UPDATE raises OperationalError after the
        default ~5s connect timeout)."""
        self._require_real_engine()
        tmp = Path(tempfile.mkdtemp(prefix="b05_locked_"))
        lancedb_path = tmp / "lancedb"
        sqlite_path = tmp / "app.db"
        stored_dim = int(settings.embedding_dim) + 8
        _make_minimal_sqlite(sqlite_path, [(1, "indexed", 5)])
        holder = sqlite3.connect(str(sqlite_path))
        try:
            holder.execute("BEGIN IMMEDIATE")
            holder.execute("UPDATE files SET status='indexed' WHERE id=1")
            with self._real_engine():
                self._make_real_table(lancedb_path, stored_dim)
                try:
                    script.run_migration(
                        lancedb_path=lancedb_path,
                        sqlite_path=sqlite_path,
                        dry_run=False,
                        force=False,
                    )
                except Exception:
                    pass  # the locked-reset crash is the scenario under test
                dim = script._detect_stored_dim(lancedb_path) or 0
        finally:
            holder.close()
        assert dim == stored_dim


class TestResetMatchesLegacySweepPredicate(_RealEngineTestCase):
    def test_reset_rows_match_legacy_sweep_predicate(self):
        """The reset must restore ``files.phase`` to the queue signal too —
        not just ``status`` (base: a reset row keeps phase='indexed' on the
        app's real schema from run_migrations)."""
        self._require_real_engine()
        tmp = Path(tempfile.mkdtemp(prefix="b05_phase_"))
        lancedb_path = tmp / "lancedb"
        sqlite_path = tmp / "app.db"
        run_migrations(str(sqlite_path))
        conn = sqlite3.connect(str(sqlite_path))
        conn.execute(
            "INSERT INTO files (vault_id, file_path, file_name, file_size,"
            " status, phase, chunk_count)"
            " VALUES (1, 'kv/doc.txt', 'doc.txt', 128, 'indexed', 'indexed', 5)"
        )
        conn.commit()
        conn.close()
        with self._real_engine():
            self._make_real_table(lancedb_path, int(settings.embedding_dim) + 8)
            script.run_migration(
                lancedb_path=lancedb_path,
                sqlite_path=sqlite_path,
                dry_run=False,
                force=False,
            )
        phase = _files_column(sqlite_path, "phase")
        assert phase == "queued"


class TestHelpEncodingUnderCp1252(unittest.TestCase):
    def test_help_exits_zero_under_cp1252(self):
        """--help must be printable on a cp1252 console (base: the U+2192
        arrow in the argparse description raises UnicodeEncodeError and
        exits 1). Needs NO engine — the script imports only stdlib before
        argparse runs."""
        env = {
            k: v
            for k, v in os.environ.items()
            if k != "PYTHONUTF8"  # UTF-8 mode would override the encoding pin
        }
        env["PYTHONIOENCODING"] = "cp1252"
        proc = subprocess.run(
            [sys.executable, str(_SCRIPT), "--help"],
            env=env,
            capture_output=True,
        )
        assert proc.returncode == 0


if __name__ == "__main__":
    unittest.main()
