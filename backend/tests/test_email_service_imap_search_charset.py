"""
Unit tests for FR-007 / issue #494 EMAIL-003: IMAP SEARCH charset contract.

Verifies that the IMAP search passes UNSEEN as the criteria with
charset=None, matching the installed aioimaplib 2.0.1 signature
``search(*criteria, charset='utf-8')`` (charset is keyword-only and the
default 'utf-8' prefixes the wire command with "CHARSET utf-8").

History: the FR-007 fix changed ``search('UTF-8', 'UNSEEN')`` to
``search(None, 'UNSEEN')`` — a positional None that aioimaplib serializes
as an EMPTY criterion (double space on the wire). Issue #494 Group 9
corrected the form to ``search('UNSEEN', charset=None)``.
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
    """Fake IMAP client mirroring the installed aioimaplib 2.0.1 contracts
    (issue #494 Group 9): greeting returns None on success; commands return
    ``aioimaplib.Response(result, lines)`` namedtuples; ``search`` takes
    criteria positionally with a keyword-only charset and records calls as
    ``(criteria_tuple, charset)``.
    """

    def __init__(self):
        self.selected_mailbox = None
        self.search_calls = []  # List of (criteria_tuple, charset) records
        self.fetched_uids = []
        self.logged_out = False
        self.emails = {}  # uid -> email data

    async def wait_hello_from_server(self):
        return None  # aioimaplib 2.0.1 returns None on success

    async def login(self, username, password):
        return aioimaplib.Response('OK', [b'LOGIN completed'])

    async def select(self, mailbox):
        self.selected_mailbox = mailbox
        return aioimaplib.Response('OK', [b'1 EXISTS'])

    async def search(self, *criteria, charset='utf-8'):
        """Track search calls for verification."""
        self.search_calls.append((criteria, charset))
        uids = ' '.join(self.emails.keys()).encode()
        return aioimaplib.Response('OK', [uids])

    async def fetch(self, uid, message_parts):
        if uid in self.emails:
            email_data = self.emails[uid]
            if 'RFC822.SIZE' in message_parts:
                size = len(email_data['content'])
                return aioimaplib.Response(
                    'OK', [f'{uid} FETCH (UID {uid} RFC822.SIZE {size})'.encode()]
                )
            elif 'RFC822' in message_parts:
                content = email_data['content']
                opening = f'{uid} FETCH (UID {uid} RFC822 {{{len(content)}}}'
                return aioimaplib.Response(
                    'OK', [opening.encode(), content, b')']
                )
        return aioimaplib.Response('OK', [])

    async def logout(self):
        self.logged_out = True
        return aioimaplib.Response('OK', [b'BYE'])


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
        """Test _poll_once calls search() with criteria ('UNSEEN',) and
        charset=None.

        This verifies the issue #494 EMAIL-003 contract:
        search('UNSEEN', charset=None) — the keyword-only charset form of
        the installed aioimaplib 2.0.1 signature — instead of the buggy
        positional search(None, 'UNSEEN').
        """
        fake_imap = TrackingIMAPClient()

        with patch('app.services.email_service.aioimaplib.IMAP4_SSL', return_value=fake_imap):
            # Call _poll_once directly
            await self.service._poll_once()

            # Verify search was called
            self.assertTrue(fake_imap.search_calls, "search() should have been called")

            # Verify exactly one search call
            self.assertEqual(len(fake_imap.search_calls), 1)

            # Verify the criteria are ('UNSEEN',) and the charset is None
            criteria, charset = fake_imap.search_calls[0]
            self.assertEqual(criteria, ('UNSEEN',), "criteria should be ('UNSEEN',)")
            self.assertIsNone(
                charset,
                "charset should be None (not the library default 'utf-8')"
            )

    async def test_poll_once_search_charset_is_not_utf8(self):
        """Test search() is NOT called with 'UTF-8' (or 'utf-8') charset.

        This is the negative test case for FR-007: the original bug
        was using search('UTF-8', 'UNSEEN') which can cause charset
        encoding issues.
        """
        fake_imap = TrackingIMAPClient()

        with patch('app.services.email_service.aioimaplib.IMAP4_SSL', return_value=fake_imap):
            await self.service._poll_once()

            # Verify no search call uses a non-None charset or a positional
            # None criterion (the pre-#494 buggy form)
            for i, (criteria, charset) in enumerate(fake_imap.search_calls):
                with self.subTest(call_index=i):
                    self.assertIsNone(
                        charset,
                        f"search call {i} charset should be None"
                    )
                    self.assertEqual(criteria, ('UNSEEN',))

    async def test_poll_once_search_charset_none_with_multiple_calls(self):
        """Test search() always uses charset=None even with multiple searches.

        This ensures future modifications don't reintroduce the charset bug.
        """
        fake_imap = TrackingIMAPClient()

        with patch('app.services.email_service.aioimaplib.IMAP4_SSL', return_value=fake_imap):
            # Run poll once
            await self.service._poll_once()

            # All search calls should use charset=None
            for i, (criteria, charset) in enumerate(fake_imap.search_calls):
                with self.subTest(call_index=i):
                    self.assertIsNone(charset, f"search call {i} charset should be None")
                    self.assertEqual(criteria, ('UNSEEN',), f"search call {i} criteria should be ('UNSEEN',)")


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
        """Test search() is called with criteria ('UNSEEN',) and charset=None.

        Uses mock to assert the call arguments directly against the
        installed aioimaplib 2.0.1 signature
        ``search(*criteria, charset='utf-8')`` (issue #494 EMAIL-003).
        """
        fake_imap = AsyncMock()
        fake_imap.wait_hello_from_server = AsyncMock(return_value=None)
        fake_imap.login = AsyncMock(
            return_value=aioimaplib.Response('OK', [b'LOGIN completed'])
        )
        fake_imap.select = AsyncMock(
            return_value=aioimaplib.Response('OK', [b'1 EXISTS'])
        )
        fake_imap.search = AsyncMock(
            return_value=aioimaplib.Response('OK', [b''])
        )
        fake_imap.logout = AsyncMock(
            return_value=aioimaplib.Response('OK', [b'BYE'])
        )

        with patch('app.services.email_service.aioimaplib.IMAP4_SSL', return_value=fake_imap):
            await self.service._poll_once()

            # Verify search was called exactly once
            fake_imap.search.assert_called_once()

            # Get the call args: criteria positional, charset keyword-only
            call_args = fake_imap.search.call_args
            self.assertEqual(call_args.args, ('UNSEEN',))
            self.assertIsNone(
                call_args.kwargs.get('charset'),
                f"search() should be called with charset=None, not "
                f"{call_args.kwargs.get('charset')!r}"
            )

    async def test_search_not_called_with_utf8_charset(self):
        """Verify search() is never called with 'UTF-8' charset or a
        positional None criterion.

        This is a regression test ensuring the FR-007 bug (charset as a
        positional argument) and the #494 bug (positional None criterion)
        don't get reintroduced.
        """
        fake_imap = AsyncMock()
        fake_imap.wait_hello_from_server = AsyncMock(return_value=None)
        fake_imap.login = AsyncMock(
            return_value=aioimaplib.Response('OK', [b'LOGIN completed'])
        )
        fake_imap.select = AsyncMock(
            return_value=aioimaplib.Response('OK', [b'1 EXISTS'])
        )
        fake_imap.search = AsyncMock(
            return_value=aioimaplib.Response('OK', [b''])
        )
        fake_imap.logout = AsyncMock(
            return_value=aioimaplib.Response('OK', [b'BYE'])
        )

        with patch('app.services.email_service.aioimaplib.IMAP4_SSL', return_value=fake_imap):
            await self.service._poll_once()

            # Check all search calls avoid the buggy forms
            for call in fake_imap.search.call_args_list:
                self.assertNotIn(
                    'UTF-8', call.args,
                    "search() was called with 'UTF-8' - this is the bug FR-007 fixes"
                )
                self.assertNotIn(
                    None, call.args,
                    "search() was called with a positional None criterion - "
                    "this serializes as an empty criterion on the wire"
                )
                self.assertNotEqual(
                    call.kwargs.get('charset'), 'UTF-8',
                    "search() was called with 'UTF-8' charset - this is the bug that FR-007 fixes"
                )


if __name__ == '__main__':
    import unittest
    unittest.main()
