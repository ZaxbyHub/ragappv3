"""
Tests for _enqueue_wiki_compile_job function signature changes.

Verifies that:
1. The function accepts the simplified signature (no session_id/assistant_message_id).
2. The function's source no longer references session_id or "session:unknown".
"""

import asyncio
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# Stub missing optional dependencies
try:
    import lancedb
except ImportError:
    import types
    sys.modules['lancedb'] = types.ModuleType('lancedb')

try:
    import pyarrow
except ImportError:
    import types
    sys.modules['pyarrow'] = types.ModuleType('pyarrow')

try:
    from unstructured.partition.auto import partition
except ImportError:
    import types
    _unstructured = types.ModuleType('unstructured')
    _unstructured.__path__ = []
    _unstructured.partition = types.ModuleType('unstructured.partition')
    _unstructured.partition.__path__ = []
    _unstructured.partition.auto = types.ModuleType('unstructured.partition.auto')
    _unstructured.partition.auto.partition = lambda *args, **kwargs: []
    _unstructured.chunking = types.ModuleType('unstructured.chunking')
    _unstructured.chunking.__path__ = []
    _unstructured.chunking.title = types.ModuleType('unstructured.chunking.title')
    _unstructured.chunking.title.chunk_by_title = lambda *args, **kwargs: []
    _unstructured.documents = types.ModuleType('unstructured.documents')
    _unstructured.documents.__path__ = []
    _unstructured.documents.elements = types.ModuleType('unstructured.documents.elements')
    _unstructured.documents.elements.Element = type('Element', (), {})
    sys.modules['unstructured'] = _unstructured
    sys.modules['unstructured.partition'] = _unstructured.partition
    sys.modules['unstructured.partition.auto'] = _unstructured.partition.auto
    sys.modules['unstructured.chunking'] = _unstructured.chunking
    sys.modules['unstructured.chunking.title'] = _unstructured.chunking.title
    sys.modules['unstructured.documents'] = _unstructured.documents
    sys.modules['unstructured.documents.elements'] = _unstructured.documents.elements

from app.api.routes.chat import _enqueue_wiki_compile_job


class TestEnqueueWikiCompileJobSignature(unittest.IsolatedAsyncioTestCase):
    """Test suite for _enqueue_wiki_compile_job function."""

    @patch('app.api.routes.chat.get_pool')
    async def test_enqueue_accepts_simplified_signature(self, mock_get_pool):
        """
        Verify _enqueue_wiki_compile_job accepts the simplified signature
        and does not raise TypeError for unexpected keyword arguments.
        """
        # Set up mock pool so real DB is not accessed
        mock_pool = MagicMock()
        mock_conn = MagicMock()
        mock_pool.connection.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_pool.connection.return_value.__exit__ = MagicMock(return_value=False)
        mock_get_pool.return_value = mock_pool

        # Mock WikiStore.create_job so it is not called with a real conn
        with patch('app.api.routes.chat.WikiStore'):
            # This must not raise TypeError about session_id or assistant_message_id
            await _enqueue_wiki_compile_job(
                vault_id=1,
                user_query="What is AFOMIS?",
                assistant_answer="AFOMIS is a system.",
                wiki_refs=[{"wiki_label": "AFOMIS"}],
                doc_sources=[{"source_label": "Doc1"}],
                memories=[],
            )

        # Verify get_pool was called (function uses it)
        mock_get_pool.assert_called_once()
        # Verify the context manager was entered (pool.connection())
        mock_pool.connection.return_value.__enter__.assert_called_once()

    def test_enqueue_does_not_reference_session_id(self):
        """
        Verify the source of _enqueue_wiki_compile_job no longer contains
        'session_id' or 'session:unknown' references.
        """
        import inspect
        source = inspect.getsource(_enqueue_wiki_compile_job)

        self.assertNotIn(
            "session_id",
            source,
            "_enqueue_wiki_compile_job must not reference 'session_id'"
        )
        self.assertNotIn(
            "session:unknown",
            source,
            "_enqueue_wiki_compile_job must not reference 'session:unknown'"
        )


if __name__ == "__main__":
    unittest.main()
