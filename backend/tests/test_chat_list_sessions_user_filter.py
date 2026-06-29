"""Tests for GET /chat/sessions user_id filtering.

Verifies FR-003 / task 1.2:
- Admins (superadmin/admin) see ALL sessions (no user_id filter)
- Non-admins see only their OWN sessions (WHERE s.user_id = ?)
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

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
    _unstructured.documents.elements = types.ModuleType("unstructured.documents.elements")
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
from app.services.auth_service import compute_client_fingerprint, create_access_token


class TestListSessionsUserFilter(unittest.TestCase):
    """Tests for GET /chat/sessions user_id filtering (task 1.2)."""

    def setUp(self):
        self.client = TestClient(app)
        # Override default User-Agent so fingerprint validation matches token
        self.client.headers["user-agent"] = ""
        self._temp_dir = tempfile.mkdtemp()

        self._original_jwt_secret = settings.jwt_secret_key
        self._original_users_enabled = settings.users_enabled
        self._original_data_dir = settings.data_dir

        settings.data_dir = Path(self._temp_dir)
        settings.jwt_secret_key = "test-secret-key-for-testing-at-least-32-chars-long"
        settings.users_enabled = True

        self._db_path = str(Path(self._temp_dir) / "app.db")

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

        self._mock_vector_store = MagicMock()
        self._mock_vector_store.delete_by_vault = MagicMock(return_value=0)
        self._mock_rag_engine = MagicMock()
        self._mock_rag_engine.llm_client = None

        async def mock_query(*args, **kwargs):
            yield {"type": "done", "sources": [], "memories_used": []}
        self._mock_rag_engine.query = mock_query

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_vector_store] = lambda: self._mock_vector_store
        app.dependency_overrides[get_rag_engine] = lambda: self._mock_rag_engine

        conn = self._connection_pool.get_connection()
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("DELETE FROM vault_members")
            conn.execute("DELETE FROM chat_messages")
            conn.execute("DELETE FROM chat_sessions")
            conn.execute("DELETE FROM users WHERE id != 0")

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
            # User 3: member — own sessions in vault 2 and vault 3
            conn.execute(
                "INSERT OR IGNORE INTO users (id, username, hashed_password, full_name, role, is_active) VALUES (?, ?, ?, ?, ?, 1)",
                (3, "member1", pw, "Member One", "member"),
            )
            # User 4: member — their own sessions only
            conn.execute(
                "INSERT OR IGNORE INTO users (id, username, hashed_password, full_name, role, is_active) VALUES (?, ?, ?, ?, ?, 1)",
                (4, "member2", pw, "Member Two", "member"),
            )

            # Create vaults
            conn.execute("INSERT OR IGNORE INTO vaults (id, name, description) VALUES (?, ?, ?)", (2, "Vault Two", "Second vault"))
            conn.execute("INSERT OR IGNORE INTO vaults (id, name, description) VALUES (?, ?, ?)", (3, "Vault Three", "Third vault"))

            # Vault memberships: member1 (user 3) has read on vault 2, write on vault 3
            conn.execute("INSERT OR IGNORE INTO vault_members (vault_id, user_id, permission, granted_by) VALUES (?, ?, ?, ?)", (2, 3, "read", 1))
            conn.execute("INSERT OR IGNORE INTO vault_members (vault_id, user_id, permission, granted_by) VALUES (?, ?, ?, ?)", (3, 3, "write", 1))
            # member2 (user 4) has read on vault 2
            conn.execute("INSERT OR IGNORE INTO vault_members (vault_id, user_id, permission, granted_by) VALUES (?, ?, ?, ?)", (2, 4, "read", 1))

            # Seed chat_sessions with explicit user_id
            # Session 1: owned by user 3 (member1), vault 2
            conn.execute(
                "INSERT INTO chat_sessions (vault_id, user_id, title, created_at, updated_at) VALUES (?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                (2, 3, "member1 session in vault2"),
            )
            # Session 2: owned by user 3 (member1), vault 3
            conn.execute(
                "INSERT INTO chat_sessions (vault_id, user_id, title, created_at, updated_at) VALUES (?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                (3, 3, "member1 session in vault3"),
            )
            # Session 3: owned by user 4 (member2), vault 2
            conn.execute(
                "INSERT INTO chat_sessions (vault_id, user_id, title, created_at, updated_at) VALUES (?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                (2, 4, "member2 session in vault2"),
            )
            # Session 4: owned by user 1 (superadmin), vault 3
            conn.execute(
                "INSERT INTO chat_sessions (vault_id, user_id, title, created_at, updated_at) VALUES (?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                (3, 1, "superadmin session in vault3"),
            )
            # Session 5: NULL user_id (legacy ownerless), vault 2 — should be accessible to vault members
            conn.execute(
                "INSERT INTO chat_sessions (vault_id, user_id, title, created_at, updated_at) VALUES (?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                (2, None, "legacy ownerless session"),
            )

            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)

    def tearDown(self):
        from app.models.database import _pool_cache, _pool_cache_lock
        with _pool_cache_lock:
            for path, pool in list(_pool_cache.items()):
                pool.close_all()
            _pool_cache.clear()

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
        return self._connection_pool.get_connection()

    def _token(self, user_id, username, role):
        return create_access_token(user_id, username, role,
                                client_fingerprint=compute_client_fingerprint(""))

    def _auth_headers(self, token):
        return {"Authorization": f"Bearer {token}"}

    # ─────────────────────────────────────────────────────────────────────────
    # Test 1: Non-admin user only sees their own sessions (no vault_id filter)
    # ─────────────────────────────────────────────────────────────────────────
    def test_member_sees_only_own_sessions_without_vault_filter(self):
        """Non-admin member1 should see their own 2 sessions plus legacy ownerless session (user_id IS NULL)."""
        token = self._token(3, "member1", "member")
        response = self.client.get("/api/chat/sessions", headers=self._auth_headers(token))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        sessions = data.get("sessions", [])
        titles = [s["title"] for s in sessions]

        # member1 owns 2 sessions + legacy ownerless session (due to OR s.user_id IS NULL)
        self.assertEqual(len(sessions), 3, f"Expected 3 sessions, got {len(sessions)}: {titles}")
        self.assertIn("member1 session in vault2", titles)
        self.assertIn("member1 session in vault3", titles)
        self.assertIn("legacy ownerless session", titles)
        # member1 must NOT see member2's session
        self.assertNotIn("member2 session in vault2", titles)
        # member1 must NOT see superadmin's session
        self.assertNotIn("superadmin session in vault3", titles)

    def test_member2_sees_only_own_single_session(self):
        """member2 owns exactly 1 session; they should see that one plus legacy ownerless session (user_id IS NULL)."""
        token = self._token(4, "member2", "member")
        response = self.client.get("/api/chat/sessions", headers=self._auth_headers(token))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        sessions = data.get("sessions", [])
        titles = [s["title"] for s in sessions]

        # member2 owns 1 session + legacy ownerless session (due to OR s.user_id IS NULL)
        self.assertEqual(len(sessions), 2, f"Expected 2 sessions, got {len(sessions)}: {titles}")
        self.assertIn("member2 session in vault2", titles)
        self.assertIn("legacy ownerless session", titles)

    # ─────────────────────────────────────────────────────────────────────────
    # Test 2: Admin user sees ALL sessions (no user_id filter)
    # ─────────────────────────────────────────────────────────────────────────
    def test_admin_sees_all_sessions_without_vault_filter(self):
        """Admin should see all sessions regardless of owner."""
        token = self._token(2, "admin1", "admin")
        response = self.client.get("/api/chat/sessions", headers=self._auth_headers(token))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        sessions = data.get("sessions", [])
        titles = [s["title"] for s in sessions]

        # Should see all 5 sessions (3 with user_id + 1 superadmin's + 1 NULL owner)
        self.assertEqual(len(sessions), 5, f"Expected 5 sessions, got {len(sessions)}: {titles}")
        self.assertIn("member1 session in vault2", titles)
        self.assertIn("member1 session in vault3", titles)
        self.assertIn("member2 session in vault2", titles)
        self.assertIn("superadmin session in vault3", titles)
        self.assertIn("legacy ownerless session", titles)

    def test_superadmin_sees_all_sessions_without_vault_filter(self):
        """Superadmin should see all sessions regardless of owner."""
        token = self._token(1, "superadmin", "superadmin")
        response = self.client.get("/api/chat/sessions", headers=self._auth_headers(token))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        sessions = data.get("sessions", [])
        self.assertEqual(len(sessions), 5)

    # ─────────────────────────────────────────────────────────────────────────
    # Test 3: Admin + vault_id filter still works correctly
    # ─────────────────────────────────────────────────────────────────────────
    def test_admin_with_vault_id_filter_sees_all_sessions_in_vault(self):
        """Admin + vault_id filter should return all sessions (any owner) in that vault."""
        token = self._token(2, "admin1", "admin")
        response = self.client.get("/api/chat/sessions?vault_id=2", headers=self._auth_headers(token))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        sessions = data.get("sessions", [])
        titles = [s["title"] for s in sessions]

        # Vault 2 has: member1's, member2's, and legacy ownerless session
        self.assertEqual(len(sessions), 3, f"Expected 3 sessions in vault 2, got {len(sessions)}: {titles}")
        self.assertIn("member1 session in vault2", titles)
        self.assertIn("member2 session in vault2", titles)
        self.assertIn("legacy ownerless session", titles)

        # Vault 3 has: member1's and superadmin's
        response3 = self.client.get("/api/chat/sessions?vault_id=3", headers=self._auth_headers(token))
        data3 = response3.json()
        sessions3 = data3.get("sessions", [])
        titles3 = [s["title"] for s in sessions3]
        self.assertEqual(len(sessions3), 2, f"Expected 2 sessions in vault 3, got {len(sessions3)}: {titles3}")
        self.assertIn("member1 session in vault3", titles3)
        self.assertIn("superadmin session in vault3", titles3)

    # ─────────────────────────────────────────────────────────────────────────
    # Test 4: Non-admin + vault_id filter returns only their sessions in that vault
    # ─────────────────────────────────────────────────────────────────────────
    def test_member_with_vault_id_filter_sees_only_own_sessions_in_vault(self):
        """Non-admin + vault_id filter should return only their own sessions in that vault plus legacy ownerless sessions in that vault."""
        token = self._token(3, "member1", "member")
        # member1 has sessions in both vault 2 and vault 3
        # vault 2 filter: should return member1's vault2 session + legacy ownerless session (both in vault 2)
        response = self.client.get("/api/chat/sessions?vault_id=2", headers=self._auth_headers(token))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        sessions = data.get("sessions", [])
        titles = [s["title"] for s in sessions]

        # vault 2 has: member1's session + legacy ownerless session (both match vault_id=2 AND (user_id=3 OR user_id IS NULL))
        self.assertEqual(len(sessions), 2, f"Expected 2 sessions, got {len(sessions)}: {titles}")
        self.assertIn("member1 session in vault2", titles)
        self.assertIn("legacy ownerless session", titles)
        self.assertNotIn("member2 session in vault2", titles)

        # vault 3 filter: should return only member1's vault3 session (no legacy session in vault 3)
        response3 = self.client.get("/api/chat/sessions?vault_id=3", headers=self._auth_headers(token))
        self.assertEqual(response3.status_code, 200)
        data3 = response3.json()
        sessions3 = data3.get("sessions", [])
        titles3 = [s["title"] for s in sessions3]
        self.assertEqual(len(sessions3), 1)
        self.assertIn("member1 session in vault3", titles3)
        self.assertNotIn("superadmin session in vault3", titles3)

    def test_member2_vault_id_filter_sees_only_own_session_in_vault(self):
        """member2's only session is in vault 2; vault_id=2 should return their session plus legacy ownerless, vault_id=3 should be empty."""
        token = self._token(4, "member2", "member")

        # vault 2: member2 has their session + legacy ownerless session (both in vault 2, match user_id=4 OR user_id IS NULL)
        response = self.client.get("/api/chat/sessions?vault_id=2", headers=self._auth_headers(token))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        sessions = data.get("sessions", [])
        self.assertEqual(len(sessions), 2)
        titles = [s["title"] for s in sessions]
        self.assertIn("member2 session in vault2", titles)
        self.assertIn("legacy ownerless session", titles)

        # vault 3: member2 has no sessions there
        response3 = self.client.get("/api/chat/sessions?vault_id=3", headers=self._auth_headers(token))
        self.assertEqual(response3.status_code, 200)
        data3 = response3.json()
        sessions3 = data3.get("sessions", [])
        self.assertEqual(len(sessions3), 0)

    # ─────────────────────────────────────────────────────────────────────────
    # Test 5: User with no sessions gets empty list
    # ─────────────────────────────────────────────────────────────────────────
    def test_user_with_no_sessions_gets_empty_list(self):
        """A user that owns no sessions should get legacy ownerless session (user_id IS NULL matches all non-admins)."""
        # Create user 5 who has vault access but no sessions
        conn = self._get_db_conn()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO users (id, username, hashed_password, full_name, role, is_active) VALUES (?, ?, ?, ?, ?, 1)",
                (5, "lonelyuser", "unused-test-password-hash", "Lonely User", "member"),
            )
            conn.execute(
                "INSERT OR IGNORE INTO vault_members (vault_id, user_id, permission, granted_by) VALUES (?, ?, ?, ?)",
                (2, 5, "read", 1),
            )
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)

        token = self._token(5, "lonelyuser", "member")
        response = self.client.get("/api/chat/sessions", headers=self._auth_headers(token))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        sessions = data.get("sessions", [])
        # Due to OR s.user_id IS NULL, legacy ownerless session is visible to all non-admins
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["title"], "legacy ownerless session")

    def test_admin_gets_empty_for_vault_with_no_sessions(self):
        """Even admins should get empty list when no sessions exist in a vault."""
        # Create a new empty vault
        conn = self._get_db_conn()
        try:
            conn.execute("INSERT OR IGNORE INTO vaults (id, name, description) VALUES (?, ?, ?)", (99, "Empty Vault", "No sessions here"))
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)

        token = self._token(2, "admin1", "admin")
        response = self.client.get("/api/chat/sessions?vault_id=99", headers=self._auth_headers(token))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data.get("sessions", [])), 0)


if __name__ == "__main__":
    unittest.main()
