"""Frozen issue-#707 acceptance checks, authored at arm's length.

These three checks pin the contract the issue-#707 fix must land, and each
one FAILS at the buggy tree (verified against HEAD):

* AC1 — an ``author`` filter must resolve against the REAL configured
  database (``settings.sqlite_path`` via a fully migrated schema), with
  ``get_pool`` NOT patched anywhere: the resolver itself must check out the
  pool correctly. Today the call site invokes ``get_pool()`` with no
  arguments (``get_pool(sqlite_path: str, max_size: int = 5)``), every
  resolution raises TypeError, and the broad ``except Exception`` swallows
  it into the zero-match sentinel — so chat metadata filters silently
  retrieve nothing.
* AC2 — a programming error (TypeError from a signature-faithful
  ``get_pool`` double) must propagate, not be swallowed into the sentinel.
  Only the three documented environmental-unavailability classes
  (``OSError``, ``RuntimeError``, ``sqlite3.Error``) may degrade to
  zero-match.
* AC3 — the legacy suite's signature-blind ``lambda *a, **kw`` replacement
  of ``app.models.database.get_pool`` (the double that let the buggy
  zero-argument call site keep passing) must be gone.

AC1/AC2 tolerate the resolver becoming async as part of the fix: they call
it and await the result only when ``inspect.isawaitable`` says so (checking
the function with ``asyncio.iscoroutinefunction`` alone is not enough — a
sync function may return an awaitable and vice versa).
"""

from __future__ import annotations

import ast
import inspect
import sqlite3
from pathlib import Path

import pytest

from app.config import settings
from app.models.database import run_migrations
from app.services.metadata_filter import resolve_metadata_filter

_GET_POOL_DOTTED = "app.models.database.get_pool"


async def _resolve_awaiting_if_needed(
    metadata_filter: dict, vault_id: int
) -> str | None:
    """Call ``resolve_metadata_filter`` and await the result if awaitable.

    The issue-#707 fix may convert the resolver (or its pool checkout) to
    async; these checks must stay valid across that conversion, so the call
    result is inspected rather than the function object.
    """
    result = resolve_metadata_filter(metadata_filter, vault_id=vault_id)
    if inspect.isawaitable(result):
        result = await result
    return result


@pytest.fixture()
def migrated_db(monkeypatch, tmp_path):
    """Real migrated DB under ``tmp_path`` with ``settings.data_dir`` on it.

    Mirrors the ``seeded_env`` fixture in ``tests/test_issue645_converted_route_http.py``:
    ``settings.data_dir`` drives the read-only derived property
    ``settings.sqlite_path`` (never patch the property itself), the global
    pool cache is closed and cleared under its lock so any
    ``get_pool(str(settings.sqlite_path))`` checkout creates a fresh pool
    over THIS database, and the REAL schema is created via ``run_migrations``.
    """
    monkeypatch.setattr("app.config.settings.data_dir", tmp_path)
    db_path = str(settings.sqlite_path)

    from app.models.database import _pool_cache, _pool_cache_lock

    with _pool_cache_lock:
        for _path, pool in list(_pool_cache.items()):
            pool.close_all()
        _pool_cache.clear()

    run_migrations(db_path)

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        # The migrations already seed vault id=1 ('Default') on a fresh DB;
        # OR IGNORE keeps this valid whether or not that seed row exists.
        conn.execute(
            "INSERT OR IGNORE INTO vaults (id, name) VALUES (1, 'c02-ac-vault')"
        )
        # Minimal-but-valid row against the REAL files schema: the only
        # NOT NULL columns without defaults are vault_id, file_path,
        # file_name and file_size; email_sender is the author field the
        # resolver queries.
        conn.execute(
            "INSERT INTO files ("
            "id, vault_id, file_path, file_name, file_size, email_sender"
            ") VALUES (7, 1, 'ingest/alice.eml', 'alice.eml', 128, "
            "'alice@corp.example')"
        )
        conn.commit()
    finally:
        conn.close()

    yield db_path

    with _pool_cache_lock:
        if db_path in _pool_cache:
            _pool_cache[db_path].close_all()
            del _pool_cache[db_path]


@pytest.mark.asyncio
async def test_author_filter_resolves_against_configured_db(migrated_db):
    """AC1: an author filter resolves file ids from the configured DB.

    No ``get_pool`` patch exists in this test — the resolver must obtain the
    pool for ``str(settings.sqlite_path)`` itself and run its query against
    the real migrated schema. At the buggy tree every resolution raises
    TypeError at the ``get_pool()`` call site and the broad except degrades
    it to the zero-match sentinel, so this fails with
    ``"file_id IN ('')" != "file_id IN ('7')"``.
    """
    expr = await _resolve_awaiting_if_needed(
        {"author": "alice@corp.example"}, vault_id=1
    )
    assert expr == "file_id IN ('7')"


@pytest.mark.asyncio
async def test_programming_error_is_not_swallowed(monkeypatch):
    """AC2: a TypeError from signature drift must escape the resolver.

    A signature-faithful ``get_pool`` double raises TypeError the moment the
    resolver calls it with the wrong shape. A genuinely unavailable metadata
    DB (``sqlite3.Error``) may legitimately degrade to the zero-match
    sentinel, but a programming error must propagate — swallowing it is how
    the bug stayed invisible. At the buggy tree the broad except eats the
    TypeError and returns the sentinel, so ``raised`` stays False.
    """

    def _drift(sqlite_path, max_size=5):
        raise TypeError("signature drift")

    monkeypatch.setattr(_GET_POOL_DOTTED, _drift)

    raised = False
    try:
        await _resolve_awaiting_if_needed({"tags": ["finance"]}, vault_id=1)
    except TypeError:
        raised = True
    assert raised is True


def test_no_signature_blind_get_pool_patch():
    """AC3: no signature-blind ``get_pool`` lambda in the legacy suite.

    ``tests/test_metadata_filter.py`` replaced ``get_pool`` with
    ``lambda *a, **kw: real_pool`` — a double that accepts ANY call shape,
    which is exactly why the buggy zero-argument call site kept passing its
    tests. This check parses that file's AST (path resolved relative to this
    file, never a hardcoded absolute path) and fails while any monkeypatch
    of the dotted ``get_pool`` name with a variadic-args lambda remains.
    """
    target = Path(__file__).with_name("test_metadata_filter.py")
    tree = ast.parse(target.read_text(encoding="utf-8"), filename=str(target))

    count = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (
            isinstance(func, ast.Attribute)
            and func.attr == "setattr"
            and isinstance(func.value, ast.Name)
            and func.value.id == "monkeypatch"
        ):
            continue
        if len(node.args) < 2:
            continue
        target_arg, value_arg = node.args[0], node.args[1]
        if not (
            isinstance(target_arg, ast.Constant)
            and target_arg.value == _GET_POOL_DOTTED
        ):
            continue
        if not isinstance(value_arg, ast.Lambda):
            continue
        if value_arg.args.vararg is not None or value_arg.args.kwarg is not None:
            count += 1
    assert count == 0
