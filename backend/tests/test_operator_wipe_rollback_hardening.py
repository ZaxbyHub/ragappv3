"""Supplementary hardening coverage for the operator embedding scripts' wipe
guards (collated #1094 + #1080).

Unfrozen companion to the frozen checks (test_migrate_embeddings_rollback.py,
test_reset_embeddings_refusal.py): this module pins the plan-approved
semantics beyond the issue ACs (plan-critic round 1) — rename mechanics,
reset preflight taxonomy, D2 probe boundary legs, the R3 recoverability
window, and the 4.2 source-contract guardrail. All legs are RED at the
pre-fix tree and GREEN after the fix unless noted.
"""

import importlib.util
import io
import logging
import re
import sqlite3
import sys
import types
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "backend"))


def _load(name, script):
    spec = importlib.util.spec_from_file_location(name, _REPO_ROOT / "scripts" / script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mig = _load("mig_embs_hard", "migrate_embeddings.py")
rst = _load("rst_embs_hard", "reset_embeddings.py")

import lancedb as _lancedb  # noqa: E402
import pyarrow as _pyarrow  # noqa: E402

from app.config import settings  # noqa: E402


def _require_real_engine():
    if (
        getattr(_lancedb, "__file__", None) is None
        or getattr(_pyarrow, "__file__", None) is None
    ):
        pytest.skip("real lancedb/pyarrow not installed (CI stub environment)")


def _make_chunks_index(lancedb_path, file_ids, dim=8):
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


def _make_files_db(path, rows, columns=("file_name", "status", "chunk_count",
                                        "processed_at", "modified_at")):
    conn = sqlite3.connect(str(path))
    cols = ", ".join(("id INTEGER PRIMARY KEY",) + tuple(
        f"{c} TEXT" if c in ("file_name", "status") else f"{c} INTEGER"
        if c == "chunk_count" else f"{c} TIMESTAMP" for c in columns
    ))
    conn.execute(f"CREATE TABLE files ({cols})")
    conn.executemany(
        f"INSERT INTO files (id, {', '.join(columns)}) VALUES (?, {', '.join('?' for _ in columns)})",
        rows,
    )
    conn.commit()
    conn.close()


def _files_rows(path, columns="id, status, chunk_count"):
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute(f"SELECT {columns} FROM files ORDER BY id").fetchall()
    finally:
        conn.close()


def _capture_migrate(**kwargs):
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    mig.logger.addHandler(handler)
    try:
        with redirect_stdout(buf):
            ret = mig.run_migration(**kwargs)
        return ret, buf.getvalue(), None
    except BaseException as exc:  # noqa: BLE001 - probe captures everything
        return None, buf.getvalue(), exc
    finally:
        mig.logger.removeHandler(handler)


def _run_reset(monkeypatch, db_path, lancedb_dir):
    monkeypatch.setattr(
        rst, "settings", types.SimpleNamespace(
            sqlite_path=str(db_path), lancedb_path=str(lancedb_dir))
    )
    buf = io.StringIO()
    raised = None
    try:
        with redirect_stdout(buf):
            rst.reset_all_embeddings()
    except BaseException as exc:  # noqa: BLE001 - probe captures everything
        raised = exc
    return buf.getvalue(), raised


_ROLLBACK_RE = r"lancedb\.rollback-\d{8}-\d{6}"


# ---------------------------------------------------------------------------
# Group 1 — R3 / reset D4: schema-compatible foreign database is recoverable
# ---------------------------------------------------------------------------


def test_reset_foreign_compatible_db_preserves_rollback(tmp_path, monkeypatch):
    """R3 (executed Phase-2 probe): a schema-compatible WRONG database resets
    its rows, but the live index survives as a rollback sibling named in the
    output (recoverability, not detection)."""
    db_path = tmp_path / "app.db"
    _make_files_db(db_path, [(1, "doc.txt", "indexed", 5, None, None)])
    lancedb_dir = tmp_path / "lancedb"
    lancedb_dir.mkdir()
    marker = lancedb_dir / "sentinel.marker"
    marker.write_text("live vectors", encoding="utf-8")

    out, raised = _run_reset(monkeypatch, db_path, lancedb_dir)

    assert raised is None, f"reset crashed: {raised!r}"
    rollbacks = [
        p for p in tmp_path.iterdir()
        if p.is_dir() and re.fullmatch(_ROLLBACK_RE, p.name)
    ]
    assert rollbacks, f"no rollback sibling; entries: {sorted(p.name for p in tmp_path.iterdir())}"
    assert (rollbacks[0] / "sentinel.marker").exists(), "rollback lost the index contents"
    assert lancedb_dir.exists() and not any(lancedb_dir.iterdir()), "live dir not recreated empty"
    assert str(rollbacks[0]) in out, "rollback directory not named in the output"
    assert _files_rows(db_path) == [(1, "pending", 0)]


# ---------------------------------------------------------------------------
# Group 2 — reset D3b: minimal-schema application database resets cleanly
# ---------------------------------------------------------------------------


def test_reset_minimal_core_schema_resets_cleanly(tmp_path, monkeypatch):
    """The preflight accepts an id+status-only files table; the reset must
    not crash on absent optional columns (mirrors migrate batch-2 guards)."""
    db_path = tmp_path / "app.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE files (id INTEGER PRIMARY KEY, status TEXT)")
    conn.execute("INSERT INTO files (id, status) VALUES (7, 'indexed')")
    conn.commit()
    conn.close()
    lancedb_dir = tmp_path / "lancedb"
    lancedb_dir.mkdir()

    out, raised = _run_reset(monkeypatch, db_path, lancedb_dir)

    assert raised is None, f"minimal-schema reset crashed: {raised!r}"
    assert _files_rows(db_path, "id, status") == [(7, "pending")]


def test_reset_missing_core_column_refuses(tmp_path, monkeypatch):
    """A files table without the application's core columns is not the
    application database: exit 8 naming both paths, index survives."""
    db_path = tmp_path / "app.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE files (id INTEGER PRIMARY KEY, owner TEXT)")
    conn.commit()
    conn.close()
    lancedb_dir = tmp_path / "lancedb"
    lancedb_dir.mkdir()
    marker = lancedb_dir / "sentinel.marker"
    marker.write_text("live vectors", encoding="utf-8")

    out, raised = _run_reset(monkeypatch, db_path, lancedb_dir)

    assert isinstance(raised, SystemExit) and raised.code == 8, f"raised={raised!r}"
    assert "ERROR" in out and str(db_path) in out and str(lancedb_dir) in out
    assert marker.exists()


# ---------------------------------------------------------------------------
# Group 3 — reset preflight taxonomy
# ---------------------------------------------------------------------------


def test_reset_directory_as_database_refuses(tmp_path, monkeypatch):
    db_path = tmp_path / "app.db"
    db_path.mkdir()  # a DIRECTORY at the sqlite path
    lancedb_dir = tmp_path / "lancedb"
    lancedb_dir.mkdir()
    marker = lancedb_dir / "sentinel.marker"
    marker.write_text("x", encoding="utf-8")

    out, raised = _run_reset(monkeypatch, db_path, lancedb_dir)

    assert isinstance(raised, SystemExit) and raised.code == 8, f"raised={raised!r}"
    assert "ERROR" in out and str(db_path) in out and str(lancedb_dir) in out
    assert marker.exists()


def test_reset_corrupt_database_refuses(tmp_path, monkeypatch):
    db_path = tmp_path / "app.db"
    db_path.write_bytes(b"this is definitely not a sqlite database file")
    lancedb_dir = tmp_path / "lancedb"
    lancedb_dir.mkdir()
    marker = lancedb_dir / "sentinel.marker"
    marker.write_text("x", encoding="utf-8")

    out, raised = _run_reset(monkeypatch, db_path, lancedb_dir)

    assert isinstance(raised, SystemExit) and raised.code == 8, f"raised={raised!r}"
    assert "ERROR" in out
    assert marker.exists()


def test_reset_lock_shaped_failure_gives_stop_app_guidance(tmp_path, monkeypatch):
    db_path = tmp_path / "app.db"
    _make_files_db(db_path, [(1, "doc.txt", "indexed", 5, None, None)])
    lancedb_dir = tmp_path / "lancedb"
    lancedb_dir.mkdir()

    real_connect = sqlite3.connect

    def locked_connect(*args, **kwargs):
        if args and str(args[0]) == str(db_path):
            raise sqlite3.OperationalError("database is locked")
        return real_connect(*args, **kwargs)

    with patch.object(rst.sqlite3, "connect", locked_connect):
        out, raised = _run_reset(monkeypatch, db_path, lancedb_dir)

    assert isinstance(raised, SystemExit) and raised.code == 8, f"raised={raised!r}"
    assert "docker compose stop knowledgevault" in out, "missing stop-the-app guidance"


# ---------------------------------------------------------------------------
# Group 4 — migrate D2 boundary legs
# ---------------------------------------------------------------------------


def _broken_stream(raise_after=None, exc=sqlite3.OperationalError("database is locked")):
    """A claimed-id stream that raises `exc` immediately (raise_after=None)
    or after yielding `raise_after` non-matching batches."""
    state = {"batches": 0, "closed": []}

    def gen(_sqlite_path):
        try:
            for i in range(raise_after if raise_after is not None else 0):
                state["batches"] += 1
                yield [f"90{i}"]  # ids that match nothing in the index
            raise exc
        finally:
            state["closed"].append(True)

    gen.state = state
    return gen


def test_probe_midstream_sqlite_failure_after_misses_refuses(tmp_path):
    """A stream that misses on batch 1 then dies on batch 2 must REFUSE (the
    pre-fix code swallowed this into None -> pass)."""
    _require_real_engine()
    lance = tmp_path / "lancedb"
    _make_chunks_index(lance, range(1, 101))
    gen = _broken_stream(raise_after=1)
    closed = []

    def tracking_gen(p):
        try:
            yield from gen(p)
        finally:
            closed.append(True)

    with pytest.raises(mig.MigrationRefused):
        mig._index_shares_claimed_ids(lance, tracking_gen(tmp_path / "app.db"))
    assert closed == [True], "batch iterator not closed on refusal"


def test_probe_database_error_from_stream_refuses(tmp_path):
    _require_real_engine()
    lance = tmp_path / "lancedb"
    _make_chunks_index(lance, range(1, 101))
    with pytest.raises(mig.MigrationRefused):
        mig._index_shares_claimed_ids(
            lance, _broken_stream(exc=sqlite3.DatabaseError("file is not a database"))(
                tmp_path / "app.db"
            )
        )


def test_probe_lance_side_unreadable_is_still_no_evidence(tmp_path):
    """The other half of the AC2 contract: an index the installed lancedb
    cannot read keeps its documented None (never a refusal)."""
    lance = tmp_path / "lancedb"
    lance.mkdir()
    (lance / "chunks.lance").write_bytes(b"garbage")  # unreadable lance dir

    def gen(_p):
        yield ["1"]

    assert mig._index_shares_claimed_ids(lance, gen(None)) is None


def test_probe_sql_failure_maps_to_cli_exit_5(tmp_path, monkeypatch):
    """main() maps the probe refusal to exit code 5 (end-to-end CLI shape)."""
    _require_real_engine()
    lance = tmp_path / "lancedb"
    _make_chunks_index(lance, range(1, 101))
    db_path = tmp_path / "app.db"
    _make_files_db(db_path, [(501, "doc.txt", "indexed", 5, None, None)])  # disjoint

    monkeypatch.setattr(
        sys, "argv",
        ["migrate_embeddings.py", "--lancedb-path", str(lance),
         "--sqlite-path", str(db_path)],
    )
    with patch.object(mig, "_iter_claimed_id_batches", _broken_stream()):
        with pytest.raises(SystemExit) as excinfo:
            with redirect_stdout(io.StringIO()):
                mig.main()
    assert excinfo.value.code == 5
    # Refused BEFORE any reset or wipe.
    assert _files_rows(db_path) == [(501, "indexed", 5)]
    assert (lance / "chunks.lance").exists()


# ---------------------------------------------------------------------------
# Group 5 — rename mechanics (both scripts)
# ---------------------------------------------------------------------------


class _FixedClock:
    """datetime replacement with a deterministic now()."""

    def __init__(self, moment):
        self._moment = moment

    def now(self):
        return self._moment

    def strftime(self, fmt):  # pragma: no cover - not used directly
        return self._moment.strftime(fmt)


def test_migrate_rollback_name_collision_uses_suffix(tmp_path):
    _require_real_engine()
    lance = tmp_path / "lancedb"
    _make_chunks_index(lance, [1])
    db_path = tmp_path / "app.db"
    _make_files_db(db_path, [(1, "doc.txt", "indexed", 5, None, None)])
    stamp = datetime(2026, 11, 7, 12, 0, 0)
    with patch.object(mig, "datetime", _FixedClock(stamp)):
        # Pre-create the exact same-second name so the first attempt collides.
        (tmp_path / "lancedb.rollback-20261107-120000").mkdir()
        ret, out, exc = _capture_migrate(
            lancedb_path=lance, sqlite_path=db_path, dry_run=False, force=True)
    assert exc is None and ret == 1
    assert (tmp_path / "lancedb.rollback-20261107-120000-1" / "chunks.lance").exists(), (
        "collision suffix not used"
    )


def test_migrate_rename_failure_refuses_with_committed_sqlite_state(tmp_path):
    _require_real_engine()
    lance = tmp_path / "lancedb"
    _make_chunks_index(lance, [1])
    db_path = tmp_path / "app.db"
    _make_files_db(db_path, [(1, "doc.txt", "indexed", 5, None, None)])

    def broken_rename(self, target):
        raise OSError("The process cannot access the file (locked)")

    with patch.object(Path, "rename", broken_rename):
        ret, out, exc = _capture_migrate(
            lancedb_path=lance, sqlite_path=db_path, dry_run=False, force=True)
    assert isinstance(exc, mig.MigrationRefused), f"exc={exc!r}"
    assert "ALREADY committed" in str(exc) and "INTACT" in str(exc)
    assert "Migration complete" not in out, "success summary printed on refusal"
    # Truthful state: the SQLite reset committed, the index is untouched.
    assert _files_rows(db_path) == [(1, "pending", 0)]
    assert (lance / "chunks.lance").exists()


def test_migrate_recreation_failure_names_preserved_rollback(tmp_path):
    _require_real_engine()
    lance = tmp_path / "lancedb"
    _make_chunks_index(lance, [1])
    db_path = tmp_path / "app.db"
    _make_files_db(db_path, [(1, "doc.txt", "indexed", 5, None, None)])

    def broken_mkdir(self, parents=False, exist_ok=False):
        raise OSError("disk full")

    with patch.object(Path, "mkdir", broken_mkdir):
        ret, out, exc = _capture_migrate(
            lancedb_path=lance, sqlite_path=db_path, dry_run=False, force=True)
    assert isinstance(exc, mig.MigrationRefused), f"exc={exc!r}"
    assert "PRESERVED at" in str(exc)
    assert "Migration complete" not in out
    rollbacks = [p for p in tmp_path.iterdir() if re.fullmatch(_ROLLBACK_RE, p.name)]
    assert rollbacks and (rollbacks[0] / "chunks.lance").exists(), (
        "previous index not preserved on recreation failure"
    )
    assert not lance.exists(), "live dir absent after failed recreation"


def test_migrate_empty_index_dir_creates_no_rollback(tmp_path):
    lance = tmp_path / "lancedb"
    lance.mkdir()  # exists, empty
    db_path = tmp_path / "app.db"
    _make_files_db(db_path, [(1, "doc.txt", "pending", 0, None, None)])
    ret, out, exc = _capture_migrate(
        lancedb_path=lance, sqlite_path=db_path, dry_run=False, force=True)
    assert exc is None
    assert not any(re.fullmatch(_ROLLBACK_RE, p.name) for p in tmp_path.iterdir()), (
        "pointless rollback artifact for an empty index"
    )


def test_reset_rename_failure_refuses_index_intact(tmp_path, monkeypatch):
    db_path = tmp_path / "app.db"
    _make_files_db(db_path, [(1, "doc.txt", "indexed", 5, None, None)])
    lancedb_dir = tmp_path / "lancedb"
    lancedb_dir.mkdir()
    marker = lancedb_dir / "sentinel.marker"
    marker.write_text("x", encoding="utf-8")

    def broken_rename(self, target):
        raise OSError("locked")

    with patch.object(Path, "rename", broken_rename):
        out, raised = _run_reset(monkeypatch, db_path, lancedb_dir)

    assert isinstance(raised, SystemExit) and raised.code == 8, f"raised={raised!r}"
    assert "ALREADY committed" in out and "INTACT" in out
    assert marker.exists()
    assert _files_rows(db_path) == [(1, "pending", 0)], "reset committed before refusal"


def test_reset_empty_index_dir_creates_no_rollback(tmp_path, monkeypatch):
    db_path = tmp_path / "app.db"
    _make_files_db(db_path, [(1, "doc.txt", "pending", 0, None, None)])
    lancedb_dir = tmp_path / "lancedb"
    lancedb_dir.mkdir()

    out, raised = _run_reset(monkeypatch, db_path, lancedb_dir)

    assert raised is None
    assert not any(re.fullmatch(_ROLLBACK_RE, p.name) for p in tmp_path.iterdir())


def test_reset_rollback_name_collision_uses_suffix(tmp_path, monkeypatch):
    """Reset-side twin of the migrate collision leg (implementation-review
    round 1: the plan promised rename mechanics for BOTH scripts)."""
    db_path = tmp_path / "app.db"
    _make_files_db(db_path, [(1, "doc.txt", "indexed", 5, None, None)])
    lancedb_dir = tmp_path / "lancedb"
    lancedb_dir.mkdir()
    marker = lancedb_dir / "sentinel.marker"
    marker.write_text("x", encoding="utf-8")
    stamp = datetime(2026, 11, 7, 12, 0, 0)
    with patch.object(rst, "datetime", _FixedClock(stamp)):
        (tmp_path / "lancedb.rollback-20261107-120000").mkdir()  # collide
        out, raised = _run_reset(monkeypatch, db_path, lancedb_dir)
    assert raised is None, f"reset crashed on collision: {raised!r}"
    assert (tmp_path / "lancedb.rollback-20261107-120000-1" / "sentinel.marker").exists(), (
        "collision suffix not used on the reset side"
    )
    assert lancedb_dir.exists() and not any(lancedb_dir.iterdir())


def test_reset_recreation_failure_names_preserved_rollback(tmp_path, monkeypatch):
    """Reset-side twin of the migrate recreation-failure leg: the rollback
    name is printed before the failing mkdir, the run refuses without a
    success summary, and the previous index survives in the rollback."""
    db_path = tmp_path / "app.db"
    _make_files_db(db_path, [(1, "doc.txt", "indexed", 5, None, None)])
    lancedb_dir = tmp_path / "lancedb"
    lancedb_dir.mkdir()
    marker = lancedb_dir / "sentinel.marker"
    marker.write_text("x", encoding="utf-8")

    def broken_mkdir(self, parents=False, exist_ok=False):
        raise OSError("disk full")

    with patch.object(Path, "mkdir", broken_mkdir):
        out, raised = _run_reset(monkeypatch, db_path, lancedb_dir)

    assert isinstance(raised, SystemExit) and raised.code == 8, f"raised={raised!r}"
    assert "PRESERVED at" in out
    assert "[3/3] Migration complete" not in out, "success summary printed on refusal"
    rollbacks = [p for p in tmp_path.iterdir() if re.fullmatch(_ROLLBACK_RE, p.name)]
    assert rollbacks and (rollbacks[0] / "sentinel.marker").exists(), (
        "previous index not preserved on recreation failure"
    )
    assert not lancedb_dir.exists(), "live dir absent after failed recreation"


# ---------------------------------------------------------------------------
# Group 6 — 4.2 source-contract guardrail (subordinate tripwire)
# ---------------------------------------------------------------------------


def test_no_rmtree_in_operator_wipe_scripts():
    """The destructive path must preserve, not delete: neither operator wipe
    script may use shutil.rmtree (RED at the pre-fix tree)."""
    for script in ("migrate_embeddings.py", "reset_embeddings.py"):
        source = (_REPO_ROOT / "scripts" / script).read_text(encoding="utf-8")
        assert "shutil.rmtree" not in source, f"{script} still deletes via rmtree"
        assert "import shutil" not in source, f"{script} still imports shutil"


def test_reset_preflight_runs_before_any_connection():
    """Structural pin: inside reset_all_embeddings's body, the preflight CALL
    must precede the first get_db_connection — the refusal fires before a
    stray database file can be materialized. Scoped to the function's own
    source slice so the preflight DEFINITION (which always sorts first)
    cannot satisfy the order pin (implementation-review round 1, M3b)."""
    import ast

    source = (_REPO_ROOT / "scripts" / "reset_embeddings.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    fn = next(
        n for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "reset_all_embeddings"
    )
    linenos = {"_assert_application_database": [], "get_db_connection": []}
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if name in linenos:
            linenos[name].append(node.lineno)
    positions = {
        "preflight_call": min(linenos["_assert_application_database"], default=None),
        "first_connection": min(linenos["get_db_connection"], default=None),
    }
    assert positions["preflight_call"] is not None, (
        "reset_all_embeddings no longer CALLS _assert_application_database "
        "(the preflight was deleted or unwired)"
    )
    assert positions["first_connection"] is not None, "no get_db_connection call found"
    assert positions["preflight_call"] < positions["first_connection"], (
        "preflight must precede the first connection"
    )
