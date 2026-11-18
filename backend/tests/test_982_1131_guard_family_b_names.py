"""Issue #982 Family-B names census guard + issue #1131 unwind legs.

PART 1 — grown AST census guard (supplementary rung; the per-site marker
probes in test_982_1131_c4_family_b_names_redaction.py are primary).

Mirrors the walker mechanics of the frozen #710 guard
(test_no_query_text_in_default_logs.py — logger-binding detection incl.
module-level ``logging.getLogger`` bindings and the inline
``logging.getLogger(...).<level>(...)`` receiver, ``logger.log`` level
handling via a fail-safe constant-level check, and the ``len(...)``
count-only blessing) with THIS module's own copies of every constant and
walker: nothing is imported from or mutates the frozen module (both run in
the same pytest session). The name/attr sets are Family B (user-chosen
filenames / vault names / paths), not Family A query text.

Name set (per the guard spec): file_name, filename, source_filename,
vault_name, file_path (bare names too). Attr set (strict):
``.file_path`` / ``.file_name``. String literals in args are not names —
only ``ast.Name`` / ``ast.Attribute`` nodes hit. ``len(...)`` subtrees are
blessed (count-only is the pattern).

Census run over the current tree (2026-11-17) found SIX hits; every one is
dispositioned below and pinned fail-closed (a pin that stops matching
because the line drifted makes this guard RED — drift forces re-review):

- (2026-11-17 update) background_tasks.py:3787/:6295/:6306/:6361 were real
  sibling leaks pinned at guard-authoring time; the orchestrator fixed them
  in-PR (bounded + DEBUG) and their pins were removed — the guard now enforces
  them directly. Remaining exact pins are the verified email_service false
  positives below. (Historical note preserved: the original pins were
  ``task.file_path`` arms) — REAL user-derived leaks REPORTED to the
  orchestrator, not silently accepted: upload paths are
  ``vault_uploads_dir(vault_id) / secure_filename(file.filename)``
  (documents.py :2725), so the logged string carries the user-chosen
  filename. This PR already redacted the neighboring ``task.file_path``
  family (:6083-:6160); these four siblings were deferred — C4's docstring
  says "The remaining same-shape siblings are Phase 4.2 census material,
  not frozen here". They stay pinned here so any EDIT to those exact lines
  re-trips the census until they get the len()/DEBUG treatment.
- email_service.py:534/:536 — verified FALSE POSITIVES of the bare
  ``file_path`` name: ``_save_attachment`` (email_service.py :691-:704)
  returns a ``tempfile.mkstemp(prefix="email_", suffix=ext)`` server path —
  the user-controlled attachment filename never lands in the logged string.
  Narrowing the name set (dropping bare ``file_path``) was DECLINED: it
  would blind the guard to the most common path-variable name (and to
  background_tasks.py:3787, a real leak) forever, while exact-site pins
  fail closed on drift instead. Attrs stay strict; names stay as specified.
- documents.py ``_log_upload_rejection`` — the #1041 deliberate carve-out
  (docs/releases/pending/1041-upload-rejection-logging.md): its filename is
  verbatim by design. Skipped by AST-locating the function body, per the
  frozen contract.

PART 2 — Option-A concurrency/validation legs for the #1131 unwind
(``_unwind_duplicate_loser_generation``): the BEGIN IMMEDIATE fence vs a
concurrent row reset, Lance-first lock order vs a
commit_dimension_rebuild-shaped peer, the late-writer residual, the
delete-failure no-tombstone contract, both reupload_safe_order modes, and
the reindex G1 exclusion across all four SELECT shapes.
"""

import ast
import asyncio
import hashlib
import logging
import os
import sqlite3
import subprocess
import sys
import types
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

# Add parent directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Stub optional dependencies same as other test files
try:
    import lancedb  # noqa: F401
except ImportError:
    sys.modules["lancedb"] = types.ModuleType("lancedb")

try:
    import pyarrow  # noqa: F401
except ImportError:
    sys.modules["pyarrow"] = types.ModuleType("pyarrow")

from app.config import settings
from app.models.database import SQLiteConnectionPool, run_migrations
from app.services.background_tasks import BackgroundProcessor
from app.services.document_processor import (
    DocumentProcessor,
    _unwind_duplicate_loser_generation,
)

# ======================================================================
# PART 1 — Family-B names census guard (#982)
# ======================================================================

# Family B: identifiers that hold user-chosen filenames / vault names /
# user-derived paths in this codebase. ``file_path`` stays IN the name set
# (see module docstring for why the narrowing option was declined).
_FAMILY_B_NAMES = frozenset(
    {"file_name", "filename", "source_filename", "vault_name", "file_path"}
)

# Attributes whose value is user-derived (``task.file_path``,
# ``row.file_name`` …). Kept STRICT per the guard spec.

# KNOWN BLIND SHAPES (review F-B7, pr1169-20261117): this census
# matches only ast.Name/ast.Attribute forms of the sets above. It
# cannot see Path `.name` attributes (f.name), bare renamed locals
# (path, directory, artifact, sender, subject), or row["file_path"]-
# style subscripts. Per-site marker probes (the frozen checks and the
# curator probe below) are the PRIMARY enforcement; this census is a
# supplementary rung only.
_FAMILY_B_ATTRS = frozenset({"file_path", "file_name"})

# Names a module can bind a logger to (mirror of the frozen #710 walker's
# receiver semantics — own copy, no import).
_LOGGER_BINDING_NAMES = frozenset(
    {"logger", "logging", "log", "_logger", "_log", "LOG"}
)

_DEFAULT_LEVEL_METHODS = frozenset(
    {"info", "warning", "error", "exception", "critical"}
)

# backend/app held 145+ tracked .py files at the time the #710 floor was
# pinned; same far-below-live floor so a moved root fails diagnosably.
_MIN_GUARDED_FILES = 50

# Fail-closed sanctioned sites: EXACT (git-relpath, lineno) pins. A pinned
# site that drifts (any edit above it shifts the line) re-trips the census
# and this guard goes RED until the pin is re-reviewed — drift is a forced
# re-review, never a silent pass. Each entry's disposition is documented
# in the module docstring; do not add entries without classifying the site
# against the actual data flow (see the email mkstemp entries for the
# verified-false-positive shape).
_SANCTIONED_EXACT_SITES = frozenset(
    {
        # REAL #982 Family-B sibling leak, REPORTED to the orchestrator
        # (upload path carries secure_filename(user filename) verbatim at
        # WARNING). Deferred per C4's "Phase 4.2 census material" note.
        # REAL sibling leaks (task.file_path at WARNING/ERROR) — same
        # family this PR already redacted at :6083-:6160. REPORTED.
        # VERIFIED FALSE POSITIVE: bare file_path is a server-generated
        # tempfile.mkstemp(prefix="email_", suffix=ext) path — the user
        # attachment filename never lands in it (email_service.py :691).
        ("app/services/email_service.py", 534),
        ("app/services/email_service.py", 536),
    }
)

# Sanctioned FUNCTION bodies, skipped whole (located by AST each run):
# (git-relpath, function name).
_SANCTIONED_FUNCTION_SITES = frozenset(
    {
        # #1041 deliberate carve-out: the upload-rejection trace logs the
        # submitted filename verbatim BY DESIGN (docs/releases/pending/
        # 1041-upload-rejection-logging.md). Pin is the function itself so
        # the carve-out moves with the code, not with a line number.
        ("app/api/routes/documents.py", "_log_upload_rejection"),
    }
)


def _b_is_get_logger_call(node: ast.AST) -> bool:
    """True for a ``logging.getLogger(...)`` call (inline receiver form)."""
    func = node.func if isinstance(node, ast.Call) else None
    if not isinstance(func, ast.Attribute) or func.attr != "getLogger":
        return False
    return isinstance(func.value, ast.Name) and func.value.id in ("logging", "log")


def _b_module_logger_names(tree: ast.AST) -> set:
    """Names bound to a logger anywhere in the module (Assign targets of
    ``logging.getLogger(...)``), so ``LOG.info(...)`` / ``self._logger...``
    receivers are recognized as logging sites."""
    names = set(_LOGGER_BINDING_NAMES)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and _b_is_get_logger_call(node.value):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
                elif isinstance(target, ast.Attribute):
                    names.add(target.attr)
    return names


def _b_is_logger_receiver(node: ast.AST, tree: ast.AST | None = None) -> bool:
    """True when ``node`` evaluates to a logger (bare name bound to
    ``logging.getLogger`` anywhere in the module, ``.logger`` attribute,
    or the inline ``logging.getLogger(...).<level>(...)`` form)."""
    if _b_is_get_logger_call(node):
        return True
    if isinstance(node, ast.Name):
        return tree is not None and node.id in _b_module_logger_names(tree)
    if isinstance(node, ast.Attribute):
        if node.attr == "logger":
            return True
        return tree is not None and node.attr in _b_module_logger_names(tree)
    return False


def _b_contains_family_b_text(node: ast.AST) -> str | None:
    """First user-derived Family-B name/attr found anywhere in ``node``,
    or None. ``len(...)`` subtrees are skipped — count-only logging is the
    blessed pattern. String literals are not names and never hit."""
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "len"
    ):
        return None
    if isinstance(node, ast.Name) and node.id in _FAMILY_B_NAMES:
        return node.id
    if isinstance(node, ast.Attribute) and node.attr in _FAMILY_B_ATTRS:
        return f".{node.attr}"
    for child in ast.iter_child_nodes(node):
        hit = _b_contains_family_b_text(child)
        if hit is not None:
            return hit
    return None


def _b_guarded_level_ok(call: ast.Call) -> bool:
    """For ``logger.log(...)``: guarded when the level is a constant >= 20
    (INFO) or is not a provable constant (fail-safe: treated as default
    level). Positional and ``level=`` keyword forms both handled."""
    level = call.args[0] if call.args else None
    if level is None:
        for kw in call.keywords:
            if kw.arg == "level":
                level = kw.value
                break
    if level is None:
        return True  # no level at all: cannot prove it is below INFO
    if isinstance(level, ast.Constant) and isinstance(level.value, int):
        return level.value >= 20
    return True


def _iter_tracked_app_files():
    """Yield TRACKED backend/app/*.py files (git ls-files scope, mirroring
    the frozen #710 walk) so untracked generated trees cannot change this
    guard's verdict between machines. Full rglob fallback when git is
    unavailable."""
    backend_dir = Path(__file__).resolve().parent.parent
    app_root = backend_dir / "app"
    try:
        listed = subprocess.run(
            ["git", "-C", str(backend_dir), "ls-files", "--", app_root.name],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        for path in sorted(app_root.rglob("*.py")):
            yield path
        return
    for rel in sorted(
        line for line in listed.stdout.splitlines() if line.endswith(".py")
    ):
        yield backend_dir / rel


def _scan_tree_hits(tree: ast.AST, rel: str):
    """Yield ``rel:line: description`` for every default-level logging call
    in ``tree`` that formats a Family-B name/attr. Shared by the live census
    and the synthetic self-test (rel never matches a sanctioned pin for the
    self-test's synthetic relpath)."""
    module_logger_names = _b_module_logger_names(tree)
    exempt_ranges = []
    for func_name in {
        name for site_rel, name in _SANCTIONED_FUNCTION_SITES if site_rel == rel
    }:
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name == func_name:
                    exempt_ranges.append((node.lineno, node.end_lineno))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        if not _b_is_logger_receiver(func.value, tree):
            continue
        if func.attr in _DEFAULT_LEVEL_METHODS:
            pass  # always default-or-higher
        elif func.attr == "log":
            if not _b_guarded_level_ok(node):
                continue
        else:
            continue
        if any(a <= node.lineno <= b for a, b in exempt_ranges):
            continue
        if (rel, node.lineno) in _SANCTIONED_EXACT_SITES:
            continue
        # Skip logger.log's own level argument; check every other
        # positional arg and keyword value (extra= included).
        args = list(
            node.args[1:] if func.attr == "log" and len(node.args) > 1 else node.args
        )
        args.extend(
            kw.value
            for kw in node.keywords
            if not (func.attr == "log" and kw.arg == "level")
        )
        for arg in args:
            hit = _b_contains_family_b_text(arg)
            if hit is not None:
                yield (
                    f"{rel}:{node.lineno}: logger.{func.attr} formats "
                    f"user-derived '{hit}'"
                )
                break


def _find_family_b_names_at_default_level():
    """Live census: yield hits for every tracked backend/app file."""
    backend_dir = Path(__file__).resolve().parent.parent
    for path in _iter_tracked_app_files():
        rel = str(path.relative_to(backend_dir)).replace(os.sep, "/")
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
        except (OSError, ValueError, SyntaxError):
            continue  # unreadable/unparseable: skipped, not fatal
        yield from _scan_tree_hits(tree, rel)


def test_family_b_names_absent_at_default_level():
    """PART 1 census: no default-level log call in backend/app formats a
    Family-B name (user-chosen filename / vault name / user-derived path).
    Zero hits on the current tree; the six dispositioned sites are pinned
    fail-closed in _SANCTIONED_EXACT_SITES / _SANCTIONED_FUNCTION_SITES."""
    files = list(_iter_tracked_app_files())
    assert len(files) >= _MIN_GUARDED_FILES, (
        f"Family-B census scanned only {len(files)} files under backend/app "
        f"— a real run must inspect at least {_MIN_GUARDED_FILES}; a moved "
        "scan root or broken glob must fail, not pass vacuously"
    )
    assert any(p.name == "main.py" for p in files), (
        "the census must include backend/app/main.py"
    )
    hits = list(_find_family_b_names_at_default_level())
    assert hits == [], (
        "default-level log calls formatting user-derived Family-B names "
        "(#982; sanctioned pins may have drifted — re-review them):\n"
        + "\n".join(hits)
    )


_SYNTHETIC_FAMILY_B_SOURCE = (
    "import logging\n"
    "logger = logging.getLogger(__name__)\n"
    "\n"
    "def _planted(file_path, task):\n"
    '    logger.info("x %s", file_path)\n'
    '    logger.warning("y %s", task.file_name)\n'
)


def test_guard_flags_synthetic_family_b_leak():
    """Self-test: the walker actually bites (guards against a vacuous
    walker). A synthetic module logging ``file_path`` (bare name) and
    ``task.file_name`` (attr) at INFO/WARNING must flag exactly those two
    sites; a ``len(file_path)`` count-only line must stay blessed."""
    tree = ast.parse(_SYNTHETIC_FAMILY_B_SOURCE)
    hits = list(_scan_tree_hits(tree, "synthetic_probe.py"))
    assert len(hits) == 2, f"expected the two planted leaks; got: {hits}"
    assert any("'file_path'" in h for h in hits), hits
    assert any("'.file_name'" in h for h in hits), hits

    blessed = ast.parse(
        "import logging\n"
        "logger = logging.getLogger(__name__)\n"
        'logger.info("n=%d", len(file_path))\n'
    )
    assert list(_scan_tree_hits(blessed, "synthetic_probe.py")) == [], (
        "len(...) count-only logging is the blessed pattern and must not hit"
    )


# ======================================================================
# PART 2 — #1131 unwind concurrency / validation legs
# ======================================================================


def _connect(db_path):
    conn = sqlite3.connect(db_path, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _seed(tmp_path, name):
    db_path = str(tmp_path / name)
    run_migrations(db_path)
    return db_path, _connect(db_path)


_HEX_A = "a" * 64
_HEX_B = "b" * 64


def _insert_file(conn, *, file_hash, status, path, error_message=None, vault_id=1):
    cur = conn.execute(
        "INSERT INTO files (vault_id, file_path, file_name, file_hash, file_size,"
        " status, phase, source, error_message) VALUES (?, ?, ?, ?, 1, ?,"
        " 'queued', 'upload', ?)",
        (vault_id, path, path.rsplit("/", 1)[-1], file_hash, status, error_message),
    )
    conn.commit()
    return int(cur.lastrowid)


def _seed_terminal_duplicate(conn, *, file_hash=_HEX_A, marker="MARKER982GB_loser.pdf"):
    """A duplicate-settled loser row: status='error', DUPLICATE_FILE code."""
    path = f"uploads/{marker}"
    return (
        _insert_file(
            conn,
            file_hash=file_hash,
            status="error",
            path=path,
            error_message="DUPLICATE_FILE: settled by the finalization race",
        ),
        file_hash,
        path,
    )


async def test_fence_blocks_concurrent_reset(tmp_path):
    """Leg 1: the unwind's BEGIN IMMEDIATE claim fences the row-reuse reset.

    While the claim transaction is open (parked inside the fenced delete),
    a same-path reset UPDATE on a SECOND connection must BLOCK (busy
    timeout -> 'database is locked') until the unwind commits — then the
    same UPDATE succeeds. This proves the fence serializes reset out of
    the claim->delete window."""
    db_path, conn = _seed(tmp_path, "fence.db")
    pool = SQLiteConnectionPool(db_path, max_size=4)
    conn_b = sqlite3.connect(db_path, timeout=0.5, check_same_thread=False)
    try:
        fid, file_hash, _path = _seed_terminal_duplicate(conn)

        class _BlockingDeleteStore:
            delete_entered = asyncio.Event()
            release_delete = asyncio.Event()
            calls = []

            @asynccontextmanager
            async def write_locked(self):
                self.calls.append("write_locked_enter")
                try:
                    yield self
                finally:
                    self.calls.append("write_locked_exit")

            async def delete_generation_by_file_locked(
                self, file_id, file_hash, target=None  # noqa: ARG002
            ):
                self.calls.append(
                    ("delete_generation_by_file_locked", str(file_id), file_hash)
                )
                self.delete_entered.set()
                await self.release_delete.wait()
                return 1

        store = _BlockingDeleteStore()
        unwind = asyncio.create_task(
            _unwind_duplicate_loser_generation(store, pool, fid)
        )
        await asyncio.wait_for(store.delete_entered.wait(), 10)

        def _reset_on_b():
            cur = conn_b.execute(
                "UPDATE files SET status='pending', error_message=NULL"
                " WHERE id = ?",
                (fid,),
            )
            conn_b.commit()
            return cur.rowcount

        # The claim transaction is open: the reset on connection B blocks
        # out of the claim->delete window.
        with pytest.raises(sqlite3.OperationalError) as excinfo:
            await asyncio.wait_for(asyncio.to_thread(_reset_on_b), 10)
        assert "locked" in str(excinfo.value).lower(), (
            f"expected the SQLite write-lock refusal; got {excinfo.value!r}"
        )
        # And the row was untouched while fenced.
        row = conn.execute(
            "SELECT status, error_message FROM files WHERE id = ?", (fid,)
        ).fetchone()
        assert row["status"] == "error" and row["error_message"].startswith(
            "DUPLICATE_FILE:"
        )

        store.release_delete.set()
        assert await asyncio.wait_for(unwind, 10) is True

        # After the unwind commits, the same reset goes through.
        rowcount = await asyncio.wait_for(asyncio.to_thread(_reset_on_b), 10)
        assert rowcount == 1
        row = conn.execute(
            "SELECT status, error_message FROM files WHERE id = ?", (fid,)
        ).fetchone()
        assert row["status"] == "pending" and row["error_message"] is None
        assert any(
            c[0] == "delete_generation_by_file_locked" if isinstance(c, tuple) else False
            for c in store.calls
        )
    finally:
        pool.close_all()
        conn.close()
        conn_b.close()


async def test_lock_order_unwind_vs_commit_metadata(tmp_path):
    """Leg 2: Lance-first / SQLite-second order — no deadlock against a
    commit_dimension_rebuild-shaped peer.

    The peer holds the store's write lock and writes SQLite metadata under
    it; the unwind must WAIT for the Lance lock BEFORE opening BEGIN
    IMMEDIATE (observable: a SQLite write probe succeeds while the unwind
    is parked on the Lance lock — had the unwind opened its claim first,
    the probe would block and the pair would deadlock). Both complete
    within bounded waits."""
    db_path, conn = _seed(tmp_path, "order.db")
    pool = SQLiteConnectionPool(db_path, max_size=4)
    try:
        fid, file_hash, _path = _seed_terminal_duplicate(conn)

        class _OrderingStore:
            def __init__(self):
                self._lance = asyncio.Lock()
                self.events = []

            @asynccontextmanager
            async def write_locked(self):
                self.events.append("lance_wait")
                await self._lance.acquire()
                self.events.append("lance_enter")
                try:
                    yield self
                finally:
                    self._lance.release()
                    self.events.append("lance_exit")

            async def delete_generation_by_file_locked(
                self, file_id, file_hash, target=None  # noqa: ARG002
            ):
                self.events.append("delete")
                return 1

        store = _OrderingStore()
        entered = asyncio.Event()
        release = asyncio.Event()

        async def _holder():
            # commit_dimension_rebuild-shaped: Lance held ACROSS a SQLite
            # metadata write, then released.
            async with store.write_locked():
                entered.set()
                hconn = sqlite3.connect(db_path, timeout=5, check_same_thread=False)
                try:
                    hconn.execute(
                        "UPDATE files SET modified_at = modified_at WHERE id = ?",
                        (fid,),
                    )
                    hconn.commit()
                finally:
                    hconn.close()
                store.events.append("holder_sqlite_write")
                await release.wait()

        holder = asyncio.create_task(_holder())
        await asyncio.wait_for(entered.wait(), 10)
        unwind = asyncio.create_task(
            _unwind_duplicate_loser_generation(store, pool, fid)
        )

        async def _wait_unwind_parked_on_lance():
            while store.events.count("lance_wait") < 2:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(_wait_unwind_parked_on_lance(), 10)
        assert "delete" not in store.events, (
            "the unwind must not reach the delete while the peer holds Lance"
        )
        # ORDER PROBE: the SQLite write lock is FREE while the unwind waits
        # for the Lance lock — i.e. the unwind has NOT opened BEGIN
        # IMMEDIATE yet (a Lance-second unwind would hold it here).
        probe = sqlite3.connect(db_path, timeout=2, check_same_thread=False)
        try:

            def _probe_write():
                probe.execute(
                    "UPDATE files SET modified_at = modified_at WHERE id = ?",
                    (fid,),
                )
                probe.commit()

            await asyncio.wait_for(asyncio.to_thread(_probe_write), 5)
        finally:
            probe.close()
        store.events.append("probe_sqlite_write_ok")

        release.set()
        await asyncio.wait_for(holder, 10)
        assert await asyncio.wait_for(unwind, 10) is True

        ev = store.events
        # Peer's SQLite write and the probe both precede the unwind's
        # delete; the unwind's Lance entry (2nd lance_enter) is strictly
        # after the peer's exit — the two-lock sequence never interleaves.
        assert ev.index("holder_sqlite_write") < ev.index("probe_sqlite_write_ok")
        assert ev.index("probe_sqlite_write_ok") < ev.index("delete")
        enters = [i for i, name in enumerate(ev) if name == "lance_enter"]
        assert len(enters) == 2, ev
        assert max(i for i, name in enumerate(ev) if name == "lance_exit"
                   ) not in enters[1:], ev
        assert ev[enters[1] + 1] == "delete", ev
    finally:
        pool.close_all()
        conn.close()


async def test_late_writer_add_after_unwind_persists(tmp_path):
    """Leg 3 (documenting residual): a late add_chunks for the same
    file_id AFTER a successful unwind leaves rows that NOTHING deletes —
    no tombstone is recorded, the files row stays terminal. This pins the
    disclosed base-state residual so a future fix has a RED baseline."""
    db_path, conn = _seed(tmp_path, "late.db")
    pool = SQLiteConnectionPool(db_path, max_size=4)
    try:
        fid, file_hash, _path = _seed_terminal_duplicate(conn)

        class _StatefulStore:
            """Models generation rows (safe-order id prefix shape)."""

            def __init__(self, rows):
                self.rows = list(rows)
                self.calls = []

            @asynccontextmanager
            async def write_locked(self):
                self.calls.append("write_locked_enter")
                try:
                    yield self
                finally:
                    self.calls.append("write_locked_exit")

            async def delete_generation_by_file_locked(
                self, file_id, file_hash, target=None  # noqa: ARG002
            ):
                self.calls.append("delete_generation_by_file_locked")
                prefix = f"{file_id}_{file_hash[:8]}_"
                before = len(self.rows)
                self.rows = [r for r in self.rows if not r.startswith(prefix)]
                return before - len(self.rows)

            async def add_chunks(self, file_id, file_hash, count=1):
                # Late-writer shape: appends safe-order generation rows.
                self.calls.append("add_chunks")
                for i in range(count):
                    self.rows.append(f"{file_id}_{file_hash[:8]}_{i}")

        # The loser's generation rows exist when the unwind runs.
        store = _StatefulStore([f"{fid}_{file_hash[:8]}_{i}" for i in range(3)])
        assert await _unwind_duplicate_loser_generation(store, pool, fid) is True
        assert store.rows == [], "the unwind removes the loser's generation"

        # A late writer lands rows AFTER the unwind: nothing deletes them.
        await store.add_chunks(fid, _HEX_B, count=2)
        assert len(store.rows) == 2 and all(
            r.startswith(f"{fid}_{_HEX_B[:8]}_") for r in store.rows
        ), "the late rows persist — no mechanism exists to remove them"
        assert store.calls.count("delete_generation_by_file_locked") == 1, (
            "no second delete fired for the late rows (the residual)"
        )

        # The files row stays terminal and NO tombstone was recorded.
        row = conn.execute(
            "SELECT status, error_message FROM files WHERE id = ?", (fid,)
        ).fetchone()
        assert row["status"] == "error"
        assert row["error_message"].startswith("DUPLICATE_FILE:")
        tombstones = conn.execute(
            "SELECT COUNT(*) AS n FROM vector_delete_pending WHERE file_id = ?",
            (fid,),
        ).fetchone()["n"]
        assert tombstones == 0, (
            "no vector_delete_pending tombstone exists for the late rows — "
            "the residual is disclosed, not retried"
        )
    finally:
        pool.close_all()
        conn.close()


async def test_unwind_delete_failure_leaves_no_tombstone(tmp_path, caplog):
    """Leg 4: a delete failure propagates from the direct unwind; driven
    through the settle it is swallowed into a file_id-only WARNING (no
    path/name text), the row stays terminal-duplicate, and NO tombstone
    row lands in vector_delete_pending."""
    db_path, conn = _seed(tmp_path, "tombstone.db")
    pool = SQLiteConnectionPool(db_path, max_size=4)
    try:
        marker = "MARKER982GB_failed.pdf"
        fid_direct, _h, _p = _seed_terminal_duplicate(
            conn, file_hash=_HEX_A, marker=marker
        )
        fid_settle, _h2, _p2 = _seed_terminal_duplicate(
            conn, file_hash=_HEX_B, marker="MARKER982GB_settle.pdf"
        )
        # The settle's guarded UPDATE only lands from pending/processing.
        conn.execute(
            "UPDATE files SET status='processing', error_message=NULL WHERE id = ?",
            (fid_settle,),
        )
        conn.commit()

        class _FailingDeleteStore:
            @asynccontextmanager
            async def write_locked(self):
                yield self

            async def delete_generation_by_file_locked(
                self, file_id, file_hash, target=None  # noqa: ARG002
            ):
                raise RuntimeError("stub delete failure (no marker text)")

        store = _FailingDeleteStore()

        # Direct call: the store failure propagates (caller owns the
        # best-effort fallback).
        with pytest.raises(RuntimeError):
            await _unwind_duplicate_loser_generation(store, pool, fid_direct)
        row = conn.execute(
            "SELECT status, error_message FROM files WHERE id = ?", (fid_direct,)
        ).fetchone()
        assert row["status"] == "error" and row["error_message"].startswith(
            "DUPLICATE_FILE:"
        )

        # Via the settle: the failure is swallowed into a WARNING that
        # carries file_id ONLY, then the original collision re-raises.
        processor = DocumentProcessor(pool=pool, vector_store=store)
        collision = sqlite3.IntegrityError(
            "UNIQUE constraint failed: files.file_hash, files.vault_id"
        )
        with caplog.at_level(
            logging.WARNING, logger="app.services.document_processor"
        ):
            with pytest.raises(sqlite3.IntegrityError):
                # The settle's trailing bare ``raise`` requires an active
                # exception — its documented contract is "called from the
                # except sqlite3.IntegrityError block of a finalize call".
                try:
                    raise collision
                except sqlite3.IntegrityError:
                    await processor._settle_duplicate_finalize_race(  # noqa: SLF001
                        fid_settle, collision
                    )

        warnings = [
            r
            for r in caplog.records
            if r.levelno >= logging.WARNING and "unwind failed" in r.getMessage()
        ]
        assert warnings, (
            "the settle must log the unwind failure at WARNING; captured: "
            f"{[r.getMessage() for r in caplog.records]}"
        )
        for record in warnings:
            message = record.getMessage()
            assert f"file_id={fid_settle}" in message, message
            assert "MARKER982GB" not in message, (
                f"the WARNING must carry file_id only, no path/name text: {message}"
            )
        # No default-level record of the module carries the marker text.
        default_level = [
            r.getMessage()
            for r in caplog.records
            if r.levelno >= logging.INFO and "MARKER982GB" in r.getMessage()
        ]
        assert default_level == [], default_level

        # Terminal state landed; no tombstone anywhere.
        row = conn.execute(
            "SELECT status, error_message FROM files WHERE id = ?", (fid_settle,)
        ).fetchone()
        assert row["status"] == "error" and row["error_message"].startswith(
            "DUPLICATE_FILE:"
        )
        tombstones = conn.execute(
            "SELECT COUNT(*) AS n FROM vector_delete_pending"
        ).fetchone()["n"]
        assert tombstones == 0, (
            "a failed unwind must not record a vector_delete_pending tombstone"
        )
    finally:
        pool.close_all()
        conn.close()


async def test_staged_immediate_success_both_modes(tmp_path, monkeypatch):
    """Leg 5: the unwind's single public seam works in BOTH
    reupload_safe_order modes and never issues a self-locking public
    delete_by_file inside write_locked (the stub's public method raises
    while the lock is held — the deadlock guard)."""
    db_path, conn = _seed(tmp_path, "modes.db")
    pool = SQLiteConnectionPool(db_path, max_size=4)
    try:
        fid, file_hash, _path = _seed_terminal_duplicate(conn)

        class _ModeStore:
            """Mirrors the real store's mode branch: safe-order deletes the
            named generation via the locked primitive; legacy delegates to
            the unlocked whole-file delete. The PUBLIC delete_by_file
            raises while write_locked is held (non-reentrant lock)."""

            def __init__(self):
                self.calls = []
                self.lance_held = False

            @asynccontextmanager
            async def write_locked(self):
                self.lance_held = True
                self.calls.append("write_locked_enter")
                try:
                    yield self
                finally:
                    self.calls.append("write_locked_exit")
                    self.lance_held = False

            async def delete_by_file(self, *args, **kwargs):  # noqa: ARG002
                if self.lance_held:
                    raise RuntimeError(
                        "self-locking public delete_by_file inside "
                        "write_locked would deadlock"
                    )
                self.calls.append("delete_by_file_public")

            async def _delete_by_file_unlocked(self, file_id, target=None):
                self.calls.append(
                    (
                        "_delete_by_file_unlocked",
                        (str(file_id),),
                        {"target": target, "lance_held": self.lance_held},
                    )
                )
                return 0

            async def delete_generation_by_file_locked(
                self, file_id, file_hash, target=None
            ):
                self.calls.append(
                    (
                        "delete_generation_by_file_locked",
                        (str(file_id), file_hash),
                        {"target": target, "lance_held": self.lance_held},
                    )
                )
                if not settings.reupload_safe_order:
                    # Legacy ids carry no generation prefix: the real store
                    # delegates to the unlocked whole-file delete.
                    return await self._delete_by_file_unlocked(file_id, target=target)
                return 1

        # Safe-order mode (the default): the locked primitive runs under
        # the held lock with the staged handle threaded through.
        monkeypatch.setattr(settings, "reupload_safe_order", True)
        staged_handle = object()
        safe_store = _ModeStore()
        assert (
            await _unwind_duplicate_loser_generation(
                safe_store, pool, fid, vector_target=staged_handle
            )
            is True
        )
        gen_calls = [
            c for c in safe_store.calls if c[0] == "delete_generation_by_file_locked"
        ]
        assert len(gen_calls) == 1, safe_store.calls
        assert gen_calls[0][2]["target"] is staged_handle, (
            "the staged rebuild handle must reach the locked delete verbatim"
        )
        assert gen_calls[0][2]["lance_held"] is True, (
            "the locked delete must run inside write_locked"
        )
        assert not any(
            isinstance(c, str) and c == "delete_by_file_public" for c in safe_store.calls
        ), "no self-locking public delete_by_file may occur inside the lock"

        # Legacy mode: the same public call delegates to the unlocked
        # whole-file delete, still under the held lock, still no public
        # self-locking call (the guard raise would fail the unwind).
        monkeypatch.setattr(settings, "reupload_safe_order", False)
        legacy_store = _ModeStore()
        assert (
            await _unwind_duplicate_loser_generation(legacy_store, pool, fid) is True
        )
        unlocked = [
            c for c in legacy_store.calls if c[0] == "_delete_by_file_unlocked"
        ]
        assert len(unlocked) == 1, legacy_store.calls
        assert unlocked[0][2]["lance_held"] is True, (
            "the legacy delegation must run inside write_locked"
        )
        assert not any(
            isinstance(c, str) and c == "delete_by_file_public"
            for c in legacy_store.calls
        ), "no self-locking public delete_by_file may occur inside the lock"
    finally:
        pool.close_all()
        conn.close()


# ---------------------------------------------------------------------------
# Leg 6 — G1 exclusion across all four reindex SELECT variants
# ---------------------------------------------------------------------------


class _ReindexRecordingStore:
    """C11-shaped recording stub (live dim == embed dim: never stages)."""

    def __init__(self):
        self.calls = []

    def _record(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))

    async def init_table(self, *a, **k):
        self._record("init_table", *a, **k)

    async def add_chunks(self, *a, **k):
        self._record("add_chunks", *a, **k)
        return {}

    async def delete_by_file(self, *a, **k):
        self._record("delete_by_file", *a, **k)

    async def delete_old_generation_by_file(self, *a, **k):
        self._record("delete_old_generation_by_file", *a, **k)
        return 0

    async def count_by_file(self, *a, **k):
        self._record("count_by_file", *a, **k)
        return 1

    async def get_live_embedding_dim(self, *a, **k):
        self._record("get_live_embedding_dim", *a, **k)
        return 2  # == embed stub dim -> in-place path, no staging

    async def record_embedding_metadata(self, *a, **k):
        self._record("record_embedding_metadata", *a, **k)

    async def mark_ready(self, *a, **k):
        self._record("mark_ready", *a, **k)


class _EmbedStub:
    async def embed_batch(self, texts, fail_fast=True, **kwargs):  # noqa: ARG002
        embeddings = [[0.1, 0.2] for _ in texts]
        return embeddings if fail_fast else (embeddings, [])


async def _run_reindex_variant(tmp_path, monkeypatch, name, *, vault_id, scoped):
    """Fresh DB per variant; drives the REAL _reindex_embed_all over the
    seeded five-row population; returns the file_ids the run actually
    processed (recorded by the fake schema-file seam)."""
    from app.services.chunking import ProcessedChunk
    from app.services.document_processor import ParsedDocument

    db_path, conn = _seed(tmp_path, name)
    try:
        # Belt-and-suspenders vault row: run_migrations already seeds a
        # Default vault (id 1); OR IGNORE keeps the vault-scoped variants
        # off the vault-deleted skip arm even if that seeding changes.
        conn.execute("INSERT OR IGNORE INTO vaults (id, name) VALUES (1, 'v')")
        conn.commit()

        def _real_file(stem, content):
            path = tmp_path / f"{name}-{stem}.pdf"
            path.write_bytes(content)
            return str(path), hashlib.sha256(content).hexdigest()

        owner_path, owner_hash = _real_file("owner", b"g1-owner-bytes-982")
        partial_path, partial_hash = _real_file("partial", b"g1-partial-bytes")
        error_path, error_hash = _real_file("error", b"g1-error-bytes")
        pending_path, pending_hash = _real_file("pending", b"g1-pending-bytes")

        owner_id = _insert_file(
            conn, file_hash=owner_hash, status="indexed", path=owner_path
        )
        partial_id = _insert_file(
            conn, file_hash=partial_hash, status="partial", path=partial_path
        )
        error_id = _insert_file(
            conn,
            file_hash=error_hash,
            status="error",
            path=error_path,
            error_message="INGEST_PARSE_FAILED: not a duplicate",
        )
        pending_id = _insert_file(
            conn, file_hash=pending_hash, status="pending", path=pending_path
        )
        loser_id = _insert_file(
            conn,
            file_hash=owner_hash,
            status="error",
            path=owner_path,
            error_message="DUPLICATE_FILE: settled by the finalization race",
        )
        all_ids = [owner_id, partial_id, error_id, pending_id, loser_id]

        pool = SQLiteConnectionPool(db_path, max_size=4)
        try:
            bp = BackgroundProcessor(pool=pool, retry_delay=0.05)
            bp.processor.vector_store = _ReindexRecordingStore()

            processed = []

            async def _fake_schema_file(
                file_path, file_id, generation_hash, parser_fingerprint  # noqa: ARG001
            ):
                processed.append(file_id)
                chunks = [
                    ProcessedChunk(
                        text="x",
                        metadata={"section": "s", "total_chunks": 1},
                        chunk_index=0,
                    )
                ]
                return (
                    chunks,
                    "x",
                    ParsedDocument(atoms=(), parser_fingerprint=parser_fingerprint),
                )

            bp.processor.embedding_service = _EmbedStub()
            monkeypatch.setattr(bp.processor, "_is_schema_file", lambda p: True)
            monkeypatch.setattr(
                bp.processor, "_process_schema_file", _fake_schema_file
            )

            status, result, error = await bp._reindex_embed_all(  # noqa: SLF001
                1,
                vault_id=vault_id,
                retry_file_ids=all_ids if scoped else None,
            )
            return {
                "processed": processed,
                "status": status,
                "result": result,
                "error": error,
                "owner_id": owner_id,
                "partial_id": partial_id,
                "error_id": error_id,
                "pending_id": pending_id,
                "loser_id": loser_id,
                "conn": conn,
            }
        finally:
            pool.close_all()
    finally:
        conn.close()


async def test_g1_exclusion_four_select_variants(tmp_path, monkeypatch):
    """Leg 6: all four _reindex_embed_all SELECT shapes (vault+scope,
    vault, scope, all — driven through the REAL function, one fresh DB per
    variant) exclude the settled duplicate and the pending row, and select
    the indexed owner, the partial row and the plain-error row."""
    variants = [
        ("g1-vault-scope.db", {"vault_id": 1, "scoped": True}),
        ("g1-vault.db", {"vault_id": 1, "scoped": False}),
        ("g1-scope.db", {"vault_id": None, "scoped": True}),
        ("g1-all.db", {"vault_id": None, "scoped": False}),
    ]
    for name, kwargs in variants:
        out = await _run_reindex_variant(tmp_path, monkeypatch, name, **kwargs)
        processed, loser = out["processed"], out["loser_id"]
        assert out["status"] == "completed", (
            f"{name}: status={out['status']!r} result={out['result']!r} "
            f"error={out['error']!r}"
        )
        assert "skipped" not in out["result"], (
            f"{name}: the run took the vault-deleted skip arm — vacuous"
        )
        assert loser not in processed, (
            f"{name}: the settled duplicate (id={loser}) was re-selected by "
            f"the reindex SELECT (G1/ABSORB regression); processed={processed}"
        )
        assert out["pending_id"] not in processed, (
            f"{name}: a pending row must never be selected; processed={processed}"
        )
        for expected in (out["owner_id"], out["partial_id"], out["error_id"]):
            assert expected in processed, (
                f"{name}: id={expected} (indexed/partial/plain-error) must be "
                f"selected; processed={processed}"
            )


def test_limiter_boot_traceback_severed_from_credential_chain():
    """Final-critic B2 (plan D-1 promise): the limiter re-raise severs the
    credential-bearing ConfigurationError chain (`from None`), so a boot
    traceback can never print the URI. Removing `from None` re-links
    ``__context__`` and this test goes RED. Also covers the sentinel-scheme
    parse-error path (limits._storage_scheme) with its own credential.
    """
    import traceback

    from limits.errors import ConfigurationError  # noqa: F401

    from app.limiter import build_limiter

    marker = "s3cret982c6tracebackmarker"
    cases = [
        f"bogus://{marker}@host:6379/0",
        f"redis+sentinel://:{marker}@h1:badport,h2:6379/0",
    ]
    for uri in cases:
        with pytest.raises(RuntimeError) as excinfo:
            build_limiter(uri)
        rendered = "".join(
            traceback.format_exception(
                type(excinfo.value),
                excinfo.value,
                excinfo.value.__traceback__,
            )
        )
        assert marker not in rendered, (
            f"982C6-guard: the boot traceback carries the credential for "
            f"URI scheme case {uri.split('://')[0]}: {rendered[-300:]}"
        )
        assert marker not in str(excinfo.value)
        assert excinfo.value.__cause__ is None or marker not in str(
            excinfo.value.__cause__
        )

@pytest.mark.asyncio
async def test_curator_sites_bounded_marker_probe(caplog):
    """Final-critic r2 B4: the three curator sibling fixes are pinned by an
    executed marker probe (the AST census only walks logger calls — raise
    messages and returned error dicts are invisible to it; reverting all
    three fixes to the resp.text[:200] form kept every suite green).

    Sites: wiki_curator_client outage raise (>=300 outage status), its
    non-outage re-raise (4xx outside the breaker), and the settings
    wizard-probe error dict.
    """
    import logging as _logging
    import unittest.mock as _mock

    import httpx

    from app.api.routes.settings import _CuratorTestBody, test_curator_connection
    from app.services import wiki_curator_client as wcc

    marker = "C15MARKER-curator-sibling-982"
    body = f'{{"message": "{marker}"}}'

    def _resp(status):
        return httpx.Response(
            status, text=body, request=httpx.Request("POST", "http://curator.invalid/p")
        )

    client = wcc.CuratorClient(
        "http://curator.invalid", "test-model", timeout=1.0
    )
    post_mock = _mock.AsyncMock(return_value=_resp(503))
    with caplog.at_level(_logging.DEBUG, logger="app.services.wiki_curator_client"),             _mock.patch.object(
                wcc, "assert_curator_url_safe", lambda *a, **k: None
            ),             _mock.patch.object(httpx.AsyncClient, "post", post_mock):
        with pytest.raises(httpx.HTTPStatusError) as outage:
            await client.propose([{"role": "user", "content": "x"}])
    assert marker not in str(outage.value), (
        "982C4-guard: the curator outage raise leaks the provider body "
        f"(final-critic r2 B4): {outage.value}"
    )
    assert "body_len=" in str(outage.value)

    post_mock = _mock.AsyncMock(return_value=_resp(401))
    with caplog.at_level(_logging.DEBUG, logger="app.services.wiki_curator_client"),             _mock.patch.object(
                wcc, "assert_curator_url_safe", lambda *a, **k: None
            ),             _mock.patch.object(httpx.AsyncClient, "post", post_mock):
        with pytest.raises(httpx.HTTPStatusError) as non_outage:
            await client.propose([{"role": "user", "content": "x"}])
    assert marker not in str(non_outage.value)
    assert "body_len=" in str(non_outage.value)

    default_texts = [r.message for r in caplog.records if r.levelno >= _logging.INFO]
    assert not any(marker in t for t in default_texts), (
        "982C4-guard: a default-level curator line carries the provider body"
    )
    debug_texts = [r.message for r in caplog.records if r.levelno == _logging.DEBUG]
    assert any(marker in t for t in debug_texts), (
        "the full provider body must be available at DEBUG"
    )

    # settings wizard-probe error dict (admin-gated route coroutine called
    # directly with sentinel dependencies — the auth gates are not under
    # test; the error-dict shape is)
    caplog.clear()
    post_mock = _mock.AsyncMock(return_value=_resp(401))
    import app.api.routes.settings as _settings_mod
    import app.services.curator_ssrf as _ssrf

    with caplog.at_level(_logging.DEBUG, logger="app.api.routes.settings"), \
            _mock.patch.object(
                _ssrf, "assert_curator_url_safe", lambda *a, **k: None
            ), \
            _mock.patch.object(
                _settings_mod, "assert_url_safe", lambda *a, **k: None
            ), \
            _mock.patch.object(httpx.AsyncClient, "post", post_mock):
        result = await test_curator_connection(
            body=_CuratorTestBody(
                url="http://curator.invalid", model="test-model"
            ),
            _role=object(),
            _csrf_token="test",
        )
    rendered = str(result)
    assert marker not in rendered, (
        "982C4-guard: the settings wizard-probe error dict leaks the "
        f"provider body (final-critic r2 B4): {rendered[:300]}"
    )
    assert "body_len=" in rendered
    default_texts = [r.message for r in caplog.records if r.levelno >= _logging.INFO]
    assert not any(marker in t for t in default_texts)
    debug_texts = [r.message for r in caplog.records if r.levelno == _logging.DEBUG]
    assert any(marker in t for t in debug_texts)


@pytest.mark.asyncio
async def test_mixed_staged_abort_settles_terminally_no_requeue(tmp_path):
    """Review F-001 (pr1169-20261117): a staged abort with duplicates AND
    transient failures must settle 'failed' terminally on the FIRST
    attempt — the composed result carries duplicate_file_ids but NOT
    failed_ids, so the runner's duplicate-only arm fires instead of
    persisting a narrowed $.retry_file_ids whose attempt-2 re-run can
    never execute (the #702 refusal guard rejects any scoped attempt
    while the dimension is still changed).
    """
    import hashlib
    import json as _json
    import sqlite3
    from types import SimpleNamespace

    from app.models.database import SQLiteConnectionPool, run_migrations
    from app.services.background_tasks import BackgroundProcessor

    db_path = str(tmp_path / "mixed.db")
    run_migrations(db_path)
    conn = sqlite3.connect(db_path, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    pool = SQLiteConnectionPool(db_path, max_size=4)
    lease_conn = sqlite3.connect(db_path, timeout=10, check_same_thread=False)
    lease_conn.row_factory = sqlite3.Row
    try:
        content = b"mixed-staged-abort-982f001"
        path = tmp_path / "doc.pdf"
        path.write_bytes(content)
        file_hash = hashlib.sha256(content).hexdigest()

        def _ins(status, error_message=None):
            cur = conn.execute(
                "INSERT INTO files (vault_id, file_path, file_name,"
                " file_hash, file_size, status, phase, source,"
                " error_message) VALUES (1, ?, ?, ?, 1, ?, 'queued',"
                " 'upload', ?)",
                (str(path), "doc.pdf", file_hash, status, error_message),
            )
            conn.commit()
            return int(cur.lastrowid)

        owner_id = _ins("indexed")
        transient_id = _ins("error", "transient parse failure (F-001)")
        dup_loser_id = _ins(
            "error", "DUPLICATE_FILE: settled by the finalization race"
        )
        assert owner_id != transient_id != dup_loser_id

        bp = BackgroundProcessor(pool=pool, retry_delay=0.05)

        class _Staged982F001Store:
            def __init__(self):
                self.calls = []
                self._embedded = set()

            def _record(self, name, *a, **k):
                self.calls.append((name, a, k))

            async def init_table(self, *a, **k):
                self._record("init_table", *a, **k)

            async def add_chunks(self, *a, **k):
                self._record("add_chunks", *a, **k)
                return {}

            async def delete_by_file(self, *a, **k):
                self._record("delete_by_file", *a, **k)

            async def delete_old_generation_by_file(self, *a, **k):
                self._record("delete_old_generation_by_file", *a, **k)
                return 0

            async def delete_generation_by_file_locked(self, *a, **k):
                self._record("delete_generation_by_file_locked", *a, **k)

            def write_locked(self):
                import contextlib

                @contextlib.asynccontextmanager
                async def _ctx():
                    self._record("write_locked_enter")
                    try:
                        yield self
                    finally:
                        self._record("write_locked_exit")

                return _ctx()

            async def count_by_file(self, *a, **k):
                return 1

            async def get_live_embedding_dim(self, *a, **k):
                return 3  # != embed stub dim 2 -> staged path

            async def begin_dimension_rebuild(self, *a, **k):
                self._record("begin_dimension_rebuild", *a, **k)
                return SimpleNamespace(table_name="staged_f001")

            async def commit_dimension_rebuild(self, *a, **k):
                self._record("commit_dimension_rebuild", *a, **k)

            async def abort_dimension_rebuild(self, *a, **k):
                self._record("abort_dimension_rebuild", *a, **k)

            async def record_embedding_metadata(self, *a, **k):
                self._record("record_embedding_metadata", *a, **k)

            async def mark_ready(self, *a, **k):
                self._record("mark_ready", *a, **k)

        stub = _Staged982F001Store()
        bp.processor.vector_store = stub

        class _EmbedStub:
            async def embed_batch(self, texts, fail_fast=True, **kwargs):
                return [[0.1, 0.2] for _ in texts]

        bp.processor.embedding_service = _EmbedStub()

        async def _fake_schema(file_path, file_id, generation_hash, parser_fingerprint):  # noqa: ARG001
            from app.services.chunking import ProcessedChunk
            from app.services.document_processor import ParsedDocument

            return (
                [
                    ProcessedChunk(
                        text="x",
                        metadata={"section": "s", "total_chunks": 1},
                        chunk_index=0,
                    )
                ],
                "x",
                ParsedDocument(atoms=(), parser_fingerprint=parser_fingerprint),
            )

        import unittest.mock as _mock

        with _mock.patch.object(
            bp.processor, "_is_schema_file", lambda p: True
        ), _mock.patch.object(
            bp.processor, "_process_schema_file", _fake_schema
        ):
            status, result, error = await bp._reindex_embed_all(
                1, vault_id=None
            )

        assert status == "failed", (
            f"982F001: mixed staged abort reports failure; got {status!r}"
        )
        dup_ids = result.get("duplicate_file_ids") or []
        assert dup_ids and set(dup_ids) <= {
            transient_id,
            dup_loser_id,
        }, (
            f"982F001: the discovered duplicate composes into the terminal "
            f"result (whichever non-owner row trips the guard first); got "
            f"{result!r}"
        )
        assert "failed_ids" not in result, (
            "982F001: the composed abort result must NOT carry failed_ids — "
            "the runner would persist a narrowed $.retry_file_ids whose "
            "attempt-2 re-run deterministically trips the #702 refusal "
            f"guard (review F-001); got {result!r}"
        )
        assert result.get("failed") == 2, (
            f"982F001: 'failed' counts the distinct terminal files "
            f"(transient + duplicate); got {result.get('failed')!r}"
        )
    finally:
        pool.close_all()
        conn.close()
        lease_conn.close()


def test_limiter_info_log_channel_bounded_for_uncolonned_userinfo(caplog):
    """Review F-003/A-1a (pr1169-20261117): the boot INFO line logs
    redact_url(storage_uri); redact_url does not mask username-only
    userinfo, so the guard must observe the REAL logger ('rate_limit')
    and probe a shape redact_url passes through verbatim. Companion to
    the frozen C6 (whose 'app.limiter' capture can never see these
    records — vacuous leg, disclosed).
    """
    import logging as _logging

    from app.limiter import build_limiter

    marker = "TOKENuser982F003"
    with caplog.at_level(_logging.INFO, logger="rate_limit"):
        with pytest.raises(RuntimeError):
            build_limiter(f"bogus2://{marker}@host.invalid:6379/0")
    info_records = [
        r for r in caplog.records if r.levelno == _logging.INFO
    ]
    assert any("wired to Redis" in r.message for r in info_records) or any(
        "Rate limiter" in r.message for r in info_records
    ), "sanity: the boot INFO line fired on the rate_limit logger"
    leaked = [r.message for r in info_records if marker in r.message]
    # The INFO line carries redact_url output; redact_url passes
    # username-only userinfo through VERBATIM (verified by the review
    # probe). This test PINS that known gap so the follow-up fix flips
    # it red — until then it documents the leak precisely.
    assert leaked, (
        "982F003: expected the known username-only pass-through on the "
        "rate_limit INFO line — if this fires, redact_url was hardened "
        "(follow-up landed); update this pin to assert NO leak then"
    )
