"""Supplementary hardening coverage for the operator embedding scripts' wipe
guards (collated #1094 + #1080).

Unfrozen companion to the frozen checks (test_migrate_embeddings_rollback.py,
test_reset_embeddings_refusal.py): this module pins the plan-approved
semantics beyond the issue ACs (plan-critic round 1) — rename mechanics,
reset preflight taxonomy, D2 probe boundary legs, the R3 recoverability
window, and the 4.2 source-contract guardrail. Most legs are RED at the
pre-fix tree and GREEN after the fix; THREE legs are GREEN AT BOTH ENDS
by design (controls, verified by a base-worktree run at 01084c06:
30 failed / 3 passed): test_probe_lance_side_unreadable_is_still_no_evidence,
test_migrate_empty_index_dir_creates_no_rollback, and
test_probe_lance_query_failure_is_no_evidence (the pre-fix outer handler
also returned the documented None). Most Group-7 legs pin contracts that
only exist at the fixed tree; the issues' RED-at-base certification lives
in the frozen check files.
"""

import importlib.util
import io
import logging
import os
import re
import sqlite3
import subprocess
import sys
import types
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

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
    refusal_block = out.split("ERROR:", 1)[-1] if "ERROR:" in out else ""
    assert str(db_path) in refusal_block and str(lancedb_dir) in refusal_block, (
        "the refusal block itself must name both paths, not the startup banner"
    )
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
    refusal_block = out.split("ERROR:", 1)[-1] if "ERROR:" in out else ""
    assert str(db_path) in refusal_block and str(lancedb_dir) in refusal_block, (
        "the refusal block itself must name both paths, not the startup banner"
    )
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
    refusal_block = out.split("ERROR:", 1)[-1] if "ERROR:" in out else ""
    assert str(db_path) in refusal_block and str(lancedb_dir) in refusal_block, (
        "the refusal block itself must name both paths, not the startup banner"
    )
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
    pre-fix code swallowed this into None -> pass). The stream is a plain
    object the TEST never closes, so only production's ``finally`` can
    record the close — the earlier generator fixture's own ``finally`` made
    the close assertion vacuous (implementation-review round 1)."""
    _require_real_engine()
    lance = tmp_path / "lancedb"
    _make_chunks_index(lance, range(1, 101))

    class _CloseTrackingStream:
        """Batches ["901"], ["902"], then a SQLite-layer failure. close()
        is ONLY called by production's finally."""

        def __init__(self):
            self.close_calls = []
            self._it = iter([["901"], ["902"]])

        def __iter__(self):
            return self

        def __next__(self):
            try:
                return next(self._it)
            except StopIteration:
                raise sqlite3.OperationalError("database is locked") from None

        def close(self):
            self.close_calls.append(True)

    stream = _CloseTrackingStream()
    with pytest.raises(mig.MigrationRefused):
        mig._index_shares_claimed_ids(lance, stream)
    assert stream.close_calls == [True], (
        "production's finally did not close the batch iterator on refusal"
    )


def test_probe_non_sqlite_stream_error_propagates(tmp_path):
    """A NON-sqlite error from the id stream is a programming error: it
    propagates (never swallowed into the documented None, never converted
    to MigrationRefused) — pins the outer handler's boundary (bot-review
    F-10 mutation-survivor: widening except sqlite3.Error to Exception
    would fail this leg)."""
    _require_real_engine()
    lance = tmp_path / "lancedb"
    _make_chunks_index(lance, range(1, 101))

    def bad_stream(_sqlite_path):
        raise ValueError("not a sqlite failure")
        yield  # pragma: no cover - makes this a generator function

    with pytest.raises(ValueError):
        mig._index_shares_claimed_ids(lance, bad_stream(tmp_path / "app.db"))


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
    _require_real_engine()
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
    assert out.index("Preserved previous index as rollback directory") < out.index(
        "ERROR:"
    ), "rollback name printed after the refusal (print-before-mkdir unpinned)"
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
    script may use shutil.rmtree in ANY import form (RED at the pre-fix
    tree; the import-form pin closes the `from shutil import rmtree`
    bypass, bot-review F-11)."""
    for script in ("migrate_embeddings.py", "reset_embeddings.py"):
        source = (_REPO_ROOT / "scripts" / script).read_text(encoding="utf-8")
        assert "shutil.rmtree" not in source, f"{script} still deletes via rmtree"
        assert "import shutil" not in source, f"{script} still imports shutil"
        assert "from shutil import" not in source, (
            f"{script} imports from shutil (bypasses the module-import pin)"
        )


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


# ---------------------------------------------------------------------------
# Group 7 — feedback round (pr1095-20261107): guards + truthful messages
#
# Several legs below pin contracts that only exist at the FIXED tree (the
# pre-fix tree could not even reach these states — base raised unhandled
# errors or lacked the guards entirely); the RED-at-base certification for
# the issues' ACs lives in the frozen check files, replayed by repro-check.
# ---------------------------------------------------------------------------


def _make_link(target: Path, link: Path) -> bool:
    """Create a directory link: a junction on Windows (no privilege needed),
    a symlink elsewhere. Returns False when neither is creatable."""
    if sys.platform == "win32":
        proc = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
        )
        return proc.returncode == 0
    try:
        os.symlink(target, link)
        return True
    except OSError:
        return False


def test_reset_backup_named_database_refuses(tmp_path, monkeypatch):
    """PRR-002: a backup-named database (app.db.bak) is refused with the
    migrate guard's taxonomy BEFORE any reset or wipe (the base script had
    no backup guard at all)."""
    db_path = tmp_path / "app.db.bak"
    _make_files_db(db_path, [(1, "doc.txt", "indexed", 5, None, None)])
    lancedb_dir = tmp_path / "lancedb"
    lancedb_dir.mkdir()
    marker = lancedb_dir / "sentinel.marker"
    marker.write_text("x", encoding="utf-8")

    out, raised = _run_reset(monkeypatch, db_path, lancedb_dir)

    assert isinstance(raised, SystemExit) and raised.code == 8, f"raised={raised!r}"
    refusal_block = out.split("ERROR:", 1)[-1] if "ERROR:" in out else ""
    assert "BACKUP copy" in refusal_block
    assert str(db_path) in refusal_block and str(lancedb_dir) in refusal_block
    assert marker.exists(), "index not preserved across the refusal"
    assert _files_rows(db_path) == [(1, "indexed", 5)], "backup rows were reset"
    assert not any(
        re.fullmatch(_ROLLBACK_RE, p.name) for p in tmp_path.iterdir()
    ), "rollback artifact created despite the refusal"


def test_migrate_link_guard_refuses_before_reset(tmp_path):
    """PRR-015: a symlinked/junctioned index path is refused BEFORE the
    SQLite reset commits (the rename would move the LINK, not the index;
    base's rmtree refused links the same way)."""
    real_dir = tmp_path / "real-index"
    real_dir.mkdir()
    (real_dir / "chunks.lance").mkdir()
    link = tmp_path / "lancedb"
    if not _make_link(real_dir, link):
        pytest.skip("cannot create a symlink/junction on this host")
    db_path = tmp_path / "app.db"
    _make_files_db(db_path, [(1, "doc.txt", "indexed", 5, None, None)])

    ret, out, exc = _capture_migrate(
        lancedb_path=link, sqlite_path=db_path, dry_run=False, force=True)

    assert isinstance(exc, SystemExit) and exc.code == 8, f"exc={exc!r}"
    assert "symlink or junction" in out and "Nothing was migrated" in out
    assert _files_rows(db_path) == [(1, "indexed", 5)], "rows reset despite the refusal"
    assert real_dir.exists() and (real_dir / "chunks.lance").exists(), "target disturbed"
    assert not any(
        re.fullmatch(r"lancedb\.rollback-\d{8}-\d{6}.*", p.name)
        for p in tmp_path.iterdir()
    ), "rollback artifact created despite the refusal"


def test_reset_link_guard_refuses_before_reset(tmp_path, monkeypatch):
    """PRR-015 (reset twin): the link refusal fires BEFORE the status
    reset — rows keep claiming vectors."""
    real_dir = tmp_path / "real-index"
    real_dir.mkdir()
    marker = real_dir / "chunks.marker"
    marker.write_text("x", encoding="utf-8")
    link = tmp_path / "lancedb"
    if not _make_link(real_dir, link):
        pytest.skip("cannot create a symlink/junction on this host")
    db_path = tmp_path / "app.db"
    _make_files_db(db_path, [(1, "doc.txt", "indexed", 5, None, None)])

    out, raised = _run_reset(monkeypatch, db_path, link)

    assert isinstance(raised, SystemExit) and raised.code == 8, f"raised={raised!r}"
    assert "symlink or junction" in out and "Nothing was reset or renamed" in out
    assert _files_rows(db_path) == [(1, "indexed", 5)], "rows reset despite the refusal"
    assert marker.exists(), "link target disturbed"


def test_migrate_iterdir_permission_refuses(tmp_path):
    """Copilot 4504200757: an unreadable index directory must refuse with
    the state-aware message, not crash with a raw PermissionError after the
    reset committed."""
    _require_real_engine()
    lance = tmp_path / "lancedb"
    _make_chunks_index(lance, [1])
    db_path = tmp_path / "app.db"
    _make_files_db(db_path, [(1, "doc.txt", "indexed", 5, None, None)])
    original_iterdir = Path.iterdir

    def denied_iterdir(self):
        if self == lance:
            raise PermissionError("access denied")
        return original_iterdir(self)

    with patch.object(Path, "iterdir", denied_iterdir):
        ret, out, exc = _capture_migrate(
            lancedb_path=lance, sqlite_path=db_path, dry_run=False, force=True)
    # The probe fires at the pre-reset warning site: NOTHING has changed.
    assert isinstance(exc, SystemExit) and exc.code == 8, f"exc={exc!r}"
    assert "could not inspect" in out and "Nothing was migrated" in out
    assert _files_rows(db_path) == [(1, "indexed", 5)], "rows reset despite the refusal"
    assert (lance / "chunks.lance").exists(), "index disturbed"


def test_reset_iterdir_permission_refuses(tmp_path, monkeypatch):
    """Copilot 4504200778 (reset twin)."""
    db_path = tmp_path / "app.db"
    _make_files_db(db_path, [(1, "doc.txt", "indexed", 5, None, None)])
    lancedb_dir = tmp_path / "lancedb"
    lancedb_dir.mkdir()
    marker = lancedb_dir / "sentinel.marker"
    marker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(
        rst, "settings", types.SimpleNamespace(
            sqlite_path=str(db_path), lancedb_path=str(lancedb_dir))
    )
    original_iterdir = Path.iterdir

    def denied_iterdir(self):
        if self == lancedb_dir:
            raise PermissionError("access denied")
        return original_iterdir(self)

    buf = io.StringIO()
    raised = None
    with patch.object(Path, "iterdir", denied_iterdir):
        try:
            with redirect_stdout(buf):
                rst.reset_all_embeddings()
        except BaseException as exc:  # noqa: BLE001 - probe captures everything
            raised = exc
    assert isinstance(raised, SystemExit) and raised.code == 8, f"raised={raised!r}"
    assert "could not inspect" in buf.getvalue()
    assert marker.exists(), "index disturbed"


def test_migrate_summary_truthful_on_empty_dir(tmp_path):
    """PRR-011: the run summary never claims a replacement that did not
    happen (empty index directory is left as-is)."""
    lance = tmp_path / "lancedb"
    lance.mkdir()  # exists, empty
    db_path = tmp_path / "app.db"
    _make_files_db(db_path, [(1, "doc.txt", "indexed", 5, None, None)])
    ret, out, exc = _capture_migrate(
        lancedb_path=lance, sqlite_path=db_path, dry_run=False, force=True)
    assert exc is None
    assert "no (index directory absent or already empty)" in out, out[-600:]
    assert "replaced     : yes" not in out




def test_migrate_exhaustion_refusal_discloses_reset(tmp_path):
    """PRR-009: when all six rollback names are taken, the refusal carries
    the committed-reset disclosure (the sibling arms have it)."""
    _require_real_engine()
    lance = tmp_path / "lancedb"
    _make_chunks_index(lance, [1])
    db_path = tmp_path / "app.db"
    _make_files_db(db_path, [(1, "doc.txt", "indexed", 5, None, None)])
    stamp = datetime(2026, 11, 7, 12, 0, 0)
    with patch.object(mig, "datetime", _FixedClock(stamp)):
        for suffix in ("", "-1", "-2", "-3", "-4", "-5"):
            (tmp_path / ("lancedb.rollback-20261107-120000" + suffix)).mkdir()
        ret, out, exc = _capture_migrate(
            lancedb_path=lance, sqlite_path=db_path, dry_run=False, force=True)
    assert isinstance(exc, mig.MigrationRefused), f"exc={exc!r}"
    assert "ALREADY committed" in str(exc)
    assert (lance / "chunks.lance").exists(), "index disturbed"


def test_migrate_rerun_after_recreate_failure_is_noop(tmp_path):
    """PRR-001: pins the truthful outcome the recreation-failure refusal now
    describes — a plain re-run does NOT repair the state (live dir stays
    absent; the rollback copy is the recovery)."""
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
    assert isinstance(exc, mig.MigrationRefused)
    assert "A plain re-run WITHOUT restoring the directory" in str(exc)
    rollbacks = [p for p in tmp_path.iterdir() if re.fullmatch(_ROLLBACK_RE, p.name)]
    assert rollbacks, "no rollback sibling after the recreate failure"

    ret2, out2, exc2 = _capture_migrate(
        lancedb_path=lance, sqlite_path=db_path, dry_run=False, force=True)
    assert exc2 is None and ret2 == 0, f"re-run: ret={ret2!r} exc={exc2!r}"
    assert not lance.exists(), "re-run silently restored the directory"
    assert rollbacks[0].exists(), "rollback copy disturbed by the re-run"


def test_migrate_rename_back_then_rerun_repairs(tmp_path):
    """PRR-001's documented recovery: rename the rollback directory back,
    then re-run — the migration completes for real."""
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
    assert isinstance(exc, mig.MigrationRefused)
    rollbacks = [p for p in tmp_path.iterdir() if re.fullmatch(_ROLLBACK_RE, p.name)]
    rollbacks[0].rename(lance)  # the documented recovery step

    ret2, out2, exc2 = _capture_migrate(
        lancedb_path=lance, sqlite_path=db_path, dry_run=False, force=True)
    assert exc2 is None, f"re-run: ret={ret2!r} exc={exc2!r}"
    assert "Migration complete" in out2
    assert "replaced     : yes" in out2, out2[-600:]
    assert lance.exists() and not (lance / "chunks.lance").exists(), "live dir not recreated"


def test_reset_permission_arm_gives_permission_remedy(tmp_path, monkeypatch):
    """PRR-008: a NON-lock open failure routes to the permission remedy, not
    the stop-the-app advice."""
    db_path = tmp_path / "app.db"
    _make_files_db(db_path, [(1, "doc.txt", "indexed", 5, None, None)])
    lancedb_dir = tmp_path / "lancedb"
    lancedb_dir.mkdir()
    real_connect = sqlite3.connect

    def denied_connect(*args, **kwargs):
        if args and str(args[0]) == str(db_path):
            raise sqlite3.OperationalError("attempt to write a readonly database")
        return real_connect(*args, **kwargs)

    with patch.object(rst.sqlite3, "connect", denied_connect):
        out, raised = _run_reset(monkeypatch, db_path, lancedb_dir)
    assert isinstance(raised, SystemExit) and raised.code == 8, f"raised={raised!r}"
    refusal_block = out.split("ERROR:", 1)[-1]
    assert "not a writer lock" in refusal_block
    assert "stop knowledgevault" not in refusal_block


def test_probe_lance_query_failure_is_no_evidence(tmp_path):
    """bot-review F-10: the lance QUERY handler (inner) keeps its documented
    None even when connect/table checks succeed but the search itself
    fails — the outer connect handler is tested by
    test_probe_lance_side_unreadable_is_still_no_evidence."""
    lance = tmp_path / "lancedb"
    (lance / "chunks.lance").mkdir(parents=True)

    fake_ldb = MagicMock()
    fake_db = fake_ldb.connect.return_value
    fake_db.table_names.return_value = ["chunks"]
    fake_tbl = fake_db.open_table.return_value
    fake_tbl.schema.names = ["file_id"]
    fake_tbl.count_rows.return_value = 1
    fake_tbl.search.return_value.where.return_value.select.return_value.\
        limit.return_value.to_arrow.side_effect = RuntimeError("lance boom")

    def gen(_p):
        yield ["1"]

    saved = sys.modules.get("lancedb")
    sys.modules["lancedb"] = fake_ldb
    try:
        result = mig._index_shares_claimed_ids(lance, gen(None))
    finally:
        if saved is None:
            sys.modules.pop("lancedb", None)
        else:
            sys.modules["lancedb"] = saved
    assert result is None
    assert fake_tbl.search.called, (
        "the leg must reach the lance QUERY (the inner handler), not return "
        "at the table-names check"
    )


def test_migrate_dry_run_over_link_refuses(tmp_path):
    """Delta-review R2: a --dry-run over a symlinked/junctioned index path
    must refuse like the live run — a dry run that predicts a replacement
    the live run would refuse is the misleading report the preflight
    refusals exist to prevent."""
    real_dir = tmp_path / "real-index"
    real_dir.mkdir()
    (real_dir / "chunks.lance").mkdir()
    link = tmp_path / "lancedb"
    if not _make_link(real_dir, link):
        pytest.skip("cannot create a symlink/junction on this host")
    db_path = tmp_path / "app.db"
    _make_files_db(db_path, [(1, "doc.txt", "indexed", 5, None, None)])

    ret, out, exc = _capture_migrate(
        lancedb_path=link, sqlite_path=db_path, dry_run=True, force=True)

    assert isinstance(exc, SystemExit) and exc.code == 8, f"exc={exc!r}"
    assert "symlink or junction" in out and "Nothing was migrated" in out
    assert "Would preserve" not in out, "dry-run predicted a refused replacement"
    assert real_dir.exists() and (real_dir / "chunks.lance").exists()
