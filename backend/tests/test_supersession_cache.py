"""Unit tests for _supersedes_column_exists caching in RAGEngine._check_supersession."""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.document_retrieval import RAGSource
from app.services.rag_engine import RAGEngine


def make_source(file_id: str) -> RAGSource:
    """Helper to create a RAGSource with a file_id."""
    return RAGSource(
        text=f"content from {file_id}", file_id=file_id, score=0.9, metadata={}
    )


class TestSupersedesColumnCache(unittest.IsolatedAsyncioTestCase):
    """Test suite for _supersedes_column_exists caching behavior."""

    def setUp(self):
        self._ssrf_embed = patch("app.services.embeddings.assert_url_safe")
        self._ssrf_llm = patch("app.services.llm_client.assert_url_safe")
        self._ssrf_embed.start()
        self._ssrf_llm.start()

    def tearDown(self):
        self._ssrf_embed.stop()
        self._ssrf_llm.stop()

    def test_cache_initialized_to_none(self):
        """The cache is None after engine construction (before any supersession check)."""
        engine = RAGEngine()
        self.assertIsNone(engine._supersedes_column_exists)

    @patch("app.services.rag_engine._get_pool")
    async def test_cache_set_after_first_call_when_column_exists(self, mock_get_pool):
        """Cache is set to True after first call when supersedes_file_id column exists."""
        mock_pool = MagicMock()
        mock_conn = MagicMock()
        mock_pool.connection.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_pool.connection.return_value.__exit__ = MagicMock(return_value=False)
        mock_get_pool.return_value = mock_pool

        # Mock PRAGMA result WITH supersedes_file_id column
        mock_cursor_pragma = MagicMock()
        mock_cursor_pragma.fetchall.return_value = [
            (0, "id", "TEXT", 0, None, 0),
            (1, "file_name", "TEXT", 0, None, 0),
            (2, "supersedes_file_id", "TEXT", 0, None, 0),
            (3, "status", "TEXT", 0, None, 0),
        ]

        # Mock query returning no superseded files
        mock_cursor_query = MagicMock()
        mock_cursor_query.fetchall.return_value = []

        mock_conn.execute.side_effect = [mock_cursor_pragma, mock_cursor_query]

        engine = RAGEngine()
        sources = [make_source("file123")]

        # Before first call, cache is None
        self.assertIsNone(engine._supersedes_column_exists)

        # First call
        await engine._check_supersession(sources)

        # After first call, cache should be True (column exists)
        self.assertTrue(engine._supersedes_column_exists)

    @patch("app.services.rag_engine._get_pool")
    async def test_cache_set_after_first_call_when_column_missing(self, mock_get_pool):
        """Cache is set to False after first call when supersedes_file_id column is missing."""
        mock_pool = MagicMock()
        mock_conn = MagicMock()
        mock_pool.connection.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_pool.connection.return_value.__exit__ = MagicMock(return_value=False)
        mock_get_pool.return_value = mock_pool

        # Mock PRAGMA result WITHOUT supersedes_file_id column
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = [
            (0, "id", "TEXT", 0, None, 0),
            (1, "file_name", "TEXT", 0, None, 0),
            (2, "status", "TEXT", 0, None, 0),
        ]
        mock_conn.execute.return_value = mock_cursor

        engine = RAGEngine()
        sources = [make_source("file123")]

        # Before first call, cache is None
        self.assertIsNone(engine._supersedes_column_exists)

        # First call
        await engine._check_supersession(sources)

        # After first call, cache should be False (column does not exist)
        self.assertFalse(engine._supersedes_column_exists)

    @patch("app.services.rag_engine._get_pool")
    async def test_cache_avoids_second_pragma_call_on_subsequent_calls(self, mock_get_pool):
        """Subsequent calls use the cached value and do not call PRAGMA again."""
        mock_pool = MagicMock()
        mock_conn = MagicMock()
        mock_pool.connection.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_pool.connection.return_value.__exit__ = MagicMock(return_value=False)
        mock_get_pool.return_value = mock_pool

        # Mock PRAGMA result WITH supersedes_file_id column
        mock_cursor_pragma = MagicMock()
        mock_cursor_pragma.fetchall.return_value = [
            (0, "id", "TEXT", 0, None, 0),
            (1, "file_name", "TEXT", 0, None, 0),
            (2, "supersedes_file_id", "TEXT", 0, None, 0),
            (3, "status", "TEXT", 0, None, 0),
        ]

        # Mock query returning no superseded files
        mock_cursor_query = MagicMock()
        mock_cursor_query.fetchall.return_value = []

        mock_conn.execute.side_effect = [mock_cursor_pragma, mock_cursor_query]

        engine = RAGEngine()
        sources = [make_source("file123")]

        # First call - populates cache
        await engine._check_supersession(sources)
        self.assertTrue(engine._supersedes_column_exists)

        # Reset the mock to track new calls
        mock_conn.execute.reset_mock()
        mock_conn.execute.side_effect = [mock_cursor_query]

        # Second call - should use cache, not call PRAGMA again
        await engine._check_supersession(sources)

        # PRAGMA should NOT be called again - only the actual supersession query
        execute_calls = mock_conn.execute.call_args_list
        pragma_calls = [
            c for c in execute_calls
            if isinstance(c[0][0], str) and "PRAGMA" in c[0][0]
        ]
        self.assertEqual(
            0, len(pragma_calls),
            f"PRAGMA should not be called when cache is set. Called: {pragma_calls}"
        )
        # The supersession query SHOULD be called (since column exists)
        query_calls = [
            c for c in execute_calls
            if isinstance(c[0][0], str) and "supersedes_file_id" in c[0][0]
        ]
        self.assertGreater(len(query_calls), 0)

    @patch("app.services.rag_engine._get_pool")
    async def test_cache_prevents_pragma_call_when_column_missing(self, mock_get_pool):
        """When cache is False, subsequent calls do not call PRAGMA."""
        mock_pool = MagicMock()
        mock_conn = MagicMock()
        mock_pool.connection.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_pool.connection.return_value.__exit__ = MagicMock(return_value=False)
        mock_get_pool.return_value = mock_pool

        # Mock PRAGMA result WITHOUT supersedes_file_id column
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = [
            (0, "id", "TEXT", 0, None, 0),
            (1, "file_name", "TEXT", 0, None, 0),
            (2, "status", "TEXT", 0, None, 0),
        ]
        mock_conn.execute.return_value = mock_cursor

        engine = RAGEngine()
        sources = [make_source("file123")]

        # First call - sets cache to False
        await engine._check_supersession(sources)
        self.assertFalse(engine._supersedes_column_exists)

        # Reset to track new calls
        mock_conn.execute.reset_mock()

        # Second call - should use cache and not call PRAGMA
        await engine._check_supersession(sources)

        # PRAGMA should NOT be called again
        pragma_calls = [
            c for c in mock_conn.execute.call_args_list
            if isinstance(c[0][0], str) and "PRAGMA" in c[0][0]
        ]
        self.assertEqual(
            0, len(pragma_calls),
            f"PRAGMA should not be called when cache is False. Called: {pragma_calls}"
        )

    @patch("app.services.rag_engine._get_pool")
    async def test_cache_is_instance_specific(self, mock_get_pool):
        """Each engine instance has its own cache."""
        mock_pool = MagicMock()
        mock_conn = MagicMock()
        mock_pool.connection.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_pool.connection.return_value.__exit__ = MagicMock(return_value=False)
        mock_get_pool.return_value = mock_pool

        # Mock PRAGMA result WITH supersedes_file_id column
        mock_cursor_pragma = MagicMock()
        mock_cursor_pragma.fetchall.return_value = [
            (0, "id", "TEXT", 0, None, 0),
            (1, "file_name", "TEXT", 0, None, 0),
            (2, "supersedes_file_id", "TEXT", 0, None, 0),
            (3, "status", "TEXT", 0, None, 0),
        ]

        mock_cursor_query = MagicMock()
        mock_cursor_query.fetchall.return_value = []

        mock_conn.execute.side_effect = [mock_cursor_pragma, mock_cursor_query]

        engine1 = RAGEngine()
        engine2 = RAGEngine()

        sources = [make_source("file123")]

        # First call on engine1 - populates its cache
        await engine1._check_supersession(sources)
        self.assertTrue(engine1._supersedes_column_exists)
        self.assertIsNone(engine2._supersedes_column_exists)

        # engine2's cache is independent
        self.assertIsNone(engine2._supersedes_column_exists)

    @patch("app.services.rag_engine.asyncio.to_thread")
    @patch("app.services.rag_engine._get_pool")
    async def test_probe_runs_via_asyncio_to_thread(self, mock_get_pool, mock_to_thread):
        """The column existence probe is called via asyncio.to_thread."""
        mock_pool = MagicMock()
        mock_conn = MagicMock()
        mock_pool.connection.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_pool.connection.return_value.__exit__ = MagicMock(return_value=False)
        mock_get_pool.return_value = mock_pool

        # Make asyncio.to_thread call the actual function synchronously
        def run_sync(fn):
            return fn()
        mock_to_thread.side_effect = run_sync

        # Mock PRAGMA result WITH supersedes_file_id column
        mock_cursor_pragma = MagicMock()
        mock_cursor_pragma.fetchall.return_value = [
            (0, "id", "TEXT", 0, None, 0),
            (1, "file_name", "TEXT", 0, None, 0),
            (2, "supersedes_file_id", "TEXT", 0, None, 0),
            (3, "status", "TEXT", 0, None, 0),
        ]

        mock_cursor_query = MagicMock()
        mock_cursor_query.fetchall.return_value = []

        mock_conn.execute.side_effect = [mock_cursor_pragma, mock_cursor_query]

        engine = RAGEngine()
        sources = [make_source("file123")]

        await engine._check_supersession(sources)

        # Verify asyncio.to_thread was called twice:
        # 1. For the column-existence probe
        # 2. For the actual supersession query (since column exists)
        self.assertEqual(2, mock_to_thread.call_count)
        # And cache was set correctly
        self.assertTrue(engine._supersedes_column_exists)

    # REALIGNMENT (issue #510 RAG-008): the two tests that previously asserted
    # "except → cache False" were superseded. A transient probe failure (pool
    # exhaustion, locked DB) must NOT be cached as "column absent" — the cache
    # stays None (unknown) so the next query re-probes. Only a SUCCESSFUL
    # observation may populate the cache (a successful absent-column probe may
    # still cache False — covered by test_cache_set_after_first_call_when_column_missing
    # and test_cache_prevents_pragma_call_when_column_missing above).

    @patch("app.services.rag_engine.asyncio.to_thread")
    @patch("app.services.rag_engine._get_pool")
    async def test_probe_exception_leaves_cache_none(self, mock_get_pool, mock_to_thread):
        """A probe exception is suppressed but caches NOTHING (stays None)."""
        mock_to_thread.side_effect = RuntimeError("Pool connection failed")

        engine = RAGEngine()
        sources = [make_source("file123")]

        # Before call, cache is None
        self.assertIsNone(engine._supersedes_column_exists)

        # Call should not raise - errors are suppressed
        result = await engine._check_supersession(sources)

        # Result should be None (check skipped)
        self.assertIsNone(result)
        # Cache must remain None (unknown) — a transient failure must never
        # be recorded as "column absent".
        self.assertIsNone(engine._supersedes_column_exists)

    @patch("app.services.rag_engine.asyncio.to_thread")
    @patch("app.services.rag_engine._get_pool")
    async def test_probe_exception_does_not_permanently_cache_absence(
        self, mock_get_pool, mock_to_thread
    ):
        """After a transient probe failure the next call RE-PROBES; once the
        pool recovers, the supersession warning is actually returned."""
        mock_pool = MagicMock()
        mock_conn = MagicMock()
        mock_pool.connection.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_pool.connection.return_value.__exit__ = MagicMock(return_value=False)
        mock_get_pool.return_value = mock_pool

        # PRAGMA result WITH supersedes_file_id + a newer file row.
        mock_cursor_pragma = MagicMock()
        mock_cursor_pragma.fetchall.return_value = [
            (0, "id", "TEXT", 0, None, 0),
            (1, "file_name", "TEXT", 0, None, 0),
            (2, "supersedes_file_id", "TEXT", 0, None, 0),
            (3, "status", "TEXT", 0, None, 0),
        ]
        mock_cursor_query = MagicMock()
        mock_cursor_query.fetchall.return_value = [("report_v2.pdf",)]
        mock_conn.execute.side_effect = [mock_cursor_pragma, mock_cursor_query]

        to_thread_calls = []

        def run_sync(fn):
            to_thread_calls.append(fn)
            if len(to_thread_calls) == 1:
                raise RuntimeError("Pool connection failed")  # transient only
            return fn()

        mock_to_thread.side_effect = run_sync

        engine = RAGEngine()
        sources = [make_source("file123")]

        # First call: probe fails transiently — no warning, cache unknown.
        first = await engine._check_supersession(sources)
        self.assertIsNone(first)
        self.assertIsNone(engine._supersedes_column_exists)

        # Second call: re-probes (NOT satisfied by a stale cached absence).
        second = await engine._check_supersession(sources)
        # After recovery the warning is returned.
        self.assertIsNotNone(second)
        self.assertIn("superseded", second)
        # Probe ran again, then the supersession query: 3 to_thread calls total.
        self.assertEqual(3, len(to_thread_calls))

    @patch("app.services.rag_engine._get_pool")
    async def test_empty_sources_returns_none_without_probe(self, mock_get_pool):
        """When sources have no file_ids, _check_supersession returns None without probing."""
        mock_get_pool.return_value = MagicMock()

        engine = RAGEngine()

        # Call with empty sources
        result = await engine._check_supersession([])

        # Should return None immediately
        self.assertIsNone(result)
        # Cache should remain None (never probed)
        self.assertIsNone(engine._supersedes_column_exists)
        # Pool should not have been accessed
        mock_get_pool.return_value.connection.assert_not_called()

    @patch("app.services.rag_engine._get_pool")
    async def test_sources_with_none_file_id_returns_none_without_probe(self, mock_get_pool):
        """When sources have no valid file_ids, _check_supersession returns None without probing."""
        mock_get_pool.return_value = MagicMock()

        engine = RAGEngine()
        # RAGSource with no file_id
        sources = [RAGSource(text="content", file_id=None, score=0.9, metadata={})]

        result = await engine._check_supersession(sources)

        # Should return None immediately
        self.assertIsNone(result)
        # Cache should remain None (never probed)
        self.assertIsNone(engine._supersedes_column_exists)


if __name__ == "__main__":
    unittest.main()
