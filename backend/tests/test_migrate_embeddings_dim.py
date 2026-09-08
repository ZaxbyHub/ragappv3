"""scripts/migrate_embeddings.py dimension-detection tests (issue #512
VECTOR-006).

Registered companion of the frozen issue-tracer check C7:

- primary detection path uses the INSTALLED LanceDB API (schema
  field("embedding").type.list_size, mirroring production
  VectorStore._get_expected_embedding_dim);
- a matching dimension is a no-op (no wipe, no status reset);
- a genuine mismatch reaches the explicit migration path (dry-run reports
  the scheduled reset without touching data);
- an UNDETECTABLE dimension on a non-empty index errors out (SystemExit)
  WITHOUT wiping or resetting — the operator must diagnose first.

Tests that need the real lancedb engine are skipped when the real package
is not importable (CI stubs lancedb; the real-engine runs are the
host-level corroboration path, per the issue #512 fix plan).
"""

import importlib.machinery
import importlib.util
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "migrate_embeddings.py"

sys.path.insert(0, str(_REPO_ROOT))

_spec = importlib.util.spec_from_file_location("migrate_embeddings_dim_test", _SCRIPT)
script = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(script)

from app.config import settings  # noqa: E402


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
    because it was executed under its own name."""
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


def _marker_files(lancedb_path):
    if not lancedb_path.exists():
        return set()
    return {
        str(p.relative_to(lancedb_path))
        for p in lancedb_path.rglob("*")
        if p.is_file()
    }


def _make_corrupt_index(lancedb_path):
    """An index directory whose table carries no readable dimension."""
    lance_dir = lancedb_path / "chunks.lance"
    lance_dir.mkdir(parents=True)
    (lance_dir / "_versions").mkdir()
    (lance_dir / "_versions" / "1-corrupted.manifest").write_bytes(
        b"\x00\x01 not a real manifest - no dimension information"
    )
    (lance_dir / "data").mkdir()
    (lance_dir / "data" / "0000-corrupted.lance").write_bytes(b"\xde\xad\xbe\xef")
    return _marker_files(lancedb_path)


class TestDetectStoredDim(unittest.TestCase):
    def test_missing_index_directory_returns_none(self):
        tmp = Path(tempfile.mkdtemp(prefix="dimdetect_absent_"))
        self.assertIsNone(script._detect_stored_dim(tmp / "lancedb"))

    def test_corrupt_index_returns_none_when_real_lancedb_unavailable(self):
        """In the CI stub environment neither the lancedb API path nor the
        raw pyarrow fallback can read the layout — detection must return
        None (never a guess)."""
        if _REAL_LANCEDB is not None:
            self.skipTest("real lancedb installed: covered by the real-engine tests")
        tmp = Path(tempfile.mkdtemp(prefix="dimdetect_corrupt_"))
        lance = tmp / "lancedb"
        _make_corrupt_index(lance)
        self.assertIsNone(script._detect_stored_dim(lance))

    def test_real_lancedb_table_dim_is_read_via_installed_api(self):
        """Primary path: a REAL lancedb table with embedding dim 1024 is
        detected as 1024 (the pre-fix pyarrow path returned None here and
        the script wiped a compatible index)."""
        if _REAL_LANCEDB is None or _REAL_PYARROW is None:
            self.skipTest("real lancedb/pyarrow not installed (CI stub environment)")
        tmp = Path(tempfile.mkdtemp(prefix="dimdetect_real_"))
        lancedb_path = tmp / "lancedb"
        with patch.dict(sys.modules, {"lancedb": _REAL_LANCEDB, "pyarrow": _REAL_PYARROW}):
            pa = _REAL_PYARROW
            db = _REAL_LANCEDB.connect(str(lancedb_path))
            schema = pa.schema(
                [
                    ("id", pa.string()),
                    ("text", pa.string()),
                    ("metadata", pa.string()),
                    ("embedding", pa.list_(pa.float32(), 1024)),
                ]
            )
            table = db.create_table("chunks", schema=schema)
            table.add(
                [
                    {
                        "id": "f1_0",
                        "text": "chunk text",
                        "metadata": "{}",
                        "embedding": [0.01] * 1024,
                    }
                ]
            )
            self.assertEqual(table.count_rows(), 1)
            detected = script._detect_stored_dim(lancedb_path)
        self.assertEqual(detected, 1024)


class TestRunMigrationUnknownDim(unittest.TestCase):
    def test_unknown_dim_errors_without_wiping(self):
        """Undetectable dimension + non-empty index -> explicit error,
        nonzero exit, NO wipe and NO status reset (the pre-fix behavior
        assumed migration was needed and wiped)."""
        tmp = Path(tempfile.mkdtemp(prefix="dimunknown_"))
        lancedb_path = tmp / "lancedb"
        sqlite_path = tmp / "app.db"
        markers = _make_corrupt_index(lancedb_path)
        _make_sqlite(sqlite_path)

        with patch.object(script, "_detect_stored_dim", lambda p: None):
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

        self.assertNotEqual(ctx.exception.code, 0)
        self.assertTrue(
            markers.issubset(_marker_files(lancedb_path)),
            "index files must not be wiped on undetectable dimension",
        )
        self.assertEqual(_files_status(sqlite_path), "indexed")


class TestRunMigrationRealEngine(unittest.TestCase):
    """Matching-dim no-op and genuine-mismatch dry-run against the REAL
    lancedb engine (host-level corroboration; skipped in the CI stub)."""

    def _real_engine(self):
        """Context manager swapping the conftest stubs for the real engines
        (fixture writes AND the script's own lancedb import both need it)."""
        return patch.dict(
            sys.modules, {"lancedb": _REAL_LANCEDB, "pyarrow": _REAL_PYARROW}
        )

    def _make_matching_table(self, lancedb_path, dim):
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

    def test_matching_dim_is_a_no_op(self):
        if _REAL_LANCEDB is None or _REAL_PYARROW is None:
            self.skipTest("real lancedb/pyarrow not installed (CI stub environment)")
        tmp = Path(tempfile.mkdtemp(prefix="dimmatch_"))
        lancedb_path = tmp / "lancedb"
        sqlite_path = tmp / "app.db"
        _make_sqlite(sqlite_path)
        markers_before = None

        with self._real_engine():
            self._make_matching_table(lancedb_path, int(settings.embedding_dim))
            markers_before = _marker_files(lancedb_path)
            ret = script.run_migration(
                lancedb_path=lancedb_path,
                sqlite_path=sqlite_path,
                dry_run=False,
                force=False,
            )

        self.assertEqual(ret, 0)
        self.assertTrue(markers_before.issubset(_marker_files(lancedb_path)))
        self.assertEqual(_files_status(sqlite_path), "indexed")

    def test_genuine_mismatch_dry_run_schedules_without_wiping(self):
        if _REAL_LANCEDB is None or _REAL_PYARROW is None:
            self.skipTest("real lancedb/pyarrow not installed (CI stub environment)")
        tmp = Path(tempfile.mkdtemp(prefix="dimmismatch_"))
        lancedb_path = tmp / "lancedb"
        sqlite_path = tmp / "app.db"
        stored_dim = int(settings.embedding_dim) + 8
        _make_sqlite(sqlite_path)
        markers_before = None

        with self._real_engine():
            self._make_matching_table(lancedb_path, stored_dim)
            markers_before = _marker_files(lancedb_path)
            ret = script.run_migration(
                lancedb_path=lancedb_path,
                sqlite_path=sqlite_path,
                dry_run=True,
                force=False,
            )

        self.assertEqual(ret, 1)  # one indexed file would be reset
        self.assertTrue(markers_before.issubset(_marker_files(lancedb_path)))
        self.assertEqual(_files_status(sqlite_path), "indexed")


if __name__ == "__main__":
    unittest.main()
