"""
Unit tests for FR-007: IMAP SEARCH charset fix.

Verifies that the IMAP search uses charset=None for UNSEEN searches
to avoid charset encoding issues with simple ASCII-only criteria.

The fix changed line 222 from:
    search('UTF-8', 'UNSEEN')
to:
    search(None, 'UNSEEN')
"""

import asyncio
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# Stub missing optional dependencies
try:
    import lancedb
except ImportError:
    sys.modules['lancedb'] = types.ModuleType('lancedb')

try:
    import pyarrow
except ImportError:
    sys.modules['pyarrow'] = types.ModuleType('pyarrow')

try:
    from unstructured.partition.auto import partition
except ImportError:
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

try:
    import aioimaplib
except ImportError:
    sys.modules['aioimaplib'] = types.ModuleType('aioimaplib')

from pydantic import SecretStr

from app.config import Settings
from app.models.database import SQLiteConnectionPool, init_db
from app.services.email_service import EmailIngestionService


class FakeBackgroundProcessor:
    """Fake BackgroundProcessor for testing."""

    def __init__(self):
        self.enqueued = []

    async def enqueue(self, file_path, source=None, email_subject=None, email_sender=None):
        self.enqueued.append({
            'file_path': file_path,
            'source': source,
            'email_subject': email_subject,
            'email_sender': email_sender,
        })


class TrackingIMAPClient:
    """Fake IMAP client that tracks search() call arguments."""

    def __init__(self):
        self.selected_mailbox = None
        self.search_calls = []  # List of (charset, criterion) tuples
        self.fetched_uids = []
        self.logged_out = False
        self.emails = {}  # uid -> email data

    async def wait_hello_from_server(self):
        return 'OK'

    async def login(self, username, password):
        return ('OK', None)

    async def select(self, mailbox):
        self.selected_mailbox = mailbox
        return ('OK', None)

    async def search(self, charset, criterion):
        """Track search calls for verification."""
        self.search_calls.append((charset, criterion))
        uids = ' '.join(self.emails.keys()).encode()
        return ('OK', [uids])

    async def fetch(self, uid, parts):
        if uid in self.emails:
            email_data = self.emails[uid]
            if 'RFC822.SIZE' in parts:
                return ('OK', [(b'1 (RFC822.SIZE 100)',)])
            elif 'RFC822' in parts:
                return ('OK', [(b'1 (RFC822)', email_data['content'])])
        return ('OK', [])

    async def logout(self):
        self.logged_out = True
        return ('OK', None)


class TestIMAPSearchCharset(unittest.IsolatedAsyncioTestCase):
    """Test FR-007: IMAP SEARCH charset fix.

    Verifies that _poll_once calls search() with charset=None for UNSEEN searches.
    """

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.settings = self._create_test_settings()
        self.pool = self._create_test_pool()
        self.background_processor = FakeBackgroundProcessor()
        self.service = EmailIngestionService(
            self.settings,
            self.pool,
            self.background_processor
        )

    def tearDown(self):
        self.pool.close_all()
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _create_test_settings(self):
        settings = Settings()
        settings.imap_enabled = True
        settings.imap_host = "test.example.com"
        settings.imap_port = 993
        settings.imap_username = "test@example.com"
        settings.imap_password = SecretStr("password123")
        settings.imap_mailbox = "INBOX"
        settings.imap_poll_interval = 10
        settings.imap_use_ssl = True
        settings.data_dir = Path(self.temp_dir)
        settings.uploads_dir.mkdir(parents=True, exist_ok=True)
        return settings

    def _create_test_pool(self):
        db_path = os.path.join(self.temp_dir, 'test.db')
        init_db(db_path)
        return SQLiteConnectionPool(db_path, max_size=2)

    async def test_poll_once_search_uses_none_charset(self):
        """Test _poll_once calls search() with charset=None for UNSEEN criterion.

        This verifies FR-007 fix: search(None, 'UNSEEN') instead of
        search('UTF-8', 'UNSEEN') to avoid charset encoding issues.
        """
        fake_imap = TrackingIMAPClient()

        with patch('app.services.email_service.aioimaplib.IMAP4_SSL', return_value=fake_imap):
            # Call _poll_once directly
            await self.service._poll_once()

            # Verify search was called
            self.assertTrue(fake_imap.search_calls, "search() should have been called")

            # Verify exactly one search call
            self.assertEqual(len(fake_imap.search_calls), 1)

            # Verify the charset is None and criterion is 'UNSEEN'
            charset, criterion = fake_imap.search_calls[0]
            self.assertIsNone(charset, "charset should be None (not 'UTF-8')")
            self.assertEqual(criterion, 'UNSEEN', "criterion should be 'UNSEEN'")

    async def test_poll_once_search_charset_is_not_utf8(self):
        """Test search() is NOT called with 'UTF-8' charset.

        This is the negative test case for FR-007: the original bug
        was using search('UTF-8', 'UNSEEN') which can cause charset
        encoding issues.
        """
        fake_imap = TrackingIMAPClient()

        with patch('app.services.email_service.aioimaplib.IMAP4_SSL', return_value=fake_imap):
            await self.service._poll_once()

            # Verify no search call uses 'UTF-8' as charset
            for charset, criterion in fake_imap.search_calls:
                self.assertNotEqual(
                    charset, 'UTF-8',
                    "search() should not be called with 'UTF-8' charset"
                )

    async def test_poll_once_search_charset_none_with_multiple_calls(self):
        """Test search() always uses charset=None even with multiple searches.

        This ensures future modifications don't reintroduce the charset bug.
        """
        fake_imap = TrackingIMAPClient()

        with patch('app.services.email_service.aioimaplib.IMAP4_SSL', return_value=fake_imap):
            # Run poll once
            await self.service._poll_once()

            # All search calls should use charset=None
            for i, (charset, criterion) in enumerate(fake_imap.search_calls):
                with self.subTest(call_index=i):
                    self.assertIsNone(charset, f"search call {i} charset should be None")
                    self.assertEqual(criterion, 'UNSEEN', f"search call {i} criterion should be 'UNSEEN'")


class TestIMAPSearchCharsetWithMock(unittest.IsolatedAsyncioTestCase):
    """Test FR-007 using unittest.mock to verify call arguments directly."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.settings = self._create_test_settings()
        self.pool = self._create_test_pool()
        self.background_processor = FakeBackgroundProcessor()
        self.service = EmailIngestionService(
            self.settings,
            self.pool,
            self.background_processor
        )

    def tearDown(self):
        self.pool.close_all()
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _create_test_settings(self):
        settings = Settings()
        settings.imap_enabled = True
        settings.imap_host = "test.example.com"
        settings.imap_port = 993
        settings.imap_username = "test@example.com"
        settings.imap_password = SecretStr("password123")
        settings.imap_mailbox = "INBOX"
        settings.imap_poll_interval = 10
        settings.imap_use_ssl = True
        settings.data_dir = Path(self.temp_dir)
        settings.uploads_dir.mkdir(parents=True, exist_ok=True)
        return settings

    def _create_test_pool(self):
        db_path = os.path.join(self.temp_dir, 'test.db')
        init_db(db_path)
        return SQLiteConnectionPool(db_path, max_size=2)

    async def test_search_called_with_exact_arguments(self):
        """Test search() is called with the exact arguments: (None, 'UNSEEN').

        Uses mock to assert the call arguments directly.
        """
        fake_imap = AsyncMock()
        fake_imap.wait_hello_from_server = AsyncMock(return_value='OK')
        fake_imap.login = AsyncMock(return_value=('OK', None))
        fake_imap.select = AsyncMock(return_value=('OK', None))
        fake_imap.search = AsyncMock(return_value=('OK', [b'']))
        fake_imap.logout = AsyncMock(return_value=('OK', None))

        with patch('app.services.email_service.aioimaplib.IMAP4_SSL', return_value=fake_imap):
            await self.service._poll_once()

            # Verify search was called exactly once
            fake_imap.search.assert_called_once()

            # Get the call args
            call_args = fake_imap.search.call_args
            # call_args is (args, kwargs) - args[0] is charset, args[1] is criterion
            charset = call_args[0][0]
            criterion = call_args[0][1]

            # Assert charset is None (not 'UTF-8')
            self.assertIsNone(
                charset,
                f"search() should be called with charset=None, not '{charset}'"
            )
            # Assert criterion is 'UNSEEN'
            self.assertEqual(criterion, 'UNSEEN')

    async def test_search_not_called_with_utf8_charset(self):
        """Verify search() is never called with 'UTF-8' charset.

        This is a regression test ensuring the bug doesn't get reintroduced.
        """
        fake_imap = AsyncMock()
        fake_imap.wait_hello_from_server = AsyncMock(return_value='OK')
        fake_imap.login = AsyncMock(return_value=('OK', None))
        fake_imap.select = AsyncMock(return_value=('OK', None))
        fake_imap.search = AsyncMock(return_value=('OK', [b'']))
        fake_imap.logout = AsyncMock(return_value=('OK', None))

        with patch('app.services.email_service.aioimaplib.IMAP4_SSL', return_value=fake_imap):
            await self.service._poll_once()

            # Check all search calls don't use 'UTF-8'
            for call in fake_imap.search.call_args_list:
                charset = call[0][0]
                self.assertNotEqual(
                    charset, 'UTF-8',
                    "search() was called with 'UTF-8' charset - this is the bug that FR-007 fixes"
                )


if __name__ == '__main__':
    import unittest
    unittest.main()
