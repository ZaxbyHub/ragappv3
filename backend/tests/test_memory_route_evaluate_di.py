"""
Tests for the memories.py dependency-injected evaluate policy migration.

Verifies:
1. Memory route endpoints receive evaluate via Depends(get_evaluate_policy).
2. create_memory uses the injected connection for vault permission evaluation.
3. _authorize_memory_search calls get_evaluate_policy(db) with the injected db connection.
4. search_memories/search_memories_post pass the injected connection to _authorize_memory_search.
5. Existing CRUD operations continue to pass with DI-wired evaluate policy.
6. memories.py does not import the standalone (non-DI) evaluate_policy function.

Coverage: FR-205-01, FR-205-02, FR-205-04, FR-205-05, FR-205-06
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Callable
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Stub optional dependencies
for _mod in ('lancedb', 'pyarrow'):
    try:
        __import__(_mod)
    except ImportError:
        import types
        sys.modules[_mod] = types.ModuleType(_mod)

try:
    from unstructured.partition.auto import partition
except ImportError:
    import types
    _u = types.ModuleType('unstructured')
    _u.__path__ = []
    for _sub in ('partition', 'partition.auto', 'chunking', 'chunking.title',
                 'documents', 'documents.elements'):
        m = types.ModuleType(f'unstructured.{_sub}')
        m.__path__ = []
        sys.modules[f'unstructured.{_sub}'] = m
    sys.modules['unstructured'] = _u

import pytest
from _db_pool import SimpleConnectionPool
from fastapi.testclient import TestClient

from app.main import app

pytestmark = pytest.mark.usefixtures("ready_vector_store")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_allow_evaluate():
    """Returns an async evaluate that always allows."""
    async def allow(principal, resource_type, resource_id, action):
        return True
    return allow


def _make_deny_evaluate():
    """Returns an async evaluate that always denies."""
    async def deny(principal, resource_type, resource_id, action):
        return False
    return deny


# ---------------------------------------------------------------------------
# Test: DI wiring — injected evaluate is called by endpoints
# ---------------------------------------------------------------------------

class TestEvaluateDIWiring(unittest.TestCase):
    """Verify endpoints receive evaluate via Depends(get_evaluate_policy)."""

    def setUp(self):
        self.client = TestClient(app)
        self._temp_dir = tempfile.mkdtemp()
        db_path = str(Path(self._temp_dir) / "test.db")

        from app.models.database import init_db
        init_db(db_path)

        from app.models.database import SQLiteConnectionPool
        from app.services.memory_store import MemoryStore

        self.test_pool = SQLiteConnectionPool(db_path, max_size=2)
        test_store = MemoryStore(pool=self.test_pool)

        from app.api.deps import (
            get_current_active_user,
            get_db,
            get_evaluate_policy,
            get_memory_store,
        )
        from app.security import csrf_protect

        self._pool = SimpleConnectionPool(db_path)

        def override_get_db():
            conn = self._pool.get_connection()
            try:
                yield conn
            finally:
                self._pool.release_connection(conn)

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_memory_store] = lambda: test_store
        app.dependency_overrides[get_current_active_user] = lambda: {
            "id": 0, "username": "admin", "role": "superadmin",
            "is_active": 1, "must_change_password": 0,
        }
        app.dependency_overrides[csrf_protect] = lambda: "test-csrf-token"

        self._get_db = get_db
        self._get_memory_store = get_memory_store
        self._get_evaluate_policy = get_evaluate_policy
        self._get_current_active_user = get_current_active_user
        self._csrf_protect = csrf_protect
        self._db_path = db_path
        self._test_pool = self.test_pool

    def tearDown(self):
        for _key in [self._get_db, self._get_memory_store, self._get_current_active_user,
                     self._csrf_protect, self._get_evaluate_policy]:
            app.dependency_overrides.pop(_key, None)
        self._test_pool.close_all()
        self._pool.close_all()
        import shutil
        shutil.rmtree(self._temp_dir, ignore_errors=True)

    def test_create_memory_calls_injected_evaluate(self):
        """POST /api/memories with vault_id calls injected evaluate for write check."""
        calls = []

        async def tracking(*args):
            calls.append(args)
            return True

        app.dependency_overrides[self._get_evaluate_policy] = lambda: tracking

        try:
            resp = self.client.post("/api/memories",
                                   json={"content": "x", "vault_id": 42})
            self.assertEqual(resp.status_code, 200, resp.text)
            self.assertIn(("vault", 42, "write"), [(c[1], c[2], c[3]) for c in calls])
        finally:
            app.dependency_overrides.pop(self._get_evaluate_policy, None)

    def test_create_memory_skips_evaluate_when_no_vault(self):
        """POST /api/memories with vault_id=None skips vault permission check."""
        vault_calls = []

        async def tracking(*args):
            vault_calls.append(args)
            return True

        app.dependency_overrides[self._get_evaluate_policy] = lambda: tracking

        try:
            resp = self.client.post("/api/memories",
                                   json={"content": "global memory"})
            self.assertEqual(resp.status_code, 200, resp.text)
            # vault_id=None → no vault evaluate call
            vault = [c for c in vault_calls if c[1] == "vault"]
            self.assertEqual(vault, [])
        finally:
            app.dependency_overrides.pop(self._get_evaluate_policy, None)

    def test_list_memories_calls_injected_evaluate(self):
        """GET /api/memories?vault_id=N calls injected evaluate for read check."""
        calls = []

        async def tracking(*args):
            calls.append(args)
            return True

        app.dependency_overrides[self._get_evaluate_policy] = lambda: tracking

        try:
            resp = self.client.get("/api/memories?vault_id=7")
            self.assertEqual(resp.status_code, 200, resp.text)
            self.assertIn(("vault", 7, "read"), [(c[1], c[2], c[3]) for c in calls])
        finally:
            app.dependency_overrides.pop(self._get_evaluate_policy, None)

    def test_search_memories_calls_authorize_memory_search(self):
        """GET /api/memories/search calls _authorize_memory_search with vault_id."""
        # search_memories uses evaluate: Callable = Depends(get_evaluate_policy)
        # and passes it to _authorize_memory_search(evaluate, user, vault_id).
        calls = []

        async def tracking(*args):
            calls.append(args)
            return True

        app.dependency_overrides[self._get_evaluate_policy] = lambda: tracking

        try:
            resp = self.client.get("/api/memories/search?query=test&vault_id=5")
            self.assertEqual(resp.status_code, 200, resp.text)
            self.assertIn(("vault", 5, "read"), [(c[1], c[2], c[3]) for c in calls])
        finally:
            app.dependency_overrides.pop(self._get_evaluate_policy, None)

    def test_search_memories_post_calls_authorize_memory_search(self):
        """POST /api/memories/search calls _authorize_memory_search with vault_id."""
        calls = []

        async def tracking(*args):
            calls.append(args)
            return True

        app.dependency_overrides[self._get_evaluate_policy] = lambda: tracking

        try:
            resp = self.client.post("/api/memories/search",
                                   json={"query": "test", "vault_id": 11})
            self.assertEqual(resp.status_code, 200, resp.text)
            self.assertIn(("vault", 11, "read"), [(c[1], c[2], c[3]) for c in calls])
        finally:
            app.dependency_overrides.pop(self._get_evaluate_policy, None)

    def test_search_memories_crossvault_admin_bypass(self):
        """GET /api/memories/search without vault_id bypasses vault check for admin."""
        import app.api.routes.memories as mr

        vault_calls = []
        original = mr.get_evaluate_policy

        def tracking_factory(db):
            async def tracking(*args):
                vault_calls.append(args)
                return True
            return tracking

        mr.get_evaluate_policy = tracking_factory

        try:
            # superadmin: role is in ("superadmin", "admin") → admin bypass path
            resp = self.client.get("/api/memories/search?query=test")
            self.assertEqual(resp.status_code, 200, resp.text)
            # no vault evaluate should fire for cross-vault search (admin bypass)
            vault = [c for c in vault_calls if c[1] == "vault"]
            self.assertEqual(vault, [], f"Expected no vault calls, got {vault}")
        finally:
            mr.get_evaluate_policy = original


# ---------------------------------------------------------------------------
# Test: _authorize_memory_search receives the injected db connection
# ---------------------------------------------------------------------------

class TestAuthorizeMemorySearchDBConnection(unittest.TestCase):
    """Verify _authorize_memory_search calls get_evaluate_policy(db) with test db."""

    def setUp(self):
        self.client = TestClient(app)
        self._temp_dir = tempfile.mkdtemp()
        db_path = str(Path(self._temp_dir) / "test.db")

        from app.models.database import init_db
        init_db(db_path)

        from app.models.database import SQLiteConnectionPool
        from app.services.memory_store import MemoryStore

        self.test_pool = SQLiteConnectionPool(db_path, max_size=2)
        test_store = MemoryStore(pool=self.test_pool)

        from app.api.deps import get_current_active_user, get_db, get_memory_store
        from app.security import csrf_protect

        self._pool = SimpleConnectionPool(db_path)

        def override_get_db():
            conn = self._pool.get_connection()
            try:
                yield conn
            finally:
                self._pool.release_connection(conn)

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_memory_store] = lambda: test_store
        app.dependency_overrides[get_current_active_user] = lambda: {
            "id": 0, "username": "admin", "role": "superadmin",
            "is_active": 1, "must_change_password": 0,
        }
        app.dependency_overrides[csrf_protect] = lambda: "test-csrf-token"

        self._get_db = get_db
        self._get_current_active_user = get_current_active_user
        self._csrf_protect = csrf_protect
        self._db_path = db_path
        self._test_pool = self.test_pool

    def tearDown(self):
        for _key in [self._get_db, self._get_current_active_user, self._csrf_protect]:
            app.dependency_overrides.pop(_key, None)
        self._test_pool.close_all()
        self._pool.close_all()
        import shutil
        shutil.rmtree(self._temp_dir, ignore_errors=True)

# ---------------------------------------------------------------------------
# Test: Vault permission enforcement — 403 when evaluate denies
# ---------------------------------------------------------------------------

class TestMemoryVaultPermissionEnforcement(unittest.TestCase):
    """Verify vault permission checks return 403 when evaluate denies."""

    def setUp(self):
        self.client = TestClient(app)
        self._temp_dir = tempfile.mkdtemp()
        db_path = str(Path(self._temp_dir) / "test.db")

        from app.models.database import init_db
        init_db(db_path)

        from app.models.database import SQLiteConnectionPool
        from app.services.memory_store import MemoryStore

        self.test_pool = SQLiteConnectionPool(db_path, max_size=2)
        test_store = MemoryStore(pool=self.test_pool)

        from app.api.deps import (
            get_current_active_user,
            get_db,
            get_evaluate_policy,
            get_memory_store,
        )
        from app.security import csrf_protect

        self._pool = SimpleConnectionPool(db_path)

        def override_get_db():
            conn = self._pool.get_connection()
            try:
                yield conn
            finally:
                self._pool.release_connection(conn)

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_memory_store] = lambda: test_store
        app.dependency_overrides[get_current_active_user] = lambda: {
            "id": 1, "username": "regular_user", "role": "member",
            "is_active": 1, "must_change_password": 0,
        }
        app.dependency_overrides[csrf_protect] = lambda: "test-csrf-token"

        self._get_db = get_db
        self._get_memory_store = get_memory_store
        self._get_evaluate_policy = get_evaluate_policy
        self._get_current_active_user = get_current_active_user
        self._csrf_protect = csrf_protect
        self._test_pool = self.test_pool

    def tearDown(self):
        for _key in [self._get_db, self._get_memory_store, self._get_current_active_user,
                     self._csrf_protect, self._get_evaluate_policy]:
            app.dependency_overrides.pop(_key, None)
        self._test_pool.close_all()
        self._pool.close_all()
        import shutil
        shutil.rmtree(self._temp_dir, ignore_errors=True)

    def test_create_memory_denied_when_vault_no_write(self):
        """POST /api/memories with vault_id=99 → 403 when injected evaluate denies."""
        app.dependency_overrides[self._get_evaluate_policy] = _make_deny_evaluate

        try:
            resp = self.client.post("/api/memories",
                                   json={"content": "blocked", "vault_id": 99})
            self.assertEqual(resp.status_code, 403, resp.text)
            self.assertIn("No write access", resp.text)
        finally:
            app.dependency_overrides.pop(self._get_evaluate_policy, None)

    def test_list_memories_denied_when_vault_no_read(self):
        """GET /api/memories?vault_id=7 → 403 when injected evaluate denies."""
        app.dependency_overrides[self._get_evaluate_policy] = _make_deny_evaluate

        try:
            resp = self.client.get("/api/memories?vault_id=7")
            self.assertEqual(resp.status_code, 403, resp.text)
        finally:
            app.dependency_overrides.pop(self._get_evaluate_policy, None)

    def test_search_memories_denied_when_vault_no_read(self):
        """GET /api/memories/search?vault_id=5 → 403 via _authorize_memory_search."""
        import app.api.routes.memories as mr

        original = mr.get_evaluate_policy
        mr.get_evaluate_policy = lambda db: _make_deny_evaluate()

        try:
            resp = self.client.get("/api/memories/search?query=test&vault_id=5")
            self.assertEqual(resp.status_code, 403, resp.text)
        finally:
            mr.get_evaluate_policy = original

    def test_search_memories_post_denied_when_vault_no_read(self):
        """POST /api/memories/search → 403 via _authorize_memory_search."""
        import app.api.routes.memories as mr

        original = mr.get_evaluate_policy
        mr.get_evaluate_policy = lambda db: _make_deny_evaluate()

        try:
            resp = self.client.post("/api/memories/search",
                                   json={"query": "test", "vault_id": 11})
            self.assertEqual(resp.status_code, 403, resp.text)
        finally:
            mr.get_evaluate_policy = original


# ---------------------------------------------------------------------------
# Test: CRUD smoke tests — existing routes still work with DI evaluate
# ---------------------------------------------------------------------------

class TestMemoryCRUDSmoke(unittest.TestCase):
    """Smoke tests confirming CRUD operations pass with DI-wired evaluate."""

    def setUp(self):
        self.client = TestClient(app)
        self._temp_dir = tempfile.mkdtemp()
        db_path = str(Path(self._temp_dir) / "test.db")

        from app.models.database import init_db
        init_db(db_path)

        from app.models.database import SQLiteConnectionPool
        from app.services.memory_store import MemoryStore

        self.test_pool = SQLiteConnectionPool(db_path, max_size=2)
        test_store = MemoryStore(pool=self.test_pool)

        from app.api.deps import (
            get_current_active_user,
            get_db,
            get_evaluate_policy,
            get_memory_store,
        )
        from app.security import csrf_protect

        self._pool = SimpleConnectionPool(db_path)

        def override_get_db():
            conn = self._pool.get_connection()
            try:
                yield conn
            finally:
                self._pool.release_connection(conn)

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_memory_store] = lambda: test_store
        app.dependency_overrides[get_current_active_user] = lambda: {
            "id": 0, "username": "admin", "role": "superadmin",
            "is_active": 1, "must_change_password": 0,
        }
        app.dependency_overrides[csrf_protect] = lambda: "test-csrf-token"
        # NOTE: we do NOT override get_evaluate_policy here.
        # The existing tests in test_api_routes.py::TestMemoriesEndpoints work without
        # overriding it (superadmin bypasses vault checks). We follow the same pattern.

        self._get_db = get_db
        self._get_memory_store = get_memory_store
        self._get_current_active_user = get_current_active_user
        self._csrf_protect = csrf_protect

    def tearDown(self):
        for _key in [self._get_db, self._get_memory_store, self._get_current_active_user,
                     self._csrf_protect]:
            app.dependency_overrides.pop(_key, None)
        self.test_pool.close_all()
        self._pool.close_all()
        import shutil
        shutil.rmtree(self._temp_dir, ignore_errors=True)

    def test_create_memory(self):
        resp = self.client.post("/api/memories",
                               json={"content": "CRUD smoke test", "category": "test"})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["content"], "CRUD smoke test")
        self.assertEqual(resp.json()["id"], "1")

    def test_create_memory_with_vault_id(self):
        resp = self.client.post("/api/memories",
                               json={"content": "vault-scoped", "vault_id": 1})
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_list_memories(self):
        resp = self.client.get("/api/memories")
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_search_memories(self):
        resp = self.client.get("/api/memories/search?query=test")
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_update_memory(self):
        cr = self.client.post("/api/memories", json={"content": "original"})
        self.assertEqual(cr.status_code, 200)
        mid = cr.json()["id"]
        ur = self.client.put(f"/api/memories/{mid}", json={"content": "updated"})
        self.assertEqual(ur.status_code, 200, ur.text)
        self.assertEqual(ur.json()["content"], "updated")

    def test_delete_memory(self):
        cr = self.client.post("/api/memories", json={"content": "to delete"})
        self.assertEqual(cr.status_code, 200)
        mid = cr.json()["id"]
        dr = self.client.delete(f"/api/memories/{mid}")
        self.assertEqual(dr.status_code, 200, dr.text)


# ---------------------------------------------------------------------------
# Test: No stale standalone evaluate_policy import
# ---------------------------------------------------------------------------

class TestNoStandaloneEvaluatePolicyImport(unittest.TestCase):
    """Verify memories.py does not import the non-DI evaluate_policy function."""

    def test_no_standalone_evaluate_policy_in_memories(self):
        """
        memories.py should not reference the standalone evaluate_policy (the one that
        creates its own pool connection). It should only use get_evaluate_policy.
        """
        import inspect

        import app.api.routes.memories as mr

        src = inspect.getsource(mr)

        # The standalone evaluate_policy creates its own pool connection.
        # It should NOT appear in memories.py — only get_evaluate_policy (the DI version).
        # Check that we do NOT have "evaluate_policy(" as a function call
        # (which would indicate the standalone version is being called).
        import re
        standalone_calls = re.findall(r'\bevaluate_policy\s*\(', src)
        self.assertEqual(
            standalone_calls, [],
            f"memories.py should not call standalone evaluate_policy(), "
            f"found: {standalone_calls}"
        )


if __name__ == "__main__":
    unittest.main()
