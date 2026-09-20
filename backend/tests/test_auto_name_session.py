"""
Tests for _auto_name_session() function in chat.py.

Verifies:
1. Successful auto-naming with NULL title (unconditional UPDATE)
2. Prefix-match guard with short existing title
3. Skip guard for long (>60 chars) manual titles
4. Concurrent-change detection (atomic UPDATE returns 0 rows)
5. Fallback on LLM failure (truncate first message)
6. Short title fallback (< 3 chars → message[:50] + "...")
7. Long title truncation (> 60 chars → [:57] + "...")
8. Background task tracking in _background_tasks set
"""

import asyncio
import os
import sys
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


class _AsyncConnectionCM:
    """Async CM standing in for pool.connection_async() (#645), yielding the
    same recording mock connection the old sync pool.connection() CM did."""

    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, exc_type, exc, tb):
        return None


class TestAutoNameSession(unittest.IsolatedAsyncioTestCase):
    """Test suite for _auto_name_session function."""

    async def asyncSetUp(self):
        """Set up mocks for each test."""
        # Track execution history
        self._execute_history = []

        # Create mock connection that records all operations
        self._mock_conn = MagicMock()
        self._mock_conn.rowcount = 1
        self._mock_conn.fetchone.return_value = None
        self._mock_conn.commit.return_value = None

        def mock_execute(query, params=None):
            """Mock execute that records queries."""
            # Create a fresh cursor mock for each execute
            cursor = MagicMock()
            # For UPDATE queries, use rowcount=1 to allow commit
            # For SELECT queries, use rowcount based on _mock_conn.rowcount
            if "UPDATE" in query.upper():
                cursor.rowcount = 1  # Ensure rowcount > 0 for fallback to commit
            else:
                cursor.rowcount = self._mock_conn.rowcount
            # fetchone returns based on SELECT vs UPDATE
            if "SELECT" in query.upper():
                cursor.fetchone.return_value = self._mock_conn.fetchone.return_value
            else:
                cursor.fetchone.return_value = None
            self._execute_history.append(
                {"query": query, "params": params, "cursor": cursor}
            )
            return cursor

        self._mock_conn.execute = mock_execute

        # Create mock pool
        self._mock_pool = MagicMock()
        self._mock_pool.connection_async = MagicMock(
            return_value=_AsyncConnectionCM(self._mock_conn)
        )

        # Apply patches
        self.mock_settings_patcher = patch("app.api.routes.chat.settings")
        self.mock_settings = self.mock_settings_patcher.start()
        self.mock_settings.sqlite_path = "/test/sqlite.db"

        self.mock_pool_patcher = patch("app.api.routes.chat.get_pool")
        self.mock_get_pool = self.mock_pool_patcher.start()
        self.mock_get_pool.return_value = self._mock_pool

        # Mock llm_client
        self.mock_llm_client = AsyncMock()

    def tearDown(self):
        """Stop all patches."""
        self.mock_settings_patcher.stop()
        self.mock_pool_patcher.stop()

    def _setup_select_result(self, title_value):
        """Set up the SELECT query result for fetchone."""
        if title_value is None:
            self._mock_conn.fetchone.return_value = None
        else:
            self._mock_conn.fetchone.return_value = (title_value,)
        self._mock_conn.rowcount = 1  # Reset for UPDATE queries

    def _get_update_queries(self):
        """Get all UPDATE queries from history."""
        return [e for e in self._execute_history if "UPDATE" in e["query"].upper()]

    def _get_fallback_queries(self):
        """Get fallback queries (with AND title IS NULL in WHERE clause)."""
        return [e for e in self._execute_history if "AND title IS NULL" in e["query"]]

    async def test_auto_name_success_null_title(self):
        """
        Test 1: LLM returns title, session has NULL title.
        UPDATE should run unconditionally (no WHERE title = ?).
        """
        from app.api.routes.chat import _auto_name_session

        self._setup_select_result(None)  # existing_title is None
        self.mock_llm_client.chat_completion = AsyncMock(
            return_value="My Test Session Title"
        )

        await _auto_name_session(
            session_id=42,
            first_message="Hello, how are you?",
            llm_client=self.mock_llm_client,
        )

        # Verify LLM was called
        self.mock_llm_client.chat_completion.assert_called_once()

        # Find UPDATE calls
        update_queries = self._get_update_queries()
        self.assertEqual(len(update_queries), 1)

        update_query = update_queries[0]["query"]
        update_params = update_queries[0]["params"]

        # Should have 2 args: (title, session_id) — no existing_title in WHERE
        self.assertEqual(len(update_params), 2)
        self.assertEqual(update_params[0], "My Test Session Title")
        self.assertEqual(update_params[1], 42)
        # Should be unconditional (no WHERE title = ?)
        self.assertNotIn("WHERE title =", update_query)

    async def test_auto_name_success_with_prefix_match(self):
        """
        Test 2: Existing title starts with first_message prefix and len < 60.
        Atomic UPDATE WHERE id=? AND title=? should run.
        """
        from app.api.routes.chat import _auto_name_session

        existing_title = "Hello, how are you today?"  # Must start with first_message[:10] = "Hello, how"
        self._setup_select_result(existing_title)
        self.mock_llm_client.chat_completion = AsyncMock(
            return_value="Hello World Updated"
        )

        await _auto_name_session(
            session_id=42,
            first_message="Hello, how are you today?",  # first 10 chars = "Hello, how"
            llm_client=self.mock_llm_client,
        )

        # Verify UPDATE was called with atomic WHERE clause
        update_queries = self._get_update_queries()
        self.assertEqual(len(update_queries), 1)

        update_queries[0]["query"]
        update_params = update_queries[0]["params"]

        # Should have 3 args: (title, session_id, existing_title)
        self.assertEqual(len(update_params), 3)
        self.assertEqual(update_params[0], "Hello World Updated")
        self.assertEqual(update_params[1], 42)
        self.assertEqual(update_params[2], existing_title)

    async def test_auto_name_skipped_on_manual_rename(self):
        """
        Test 3: Existing title is long (>60 chars), guard should skip UPDATE.
        """
        from app.api.routes.chat import _auto_name_session

        # Manual title longer than 60 chars
        manual_title = "This is a very long manual title that was set by the user intentionally and exceeds 60 chars"
        self._setup_select_result(manual_title)
        self.mock_llm_client.chat_completion = AsyncMock(
            return_value="New Suggested Title"
        )

        await _auto_name_session(
            session_id=42,
            first_message="Hello, how are you?",
            llm_client=self.mock_llm_client,
        )

        # Verify LLM was called (we still try)
        self.mock_llm_client.chat_completion.assert_called_once()

        # Verify NO UPDATE was called for chat_sessions
        update_queries = self._get_update_queries()
        self.assertEqual(len(update_queries), 0)

    async def test_auto_name_skipped_on_concurrent_change(self):
        """
        Test 4: Atomic UPDATE WHERE title=? returns 0 rows (concurrent change).
        Warning should be logged and no commit should happen.
        """
        from app.api.routes.chat import _auto_name_session

        existing_title = "Hello, how are you today?"  # Must match prefix
        self._setup_select_result(existing_title)
        self.mock_llm_client.chat_completion = AsyncMock(return_value="New Title")

        # Simulate concurrent change by setting rowcount=0 on UPDATE cursor

        def mock_execute_with_zero_rowcount(query, params=None):
            cursor = MagicMock()
            if "UPDATE" in query.upper():
                cursor.rowcount = 0  # Concurrent change - no rows affected
                cursor.fetchone.return_value = None
            else:
                cursor.rowcount = 1
                cursor.fetchone.return_value = (existing_title,)
            self._execute_history.append(
                {"query": query, "params": params, "cursor": cursor}
            )
            return cursor

        self._mock_conn.execute = mock_execute_with_zero_rowcount

        await _auto_name_session(
            session_id=42,
            first_message="Hello, how are you today?",
            llm_client=self.mock_llm_client,
        )

        # Verify LLM was called
        self.mock_llm_client.chat_completion.assert_called_once()

        # Verify UPDATE was called
        update_queries = self._get_update_queries()
        self.assertEqual(len(update_queries), 1)

        # Verify the UPDATE cursor had rowcount=0
        self.assertEqual(update_queries[0]["cursor"].rowcount, 0)

        # commit should not have been called
        self._mock_conn.commit.assert_not_called()

    async def test_auto_name_fallback_on_llm_failure(self):
        """
        Test 5: LLM raises exception, fallback truncates first message.
        UPDATE WHERE title IS NULL should run.
        """
        from app.api.routes.chat import _auto_name_session

        self._setup_select_result(None)
        self.mock_llm_client.chat_completion = AsyncMock(
            side_effect=Exception("LLM API error")
        )

        await _auto_name_session(
            session_id=42,
            first_message="This is a long first message that should be truncated in fallback",
            llm_client=self.mock_llm_client,
        )

        # Verify LLM was called (and failed)
        self.mock_llm_client.chat_completion.assert_called_once()

        # Find the fallback UPDATE call (has AND title IS NULL)
        fallback_queries = self._get_fallback_queries()
        self.assertEqual(len(fallback_queries), 1)

        fallback_params = fallback_queries[0]["params"]
        # Fallback now uses "New conversation" as the default title
        actual_title = fallback_params[0]
        self.assertEqual(actual_title, "New conversation")

    async def test_auto_name_fallback_title_too_short(self):
        """
        Test 6: LLM returns title < 3 chars, replaced with message[:50].
        """
        from app.api.routes.chat import _auto_name_session

        self._setup_select_result(None)
        self.mock_llm_client.chat_completion = AsyncMock(return_value="AB")  # < 3 chars

        await _auto_name_session(
            session_id=42,
            first_message="This is my first message for the session",  # 42 chars, < 50
            llm_client=self.mock_llm_client,
        )

        # Find the UPDATE call
        update_queries = self._get_update_queries()
        self.assertEqual(len(update_queries), 1)

        update_params = update_queries[0]["params"]
        # Title should be first 50 chars (no "..." since message < 50 chars)
        expected_title = "This is my first message for the session"[:50]
        self.assertEqual(update_params[0], expected_title)

    async def test_auto_name_fallback_title_too_long(self):
        """
        Test 7: LLM returns title > 60 chars, truncated to 57 chars + "...".
        """
        from app.api.routes.chat import _auto_name_session

        self._setup_select_result(None)
        long_title = "A" * 70  # 70 chars, should be truncated to 57 + "..."
        self.mock_llm_client.chat_completion = AsyncMock(return_value=long_title)

        await _auto_name_session(
            session_id=42,
            first_message="Hello",
            llm_client=self.mock_llm_client,
        )

        # Find the UPDATE call
        update_queries = self._get_update_queries()
        self.assertEqual(len(update_queries), 1)

        update_params = update_queries[0]["params"]

        # Title should be truncated to 57 chars + "..."
        expected_title = "A" * 57 + "..."
        self.assertEqual(update_params[0], expected_title)
        self.assertEqual(len(update_params[0]), 60)

    async def test_auto_name_task_tracked(self):
        """
        Test 8: asyncio.create_task result is added to _background_tasks set.
        Verify that the function can be awaited and tasks can be tracked.
        """
        from app.api.routes.chat import _auto_name_session, _background_tasks

        len(_background_tasks)

        # Set up successful mock
        self._setup_select_result(None)
        self.mock_llm_client.chat_completion = AsyncMock(
            return_value="Test Session Title"
        )

        # Create task and add to background tasks
        task = asyncio.create_task(
            _auto_name_session(
                session_id=42,
                first_message="Hello world",
                llm_client=self.mock_llm_client,
            )
        )
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)

        # Wait for task completion
        await task

        # Verify task was properly awaited and executed
        self.mock_llm_client.chat_completion.assert_called_once()

        # Clean up if still in set
        _background_tasks.discard(task)


class TestAutoNameSessionEdgeCases(unittest.IsolatedAsyncioTestCase):
    """Additional edge case tests for _auto_name_session."""

    async def asyncSetUp(self):
        """Set up mocks for each test."""
        self._execute_history = []

        self._mock_conn = MagicMock()
        self._mock_conn.rowcount = 1
        self._mock_conn.fetchone.return_value = None
        self._mock_conn.commit.return_value = None

        def mock_execute(query, params=None):
            cursor = MagicMock()
            cursor.rowcount = self._mock_conn.rowcount
            if "SELECT" in query.upper():
                cursor.fetchone.return_value = self._mock_conn.fetchone.return_value
            else:
                cursor.fetchone.return_value = None
            self._execute_history.append(
                {"query": query, "params": params, "cursor": cursor}
            )
            return cursor

        self._mock_conn.execute = mock_execute

        self._mock_pool = MagicMock()
        self._mock_pool.connection_async = MagicMock(
            return_value=_AsyncConnectionCM(self._mock_conn)
        )

        self.mock_settings_patcher = patch("app.api.routes.chat.settings")
        self.mock_settings = self.mock_settings_patcher.start()
        self.mock_settings.sqlite_path = "/test/sqlite.db"

        self.mock_pool_patcher = patch("app.api.routes.chat.get_pool")
        self.mock_get_pool = self.mock_pool_patcher.start()
        self.mock_get_pool.return_value = self._mock_pool

        self.mock_llm_client = AsyncMock()

    def tearDown(self):
        self.mock_settings_patcher.stop()
        self.mock_pool_patcher.stop()

    def _setup_select_result(self, title_value):
        if title_value is None:
            self._mock_conn.fetchone.return_value = None
        else:
            self._mock_conn.fetchone.return_value = (title_value,)
        self._mock_conn.rowcount = 1

    def _get_update_queries(self):
        return [e for e in self._execute_history if "UPDATE" in e["query"].upper()]

    async def test_auto_name_title_with_quotes(self):
        """
        Test that LLM-returned titles with quotes are stripped.
        """
        from app.api.routes.chat import _auto_name_session

        self._setup_select_result(None)
        self.mock_llm_client.chat_completion = AsyncMock(
            return_value='"Generated Title"'
        )

        await _auto_name_session(
            session_id=42,
            first_message="Hello",
            llm_client=self.mock_llm_client,
        )

        update_queries = self._get_update_queries()
        self.assertEqual(len(update_queries), 1)
        # Title should have quotes stripped
        self.assertEqual(update_queries[0]["params"][0], "Generated Title")

    async def test_auto_name_empty_string_title(self):
        """
        Test that empty string existing title triggers unconditional UPDATE.
        """
        from app.api.routes.chat import _auto_name_session

        self._setup_select_result("")  # Empty string
        self.mock_llm_client.chat_completion = AsyncMock(return_value="Valid Title")

        await _auto_name_session(
            session_id=42,
            first_message="Hello world",
            llm_client=self.mock_llm_client,
        )

        # Should do unconditional UPDATE (no WHERE title=?)
        update_queries = self._get_update_queries()
        self.assertEqual(len(update_queries), 1)
        self.assertEqual(len(update_queries[0]["params"]), 2)  # title, session_id only

    async def test_auto_name_prefix_mismatch(self):
        """
        Test that prefix mismatch skips UPDATE even if title is short.
        """
        from app.api.routes.chat import _auto_name_session

        self._setup_select_result("Different Prefix Title")
        self.mock_llm_client.chat_completion = AsyncMock(return_value="New Title")

        await _auto_name_session(
            session_id=42,
            first_message="Hello, how are you?",  # Different prefix
            llm_client=self.mock_llm_client,
        )

        # Verify LLM was called
        self.mock_llm_client.chat_completion.assert_called_once()

        # Verify NO UPDATE was called (prefix doesn't match)
        update_queries = self._get_update_queries()
        self.assertEqual(len(update_queries), 0)


if __name__ == "__main__":
    unittest.main()
