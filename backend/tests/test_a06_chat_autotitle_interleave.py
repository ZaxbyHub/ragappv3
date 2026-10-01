"""Issue #688 defect 3 (A06) — auto-title UPDATEs overwrite concurrent
manual renames, RED at base.

The sibling title writes in chat.py are guarded (compare-and-set in
``_auto_name_session``'s titled branch; ``AND title IS NULL`` in the LLM
fallback), but two UPDATEs are not:

  1. the untitled branch of ``_auto_name_session`` —
     ``UPDATE chat_sessions SET title = ?, updated_at = ... WHERE id = ?``
  2. ``add_message``'s no-LLM fallback (``rag_engine=None``) — the same
     unguarded shape, writing 'New conversation'.

A manual rename that commits between the earlier SELECT and either UPDATE
gets silently overwritten.

The interleave is deterministic: the route under test runs over a REAL
sqlite3 connection proxy whose ``execute`` intercepts the first
``UPDATE chat_sessions SET title`` statement and, on a SEPARATE connection,
commits ``title='Manual'`` before the intercepted statement runs.
"""

import os
import sqlite3
import sys
import types
from unittest.mock import MagicMock

import pytest
from starlette.requests import Request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

CSRF_TEST_POLICY = "naive"


def _stub_optional_modules() -> None:
    """Stub missing optional heavy deps so importing app modules is cheap.

    Per-file stubs are load-bearing for CI (requirements-ci.txt omits
    unstructured; lancedb/pyarrow stubs are local fallbacks) — see
    docs/engineering/testing.md section 2.
    """
    for name in ("lancedb", "pyarrow"):
        if name in sys.modules:
            continue
        try:
            __import__(name)
        except ImportError:
            sys.modules[name] = types.ModuleType(name)
    try:
        from unstructured.partition.auto import partition  # noqa: F401

        return
    except Exception:
        pass
    unstructured = types.ModuleType("unstructured")
    unstructured.__path__ = []
    partition_pkg = types.ModuleType("unstructured.partition")
    partition_pkg.__path__ = []
    auto = types.ModuleType("unstructured.partition.auto")
    auto.partition = lambda *args, **kwargs: []
    chunking = types.ModuleType("unstructured.chunking")
    chunking.__path__ = []
    title = types.ModuleType("unstructured.chunking.title")
    title.chunk_by_title = lambda *args, **kwargs: []
    documents = types.ModuleType("unstructured.documents")
    documents.__path__ = []
    elements = types.ModuleType("unstructured.documents.elements")
    elements.Element = type("Element", (), {})
    unstructured.partition = partition_pkg
    partition_pkg.auto = auto
    chunking.title = title
    documents.elements = elements
    for name, mod in [
        ("unstructured", unstructured),
        ("unstructured.partition", partition_pkg),
        ("unstructured.partition.auto", auto),
        ("unstructured.chunking", chunking),
        ("unstructured.chunking.title", title),
        ("unstructured.documents", documents),
        ("unstructured.documents.elements", elements),
    ]:
        sys.modules[name] = mod


_stub_optional_modules()

from app.api.routes import chat as chat_routes  # noqa: E402
from app.models.database import init_db, run_migrations  # noqa: E402


def _mock_request():
    request = MagicMock(spec=Request)
    request.client.host = "127.0.0.1"
    return request


async def _allow(*args):
    return True


class _FakeLLM:
    """LLM stub whose chat_completion returns a fixed title."""

    def __init__(self, title):
        self._title = title
        self.calls = 0

    async def chat_completion(self, messages=None, temperature=None, max_tokens=None):
        self.calls += 1
        return self._title


class _InterleavedTitleConn(sqlite3.Connection):
    """Real-connection proxy (deterministic interleave).

    On the FIRST ``UPDATE chat_sessions SET title`` statement, a manual
    rename ('Manual') is committed on a SEPARATE connection before the
    intercepted statement is allowed to run — modeling a manual rename that
    lands between the route's earlier SELECT and its title UPDATE.
    """

    def _interleave_manual_rename(self, sql):
        if not getattr(self, "armed", False):
            return
        if "SET title" not in sql or "chat_sessions" not in sql:
            return
        self.armed = False  # only the first title UPDATE interleaves
        other = sqlite3.connect(self.manual_db_path)
        try:
            other.execute(
                "UPDATE chat_sessions SET title = ? WHERE id = ?",
                ("Manual", self.target_session_id),
            )
            other.commit()
        finally:
            other.close()

    def execute(self, sql, parameters=()):
        self._interleave_manual_rename(sql)
        return super().execute(sql, parameters)


def _connect_proxy(db_path):
    conn = sqlite3.connect(
        str(db_path), check_same_thread=False, factory=_InterleavedTitleConn
    )
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.manual_db_path = str(db_path)
    conn.armed = True
    return conn


def _connect_plain(db_path):
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _make_session(conn, title=None):
    return conn.execute(
        "INSERT INTO chat_sessions (vault_id, user_id, title) VALUES (?, ?, ?)",
        (1, 1, title),
    ).lastrowid


def _stored_title(db_path, session_id):
    check = _connect_plain(db_path)
    try:
        return check.execute(
            "SELECT title FROM chat_sessions WHERE id = ?", (session_id,)
        ).fetchone()[0]
    finally:
        check.close()


class _FakePool:
    """Minimal get_pool stand-in exposing .connection_async() over the
    proxied test connection (#645: _auto_name_session uses the async CM)."""

    def __init__(self, conn):
        self._conn = conn

    class _AsyncConnCtx:
        def __init__(self, conn):
            self._conn = conn

        async def __aenter__(self):
            return self._conn

        async def __aexit__(self, *exc):
            return False

    def connection_async(self):
        return _FakePool._AsyncConnCtx(self._conn)


@pytest.mark.asyncio
async def test_untitled_branch_auto_name_keeps_concurrent_manual_rename(
    tmp_path, monkeypatch
):
    """A manual rename committed between _auto_name_session's SELECT (title
    NULL) and its untitled-branch UPDATE must survive the UPDATE."""
    db_path = tmp_path / "autotitle-untitled.db"
    init_db(str(db_path))
    run_migrations(str(db_path))
    conn = _connect_proxy(db_path)
    try:
        session_id = _make_session(conn, title=None)
        conn.commit()
        conn.target_session_id = session_id
        conn.armed = True

        monkeypatch.setattr(
            chat_routes, "get_pool", lambda *args, **kwargs: _FakePool(conn)
        )

        await chat_routes._auto_name_session(
            session_id, "Hello, how are you?", _FakeLLM("Auto Title")
        )
    finally:
        conn.close()

    stored_title = _stored_title(db_path, session_id)
    assert stored_title == "Manual"


@pytest.mark.asyncio
async def test_fallback_title_keeps_concurrent_manual_rename(tmp_path):
    """A manual rename committed just before add_message's no-LLM fallback
    title UPDATE (rag_engine=None, first user message) must survive it."""
    db_path = tmp_path / "autotitle-fallback.db"
    init_db(str(db_path))
    run_migrations(str(db_path))
    conn = _connect_proxy(db_path)
    try:
        session_id = _make_session(conn, title=None)
        conn.commit()
        conn.target_session_id = session_id
        conn.armed = True

        await chat_routes.add_message(
            _mock_request(),
            session_id,
            chat_routes.AddMessageRequest(role="user", content="First question"),
            conn,
            {"id": 1},
            evaluate=_allow,
            rag_engine=None,
            _csrf_token="t",
        )
    finally:
        conn.close()

    stored_title = _stored_title(db_path, session_id)
    assert stored_title == "Manual"
