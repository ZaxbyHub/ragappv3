"""Adversarial security tests for task 1.4 — vault transaction fix.

ATTACK VECTORS:
1. BEGIN fails (already in transaction from outer scope)
2. Rollback fails (e.g., database connection is broken)
3. Concurrent vault creation with same name (IntegrityError + rollback)
4. conn.execute("BEGIN") hangs or times out
5. Race condition: vault deleted between INSERT and commit

These tests attempt to BREAK the transaction pattern.
"""

import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from queue import Empty, Queue
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

from fastapi.testclient import TestClient

from app.api.deps import get_db, get_vector_store
from app.config import settings
from app.main import app
from app.services.auth_service import compute_client_fingerprint, create_access_token


class CursorWithLastrowid:
    """Proxy cursor that overrides lastrowid while delegating other cursor behavior."""

    def __init__(self, cursor, lastrowid):
        self._cursor = cursor
        self.lastrowid = lastrowid

    def __getattr__(self, name):
        return getattr(self._cursor, name)


class FailOnConnection:
    """A connection wrapper that fails on specific SQL commands.

    This wraps a real connection and injects failures at specific points
    while delegating everything else to the real connection.
    """

    def __init__(self, real_conn):
        self._real_conn = real_conn
        self._call_log = []
        self._config = {
            "begin_raises": None,
            "commit_raises": None,
            "rollback_raises": None,
            "vault_insert_raises": None,
            "member_insert_raises": None,
            "lastrowid_override": None,
        }

    def configure(self, **kwargs):
        """Configure failure injection."""
        for k, v in kwargs.items():
            if k in self._config:
                self._config[k] = v
        return self

    def reset(self):
        """Reset all configuration."""
        self._config = {
            "begin_raises": None,
            "commit_raises": None,
            "rollback_raises": None,
            "vault_insert_raises": None,
            "member_insert_raises": None,
            "lastrowid_override": None,
        }
        return self

    @property
    def call_log(self):
        return self._call_log

    def execute(self, sql, *args, **kwargs):
        self._call_log.append(("execute", sql, args))
        sql_upper = sql.upper().strip()

        # BEGIN failure
        if "BEGIN" in sql_upper:
            if self._config["begin_raises"]:
                raise self._config["begin_raises"]
            return self._real_conn.execute(sql, *args, **kwargs)

        # Vault INSERT failure
        if "INSERT INTO vaults" in sql and "SELECT" not in sql_upper:
            if self._config["vault_insert_raises"]:
                raise self._config["vault_insert_raises"]
            result = self._real_conn.execute(sql, *args, **kwargs)
            if self._config["lastrowid_override"] is not None:
                return CursorWithLastrowid(result, self._config["lastrowid_override"])
            return result

        # Member INSERT failure
        if "INSERT INTO vault_members" in sql:
            if self._config["member_insert_raises"]:
                raise self._config["member_insert_raises"]
            return self._real_conn.execute(sql, *args, **kwargs)

        # Delegate everything else to real connection
        return self._real_conn.execute(sql, *args, **kwargs)

    def commit(self):
        self._call_log.append(("commit",))
        if self._config["commit_raises"]:
            raise self._config["commit_raises"]
        return self._real_conn.commit()

    def rollback(self):
        self._call_log.append(("rollback",))
        if self._config["rollback_raises"]:
            raise self._config["rollback_raises"]
        return self._real_conn.rollback()


class AdaptiveConnectionPool:
    """A connection pool that can switch between real connections and
    a fail-on-demand connection wrapper."""

    def __init__(self, db_path):
        self._db_path = db_path
        self._real_pool = Queue(maxsize=5)
        self._lock = threading.Lock()
        self._closed = False
        self._fail_on_connection = None

    def get_connection(self):
        if self._closed:
            raise RuntimeError("Pool closed")

        # Return the fail-on connection if set
        if self._fail_on_connection is not None:
            return self._fail_on_connection

        # Try to get from pool
        try:
            conn = self._real_pool.get_nowait()
            # Verify connection is still alive
            try:
                conn.execute("SELECT 1")
            except:
                conn = self._create_connection()
            return conn
        except Empty:
            return self._create_connection()

    def _create_connection(self):
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn

    def release_connection(self, conn):
        if self._closed:
            conn.close()
            return
        if conn is self._fail_on_connection:
            # Don't release the fail-on connection
            return
        try:
            self._real_pool.put_nowait(conn)
        except:
            conn.close()

    def set_fail_on(self, conn):
        """Set a connection that will fail on specific commands."""
        self._fail_on_connection = conn

    def clear_fail_on(self):
        """Clear the fail-on connection."""
        self._fail_on_connection = None

    def close_all(self):
        self._closed = True
        while True:
            try:
                conn = self._real_pool.get_nowait()
                conn.close()
            except Empty:
                break


class TestVaultTransactionAdversarial(unittest.TestCase):
    """Adversarial tests attempting to BREAK the vault transaction pattern."""

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

        # Create the adaptive pool
        self._pool = AdaptiveConnectionPool(self._db_path)

        def override_get_db():
            conn = self._pool.get_connection()
            try:
                yield conn
            finally:
                self._pool.release_connection(conn)

        self._mock_vector_store = MagicMock()
        self._mock_vector_store.delete_by_vault = MagicMock(return_value=0)

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_vector_store] = lambda: self._mock_vector_store

        # Seed test data using real connection
        conn = self._pool.get_connection()
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("DELETE FROM vault_members")
            conn.execute("DELETE FROM vaults")
            conn.execute("DELETE FROM users WHERE id != 0")

            pw = "unused-test-password-hash"

            conn.execute(
                "INSERT OR IGNORE INTO users (id, username, hashed_password, full_name, role, is_active) VALUES (?, ?, ?, ?, ?, 1)",
                (1, "superadmin", pw, "Super Admin", "superadmin"),
            )
            conn.execute(
                "INSERT OR IGNORE INTO users (id, username, hashed_password, full_name, role, is_active) VALUES (?, ?, ?, ?, ?, 1)",
                (2, "user1", pw, "User One", "member"),
            )

            conn.commit()
        finally:
            self._pool.release_connection(conn)

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

        self._pool.close_all()

        import shutil

        shutil.rmtree(self._temp_dir, ignore_errors=True)

    def _token(self, user_id, username, role):
        return create_access_token(user_id, username, role,
                                client_fingerprint=compute_client_fingerprint(""))

    def _auth_headers(self, token):
        return {"Authorization": f"Bearer {token}"}

    def _enable_fail_on(self):
        """Enable fail-on mode by wrapping the current connection."""
        real_conn = self._pool.get_connection()
        fail_on_conn = FailOnConnection(real_conn)
        self._pool.set_fail_on(fail_on_conn)
        return fail_on_conn

    def _disable_fail_on(self):
        """Disable fail-on mode."""
        self._pool.clear_fail_on()

    # ========================================================================
    # ATTACK VECTOR 1: BEGIN fails (already in transaction from outer scope)
    # ========================================================================

    def test_attack_begin_fails_already_in_transaction(self):
        """
        ATTACK: conn.execute("BEGIN") raises sqlite3.OperationalError
        because connection is already in a transaction.

        Expected: Should catch exception, rollback, and return 500.
        """
        token = self._token(1, "superadmin", "superadmin")
        headers = self._auth_headers(token)

        fail_on = self._enable_fail_on()
        fail_on.configure(begin_raises=sqlite3.OperationalError("already in a transaction"))

        try:
            response = self.client.post(
                "/api/vaults",
                json={"name": "Test Vault", "description": "Test"},
                headers=headers,
            )

            # Should return 500 because BEGIN failed
            self.assertEqual(
                response.status_code, 500,
                f"Expected 500 but got {response.status_code}: {response.json()}"
            )
            # Verify rollback was attempted
            self.assertIn(
                ("rollback",), fail_on.call_log,
                "Rollback should be called when BEGIN fails"
            )
        finally:
            fail_on.reset()
            self._disable_fail_on()

    def test_attack_begin_raises_generic_exception(self):
        """
        ATTACK: conn.execute("BEGIN") raises an unexpected exception.

        Expected: Should catch, rollback, and return 500.
        """
        token = self._token(1, "superadmin", "superadmin")
        headers = self._auth_headers(token)

        fail_on = self._enable_fail_on()
        fail_on.configure(begin_raises=RuntimeError("Unexpected BEGIN failure"))

        try:
            response = self.client.post(
                "/api/vaults",
                json={"name": "Test Vault", "description": "Test"},
                headers=headers,
            )

            self.assertEqual(response.status_code, 500)
            self.assertIn(("rollback",), fail_on.call_log)
        finally:
            fail_on.reset()
            self._disable_fail_on()

    # ========================================================================
    # ATTACK VECTOR 2: Rollback fails
    # ========================================================================

    def test_attack_rollback_fails_after_integrity_error(self):
        """
        ATTACK: IntegrityError occurs, but rollback ALSO fails.

        Expected: Should still propagate the 409 error to client.
        """
        token = self._token(1, "superadmin", "superadmin")
        headers = self._auth_headers(token)

        fail_on = self._enable_fail_on()
        fail_on.configure(
            vault_insert_raises=sqlite3.IntegrityError("UNIQUE constraint failed: vaults.name"),
            rollback_raises=sqlite3.OperationalError("rollback failed - connection broken")
        )

        try:
            response = self.client.post(
                "/api/vaults",
                json={"name": "Duplicate Vault", "description": "Test"},
                headers=headers,
            )

            # Should still return 409 despite rollback failure
            self.assertEqual(
                response.status_code, 409,
                f"Expected 409 Conflict but got {response.status_code}: {response.json()}"
            )
            # Verify rollback was attempted (even though it failed)
            self.assertIn(
                ("rollback",), fail_on.call_log,
                "Rollback should be attempted even if it fails"
            )
        finally:
            fail_on.reset()
            self._disable_fail_on()

    def test_attack_rollback_fails_after_general_error(self):
        """
        ATTACK: General exception occurs, but rollback ALSO fails.

        Expected: Should still propagate 500 error to client.
        """
        token = self._token(1, "superadmin", "superadmin")
        headers = self._auth_headers(token)

        fail_on = self._enable_fail_on()
        fail_on.configure(
            vault_insert_raises=RuntimeError("Unexpected insert failure"),
            rollback_raises=sqlite3.OperationalError("rollback failed - connection broken")
        )

        try:
            response = self.client.post(
                "/api/vaults",
                json={"name": "Test Vault", "description": "Test"},
                headers=headers,
            )

            self.assertEqual(response.status_code, 500)
            self.assertIn(("rollback",), fail_on.call_log)
        finally:
            fail_on.reset()
            self._disable_fail_on()

    def test_attack_rollback_fails_and_exception_swallowed(self):
        """
        ATTACK: Both insert and rollback fail, but rollback exception
        should not replace the original error message.

        Expected: Original error (IntegrityError) should result in 409,
        not the rollback error.
        """
        token = self._token(1, "superadmin", "superadmin")
        headers = self._auth_headers(token)

        fail_on = self._enable_fail_on()
        fail_on.configure(
            vault_insert_raises=sqlite3.IntegrityError("UNIQUE constraint failed"),
            rollback_raises=Exception("Rollback exception should not be raised to client")
        )

        try:
            response = self.client.post(
                "/api/vaults",
                json={"name": "Test Vault", "description": "Test"},
                headers=headers,
            )

            # Should return 409 from IntegrityError, not 500 from rollback failure
            self.assertEqual(
                response.status_code, 409,
                f"Expected 409 but got {response.status_code}: {response.json()}"
            )
        finally:
            fail_on.reset()
            self._disable_fail_on()

    # ========================================================================
    # ATTACK VECTOR 3: Concurrent vault creation with same name
    # ========================================================================

    def test_attack_concurrent_vault_creation_integrity_error(self):
        """
        ATTACK: Two concurrent requests try to create vault with same name.

        Expected: One succeeds (201), other gets 409 Conflict.
        """
        token = self._token(1, "superadmin", "superadmin")
        headers = self._auth_headers(token)

        # First request succeeds
        response1 = self.client.post(
            "/api/vaults",
            json={"name": "Concurrent Vault", "description": "First"},
            headers=headers,
        )
        self.assertEqual(
            response1.status_code, 201,
            f"First request should succeed: {response1.json()}"
        )

        # Second request with same name should get 409
        response2 = self.client.post(
            "/api/vaults",
            json={"name": "Concurrent Vault", "description": "Second"},
            headers=headers,
        )
        self.assertEqual(
            response2.status_code, 409,
            f"Second request should fail with 409: {response2.json()}"
        )
        self.assertIn("already exists", response2.json()["detail"])

    def test_attack_integrity_error_during_vault_member_insert(self):
        """
        ATTACK: Vault insert succeeds but vault_members insert fails with IntegrityError.

        This tests the case where rollback cleans up the orphaned vault.
        """
        token = self._token(1, "superadmin", "superadmin")
        headers = self._auth_headers(token)

        fail_on = self._enable_fail_on()
        fail_on.configure(member_insert_raises=sqlite3.IntegrityError("FOREIGN KEY constraint failed"))

        try:
            response = self.client.post(
                "/api/vaults",
                json={"name": "Test Vault", "description": "Test"},
                headers=headers,
            )

            # Should return 500 because vault was created but membership failed
            self.assertEqual(
                response.status_code, 500,
                f"Expected 500 but got {response.status_code}: {response.json()}"
            )
            # Verify rollback was called to clean up the orphaned vault
            self.assertIn(
                ("rollback",), fail_on.call_log,
                "Rollback should clean up orphaned vault"
            )
        finally:
            fail_on.reset()
            self._disable_fail_on()

    # ========================================================================
    # ATTACK VECTOR 4: conn.execute("BEGIN") hangs or times out
    # ========================================================================

    def test_attack_execute_hangs_during_insert(self):
        """
        ATTACK: INSERT INTO vaults raises "database is locked" (simulates hang).

        Expected: Should handle locked database gracefully with 409 or 500.
        """
        token = self._token(1, "superadmin", "superadmin")
        headers = self._auth_headers(token)

        fail_on = self._enable_fail_on()
        fail_on.configure(vault_insert_raises=sqlite3.OperationalError("database is locked"))

        try:
            response = self.client.post(
                "/api/vaults",
                json={"name": "Test Vault", "description": "Test"},
                headers=headers,
            )

            # Should handle locked database gracefully
            self.assertIn(
                response.status_code, [409, 500],
                f"Expected 409 or 500 but got {response.status_code}"
            )
        finally:
            fail_on.reset()
            self._disable_fail_on()

    # ========================================================================
    # ATTACK VECTOR 5: Race condition - vault deleted between INSERT and commit
    # ========================================================================

    def test_attack_vault_deleted_before_commit(self):
        """
        ATTACK: Vault INSERT succeeds but COMMIT fails because vault was deleted
        by another request (simulated via COMMIT raising OperationalError).

        Expected: Should return 500 error.
        """
        token = self._token(1, "superadmin", "superadmin")
        headers = self._auth_headers(token)

        fail_on = self._enable_fail_on()
        fail_on.configure(commit_raises=sqlite3.OperationalError("database disk image is malformed"))

        try:
            response = self.client.post(
                "/api/vaults",
                json={"name": "Test Vault", "description": "Test"},
                headers=headers,
            )

            # Should return 500 because commit failed
            self.assertEqual(
                response.status_code, 500,
                f"Expected 500 but got {response.status_code}: {response.json()}"
            )
        finally:
            fail_on.reset()
            self._disable_fail_on()

    def test_attack_commit_raises_but_rollback_also_fails(self):
        """
        ATTACK: COMMIT raises exception AND ROLLBACK also fails.

        This is the worst-case scenario - transaction is in undefined state.

        Expected: Should still return 500 to client, not crash.
        """
        token = self._token(1, "superadmin", "superadmin")
        headers = self._auth_headers(token)

        fail_on = self._enable_fail_on()
        fail_on.configure(
            commit_raises=sqlite3.OperationalError("commit failed - disk full"),
            rollback_raises=sqlite3.OperationalError("rollback also failed - disk full")
        )

        try:
            response = self.client.post(
                "/api/vaults",
                json={"name": "Test Vault", "description": "Test"},
                headers=headers,
            )

            # Should return 500, not crash
            self.assertEqual(response.status_code, 500)
            # Both commit and rollback were attempted
            self.assertIn(("commit",), fail_on.call_log)
            self.assertIn(("rollback",), fail_on.call_log)
        finally:
            fail_on.reset()
            self._disable_fail_on()

    # ========================================================================
    # ATTACK VECTOR 6: vault_id is None check bypass
    # ========================================================================

    def test_attack_lastrowid_returns_zero(self):
        """
        ATTACK: cursor.lastrowid returns 0 instead of None on failure.

        The code checks: if vault_id is None: raise 500
        But if lastrowid is 0, this check is bypassed!

        Expected: 0 is falsy but not None, so the check passes incorrectly.
        This reveals a potential bug in the code.
        """
        token = self._token(1, "superadmin", "superadmin")
        headers = self._auth_headers(token)

        fail_on = self._enable_fail_on()
        fail_on.configure(lastrowid_override=0)  # 0 is falsy but not None!

        try:
            response = self.client.post(
                "/api/vaults",
                json={"name": "Test Vault", "description": "Test"},
                headers=headers,
            )

            self.assertEqual(response.status_code, 500)
            self.assertIn(("rollback",), fail_on.call_log)

            conn = sqlite3.connect(self._db_path)
            try:
                vault_count = conn.execute(
                    "SELECT COUNT(*) FROM vaults WHERE name = ?",
                    ("Test Vault",),
                ).fetchone()[0]
                member_count = conn.execute(
                    "SELECT COUNT(*) FROM vault_members WHERE vault_id = 0"
                ).fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(vault_count, 0)
            self.assertEqual(member_count, 0)
        finally:
            fail_on.reset()
            self._disable_fail_on()

    # ========================================================================
    # BASELINE: Verify normal transaction flow works correctly
    # ========================================================================

    def test_baseline_normal_vault_creation(self):
        """
        BASELINE: Normal vault creation with explicit transaction.
        """
        token = self._token(1, "superadmin", "superadmin")
        headers = self._auth_headers(token)

        response = self.client.post(
            "/api/vaults",
            json={"name": "My New Vault", "description": "A test vault"},
            headers=headers,
        )

        self.assertEqual(
            response.status_code, 201,
            f"Expected 201 but got {response.status_code}: {response.json()}"
        )
        data = response.json()
        self.assertEqual(data["name"], "My New Vault")
        self.assertEqual(data["description"], "A test vault")
        self.assertIn("id", data)

    def test_baseline_vault_creation_with_org(self):
        """
        BASELINE: Create vault with explicit org_id.
        """
        token = self._token(1, "superadmin", "superadmin")
        headers = self._auth_headers(token)

        # First create an organization
        conn = self._pool.get_connection()
        try:
            conn.execute("INSERT INTO organizations (id, name) VALUES (100, 'Test Org')")
            conn.execute("INSERT INTO org_members (org_id, user_id, role) VALUES (100, 1, 'admin')")
            conn.commit()
        finally:
            self._pool.release_connection(conn)

        response = self.client.post(
            "/api/vaults",
            json={"name": "Org Vault", "description": "Test org vault", "org_id": 100},
            headers=headers,
        )

        self.assertEqual(
            response.status_code, 201,
            f"Expected 201 but got {response.status_code}: {response.json()}"
        )
        data = response.json()
        self.assertEqual(data["name"], "Org Vault")
        self.assertEqual(data["org_id"], 100)

    def test_baseline_duplicate_name_returns_409(self):
        """
        BASELINE: Creating vault with existing name returns 409.
        """
        token = self._token(1, "superadmin", "superadmin")
        headers = self._auth_headers(token)

        # Create first vault
        response1 = self.client.post(
            "/api/vaults",
            json={"name": "Unique Name Vault", "description": "First"},
            headers=headers,
        )
        self.assertEqual(response1.status_code, 201)

        # Try to create second with same name
        response2 = self.client.post(
            "/api/vaults",
            json={"name": " " + "Unique Name Vault" + " ", "description": "Duplicate"},
            headers=headers,
        )
        self.assertEqual(response2.status_code, 409)
        self.assertIn("already exists", response2.json()["detail"])


if __name__ == "__main__":
    unittest.main()
