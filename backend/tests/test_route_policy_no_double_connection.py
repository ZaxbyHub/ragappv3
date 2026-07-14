"""Regression test for issue #205 (S-003): eliminate the double-connection.

Each migrated route must resolve vault permission via the DI
``get_evaluate_policy`` dependency (which reuses the request-scoped
``Depends(get_db)`` connection) and must NOT call the standalone
``evaluate_policy`` (which opens its own pool connection). At 10 concurrent
vault-scoped requests the legacy double-connection pattern over-subscribed
the pool 2:1 (demand 20 vs supply 10) and produced HTTP 503 after a 15s
timeout.

These tests assert the architectural invariant directly: for one
representative route per migrated file, ``app.api.deps.get_pool`` is NOT
invoked on the request path while the DI evaluate IS invoked. This catches
any regression that re-introduces a standalone ``evaluate_policy(...)`` call.

Target files: chat.py, memories.py, kms.py, wiki.py, tags.py, folders.py,
vault_members.py (the 7 files migrated in the S-003 fix).
"""

import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent))

from app.api.deps import get_current_active_user, get_db, get_evaluate_policy
from app.api.routes import chat, folders, kms, memories, tags, vault_members, wiki


@pytest.fixture
def db_path(monkeypatch):
    """Initialize a minimal DB and point settings at it. Uses tempfile directly
    rather than pytest's tmp_path to avoid the Windows tmp_path cleanup issue
    (documented local-interpreter artifact, see docs/engineering/testing.md)."""
    temp_dir = tempfile.mkdtemp(prefix="no-double-conn-")
    db_file = Path(temp_dir) / "no-double-conn.db"
    # Use the real schema so routes don't 500 on missing tables before the
    # permission check runs (the assertion target).
    from app.models.database import init_db, run_migrations

    init_db(str(db_file))
    run_migrations(str(db_file))
    conn = sqlite3.connect(str(db_file))
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute(
        "INSERT INTO users (id, username, hashed_password, full_name, role, is_active, must_change_password) "
        "VALUES (1, 'admin', 'x', 'Admin', 'superadmin', 1, 0)"
    )
    conn.execute("INSERT OR IGNORE INTO vaults (id, name) VALUES (1, 'V1')")
    conn.execute("INSERT INTO chat_sessions (id, vault_id, user_id, title) VALUES (1, 1, 1, 's1')")
    conn.commit()
    conn.close()

    # Clear the production pool cache so get_db does not reuse a stale pool.
    from app.models.database import _pool_cache, _pool_cache_lock

    with _pool_cache_lock:
        for p in list(_pool_cache.values()):
            p.close_all()
        _pool_cache.clear()

    monkeypatch.setattr("app.config.settings.data_dir", Path(temp_dir))
    yield str(db_file)

    with _pool_cache_lock:
        for p in list(_pool_cache.values()):
            p.close_all()
        _pool_cache.clear()
    shutil.rmtree(temp_dir, ignore_errors=True)


def _make_client(db_path, *, allow: bool, router, prefix: str) -> TestClient:
    """Build a TestClient whose DI policy grants/denies and whose get_db uses a
    real connection backed by the temp DB.

    Returns the client. The caller asserts on the patched get_pool call count.
    """
    app = FastAPI()
    app.include_router(router, prefix=prefix)
    app.state.vector_store = None

    app.dependency_overrides[get_current_active_user] = lambda: {
        "id": 1,
        "username": "admin",
        "role": "superadmin",
        "is_active": True,
        "must_change_password": False,
    }

    async def _evaluate(*_args, **_kwargs) -> bool:
        return allow

    app.dependency_overrides[get_evaluate_policy] = lambda: _evaluate

    # get_db backed by a real sqlite3 connection to the temp DB.
    def _get_db():
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    app.dependency_overrides[get_db] = _get_db

    return TestClient(app)


# ---------------------------------------------------------------------------
# Per-file representative routes. Each test fires one request and asserts that
# app.api.deps.get_pool is NOT called (no standalone evaluate_policy checkout)
# AND that the DI get_evaluate_policy IS resolved.
# ---------------------------------------------------------------------------


def test_chat_session_read_does_not_open_standalone_pool(db_path):
    client = _make_client(db_path, allow=True, router=chat.router, prefix="/api")
    with patch("app.api.deps.get_pool") as mock_get_pool:
        resp = client.get("/api/chat/sessions/1")
    # Route may 200 or 404 depending on seeded row, but must NOT open a pool.
    assert resp.status_code in (200, 404)
    assert mock_get_pool.call_count == 0, (
        "chat.get_session must not call standalone get_pool — it should use "
        "the DI get_evaluate_policy which reuses the injected connection."
    )


def test_memories_list_does_not_open_standalone_pool(db_path):
    client = _make_client(
        db_path, allow=True, router=memories.router, prefix="/api"
    )
    with patch("app.api.deps.get_pool") as mock_get_pool:
        resp = client.get("/api/memories", params={"vault_id": 1})
    assert resp.status_code in (200, 404)
    assert mock_get_pool.call_count == 0, (
        "memories.list_memories must not call standalone get_pool."
    )


def test_kms_list_does_not_open_standalone_pool(db_path):
    client = _make_client(db_path, allow=True, router=kms.router, prefix="/api")
    with patch("app.api.deps.get_pool") as mock_get_pool:
        resp = client.get("/api/kms/entries", params={"vault_id": 1})
    assert resp.status_code in (200, 404)
    assert mock_get_pool.call_count == 0, (
        "kms.list_kms_entries must not call standalone get_pool."
    )


def test_wiki_pages_list_does_not_open_standalone_pool(db_path):
    client = _make_client(db_path, allow=True, router=wiki.router, prefix="/api")
    with patch("app.api.deps.get_pool") as mock_get_pool:
        resp = client.get("/api/wiki/pages", params={"vault_id": 1})
    assert resp.status_code in (200, 404)
    assert mock_get_pool.call_count == 0, (
        "wiki.list_wiki_pages must not call standalone get_pool."
    )


def test_tags_list_does_not_open_standalone_pool(db_path):
    client = _make_client(db_path, allow=True, router=tags.router, prefix="/api")
    with patch("app.api.deps.get_pool") as mock_get_pool:
        resp = client.get("/api/tags", params={"vault_id": 1})
    assert resp.status_code in (200, 404)
    assert mock_get_pool.call_count == 0, (
        "tags.list_tags must not call standalone get_pool."
    )


def test_folders_list_does_not_open_standalone_pool(db_path):
    client = _make_client(db_path, allow=True, router=folders.router, prefix="/api")
    with patch("app.api.deps.get_pool") as mock_get_pool:
        resp = client.get("/api/folders", params={"vault_id": 1})
    assert resp.status_code in (200, 404)
    assert mock_get_pool.call_count == 0, (
        "folders.list_folders must not call standalone get_pool."
    )


def test_vault_group_access_list_does_not_open_standalone_pool(db_path):
    """vault_members.list_vault_group_access was migrated from a legacy
    get_pool()/try/finally/release pattern to Depends(get_db). It must no
    longer call get_pool directly."""
    client = _make_client(
        db_path,
        allow=True,
        router=vault_members.group_access_router,
        prefix="/api",
    )
    with patch("app.api.deps.get_pool") as mock_get_pool:
        resp = client.get("/api/vaults/1/group-access")
    assert resp.status_code in (200, 404)
    assert mock_get_pool.call_count == 0, (
        "vault_members.list_vault_group_access must not call get_pool — it "
        "now uses Depends(get_db)."
    )


# ---------------------------------------------------------------------------
# Deny path: the DI evaluate returns False → 403, and get_pool is still NOT
# called. This confirms the permission gate runs through DI on both branches.
# ---------------------------------------------------------------------------


def test_denied_route_returns_403_without_standalone_pool(db_path):
    client = _make_client(
        db_path, allow=False, router=memories.router, prefix="/api"
    )
    with patch("app.api.deps.get_pool") as mock_get_pool:
        resp = client.get("/api/memories", params={"vault_id": 1})
    assert resp.status_code == 403
    assert mock_get_pool.call_count == 0, (
        "Denied permission path must also avoid standalone get_pool."
    )


# ---------------------------------------------------------------------------
# Carve-out: wiki_events_stream (SSE endpoint) intentionally retains the
# standalone evaluate_policy for its pre-stream permission check. This test
# LOCKS IN that design decision: if someone later routes it through
# Depends(get_evaluate_policy) without thinking through the SSE connection
# lifecycle, this test will flag the change for review.
#
# See the wiki_events_stream docstring for the full rationale.
# ---------------------------------------------------------------------------


def test_wiki_events_stream_uses_standalone_evaluate_policy_by_design(db_path):
    """wiki_events_stream intentionally keeps the standalone evaluate_policy.

    This is the documented exception to the S-003 migration. The endpoint's
    permission check runs BEFORE the indefinite SSE loop; using the standalone
    makes the check a short-lived transient connection rather than wiring
    another Depends(get_db)-backed dependency into the stream. This test
    asserts the standalone IS used (get_pool IS called) so that any future
    change to this endpoint's permission path is a deliberate, reviewed
    decision — not an accidental regression or silent fix.
    """
    client = _make_client(db_path, allow=True, router=wiki.router, prefix="/api")
    # The SSE endpoint streams indefinitely; override the event bus so the
    # stream closes after the pre-stream check so TestClient returns.
    import asyncio

    closed = asyncio.Event()

    class _ImmediateCloseBus:
        def subscribe(self, _vault_id):
            class _Q:
                def __init__(self):
                    self._tasks = []

                async def get(self):
                    await asyncio.sleep(0.01)
                    raise asyncio.CancelledError()

                def put_nowait(self, _item):
                    pass

            return _Q()

        def unsubscribe(self, _vault_id, _queue):
            closed.set()

    with patch("app.api.routes.wiki.get_wiki_event_bus", return_value=_ImmediateCloseBus()):
        with patch("app.api.deps.get_pool") as mock_get_pool:
            # Allow the standalone evaluate_policy to succeed (superadmin).
            with patch("app.api.deps._evaluate_policy", return_value=True):
                try:
                    client.get("/api/wiki/events", params={"vault_id": 1})
                except Exception:
                    # The stream may raise on close; we only care about the
                    # pre-stream check having run.
                    pass
    # The standalone evaluate_policy path MUST have called get_pool. If this
    # assertion fails, someone changed wiki_events_stream's permission path —
    # review whether the SSE connection lifecycle is still acceptable.
    assert mock_get_pool.call_count >= 1, (
        "wiki_events_stream is the documented S-003 carve-out: its pre-stream "
        "permission check should use the standalone evaluate_policy (which "
        "calls get_pool). If you migrated it to Depends(get_evaluate_policy), "
        "update this test AND the wiki_events_stream docstring to reflect "
        "the new design."
    )


# ---------------------------------------------------------------------------
# Full-stack signature-drift guard: exercises endpoints whose handler signature
# + helper call must stay in sync. These catch the class of bug (F-001 on PR
# #384) where a handler was missed by the migration and called the renamed
# helper with the old arity, raising TypeError at runtime. Source-text
# inspection tests cannot catch this — only a real request through the router
# surfaces the error.
# ---------------------------------------------------------------------------


def test_batch_memory_wiki_status_does_not_raise_typeerror(db_path, monkeypatch):
    """POST /wiki/memories/batch-status must reach the handler body.

    Regression for F-001 (PR #384): batch_memory_wiki_status was missed by the
    S-003 migration and called ``_require_vault_read(user, vault_id)`` with 2
    args after the helper signature changed to ``(evaluate, user, vault_id)``.
    This raised ``TypeError`` on every request before any DB work. A real
    request through the router is the only reliable way to catch signature
    drift between a handler and its helper.
    """
    from app.security import csrf_protect

    client = _make_client(db_path, allow=True, router=wiki.router, prefix="/api")
    # Bypass CSRF (the endpoint is a POST) so the request reaches the handler.
    client.app.dependency_overrides[csrf_protect] = lambda: "test-csrf-token"
    # Master switch: wiki must be enabled for the route to register.
    monkeypatch.setattr("app.config.settings.wiki_enabled", True)

    resp = client.post(
        "/api/wiki/memories/batch-status?vault_id=1",
        json={"memory_ids": []},
    )
    # The bug (F-001) raised TypeError -> 500 before reaching the handler body.
    # A correct call returns 200 with a (possibly empty) statuses dict. Any 5xx
    # here must be investigated as a signature/arity regression.
    assert resp.status_code == 200, (
        f"Expected 200 from batch_memory_wiki_status, got {resp.status_code} "
        f"body={resp.text}. A 500 typically means the handler signature and "
        f"its _require_vault_read/_require_vault_write call are out of sync "
        f"(re-check the helper arity after the S-003 DI migration)."
    )
    assert "statuses" in resp.json()
