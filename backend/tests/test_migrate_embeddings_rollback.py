"""Acceptance checks for issue #1094 — migrate_embeddings wipe guards.

Frozen issue-tracer checks. Each test encodes the POST-fix contract, so it is
RED at base, GREEN after the fix:

- AC1 (test_wipe_preserves_timestamped_rollback): the destructive path of
  ``run_migration`` must preserve the previous non-empty LanceDB index under a
  timestamped rollback directory (a sibling of the LanceDB directory) and name
  that directory in the run output, while the live directory is recreated
  empty. At base the wipe is a bare ``shutil.rmtree`` with no rollback copy.
- AC2 (test_probe_sql_failure_refuses): a ``sqlite3.OperationalError`` raised
  by the streamed claimed-id probe must refuse with ``MigrationRefused`` (exit
  5 via ``main()``) BEFORE any status reset or wipe. At base the lance-side
  ``except Exception`` swallows the SQLite failure into ``None`` ("no
  evidence"), the disjoint-identity guard is skipped, and the run completes
  the destructive path end to end.

Both checks build a REAL LanceDB chunks index (production shape: a string
``file_id`` column) plus a real sqlite file, mirroring the executed Phase-2
probes M1/M2/M3b. The real-engine guard only fires on machines without the
real lancedb/pyarrow packages — CI installs both for real
(docs/engineering/testing.md).
"""

import importlib.util
import io
import logging
import re
import sqlite3
import sys
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "migrate_embeddings.py"

sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "backend"))

_spec = importlib.util.spec_from_file_location("mig_embs_checks", _SCRIPT)
script = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(script)

import lancedb as _lancedb  # noqa: E402
import pyarrow as _pyarrow  # noqa: E402

from app.config import settings  # noqa: E402


def _require_real_engine():
    """Skip only when conftest installed its stub (real package not present).

    backend/conftest.py imports the real lancedb/pyarrow at session start and
    only stubs them when the real import fails, so CI (which installs both)
    always runs these checks for real — the same guard the sibling modules
    test_b05_migrate_embeddings_safety.py / test_migrate_embeddings_cli_refusals.py
    use. The synthetic stub has no ``__file__``.
    """
    if (
        getattr(_lancedb, "__file__", None) is None
        or getattr(_pyarrow, "__file__", None) is None
    ):
        pytest.skip("real lancedb/pyarrow not installed (CI stub environment)")


def _make_chunks_index(lancedb_path, file_ids, dim):
    """Build a real, non-empty chunks table in the production shape (#1094):
    a string ``file_id`` column whose values are the file ids owning chunks."""
    db = _lancedb.connect(str(lancedb_path))
    schema = _pyarrow.schema(
        [
            ("id", _pyarrow.string()),
            ("file_id", _pyarrow.string()),
            ("text", _pyarrow.string()),
            ("embedding", _pyarrow.list_(_pyarrow.float32(), dim)),
        ]
    )
    table = db.create_table("chunks", schema=schema)
    table.add(
        [
            {
                "id": f"c{i}",
                "file_id": str(fid),
                "text": "chunk text",
                "embedding": [0.01] * dim,
            }
            for i, fid in enumerate(file_ids)
        ]
    )
    return table


def _make_files_db(path, rows):
    """sqlite file with claiming rows; ``rows`` are (id, status, chunk_count)."""
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE files (
            id INTEGER PRIMARY KEY, file_name TEXT, status TEXT,
            chunk_count INTEGER, processed_at TIMESTAMP, modified_at TIMESTAMP
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


def _files_rows(path):
    conn = sqlite3.connect(str(path))
    out = conn.execute(
        "SELECT id, status, chunk_count FROM files ORDER BY id"
    ).fetchall()
    conn.close()
    return out


def _capture_run(**kwargs):
    """Run ``run_migration`` capturing the run output (prints AND the script's
    own log records) into one buffer; the rollback naming may land on either
    stream, and both are operator-visible run output."""
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    script.logger.addHandler(handler)
    try:
        with redirect_stdout(buf):
            ret = script.run_migration(**kwargs)
    finally:
        script.logger.removeHandler(handler)
    return ret, buf.getvalue()


def test_wipe_preserves_timestamped_rollback(tmp_path):
    """AC1 (#1094): the destructive wipe preserves the previous index under a
    timestamped rollback directory named in the run output."""
    _require_real_engine()
    lancedb_path = tmp_path / "lancedb"
    sqlite_path = tmp_path / "app.db"
    # A live index whose chunks carry file ids 1..100, and claiming rows whose
    # ids overlap them (every deployment numbers files.id from 1) — the exact
    # shape that passes the base exit-8 identity check and reaches the wipe.
    _make_chunks_index(lancedb_path, range(1, 101), int(settings.embedding_dim))
    _make_files_db(sqlite_path, [(1, "indexed", 5), (2, "indexed", 4)])
    assert (lancedb_path / "chunks.lance").exists(), "precondition: non-empty index"
    # Content marker: proves the rollback directory holds the PREVIOUS index's
    # actual bytes, not a same-named empty directory (plan-critic round 1).
    (lancedb_path / "PRE-ROLLBACK-CONTENT.marker").write_text(
        "previous index contents", encoding="utf-8"
    )

    ret, out = _capture_run(
        lancedb_path=lancedb_path,
        sqlite_path=sqlite_path,
        dry_run=False,
        force=True,
    )
    assert ret == 2, "both claiming rows were reset on the destructive path"

    # (a) a rollback directory with the EXACT naming contract
    # (<name>.rollback-YYYYMMDD-HHMMSS) exists as a SIBLING of the LanceDB
    # directory and holds the PREVIOUS index contents (the chunks data plus
    # the pre-rollback marker).
    rollback_dirs = [
        p
        for p in lancedb_path.parent.iterdir()
        if p.is_dir()
        and p != lancedb_path
        and re.fullmatch(
            rf"{re.escape(lancedb_path.name)}\.rollback-\d{{8}}-\d{{6}}", p.name
        )
    ]
    assert rollback_dirs, (
        "no timestamped rollback directory sibling of the LanceDB dir holds "
        "the previous index contents (#1094) — the wipe destroyed it; parent "
        f"entries: {sorted(p.name for p in lancedb_path.parent.iterdir())}"
    )
    rollback = rollback_dirs[0]
    assert (rollback / "chunks.lance").exists(), (
        "rollback directory does not hold the previous chunks data (#1094)"
    )
    assert (rollback / "PRE-ROLLBACK-CONTENT.marker").exists(), (
        "rollback directory does not hold the previous index contents (#1094)"
    )

    # (b) the run output names that rollback directory.
    assert str(rollback) in out or rollback.name in out, (
        f"run output does not name the rollback directory (#1094): {rollback.name!r}"
    )

    # (c) the live LanceDB directory itself is recreated, empty of the old data.
    assert lancedb_path.exists()
    assert not (lancedb_path / "chunks.lance").exists()
    assert not (lancedb_path / "PRE-ROLLBACK-CONTENT.marker").exists()


def test_probe_sql_failure_refuses(tmp_path):
    """AC2 (#1094): a SQLite error inside the streamed claimed-id probe
    refuses with MigrationRefused before any reset or wipe."""
    _require_real_engine()
    lancedb_path = tmp_path / "lancedb"
    sqlite_path = tmp_path / "app.db"
    _make_chunks_index(lancedb_path, range(1, 101), int(settings.embedding_dim))
    # DISJOINT claiming rows (501 not among the index's 1..100): with a
    # working probe this database is refused (exit 8) — proving the patched
    # leg below proceeds ONLY because the probe failure was swallowed.
    _make_files_db(sqlite_path, [(501, "indexed", 5)])
    _sink = io.StringIO()
    with redirect_stdout(_sink):
        with pytest.raises(SystemExit) as ctrl:
            script.run_migration(
                lancedb_path=lancedb_path,
                sqlite_path=sqlite_path,
                dry_run=False,
                force=True,
            )
    assert ctrl.value.code == 8, "control: the disjoint database is refused"
    assert _files_rows(sqlite_path) == [(501, "indexed", 5)]
    assert (lancedb_path / "chunks.lance").exists()

    # A claimed-id stream that dies with a SQLite-layer error before the
    # first fetch (generator body: the raise fires on first iteration, inside
    # the probe — not at the call site).
    def _locked_stream(_sqlite_path):
        raise sqlite3.OperationalError("database is locked")
        yield  # pragma: no cover - makes this a generator function

    with patch.object(script, "_iter_claimed_id_batches", _locked_stream):
        with pytest.raises(script.MigrationRefused):
            with redirect_stdout(io.StringIO()):
                script.run_migration(
                    lancedb_path=lancedb_path,
                    sqlite_path=sqlite_path,
                    dry_run=False,
                    force=True,
                )

    # The refusal fired BEFORE any reset or wipe: rows keep claiming vectors
    # and the index keeps its data.
    assert _files_rows(sqlite_path) == [(501, "indexed", 5)], (
        "rows were reset despite the probe's SQLite failure (#1094)"
    )
    assert (lancedb_path / "chunks.lance").exists(), (
        "the index was wiped despite the probe's SQLite failure (#1094)"
    )
