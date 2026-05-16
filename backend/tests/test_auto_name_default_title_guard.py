"""
Tests for FR-009: auto-name heuristic fix (is_default_title guard).

Verifies:
1. "New conversation" title is NOT overwritten by auto-name
2. Auto-generated titles (short, starting with first_message) ARE still overwritten
3. Non-default short titles that don't match prefix are protected
4. NULL/empty titles are handled

The is_default_title guard at chat.py line 1118 protects "New conversation"
from LLM auto-name overwrite.
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


class TestAutoNameDefaultTitleGuard(unittest.IsolatedAsyncioTestCase):
    """Test suite for the is_default_title guard in _auto_name_session."""

    async def asyncSetUp(self):
        """Set up mocks for each test."""
        self._execute_history = []

        self._mock_conn = MagicMock()
        self._mock_conn.rowcount = 1
        self._mock_conn.fetchone.return_value = None
        self._mock_conn.commit.return_value = None

        def mock_execute(query, params=None):
            cursor = MagicMock()
            if "UPDATE" in query.upper():
                cursor.rowcount = 1
            else:
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
        self._cm = MagicMock()
        self._cm.__enter__ = MagicMock(return_value=self._mock_conn)
        self._cm.__exit__ = MagicMock(return_value=None)
        self._mock_pool.connection.return_value = self._cm

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
        """Set up the SELECT query result for fetchone."""
        if title_value is None:
            self._mock_conn.fetchone.return_value = None
        else:
            self._mock_conn.fetchone.return_value = (title_value,)
        self._mock_conn.rowcount = 1

    def _get_update_queries(self):
        """Get all UPDATE queries from history."""
        return [e for e in self._execute_history if "UPDATE" in e["query"].upper()]

    async def test_new_conversation_not_overwritten(self):
        """
        FR-009 Test 1: "New conversation" title is NOT overwritten by auto-name.

        When existing_title is exactly "New conversation", the is_default_title guard
        should prevent the UPDATE even if the prefix matches.
        """
        from app.api.routes.chat import _auto_name_session

        # Existing title is "New conversation" - should be protected
        self._setup_select_result("New conversation")
        self.mock_llm_client.chat_completion = AsyncMock(
            return_value="Hello World Title"
        )

        await _auto_name_session(
            session_id=42,
            first_message="Hello world",  # first_message starts with "Hello"
            llm_client=self.mock_llm_client,
        )

        # LLM was called (we still try to generate)
        self.mock_llm_client.chat_completion.assert_called_once()

        # But NO UPDATE should happen because of is_default_title guard
        update_queries = self._get_update_queries()
        self.assertEqual(
            len(update_queries),
            0,
            "is_default_title guard should prevent UPDATE for 'New conversation' title",
        )

    async def test_auto_generated_title_still_overwritten(self):
        """
        FR-009 Test 2: Auto-generated titles (short, starting with first_message)
        ARE still overwritten.

        When existing_title starts with first_message[:10] AND is NOT "New conversation",
        the UPDATE should proceed.
        """
        from app.api.routes.chat import _auto_name_session

        # Existing title is auto-generated (short, starts with prefix)
        existing_title = "Hello world question"
        self._setup_select_result(existing_title)
        self.mock_llm_client.chat_completion = AsyncMock(
            return_value="Better Title"
        )

        await _auto_name_session(
            session_id=42,
            first_message="Hello world question",  # first 10 chars = "Hello worl"
            llm_client=self.mock_llm_client,
        )

        # LLM was called
        self.mock_llm_client.chat_completion.assert_called_once()

        # UPDATE should happen (is_likely_auto = True since not "New conversation")
        update_queries = self._get_update_queries()
        self.assertEqual(len(update_queries), 1)
        update_params = update_queries[0]["params"]
        # Should have 3 args: (title, session_id, existing_title)
        self.assertEqual(len(update_params), 3)
        self.assertEqual(update_params[0], "Better Title")
        self.assertEqual(update_params[2], existing_title)

    async def test_non_default_short_title_protected(self):
        """
        FR-009 Test 3: Non-default short titles that DON'T match prefix are protected.

        When existing_title is short but doesn't start with first_message[:10],
        the UPDATE should NOT happen.
        """
        from app.api.routes.chat import _auto_name_session

        # Short title that doesn't match the prefix
        existing_title = "Custom short title"
        self._setup_select_result(existing_title)
        self.mock_llm_client.chat_completion = AsyncMock(
            return_value="New Suggested Title"
        )

        await _auto_name_session(
            session_id=42,
            first_message="Hello world question",  # first 10 chars = "Hello worl"
            llm_client=self.mock_llm_client,
        )

        # LLM was called
        self.mock_llm_client.chat_completion.assert_called_once()

        # UPDATE should NOT happen (prefix doesn't match)
        update_queries = self._get_update_queries()
        self.assertEqual(
            len(update_queries),
            0,
            "Prefix mismatch should prevent UPDATE even for short titles",
        )

    async def test_null_title_unconditional_update(self):
        """
        FR-009 Test 4a: NULL titles are handled - unconditional UPDATE.

        When existing_title is NULL, the UPDATE should run unconditionally.
        """
        from app.api.routes.chat import _auto_name_session

        self._setup_select_result(None)
        self.mock_llm_client.chat_completion = AsyncMock(
            return_value="Generated Title"
        )

        await _auto_name_session(
            session_id=42,
            first_message="Hello world",
            llm_client=self.mock_llm_client,
        )

        # UPDATE should happen with 2 args (no WHERE title =)
        update_queries = self._get_update_queries()
        self.assertEqual(len(update_queries), 1)
        self.assertEqual(len(update_queries[0]["params"]), 2)
        self.assertNotIn("WHERE title =", update_queries[0]["query"])

    async def test_empty_string_title_unconditional_update(self):
        """
        FR-009 Test 4b: Empty string titles are handled - unconditional UPDATE.

        When existing_title is "", the UPDATE should run unconditionally.
        """
        from app.api.routes.chat import _auto_name_session

        self._setup_select_result("")
        self.mock_llm_client.chat_completion = AsyncMock(
            return_value="Generated Title"
        )

        await _auto_name_session(
            session_id=42,
            first_message="Hello world",
            llm_client=self.mock_llm_client,
        )

        # UPDATE should happen with 2 args (no WHERE title =)
        update_queries = self._get_update_queries()
        self.assertEqual(len(update_queries), 1)
        self.assertEqual(len(update_queries[0]["params"]), 2)
        self.assertNotIn("WHERE title =", update_queries[0]["query"])


class TestAutoNameDefaultTitleGuardIsolated(unittest.IsolatedAsyncioTestCase):
    """Additional isolated tests for is_default_title logic edge cases."""

    async def asyncSetUp(self):
        """Set up mocks for each test."""
        self._execute_history = []

        self._mock_conn = MagicMock()
        self._mock_conn.rowcount = 1
        self._mock_conn.fetchone.return_value = None
        self._mock_conn.commit.return_value = None

        def mock_execute(query, params=None):
            cursor = MagicMock()
            cursor.rowcount = 1
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
        self._cm = MagicMock()
        self._cm.__enter__ = MagicMock(return_value=self._mock_conn)
        self._cm.__exit__ = MagicMock(return_value=None)
        self._mock_pool.connection.return_value = self._cm

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

    def _get_update_queries(self):
        return [e for e in self._execute_history if "UPDATE" in e["query"].upper()]

    async def test_new_conversation_with_matching_prefix(self):
        """
        Verify "New conversation" is protected even when first_message
        also starts with "New".
        """
        from app.api.routes.chat import _auto_name_session

        self._setup_select_result("New conversation")
        self.mock_llm_client.chat_completion = AsyncMock(
            return_value="New Title Here"
        )

        await _auto_name_session(
            session_id=42,
            first_message="New feature request",  # Also starts with "New"
            llm_client=self.mock_llm_client,
        )

        # NO UPDATE - is_default_title guard should protect "New conversation"
        update_queries = self._get_update_queries()
        self.assertEqual(len(update_queries), 0)

    async def test_new_conversation_exact_match_required(self):
        """
        Verify that only exact "New conversation" is protected.
        Similar titles like "New conversation 2" should be overwritable.
        """
        from app.api.routes.chat import _auto_name_session

        # "New conversation 2" is NOT the default title
        self._setup_select_result("New conversation 2")
        self.mock_llm_client.chat_completion = AsyncMock(
            return_value="Updated Title"
        )

        await _auto_name_session(
            session_id=42,
            first_message="New conversation 2 request",  # matches prefix
            llm_client=self.mock_llm_client,
        )

        # UPDATE should happen since is_default_title = False
        update_queries = self._get_update_queries()
        self.assertEqual(len(update_queries), 1)

    async def test_long_auto_title_not_overwritten(self):
        """
        Verify that even auto-looking titles longer than 60 chars are not overwritten.
        """
        from app.api.routes.chat import _auto_name_session

        long_auto_title = "This is a very long title that looks auto-generated but exceeds 60 chars"
        self._setup_select_result(long_auto_title)
        self.mock_llm_client.chat_completion = AsyncMock(
            return_value="Short Title"
        )

        await _auto_name_session(
            session_id=42,
            first_message="This is a very long title that looks auto-generated but exceeds 60 chars",
            llm_client=self.mock_llm_client,
        )

        # NO UPDATE - title is too long (> 60 chars)
        update_queries = self._get_update_queries()
        self.assertEqual(len(update_queries), 0)


if __name__ == "__main__":
    unittest.main()
