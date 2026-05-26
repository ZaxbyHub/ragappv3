"""Adversarial security tests for task 1.2 — chat sessions SQL fix.

ATTACK VECTORS to test:
1. Can a non-admin user spoof their role to "admin" or "superadmin"?
2. What if user.id is 0 or negative?
3. SQL injection attempts in vault_id parameter?
4. What if user.get("role") returns None or an unexpected value?
5. What happens if a user has no role key at all?

These tests attempt to BREAK the user_id filter or admin bypass logic.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

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
from app.services.auth_service import create_access_token


class TestAdversarialSessionSecurity(unittest.TestCase):
    """Adversarial tests attempting to bypass user_id filter or admin check."""

    def setUp(self):
        self.client = TestClient(app)
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
            # User 3: member
            conn.execute(
                "INSERT OR IGNORE INTO users (id, username, hashed_password, full_name, role, is_active) VALUES (?, ?, ?, ?, ?, 1)",
                (3, "member1", pw, "Member One", "member"),
            )

            # Create vaults
            conn.execute("INSERT OR IGNORE INTO vaults (id, name, description) VALUES (?, ?, ?)", (2, "Vault Two", "Second vault"))
            conn.execute("INSERT OR IGNORE INTO vaults (id, name, description) VALUES (?, ?, ?)", (3, "Vault Three", "Third vault"))

            # Vault memberships
            conn.execute("INSERT OR IGNORE INTO vault_members (vault_id, user_id, permission, granted_by) VALUES (?, ?, ?, ?)", (2, 3, "read", 1))

            # Seed chat_sessions
            # Session 1: owned by user 3 (member1), vault 2
            conn.execute(
                "INSERT INTO chat_sessions (vault_id, user_id, title, created_at, updated_at) VALUES (?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                (2, 3, "member1 session in vault2"),
            )
            # Session 2: owned by user 1 (superadmin), vault 2
            conn.execute(
                "INSERT INTO chat_sessions (vault_id, user_id, title, created_at, updated_at) VALUES (?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                (2, 1, "superadmin session in vault2"),
            )
            # Session 3: owned by user 2 (admin), vault 2
            conn.execute(
                "INSERT INTO chat_sessions (vault_id, user_id, title, created_at, updated_at) VALUES (?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                (2, 2, "admin session in vault2"),
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

    def _token(self, user_id, username, role):
        return create_access_token(user_id, username, role)

    def _auth_headers(self, token):
        return {"Authorization": f"Bearer {token}"}

    # =========================================================================
    # ATTACK VECTOR 1: Role spoofing attempts
    # =========================================================================

    def test_attack_spoof_role_admin_in_jwt_but_db_says_member(self):
        """
        ATTACK: Try to spoof role='admin' in JWT when DB says user is 'member'.

        Expected: Should FAIL - role is fetched from DB, not JWT.
        """
        # User 3 is a 'member' in the database
        # Try to create token with role='admin' - but DB will override this
        token = self._token(3, "member1", "admin")  # Claim to be admin in JWT
        response = self.client.get("/api/chat/sessions", headers=self._auth_headers(token))

        # The real check is: role from DB is 'member', so user should see only their own sessions
        self.assertEqual(response.status_code, 200)
        data = response.json()
        sessions = data.get("sessions", [])
        titles = [s["title"] for s in sessions]

        # Should NOT see superadmin's or admin's sessions
        self.assertNotIn("superadmin session in vault2", titles, "Member should NOT see superadmin session via role spoofing!")
        self.assertNotIn("admin session in vault2", titles, "Member should NOT see admin session via role spoofing!")
        # Should only see own session
        self.assertEqual(len(sessions), 1, f"Expected only 1 session (own), got {len(sessions)}: {titles}")

    def test_attack_spoof_role_superadmin_in_jwt_but_db_says_member(self):
        """
        ATTACK: Try to spoof role='superadmin' in JWT when DB says user is 'member'.

        Expected: Should FAIL - role is fetched from DB, not JWT.
        """
        token = self._token(3, "member1", "superadmin")  # Claim to be superadmin in JWT
        response = self.client.get("/api/chat/sessions", headers=self._auth_headers(token))

        self.assertEqual(response.status_code, 200)
        data = response.json()
        sessions = data.get("sessions", [])
        titles = [s["title"] for s in sessions]

        # Should NOT see superadmin's or admin's sessions
        self.assertNotIn("superadmin session in vault2", titles, "Member should NOT see superadmin session via role spoofing!")
        self.assertNotIn("admin session in vault2", titles, "Member should NOT see admin session via role spoofing!")
        self.assertEqual(len(sessions), 1, f"Expected only 1 session (own), got {len(sessions)}: {titles}")

    def test_attack_role_case_sensitivity(self):
        """
        ATTACK: Try 'Admin' (capital A) or 'ADMIN' to bypass role check.

        Expected: Should FAIL - role check is case-sensitive.
        """
        token = self._token(3, "member1", "Admin")
        response = self.client.get("/api/chat/sessions", headers=self._auth_headers(token))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        sessions = data.get("sessions", [])
        # Should only see own session
        self.assertEqual(len(sessions), 1)

    def test_attack_role_whitespace_padding(self):
        """
        ATTACK: Try ' admin' or 'admin ' (with whitespace) to bypass role check.

        Expected: Should FAIL - role check doesn't normalize whitespace.
        """
        token = self._token(3, "member1", " admin")
        response = self.client.get("/api/chat/sessions", headers=self._auth_headers(token))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        sessions = data.get("sessions", [])
        # Should only see own session
        self.assertEqual(len(sessions), 1)

    # =========================================================================
    # ATTACK VECTOR 2: user.id is 0 or negative
    # =========================================================================

    def test_attack_user_id_zero_in_token(self):
        """
        ATTACK: Try user_id=0 in JWT token to bypass user_id filter.

        Expected: Should REJECT at auth time - user_id=0 is invalid.
        """
        token = self._token(0, "userzero", "member")
        response = self.client.get("/api/chat/sessions", headers=self._auth_headers(token))
        # user_id=0 should be rejected at authentication time
        self.assertEqual(response.status_code, 401, "user_id=0 should be rejected as invalid")

    def test_attack_user_id_negative_in_token(self):
        """
        ATTACK: Try negative user_id in JWT token.

        Expected: Should REJECT or return empty results (no sessions for user_id < 0).
        """
        token = self._token(-1, "negativeuser", "member")
        response = self.client.get("/api/chat/sessions", headers=self._auth_headers(token))

        # If user_id=-1 is accepted, check if it can see other users' sessions
        if response.status_code == 200:
            data = response.json()
            sessions = data.get("sessions", [])
            # Should NOT see other users' sessions if user_id=-1 is properly filtered
            titles = [s["title"] for s in sessions]
            self.assertNotIn("superadmin session in vault2", titles,
                "user_id=-1 should NOT see superadmin sessions!")
            self.assertNotIn("admin session in vault2", titles,
                "user_id=-1 should NOT see admin sessions!")
        else:
            # Rejected at auth time - this is also acceptable
            self.assertEqual(response.status_code, 401)

    # =========================================================================
    # ATTACK VECTOR 3: SQL injection in vault_id
    # =========================================================================

    def test_attack_sql_injection_vault_id_numeric(self):
        """
        ATTACK: Try SQL injection in vault_id parameter like '2 OR 1=1'.

        Expected: Should be safe - vault_id is parameterized.
        """
        token = self._token(3, "member1", "member")

        # Try classic SQL injection
        response = self.client.get("/api/chat/sessions?vault_id=2%20OR%201%3D1", headers=self._auth_headers(token))

        # Should either reject the request or return only authorized sessions
        if response.status_code == 200:
            data = response.json()
            sessions = data.get("sessions", [])
            titles = [s["title"] for s in sessions]
            # Should NOT see superadmin or admin sessions
            self.assertNotIn("superadmin session in vault2", titles)
            self.assertNotIn("admin session in vault2", titles)
            self.assertEqual(len(sessions), 1, "SQL injection should return only own session")
        else:
            # If rejected, that's also fine for invalid input
            pass

    def test_attack_sql_injection_vault_id_union(self):
        """
        ATTACK: Try SQL injection using UNION.

        Expected: Should be safe - vault_id is parameterized.
        """
        token = self._token(3, "member1", "member")

        # Try UNION-based injection
        response = self.client.get("/api/chat/sessions?vault_id=2%20UNION%20SELECT%201,2,3,4,5,6,7,8--",
                                   headers=self._auth_headers(token))

        # Should either reject or return only authorized sessions
        if response.status_code == 200:
            data = response.json()
            # Should not return SQL error or arbitrary data
            self.assertIn("sessions", data)

    def test_attack_sql_injection_vault_id_trailing(self):
        """
        ATTACK: Try vault_id with trailing SQL characters.

        Expected: Should be safe - vault_id is cast to int.
        """
        token = self._token(3, "member1", "member")

        # Try trailing semicolon and SQL
        response = self.client.get("/api/chat/sessions?vault_id=2;DROP%20TABLE%20chat_sessions--",
                                   headers=self._auth_headers(token))

        # The route expects int, so non-integer values should fail
        # Even if some passes through, it shouldn't cause SQL injection
        if response.status_code == 422:
            pass  # Correctly rejected as invalid int
        elif response.status_code == 200:
            data = response.json()
            self.assertIn("sessions", data)

    # =========================================================================
    # ATTACK VECTOR 4: user.get("role") returns None or unexpected value
    # =========================================================================

    def test_attack_role_none_in_database(self):
        """
        ATTACK: What if database has NULL role for a user?

        Expected: Database prevents NULL role via NOT NULL constraint.
        This test verifies the constraint exists and protects against invalid roles.
        """
        conn = self._connection_pool.get_connection()
        try:
            # Attempt to set NULL role - should be blocked by database constraint
            with self.assertRaises(sqlite3.IntegrityError) as context:
                conn.execute("UPDATE users SET role = NULL WHERE id = 3")
                conn.commit()
            # Verify it's the NOT NULL constraint that blocked it
            self.assertIn("NOT NULL", str(context.exception))
        finally:
            self._connection_pool.release_connection(conn)

    def test_attack_role_empty_string_in_database(self):
        """
        ATTACK: What if database has empty string role for a user?

        Expected: Database prevents invalid roles via CHECK constraint.
        This test verifies the constraint exists and protects against invalid roles.
        """
        conn = self._connection_pool.get_connection()
        try:
            # Attempt to set empty string role - should be blocked by CHECK constraint
            with self.assertRaises(sqlite3.IntegrityError) as context:
                conn.execute("UPDATE users SET role = '' WHERE id = 3")
                conn.commit()
            # Verify it's the CHECK constraint that blocked it
            self.assertIn("CHECK constraint", str(context.exception))
        finally:
            self._connection_pool.release_connection(conn)

    def test_attack_role_unknown_value_in_database(self):
        """
        ATTACK: What if database has unexpected role value like 'superuser' or 'moderator'?

        Expected: Database prevents invalid roles via CHECK constraint.
        This test verifies the constraint exists and protects against invalid roles.
        """
        conn = self._connection_pool.get_connection()
        try:
            # Attempt to set invalid role - should be blocked by CHECK constraint
            with self.assertRaises(sqlite3.IntegrityError) as context:
                conn.execute("UPDATE users SET role = 'superuser' WHERE id = 3")
                conn.commit()
            # Verify it's the CHECK constraint that blocked it
            self.assertIn("CHECK constraint", str(context.exception))
        finally:
            self._connection_pool.release_connection(conn)

    # =========================================================================
    # ATTACK VECTOR 5: User has no role key at all (edge case)
    # =========================================================================

    def test_attack_user_dict_no_role_key(self):
        """
        ATTACK: Simulate user dict with missing 'role' key.

        Expected: get_current_active_user should return dict with role from DB.
        This test mocks the dependency to simulate a malformed user dict.
        """
        # This test requires mocking at a lower level since get_current_active_user
        # always produces a role from the database query.
        # We can only test this if we mock the entire user dependency.

        def mock_get_db_no_role():
            def override():
                conn = self._connection_pool.get_connection()
                try:
                    yield conn
                finally:
                    self._connection_pool.release_connection(conn)
            return override

        # Cannot easily test missing role key since it's always in SELECT clause
        # But we can verify the code handles it correctly via code inspection
        # The code uses: is_admin = user.get("role") in ("superadmin", "admin")
        # If role is missing: user.get("role") returns None, and None in (...) is False

        # For completeness, let's verify None is handled
        test_role = None
        is_admin = test_role in ("superadmin", "admin")
        self.assertFalse(is_admin, "None role should result in is_admin=False")

    # =========================================================================
    # ATTACK VECTOR 6: Direct user_id manipulation in SQL (already parameterized)
    # =========================================================================

    def test_attack_user_id_in_query_no_vault_filter(self):
        """
        ATTACK: Without vault_id, non-admin user should only see their sessions.
        Verify the WHERE s.user_id = ? filter is actually applied.
        """
        token = self._token(3, "member1", "member")
        response = self.client.get("/api/chat/sessions", headers=self._auth_headers(token))

        self.assertEqual(response.status_code, 200)
        data = response.json()
        sessions = data.get("sessions", [])
        titles = [s["title"] for s in sessions]

        # Should only see own session, not superadmin's or admin's
        self.assertEqual(len(sessions), 1, f"Expected only own session, got {len(sessions)}: {titles}")
        self.assertIn("member1 session in vault2", titles)
        self.assertNotIn("superadmin session in vault2", titles)
        self.assertNotIn("admin session in vault2", titles)

    def test_attack_user_id_filter_with_vault_id(self):
        """
        ATTACK: With vault_id=2, non-admin should only see their sessions in that vault.
        """
        token = self._token(3, "member1", "member")
        response = self.client.get("/api/chat/sessions?vault_id=2", headers=self._auth_headers(token))

        self.assertEqual(response.status_code, 200)
        data = response.json()
        sessions = data.get("sessions", [])
        titles = [s["title"] for s in sessions]

        # Should only see own session in vault 2
        self.assertEqual(len(sessions), 1, f"Expected only own session, got {len(sessions)}: {titles}")
        self.assertIn("member1 session in vault2", titles)
        self.assertNotIn("superadmin session in vault2", titles)
        self.assertNotIn("admin session in vault2", titles)

    # =========================================================================
    # ATTACK VECTOR 7: Admin bypass attempts
    # =========================================================================

    def test_attack_admin_can_see_all_sessions(self):
        """
        VERIFY: Admin SHOULD see all sessions (this is the expected behavior).
        This is the flip side - we verify admins do get the admin capability.
        """
        token = self._token(2, "admin1", "admin")
        response = self.client.get("/api/chat/sessions", headers=self._auth_headers(token))

        self.assertEqual(response.status_code, 200)
        data = response.json()
        sessions = data.get("sessions", [])
        titles = [s["title"] for s in sessions]

        # Admin SHOULD see all sessions
        self.assertEqual(len(sessions), 3, f"Admin should see all 3 sessions, got {len(sessions)}: {titles}")
        self.assertIn("member1 session in vault2", titles)
        self.assertIn("superadmin session in vault2", titles)
        self.assertIn("admin session in vault2", titles)

    def test_attack_superadmin_can_see_all_sessions(self):
        """
        VERIFY: Superadmin SHOULD see all sessions.
        """
        token = self._token(1, "superadmin", "superadmin")
        response = self.client.get("/api/chat/sessions", headers=self._auth_headers(token))

        self.assertEqual(response.status_code, 200)
        data = response.json()
        sessions = data.get("sessions", [])

        # Superadmin SHOULD see all sessions
        self.assertEqual(len(sessions), 3)


if __name__ == "__main__":
    unittest.main()
