"""Tests for wiki job enqueue gating in DocumentProcessor.

Verifies that _WikiStore.create_job is only called when both:
  - settings.wiki_enabled is True
  - settings.wiki_compile_on_ingest is True

Also verifies that wiki_pending flag is always cleared (not left stale).
"""

import asyncio
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

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


def _make_mock_settings(wiki_enabled=True, wiki_compile_on_ingest=True):
    """Create a mock settings object with properly configured attributes."""
    mock = MagicMock()
    # Wiki settings
    mock.wiki_enabled = wiki_enabled
    mock.wiki_compile_on_ingest = wiki_compile_on_ingest
    # Processing settings that affect control flow
    mock.contextual_chunking_enabled = False
    mock.parent_retrieval_enabled = False
    mock.multi_scale_indexing_enabled = False
    mock.chunk_enrichment_enabled = False
    # Chunking settings
    mock.chunk_size_chars = 1200
    mock.chunk_overlap_chars = 120
    # Embedding settings
    mock.embedding_batch_size = 64
    mock.reupload_safe_order = False
    # Return the mock
    return mock


class TestWikiJobEnqueueGating(unittest.IsolatedAsyncioTestCase):
    """Test wiki job creation is gated on wiki_enabled AND wiki_compile_on_ingest."""

    async def asyncSetUp(self):
        from app.models.database import init_db

        self.temp_dir = tempfile.mkdtemp()
        self.temp_db_path = os.path.join(self.temp_dir, 'test.db')
        init_db(self.temp_db_path)

        # Create a simple text file for processing
        self.txt_file_path = os.path.join(self.temp_dir, 'test.txt')
        with open(self.txt_file_path, 'w', encoding='utf-8') as f:
            f.write("Justice Sakyi is the AFOMIS Chief.")

    async def asyncTearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _make_processor(self):
        """Create a DocumentProcessor with mocked external dependencies."""
        from app.services.document_processor import DocumentProcessor

        pool = MagicMock()
        conn = MagicMock()
        pool.get_connection.return_value = conn

        embedding_service = MagicMock()
        embedding_service.embed_batch = AsyncMock(return_value=([[0.1, 0.2]], []))
        vector_store = MagicMock()
        vector_store.init_table = AsyncMock()
        vector_store.delete_by_file = AsyncMock(return_value=0)
        vector_store.add_chunks = AsyncMock(
            return_value={"vector_write_ms": 1.0, "optimize_ms": 2.0}
        )
        vector_store.count_by_file = AsyncMock(return_value=1)

        processor = DocumentProcessor(
            pool=pool,
            embedding_service=embedding_service,
            vector_store=vector_store,
        )
        return processor, pool, conn, vector_store

    def _make_chunk(self, text="Justice Sakyi is the AFOMIS Chief."):
        """Create a properly-configured ProcessedChunk for testing."""
        from app.services.chunking import ProcessedChunk
        return ProcessedChunk(
            text=text,
            metadata={"chunk_scale": "default", "raw_text": text},
            chunk_index=0,
        )

    async def _run_process_file(self, processor, pool, conn, mock_settings, txt_file_path):
        """Run process_file with common mocks applied."""
        chunk = self._make_chunk()

        with patch("app.services.document_processor.settings", mock_settings), \
            patch.object(processor, "_check_duplicate", return_value=None), \
            patch.object(processor, "_insert_or_get_file_record", return_value=123), \
            patch.object(processor, "_update_status"), \
            patch.object(processor, "_validate_chunk_sizes"), \
            patch.object(processor, "_is_schema_file", return_value=False), \
            patch.object(processor, "_is_spreadsheet_file", return_value=False), \
            patch.object(
                processor,
                "_process_document_file",
                new=AsyncMock(return_value=([chunk], "Justice Sakyi is the AFOMIS Chief.")),
            ), \
            patch.object(processor, "_get_chunk_enrichment_service", return_value=None), \
            patch("app.services.document_processor.compute_file_hash", return_value="abc12345"), \
            patch("app.services.document_processor.set_phase"), \
            patch("app.services.document_processor.clear_progress"), \
            patch("app.services.document_processor.compute_parent_windows"), \
            patch("app.services.document_processor.set_wiki_pending") as mock_set_wiki_pending, \
            patch("app.services.wiki_store.WikiStore") as mock_wiki_store_cls:
            mock_wiki_store = MagicMock()
            mock_wiki_store.create_job.return_value = None
            mock_wiki_store_cls.return_value = mock_wiki_store

            await processor.process_file(txt_file_path, vault_id=1)

            return mock_set_wiki_pending, mock_wiki_store

    async def test_wiki_job_not_created_when_wiki_enabled_false(self):
        """When wiki_enabled=False, _WikiStore.create_job must NOT be called."""
        processor, pool, conn, _ = self._make_processor()
        mock_settings = _make_mock_settings(wiki_enabled=False, wiki_compile_on_ingest=True)

        mock_set_wiki_pending, mock_wiki_store = await self._run_process_file(
            processor, pool, conn, mock_settings, self.txt_file_path
        )

        # create_job should NOT have been called because wiki_enabled=False
        mock_wiki_store.create_job.assert_not_called()
        # But set_wiki_pending SHOULD have been called twice: True then False
        self.assertEqual(mock_set_wiki_pending.call_count, 2)
        mock_set_wiki_pending.assert_any_call(pool, 123, True)
        mock_set_wiki_pending.assert_any_call(pool, 123, False)

    async def test_wiki_job_not_created_when_wiki_compile_on_ingest_false(self):
        """When wiki_compile_on_ingest=False, _WikiStore.create_job must NOT be called."""
        processor, pool, conn, _ = self._make_processor()
        mock_settings = _make_mock_settings(wiki_enabled=True, wiki_compile_on_ingest=False)

        mock_set_wiki_pending, mock_wiki_store = await self._run_process_file(
            processor, pool, conn, mock_settings, self.txt_file_path
        )

        # create_job should NOT have been called because wiki_compile_on_ingest=False
        mock_wiki_store.create_job.assert_not_called()
        # set_wiki_pending should have been called twice
        self.assertEqual(mock_set_wiki_pending.call_count, 2)

    async def test_wiki_job_created_when_both_flags_true(self):
        """When both wiki_enabled AND wiki_compile_on_ingest are True, create_job MUST be called."""
        processor, pool, conn, _ = self._make_processor()
        mock_settings = _make_mock_settings(wiki_enabled=True, wiki_compile_on_ingest=True)

        mock_set_wiki_pending, mock_wiki_store = await self._run_process_file(
            processor, pool, conn, mock_settings, self.txt_file_path
        )

        # create_job SHOULD have been called
        mock_wiki_store.create_job.assert_called_once()
        # Verify the call arguments
        mock_wiki_store.create_job.assert_called_with(
            vault_id=1,
            trigger_type="ingest",
            trigger_id="file:123",
            input_json={"file_id": 123, "vault_id": 1},
        )
        # set_wiki_pending should have been called twice
        self.assertEqual(mock_set_wiki_pending.call_count, 2)
        mock_set_wiki_pending.assert_any_call(pool, 123, True)
        mock_set_wiki_pending.assert_any_call(pool, 123, False)

    async def test_wiki_pending_flag_always_cleared(self):
        """Verify wiki_pending is always cleared after processing, regardless of gating."""
        processor, pool, conn, _ = self._make_processor()
        mock_settings = _make_mock_settings(wiki_enabled=True, wiki_compile_on_ingest=False)

        chunk = self._make_chunk()

        with patch("app.services.document_processor.settings", mock_settings), \
            patch.object(processor, "_check_duplicate", return_value=None), \
            patch.object(processor, "_insert_or_get_file_record", return_value=456), \
            patch.object(processor, "_update_status"), \
            patch.object(processor, "_validate_chunk_sizes"), \
            patch.object(processor, "_is_schema_file", return_value=False), \
            patch.object(processor, "_is_spreadsheet_file", return_value=False), \
            patch.object(
                processor,
                "_process_document_file",
                new=AsyncMock(return_value=([chunk], "Justice Sakyi is the AFOMIS Chief.")),
            ), \
            patch.object(processor, "_get_chunk_enrichment_service", return_value=None), \
            patch("app.services.document_processor.compute_file_hash", return_value="abc12345"), \
            patch("app.services.document_processor.set_phase"), \
            patch("app.services.document_processor.clear_progress"), \
            patch("app.services.document_processor.compute_parent_windows"), \
            patch("app.services.document_processor.set_wiki_pending") as mock_set_wiki_pending, \
            patch("app.services.wiki_store.WikiStore") as mock_wiki_store_cls:
            mock_wiki_store = MagicMock()
            mock_wiki_store.create_job.return_value = None
            mock_wiki_store_cls.return_value = mock_wiki_store

            await processor.process_file(self.txt_file_path, vault_id=1)

            # The final call to set_wiki_pending should be with False (cleared)
            last_call = mock_set_wiki_pending.call_args_list[-1]
            self.assertEqual(last_call, unittest.mock.call(pool, 456, False))


class TestWikiJobEnqueueGatingProcessExistingFile(unittest.IsolatedAsyncioTestCase):
    """Test wiki job creation gating in process_existing_file (same gating logic)."""

    async def asyncSetUp(self):
        from app.models.database import init_db

        self.temp_dir = tempfile.mkdtemp()
        self.temp_db_path = os.path.join(self.temp_dir, 'test.db')
        init_db(self.temp_db_path)

        self.txt_file_path = os.path.join(self.temp_dir, 'test.txt')
        with open(self.txt_file_path, 'w', encoding='utf-8') as f:
            f.write("Justice Sakyi is the AFOMIS Chief.")

    async def asyncTearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _make_chunk(self, text="Justice Sakyi is the AFOMIS Chief."):
        """Create a properly-configured ProcessedChunk for testing."""
        from app.services.chunking import ProcessedChunk
        return ProcessedChunk(
            text=text,
            metadata={"chunk_scale": "default", "raw_text": text},
            chunk_index=0,
        )

    async def _run_process_existing_file(self, mock_settings):
        """Run process_existing_file with the given settings and the standard
        mock stack. Returns the (mock_set_wiki_pending, mock_wiki_store) so each
        test can assert against them.

        Mirrors ``_run_process_file`` in the sibling ``process_file`` test
        class so the two paths are exercised with identical mock plumbing
        (issue #276 C3-4 test-parity gap).
        """
        from app.services.document_processor import DocumentProcessor

        pool = MagicMock()
        conn = MagicMock()
        pool.get_connection.return_value = conn

        embedding_service = MagicMock()
        embedding_service.embed_batch = AsyncMock(return_value=([[0.1, 0.2]], []))
        vector_store = MagicMock()
        vector_store.init_table = AsyncMock()
        vector_store.delete_by_file = AsyncMock(return_value=0)
        vector_store.add_chunks = AsyncMock(
            return_value={"vector_write_ms": 1.0, "optimize_ms": 2.0}
        )
        vector_store.count_by_file = AsyncMock(return_value=1)

        processor = DocumentProcessor(
            pool=pool,
            embedding_service=embedding_service,
            vector_store=vector_store,
        )

        chunk = self._make_chunk()

        with patch("app.services.document_processor.settings", mock_settings), \
            patch.object(processor, "_update_status"), \
            patch.object(processor, "_validate_chunk_sizes"), \
            patch.object(processor, "_is_schema_file", return_value=False), \
            patch.object(processor, "_is_spreadsheet_file", return_value=False), \
            patch.object(
                processor,
                "_process_document_file",
                new=AsyncMock(return_value=([chunk], "Justice Sakyi is the AFOMIS Chief.")),
            ), \
            patch.object(processor, "_get_chunk_enrichment_service", return_value=None), \
            patch("app.services.document_processor.compute_file_hash", return_value="abc12345"), \
            patch("app.services.document_processor.set_phase"), \
            patch("app.services.document_processor.clear_progress"), \
            patch("app.services.document_processor.compute_parent_windows"), \
            patch("app.services.document_processor.set_wiki_pending") as mock_set_wiki_pending, \
            patch("app.services.wiki_store.WikiStore") as mock_wiki_store_cls:
            mock_wiki_store = MagicMock()
            mock_wiki_store.create_job.return_value = None
            mock_wiki_store_cls.return_value = mock_wiki_store

            await processor.process_existing_file(
                file_id=789, file_path=self.txt_file_path, vault_id=1
            )

            return mock_set_wiki_pending, mock_wiki_store

    async def test_process_existing_file_respects_gating(self):
        """process_existing_file must NOT create a wiki job when wiki_enabled=False."""
        mock_settings = _make_mock_settings(wiki_enabled=False, wiki_compile_on_ingest=True)

        _mock_set_wiki_pending, mock_wiki_store = await self._run_process_existing_file(mock_settings)

        # create_job should NOT have been called because wiki_enabled=False
        mock_wiki_store.create_job.assert_not_called()

    async def test_process_existing_file_skips_when_compile_on_ingest_false(self):
        """process_existing_file must NOT create a wiki job when wiki_compile_on_ingest=False.

        Closes the test-parity gap with ``process_file`` flagged in issue #276
        (C3-4): the negative-on-compile-flag case was previously untested for
        ``process_existing_file``.
        """
        mock_settings = _make_mock_settings(wiki_enabled=True, wiki_compile_on_ingest=False)

        _mock_set_wiki_pending, mock_wiki_store = await self._run_process_existing_file(mock_settings)

        # create_job should NOT have been called because wiki_compile_on_ingest=False
        mock_wiki_store.create_job.assert_not_called()

    async def test_process_existing_file_enqueues_when_both_flags_true(self):
        """process_existing_file MUST create a wiki job when both flags are True.

        Closes the test-parity gap with ``process_file`` flagged in issue #276
        (C3-4): the positive case was previously untested for
        ``process_existing_file``, so a regression that broke only this path's
        enqueue would have shipped green.
        """
        mock_settings = _make_mock_settings(wiki_enabled=True, wiki_compile_on_ingest=True)

        _mock_set_wiki_pending, mock_wiki_store = await self._run_process_existing_file(mock_settings)

        # create_job SHOULD have been called once with the file's file_id (789)
        mock_wiki_store.create_job.assert_called_once()
        mock_wiki_store.create_job.assert_called_with(
            vault_id=1,
            trigger_type="ingest",
            trigger_id="file:789",
            input_json={"file_id": 789, "vault_id": 1},
        )


if __name__ == '__main__':
    unittest.main()
