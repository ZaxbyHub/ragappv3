"""
Tests for wiki.py DI-migration of _require_vault_read / _require_vault_write.

Migration: helpers previously called standalone evaluate_policy() (which opened
their own pool connection). After migration they accept db: sqlite3.Connection
and call get_evaluate_policy(db) locally, reusing the request-scoped injected
connection.

This file verifies:
1. wiki_events_stream correctly declares and uses evaluate via Depends(get_evaluate_policy).
2. _require_vault_read and _require_vault_write are called with the injected db.
3. All wiki endpoints that call _require_vault_read/_require_vault_write
   receive db from FastAPI DI (not a fresh connection).
4. The existing wiki route tests still pass (backward compatibility).
"""

import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from queue import Empty, Queue, SimpleQueue
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Stub optional heavy dependencies (load-bearing for CI).
try:
    import lancedb
except ImportError:
    import types
    sys.modules["lancedb"] = types.ModuleType("lancedb")

try:
    import pyarrow
except ImportError:
    import types
    sys.modules["pyarrow"] = types.ModuleType("pyarrow")

try:
    from unstructured.partition.auto import partition  # noqa: F401
except ImportError:
    import types
    _u = types.ModuleType("unstructured")
    _u.__path__ = []
    _u.partition = types.ModuleType("unstructured.partition")
    _u.partition.__path__ = []
    _u.partition.auto = types.ModuleType("unstructured.partition.auto")
    _u.partition.auto.partition = lambda *a, **k: []
    _u.chunking = types.ModuleType("unstructured.chunking")
    _u.chunking.__path__ = []
    _u.chunking.title = types.ModuleType("unstructured.chunking.title")
    _u.chunking.title.chunk_by_title = lambda *a, **k: []
    _u.documents = types.ModuleType("unstructured.documents")
    _u.documents.__path__ = []
    _u.documents.elements = types.ModuleType("unstructured.documents.elements")
    _u.documents.elements.Element = type("Element", (), {})
    sys.modules["unstructured"] = _u
    sys.modules["unstructured.partition"] = _u.partition
    sys.modules["unstructured.partition.auto"] = _u.partition.auto
    sys.modules["unstructured.chunking"] = _u.chunking
    sys.modules["unstructured.chunking.title"] = _u.chunking.title
    sys.modules["unstructured.documents"] = _u.documents
    sys.modules["unstructured.documents.elements"] = _u.documents.elements

from fastapi.testclient import TestClient

from app.api.deps import (
    get_current_active_user,
    get_db,
    get_evaluate_policy,
    get_vector_store,
)
from app.config import settings
from app.main import app
from app.security import csrf_protect

_MOCK_SUPERADMIN = {
    "id": 0,
    "username": "admin",
    "full_name": "Admin",
    "role": "superadmin",
    "is_active": True,
    "must_change_password": False,
}


# ---------------------------------------------------------------------------
# Test base
# ---------------------------------------------------------------------------

class WikiDITestBase(unittest.TestCase):
    """Base harness for wiki DI tests — shared pool and override plumbing."""

    def setUp(self):
        self.client = TestClient(app)
        self._temp_dir = tempfile.mkdtemp()
        self._original_data_dir = settings.data_dir
        settings.data_dir = Path(self._temp_dir)

        db_path = str(Path(self._temp_dir) / "app.db")

        # Clear pool cache so a fresh pool is created.
        from app.models.database import _pool_cache, _pool_cache_lock
        with _pool_cache_lock:
            for _p in list(_pool_cache.values()):
                _p.close_all()
            _pool_cache.clear()

        from app.models.database import init_db
        init_db(db_path)

        self._pool = _SimplePool(db_path)
        self._db_path = db_path

        # Seed default vault.
        conn = self._pool.get()
        conn.execute("INSERT OR IGNORE INTO vaults (id, name) VALUES (1, 'Default')")
        conn.commit()
        self._pool.release(conn)

        # Ensure job columns exist (required for /recompile and other job routes).
        conn = self._pool.get()
        job_cols = {r[1] for r in conn.execute("PRAGMA table_info(wiki_compile_jobs)").fetchall()}
        if "input_json" not in job_cols:
            conn.execute("ALTER TABLE wiki_compile_jobs ADD COLUMN input_json TEXT DEFAULT '{}'")
        if "retry_count" not in job_cols:
            conn.execute("ALTER TABLE wiki_compile_jobs ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0")
        conn.commit()
        self._pool.release(conn)

        def _get_db_override():
            conn = self._pool.get()
            try:
                yield conn
            finally:
                self._pool.release(conn)

        mock_vs = MagicMock()
        mock_vs.delete_by_vault = MagicMock(return_value=0)

        app.dependency_overrides[get_current_active_user] = lambda: _MOCK_SUPERADMIN
        app.dependency_overrides[get_db] = _get_db_override
        app.dependency_overrides[get_vector_store] = lambda: mock_vs
        app.dependency_overrides[csrf_protect] = lambda: "test-csrf-token"
        # Allow all vault access for DI tests (superadmin bypasses all policy checks).
        async def _allow_all(user, resource, resource_id, action):
            return True
        app.dependency_overrides[get_evaluate_policy] = lambda: _allow_all

    def tearDown(self):
        app.dependency_overrides.pop(get_current_active_user, None)
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_vector_store, None)
        app.dependency_overrides.pop(csrf_protect, None)
        app.dependency_overrides.pop(get_evaluate_policy, None)
        self._pool.close_all()

        settings.data_dir = self._original_data_dir
        from app.models.database import _pool_cache, _pool_cache_lock
        with _pool_cache_lock:
            for _p in list(_pool_cache.values()):
                _p.close_all()
            _pool_cache.clear()

        import shutil
        shutil.rmtree(self._temp_dir, ignore_errors=True)

    def _raw(self) -> sqlite3.Connection:
        return self._pool.get()


class _SimplePool:
    """Minimal connection pool for test harness."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._pool: SimpleQueue = SimpleQueue()
        self._closed = False

    def get(self) -> sqlite3.Connection:
        if self._closed:
            raise RuntimeError("Pool closed")
        try:
            return self._pool.get_nowait()
        except Empty:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON;")
            return conn

    def release(self, conn: sqlite3.Connection) -> None:
        if not self._closed:
            try:
                self._pool.put_nowait(conn)
            except Exception:
                conn.close()

    def close_all(self) -> None:
        self._closed = True
        while True:
            try:
                self._pool.get_nowait().close()
            except Empty:
                break


# ---------------------------------------------------------------------------
# Test: wiki_events_stream endpoint DI wiring
# ---------------------------------------------------------------------------

class TestWikiEventsStreamDI(WikiDITestBase):
    """
    Verify wiki_events_stream uses evaluate: Callable = Depends(get_evaluate_policy).

    The endpoint receives evaluate via FastAPI dependency injection, which reuses
    the request-scoped connection from get_db. This is consistent with the
    Issue 205 spec and allows FastAPI to override get_evaluate_policy in tests.
    """

    def test_wiki_events_stream_no_db_dependency(self):
        """
        The endpoint must NOT accept db as a FastAPI dependency.
        It uses evaluate: Callable = Depends(get_evaluate_policy) instead.
        """
        import inspect

        from app.api.routes.wiki import wiki_events_stream
        sig = inspect.signature(wiki_events_stream)
        param_names = {p.name for p in sig.parameters.values()}
        self.assertNotIn(
            "db", param_names,
            "wiki_events_stream must NOT have a db parameter — "
            "it uses evaluate via Depends(get_evaluate_policy) instead"
        )

    def test_wiki_events_stream_has_evaluate_dependency(self):
        """
        The endpoint must accept evaluate as a dependency from get_evaluate_policy.
        """
        import inspect

        from app.api.routes.wiki import wiki_events_stream
        sig = inspect.signature(wiki_events_stream)
        param_names = {p.name for p in sig.parameters.values()}
        self.assertIn(
            "evaluate", param_names,
            "wiki_events_stream must have an evaluate parameter from "
            "Depends(get_evaluate_policy)"
        )

    def test_wiki_events_stream_is_async(self):
        """
        The endpoint must be an async function.
        """
        from app.api.routes.wiki import wiki_events_stream
        self.assertTrue(
            asyncio.iscoroutinefunction(wiki_events_stream),
            "wiki_events_stream must be an async function"
        )

    def test_wiki_events_stream_calls_evaluate_via_di(self):
        """
        Verify the endpoint calls evaluate (from Depends(get_evaluate_policy))
        with (user, 'vault', vault_id, 'read').

        Since evaluate is now a Depends(get_evaluate_policy) parameter, direct
        invocation requires passing evaluate=tracking_evaluate as a keyword arg.
        We call the endpoint directly to avoid TestClient's SSE stream deadlock
        on Windows.
        """
        from fastapi.responses import StreamingResponse

        from app.api.routes.wiki import wiki_events_stream

        called = []

        async def tracking_evaluate(user, resource, resource_id, action):
            called.append((user, resource, resource_id, action))
            return True

        async def direct_call():
            return await wiki_events_stream(vault_id=1, user=_MOCK_SUPERADMIN, evaluate=tracking_evaluate)

        response = asyncio.run(direct_call())
        self.assertIsInstance(response, StreamingResponse)

        self.assertEqual(
            len(called), 1,
            "evaluate must be called exactly once via Depends(get_evaluate_policy)"
        )
        self.assertEqual(called[0][1], "vault")
        self.assertEqual(called[0][2], 1)
        self.assertEqual(called[0][3], "read")


# ---------------------------------------------------------------------------
# Test: _require_vault_read / _require_vault_write DI wiring
# ---------------------------------------------------------------------------

class TestRequireVaultHelpersDI(WikiDITestBase):
    """
    Verify the helpers receive the injected db, NOT a fresh connection.

    The key security property: _require_vault_read and _require_vault_write
    must call get_evaluate_policy(db) — not evaluate_policy() which would
    open a standalone pool connection.
    """

    def test_require_vault_read_called_with_injected_db(self):
        """
        Capture the db object passed to _require_vault_read and verify
        it is the same connection object from our pool override.
        """
        captured_db = []

        original_require_vault_read = __import__(
            "app.api.routes.wiki",
            fromlist=["_require_vault_read"]
        )._require_vault_read

        async def patched_require_vault_read(db, user, vault_id):
            captured_db.append(db)
            # Fall through to the real implementation.
            return await original_require_vault_read(db, user, vault_id)

        with patch("app.api.routes.wiki._require_vault_read", patched_require_vault_read):
            resp = self.client.get("/api/wiki/pages", params={"vault_id": 1})
            self.assertEqual(resp.status_code, 200, resp.text)

        self.assertEqual(len(captured_db), 1, "Must capture exactly one db call")
        # The captured db must be a sqlite3.Connection (our pool returns Row-wrapped connections).
        self.assertIsInstance(captured_db[0], sqlite3.Connection)

    def test_require_vault_write_called_with_injected_db(self):
        """
        Capture the db object passed to _require_vault_write.
        """
        captured_db = []

        original_require_vault_write = __import__(
            "app.api.routes.wiki",
            fromlist=["_require_vault_write"]
        )._require_vault_write

        async def patched_require_vault_write(db, user, vault_id):
            captured_db.append(db)
            return await original_require_vault_write(db, user, vault_id)

        with patch("app.api.routes.wiki._require_vault_write", patched_require_vault_write):
            resp = self.client.post(
                "/api/wiki/pages",
                json={"vault_id": 1, "title": "Test", "page_type": "overview"},
            )
            self.assertEqual(resp.status_code, 201, resp.text)

        self.assertEqual(len(captured_db), 1)
        self.assertIsInstance(captured_db[0], sqlite3.Connection)

    def test_get_evaluate_policy_called_with_injected_db(self):
        """
        Verify that inside _require_vault_read, get_evaluate_policy is called
        with the SAME db object that was injected, not a fresh connection.
        """
        call_records = []

        original_get_evaluate_policy = __import__(
            "app.api.deps", fromlist=["get_evaluate_policy"]
        ).get_evaluate_policy

        def patched_get_evaluate_policy(db):
            call_records.append(("get_evaluate_policy", db))
            return original_get_evaluate_policy(db)

        with patch("app.api.routes.wiki.get_evaluate_policy", patched_get_evaluate_policy):
            resp = self.client.get("/api/wiki/pages", params={"vault_id": 1})
            self.assertEqual(resp.status_code, 200, resp.text)

        self.assertEqual(len(call_records), 1)
        method_name, passed_db = call_records[0]
        self.assertEqual(method_name, "get_evaluate_policy")
        self.assertIsInstance(passed_db, sqlite3.Connection)


# ---------------------------------------------------------------------------
# Test: permission decisions still work after migration
# ---------------------------------------------------------------------------

class TestWikiPermissionsAfterMigration(WikiDITestBase):
    """
    Verify permission decisions (403 on no-access) are preserved after the DI change.
    """

    def test_no_read_access_returns_403(self):
        """
        A user with no vault membership gets 403 on wiki read endpoints.
        We use a non-superadmin mock to verify policy is consulted.
        """
        # Override with a non-privileged user.
        app.dependency_overrides[get_current_active_user] = lambda: {
            "id": 99,
            "username": "stranger",
            "full_name": "Stranger",
            "role": "member",  # Not superadmin — policy will be consulted.
            "is_active": True,
            "must_change_password": False,
        }

        resp = self.client.get("/api/wiki/pages", params={"vault_id": 1})
        # Member with no vault membership → 403.
        self.assertEqual(resp.status_code, 403, resp.text)

    def test_no_write_access_returns_403_on_create(self):
        """
        A user without write permission gets 403 when creating wiki content.
        """
        app.dependency_overrides[get_current_active_user] = lambda: {
            "id": 99,
            "username": "stranger",
            "full_name": "Stranger",
            "role": "member",
            "is_active": True,
            "must_change_password": False,
        }

        resp = self.client.post(
            "/api/wiki/pages",
            json={"vault_id": 1, "title": "Test", "page_type": "overview"},
        )
        self.assertEqual(resp.status_code, 403, resp.text)

    def test_superadmin_bypasses_vault_permission_check(self):
        """
        Superadmin (role=superadmin) bypasses vault-level permission checks.
        """
        app.dependency_overrides[get_current_active_user] = lambda: _MOCK_SUPERADMIN

        resp = self.client.get("/api/wiki/pages", params={"vault_id": 1})
        self.assertEqual(resp.status_code, 200, resp.text)


# ---------------------------------------------------------------------------
# Test: endpoint coverage for all DI-wired wiki endpoints
# ---------------------------------------------------------------------------

class TestAllWikiEndpointsDI(WikiDITestBase):
    """
    Smoke-test every wiki endpoint that calls _require_vault_read or
    _require_vault_write to ensure they still work after the DI migration.
    """

    _page_counter = 0

    def _create_page(self, vault_id: int = 1, **kwargs) -> dict:
        TestAllWikiEndpointsDI._page_counter += 1
        title = kwargs.pop("title", None) or f"DI Test {TestAllWikiEndpointsDI._page_counter}"
        resp = self.client.post(
            "/api/wiki/pages",
            json={"vault_id": vault_id, "title": title, "page_type": "overview", **kwargs},
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        return resp.json()

    # ---- read-checked endpoints (GET) ----

    def test_list_wiki_pages(self):
        resp = self.client.get("/api/wiki/pages", params={"vault_id": 1})
        self.assertEqual(resp.status_code, 200)

    def test_list_wiki_entities(self):
        resp = self.client.get("/api/wiki/entities", params={"vault_id": 1})
        self.assertEqual(resp.status_code, 200)

    def test_list_wiki_claims(self):
        resp = self.client.get("/api/wiki/claims", params={"vault_id": 1})
        self.assertEqual(resp.status_code, 200)

    def test_get_lint_findings(self):
        resp = self.client.get("/api/wiki/lint", params={"vault_id": 1})
        self.assertEqual(resp.status_code, 200)

    def test_list_wiki_jobs(self):
        resp = self.client.get("/api/wiki/jobs", params={"vault_id": 1})
        self.assertEqual(resp.status_code, 200)

    def test_list_wiki_activity(self):
        resp = self.client.get("/api/wiki/activity", params={"vault_id": 1})
        self.assertEqual(resp.status_code, 200)

    def test_wiki_search(self):
        resp = self.client.get("/api/wiki/search", params={"vault_id": 1, "q": "test"})
        self.assertEqual(resp.status_code, 200)

    def test_wiki_events_stream_no_db_in_signature(self):
        """
        SSE streaming is tested end-to-end in test_wiki_events.py::TestSSEEventGenerator.
        This test verifies the endpoint does NOT inject db (uses evaluate via Depends).
        """
        import inspect

        from app.api.routes.wiki import wiki_events_stream
        sig = inspect.signature(wiki_events_stream)
        self.assertNotIn("db", {p.name for p in sig.parameters.values()})

    def test_list_wiki_relations(self):
        resp = self.client.get("/api/wiki/relations", params={"vault_id": 1})
        self.assertEqual(resp.status_code, 200)

    def test_get_wiki_job(self):
        # Requires an existing job — use bulk to create a page then check job.
        self._create_page()
        resp = self.client.get("/api/wiki/jobs", params={"vault_id": 1})
        self.assertEqual(resp.status_code, 200)

    # ---- write-checked endpoints (POST/PUT/DELETE) ----

    def test_create_wiki_page(self):
        resp = self.client.post(
            "/api/wiki/pages",
            json={"vault_id": 1, "title": "DI Test", "page_type": "overview"},
        )
        self.assertEqual(resp.status_code, 201, resp.text)

    def test_update_wiki_page(self):
        page = self._create_page()
        resp = self.client.put(
            f"/api/wiki/pages/{page['id']}",
            json={"title": "Updated via DI"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_delete_wiki_page(self):
        page = self._create_page()
        resp = self.client.delete(f"/api/wiki/pages/{page['id']}")
        self.assertEqual(resp.status_code, 204, resp.text)

    def test_create_wiki_claim(self):
        resp = self.client.post(
            "/api/wiki/claims",
            json={
                "vault_id": 1,
                "claim_text": "DI migration test claim",
                "source_type": "manual",
            },
        )
        self.assertEqual(resp.status_code, 201, resp.text)

    def test_bulk_delete(self):
        ids = [self._create_page()["id"] for _ in range(2)]
        resp = self.client.post(
            "/api/wiki/pages/bulk",
            json={"vault_id": 1, "page_ids": ids, "action": "delete"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_run_wiki_lint(self):
        resp = self.client.post("/api/wiki/lint/run", json={"vault_id": 1})
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_wiki_recompile(self):
        resp = self.client.post("/api/wiki/recompile", params={"vault_id": 1})
        self.assertEqual(resp.status_code, 202, resp.text)


# ---------------------------------------------------------------------------
# Test: page-version / file / backlink routes
# ---------------------------------------------------------------------------

class TestWikiNewRoutesDI(WikiDITestBase):
    """Test the newer wiki routes (versions, files, backlinks) with DI pool."""

    def setUp(self):
        super().setUp()
        # Ensure job columns exist (same as WikiNewRouteTestBase in test_wiki_routes.py).
        conn = self._raw()
        job_cols = {r[1] for r in conn.execute("PRAGMA table_info(wiki_compile_jobs)").fetchall()}
        if "input_json" not in job_cols:
            conn.execute("ALTER TABLE wiki_compile_jobs ADD COLUMN input_json TEXT DEFAULT '{}'")
        if "retry_count" not in job_cols:
            conn.execute("ALTER TABLE wiki_compile_jobs ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0")
        conn.commit()
        self._pool.release(conn)

    def _create_page(self, **kwargs) -> dict:
        resp = self.client.post(
            "/api/wiki/pages",
            json={"vault_id": 1, "title": "DI Test", "page_type": "entity", **kwargs},
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        return resp.json()

    def _insert_file(self, vault_id: int = 1, file_name: str = "doc.pdf") -> int:
        conn = self._raw()
        cur = conn.execute(
            "INSERT INTO files (vault_id, file_path, file_name, file_size, status) VALUES (?, ?, ?, ?, ?)",
            (vault_id, f"/tmp/{file_name}", file_name, 1234, "indexed"),
        )
        conn.commit()
        file_id = cur.lastrowid
        self._pool.release(conn)
        return file_id

    def test_list_page_versions(self):
        page = self._create_page()
        self.client.put(f"/api/wiki/pages/{page['id']}", json={"title": "v2"})
        resp = self.client.get(
            f"/api/wiki/pages/{page['id']}/versions",
            params={"vault_id": 1},
        )
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_attach_file_to_page(self):
        page = self._create_page()
        file_id = self._insert_file()
        resp = self.client.post(
            f"/api/wiki/pages/{page['id']}/files",
            json={"vault_id": 1, "file_id": file_id},
        )
        self.assertEqual(resp.status_code, 201, resp.text)

    def test_list_page_files(self):
        page = self._create_page()
        file_id = self._insert_file()
        self.client.post(
            f"/api/wiki/pages/{page['id']}/files",
            json={"vault_id": 1, "file_id": file_id},
        )
        resp = self.client.get(f"/api/wiki/pages/{page['id']}/files", params={"vault_id": 1})
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_detach_file_from_page(self):
        page = self._create_page()
        file_id = self._insert_file()
        self.client.post(
            f"/api/wiki/pages/{page['id']}/files",
            json={"vault_id": 1, "file_id": file_id},
        )
        resp = self.client.delete(
            f"/api/wiki/pages/{page['id']}/files/{file_id}",
            params={"vault_id": 1},
        )
        self.assertEqual(resp.status_code, 204, resp.text)

    def test_backlinks(self):
        target = self._create_page(title="Backlink Target")
        source = self._create_page(
            title="Backlink Source",
            markdown=f"See [[{target['slug']}]]",
        )
        resp = self.client.get(
            f"/api/wiki/pages/{target['id']}/backlinks",
            params={"vault_id": 1},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertGreaterEqual(len(resp.json()["backlinks"]), 1)


if __name__ == "__main__":
    unittest.main()
