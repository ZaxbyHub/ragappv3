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


# ===========================================================================
# New endpoint coverage (versions, files, backlinks, activity, bulk,
# entities/claims paging, document/memory status, optimistic lock, F-003,
# F-010). All subclass WikiRouteTestBase and mirror its harness exactly.
# ===========================================================================


class WikiNewRouteTestBase(WikiRouteTestBase):
    """Extends the base harness with a file-insert helper and vault-2 helper.

    The base ``init_db`` applies SCHEMA only; it does NOT run the production
    migrations that add ``wiki_compile_jobs.input_json`` /
    ``wiki_compile_jobs.retry_count`` (those live in ``run_migrations``). The
    ``/compile``, ``/recompile``, and job-status routes call
    ``store.create_job(..., input_json=...)``, so we patch those columns onto
    the harness DB here (test-only fixture parity with production migrations).
    This does NOT modify any source or the shared base class.
    """

    def setUp(self):
        super().setUp()
        conn = self._raw()
        job_cols = {r[1] for r in conn.execute("PRAGMA table_info(wiki_compile_jobs)").fetchall()}
        if "input_json" not in job_cols:
            conn.execute("ALTER TABLE wiki_compile_jobs ADD COLUMN input_json TEXT DEFAULT '{}'")
        if "retry_count" not in job_cols:
            conn.execute("ALTER TABLE wiki_compile_jobs ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0")
        conn.commit()
        self._pool.release(conn)

    def _insert_file(
        self,
        vault_id: int = 1,
        file_name: str = "doc.pdf",
        status: str = "indexed",
    ) -> int:
        conn = self._raw()
        cur = conn.execute(
            """INSERT INTO files (vault_id, file_path, file_name, file_size, status)
               VALUES (?, ?, ?, ?, ?)""",
            (vault_id, f"/tmp/{file_name}", file_name, 1234, status),
        )
        conn.commit()
        file_id = cur.lastrowid
        self._pool.release(conn)
        return file_id

    def _insert_vault2(self) -> None:
        conn = self._raw()
        conn.execute("INSERT OR IGNORE INTO vaults (id, name) VALUES (2, 'Other')")
        conn.commit()
        self._pool.release(conn)

    def _create_page(self, vault_id: int = 1, title: str = "Page", page_type: str = "entity", markdown: str = "") -> dict:
        resp = self.client.post(
            "/api/wiki/pages",
            json={"vault_id": vault_id, "title": title, "page_type": page_type, "markdown": markdown},
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        return resp.json()


class TestWikiPageVersionsRoute(WikiNewRouteTestBase):

    def test_versions_returns_history_after_update(self):
        page = self._create_page(title="Versioned")
        page_id = page["id"]
        # An update snapshots the prior state into wiki_page_versions.
        upd = self.client.put(
            f"/api/wiki/pages/{page_id}",
            json={"title": "Versioned v2", "status": "verified"},
        )
        self.assertEqual(upd.status_code, 200, upd.text)

        resp = self.client.get(f"/api/wiki/pages/{page_id}/versions", params={"vault_id": 1})
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertIn("versions", data)
        self.assertGreaterEqual(len(data["versions"]), 1)
        v = data["versions"][0]
        self.assertEqual(v["page_id"], page_id)
        self.assertEqual(v["vault_id"], 1)
        # The snapshot is the PRE-update title.
        self.assertEqual(v["title"], "Versioned")

    def test_versions_nonexistent_page_returns_404(self):
        resp = self.client.get("/api/wiki/pages/99999/versions", params={"vault_id": 1})
        self.assertEqual(resp.status_code, 404)

    def test_versions_wrong_vault_returns_404(self):
        self._insert_vault2()
        page = self._create_page(vault_id=1, title="In Vault One")
        page_id = page["id"]
        # Page exists but is in vault 1; query as vault 2 -> 404 (scoping).
        resp = self.client.get(f"/api/wiki/pages/{page_id}/versions", params={"vault_id": 2})
        self.assertEqual(resp.status_code, 404)


class TestWikiPageFilesRoute(WikiNewRouteTestBase):

    def test_attach_list_detach_file(self):
        page = self._create_page(title="Has Files")
        page_id = page["id"]
        file_id = self._insert_file(vault_id=1, file_name="attach.pdf")

        # Attach -> 201
        attach = self.client.post(
            f"/api/wiki/pages/{page_id}/files",
            json={"vault_id": 1, "file_id": file_id},
        )
        self.assertEqual(attach.status_code, 201, attach.text)
        pf = attach.json()
        self.assertEqual(pf["page_id"], page_id)
        self.assertEqual(pf["file_id"], file_id)
        self.assertEqual(pf["vault_id"], 1)

        # List -> 200 with the attachment
        listing = self.client.get(f"/api/wiki/pages/{page_id}/files", params={"vault_id": 1})
        self.assertEqual(listing.status_code, 200, listing.text)
        files = listing.json()["files"]
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0]["file_id"], file_id)

        # Detach -> 204
        detach = self.client.delete(
            f"/api/wiki/pages/{page_id}/files/{file_id}", params={"vault_id": 1}
        )
        self.assertEqual(detach.status_code, 204, detach.text)

        # List again -> empty
        listing2 = self.client.get(f"/api/wiki/pages/{page_id}/files", params={"vault_id": 1})
        self.assertEqual(listing2.json()["files"], [])

    def test_duplicate_attach_returns_409(self):
        # A second attach of the same (page, file) pair must return 409.
        # WikiStore.attach_file uses a plain INSERT, so the UNIQUE(page_id,
        # file_id) constraint raises sqlite3.IntegrityError, which the route
        # translates into 409. The first attach still leaves exactly one row.
        page = self._create_page(title="Dup Files")
        page_id = page["id"]
        file_id = self._insert_file(vault_id=1)
        first = self.client.post(
            f"/api/wiki/pages/{page_id}/files",
            json={"vault_id": 1, "file_id": file_id},
        )
        self.assertEqual(first.status_code, 201, first.text)
        dup = self.client.post(
            f"/api/wiki/pages/{page_id}/files",
            json={"vault_id": 1, "file_id": file_id},
        )
        self.assertEqual(dup.status_code, 409, dup.text)
        self.assertIn("already attached", dup.json()["detail"].lower())
        # The conflict left the original single attachment intact.
        listing = self.client.get(f"/api/wiki/pages/{page_id}/files", params={"vault_id": 1})
        self.assertEqual(len(listing.json()["files"]), 1)

    def test_attach_wrong_vault_page_returns_403(self):
        # Page is in vault 1; request claims vault 2 -> handler returns 403.
        self._insert_vault2()
        page = self._create_page(vault_id=1, title="Vault1 Page")
        page_id = page["id"]
        file_id = self._insert_file(vault_id=2)
        resp = self.client.post(
            f"/api/wiki/pages/{page_id}/files",
            json={"vault_id": 2, "file_id": file_id},
        )
        self.assertEqual(resp.status_code, 403, resp.text)
        self.assertIn("vault", resp.json()["detail"].lower())

    def test_detach_nonexistent_attachment_returns_404(self):
        page = self._create_page(title="No Attach")
        page_id = page["id"]
        resp = self.client.delete(
            f"/api/wiki/pages/{page_id}/files/99999", params={"vault_id": 1}
        )
        self.assertEqual(resp.status_code, 404)

    def test_list_files_wrong_vault_returns_404(self):
        self._insert_vault2()
        page = self._create_page(vault_id=1, title="Vault1 Files")
        page_id = page["id"]
        resp = self.client.get(f"/api/wiki/pages/{page_id}/files", params={"vault_id": 2})
        self.assertEqual(resp.status_code, 404)


class TestWikiBacklinksRoute(WikiNewRouteTestBase):

    def test_backlinks_returns_linking_pages(self):
        # DD-C030 end-to-end: creating a page whose body contains [[slug]] must
        # auto-create the link (create_page wires sync_page_links), so the
        # target's backlinks list it without any manual store call.
        target = self._create_page(title="Target Page")
        target_slug = target["slug"]
        target_id = target["id"]
        source = self._create_page(
            title="Source Page",
            markdown=f"See [[{target_slug}]] for details.",
        )
        source_id = source["id"]

        resp = self.client.get(f"/api/wiki/pages/{target_id}/backlinks", params={"vault_id": 1})
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertIn("backlinks", data)
        self.assertEqual(len(data["backlinks"]), 1)
        bl = data["backlinks"][0]
        self.assertEqual(bl["source_page_id"], source_id)
        self.assertEqual(bl["target_page_id"], target_id)

    def test_update_page_markdown_resyncs_backlinks(self):
        # DD-C030 end-to-end: editing a page's body via PUT re-resolves its
        # [[slug]] links (update_page wires sync_page_links). A link added on
        # edit appears; one removed on a later edit disappears.
        target = self._create_page(title="Edit Target")
        target_slug = target["slug"]
        target_id = target["id"]
        source = self._create_page(title="Edit Source", markdown="no links yet")
        source_id = source["id"]

        # Initially no backlinks.
        before = self.client.get(
            f"/api/wiki/pages/{target_id}/backlinks", params={"vault_id": 1}
        )
        self.assertEqual(before.json()["backlinks"], [])

        # Edit the source to reference the target.
        put = self.client.put(
            f"/api/wiki/pages/{source_id}",
            json={"markdown": f"Now see [[{target_slug}]]."},
        )
        self.assertEqual(put.status_code, 200, put.text)
        after = self.client.get(
            f"/api/wiki/pages/{target_id}/backlinks", params={"vault_id": 1}
        )
        self.assertEqual(len(after.json()["backlinks"]), 1)
        self.assertEqual(after.json()["backlinks"][0]["source_page_id"], source_id)

        # Edit again to remove the link -> backlink is dropped.
        put2 = self.client.put(
            f"/api/wiki/pages/{source_id}",
            json={"markdown": "link removed"},
        )
        self.assertEqual(put2.status_code, 200, put2.text)
        final = self.client.get(
            f"/api/wiki/pages/{target_id}/backlinks", params={"vault_id": 1}
        )
        self.assertEqual(final.json()["backlinks"], [])

    def test_backlinks_wrong_vault_returns_404(self):
        # F-002 vault scoping: a page in vault 1 is invisible when queried as vault 2.
        self._insert_vault2()
        page = self._create_page(vault_id=1, title="Scoped Page")
        page_id = page["id"]
        resp = self.client.get(f"/api/wiki/pages/{page_id}/backlinks", params={"vault_id": 2})
        self.assertEqual(resp.status_code, 404)

    def test_backlinks_nonexistent_page_returns_404(self):
        resp = self.client.get("/api/wiki/pages/99999/backlinks", params={"vault_id": 1})
        self.assertEqual(resp.status_code, 404)


class TestWikiActivityRoute(WikiNewRouteTestBase):

    def test_activity_returns_entries_for_vault(self):
        page = self._create_page(title="Activity Page")
        page_id = page["id"]
        # An update logs a page_updated activity entry.
        self.client.put(f"/api/wiki/pages/{page_id}", json={"status": "verified"})

        resp = self.client.get("/api/wiki/activity", params={"vault_id": 1})
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertIn("activity", data)
        self.assertGreaterEqual(len(data["activity"]), 1)
        actions = {e["action"] for e in data["activity"]}
        self.assertIn("page_updated", actions)
        for e in data["activity"]:
            self.assertEqual(e["vault_id"], 1)

    def test_activity_respects_limit(self):
        # Generate several activity entries via repeated updates.
        page = self._create_page(title="Limit Page")
        page_id = page["id"]
        for i in range(5):
            self.client.put(f"/api/wiki/pages/{page_id}", json={"status": "verified", "summary": f"s{i}"})

        resp = self.client.get("/api/wiki/activity", params={"vault_id": 1, "limit": 2})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertLessEqual(len(resp.json()["activity"]), 2)


class TestWikiBulkRoute(WikiNewRouteTestBase):

    def test_bulk_delete_removes_pages(self):
        ids = [self._create_page(title=f"Bulk Del {i}")["id"] for i in range(3)]
        resp = self.client.post(
            "/api/wiki/pages/bulk",
            json={"vault_id": 1, "page_ids": ids, "action": "delete"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertEqual(data["action"], "delete")
        self.assertEqual(data["deleted"], 3)
        for pid in ids:
            self.assertEqual(self.client.get(f"/api/wiki/pages/{pid}").status_code, 404)

    def test_bulk_update_sets_status_on_all(self):
        ids = [self._create_page(title=f"Bulk Upd {i}")["id"] for i in range(3)]
        resp = self.client.post(
            "/api/wiki/pages/bulk",
            json={
                "vault_id": 1,
                "page_ids": ids,
                "action": "update",
                "updates": {"status": "verified"},
            },
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertEqual(data["action"], "update")
        self.assertEqual(data["updated"], 3)
        for pid in ids:
            page = self.client.get(f"/api/wiki/pages/{pid}").json()
            self.assertEqual(page["status"], "verified")

    def test_bulk_update_without_updates_returns_400(self):
        ids = [self._create_page(title="Bulk NoUpdates")["id"]]
        resp = self.client.post(
            "/api/wiki/pages/bulk",
            json={"vault_id": 1, "page_ids": ids, "action": "update"},
        )
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertIn("updates", resp.json()["detail"].lower())

    def test_bulk_unknown_action_returns_400(self):
        ids = [self._create_page(title="Bulk Unknown")["id"]]
        resp = self.client.post(
            "/api/wiki/pages/bulk",
            json={"vault_id": 1, "page_ids": ids, "action": "frobnicate"},
        )
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertIn("unknown bulk action", resp.json()["detail"].lower())


class TestWikiEntitiesClaimsPaging(WikiNewRouteTestBase):

    def _insert_entity(self, canonical_name: str, vault_id: int = 1) -> int:
        conn = self._raw()
        now = "2026-01-01T00:00:00"
        cur = conn.execute(
            """INSERT INTO wiki_entities
               (vault_id, canonical_name, entity_type, aliases_json, description, created_at, updated_at)
               VALUES (?, ?, 'unknown', '[]', '', ?, ?)""",
            (vault_id, canonical_name, now, now),
        )
        conn.commit()
        eid = cur.lastrowid
        self._pool.release(conn)
        return eid

    def test_entities_limit_caps_results(self):
        for i in range(5):
            self._insert_entity(f"Entity{i:02d}")
        resp = self.client.get(
            "/api/wiki/entities", params={"vault_id": 1, "limit": 2, "offset": 0}
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(len(resp.json()["entities"]), 2)

    def test_entities_offset_windows_results(self):
        for i in range(5):
            self._insert_entity(f"Ent{i:02d}")
        full = self.client.get("/api/wiki/entities", params={"vault_id": 1, "limit": 1000}).json()["entities"]
        self.assertEqual(len(full), 5)
        windowed = self.client.get(
            "/api/wiki/entities", params={"vault_id": 1, "limit": 2, "offset": 2}
        ).json()["entities"]
        self.assertEqual(len(windowed), 2)
        # ordered by canonical_name, so offset window matches the full ordering
        self.assertEqual(
            [e["canonical_name"] for e in windowed],
            [e["canonical_name"] for e in full[2:4]],
        )

    def test_claims_limit_caps_results(self):
        for i in range(5):
            self.client.post(
                "/api/wiki/claims",
                json={"vault_id": 1, "claim_text": f"Claim number {i}", "source_type": "manual"},
            )
        resp = self.client.get(
            "/api/wiki/claims", params={"vault_id": 1, "limit": 3, "offset": 0}
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(len(resp.json()["claims"]), 3)

    def test_claims_status_filter(self):
        self.client.post(
            "/api/wiki/claims",
            json={"vault_id": 1, "claim_text": "Active claim", "source_type": "manual", "status": "active"},
        )
        self.client.post(
            "/api/wiki/claims",
            json={"vault_id": 1, "claim_text": "Superseded claim", "source_type": "manual", "status": "superseded"},
        )
        resp = self.client.get(
            "/api/wiki/claims", params={"vault_id": 1, "status": "active"}
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        claims = resp.json()["claims"]
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0]["status"], "active")


class TestWikiDocumentStatusRoute(WikiNewRouteTestBase):

    def test_document_status_no_jobs(self):
        file_id = self._insert_file(vault_id=1)
        resp = self.client.get(f"/api/wiki/documents/{file_id}/status", params={"vault_id": 1})
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertEqual(data["file_id"], file_id)
        self.assertEqual(data["wiki_status"], "not_compiled")
        self.assertEqual(data["claims_count"], 0)
        self.assertIsNone(data["latest_job"])
        self.assertEqual(data["job_count"], 0)

    def test_document_status_selects_latest_job(self):
        file_id = self._insert_file(vault_id=1)
        # Enqueue a compile job (POST /compile creates a wiki ingest job).
        compile_resp = self.client.post(
            f"/api/wiki/documents/{file_id}/compile", params={"vault_id": 1}
        )
        self.assertEqual(compile_resp.status_code, 202, compile_resp.text)

        resp = self.client.get(f"/api/wiki/documents/{file_id}/status", params={"vault_id": 1})
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertEqual(data["job_count"], 1)
        self.assertIsNotNone(data["latest_job"])
        self.assertEqual(data["latest_job"]["trigger_id"], f"file:{file_id}")
        # A pending job is not yet compiled.
        self.assertEqual(data["wiki_status"], "not_compiled")


class TestWikiMemoryStatusRoute(WikiNewRouteTestBase):

    def test_memory_status_not_promoted(self):
        mem_id = self._insert_memory("Some memory content")
        resp = self.client.get(f"/api/wiki/memories/{mem_id}/status", params={"vault_id": 1})
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertEqual(data["memory_id"], mem_id)
        self.assertEqual(data["wiki_status"], "not_promoted")
        self.assertEqual(data["claims_count"], 0)
        self.assertEqual(data["job_count"], 0)
        self.assertIsNone(data["latest_job"])

    def test_memory_status_selects_memory_job(self):
        mem_id = self._insert_memory("Memory with a job")
        # Seed a memory-trigger job directly through the store.
        conn = self._raw()
        from app.services.wiki_store import WikiStore
        store = WikiStore(conn)
        job = store.create_job(
            vault_id=1,
            trigger_type="memory",
            trigger_id=f"memory:{mem_id}",
            input_json={"memory_id": mem_id},
        )
        self._pool.release(conn)

        resp = self.client.get(f"/api/wiki/memories/{mem_id}/status", params={"vault_id": 1})
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertEqual(data["job_count"], 1)
        self.assertIsNotNone(data["latest_job"])
        self.assertEqual(data["latest_job"]["id"], job.id)
        self.assertEqual(data["latest_job"]["trigger_id"], f"memory:{mem_id}")


class TestWikiOptimisticLock(WikiNewRouteTestBase):

    def test_stale_expected_version_returns_409(self):
        page = self._create_page(title="Lock Me")
        page_id = page["id"]
        # A freshly-created page starts at version 1. First update (no expected
        # version) bumps it to 2.
        first = self.client.put(
            f"/api/wiki/pages/{page_id}",
            json={"title": "Lock Me v1"},
        )
        self.assertEqual(first.status_code, 200, first.text)
        current_version = first.json()["version"]
        self.assertEqual(current_version, 2)

        # Second update with a now-stale expected_version (1) -> 409 conflict.
        stale = self.client.put(
            f"/api/wiki/pages/{page_id}",
            json={"title": "Lock Me stale", "expected_version": 1},
        )
        self.assertEqual(stale.status_code, 409, stale.text)
        self.assertIn("version conflict", stale.json()["detail"].lower())

        # A matching expected_version still succeeds.
        ok = self.client.put(
            f"/api/wiki/pages/{page_id}",
            json={"title": "Lock Me ok", "expected_version": current_version},
        )
        self.assertEqual(ok.status_code, 200, ok.text)


class TestWikiF003ExistenceOracle(WikiNewRouteTestBase):
    """F-003: a vault the caller cannot read must return 404, not 403, so an
    unauthorized caller cannot distinguish 'exists elsewhere' from 'absent'."""

    def setUp(self):
        super().setUp()
        # Seed vault 2 (private by default) and a page inside it.
        self._insert_vault2()
        page = self._create_page(vault_id=2, title="Secret Vault2 Page")
        self._secret_page_id = page["id"]

    def _use_viewer(self):
        viewer = {
            "id": 4242,
            "username": "viewer",
            "full_name": "Viewer",
            "role": "viewer",
            "is_active": True,
            "must_change_password": False,
        }
        app.dependency_overrides[get_current_active_user] = lambda: viewer

    def test_unreadable_vault_page_returns_404_not_403(self):
        # A viewer with no membership has no read access to private vault 2;
        # the route collapses the 403 to 404 (F-003).
        self._use_viewer()
        resp = self.client.get(f"/api/wiki/pages/{self._secret_page_id}")
        self.assertEqual(resp.status_code, 404, resp.text)
        self.assertEqual(resp.json()["detail"], "Wiki page not found")
        # Restore superadmin override so tearDown's pop is consistent.
        app.dependency_overrides[get_current_active_user] = lambda: _MOCK_SUPERADMIN

    def test_nonexistent_page_also_returns_404(self):
        # The negative existence path returns the same status/detail, so the
        # two cases are indistinguishable to the caller.
        self._use_viewer()
        resp = self.client.get("/api/wiki/pages/99999")
        self.assertEqual(resp.status_code, 404, resp.text)
        self.assertEqual(resp.json()["detail"], "Wiki page not found")
        app.dependency_overrides[get_current_active_user] = lambda: _MOCK_SUPERADMIN


class TestWikiF010GenericError(WikiNewRouteTestBase):
    """F-010: internal create_page failures surface a generic message, never a
    raw exception string."""

    def test_create_page_internal_error_is_generic(self):
        # Force store.create_page to raise. The handler logs the exception and
        # returns a 400 with a fixed, non-leaking detail.
        from unittest.mock import patch
        with patch(
            "app.api.routes.wiki.WikiStore.create_page",
            side_effect=RuntimeError("super-secret internal stack detail"),
        ):
            resp = self.client.post(
                "/api/wiki/pages",
                json={"vault_id": 1, "title": "Boom", "page_type": "entity"},
            )
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertEqual(resp.json()["detail"], "Failed to create wiki page")
        self.assertNotIn("super-secret", resp.json()["detail"])
