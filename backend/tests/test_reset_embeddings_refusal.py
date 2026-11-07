"""Acceptance checks for issue #1080 — reset_embeddings refuses unusable
databases BEFORE any wipe.

Frozen issue-tracer checks. Each test encodes the POST-fix contract, so it is
RED at base, GREEN after the fix. The Phase-2 reproduction falsified the
issue's stated mechanism (the wipe does NOT run on these states at base): with
no ``files`` table — or a missing database file, which ``get_db_connection``
first materializes as a stray empty file — the unguarded core
``UPDATE files ...`` crashes with an unhandled ``sqlite3.OperationalError``
traceback instead of the sibling migrate guard's taxonomy refusal. The checks
therefore pin the CLEAN-REFUSAL semantics the ACs demand:

- AC4 (test_no_files_table_refuses_before_wipe): a sqlite file WITHOUT a
  ``files`` table plus a sentinel LanceDB directory must produce a clean
  non-zero refusal naming BOTH paths (a refusal message, not just the startup
  banner), never an unhandled sqlite traceback — and the index survives.
- AC5 (test_missing_database_refuses_before_wipe): same shape with the sqlite
  path missing entirely; additionally the refusal must fire BEFORE any
  database file is created (at base the script materializes a stray empty
  database via ``sqlite3.connect`` before crashing).

Importing scripts/reset_embeddings.py pulls in backend.app.config settings,
which validates env vars; backend/tests/conftest.py sets
ADMIN_SECRET_TOKEN/USERS_ENABLED/JWT_SECRET_KEY in pytest_configure (before
collection), so a normal module-level import is fine here.
"""

import importlib.util
import io
import sqlite3
import sys
import types
from contextlib import redirect_stdout
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "reset_embeddings.py"

sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "backend"))

_spec = importlib.util.spec_from_file_location("rst_embs_checks", _SCRIPT)
rst = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rst)


def _run_reset(monkeypatch, db_path, lancedb_dir):
    """Point the loaded script at the temp stores and capture its behavior.

    The real Settings' sqlite_path is a read-only property, so the script's
    module global is swapped for a plain namespace (the test_b16 pattern).
    Returns (captured stdout, raised exception-or-None); the assertions —
    not this harness — discriminate clean refusals from crashes.
    """
    monkeypatch.setattr(
        rst,
        "settings",
        types.SimpleNamespace(sqlite_path=str(db_path), lancedb_path=lancedb_dir),
    )
    buf = io.StringIO()
    raised = None
    try:
        with redirect_stdout(buf):
            rst.reset_all_embeddings()
    except BaseException as exc:  # noqa: BLE001 - probe captures everything
        raised = exc
    return buf.getvalue(), raised


def _assert_clean_refusal_naming_both_paths(out, raised, db_path, lancedb_dir):
    """The #1080 refusal contract: SystemExit(8) mirroring the migrate guard's
    taxonomy, both paths named in a refusal message, no sqlite traceback."""
    low = out.lower()
    # (a) a CLEAN refusal — never an unhandled sqlite3.OperationalError.
    assert not isinstance(raised, sqlite3.OperationalError), (
        "reset_embeddings died with an unhandled sqlite3.OperationalError "
        f"traceback instead of a clean refusal (#1080): {raised!r}"
    )
    # (b) the refusal is a real non-zero exit with the migrate guard's exit-8
    # taxonomy code — a printed ERROR alone (function returning normally) is
    # NOT a refusal (plan-critic round 1 tightening).
    assert isinstance(raised, SystemExit) and raised.code == 8, (
        "reset_embeddings must refuse with SystemExit(8) mirroring the "
        f"migrate guard taxonomy (#1080); raised={raised!r}"
    )
    printed_refusal = "refus" in low or "error" in low
    assert printed_refusal, (
        "output carries no refusal-indicating phrase (#1080); output tail="
        + repr(out[-400:])
    )
    # (c) the REFUSAL BLOCK itself names BOTH the sqlite path and the LanceDB
    # path — scoped to the output AFTER the "ERROR:" line so the startup
    # banner (which prints both paths before any check runs) cannot satisfy
    # the assertion on its own (implementation-review round 1, M4 probe).
    refusal_block = out.split("ERROR:", 1)[-1] if "ERROR:" in out else ""
    assert refusal_block, (
        "no ERROR refusal block in the output (#1080); output tail="
        + repr(out[-400:])
    )
    assert str(db_path) in refusal_block, (
        f"the refusal block does not name the sqlite path (#1080): {str(db_path)!r}"
    )
    assert str(lancedb_dir) in refusal_block, (
        f"the refusal block does not name the LanceDB path (#1080): {str(lancedb_dir)!r}"
    )


def test_no_files_table_refuses_before_wipe(tmp_path, monkeypatch):
    """AC4 (#1080): files table absent + LanceDB index present -> clean
    non-zero refusal naming both paths, before any rmtree; index survives."""
    db_path = tmp_path / "app.db"
    conn = sqlite3.connect(str(db_path))
    # A sqlite file WITHOUT a files table (a single unrelated table).
    conn.executescript("CREATE TABLE unrelated (id INTEGER PRIMARY KEY);")
    conn.commit()
    conn.close()
    lancedb_dir = tmp_path / "lancedb"
    lancedb_dir.mkdir()
    marker = lancedb_dir / "sentinel.marker"
    marker.write_text("live vectors", encoding="utf-8")

    out, raised = _run_reset(monkeypatch, db_path, lancedb_dir)

    _assert_clean_refusal_naming_both_paths(out, raised, db_path, lancedb_dir)
    # (c) the index survived: no wipe ran before the refusal.
    assert marker.exists(), "the sentinel index file was destroyed (#1080)"
    assert lancedb_dir.is_dir()


def test_missing_database_refuses_before_wipe(tmp_path, monkeypatch):
    """AC5 (#1080): database file missing + LanceDB index present -> clean
    non-zero refusal naming both paths, before any wipe AND before any
    database file is created; index survives."""
    db_path = tmp_path / "app.db"  # deliberately NOT created
    lancedb_dir = tmp_path / "lancedb"
    lancedb_dir.mkdir()
    marker = lancedb_dir / "sentinel.marker"
    marker.write_text("live vectors", encoding="utf-8")
    assert not db_path.exists()

    out, raised = _run_reset(monkeypatch, db_path, lancedb_dir)

    _assert_clean_refusal_naming_both_paths(out, raised, db_path, lancedb_dir)
    # The refusal must fire BEFORE any database file is materialized at the
    # misconfigured path (at base get_db_connection's sqlite3.connect creates
    # a stray empty database before the crash).
    assert not db_path.exists(), (
        "reset_embeddings materialized a stray database file at the missing "
        "path before refusing (#1080)"
    )
    # The index survived: no wipe ran before the refusal.
    assert marker.exists(), "the sentinel index file was destroyed (#1080)"
    assert lancedb_dir.is_dir()
