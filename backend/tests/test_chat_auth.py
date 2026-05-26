"""Tests for chat routes authentication and RBAC protection.

This test suite verifies:
- Authentication: All chat endpoints return 401 when unauthenticated
- Authorization POST /chat: Requires vault read access
- Authorization DELETE /chat/sessions/{id}: Requires vault write access
- Authorization GET /chat/sessions: Requires authentication
- Authorization GET /chat/sessions/{id}: Requires vault read access
- Authorization POST /chat/sessions: Requires vault write access
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

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

from app.api.deps import get_db, get_rag_engine, get_vector_store
from app.config import settings
from app.main import app
from app.services.auth_service import create_access_token


class TestChatAuthBase(unittest.TestCase):
    """Base test class for chat auth tests."""

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
        self._mock_vector_store.delete_by_vault = MagicMock(return_value=0)

        # Mock RAG engine for tests
        self._mock_rag_engine = MagicMock()
        self._mock_rag_engine.llm_client = None

        async def mock_query(*args, **kwargs):
            """Mock query that yields a done chunk."""
            yield {"type": "done", "sources": [], "memories_used": []}

        self._mock_rag_engine.query = mock_query

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_vector_store] = lambda: self._mock_vector_store
        app.dependency_overrides[get_rag_engine] = lambda: self._mock_rag_engine

        # Seed test users and vaults
        conn = self._connection_pool.get_connection()
        try:
            conn.execute("PRAGMA foreign_keys = ON")

            # Clear existing data
            conn.execute("DELETE FROM vault_members")
            conn.execute("DELETE FROM chat_sessions")
            conn.execute("DELETE FROM users WHERE id != 0")  # Keep admin user if exists

            # These tests authenticate with generated JWTs, so the password hash
            # only needs to satisfy the users table shape.
            pw = "unused-test-password-hash"

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
            # User 3: member with vault read access
            conn.execute(
                "INSERT OR IGNORE INTO users (id, username, hashed_password, full_name, role, is_active) VALUES (?, ?, ?, ?, ?, 1)",
                (3, "member1", pw, "Member One", "member"),
            )
            # User 4: member without vault access
            conn.execute(
                "INSERT OR IGNORE INTO users (id, username, hashed_password, full_name, role, is_active) VALUES (?, ?, ?, ?, ?, 1)",
                (4, "member2", pw, "Member Two", "member"),
            )

            # Create additional vaults for testing (Vault 1 is the Default vault)
            conn.execute(
                "INSERT OR IGNORE INTO vaults (id, name, description) VALUES (?, ?, ?)",
                (2, "Private Vault", "A private vault"),
            )
            conn.execute(
                "INSERT OR IGNORE INTO vaults (id, name, description) VALUES (?, ?, ?)",
                (3, "Vault Three", "Third vault"),
            )

            # Seed vault_members
            # member1 (user 3) has READ access to vault 2
            conn.execute(
                "INSERT OR IGNORE INTO vault_members (vault_id, user_id, permission, granted_by) VALUES (?, ?, ?, ?)",
                (2, 3, "read", 1),
            )
            # member1 (user 3) has WRITE access to vault 3
            conn.execute(
                "INSERT OR IGNORE INTO vault_members (vault_id, user_id, permission, granted_by) VALUES (?, ?, ?, ?)",
                (3, 3, "write", 1),
            )

            # Seed chat_sessions for testing
            conn.execute(
                "INSERT INTO chat_sessions (vault_id, title, created_at, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                (2, "Test Session 1"),
            )
            conn.execute(
                "INSERT INTO chat_sessions (vault_id, title, created_at, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                (3, "Test Session 2"),
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
        app.dependency_overrides.pop(get_rag_engine, None)
        if hasattr(self, "_connection_pool"):
            self._connection_pool.close_all()
        import shutil

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
        """Generate access token for member1 user (has vault read access)."""
        return create_access_token(3, "member1", "member")

    def _member_no_access_token(self):
        """Generate access token for member2 user (no vault access)."""
        return create_access_token(4, "member2", "member")

    def _auth_headers(self, token):
        """Create authorization headers with token."""
        return {"Authorization": f"Bearer {token}"}


class TestChatAuthentication(TestChatAuthBase):
    """Tests for unauthenticated access - all endpoints should return 401."""

    def test_post_chat_unauthenticated(self):
        """POST /chat without auth returns 401."""
        response = self.client.post(
            "/api/chat", json={"message": "Hello", "vault_id": 2}
        )
        self.assertEqual(response.status_code, 401)

    def test_get_chat_sessions_unauthenticated(self):
        """GET /chat/sessions without auth returns 401."""
        response = self.client.get("/api/chat/sessions")
        self.assertEqual(response.status_code, 401)

    def test_get_chat_session_by_id_unauthenticated(self):
        """GET /chat/sessions/{id} without auth returns 401."""
        response = self.client.get("/api/chat/sessions/1")
        self.assertEqual(response.status_code, 401)

    def test_post_chat_sessions_unauthenticated(self):
        """POST /chat/sessions without auth returns 401."""
        response = self.client.post(
            "/api/chat/sessions", json={"title": "New Session", "vault_id": 2}
        )
        self.assertEqual(response.status_code, 401)

    def test_delete_chat_sessions_unauthenticated(self):
        """DELETE /chat/sessions/{id} without auth returns 401."""
        response = self.client.delete("/api/chat/sessions/1")
        self.assertEqual(response.status_code, 401)

    def test_patch_message_feedback_unauthenticated(self):
        """PATCH message feedback without auth returns 401."""
        response = self.client.patch(
            "/api/chat/sessions/1/messages/1/feedback", json={"rating": "up"}
        )
        self.assertEqual(response.status_code, 401)


class TestChatAuthorization(TestChatAuthBase):
    """Tests for chat endpoint authorization."""

    def test_post_chat_member_without_vault_read_returns_403(self):
        """POST /chat by member without vault read access returns 403."""
        # member2 has no vault access, attempting to chat in vault 2
        response = self.client.post(
            "/api/chat",
            json={"message": "Hello", "vault_id": 2},
            headers=self._auth_headers(self._member_no_access_token()),
        )
        self.assertEqual(response.status_code, 403)
        data = response.json()
        self.assertIn("No read access", data.get("detail", ""))

    def test_post_chat_member_with_vault_read_returns_success(self):
        """POST /chat by member with vault read access succeeds."""
        # member1 has read access to vault 2
        response = self.client.post(
            "/api/chat",
            json={"message": "Hello", "vault_id": 2},
            headers=self._auth_headers(self._member_token()),
        )
        # Should succeed since we mocked rag_engine
        self.assertEqual(response.status_code, 200)


class TestMessageFeedbackRoute(TestChatAuthBase):
    """Tests for message feedback persistence, validation, and ownership policy."""

    def _seed_feedback_message(self, vault_id=3, owner_id=3, role="assistant"):
        conn = self._get_db_conn()
        try:
            cursor = conn.execute(
                "INSERT INTO chat_sessions (vault_id, user_id, title, created_at, updated_at) VALUES (?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                (vault_id, owner_id, "Feedback Session"),
            )
            session_id = cursor.lastrowid
            cursor = conn.execute(
                "INSERT INTO chat_messages (session_id, role, content, sources, created_at) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)",
                (session_id, role, "Feedback target", "[]"),
            )
            message_id = cursor.lastrowid
            conn.commit()
            return session_id, message_id
        finally:
            self._connection_pool.release_connection(conn)

    def _grant_member2_write_to_vault3(self):
        conn = self._get_db_conn()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO vault_members (vault_id, user_id, permission, granted_by) VALUES (?, ?, ?, ?)",
                (3, 4, "write", 1),
            )
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)

    def test_set_update_and_clear_feedback(self):
        """Owner with vault write can set, change, and clear feedback."""
        session_id, message_id = self._seed_feedback_message()

        for rating in ("up", "down", None):
            response = self.client.patch(
                f"/api/chat/sessions/{session_id}/messages/{message_id}/feedback",
                json={"rating": rating},
                headers=self._auth_headers(self._member_token()),
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["feedback"], rating)

    def test_invalid_rating_returns_422(self):
        """Only up, down, and null are accepted as feedback ratings."""
        session_id, message_id = self._seed_feedback_message()

        response = self.client.patch(
            f"/api/chat/sessions/{session_id}/messages/{message_id}/feedback",
            json={"rating": "sideways"},
            headers=self._auth_headers(self._member_token()),
        )

        self.assertEqual(response.status_code, 422)
        self.assertIn("rating must be", response.json()["detail"])

    def test_user_without_vault_write_gets_403(self):
        """Feedback mutation requires vault write access before ownership checks."""
        session_id, message_id = self._seed_feedback_message()

        response = self.client.patch(
            f"/api/chat/sessions/{session_id}/messages/{message_id}/feedback",
            json={"rating": "up"},
            headers=self._auth_headers(self._member_no_access_token()),
        )

        self.assertEqual(response.status_code, 403)
        self.assertIn("No write access", response.json()["detail"])

    def test_non_owner_with_vault_write_cannot_update_feedback(self):
        """Non-admin users cannot overwrite another user's owned feedback signal."""
        self._grant_member2_write_to_vault3()
        session_id, message_id = self._seed_feedback_message(owner_id=3)

        response = self.client.patch(
            f"/api/chat/sessions/{session_id}/messages/{message_id}/feedback",
            json={"rating": "down"},
            headers=self._auth_headers(self._member_no_access_token()),
        )

        self.assertEqual(response.status_code, 403)
        self.assertIn("another user's session", response.json()["detail"])

    def test_legacy_ownerless_session_allows_any_vault_writer(self):
        """Legacy sessions with NULL owner keep the vault-write feedback policy."""
        self._grant_member2_write_to_vault3()
        session_id, message_id = self._seed_feedback_message(owner_id=None)

        response = self.client.patch(
            f"/api/chat/sessions/{session_id}/messages/{message_id}/feedback",
            json={"rating": "up"},
            headers=self._auth_headers(self._member_no_access_token()),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["feedback"], "up")

    def test_admin_can_update_owned_session_feedback(self):
        """Admins can moderate feedback across owned sessions."""
        session_id, message_id = self._seed_feedback_message(owner_id=3)

        response = self.client.patch(
            f"/api/chat/sessions/{session_id}/messages/{message_id}/feedback",
            json={"rating": "down"},
            headers=self._auth_headers(self._admin_token()),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["feedback"], "down")

    def test_missing_session_or_message_returns_404(self):
        """Missing sessions and messages return distinct 404 responses."""
        session_id, message_id = self._seed_feedback_message()

        missing_session = self.client.patch(
            f"/api/chat/sessions/99999/messages/{message_id}/feedback",
            json={"rating": "up"},
            headers=self._auth_headers(self._member_token()),
        )
        self.assertEqual(missing_session.status_code, 404)
        self.assertIn("Session not found", missing_session.json()["detail"])

        missing_message = self.client.patch(
            f"/api/chat/sessions/{session_id}/messages/99999/feedback",
            json={"rating": "up"},
            headers=self._auth_headers(self._member_token()),
        )
        self.assertEqual(missing_message.status_code, 404)
        self.assertIn("Message not found", missing_message.json()["detail"])


if __name__ == "__main__":
    unittest.main()
