"""Tests for Wiki API routes — DTO shapes and end-to-end wiring."""

import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from queue import Empty, Queue

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Stub optional heavy dependencies
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

from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from app.api.deps import get_current_active_user, get_db, get_vector_store
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


class _SimplePool:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._pool: Queue = Queue(maxsize=5)
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


class WikiRouteTestBase(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self._temp_dir = tempfile.mkdtemp()

        # Store original settings
        self._original_data_dir = settings.data_dir

        # Point settings at our temp dir so evaluate_policy pool uses the right db
        settings.data_dir = Path(self._temp_dir)

        # DB path = settings.sqlite_path = temp_dir/app.db
        db_path = str(Path(self._temp_dir) / "app.db")

        # Clear the pool cache so a fresh pool is created for this test's db
        from app.models.database import _pool_cache, _pool_cache_lock
        with _pool_cache_lock:
            for _p in list(_pool_cache.values()):
                _p.close_all()
            _pool_cache.clear()

        from app.models.database import init_db
        init_db(db_path)

        self._pool = _SimplePool(db_path)
        self._db_path = db_path

        # Seed default vault
        conn = self._pool.get()
        conn.execute("INSERT OR IGNORE INTO vaults (id, name) VALUES (1, 'Default')")
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

        # Override auth dependency to skip real authentication
        app.dependency_overrides[get_current_active_user] = lambda: _MOCK_SUPERADMIN
        app.dependency_overrides[get_db] = _get_db_override
        app.dependency_overrides[get_vector_store] = lambda: mock_vs
        # Override CSRF protection for ordinary CRUD tests
        app.dependency_overrides[csrf_protect] = lambda: "test-csrf-token"

    def tearDown(self):
        app.dependency_overrides.pop(get_current_active_user, None)
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_vector_store, None)
        app.dependency_overrides.pop(csrf_protect, None)
        self._pool.close_all()

        # Restore settings and clear pool cache
        settings.data_dir = self._original_data_dir
        from app.models.database import _pool_cache, _pool_cache_lock
        with _pool_cache_lock:
            for _p in list(_pool_cache.values()):
                _p.close_all()
            _pool_cache.clear()

        import shutil
        shutil.rmtree(self._temp_dir, ignore_errors=True)

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------
    def _raw(self) -> sqlite3.Connection:
        return self._pool.get()

    def _insert_memory(self, content: str, vault_id: int = 1) -> int:
        conn = self._raw()
        cur = conn.execute(
            "INSERT INTO memories (content, vault_id) VALUES (?, ?)",
            (content, vault_id),
        )
        conn.commit()
        mem_id = cur.lastrowid
        self._pool.release(conn)
        return mem_id


class TestWikiPageRoutes(WikiRouteTestBase):

    def test_list_pages_empty(self):
        resp = self.client.get("/api/wiki/pages", params={"vault_id": 1})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("pages", data)
        self.assertEqual(data["pages"], [])

    def test_create_page_returns_201_with_dto_shape(self):
        resp = self.client.post(
            "/api/wiki/pages",
            json={
                "vault_id": 1,
                "title": "AFOMIS Overview",
                "page_type": "overview",
                "markdown": "# AFOMIS\nTest content.",
            },
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        data = resp.json()
        self.assertEqual(data["title"], "AFOMIS Overview")
        self.assertEqual(data["page_type"], "overview")
        self.assertIn("slug", data)
        self.assertIn("afomis", data["slug"])
        self.assertIn("status", data)
        self.assertIn("id", data)
        self.assertEqual(data["vault_id"], 1)

    def test_get_page_returns_correct_fields(self):
        create_resp = self.client.post(
            "/api/wiki/pages",
            json={"vault_id": 1, "title": "Test Page", "page_type": "entity"},
        )
        page_id = create_resp.json()["id"]

        resp = self.client.get(f"/api/wiki/pages/{page_id}")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["id"], page_id)
        self.assertEqual(data["title"], "Test Page")
        self.assertIn("claims", data)
        self.assertIn("entities", data)

    def test_get_nonexistent_page_returns_404(self):
        resp = self.client.get("/api/wiki/pages/99999")
        self.assertEqual(resp.status_code, 404)

    def test_update_page(self):
        create_resp = self.client.post(
            "/api/wiki/pages",
            json={"vault_id": 1, "title": "Old Title", "page_type": "entity"},
        )
        page_id = create_resp.json()["id"]

        resp = self.client.put(
            f"/api/wiki/pages/{page_id}",
            json={"title": "New Title", "status": "verified"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["title"], "New Title")
        self.assertEqual(data["status"], "verified")

    def test_delete_page_returns_204(self):
        create_resp = self.client.post(
            "/api/wiki/pages",
            json={"vault_id": 1, "title": "To Delete", "page_type": "entity"},
        )
        page_id = create_resp.json()["id"]

        resp = self.client.delete(f"/api/wiki/pages/{page_id}")
        self.assertEqual(resp.status_code, 204)

        # Subsequent GET must 404
        resp2 = self.client.get(f"/api/wiki/pages/{page_id}")
        self.assertEqual(resp2.status_code, 404)

    def test_list_pages_filter_by_type(self):
        self.client.post(
            "/api/wiki/pages",
            json={"vault_id": 1, "title": "Entity Page", "page_type": "entity"},
        )
        self.client.post(
            "/api/wiki/pages",
            json={"vault_id": 1, "title": "Acronym Page", "page_type": "acronym"},
        )

        resp = self.client.get("/api/wiki/pages", params={"vault_id": 1, "page_type": "entity"})
        data = resp.json()
        self.assertEqual(len(data["pages"]), 1)
        self.assertEqual(data["pages"][0]["page_type"], "entity")


class TestWikiClaimRoutes(WikiRouteTestBase):

    def test_list_claims_empty(self):
        resp = self.client.get("/api/wiki/claims", params={"vault_id": 1})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("claims", resp.json())

    def test_create_claim_returns_201(self):
        resp = self.client.post(
            "/api/wiki/claims",
            json={
                "vault_id": 1,
                "claim_text": "Justice Sakyi is the AFOMIS Chief",
                "source_type": "manual",
                "subject": "Justice Sakyi",
                "predicate": "chief",
                "object": "AFOMIS",
            },
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        data = resp.json()
        self.assertEqual(data["claim_text"], "Justice Sakyi is the AFOMIS Chief")
        self.assertIn("id", data)
        self.assertIn("sources", data)

    def test_update_claim(self):
        create_resp = self.client.post(
            "/api/wiki/claims",
            json={"vault_id": 1, "claim_text": "Original", "source_type": "manual"},
        )
        claim_id = create_resp.json()["id"]

        resp = self.client.put(
            f"/api/wiki/claims/{claim_id}",
            json={"claim_text": "Updated", "status": "superseded"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["claim_text"], "Updated")
        self.assertEqual(data["status"], "superseded")

    def test_delete_claim_returns_204(self):
        create_resp = self.client.post(
            "/api/wiki/claims",
            json={"vault_id": 1, "claim_text": "Temp", "source_type": "manual"},
        )
        claim_id = create_resp.json()["id"]

        resp = self.client.delete(f"/api/wiki/claims/{claim_id}")
        self.assertEqual(resp.status_code, 204)


class TestWikiEntityRoutes(WikiRouteTestBase):

    def test_list_entities_empty(self):
        resp = self.client.get("/api/wiki/entities", params={"vault_id": 1})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("entities", resp.json())


class TestWikiLintRoutes(WikiRouteTestBase):

    def test_get_lint_findings_empty(self):
        resp = self.client.get("/api/wiki/lint", params={"vault_id": 1})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("findings", resp.json())

    def test_run_lint_returns_findings_list(self):
        resp = self.client.post("/api/wiki/lint/run", json={"vault_id": 1})
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertIn("findings", data)
        self.assertIn("count", data)
        self.assertIsInstance(data["count"], int)

    def test_run_lint_detects_unsupported_claim(self):
        self.client.post(
            "/api/wiki/claims",
            json={"vault_id": 1, "claim_text": "Claim with no sources", "source_type": "manual"},
        )
        resp = self.client.post("/api/wiki/lint/run", json={"vault_id": 1})
        data = resp.json()
        types_found = {f["finding_type"] for f in data["findings"]}
        self.assertIn("unsupported_claim", types_found)


class TestWikiSearchRoute(WikiRouteTestBase):

    def test_search_returns_structure(self):
        self.client.post(
            "/api/wiki/pages",
            json={"vault_id": 1, "title": "AFOMIS Entity", "page_type": "entity"},
        )
        resp = self.client.get("/api/wiki/search", params={"vault_id": 1, "q": "AFOMIS"})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("pages", data)
        self.assertIn("claims", data)
        self.assertIn("entities", data)
        self.assertIn("query", data)

    def test_search_no_results(self):
        resp = self.client.get("/api/wiki/search", params={"vault_id": 1, "q": "xyznonexistent"})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["pages"], [])
        self.assertEqual(data["claims"], [])
        self.assertEqual(data["entities"], [])


class TestWikiJobsRoute(WikiRouteTestBase):

    def test_list_jobs_empty(self):
        resp = self.client.get("/api/wiki/jobs", params={"vault_id": 1})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("jobs", resp.json())


class TestWikiPromoteMemoryRoute(WikiRouteTestBase):

    AFOMIS_TEXT = (
        "AFOMIS stands for Air Force Operational Medicine Information Systems. "
        "Justice Sakyi is the AFOMIS Chief and Major Justin Woods is his deputy."
    )

    def test_promote_memory_returns_page_claims_entities_relations(self):
        mem_id = self._insert_memory(self.AFOMIS_TEXT)
        resp = self.client.post(
            "/api/wiki/promote-memory",
            json={"memory_id": mem_id, "vault_id": 1},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertIn("page", data)
        self.assertIn("claims", data)
        self.assertIn("entities", data)
        self.assertIn("relations", data)

    def test_promote_memory_page_has_required_fields(self):
        mem_id = self._insert_memory(self.AFOMIS_TEXT)
        resp = self.client.post(
            "/api/wiki/promote-memory",
            json={"memory_id": mem_id, "vault_id": 1},
        )
        page = resp.json()["page"]
        self.assertIn("id", page)
        self.assertIn("title", page)
        self.assertIn("slug", page)
        self.assertIn("vault_id", page)

    def test_promote_memory_creates_afomis_entity(self):
        mem_id = self._insert_memory(self.AFOMIS_TEXT)
        resp = self.client.post(
            "/api/wiki/promote-memory",
            json={"memory_id": mem_id, "vault_id": 1},
        )
        entity_names = {e["canonical_name"] for e in resp.json()["entities"]}
        self.assertIn("AFOMIS", entity_names)

    def test_promote_memory_claims_have_sources(self):
        mem_id = self._insert_memory(self.AFOMIS_TEXT)
        resp = self.client.post(
            "/api/wiki/promote-memory",
            json={"memory_id": mem_id, "vault_id": 1},
        )
        for claim in resp.json()["claims"]:
            self.assertGreater(len(claim["sources"]), 0, f"Claim has no sources: {claim}")
            self.assertEqual(claim["sources"][0]["source_kind"], "memory")
            self.assertEqual(claim["sources"][0]["memory_id"], mem_id)

    def test_promote_nonexistent_memory_returns_404(self):
        resp = self.client.post(
            "/api/wiki/promote-memory",
            json={"memory_id": 99999, "vault_id": 1},
        )
        self.assertEqual(resp.status_code, 404)

    def test_promote_wrong_vault_returns_403(self):
        conn = self._raw()
        conn.execute("INSERT OR IGNORE INTO vaults (id, name) VALUES (2, 'Other')")
        conn.commit()
        self._pool.release(conn)

        mem_id = self._insert_memory(self.AFOMIS_TEXT, vault_id=2)
        resp = self.client.post(
            "/api/wiki/promote-memory",
            json={"memory_id": mem_id, "vault_id": 1},
        )
        self.assertEqual(resp.status_code, 403)


# ---------------------------------------------------------------------------
# Test: wiki_enabled master switch
# ---------------------------------------------------------------------------


class TestWikiEnabledSwitch(WikiRouteTestBase):
    """Tests for the wiki_enabled master switch (503 when disabled)."""

    def setUp(self):
        super().setUp()
        self._original_wiki_enabled = settings.wiki_enabled
        settings.wiki_enabled = True  # Default to True so normal tests pass

    def tearDown(self):
        settings.wiki_enabled = self._original_wiki_enabled
        super().tearDown()

    # ---- GET endpoints ----

    def test_wiki_disabled_list_pages_returns_503(self):
        """GET /api/wiki/pages returns 503 when wiki_enabled=False."""
        settings.wiki_enabled = False
        response = self.client.get("/api/wiki/pages", params={"vault_id": 1})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"], "Wiki subsystem is disabled")

    def test_wiki_disabled_get_page_returns_503(self):
        """GET /api/wiki/pages/{id} returns 503 when wiki_enabled=False."""
        settings.wiki_enabled = False
        response = self.client.get("/api/wiki/pages/1")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"], "Wiki subsystem is disabled")

    def test_wiki_disabled_list_entities_returns_503(self):
        """GET /api/wiki/entities returns 503 when wiki_enabled=False."""
        settings.wiki_enabled = False
        response = self.client.get("/api/wiki/entities", params={"vault_id": 1})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"], "Wiki subsystem is disabled")

    def test_wiki_disabled_list_claims_returns_503(self):
        """GET /api/wiki/claims returns 503 when wiki_enabled=False."""
        settings.wiki_enabled = False
        response = self.client.get("/api/wiki/claims", params={"vault_id": 1})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"], "Wiki subsystem is disabled")

    def test_wiki_disabled_get_lint_returns_503(self):
        """GET /api/wiki/lint returns 503 when wiki_enabled=False."""
        settings.wiki_enabled = False
        response = self.client.get("/api/wiki/lint", params={"vault_id": 1})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"], "Wiki subsystem is disabled")

    def test_wiki_disabled_list_jobs_returns_503(self):
        """GET /api/wiki/jobs returns 503 when wiki_enabled=False."""
        settings.wiki_enabled = False
        response = self.client.get("/api/wiki/jobs", params={"vault_id": 1})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"], "Wiki subsystem is disabled")

    def test_wiki_disabled_search_returns_503(self):
        """GET /api/wiki/search returns 503 when wiki_enabled=False."""
        settings.wiki_enabled = False
        response = self.client.get("/api/wiki/search", params={"vault_id": 1, "q": "test"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"], "Wiki subsystem is disabled")

    def test_wiki_disabled_get_job_returns_503(self):
        """GET /api/wiki/jobs/{id} returns 503 when wiki_enabled=False."""
        settings.wiki_enabled = False
        response = self.client.get("/api/wiki/jobs/1", params={"vault_id": 1})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"], "Wiki subsystem is disabled")

    # ---- POST endpoints ----

    def test_wiki_disabled_create_page_returns_503(self):
        """POST /api/wiki/pages returns 503 when wiki_enabled=False."""
        settings.wiki_enabled = False
        response = self.client.post(
            "/api/wiki/pages",
            json={"vault_id": 1, "title": "Test Page", "page_type": "overview"},
        )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"], "Wiki subsystem is disabled")

    def test_wiki_disabled_create_claim_returns_503(self):
        """POST /api/wiki/claims returns 503 when wiki_enabled=False."""
        settings.wiki_enabled = False
        response = self.client.post(
            "/api/wiki/claims",
            json={"vault_id": 1, "claim_text": "Test claim", "source_type": "manual"},
        )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"], "Wiki subsystem is disabled")

    def test_wiki_disabled_run_lint_returns_503(self):
        """POST /api/wiki/lint/run returns 503 when wiki_enabled=False."""
        settings.wiki_enabled = False
        response = self.client.post("/api/wiki/lint/run", json={"vault_id": 1})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"], "Wiki subsystem is disabled")

    def test_wiki_disabled_promote_memory_returns_503(self):
        """POST /api/wiki/promote-memory returns 503 when wiki_enabled=False."""
        settings.wiki_enabled = False
        response = self.client.post(
            "/api/wiki/promote-memory",
            json={"memory_id": 1, "vault_id": 1},
        )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"], "Wiki subsystem is disabled")

    def test_wiki_disabled_retry_job_returns_503(self):
        """POST /api/wiki/jobs/{id}/retry returns 503 when wiki_enabled=False."""
        settings.wiki_enabled = False
        response = self.client.post("/api/wiki/jobs/1/retry", params={"vault_id": 1})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"], "Wiki subsystem is disabled")

    def test_wiki_disabled_cancel_job_returns_503(self):
        """POST /api/wiki/jobs/{id}/cancel returns 503 when wiki_enabled=False."""
        settings.wiki_enabled = False
        response = self.client.post("/api/wiki/jobs/1/cancel", params={"vault_id": 1})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"], "Wiki subsystem is disabled")

    def test_wiki_disabled_compile_document_returns_503(self):
        """POST /api/wiki/documents/{id}/compile returns 503 when wiki_enabled=False."""
        settings.wiki_enabled = False
        response = self.client.post("/api/wiki/documents/1/compile", params={"vault_id": 1})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"], "Wiki subsystem is disabled")

    def test_wiki_disabled_recompile_returns_503(self):
        """POST /api/wiki/recompile returns 503 when wiki_enabled=False."""
        settings.wiki_enabled = False
        response = self.client.post("/api/wiki/recompile", params={"vault_id": 1})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"], "Wiki subsystem is disabled")

    # ---- PUT endpoints ----

    def test_wiki_disabled_update_page_returns_503(self):
        """PUT /api/wiki/pages/{id} returns 503 when wiki_enabled=False."""
        settings.wiki_enabled = False
        response = self.client.put(
            "/api/wiki/pages/1",
            json={"title": "Updated Title"},
        )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"], "Wiki subsystem is disabled")

    def test_wiki_disabled_update_claim_returns_503(self):
        """PUT /api/wiki/claims/{id} returns 503 when wiki_enabled=False."""
        settings.wiki_enabled = False
        response = self.client.put(
            "/api/wiki/claims/1",
            json={"claim_text": "Updated claim"},
        )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"], "Wiki subsystem is disabled")

    # ---- DELETE endpoints ----

    def test_wiki_disabled_delete_page_returns_503(self):
        """DELETE /api/wiki/pages/{id} returns 503 when wiki_enabled=False."""
        settings.wiki_enabled = False
        response = self.client.delete("/api/wiki/pages/1")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"], "Wiki subsystem is disabled")

    def test_wiki_disabled_delete_claim_returns_503(self):
        """DELETE /api/wiki/claims/{id} returns 503 when wiki_enabled=False."""
        settings.wiki_enabled = False
        response = self.client.delete("/api/wiki/claims/1")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"], "Wiki subsystem is disabled")

    # ---- Enabled = True: normal operation proceeds ----

    def test_wiki_enabled_list_pages_succeeds(self):
        """GET /api/wiki/pages returns 200 when wiki_enabled=True."""
        settings.wiki_enabled = True
        response = self.client.get("/api/wiki/pages", params={"vault_id": 1})
        self.assertEqual(response.status_code, 200)


# ---------------------------------------------------------------------------
# Test: CSRF protection on wiki mutating routes
# ---------------------------------------------------------------------------


class TestWikiCSRFProtection(WikiRouteTestBase):
    """Tests for CSRF protection on wiki write endpoints.

    These tests verify that write endpoints return 403 when no valid CSRF token
    is provided. The CSRF token must be in both the cookie and the X-CSRF-Token header.

    These tests set up a proper CSRF manager so that the CSRF check is actually reached,
    allowing us to test the 403 response for missing/invalid tokens.
    """

    def setUp(self):
        super().setUp()
        # IMPORTANT: Remove the csrf_protect override from base class setUp so
        # that the real csrf_protect runs and we can test 403 behavior
        app.dependency_overrides.pop(csrf_protect, None)
        # Set up a mock CSRF manager on app.state so csrf_protect doesn't fail
        # with 503 (CSRF service unavailable). We want to test 403 (CSRF token
        # missing/mismatch), not 503.
        class MockCSRFManager:
            def validate_token(self, token):
                # Always return True so we can test the token mismatch/cookie check
                return True

        app.state.csrf_manager = MockCSRFManager()

    def tearDown(self):
        # Clean up csrf_protect override and mock CSRF manager
        app.dependency_overrides.pop(csrf_protect, None)
        if hasattr(app.state, "csrf_manager"):
            delattr(app.state, "csrf_manager")
        super().tearDown()

    def _create_page_with_csrf(self):
        """Helper to create a page with CSRF override."""
        app.dependency_overrides[csrf_protect] = lambda: "test-csrf-token"
        resp = self.client.post(
            "/api/wiki/pages",
            json={"vault_id": 1, "title": "Test Page", "page_type": "overview"},
        )
        app.dependency_overrides.pop(csrf_protect, None)
        return resp

    def _create_claim_with_csrf(self):
        """Helper to create a claim with CSRF override."""
        app.dependency_overrides[csrf_protect] = lambda: "test-csrf-token"
        resp = self.client.post(
            "/api/wiki/claims",
            json={"vault_id": 1, "claim_text": "Test claim", "source_type": "manual"},
        )
        app.dependency_overrides.pop(csrf_protect, None)
        return resp

    # ---- POST /wiki/pages ----

    def test_create_page_without_csrf_returns_403(self):
        """POST /api/wiki/pages without CSRF token returns 403."""
        response = self.client.post(
            "/api/wiki/pages",
            json={"vault_id": 1, "title": "Test Page", "page_type": "overview"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("csrf", response.json()["detail"].lower())

    # ---- PUT /wiki/pages/{page_id} ----

    def test_update_page_without_csrf_returns_403(self):
        """PUT /api/wiki/pages/{id} without CSRF token returns 403."""
        # First create a page with CSRF override
        create_resp = self._create_page_with_csrf()
        self.assertEqual(create_resp.status_code, 201)
        page_id = create_resp.json()["id"]

        # Now update without CSRF - should get 403
        response = self.client.put(
            f"/api/wiki/pages/{page_id}",
            json={"title": "Updated Title"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("csrf", response.json()["detail"].lower())

    # ---- DELETE /wiki/pages/{page_id} ----

    def test_delete_page_without_csrf_returns_403(self):
        """DELETE /api/wiki/pages/{id} without CSRF token returns 403."""
        # First create a page with CSRF override
        create_resp = self._create_page_with_csrf()
        self.assertEqual(create_resp.status_code, 201)
        page_id = create_resp.json()["id"]

        response = self.client.delete(f"/api/wiki/pages/{page_id}")
        self.assertEqual(response.status_code, 403)
        self.assertIn("csrf", response.json()["detail"].lower())

    # ---- POST /wiki/claims ----

    def test_create_claim_without_csrf_returns_403(self):
        """POST /api/wiki/claims without CSRF token returns 403."""
        response = self.client.post(
            "/api/wiki/claims",
            json={
                "vault_id": 1,
                "claim_text": "Test claim",
                "source_type": "manual",
            },
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("csrf", response.json()["detail"].lower())

    # ---- PUT /wiki/claims/{claim_id} ----

    def test_update_claim_without_csrf_returns_403(self):
        """PUT /api/wiki/claims/{id} without CSRF token returns 403."""
        # First create a claim with CSRF override
        create_resp = self._create_claim_with_csrf()
        self.assertEqual(create_resp.status_code, 201)
        claim_id = create_resp.json()["id"]

        response = self.client.put(
            f"/api/wiki/claims/{claim_id}",
            json={"claim_text": "Updated"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("csrf", response.json()["detail"].lower())

    # ---- DELETE /wiki/claims/{claim_id} ----

    def test_delete_claim_without_csrf_returns_403(self):
        """DELETE /api/wiki/claims/{id} without CSRF token returns 403."""
        # First create a claim with CSRF override
        create_resp = self._create_claim_with_csrf()
        self.assertEqual(create_resp.status_code, 201)
        claim_id = create_resp.json()["id"]

        response = self.client.delete(f"/api/wiki/claims/{claim_id}")
        self.assertEqual(response.status_code, 403)
        self.assertIn("csrf", response.json()["detail"].lower())

    # ---- POST /wiki/lint/run ----

    def test_run_lint_without_csrf_returns_403(self):
        """POST /api/wiki/lint/run without CSRF token returns 403."""
        response = self.client.post(
            "/api/wiki/lint/run",
            json={"vault_id": 1},
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("csrf", response.json()["detail"].lower())

    # ---- POST /wiki/promote-memory ----

    def test_promote_memory_without_csrf_returns_403(self):
        """POST /api/wiki/promote-memory without CSRF token returns 403."""
        mem_id = self._insert_memory("Some memory content")
        response = self.client.post(
            "/api/wiki/promote-memory",
            json={"memory_id": mem_id, "vault_id": 1},
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("csrf", response.json()["detail"].lower())

    # ---- POST /wiki/recompile ----

    def test_recompile_without_csrf_returns_403(self):
        """POST /api/wiki/recompile without CSRF token returns 403."""
        response = self.client.post("/api/wiki/recompile", params={"vault_id": 1})
        self.assertEqual(response.status_code, 403)
        self.assertIn("csrf", response.json()["detail"].lower())

    # ---- GET routes should still work without CSRF ----

    def test_list_pages_without_csrf_succeeds(self):
        """GET /api/wiki/pages does NOT require CSRF and returns 200."""
        response = self.client.get("/api/wiki/pages", params={"vault_id": 1})
        self.assertEqual(response.status_code, 200)

    def test_get_page_without_csrf_succeeds(self):
        """GET /api/wiki/pages/{id} does NOT require CSRF and returns 200 or 404."""
        # Create a page first with CSRF
        create_resp = self._create_page_with_csrf()
        self.assertEqual(create_resp.status_code, 201)
        page_id = create_resp.json()["id"]

        # GET should work without CSRF
        response = self.client.get(f"/api/wiki/pages/{page_id}")
        self.assertEqual(response.status_code, 200)

    def test_list_claims_without_csrf_succeeds(self):
        """GET /api/wiki/claims does NOT require CSRF and returns 200."""
        response = self.client.get("/api/wiki/claims", params={"vault_id": 1})
        self.assertEqual(response.status_code, 200)

    # ---- Mutating routes work correctly with valid CSRF token ----

    def test_create_page_with_csrf_succeeds(self):
        """POST /api/wiki/pages with CSRF override succeeds."""
        response = self._create_page_with_csrf()
        self.assertEqual(response.status_code, 201)

    def test_update_page_with_csrf_succeeds(self):
        """PUT /api/wiki/pages/{id} with CSRF override succeeds."""
        create_resp = self._create_page_with_csrf()
        self.assertEqual(create_resp.status_code, 201)
        page_id = create_resp.json()["id"]

        app.dependency_overrides[csrf_protect] = lambda: "test-csrf-token"
        response = self.client.put(
            f"/api/wiki/pages/{page_id}",
            json={"title": "Updated Title"},
        )
        app.dependency_overrides.pop(csrf_protect, None)
        self.assertEqual(response.status_code, 200)

    def test_delete_page_with_csrf_succeeds(self):
        """DELETE /api/wiki/pages/{id} with CSRF override succeeds."""
        create_resp = self._create_page_with_csrf()
        self.assertEqual(create_resp.status_code, 201)
        page_id = create_resp.json()["id"]

        app.dependency_overrides[csrf_protect] = lambda: "test-csrf-token"
        response = self.client.delete(f"/api/wiki/pages/{page_id}")
        app.dependency_overrides.pop(csrf_protect, None)
        self.assertEqual(response.status_code, 204)

    # ---- Job management routes with CSRF ----
    # These require seeding a wiki_job directly, which needs proper DB schema.
    # Skipping for now as they test the same CSRF pattern as other routes.

    # ---- Document compile route with CSRF ----
    # Skipping as it requires file seeding with proper schema

    # ---- Lint finding resolve route with CSRF ----
    # Skipping as it requires lint_finding seeding with proper schema
