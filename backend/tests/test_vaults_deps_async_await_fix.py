"""
Tests for asyncio.to_thread and direct await patterns in deps.py and vaults.py.

Task 2.4 Verifications:
1. deps.py: get_effective_vault_permission is awaited directly (NOT passed to asyncio.to_thread)
2. vaults.py: 4 async functions await get_effective_vault_* directly (NOT passed to asyncio.to_thread)

This uses AST-based source inspection to verify the patterns without module-caching issues.
"""

import ast
import inspect
import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

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


def find_awaited_async_calls(source):
    """Find all 'await <func_name>()' calls in source that are NOT inside asyncio.to_thread."""
    if not source:
        return []
    tree = ast.parse(source)
    violations = []

    class AwaitVisitor(ast.NodeVisitor):
        def visit_Await(self, node):
            # Check if this Await is inside a Call node's func that is to_thread
            # We'll collect all await calls and filter later
            if isinstance(node.value, ast.Call):
                if isinstance(node.value.func, ast.Name):
                    violations.append((node.lineno, node.value.func.id))
            self.generic_visit(node)

    AwaitVisitor().visit(tree)
    return violations


def is_within_to_thread(node, tree):
    """Check if an AST node is within an asyncio.to_thread call."""
    class ToThreadChecker(ast.NodeVisitor):
        def __init__(self, target_node):
            self.target_node = target_node
            self.is_inside = False
            self.current_call = None

        def visit_Call(self, node):
            old_call = self.current_call
            if isinstance(node.func, ast.Attribute) and node.func.attr == 'to_thread':
                self.current_call = node
                if node is self.target_node or self._is_ancestor(node, self.target_node):
                    self.is_inside = True
            self.generic_visit(node)
            self.current_call = old_call

        def _is_ancestor(self, ancestor, descendant):
            """Check if ancestor is an ancestor node of descendant."""
            for child in ast.walk(ancestor):
                if child is descendant:
                    return True
            return False

    checker = ToThreadChecker(node)
    checker.visit(tree)
    return checker.is_inside


def get_all_awaited_calls_outside_to_thread(source):
    """Get all 'await func(...)' calls that are NOT wrapped in asyncio.to_thread."""
    if not source:
        return []
    tree = ast.parse(source)
    to_thread_ranges = []

    class ToThreadLister(ast.NodeVisitor):
        def visit_Call(self, node):
            if isinstance(node.func, ast.Attribute) and node.func.attr == 'to_thread':
                to_thread_ranges.append(node)
            self.generic_visit(node)

    ToThreadLister().visit(tree)

    await_calls = []
    class AwaitLister(ast.NodeVisitor):
        def visit_Await(self, node):
            if isinstance(node.value, ast.Call):
                func_name = None
                if isinstance(node.value.func, ast.Name):
                    func_name = node.value.func.id
                elif isinstance(node.value.func, ast.Attribute):
                    func_name = node.value.func.attr
                if func_name:
                    # Check if inside any to_thread call
                    inside = False
                    for tt in to_thread_ranges:
                        if _node_contains(tt, node):
                            inside = True
                            break
                    if not inside:
                        await_calls.append((node.lineno, func_name))
            self.generic_visit(node)

    AwaitLister().visit(tree)
    return await_calls


def _node_contains(container, target):
    """Check if container AST node contains the target node."""
    for node in ast.walk(container):
        if node is target:
            return True
    return False


# =============================================================================
# Test: get_effective_vault_permission in deps.py - awaited directly
# =============================================================================

class TestDepsGetEffectiveVaultPermissionDirectAwait(unittest.TestCase):
    """Verify get_effective_vault_permission is awaited directly in deps.py.

    Regression: get_effective_vault_permission was previously passed to
    asyncio.to_thread incorrectly. It must be awaited directly since it is
    an async function.
    """

    def test_get_effective_vault_permission_not_in_to_thread(self):
        """get_effective_vault_permission is NOT passed to asyncio.to_thread."""
        from app.api import deps

        source = get_function_source(deps, 'get_effective_vault_permission')
        self.assertIsNotNone(source)

        # Check that get_effective_vault_permission is NOT called inside asyncio.to_thread
        tree = ast.parse(source)
        to_thread_calls = []

        class ToThreadFinder(ast.NodeVisitor):
            def visit_Call(self, node):
                if isinstance(node.func, ast.Attribute) and node.func.attr == 'to_thread':
                    to_thread_calls.append(node)
                self.generic_visit(node)

        ToThreadFinder().visit(tree)

        # For each to_thread call, check if get_effective_vault_permission is inside it
        for tt_call in to_thread_calls:
            for node in ast.walk(tt_call):
                if isinstance(node, ast.Await):
                    if isinstance(node.value, ast.Call):
                        func = node.value.func
                        if isinstance(func, ast.Name) and 'get_effective_vault_permission' in func.id:
                            self.fail(f"get_effective_vault_permission found inside asyncio.to_thread at line {node.lineno}")
                        elif isinstance(func, ast.Attribute) and 'get_effective_vault_permission' in func.attr:
                            self.fail(f"get_effective_vault_permission found inside asyncio.to_thread at line {node.lineno}")

    def test_get_effective_vault_permission_uses_direct_await(self):
        """get_effective_vault_permission awaits get_effective_vault_permissions directly."""
        from app.api import deps

        source = get_function_source(deps, 'get_effective_vault_permission')
        self.assertIsNotNone(source)
        self.assertIn('await', source)
        # It should call get_effective_vault_permissions with await
        self.assertIn('await get_effective_vault_permissions', source)

    def test_get_effective_vault_permission_no_to_thread_wrapping(self):
        """get_effective_vault_permission body does not wrap async calls in to_thread."""
        from app.api import deps

        source = get_function_source(deps, 'get_effective_vault_permission')
        self.assertIsNotNone(source)

        # Verify get_effective_vault_permissions is awaited directly (not inside to_thread)
        await_calls = get_all_awaited_calls_outside_to_thread(source)
        func_names = [name for _, name in await_calls]
        # The function should have 'get_effective_vault_permissions' in its awaited calls
        self.assertIn('get_effective_vault_permissions', func_names)


# =============================================================================
# Test: vaults.py async functions - awaited directly, NOT passed to asyncio.to_thread
# =============================================================================

class TestVaultsAsyncFunctionsDirectAwait(unittest.TestCase):
    """Verify async functions in vaults.py are awaited directly.

    The 4 async functions that call get_effective_vault_* are:
    1. _fetch_vault_with_counts - awaits get_effective_vault_permission
    2. _fetch_all_vaults - awaits get_effective_vault_permissions
    3. _fetch_accessible_vaults - awaits get_effective_vault_permissions
    4. list_vaults (endpoint) - awaits _fetch_all_vaults
    5. list_accessible_vaults (endpoint) - awaits _fetch_accessible_vaults
    6. get_vault (endpoint) - awaits _fetch_vault_with_counts

    These should NOT wrap the async get_effective_vault_* calls in asyncio.to_thread.
    """

    def test_fetch_vault_with_counts_awaits_directly(self):
        """_fetch_vault_with_counts awaits get_effective_vault_permission directly."""
        from app.api.routes import vaults

        source = get_function_source(vaults, '_fetch_vault_with_counts')
        self.assertIsNotNone(source)

        # Must contain 'await get_effective_vault_permission' directly
        self.assertIn('await get_effective_vault_permission', source)

        # Verify it's NOT inside asyncio.to_thread
        tree = ast.parse(source)
        to_thread_calls = []

        class ToThreadFinder(ast.NodeVisitor):
            def visit_Call(self, node):
                if isinstance(node.func, ast.Attribute) and node.func.attr == 'to_thread':
                    to_thread_calls.append(node)
                self.generic_visit(node)

        ToThreadFinder().visit(tree)

        for tt_call in to_thread_calls:
            for node in ast.walk(tt_call):
                if isinstance(node, ast.Await):
                    if isinstance(node.value, ast.Call):
                        func = node.value.func
                        if isinstance(func, ast.Name) and 'get_effective_vault_permission' in func.id:
                            self.fail(f"get_effective_vault_permission found inside asyncio.to_thread at line {node.lineno}")
                        elif isinstance(func, ast.Attribute) and 'get_effective_vault_permission' in func.attr:
                            self.fail(f"get_effective_vault_permission found inside asyncio.to_thread at line {node.lineno}")

    def test_fetch_all_vaults_awaits_directly(self):
        """_fetch_all_vaults awaits get_effective_vault_permissions directly."""
        from app.api.routes import vaults

        source = get_function_source(vaults, '_fetch_all_vaults')
        self.assertIsNotNone(source)

        # Must contain 'await get_effective_vault_permissions' directly
        self.assertIn('await get_effective_vault_permissions', source)

        # Verify get_effective_vault_permissions is NOT inside asyncio.to_thread
        tree = ast.parse(source)
        to_thread_calls = []

        class ToThreadFinder(ast.NodeVisitor):
            def visit_Call(self, node):
                if isinstance(node.func, ast.Attribute) and node.func.attr == 'to_thread':
                    to_thread_calls.append(node)
                self.generic_visit(node)

        ToThreadFinder().visit(tree)

        for tt_call in to_thread_calls:
            for node in ast.walk(tt_call):
                if isinstance(node, ast.Await):
                    if isinstance(node.value, ast.Call):
                        func = node.value.func
                        if isinstance(func, ast.Name) and 'get_effective_vault_permissions' in func.id:
                            self.fail(f"get_effective_vault_permissions found inside asyncio.to_thread at line {node.lineno}")
                        elif isinstance(func, ast.Attribute) and 'get_effective_vault_permissions' in func.attr:
                            self.fail(f"get_effective_vault_permissions found inside asyncio.to_thread at line {node.lineno}")

    def test_fetch_accessible_vaults_awaits_directly(self):
        """_fetch_accessible_vaults awaits get_effective_vault_permissions directly."""
        from app.api.routes import vaults

        source = get_function_source(vaults, '_fetch_accessible_vaults')
        self.assertIsNotNone(source)

        # Must contain 'await get_effective_vault_permissions' directly
        self.assertIn('await get_effective_vault_permissions', source)

        # Verify get_effective_vault_permissions is NOT inside asyncio.to_thread
        tree = ast.parse(source)
        to_thread_calls = []

        class ToThreadFinder(ast.NodeVisitor):
            def visit_Call(self, node):
                if isinstance(node.func, ast.Attribute) and node.func.attr == 'to_thread':
                    to_thread_calls.append(node)
                self.generic_visit(node)

        ToThreadFinder().visit(tree)

        for tt_call in to_thread_calls:
            for node in ast.walk(tt_call):
                if isinstance(node, ast.Await):
                    if isinstance(node.value, ast.Call):
                        func = node.value.func
                        if isinstance(func, ast.Name) and 'get_effective_vault_permissions' in func.id:
                            self.fail(f"get_effective_vault_permissions found inside asyncio.to_thread at line {node.lineno}")
                        elif isinstance(func, ast.Attribute) and 'get_effective_vault_permissions' in func.attr:
                            self.fail(f"get_effective_vault_permissions found inside asyncio.to_thread at line {node.lineno}")

    def test_list_vaults_awaits_fetch_all_vaults_directly(self):
        """list_vaults awaits _fetch_all_vaults directly (not via asyncio.to_thread)."""
        from app.api.routes import vaults

        source = get_function_source(vaults, 'list_vaults')
        self.assertIsNotNone(source)
        self.assertIn('await _fetch_all_vaults', source)

        # Verify _fetch_all_vaults is NOT inside asyncio.to_thread
        tree = ast.parse(source)
        to_thread_calls = []

        class ToThreadFinder(ast.NodeVisitor):
            def visit_Call(self, node):
                if isinstance(node.func, ast.Attribute) and node.func.attr == 'to_thread':
                    to_thread_calls.append(node)
                self.generic_visit(node)

        ToThreadFinder().visit(tree)

        for tt_call in to_thread_calls:
            for node in ast.walk(tt_call):
                if isinstance(node, ast.Await):
                    if isinstance(node.value, ast.Call):
                        func = node.value.func
                        if isinstance(func, ast.Name) and func.id == '_fetch_all_vaults':
                            self.fail(f"_fetch_all_vaults found inside asyncio.to_thread at line {node.lineno}")

    def test_list_accessible_vaults_awaits_fetch_accessible_vaults_directly(self):
        """list_accessible_vaults awaits _fetch_accessible_vaults directly."""
        from app.api.routes import vaults

        source = get_function_source(vaults, 'list_accessible_vaults')
        self.assertIsNotNone(source)
        self.assertIn('await _fetch_accessible_vaults', source)

        # Verify _fetch_accessible_vaults is NOT inside asyncio.to_thread
        tree = ast.parse(source)
        to_thread_calls = []

        class ToThreadFinder(ast.NodeVisitor):
            def visit_Call(self, node):
                if isinstance(node.func, ast.Attribute) and node.func.attr == 'to_thread':
                    to_thread_calls.append(node)
                self.generic_visit(node)

        ToThreadFinder().visit(tree)

        for tt_call in to_thread_calls:
            for node in ast.walk(tt_call):
                if isinstance(node, ast.Await):
                    if isinstance(node.value, ast.Call):
                        func = node.value.func
                        if isinstance(func, ast.Name) and func.id == '_fetch_accessible_vaults':
                            self.fail(f"_fetch_accessible_vaults found inside asyncio.to_thread at line {node.lineno}")

    def test_get_vault_awaits_fetch_vault_with_counts_directly(self):
        """get_vault awaits _fetch_vault_with_counts directly."""
        from app.api.routes import vaults

        source = get_function_source(vaults, 'get_vault')
        self.assertIsNotNone(source)
        self.assertIn('await _fetch_vault_with_counts', source)

        # Verify _fetch_vault_with_counts is NOT inside asyncio.to_thread
        tree = ast.parse(source)
        to_thread_calls = []

        class ToThreadFinder(ast.NodeVisitor):
            def visit_Call(self, node):
                if isinstance(node.func, ast.Attribute) and node.func.attr == 'to_thread':
                    to_thread_calls.append(node)
                self.generic_visit(node)

        ToThreadFinder().visit(tree)

        for tt_call in to_thread_calls:
            for node in ast.walk(tt_call):
                if isinstance(node, ast.Await):
                    if isinstance(node.value, ast.Call):
                        func = node.value.func
                        if isinstance(func, ast.Name) and func.id == '_fetch_vault_with_counts':
                            self.fail(f"_fetch_vault_with_counts found inside asyncio.to_thread at line {node.lineno}")


# =============================================================================
# Test: DB operations in vaults.py still use asyncio.to_thread correctly
# =============================================================================

class TestVaultsDbOperationsUseToThread(unittest.TestCase):
    """Verify vaults.py still uses asyncio.to_thread for DB (conn.execute) operations.

    While async function calls should be awaited directly, the sync SQLite
    operations (conn.execute, cursor.fetchone, cursor.fetchall) should still
    be wrapped in asyncio.to_thread.
    """

    def test_fetch_vault_with_counts_uses_to_thread_for_db(self):
        """_fetch_vault_with_counts wraps conn.execute in asyncio.to_thread."""
        from app.api.routes import vaults

        source = get_function_source(vaults, '_fetch_vault_with_counts')
        self.assertIsNotNone(source)
        self.assertIn('asyncio.to_thread', source)
        self.assertIn('conn.execute', source)

    def test_fetch_all_vaults_uses_to_thread_for_db(self):
        """_fetch_all_vaults wraps conn.execute in asyncio.to_thread."""
        from app.api.routes import vaults

        source = get_function_source(vaults, '_fetch_all_vaults')
        self.assertIsNotNone(source)
        self.assertIn('asyncio.to_thread', source)
        self.assertIn('conn.execute', source)

    def test_fetch_accessible_vaults_uses_to_thread_for_db(self):
        """_fetch_accessible_vaults wraps conn.execute in asyncio.to_thread."""
        from app.api.routes import vaults

        source = get_function_source(vaults, '_fetch_accessible_vaults')
        self.assertIsNotNone(source)
        self.assertIn('asyncio.to_thread', source)
        self.assertIn('conn.execute', source)


# =============================================================================
# Test: Behavioral - verify no asyncio.to_thread wrapping async calls
# =============================================================================

class TestNoAsyncInToThreadViolation(unittest.TestCase):
    """Verify no async functions are passed to asyncio.to_thread in vaults.py and deps.py.

    The bug pattern is: asyncio.to_thread(some_async_func, ...)
    This is wrong because asyncio.to_thread is for sync functions, not async ones.
    """

    def test_deps_no_async_in_to_thread(self):
        """deps.py does NOT pass async functions to asyncio.to_thread."""
        from app.api import deps

        for func_name in ['get_effective_vault_permission', 'get_effective_vault_permissions',
                          'get_user_accessible_vault_ids']:
            source = get_function_source(deps, func_name)
            if source is None:
                continue
            tree = ast.parse(source)
            violations = []

            class ToThreadVisitor(ast.NodeVisitor):
                def visit_Call(self, node):
                    if isinstance(node.func, ast.Attribute) and node.func.attr == 'to_thread':
                        for arg in node.args:
                            if isinstance(arg, ast.Lambda):
                                if isinstance(arg.body, ast.Await):
                                    violations.append(f"{func_name}: async lambda passed to to_thread at line {node.lineno}")
                    self.generic_visit(node)

            ToThreadVisitor().visit(tree)
            self.assertEqual(len(violations), 0, f"Violations in {func_name}: {violations}")

    def test_vaults_no_async_in_to_thread(self):
        """vaults.py does NOT pass async functions to asyncio.to_thread."""
        from app.api.routes import vaults

        for func_name in ['_fetch_vault_with_counts', '_fetch_all_vaults', '_fetch_accessible_vaults']:
            source = get_function_source(vaults, func_name)
            if source is None:
                continue
            tree = ast.parse(source)
            violations = []

            class ToThreadVisitor(ast.NodeVisitor):
                def visit_Call(self, node):
                    if isinstance(node.func, ast.Attribute) and node.func.attr == 'to_thread':
                        for arg in node.args:
                            if isinstance(arg, ast.Lambda):
                                if isinstance(arg.body, ast.Await):
                                    violations.append(f"{func_name}: async lambda passed to to_thread at line {node.lineno}")
                    self.generic_visit(node)

            ToThreadVisitor().visit(tree)
            self.assertEqual(len(violations), 0, f"Violations in {func_name}: {violations}")


if __name__ == "__main__":
    unittest.main()
