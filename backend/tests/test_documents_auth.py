"""Tests for documents routes authentication and RBAC protection.

This test suite verifies:
- Authentication: All document endpoints return 401 when unauthenticated
- Authorization: Permission-based access control for document operations
"""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Stub missing optional dependencies
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
    from unstructured.partition.auto import partition
except ImportError:
    import types

    _unstructured = types.ModuleType("unstructured")
    _unstructured.__path__ = []
    _unstructured.partition = types.ModuleType("unstructured.partition")
    _unstructured.partition.__path__ = []
    _unstructured.partition.auto = types.ModuleType("unstructured.partition.auto")
    _unstructured.partition.auto.partition = lambda *args, **kwargs: []
    _unstructured.chunking = types.ModuleType("unstructured.chunking")
    _unstructured.chunking.__path__ = []
    _unstructured.chunking.title = types.ModuleType("unstructured.chunking.title")
    _unstructured.chunking.title.chunk_by_title = lambda *args, **kwargs: []
    _unstructured.documents = types.ModuleType("unstructured.documents")
    _unstructured.documents.__path__ = []
    _unstructured.documents.elements = types.ModuleType(
        "unstructured.documents.elements"
    )
    _unstructured.documents.elements.Element = type("Element", (), {})
    sys.modules["unstructured"] = _unstructured
    sys.modules["unstructured.partition"] = _unstructured.partition
    sys.modules["unstructured.partition.auto"] = _unstructured.partition.auto
    sys.modules["unstructured.chunking"] = _unstructured.chunking
    sys.modules["unstructured.chunking.title"] = _unstructured.chunking.title
    sys.modules["unstructured.documents"] = _unstructured.documents
    sys.modules["unstructured.documents.elements"] = _unstructured.documents.elements

import sqlite3

from _db_pool import SimpleConnectionPool
from fastapi.testclient import TestClient

from app.api.deps import (
    get_background_processor,
    get_db,
    get_db_pool,
    get_embedding_service,
    get_secret_manager,
    get_vector_store,
)
from app.api.routes.documents import _allowed_document_roots, _path_is_within
from app.config import settings
from app.main import app
from app.services.auth_service import create_access_token


class TestDocumentAuthBase(unittest.TestCase):
    """Base test class for document auth tests."""

    def setUp(self):
        """Set up test client with temporary database."""
        self.client = TestClient(app)
        self._temp_dir = tempfile.mkdtemp()

        # Store original settings BEFORE modifying
        self._original_jwt_secret = settings.jwt_secret_key
        self._original_users_enabled = settings.users_enabled
        self._original_data_dir = settings.data_dir

        # CRITICAL: Update settings.data_dir so sqlite_path points to test db
        settings.data_dir = Path(self._temp_dir)
        settings.jwt_secret_key = "test-secret-key-for-testing-at-least-32-chars-long"
        settings.users_enabled = True

        # Use app.db to align with settings.sqlite_path
        self._db_path = str(Path(self._temp_dir) / "app.db")

        # Clear pool cache BEFORE setting up new database
        from app.models.database import _pool_cache, _pool_cache_lock

        with _pool_cache_lock:
            for path, pool in list(_pool_cache.items()):
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

        # Mock vector store for tests
        self._mock_vector_store = MagicMock()
        self._mock_vector_store.db = None
        self._mock_vector_store.delete_by_file = AsyncMock(return_value=1)

        # Mock embedding service
        self._mock_embedding_service = MagicMock()

        # Mock db pool
        self._mock_db_pool = self._connection_pool

        # Mock background processor
        self._mock_background_processor = MagicMock()
        self._mock_background_processor.is_running = True
        self._mock_background_processor.enqueue = AsyncMock()

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_vector_store] = lambda: self._mock_vector_store
        app.dependency_overrides[get_embedding_service] = lambda: (
            self._mock_embedding_service
        )
        app.dependency_overrides[get_db_pool] = lambda: self._mock_db_pool
        app.dependency_overrides[get_background_processor] = lambda: (
            self._mock_background_processor
        )
        _mock_sm = MagicMock()
        _mock_sm.get_hmac_key.return_value = (b"test-hmac-key-32bytes-padding!!", "v1")
        app.dependency_overrides[get_secret_manager] = lambda: _mock_sm

        # Seed test users and vaults
        conn = self._connection_pool.get_connection()
        try:
            conn.execute("PRAGMA foreign_keys = ON")

            # Clear existing data
            conn.execute("DELETE FROM files")
            conn.execute("DELETE FROM vault_members")
            conn.execute("DELETE FROM users WHERE id != 0")

            # These route tests authenticate with JWTs; the stored password hash
            # is never verified, so keep setup independent from bcrypt backends.
            pw = "test-password-hash"

            # User 1: superadmin
            conn.execute(
                "INSERT OR IGNORE INTO users (id, username, hashed_password, full_name, role, is_active) VALUES (?, ?, ?, ?, ?, 1)",
                (1, "superadmin", pw, "Super Admin", "superadmin"),
            )
            # User 2: admin
            conn.execute(
                "INSERT OR IGNORE INTO users (id, username, hashed_password, full_name, role, is_active) VALUES (?, ?, ?, ?, ?, 1)",
                (2, "admin1", pw, "Admin One", "admin"),
            )
            # User 3: member with vault access
            conn.execute(
                "INSERT OR IGNORE INTO users (id, username, hashed_password, full_name, role, is_active) VALUES (?, ?, ?, ?, ?, 1)",
                (3, "member1", pw, "Member One", "member"),
            )
            # User 4: member with read-only vault access
            conn.execute(
                "INSERT OR IGNORE INTO users (id, username, hashed_password, full_name, role, is_active) VALUES (?, ?, ?, ?, ?, 1)",
                (4, "member_readonly", pw, "Member Read Only", "member"),
            )
            # User 5: member without any vault access
            conn.execute(
                "INSERT OR IGNORE INTO users (id, username, hashed_password, full_name, role, is_active) VALUES (?, ?, ?, ?, ?, 1)",
                (5, "member_novault", pw, "Member No Vault", "member"),
            )

            # Create additional vaults for testing (Vault 1 is the Default vault)
            conn.execute(
                "INSERT OR IGNORE INTO vaults (id, name, description) VALUES (?, ?, ?)",
                (2, "Private Vault", "A private vault"),
            )
            conn.execute(
                "INSERT OR IGNORE INTO vaults (id, name, description) VALUES (?, ?, ?)",
                (3, "Read-Only Vault", "A read-only vault"),
            )

            # Seed vault_members
            # member1 (user 3) has WRITE access to vault 2
            conn.execute(
                "INSERT OR IGNORE INTO vault_members (vault_id, user_id, permission, granted_by) VALUES (?, ?, ?, ?)",
                (2, 3, "write", 1),
            )
            # member_readonly (user 4) has READ access to vault 3
            conn.execute(
                "INSERT OR IGNORE INTO vault_members (vault_id, user_id, permission, granted_by) VALUES (?, ?, ?, ?)",
                (3, 4, "read", 1),
            )

            # Seed a test document
            conn.execute(
                "INSERT OR IGNORE INTO files (id, file_name, file_path, file_size, status, chunk_count, vault_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (1, "test_doc.txt", "/uploads/test_doc.txt", 100, "indexed", 0, 2),
            )

            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)

    def tearDown(self):
        """Clean up after each test."""
        # Clear pool cache before changing paths
        from app.models.database import _pool_cache, _pool_cache_lock

        with _pool_cache_lock:
            for path, pool in list(_pool_cache.items()):
                pool.close_all()
            _pool_cache.clear()

        # Restore original settings
        settings.jwt_secret_key = self._original_jwt_secret
        settings.users_enabled = self._original_users_enabled
        if hasattr(self, "_original_data_dir"):
            settings.data_dir = self._original_data_dir

        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_vector_store, None)
        app.dependency_overrides.pop(get_embedding_service, None)
        app.dependency_overrides.pop(get_db_pool, None)
        app.dependency_overrides.pop(get_background_processor, None)
        app.dependency_overrides.pop(get_secret_manager, None)

        if hasattr(self, "_connection_pool"):
            self._connection_pool.close_all()

        shutil.rmtree(self._temp_dir, ignore_errors=True)

    def _get_db_conn(self):
        """Get a raw connection for test data setup."""
        return self._connection_pool.get_connection()

    def _superadmin_token(self):
        """Generate access token for superadmin user."""
        return create_access_token(1, "superadmin", "superadmin")

    def _admin_token(self):
        """Generate access token for admin user."""
        return create_access_token(2, "admin1", "admin")

    def _member_token(self):
        """Generate access token for member1 user (has vault access)."""
        return create_access_token(3, "member1", "member")

    def _member_readonly_token(self):
        """Generate access token for member_readonly user."""
        return create_access_token(4, "member_readonly", "member")

    def _member_no_access_token(self):
        """Generate access token for member_novault user (no vault access)."""
        return create_access_token(5, "member_novault", "member")

    def _auth_headers(self, token):
        """Create authorization headers with token."""
        return {"Authorization": f"Bearer {token}"}


class TestAuthentication(TestDocumentAuthBase):
    """Tests for unauthenticated access - all endpoints should return 401."""

    def test_get_documents_unauthenticated(self):
        """GET /documents without auth returns 401."""
        response = self.client.get("/api/documents")
        self.assertEqual(response.status_code, 401)

    def test_get_documents_stats_unauthenticated(self):
        """GET /documents/stats without auth returns 401."""
        response = self.client.get("/api/documents/stats")
        self.assertEqual(response.status_code, 401)

    def test_post_documents_unauthenticated(self):
        """POST /documents without auth returns 401."""
        response = self.client.post(
            "/api/documents", data={"file": (b"test content", "test.txt")}
        )
        self.assertEqual(response.status_code, 401)

    def test_post_documents_upload_unauthenticated(self):
        """POST /documents/upload without auth returns 401."""
        response = self.client.post(
            "/api/documents/upload", data={"file": (b"test content", "test.txt")}
        )
        self.assertEqual(response.status_code, 401)

    def test_post_documents_scan_unauthenticated(self):
        """POST /documents/scan without auth returns 401."""
        response = self.client.post("/api/documents/scan")
        self.assertEqual(response.status_code, 401)

    def test_delete_documents_by_id_unauthenticated(self):
        """DELETE /documents/{id} without auth returns 401."""
        response = self.client.delete("/api/documents/1")
        self.assertEqual(response.status_code, 401)

    def test_get_document_raw_unauthenticated(self):
        """GET /documents/{id}/raw without auth returns 401."""
        response = self.client.get("/api/documents/1/raw")
        self.assertEqual(response.status_code, 401)

    def test_post_documents_batch_unauthenticated(self):
        """POST /documents/batch without auth returns 401."""
        response = self.client.post("/api/documents/batch", json=["1", "2"])
        self.assertEqual(response.status_code, 401)

    def test_delete_documents_vault_all_unauthenticated(self):
        """DELETE /documents/vault/{id}/all without auth returns 401."""
        response = self.client.delete("/api/documents/vault/2/all")
        self.assertEqual(response.status_code, 401)

    def test_post_documents_admin_retry_unauthenticated(self):
        """POST /documents/admin/retry/{id} without auth returns 401."""
        response = self.client.post("/api/documents/admin/retry/1")
        self.assertEqual(response.status_code, 401)


class TestWritePermission(TestDocumentAuthBase):
    """Tests for write permission checks (member without write permission → 403)."""

    def test_member_without_write_permission_post_documents_returns_403(self):
        """Member with read-only access → 403 on POST /documents."""
        # member_readonly (user 4) has READ permission on vault 3, not write
        response = self.client.post(
            "/api/documents?vault_id=3",
            headers=self._auth_headers(self._member_readonly_token()),
            data={"file": (b"test content", "test.txt")},
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("Insufficient vault permissions", response.text or "")

    def test_member_without_write_permission_post_documents_upload_returns_403(self):
        """Member with read-only access → 403 on POST /documents/upload."""
        # member_readonly (user 4) has READ permission on vault 3, not write
        response = self.client.post(
            "/api/documents/upload?vault_id=3",
            headers=self._auth_headers(self._member_readonly_token()),
            data={"file": (b"test content", "test.txt")},
        )
        self.assertEqual(response.status_code, 403)

    def test_member_without_write_permission_post_documents_scan_returns_403(self):
        """Member with read-only access → 403 on POST /documents/scan."""
        # member_readonly (user 4) has READ permission on vault 3, not write
        response = self.client.post(
            "/api/documents/scan?vault_id=3",
            headers=self._auth_headers(self._member_readonly_token()),
        )
        self.assertEqual(response.status_code, 403)

    def test_member_without_vault_access_post_documents_returns_403(self):
        """Member without vault access → 403 on POST /documents."""
        # member_novault (user 5) has no vault access
        response = self.client.post(
            "/api/documents?vault_id=2",
            headers=self._auth_headers(self._member_no_access_token()),
            data={"file": (b"test content", "test.txt")},
        )
        self.assertEqual(response.status_code, 403)


class TestAdminPermission(TestDocumentAuthBase):
    """Tests for admin permission checks on document operations."""

    def test_member_without_admin_permission_delete_document_returns_403(self):
        """Member without admin permission → 403 on DELETE /documents/{id}."""
        # member1 (user 3) has WRITE permission on vault 2, not admin
        # Delete document in vault 2 should require vault admin
        response = self.client.delete(
            "/api/documents/1", headers=self._auth_headers(self._member_token())
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("Insufficient vault permissions", response.text or "")

    def test_member_without_admin_permission_delete_vault_all_returns_403(self):
        """Member without admin permission → 403 on DELETE /documents/vault/{id}/all."""
        # member1 (user 3) has WRITE permission on vault 2, not admin
        response = self.client.delete(
            "/api/documents/vault/2/all",
            headers=self._auth_headers(self._member_token()),
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("Insufficient vault permissions", response.text or "")


class TestRoleAdminRequired(TestDocumentAuthBase):
    """Tests for endpoints requiring admin/superadmin role."""

    def test_regular_member_post_documents_batch_returns_403(self):
        """Regular member → 403 on POST /documents/batch (requires admin role)."""
        # member1 (user 3) is a regular member, not admin
        response = self.client.post(
            "/api/documents/batch",
            headers=self._auth_headers(self._member_token()),
            json=["1"],
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("Admin access required", response.text or "")

    def test_regular_member_post_documents_admin_retry_returns_403(self):
        """Regular member → 403 on POST /documents/admin/retry/{id} (requires admin role)."""
        # member1 (user 3) is a regular member, not admin
        response = self.client.post(
            "/api/documents/admin/retry/1",
            headers=self._auth_headers(self._member_token()),
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("Admin access required", response.text or "")

    def test_admin_can_post_documents_batch(self):
        """Admin can POST /documents/batch."""
        # admin1 (user 2) is an admin
        response = self.client.post(
            "/api/documents/batch",
            headers=self._auth_headers(self._admin_token()),
            json=["1"],
        )
        # Should not return 401 or 403 - might return 404 for missing file
        self.assertNotEqual(response.status_code, 401)
        self.assertNotEqual(response.status_code, 403)

    def test_superadmin_can_post_documents_admin_retry(self):
        """Superadmin can POST /documents/admin/retry/{id}."""
        # superadmin (user 1) is a superadmin
        response = self.client.post(
            "/api/documents/admin/retry/1",
            headers=self._auth_headers(self._superadmin_token()),
        )
        # Should not return 401 or 403 - might return 404 for missing file
        self.assertNotEqual(response.status_code, 401)
        self.assertNotEqual(response.status_code, 403)


class TestReadAccess(TestDocumentAuthBase):
    """Tests for read access - members with read access can list documents."""

    def _seed_document_file(
        self,
        file_id: int,
        vault_id: int,
        filename: str,
        content: bytes,
    ) -> Path:
        file_path = settings.vault_uploads_dir(vault_id) / filename
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(content)

        conn = self._get_db_conn()
        try:
            conn.execute(
                """
                INSERT OR REPLACE INTO files (
                    id, file_name, file_path, file_size, file_type, status,
                    chunk_count, vault_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    file_id,
                    filename,
                    str(file_path),
                    len(content),
                    Path(filename).suffix.lower(),
                    "indexed",
                    1,
                    vault_id,
                ),
            )
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)

        return file_path

    def test_member_with_read_access_can_list_documents(self):
        """Member with read access can GET /documents."""
        # member_readonly (user 4) has READ permission on vault 3
        response = self.client.get(
            "/api/documents?vault_id=3",
            headers=self._auth_headers(self._member_readonly_token()),
        )
        # Should succeed (200) or at least not 403
        self.assertNotEqual(response.status_code, 401)
        self.assertNotEqual(response.status_code, 403)

    def test_member_with_read_access_can_get_stats(self):
        """Member with read access can GET /documents/stats."""
        # member_readonly (user 4) has READ permission on vault 3
        response = self.client.get(
            "/api/documents/stats?vault_id=3",
            headers=self._auth_headers(self._member_readonly_token()),
        )
        # Should succeed (200) or at least not 403
        self.assertNotEqual(response.status_code, 401)
        self.assertNotEqual(response.status_code, 403)

    def test_member_with_read_access_can_fetch_pdf_raw_bytes(self):
        """Member with read access can fetch inline PDF bytes."""
        pdf_bytes = b"%PDF-1.4\n% test pdf\n"
        self._seed_document_file(30, 3, "readable.pdf", pdf_bytes)

        response = self.client.get(
            "/api/documents/30/raw",
            headers=self._auth_headers(self._member_readonly_token()),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, pdf_bytes)
        self.assertIn("application/pdf", response.headers.get("content-type", ""))
        self.assertIn("inline", response.headers.get("content-disposition", ""))
        self.assertIn("readable.pdf", response.headers.get("content-disposition", ""))
        self.assertEqual(response.headers.get("x-content-type-options"), "nosniff")

    def test_member_with_read_access_can_fetch_non_pdf_raw_bytes(self):
        """Non-PDF originals download instead of rendering active content inline."""
        html_bytes = b"<script>window.opener.location='https://evil.example'</script>"
        self._seed_document_file(34, 3, "preview.html", html_bytes)

        response = self.client.get(
            "/api/documents/34/raw",
            headers=self._auth_headers(self._member_readonly_token()),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, html_bytes)
        self.assertIn("text/html", response.headers.get("content-type", ""))
        self.assertIn("attachment", response.headers.get("content-disposition", ""))
        self.assertIn("preview.html", response.headers.get("content-disposition", ""))
        self.assertEqual(response.headers.get("x-content-type-options"), "nosniff")

    def test_member_without_read_access_cannot_fetch_raw_bytes(self):
        """A file in another vault returns 403 before serving bytes."""
        self._seed_document_file(31, 2, "private.pdf", b"%PDF-1.4\nprivate\n")

        response = self.client.get(
            "/api/documents/31/raw",
            headers=self._auth_headers(self._member_no_access_token()),
        )

        self.assertEqual(response.status_code, 403)
        self.assertIn("No read access", response.text)

    def test_document_raw_missing_row_returns_404(self):
        """Missing document IDs return 404."""
        response = self.client.get(
            "/api/documents/404/raw",
            headers=self._auth_headers(self._superadmin_token()),
        )

        self.assertEqual(response.status_code, 404)

    def test_document_raw_missing_file_returns_404(self):
        """Missing files on disk return a controlled 404."""
        missing_path = settings.vault_uploads_dir(3) / "missing.pdf"
        conn = self._get_db_conn()
        try:
            conn.execute(
                """
                INSERT OR REPLACE INTO files (
                    id, file_name, file_path, file_size, file_type, status,
                    chunk_count, vault_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    32,
                    "missing.pdf",
                    str(missing_path),
                    128,
                    ".pdf",
                    "indexed",
                    1,
                    3,
                ),
            )
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)

        response = self.client.get(
            "/api/documents/32/raw",
            headers=self._auth_headers(self._member_readonly_token()),
        )

        self.assertEqual(response.status_code, 404)

    def test_document_raw_rejects_paths_outside_document_roots(self):
        """DB paths outside configured document roots are not served."""
        outside_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, outside_dir, ignore_errors=True)
        outside_path = outside_dir / "outside.pdf"
        outside_path.write_bytes(b"%PDF-1.4\noutside\n")
        resolved_outside_path = outside_path.resolve(strict=False)
        allowed_roots = [
            root.resolve(strict=False) for root in _allowed_document_roots(settings, 3)
        ]
        self.assertFalse(
            any(_path_is_within(resolved_outside_path, root) for root in allowed_roots)
        )

        conn = self._get_db_conn()
        try:
            conn.execute(
                """
                INSERT OR REPLACE INTO files (
                    id, file_name, file_path, file_size, file_type, status,
                    chunk_count, vault_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    33,
                    "outside.pdf",
                    str(outside_path),
                    outside_path.stat().st_size,
                    ".pdf",
                    "indexed",
                    1,
                    3,
                ),
            )
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)

        response = self.client.get(
            "/api/documents/33/raw",
            headers=self._auth_headers(self._member_readonly_token()),
        )

        self.assertEqual(response.status_code, 404)

    def test_document_raw_serves_unknown_extension_as_attachment(self):
        """Unknown MIME types are still downloadable without inline rendering."""
        original_bytes = b"opaque bytes"
        self._seed_document_file(35, 3, "archive.unknownext", original_bytes)

        response = self.client.get(
            "/api/documents/35/raw",
            headers=self._auth_headers(self._member_readonly_token()),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, original_bytes)
        self.assertIn(
            "application/octet-stream", response.headers.get("content-type", "")
        )
        self.assertIn("attachment", response.headers.get("content-disposition", ""))
        self.assertEqual(response.headers.get("x-content-type-options"), "nosniff")

    def test_document_search_matches_metadata_fields(self):
        """Document list search matches metadata, not only filename."""
        conn = self._get_db_conn()
        try:
            conn.execute(
                """
                INSERT INTO files (
                    id, file_name, file_path, file_size, file_type, status,
                    chunk_count, vault_id, source, email_subject, email_sender,
                    document_date
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    20,
                    "budget.pdf",
                    "/uploads/budget.pdf",
                    100,
                    "application/pdf",
                    "indexed",
                    1,
                    2,
                    "email",
                    "Quarterly planning packet",
                    "finance@example.com",
                    "2026-05-13",
                ),
            )
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)

        response = self.client.get(
            "/api/documents?vault_id=2&search=quarterly",
            headers=self._auth_headers(self._member_token()),
        )

        self.assertEqual(response.status_code, 200)
        filenames = [doc["filename"] for doc in response.json()["documents"]]
        self.assertEqual(filenames, ["budget.pdf"])

    def test_document_search_preserves_filename_substring_matching(self):
        """Filename search remains substring-based for existing UX expectations."""
        response = self.client.get(
            "/api/documents?vault_id=2&search=est_doc",
            headers=self._auth_headers(self._member_token()),
        )

        self.assertEqual(response.status_code, 200)
        filenames = [doc["filename"] for doc in response.json()["documents"]]
        self.assertEqual(filenames, ["test_doc.txt"])

    def test_document_search_non_token_query_still_matches_filenames(self):
        """Punctuation-only searches keep the legacy filename substring behavior."""
        conn = self._get_db_conn()
        try:
            conn.execute(
                """
                INSERT INTO files (id, file_name, file_path, file_size, vault_id, status)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (997, "notes!!!.pdf", "/tmp/notes!!!.pdf", 128, 2, "indexed"),
            )
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)

        response = self.client.get(
            "/api/documents?vault_id=2&search=!!!",
            headers=self._auth_headers(self._member_token()),
        )

        self.assertEqual(response.status_code, 200)
        filenames = [doc["filename"] for doc in response.json()["documents"]]
        self.assertEqual(filenames, ["notes!!!.pdf"])

    def test_document_search_fts_operator_text_is_escaped_to_tokens(self):
        """FTS metacharacters are reduced to safe prefix tokens before MATCH."""
        conn = self._get_db_conn()
        try:
            conn.execute(
                """
                INSERT INTO files (
                    id, file_name, file_path, file_size, vault_id, status,
                    source, email_subject
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    998,
                    "operator-report.pdf",
                    "/tmp/operator-report.pdf",
                    128,
                    2,
                    "indexed",
                    "email",
                    'OR file_name MATCH x NEAR("unsafe")',
                ),
            )
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)

        response = self.client.get(
            '/api/documents?vault_id=2&search=NEAR("unsafe")',
            headers=self._auth_headers(self._member_token()),
        )

        self.assertEqual(response.status_code, 200)
        filenames = [doc["filename"] for doc in response.json()["documents"]]
        self.assertEqual(filenames, ["operator-report.pdf"])


class TestDeleteAuditLogging(TestDocumentAuthBase):
    """Bulk and vault-wide deletes must each write a per-file HMAC audit row."""

    def _install_secret_manager(self):
        sm = MagicMock()
        sm.get_hmac_key.return_value = (b"test-hmac-key", "v1")
        app.state.secret_manager = sm
        return sm

    def _seed_files(self, ids):
        conn = self._connection_pool.get_connection()
        try:
            # Clean slate for vault 2 so vault-wide delete counts are deterministic
            # (the base setUp seeds an unrelated file in vault 2).
            conn.execute("DELETE FROM files WHERE vault_id = 2")
            for fid in ids:
                conn.execute(
                    "INSERT OR REPLACE INTO files (id, file_name, file_path, file_size, status, chunk_count, vault_id) "
                    "VALUES (?, ?, ?, ?, 'indexed', 0, 2)",
                    (fid, f"doc{fid}.txt", f"/uploads/doc{fid}.txt", 10),
                )
            conn.execute("DELETE FROM document_actions")
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)

    def _audit_count(self, action="delete"):
        conn = self._connection_pool.get_connection()
        try:
            return conn.execute(
                "SELECT COUNT(*) FROM document_actions WHERE action = ?", (action,)
            ).fetchone()[0]
        finally:
            self._connection_pool.release_connection(conn)

    def tearDown(self):
        if hasattr(app.state, "secret_manager"):
            delattr(app.state, "secret_manager")
        super().tearDown()

    def test_batch_delete_writes_audit_row_per_file(self):
        self._install_secret_manager()
        self._seed_files([101, 102])
        resp = self.client.post(
            "/api/documents/batch",
            headers=self._auth_headers(self._superadmin_token()),
            json={"file_ids": ["101", "102"]},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["deleted_count"], 2)
        self.assertEqual(self._audit_count("delete"), 2)

    def test_vault_all_delete_writes_audit_row_per_file(self):
        self._install_secret_manager()
        self._seed_files([201, 202, 203])
        resp = self.client.delete(
            "/api/documents/vault/2/all",
            headers=self._auth_headers(self._superadmin_token()),
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["deleted_count"], 3)
        self.assertEqual(self._audit_count("delete"), 3)


if __name__ == "__main__":
    unittest.main()
