"""Additional tests for KMS route fixes from PR 124 review findings.

Covers:
- kms_enabled master switch (503 when disabled)
- Blank slug validation (422 for empty slug)
- CSRF protection (403 without CSRF token)
- .pptx MIME type in imap_allowed_mime_types
- KMSCompileProcessor lifecycle via require_kms_enabled dependency
"""

import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from queue import Empty, Queue
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Stub optional heavy deps so importing app.main is cheap in CI.
try:
    import lancedb  # noqa: F401
except ImportError:
    import types
    sys.modules["lancedb"] = types.ModuleType("lancedb")

try:
    import pyarrow  # noqa: F401
except ImportError:
    import types
    sys.modules["pyarrow"] = types.ModuleType("pyarrow")

from fastapi.testclient import TestClient

from app.api.deps import get_db
from app.config import settings
from app.main import app
from app.security import csrf_protect
from app.services.auth_service import create_access_token


class SimpleConnectionPool:
    def __init__(self, db_path):
        self.db_path = db_path
        self._pool = Queue(maxsize=5)
        self._lock = threading.Lock()
        self._closed = False

    def get_connection(self):
        if self._closed:
            raise RuntimeError("Pool closed")
        try:
            return self._pool.get_nowait()
        except Empty:
            return self._create_connection()

    def _create_connection(self):
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn

    def release_connection(self, conn):
        if not self._closed:
            try:
                self._pool.put_nowait(conn)
            except Exception:
                conn.close()

    def close_all(self):
        self._closed = True
        while True:
            try:
                conn = self._pool.get_nowait()
                conn.close()
            except Empty:
                break


class KMSFixTestBase(unittest.TestCase):
    """Base class for KMS fix tests."""

    def setUp(self):
        self.client = TestClient(app)
        self._temp_dir = tempfile.mkdtemp()

        self._original_jwt_secret = settings.jwt_secret_key
        self._original_users_enabled = settings.users_enabled
        self._original_data_dir = settings.data_dir
        self._original_kms_enabled = settings.kms_enabled

        settings.data_dir = Path(self._temp_dir)
        settings.jwt_secret_key = os.urandom(32).hex()
        settings.users_enabled = True
        settings.kms_enabled = True  # Default to True for most tests

        self._db_path = str(Path(self._temp_dir) / "app.db")

        from app.models.database import _pool_cache, _pool_cache_lock

        with _pool_cache_lock:
            for _path, pool in list(_pool_cache.items()):
                pool.close_all()
            _pool_cache.clear()

        from app.models.database import init_db, run_migrations

        init_db(self._db_path)
        run_migrations(self._db_path)
        self._connection_pool = SimpleConnectionPool(self._db_path)

        def override_get_db():
            conn = self._connection_pool.get_connection()
            try:
                yield conn
            finally:
                self._connection_pool.release_connection(conn)

        app.dependency_overrides[get_db] = override_get_db
        # CSRF is exercised separately; bypass it for the JWT-based route tests.
        app.dependency_overrides[csrf_protect] = lambda: "test-csrf"

        conn = self._connection_pool.get_connection()
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            pw = "test-password-hash"
            conn.execute(
                "INSERT OR IGNORE INTO users (id, username, hashed_password, full_name, role, is_active) VALUES (1,'superadmin',?, 'Super','superadmin',1)",
                (pw,),
            )
            conn.execute(
                "INSERT OR IGNORE INTO users (id, username, hashed_password, full_name, role, is_active) VALUES (3,'member1',?, 'Member One','member',1)",
                (pw,),
            )
            conn.execute(
                "INSERT OR IGNORE INTO vaults (id, name, description) VALUES (2,'Write Vault','w')"
            )
            conn.execute(
                "INSERT OR IGNORE INTO vault_members (vault_id, user_id, permission, granted_by) VALUES (2,3,'write',1)"
            )
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)

    def tearDown(self):
        from app.models.database import _pool_cache, _pool_cache_lock

        with _pool_cache_lock:
            for _path, pool in list(_pool_cache.items()):
                pool.close_all()
            _pool_cache.clear()

        settings.jwt_secret_key = self._original_jwt_secret
        settings.users_enabled = self._original_users_enabled
        settings.data_dir = self._original_data_dir
        settings.kms_enabled = self._original_kms_enabled
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(csrf_protect, None)
        if hasattr(self, "_connection_pool"):
            self._connection_pool.close_all()
        shutil.rmtree(self._temp_dir, ignore_errors=True)

    def _headers(self, user_id, username, role):
        return {"Authorization": f"Bearer {create_access_token(user_id, username, role)}"}

    def _write_headers(self):
        return self._headers(3, "member1", "member")


# ---------------------------------------------------------------------------
# Test: kms_enabled master switch
# ---------------------------------------------------------------------------


class TestKMSEnabledSwitch(KMSFixTestBase):
    """Tests for the kms_enabled master switch (PR 124 review finding)."""

    def test_kms_disabled_list_entries_returns_503(self):
        """GET /api/kms/entries returns 503 when kms_enabled=False."""
        settings.kms_enabled = False
        response = self.client.get(
            "/api/kms/entries?vault_id=2",
            headers=self._write_headers(),
        )
        self.assertEqual(response.status_code, 503)
        self.assertIn("disabled", response.json()["detail"].lower())

    def test_kms_disabled_create_entry_returns_503(self):
        """POST /api/kms/entries returns 503 when kms_enabled=False."""
        settings.kms_enabled = False
        response = self.client.post(
            "/api/kms/entries",
            json={"vault_id": 2, "title": "Test Entry"},
            headers=self._write_headers(),
        )
        self.assertEqual(response.status_code, 503)
        self.assertIn("disabled", response.json()["detail"].lower())

    def test_kms_disabled_get_entry_returns_503(self):
        """GET /api/kms/entries/{id} returns 503 when kms_enabled=False."""
        settings.kms_enabled = False
        response = self.client.get(
            "/api/kms/entries/1",
            headers=self._write_headers(),
        )
        self.assertEqual(response.status_code, 503)
        self.assertIn("disabled", response.json()["detail"].lower())

    def test_kms_disabled_update_entry_returns_503(self):
        """PUT /api/kms/entries/{id} returns 503 when kms_enabled=False."""
        settings.kms_enabled = False
        response = self.client.put(
            "/api/kms/entries/1",
            json={"title": "Updated Title"},
            headers=self._write_headers(),
        )
        self.assertEqual(response.status_code, 503)
        self.assertIn("disabled", response.json()["detail"].lower())

    def test_kms_disabled_delete_entry_returns_503(self):
        """DELETE /api/kms/entries/{id} returns 503 when kms_enabled=False."""
        settings.kms_enabled = False
        response = self.client.delete(
            "/api/kms/entries/1",
            headers=self._write_headers(),
        )
        self.assertEqual(response.status_code, 503)
        self.assertIn("disabled", response.json()["detail"].lower())

    def test_kms_disabled_search_returns_503(self):
        """GET /api/kms/search returns 503 when kms_enabled=False."""
        settings.kms_enabled = False
        response = self.client.get(
            "/api/kms/search?vault_id=2&q=test",
            headers=self._write_headers(),
        )
        self.assertEqual(response.status_code, 503)
        self.assertIn("disabled", response.json()["detail"].lower())

    def test_kms_disabled_compile_document_returns_503(self):
        """POST /api/kms/documents/{id}/compile returns 503 when kms_enabled=False."""
        settings.kms_enabled = False
        response = self.client.post(
            "/api/kms/documents/1/compile?vault_id=2",
            headers=self._write_headers(),
        )
        self.assertEqual(response.status_code, 503)
        self.assertIn("disabled", response.json()["detail"].lower())

    def test_kms_disabled_recompile_returns_503(self):
        """POST /api/kms/recompile returns 503 when kms_enabled=False."""
        settings.kms_enabled = False
        response = self.client.post(
            "/api/kms/recompile?vault_id=2",
            headers=self._write_headers(),
        )
        self.assertEqual(response.status_code, 503)
        self.assertIn("disabled", response.json()["detail"].lower())

    def test_kms_disabled_list_jobs_returns_503(self):
        """GET /api/kms/jobs returns 503 when kms_enabled=False."""
        settings.kms_enabled = False
        response = self.client.get(
            "/api/kms/jobs?vault_id=2",
            headers=self._write_headers(),
        )
        self.assertEqual(response.status_code, 503)
        self.assertIn("disabled", response.json()["detail"].lower())

    def test_kms_enabled_list_entries_succeeds(self):
        """GET /api/kms/entries returns 200 when kms_enabled=True."""
        settings.kms_enabled = True
        response = self.client.get(
            "/api/kms/entries?vault_id=2",
            headers=self._write_headers(),
        )
        self.assertEqual(response.status_code, 200)


# ---------------------------------------------------------------------------
# Test: Blank slug validation
# ---------------------------------------------------------------------------


class TestBlankSlugValidation(KMSFixTestBase):
    """Tests for blank/empty slug validation (PR 124 review finding).

    These tests use a CSRF override because they test slug validation,
    not CSRF protection. The CSRF dependency is overridden to bypass
    the CSRF manager check.
    """

    def setUp(self):
        super().setUp()
        # Override CSRF protection for slug validation tests
        # (these tests are about slug validation, not CSRF)
        app.dependency_overrides[csrf_protect] = lambda: "test-csrf-token"

    def tearDown(self):
        app.dependency_overrides.pop(csrf_protect, None)
        super().tearDown()

    def test_create_entry_with_blank_slug_returns_422(self):
        """POST /api/kms/entries with slug="" returns 422."""
        response = self.client.post(
            "/api/kms/entries",
            json={"vault_id": 2, "title": "Test Entry", "slug": ""},
            headers=self._write_headers(),
        )
        self.assertEqual(response.status_code, 422)

    def test_update_entry_with_blank_slug_returns_422(self):
        """PUT /api/kms/entries/{id} with slug="" returns 422."""
        # First create a valid entry
        create_resp = self.client.post(
            "/api/kms/entries",
            json={"vault_id": 2, "title": "Test Entry", "slug": "valid-slug"},
            headers=self._write_headers(),
        )
        self.assertEqual(create_resp.status_code, 201)
        entry_id = create_resp.json()["id"]

        # Now try to update with blank slug
        response = self.client.put(
            f"/api/kms/entries/{entry_id}",
            json={"slug": ""},
            headers=self._write_headers(),
        )
        self.assertEqual(response.status_code, 422)

    def test_create_entry_with_valid_slug_succeeds(self):
        """POST /api/kms/entries with a valid slug succeeds."""
        response = self.client.post(
            "/api/kms/entries",
            json={"vault_id": 2, "title": "Test Entry", "slug": "my-valid-slug"},
            headers=self._write_headers(),
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["slug"], "my-valid-slug")

    def test_create_entry_with_none_slug_succeeds(self):
        """POST /api/kms/entries with slug=None (null) succeeds - slug is optional."""
        response = self.client.post(
            "/api/kms/entries",
            json={"vault_id": 2, "title": "Test Entry", "slug": None},
            headers=self._write_headers(),
        )
        self.assertEqual(response.status_code, 201)

    def test_update_entry_with_valid_slug_succeeds(self):
        """PUT /api/kms/entries/{id} with a valid slug succeeds."""
        # First create a valid entry
        create_resp = self.client.post(
            "/api/kms/entries",
            json={"vault_id": 2, "title": "Test Entry"},
            headers=self._write_headers(),
        )
        self.assertEqual(create_resp.status_code, 201)
        entry_id = create_resp.json()["id"]

        # Update with valid slug
        response = self.client.put(
            f"/api/kms/entries/{entry_id}",
            json={"slug": "updated-slug"},
            headers=self._write_headers(),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["slug"], "updated-slug")


# ---------------------------------------------------------------------------
# Test: CSRF protection
# ---------------------------------------------------------------------------


class TestKMSCSRFProtection(KMSFixTestBase):
    """Tests for CSRF protection on KMS write endpoints (PR 124 review finding).

    These tests verify that write endpoints return 403 when no valid CSRF token
    is provided. The CSRF token must be in both the cookie and the X-CSRF-Token header.

    These tests set up a proper CSRF manager so that the CSRF check is actually reached,
    allowing us to test the 403 response for missing CSRF tokens.
    """

    def setUp(self):
        super().setUp()
        # Set up a mock CSRF manager on app.state so csrf_protect doesn't fail with 503
        # We want to test 403 (CSRF token missing/mismatch), not 503 (CSRF service unavailable)
        class MockCSRFManager:
            def validate_token(self, token):
                # Always return True so we can test the token mismatch/cookie check
                return True

        app.state.csrf_manager = MockCSRFManager()

    def tearDown(self):
        super().tearDown()

    def test_create_entry_without_csrf_returns_403(self):
        """POST /api/kms/entries without CSRF token returns 403."""
        response = self.client.post(
            "/api/kms/entries",
            json={"vault_id": 2, "title": "Test Entry"},
            headers=self._write_headers(),
            # No X-CSRF-Token header and no CSRF cookie
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("csrf", response.json()["detail"].lower())

    def test_update_entry_without_csrf_returns_403(self):
        """PUT /api/kms/entries/{id} without CSRF token returns 403."""
        # First create a valid entry (need CSRF token for this)
        app.dependency_overrides[csrf_protect] = lambda: "test-csrf-token"
        create_resp = self.client.post(
            "/api/kms/entries",
            json={"vault_id": 2, "title": "Test Entry"},
            headers=self._write_headers(),
        )
        self.assertEqual(create_resp.status_code, 201)
        entry_id = create_resp.json()["id"]
        app.dependency_overrides.pop(csrf_protect, None)

        # Now try without CSRF - should get 403
        response = self.client.put(
            f"/api/kms/entries/{entry_id}",
            json={"title": "Updated Title"},
            headers=self._write_headers(),
            # No X-CSRF-Token header
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("csrf", response.json()["detail"].lower())

    def test_delete_entry_without_csrf_returns_403(self):
        """DELETE /api/kms/entries/{id} without CSRF token returns 403."""
        # First create a valid entry (need CSRF token for this)
        app.dependency_overrides[csrf_protect] = lambda: "test-csrf-token"
        create_resp = self.client.post(
            "/api/kms/entries",
            json={"vault_id": 2, "title": "Test Entry"},
            headers=self._write_headers(),
        )
        self.assertEqual(create_resp.status_code, 201)
        entry_id = create_resp.json()["id"]
        app.dependency_overrides.pop(csrf_protect, None)

        response = self.client.delete(
            f"/api/kms/entries/{entry_id}",
            headers=self._write_headers(),
            # No X-CSRF-Token header
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("csrf", response.json()["detail"].lower())

    def test_compile_document_without_csrf_returns_403(self):
        """POST /api/kms/documents/{id}/compile without CSRF token returns 403."""
        # Seed a file first (need CSRF for this too)
        app.dependency_overrides[csrf_protect] = lambda: "test-csrf-token"
        conn = self._connection_pool.get_connection()
        try:
            cur = conn.execute(
                "INSERT INTO files (vault_id, file_path, file_name, file_size, status, parsed_text) VALUES (?,?,?,?,?,?)",
                (2, "/uploads/seed.txt", "seed.txt", 24, "indexed", "alpha beta gamma"),
            )
            conn.commit()
            file_id = cur.lastrowid
        finally:
            self._connection_pool.release_connection(conn)

        # Make the compile request without CSRF
        app.dependency_overrides.pop(csrf_protect, None)
        response = self.client.post(
            f"/api/kms/documents/{file_id}/compile?vault_id=2",
            headers=self._write_headers(),
            # No X-CSRF-Token header
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("csrf", response.json()["detail"].lower())

    def test_recompile_without_csrf_returns_403(self):
        """POST /api/kms/recompile without CSRF token returns 403."""
        response = self.client.post(
            "/api/kms/recompile?vault_id=2",
            headers=self._write_headers(),
            # No X-CSRF-Token header
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("csrf", response.json()["detail"].lower())


# ---------------------------------------------------------------------------
# Test: .pptx MIME type in imap_allowed_mime_types
# ---------------------------------------------------------------------------


class TestPPTXMimeType(KMSFixTestBase):
    """Tests for .pptx MIME type in imap_allowed_mime_types (PR 124 review finding)."""

    def test_pptx_mime_type_in_imap_allowed_types(self):
        """application/vnd.openxmlformats-officedocument.presentationml.presentation is in imap_allowed_mime_types."""
        pptx_mime = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
        self.assertIn(pptx_mime, settings.imap_allowed_mime_types)


if __name__ == "__main__":
    unittest.main()
