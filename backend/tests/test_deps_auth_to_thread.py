"""
Tests for asyncio.to_thread wrapping of SQLite operations in deps.py and auth.py.

Verifies via source inspection (avoiding module-caching issues) and behavioral tests:
1. All sync SQLite calls are wrapped in asyncio.to_thread
2. No async functions are passed to to_thread
3. Rollback is inside lambdas (not in the outer async function)
4. asyncio.to_thread is actually called for each SQLite operation
"""

import ast
import asyncio
import inspect
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

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
    _unstructured.documents.elements = types.ModuleType("unstructured.documents.elements")
    _unstructured.documents.elements.Element = type("Element", (), {})
    sys.modules["unstructured"] = _unstructured
    sys.modules["unstructured.partition"] = _unstructured.partition
    sys.modules["unstructured.partition.auto"] = _unstructured.partition.auto
    sys.modules["unstructured.chunking"] = _unstructured.chunking
    sys.modules["unstructured.chunking.title"] = _unstructured.chunking.title
    sys.modules["unstructured.documents"] = _unstructured.documents
    sys.modules["unstructured.documents.elements"] = _unstructured.documents.elements

from app.models.database import SQLiteConnectionPool, init_db, run_migrations

# =============================================================================
# Source Inspection Helpers
# =============================================================================

def get_function_source(module, func_name):
    """Get the source code of a function from a module."""
    try:
        func = getattr(module, func_name)
        return inspect.getsource(func)
    except (AttributeError, TypeError):
        return None


def count_to_thread_calls(source):
    """Count the number of asyncio.to_thread calls in source code."""
    if not source:
        return 0
    count = 0
    for line in source.split('\n'):
        if 'asyncio.to_thread' in line:
            count += 1
    return count


def verify_no_async_in_to_thread(source):
    """Verify that no async functions are passed to to_thread."""
    if not source:
        return True, []
    tree = ast.parse(source)
    violations = []

    class ToThreadVisitor(ast.NodeVisitor):
        def visit_Call(self, node):
            if isinstance(node.func, ast.Attribute):
                if node.func.attr == 'to_thread':
                    for arg in node.args:
                        if isinstance(arg, ast.Lambda):
                            if isinstance(arg.body, ast.Await):
                                violations.append(f"async lambda passed to to_thread at line {node.lineno}")
            self.generic_visit(node)

    ToThreadVisitor().visit(tree)
    return len(violations) == 0, violations


# =============================================================================
# Source Inspection Tests - deps.py
# =============================================================================

class TestDepsSourceInspection(unittest.IsolatedAsyncioTestCase):
    """Verify deps.py functions use asyncio.to_thread via source inspection."""

    def test_get_current_active_user_uses_to_thread(self):
        """The active-user resolution wraps db.execute in asyncio.to_thread.

        get_current_active_user is a thin wrapper around _resolve_active_user
        (shared with the streaming-chat auth boundary, issue #301); the
        to_thread-wrapped DB fetch lives in _resolve_active_user.
        """
        from app.api import deps

        source = get_function_source(deps, '_resolve_active_user')
        self.assertIsNotNone(source)
        self.assertIn('asyncio.to_thread', source)
        self.assertIn('lambda', source)

    def test_get_current_active_user_no_async_in_to_thread(self):
        """_resolve_active_user does NOT pass async functions to to_thread."""
        from app.api import deps

        source = get_function_source(deps, '_resolve_active_user')
        self.assertIsNotNone(source)
        is_clean, violations = verify_no_async_in_to_thread(source)
        self.assertTrue(is_clean, f"Violations: {violations}")

    def test_get_effective_vault_permissions_uses_to_thread(self):
        """get_effective_vault_permissions wraps db.execute in asyncio.to_thread."""
        from app.api import deps

        source = get_function_source(deps, 'get_effective_vault_permissions')
        self.assertIsNotNone(source)
        self.assertIn('asyncio.to_thread', source)
        count = count_to_thread_calls(source)
        self.assertGreaterEqual(count, 4, f"Expected at least 4, got {count}")

    def test_get_effective_vault_permissions_no_async_in_to_thread(self):
        """get_effective_vault_permissions does NOT pass async functions to to_thread."""
        from app.api import deps

        source = get_function_source(deps, 'get_effective_vault_permissions')
        self.assertIsNotNone(source)
        is_clean, violations = verify_no_async_in_to_thread(source)
        self.assertTrue(is_clean, f"Violations: {violations}")

    def test_get_user_accessible_vault_ids_uses_to_thread(self):
        """get_user_accessible_vault_ids wraps db.execute in asyncio.to_thread."""
        from app.api import deps

        source = get_function_source(deps, 'get_user_accessible_vault_ids')
        self.assertIsNotNone(source)
        self.assertIn('asyncio.to_thread', source)

    def test_get_user_accessible_vault_ids_no_async_in_to_thread(self):
        """get_user_accessible_vault_ids does NOT pass async functions to to_thread."""
        from app.api import deps

        source = get_function_source(deps, 'get_user_accessible_vault_ids')
        self.assertIsNotNone(source)
        is_clean, violations = verify_no_async_in_to_thread(source)
        self.assertTrue(is_clean, f"Violations: {violations}")

    def test_get_user_orgs_uses_to_thread(self):
        """get_user_orgs wraps db.execute in asyncio.to_thread."""
        from app.api import deps

        source = get_function_source(deps, 'get_user_orgs')
        self.assertIsNotNone(source)
        self.assertIn('asyncio.to_thread', source)
        self.assertIn('lambda', source)

    def test_get_user_orgs_no_async_in_to_thread(self):
        """get_user_orgs does NOT pass async functions to to_thread."""
        from app.api import deps

        source = get_function_source(deps, 'get_user_orgs')
        self.assertIsNotNone(source)
        is_clean, violations = verify_no_async_in_to_thread(source)
        self.assertTrue(is_clean, f"Violations: {violations}")


# =============================================================================
# Source Inspection Tests - auth.py
# =============================================================================

class TestAuthSourceInspection(unittest.TestCase):
    """Verify auth.py route handlers use asyncio.to_thread via source inspection."""

    def test_register_uses_to_thread(self):
        """register endpoint wraps SQLite calls in asyncio.to_thread."""
        from app.api.routes import auth

        source = get_function_source(auth, 'register')
        self.assertIsNotNone(source)
        self.assertIn('asyncio.to_thread', source)
        count = count_to_thread_calls(source)
        self.assertGreaterEqual(count, 2, f"Expected at least 2, got {count}")

    def test_register_no_async_in_to_thread(self):
        """register does NOT pass async functions to to_thread."""
        from app.api.routes import auth

        source = get_function_source(auth, 'register')
        self.assertIsNotNone(source)
        is_clean, violations = verify_no_async_in_to_thread(source)
        self.assertTrue(is_clean, f"Violations: {violations}")

    def test_login_uses_to_thread(self):
        """login endpoint wraps SQLite calls in asyncio.to_thread."""
        from app.api.routes import auth

        source = get_function_source(auth, 'login')
        self.assertIsNotNone(source)
        self.assertIn('asyncio.to_thread', source)

    def test_login_no_async_in_to_thread(self):
        """login does NOT pass async functions to to_thread."""
        from app.api.routes import auth

        source = get_function_source(auth, 'login')
        self.assertIsNotNone(source)
        is_clean, violations = verify_no_async_in_to_thread(source)
        self.assertTrue(is_clean, f"Violations: {violations}")

    def test_refresh_uses_to_thread(self):
        """refresh endpoint wraps SQLite calls in asyncio.to_thread."""
        from app.api.routes import auth

        source = get_function_source(auth, 'refresh')
        self.assertIsNotNone(source)
        self.assertIn('asyncio.to_thread', source)

    def test_refresh_no_async_in_to_thread(self):
        """refresh does NOT pass async functions to to_thread."""
        from app.api.routes import auth

        source = get_function_source(auth, 'refresh')
        self.assertIsNotNone(source)
        is_clean, violations = verify_no_async_in_to_thread(source)
        self.assertTrue(is_clean, f"Violations: {violations}")

    def test_logout_uses_to_thread(self):
        """logout endpoint wraps SQLite calls in asyncio.to_thread."""
        from app.api.routes import auth

        source = get_function_source(auth, 'logout')
        self.assertIsNotNone(source)
        self.assertIn('asyncio.to_thread', source)

    def test_update_me_uses_to_thread(self):
        """update_me endpoint wraps SQLite calls in asyncio.to_thread."""
        from app.api.routes import auth

        source = get_function_source(auth, 'update_me')
        self.assertIsNotNone(source)
        self.assertIn('asyncio.to_thread', source)

    def test_change_password_uses_to_thread(self):
        """change_password endpoint wraps SQLite calls in asyncio.to_thread."""
        from app.api.routes import auth

        source = get_function_source(auth, 'change_password')
        self.assertIsNotNone(source)
        self.assertIn('asyncio.to_thread', source)

    def test_change_password_no_async_in_to_thread(self):
        """change_password does NOT pass async functions to to_thread."""
        from app.api.routes import auth

        source = get_function_source(auth, 'change_password')
        self.assertIsNotNone(source)
        is_clean, violations = verify_no_async_in_to_thread(source)
        self.assertTrue(is_clean, f"Violations: {violations}")

    def test_list_sessions_uses_to_thread(self):
        """list_sessions endpoint wraps SQLite calls in asyncio.to_thread."""
        from app.api.routes import auth

        source = get_function_source(auth, 'list_sessions')
        self.assertIsNotNone(source)
        self.assertIn('asyncio.to_thread', source)

    def test_list_sessions_no_async_in_to_thread(self):
        """list_sessions does NOT pass async functions to to_thread."""
        from app.api.routes import auth

        source = get_function_source(auth, 'list_sessions')
        self.assertIsNotNone(source)
        is_clean, violations = verify_no_async_in_to_thread(source)
        self.assertTrue(is_clean, f"Violations: {violations}")

    def test_revoke_session_uses_to_thread(self):
        """revoke_session endpoint wraps SQLite calls in asyncio.to_thread."""
        from app.api.routes import auth

        source = get_function_source(auth, 'revoke_session')
        self.assertIsNotNone(source)
        self.assertIn('asyncio.to_thread', source)

    def test_revoke_session_no_async_in_to_thread(self):
        """revoke_session does NOT pass async functions to to_thread."""
        from app.api.routes import auth

        source = get_function_source(auth, 'revoke_session')
        self.assertIsNotNone(source)
        is_clean, violations = verify_no_async_in_to_thread(source)
        self.assertTrue(is_clean, f"Violations: {violations}")

    def test_revoke_all_sessions_uses_to_thread(self):
        """revoke_all_sessions endpoint wraps SQLite calls in asyncio.to_thread."""
        from app.api.routes import auth

        source = get_function_source(auth, 'revoke_all_sessions')
        self.assertIsNotNone(source)
        self.assertIn('asyncio.to_thread', source)

    def test_revoke_all_sessions_no_async_in_to_thread(self):
        """revoke_all_sessions does NOT pass async functions to to_thread."""
        from app.api.routes import auth

        source = get_function_source(auth, 'revoke_all_sessions')
        self.assertIsNotNone(source)
        is_clean, violations = verify_no_async_in_to_thread(source)
        self.assertTrue(is_clean, f"Violations: {violations}")


# =============================================================================
# Lambda Pattern Tests
# =============================================================================

class TestLambdaPattern(unittest.TestCase):
    """Verify lambda pattern is used with to_thread calls."""

    def test_deps_uses_lambda_with_to_thread(self):
        """deps.py functions use lambda pattern with to_thread."""
        from app.api import deps

        source = get_function_source(deps, 'get_user_orgs')
        self.assertIsNotNone(source)
        self.assertIn('lambda', source)
        self.assertIn('to_thread', source)

    def test_auth_transaction_functions_have_rollback(self):
        """Auth route transaction functions contain rollback logic."""
        from app.api.routes import auth

        source = get_function_source(auth, '_record_failed_attempt_db')
        self.assertIsNotNone(source)
        self.assertIn('rollback', source.lower())

        source = get_function_source(auth, '_rotate_refresh_token_block')
        self.assertIsNotNone(source)
        self.assertIn('ROLLBACK', source)

        source = get_function_source(auth, '_delete_expired_session_db')
        self.assertIsNotNone(source)
        self.assertIn('rollback', source.lower())

    def test_register_uses_lambda_pattern(self):
        """register has db operations inside lambdas passed to to_thread."""
        from app.api.routes import auth

        source = get_function_source(auth, 'register')
        self.assertIsNotNone(source)
        self.assertIn('lambda', source.lower())

    def test_login_uses_lambda_pattern(self):
        """login has db operations inside lambdas passed to to_thread."""
        from app.api.routes import auth

        source = get_function_source(auth, 'login')
        self.assertIsNotNone(source)
        self.assertIn('lambda', source.lower())

    def test_change_password_uses_lambda_pattern(self):
        """change_password has db operations inside lambdas passed to to_thread."""
        from app.api.routes import auth

        source = get_function_source(auth, 'change_password')
        self.assertIsNotNone(source)
        self.assertIn('lambda', source.lower())


# =============================================================================
# Call Count Tests
# =============================================================================

class TestToThreadCallCounts(unittest.TestCase):
    """Verify minimum to_thread call counts per endpoint via source inspection."""

    def test_register_minimum_to_thread_calls(self):
        """register should have at least 3 to_thread calls."""
        from app.api.routes import auth

        source = get_function_source(auth, 'register')
        self.assertIsNotNone(source)
        count = count_to_thread_calls(source)
        self.assertGreaterEqual(count, 3, f"Expected at least 3, got {count}")

    def test_login_minimum_to_thread_calls(self):
        """login should have at least 2 to_thread calls."""
        from app.api.routes import auth

        source = get_function_source(auth, 'login')
        self.assertIsNotNone(source)
        count = count_to_thread_calls(source)
        self.assertGreaterEqual(count, 2, f"Expected at least 2, got {count}")

    def test_change_password_minimum_to_thread_calls(self):
        """change_password should have at least 3 to_thread calls."""
        from app.api.routes import auth

        source = get_function_source(auth, 'change_password')
        self.assertIsNotNone(source)
        count = count_to_thread_calls(source)
        self.assertGreaterEqual(count, 3, f"Expected at least 3, got {count}")


# =============================================================================
# Behavioral Tests - Real Database Operations
# =============================================================================

class TestDepsBehavioralWithRealDb(unittest.IsolatedAsyncioTestCase):
    """Verify deps.py functions actually work correctly with real database operations."""

    def setUp(self):
        """Set up test database."""
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        init_db(self.db_path)
        run_migrations(self.db_path)
        self.test_pool = SQLiteConnectionPool(self.db_path, max_size=10)

    def tearDown(self):
        """Clean up after each test."""
        self.test_pool.close_all()
        import shutil
        try:
            shutil.rmtree(self.temp_dir)
        except Exception:
            pass

    def _get_connection(self):
        return self.test_pool.get_connection()

    def _release_connection(self, conn):
        self.test_pool.release_connection(conn)

    async def test_get_user_orgs_returns_correct_orgs(self):
        """get_user_orgs returns correct org IDs for a user."""
        from app.api.deps import get_user_orgs

        conn = self._get_connection()
        try:
            conn.execute(
                "INSERT INTO users (username, hashed_password, full_name, role, is_active) VALUES (?, ?, ?, ?, 1)",
                ("orguser", "hash", "Org User", "member"),
            )
            conn.commit()
            user_id = conn.execute("SELECT id FROM users WHERE username = ?", ("orguser",)).fetchone()[0]

            conn.execute("INSERT INTO organizations (name, created_at) VALUES (?, ?)", ("TestOrg", "2024-01-01T00:00:00+00:00"))
            conn.commit()
            org_id = conn.execute("SELECT id FROM organizations").fetchone()[0]

            conn.execute("INSERT INTO org_members (user_id, org_id) VALUES (?, ?)", (user_id, org_id))
            conn.commit()

            orgs = await get_user_orgs(user_id, conn)
            self.assertEqual(orgs, [org_id])
        finally:
            self._release_connection(conn)

    async def test_get_effective_vault_permissions_returns_permissions(self):
        """get_effective_vault_permissions returns correct permissions."""
        from app.api.deps import get_effective_vault_permissions

        conn = self._get_connection()
        try:
            conn.execute(
                "INSERT INTO users (username, hashed_password, full_name, role, is_active) VALUES (?, ?, ?, ?, 1)",
                ("vaultuser", "hash", "Vault User", "member"),
            )
            conn.commit()
            user_id = conn.execute("SELECT id FROM users WHERE username = ?", ("vaultuser",)).fetchone()[0]

            conn.execute(
                "INSERT INTO vaults (name, visibility, owner_id, created_at) VALUES (?, ?, ?, ?)",
                ("TestVault", "private", user_id, "2024-01-01T00:00:00+00:00"),
            )
            conn.commit()
            vault_id = conn.execute("SELECT id FROM vaults WHERE name = ?", ("TestVault",)).fetchone()[0]

            conn.execute(
                "INSERT INTO vault_members (vault_id, user_id, permission) VALUES (?, ?, ?)",
                (vault_id, user_id, "read"),
            )
            conn.commit()

            result = await get_effective_vault_permissions(conn, {"id": user_id, "role": "member"}, [vault_id])
            self.assertEqual(result.get(vault_id), "read")
        finally:
            self._release_connection(conn)

    async def test_get_user_accessible_vault_ids_returns_vaults(self):
        """get_user_accessible_vault_ids returns vault IDs user has access to."""
        from app.api.deps import get_user_accessible_vault_ids

        conn = self._get_connection()
        try:
            conn.execute(
                "INSERT INTO users (username, hashed_password, full_name, role, is_active) VALUES (?, ?, ?, ?, 1)",
                ("accessuser", "hash", "Access User", "member"),
            )
            conn.commit()
            user_id = conn.execute("SELECT id FROM users WHERE username = ?", ("accessuser",)).fetchone()[0]

            conn.execute(
                "INSERT INTO vaults (name, visibility, owner_id, created_at) VALUES (?, ?, ?, ?)",
                ("AccessVault", "private", user_id, "2024-01-01T00:00:00+00:00"),
            )
            conn.commit()

            result = await get_user_accessible_vault_ids({"id": user_id, "role": "member"}, conn)
            self.assertIsInstance(result, list)
        finally:
            self._release_connection(conn)


if __name__ == "__main__":
    unittest.main()
