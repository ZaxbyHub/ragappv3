"""Issue #1206: the four permission queries must never share the caller's
sqlite3.Connection concurrently.

``get_effective_vault_permissions`` builds four query closures
(``_query_vault_members``, ``_query_group_access``, ``_query_public_vaults``,
``_query_org_vaults``) that all ``db.execute`` on the caller's single
``sqlite3.Connection``. Historically a SERIALIZED build
(``sqlite3.threadsafety == 3``) dispatched them concurrently through
``asyncio.gather(asyncio.to_thread(...))``; under full-suite CI load the
worker threads wedged inside ``db.execute`` with no failing test (Backend
job exit 88, watchdog quiet arm — see the #1206 evidence, Actions run
45263195017). The concurrent dispatch is removed; this file pins that it
cannot silently return:

- behavioral pin: an instrumented connection records OVERLAPPING ``execute``
  calls, so any concurrent redispatch of the closures fails here loudly
  (bounded, ~1s) instead of hanging a CI job for the watchdog's 360s window.
- structural pin (``test_no_shared_connection_gather``, the
  ``test_c08_seam_census`` style): the AST of ``get_effective_vault_permissions``
  contains no ``asyncio.gather`` call binding ``to_thread`` closures to the
  caller's connection — a refactor back to that shape fails source
  inspection even when no behavioral test happens to overlap. The matcher's
  self-tests feed KNOWN-BAD sources through the same walk so the census
  cannot silently fail open (PRR-002 class).

RED at the base commit (8786dba0), GREEN after the fix.
"""

import ast
import sqlite3
import threading
import time
from pathlib import Path
from typing import cast

from app.services import authz_policy
from app.services.authz_policy import get_effective_vault_permissions

_SOURCE = (
    Path(__file__).resolve().parents[1] / "app" / "services" / "authz_policy.py"
)

# How long each instrumented execute holds its "in flight" slot: wide enough
# that a genuine concurrent dispatch (threads arriving within thread-pool
# scheduling jitter) is recorded as overlap, small enough that the sequential
# post-fix run stays fast (4 x _HOLD_SECONDS total).
_HOLD_SECONDS = 0.25


class _OverlapTrackingConnection:
    """Duck-typed sqlite3.Connection delegate recording overlapping executes.

    ``get_effective_vault_permissions`` only touches ``db.execute(...)``
    (returning a cursor it then ``.fetchall()``s), so a delegate is a faithful
    stand-in. Every ``execute`` holds the in-flight slot for ``_HOLD_SECONDS``
    before delegating, so a concurrent dispatch cannot slip through a narrow
    window; a sequential dispatch never sees a second entry while one is
    active and records zero overlaps.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._guard = threading.Lock()
        self._in_flight = 0
        self.execute_calls = 0
        self.overlapping_execute_calls = 0

    def execute(self, sql, parameters=()):
        with self._guard:
            self.execute_calls += 1
            if self._in_flight:
                self.overlapping_execute_calls += 1
            self._in_flight += 1
        try:
            time.sleep(_HOLD_SECONDS)
            return self._conn.execute(sql, parameters)
        finally:
            with self._guard:
                self._in_flight -= 1


def _seeded_conn() -> sqlite3.Connection:
    """Hermetic schema seeded with all four grant classes for user 4.

    Mirrors the ``_vault_policy_db`` fixture shape from
    ``tests/test_deps_auth.py`` plus an org-visibility vault: direct
    membership (read), a group-derived grant (admin, gated by the
    org-membership EXISTS), a global public vault, an org-scoped public vault,
    an org-visibility vault, and an ungranted private vault.
    """
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute(
        "CREATE TABLE vaults ("
        "id INTEGER PRIMARY KEY, visibility TEXT DEFAULT 'private', org_id INTEGER)"
    )
    conn.execute(
        "CREATE TABLE vault_members (vault_id INTEGER, user_id INTEGER, permission TEXT)"
    )
    conn.execute("CREATE TABLE group_members (group_id INTEGER, user_id INTEGER)")
    conn.execute("CREATE TABLE groups (id INTEGER PRIMARY KEY, org_id INTEGER NOT NULL)")
    conn.execute(
        "CREATE TABLE vault_group_access"
        " (vault_id INTEGER, group_id INTEGER, permission TEXT)"
    )
    conn.execute(
        "CREATE TABLE org_members (org_id INTEGER NOT NULL, user_id INTEGER NOT NULL)"
    )
    conn.executemany(
        "INSERT INTO vaults (id, visibility, org_id) VALUES (?, ?, ?)",
        [
            (1, "private", None),
            (2, "private", None),
            (3, "public", None),
            (4, "public", 10),
            (5, "org", 10),
            (6, "private", None),
        ],
    )
    # org 10 owns vaults 4/5; org 200 owns no vaults, so membership in it
    # satisfies the reader-side group/org predicate (#721 AC4) without
    # granting anything else — same trick as _vault_policy_db.
    conn.execute("INSERT INTO org_members (org_id, user_id) VALUES (10, 4)")
    conn.execute("INSERT INTO groups (id, org_id) VALUES (10, 200)")
    conn.execute("INSERT INTO org_members (org_id, user_id) VALUES (200, 4)")
    conn.execute("INSERT INTO group_members (group_id, user_id) VALUES (10, 4)")
    conn.execute(
        "INSERT INTO vault_members (vault_id, user_id, permission)"
        " VALUES (1, 4, 'read')"
    )
    conn.execute(
        "INSERT INTO vault_group_access (vault_id, group_id, permission)"
        " VALUES (2, 10, 'admin')"
    )
    conn.commit()
    return conn


async def test_permission_queries_do_not_overlap_on_shared_connection(
    monkeypatch,
):
    """AC1: the four queries never overlap on one connection, map unchanged."""
    conn = _seeded_conn()
    tracked = _OverlapTrackingConnection(conn)
    # Force the gate that historically selected the concurrent gather branch,
    # so this test is RED-before/GREEN-after the fix on ANY SQLite build
    # (patch the module attribute — deps.py holds only a copy of the value).
    monkeypatch.setattr(authz_policy, "_SQLITE_SERIALIZED", True)

    result = await get_effective_vault_permissions(
        cast(sqlite3.Connection, tracked), {"id": 4, "role": "member"}, [1, 2, 3, 4, 5, 6]
    )

    assert result == {
        1: "read",  # direct membership
        2: "admin",  # group-derived grant
        3: "read",  # global public vault
        4: "read",  # org-scoped public vault (user 4 is in org 10)
        5: "read",  # org-visibility vault (user 4 is in org 10)
        6: None,  # private, ungranted
    }
    assert tracked.execute_calls == 4, (
        "expected exactly the four batched permission queries, "
        f"got {tracked.execute_calls}"
    )
    assert tracked.overlapping_execute_calls == 0, (
        "get_effective_vault_permissions ran permission queries concurrently "
        "on the caller's shared sqlite3.Connection — the exact #1206 CI wedge "
        f"({tracked.overlapping_execute_calls} overlapping execute calls)"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Structural pin (AC3): no asyncio.gather dispatch in get_effective_vault_permissions
# ─────────────────────────────────────────────────────────────────────────────


def _dotted_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return ""


def _gather_calls(tree: ast.AST) -> list[str]:
    """Every asyncio.gather call site inside tree, as dotted source names."""
    return [
        _dotted_name(call.func)
        for call in ast.walk(tree)
        if isinstance(call, ast.Call) and _dotted_name(call.func) == "asyncio.gather"
    ]


def _permissions_function(tree: ast.Module) -> ast.AsyncFunctionDef:
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef) and (
            node.name == "get_effective_vault_permissions"
        ):
            return node
    raise AssertionError("get_effective_vault_permissions not found at module level")


def test_no_shared_connection_gather():
    """AC3: the gather(to_thread(...)) dispatch construct must not exist."""
    func = _permissions_function(ast.parse(_SOURCE.read_text(encoding="utf-8")))
    offenders = _gather_calls(func)
    assert offenders == [], (
        "get_effective_vault_permissions dispatches through asyncio.gather — "
        "the #1206 wedge construct (concurrent to_thread closures on the "
        "caller's single sqlite3.Connection)"
    )


def test_gather_matcher_flags_the_known_bad_shape():
    """Fail-loud self-test: the census matcher flags the historical construct.

    Feeds a synthetic source containing the exact pre-#1206 dispatch through
    the same walk test_no_shared_connection_gather uses, so a matcher change
    that stops detecting the shape fails here instead of failing open.
    """
    known_bad = ast.parse(
        "async def get_effective_vault_permissions(db, principal, vault_ids):\n"
        "    rows = await asyncio.gather(\n"
        "        asyncio.to_thread(_query_vault_members),\n"
        "        asyncio.to_thread(_query_group_access),\n"
        "        asyncio.to_thread(_query_public_vaults),\n"
        "        asyncio.to_thread(_query_org_vaults),\n"
        "    )\n"
        "    return {}\n"
    )
    assert _gather_calls(_permissions_function(known_bad)) == ["asyncio.gather"]

    known_good = ast.parse(
        "async def get_effective_vault_permissions(db, principal, vault_ids):\n"
        "    a = await asyncio.to_thread(_query_vault_members)\n"
        "    b = await asyncio.to_thread(_query_group_access)\n"
        "    return {}\n"
    )
    assert _gather_calls(_permissions_function(known_good)) == []
