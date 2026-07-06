"""
Unit tests for EmailIngestionService.

Tests cover:
- Vault name extraction from subject tags
- Attachment validation (MIME types, size limits)
- Email parsing (text/HTML, multipart)
- IMAP connection (exponential backoff, auth failure)
- Service integration (start/stop, health check)
"""

import asyncio
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

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
from app.models.database import SQLiteConnectionPool
from app.services.email_service import EmailIngestionService


class FakeBackgroundProcessor:
    """Fake BackgroundProcessor for testing."""

    def __init__(self):
        self.enqueued = []

    async def enqueue(self, file_path, source=None, email_subject=None, email_sender=None, vault_id=None):
        self.enqueued.append({
            'file_path': file_path,
            'source': source,
            'email_subject': email_subject,
            'email_sender': email_sender,
            'vault_id': vault_id,
        })


class FakeIMAPClient:
    """Fake aioimaplib.IMAP4_SSL for testing."""

    def __init__(self):
        self.selected_mailbox = None
        self.searched = False
        self.fetched_uids = []
        self.logged_out = False
        self.emails = {}  # uid -> email data
        self.stored_flags = {}  # uid -> list of store calls

    async def wait_hello_from_server(self):
        return 'OK'

    async def login(self, username, password):
        return ('OK', None)

    async def select(self, mailbox):
        self.selected_mailbox = mailbox
        return ('OK', None)

    async def search(self, charset, criterion):
        self.searched = True
        uids = ' '.join(self.emails.keys()).encode()
        return ('OK', [uids])

    async def fetch(self, uid, parts):
        if uid in self.emails:
            email_data = self.emails[uid]
            if 'RFC822.SIZE' in parts:
                size = len(email_data['content'])
                return ('OK', [(f'{uid} (RFC822.SIZE {size})'.encode(),)])
            elif 'RFC822' in parts:
                # Real aioimaplib returns: [(b'uid (RFC822 {size})', b'...email content...')]
                # where data[0][0] is the response string and data[0][1] is the email bytes
                return ('OK', [(b'1 (RFC822)', email_data['content'])])
        return ('OK', [])

    async def store(self, uid, *args):
        """Record a STORE command (e.g. +FLAGS \\Seen) for test assertions."""
        self.stored_flags.setdefault(uid, []).append(args)
        return ('OK', None)

    async def logout(self):
        self.logged_out = True
        return ('OK', None)


class TestVaultNameExtraction(unittest.TestCase):
    """Test vault name extraction from subject tags."""

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
        settings.data_dir = Path(self.temp_dir)
        settings.uploads_dir.mkdir(parents=True, exist_ok=True)
        return settings

    def _create_test_pool(self):
        db_path = os.path.join(self.temp_dir, 'test.db')
        from app.models.database import init_db
        init_db(db_path)
        return SQLiteConnectionPool(db_path, max_size=2)

    def test_extract_vault_name_bracket_pattern(self):
        """Test [VaultName] bracket pattern extraction."""
        subject = "Document regarding [ProjectX] specifications"
        vault_name = self.service._extract_vault_name(subject)
        self.assertEqual(vault_name, "ProjectX")

    def test_extract_vault_name_hashtag_pattern(self):
        """Test #vaultname hashtag pattern extraction."""
        subject = "Document regarding #marketing specifications"
        vault_name = self.service._extract_vault_name(subject)
        self.assertEqual(vault_name, "marketing")

    def test_extract_vault_name_no_tag_returns_none(self):
        """Test no tag returns None."""
        subject = "Document regarding project specifications"
        vault_name = self.service._extract_vault_name(subject)
        self.assertIsNone(vault_name)

    def test_extract_vault_name_bracket_hashtag_priority(self):
        """Test bracket pattern takes priority over hashtag."""
        subject = "[HR] Document with #finance tag"
        vault_name = self.service._extract_vault_name(subject)
        self.assertEqual(vault_name, "HR")  # First pattern wins

    def test_extract_vault_name_case_insensitive(self):
        """Test case insensitivity (extraction preserves case)."""
        subject = "Document for [SALES]"
        vault_name = self.service._extract_vault_name(subject)
        self.assertEqual(vault_name, "SALES")

    def test_extract_vault_name_multiple_bracket_tags(self):
        """Test multiple bracket tags (first wins)."""
        subject = "[TeamA] forwarded from [TeamB] document"
        vault_name = self.service._extract_vault_name(subject)
        self.assertEqual(vault_name, "TeamA")

    def test_extract_vault_name_multiple_hashtag_tags(self):
        """Test multiple hashtag tags (first wins)."""
        subject = "#dev discussion about #ops issue"
        vault_name = self.service._extract_vault_name(subject)
        self.assertEqual(vault_name, "dev")

    def test_extract_vault_name_empty_brackets(self):
        """Test empty brackets return None."""
        subject = "Document with [] empty tag"
        vault_name = self.service._extract_vault_name(subject)
        self.assertIsNone(vault_name)

    def test_extract_vault_name_whitespace_handling(self):
        """Test whitespace trimming around tag content."""
        subject = "Document [  spaced  ] with spaces"
        vault_name = self.service._extract_vault_name(subject)
        self.assertEqual(vault_name, "spaced")


class TestAttachmentValidation(unittest.TestCase):
    """Test attachment validation logic."""

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
        settings.data_dir = Path(self.temp_dir)
        settings.uploads_dir.mkdir(parents=True, exist_ok=True)
        return settings

    def _create_test_pool(self):
        db_path = os.path.join(self.temp_dir, 'test.db')
        from app.models.database import init_db
        init_db(db_path)
        return SQLiteConnectionPool(db_path, max_size=2)

    def _create_attachment_part(self, content_type, payload):
        """Helper to create an email attachment part."""
        part = EmailMessage()
        part.set_content(payload, maintype=content_type.split('/')[0],
                         subtype=content_type.split('/')[1])
        part.add_header('Content-Disposition', 'attachment')
        return part

    def test_validate_pdf_mime_type_allowed(self):
        """Test PDF MIME type passes validation."""
        part = self._create_attachment_part('application/pdf', b'fake pdf content')
        is_valid, reason = self.service._validate_attachment(part)
        self.assertTrue(is_valid)
        self.assertEqual(reason, "")

    def test_validate_plain_text_mime_type_allowed(self):
        """Test plain text MIME type passes validation."""
        part = self._create_attachment_part('text/plain', b'Hello world')
        is_valid, reason = self.service._validate_attachment(part)
        self.assertTrue(is_valid)
        self.assertEqual(reason, "")

    def test_validate_markdown_mime_type_allowed(self):
        """Test markdown MIME type passes validation."""
        part = self._create_attachment_part('text/markdown', b'# Title')
        is_valid, reason = self.service._validate_attachment(part)
        self.assertTrue(is_valid)
        self.assertEqual(reason, "")

    def test_validate_docx_mime_type_allowed(self):
        """Test DOCX MIME type passes validation."""
        part = self._create_attachment_part(
            'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
            b'fake docx content'
        )
        is_valid, reason = self.service._validate_attachment(part)
        self.assertTrue(is_valid)
        self.assertEqual(reason, "")

    def test_validate_csv_mime_type_allowed(self):
        """Test CSV MIME type passes validation."""
        part = self._create_attachment_part('text/csv', b'name,value\nfoo,bar')
        is_valid, reason = self.service._validate_attachment(part)
        self.assertTrue(is_valid)
        self.assertEqual(reason, "")

    def test_validate_json_mime_type_allowed(self):
        """Test JSON MIME type passes validation."""
        part = self._create_attachment_part('application/json', b'{"key": "value"}')
        is_valid, reason = self.service._validate_attachment(part)
        self.assertTrue(is_valid)
        self.assertEqual(reason, "")

    def test_validate_disallowed_mime_type_fails(self):
        """Test disallowed MIME type fails validation."""
        part = self._create_attachment_part('application/zip', b'fake zip content')
        is_valid, reason = self.service._validate_attachment(part)
        self.assertFalse(is_valid)
        self.assertIn("Disallowed MIME type", reason)

    def test_validate_exe_mime_type_fails(self):
        """Test EXE MIME type fails validation."""
        part = self._create_attachment_part('application/x-msdownload', b'fake exe content')
        is_valid, reason = self.service._validate_attachment(part)
        self.assertFalse(is_valid)
        self.assertIn("Disallowed MIME type", reason)

    def test_validate_image_mime_type_fails(self):
        """Test image MIME type fails validation."""
        part = self._create_attachment_part('image/jpeg', b'fake jpeg content')
        is_valid, reason = self.service._validate_attachment(part)
        self.assertFalse(is_valid)
        self.assertIn("Disallowed MIME type", reason)

    def test_validate_size_limit_enforced(self):
        """Test size limit is enforced."""
        large_payload = b'x' * (11 * 1024 * 1024)  # 11MB > default 10MB
        part = self._create_attachment_part('application/pdf', large_payload)
        is_valid, reason = self.service._validate_attachment(part)
        self.assertFalse(is_valid)
        self.assertIn("File too large", reason)

    def test_validate_exactly_at_size_limit_passes(self):
        """Test attachment exactly at size limit passes."""
        exact_payload = b'x' * (10 * 1024 * 1024)  # Exactly 10MB
        part = self._create_attachment_part('application/pdf', exact_payload)
        is_valid, reason = self.service._validate_attachment(part)
        self.assertTrue(is_valid)
        self.assertEqual(reason, "")

    def test_validate_oversized_attachment_rejected(self):
        """Test oversized attachment is rejected."""
        huge_payload = b'x' * (20 * 1024 * 1024)  # 20MB
        part = self._create_attachment_part('application/pdf', huge_payload)
        is_valid, reason = self.service._validate_attachment(part)
        self.assertFalse(is_valid)
        self.assertIn("File too large", reason)
        self.assertIn("20.00MB", reason)

    def test_validate_empty_attachment_passes(self):
        """Test empty attachment passes validation."""
        part = self._create_attachment_part('text/plain', b'')
        is_valid, reason = self.service._validate_attachment(part)
        self.assertTrue(is_valid)
        self.assertEqual(reason, "")


class TestEmailParsing(unittest.TestCase):
    """Test email parsing and extraction."""

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
        settings.data_dir = Path(self.temp_dir)
        settings.uploads_dir.mkdir(parents=True, exist_ok=True)
        return settings

    def _create_test_pool(self):
        db_path = os.path.join(self.temp_dir, 'test.db')
        from app.models.database import init_db
        init_db(db_path)
        return SQLiteConnectionPool(db_path, max_size=2)

    def test_decode_header_value_simple_text(self):
        """Test decoding simple text header value."""
        value = "Simple Subject"
        decoded = self.service._decode_header_value(value)
        self.assertEqual(decoded, "Simple Subject")

    def test_decode_header_value_encoded_utf8(self):
        """Test decoding UTF-8 encoded header value."""
        value = "=?utf-8?B?5Lit5paH?="  # "中文" in base64
        decoded = self.service._decode_header_value(value)
        self.assertEqual(decoded, "中文")

    def test_decode_header_value_multiple_parts(self):
        """Test decoding header with multiple encoded parts."""
        value = "Hello =?utf-8?B?5Lit5paH?= World"
        decoded = self.service._decode_header_value(value)
        self.assertEqual(decoded, "Hello 中文 World")

    def test_decode_header_value_empty_returns_empty(self):
        """Test empty header value returns empty string."""
        decoded = self.service._decode_header_value("")
        self.assertEqual(decoded, "")

    def test_decode_header_value_none_returns_empty(self):
        """Test None header value returns empty string."""
        decoded = self.service._decode_header_value(None)
        self.assertEqual(decoded, "")

    def test_sanitize_html_allowed_tags(self):
        """Test HTML sanitization allows only allowed tags."""
        html = "<p>Hello <strong>world</strong></p><script>alert('xss')</script>"
        sanitized = self.service._sanitize_html(html)
        self.assertIn("<p>Hello <strong>world</strong></p>", sanitized)
        self.assertNotIn("<script>", sanitized)
        # Note: bleach strips tags but leaves text content
        self.assertIn("alert", sanitized)

    def test_sanitize_html_strips_disallowed_tags(self):
        """Test HTML sanitization strips disallowed tags."""
        html = "<div><h1>Title</h1></div><p>Content</p>"
        sanitized = self.service._sanitize_html(html)
        self.assertNotIn("<div>", sanitized)
        self.assertNotIn("<h1>", sanitized)
        self.assertIn("Title", sanitized)
        self.assertIn("<p>", sanitized)

    def test_sanitize_html_strips_comments(self):
        """Test HTML sanitization strips comments."""
        html = "<p>Content</p><!-- This is a comment --><p>More content</p>"
        sanitized = self.service._sanitize_html(html)
        self.assertNotIn("<!-- This is a comment -->", sanitized)
        self.assertIn("<p>Content</p>", sanitized)
        self.assertIn("<p>More content</p>", sanitized)

    def test_sanitize_log_value_removes_newlines(self):
        """Test log sanitization removes newlines."""
        value = "Line 1\nLine 2\r\nLine 3"
        sanitized = self.service._sanitize_log_value(value)
        self.assertNotIn("\n", sanitized)
        self.assertNotIn("\r", sanitized)
        # Note: \r\n is replaced with two spaces (one for \r, one for \n)
        self.assertIn("Line 1 Line 2", sanitized)
        self.assertIn("Line 3", sanitized)

    def test_sanitize_log_value_removes_control_chars(self):
        """Test log sanitization removes control characters."""
        value = "Text\x00with\x1fcontrol\x7fchars"
        sanitized = self.service._sanitize_log_value(value)
        self.assertNotIn("\x00", sanitized)
        self.assertNotIn("\x1f", sanitized)
        self.assertNotIn("\x7f", sanitized)

    def test_sanitize_log_value_empty_returns_empty(self):
        """Test empty log value returns empty string."""
        sanitized = self.service._sanitize_log_value("")
        self.assertEqual(sanitized, "")

    def test_sanitize_log_value_none_returns_empty(self):
        """Test None log value returns empty string."""
        sanitized = self.service._sanitize_log_value(None)
        self.assertEqual(sanitized, "")

    def test_sanitize_filename_removes_path_traversal(self):
        """Test filename sanitization removes path traversal."""
        filename = "../../../etc/passwd"
        sanitized = self.service._sanitize_filename(filename)
        self.assertEqual(sanitized, "passwd")

    def test_sanitize_filename_windows_path_traversal(self):
        """Test filename sanitization removes Windows path traversal."""
        filename = "..\\..\\..\\windows\\system32\\config\\sam"
        sanitized = self.service._sanitize_filename(filename)
        self.assertEqual(sanitized, "sam")

    def test_sanitize_filename_normal_filename(self):
        """Test normal filename is unchanged."""
        filename = "document.pdf"
        sanitized = self.service._sanitize_filename(filename)
        self.assertEqual(sanitized, "document.pdf")

    def test_sanitize_filename_none_returns_none(self):
        """Test None filename returns None."""
        sanitized = self.service._sanitize_filename(None)
        self.assertIsNone(sanitized)


class TestIMAPConnection(unittest.IsolatedAsyncioTestCase):
    """Test IMAP connection logic with exponential backoff."""

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
        settings.imap_host = "test.example.com"
        settings.imap_port = 993
        settings.imap_username = "test@example.com"
        settings.imap_password = SecretStr("password123")
        settings.data_dir = Path(self.temp_dir)
        settings.uploads_dir.mkdir(parents=True, exist_ok=True)
        return settings

    def _create_test_pool(self):
        db_path = os.path.join(self.temp_dir, 'test.db')
        from app.models.database import init_db
        init_db(db_path)
        return SQLiteConnectionPool(db_path, max_size=2)

    @patch('app.services.email_service.asyncio.wait_for', side_effect=asyncio.TimeoutError())
    async def test_connect_with_backoff_success_first_attempt(self, mock_wait_for):
        """Test successful connection on first attempt."""
        fake_imap = FakeIMAPClient()

        with patch('app.services.email_service.aioimaplib.IMAP4_SSL', return_value=fake_imap):
            result = await self.service._connect_with_backoff()
            self.assertEqual(result, fake_imap)
            self.assertIsNone(self.service.get_current_backoff_delay())

    @patch('app.services.email_service.asyncio.wait_for', side_effect=asyncio.TimeoutError())
    async def test_connect_with_backoff_retry_logic(self, mock_wait_for):
        """Test exponential backoff retry logic."""
        attempt_count = 0
        fake_imap = FakeIMAPClient()

        def mock_imap_connection(*args, **kwargs):
            nonlocal attempt_count
            attempt_count += 1
            if attempt_count < 3:
                raise OSError("Connection failed")
            return fake_imap

        with patch('app.services.email_service.aioimaplib.IMAP4_SSL', side_effect=mock_imap_connection):
            result = await self.service._connect_with_backoff()
            self.assertEqual(result, fake_imap)
            self.assertEqual(attempt_count, 3)
            # Backoff delay should be set during retries
            self.assertIsNone(self.service.get_current_backoff_delay())  # Cleared on success

    @patch('app.services.email_service.asyncio.wait_for', side_effect=asyncio.TimeoutError())
    async def test_connect_with_backoff_auth_failure_no_retry(self, mock_wait_for):
        """Test auth failure doesn't trigger retry."""
        fake_imap = FakeIMAPClient()
        # Make login fail
        fake_imap.login = AsyncMock(return_value=('NO', None))

        with patch('app.services.email_service.aioimaplib.IMAP4_SSL', return_value=fake_imap):
            with self.assertRaises(Exception) as ctx:
                await self.service._connect_with_backoff()
            self.assertIn("authentication", str(ctx.exception).lower())

    @patch('app.services.email_service.asyncio.wait_for', side_effect=asyncio.TimeoutError())
    async def test_connect_with_backoff_max_delay_reached(self, mock_wait_for):
        """Test backoff delay caps at max (60s) before giving up."""
        attempt_count = 0
        FakeIMAPClient()

        def mock_imap_connection(*args, **kwargs):
            nonlocal attempt_count
            attempt_count += 1
            raise OSError("Connection failed")

        with patch('app.services.email_service.aioimaplib.IMAP4_SSL', side_effect=mock_imap_connection):
            with self.assertRaises(Exception) as ctx:
                await self.service._connect_with_backoff()
            self.assertIn("IMAP connection failed after", str(ctx.exception))
            self.assertEqual(attempt_count, 4)  # 5s -> 15s -> 45s -> 60s (max)

    @patch('app.services.email_service.asyncio.wait_for', side_effect=asyncio.TimeoutError())
    async def test_connect_with_backoff_gives_up_after_max_retries(self, mock_wait_for):
        """Test gives up after max delay reached and still failing."""
        FakeIMAPClient()

        def mock_imap_connection(*args, **kwargs):
            raise OSError("Persistent connection failure")

        with patch('app.services.email_service.aioimaplib.IMAP4_SSL', side_effect=mock_imap_connection):
            with self.assertRaises(Exception) as ctx:
                await self.service._connect_with_backoff()
            self.assertIn("IMAP connection failed after", str(ctx.exception))

    @patch('app.services.email_service.asyncio.wait_for', side_effect=asyncio.TimeoutError())
    async def test_connect_with_backoff_stop_event_interrupts(self, mock_wait_for):
        """Test stop event interrupts backoff."""
        FakeIMAPClient()

        def mock_imap_connection(*args, **kwargs):
            raise Exception("Connection failed")

        with patch('app.services.email_service.aioimaplib.IMAP4_SSL', side_effect=mock_imap_connection):
            # Set stop event to interrupt
            self.service._stop_event.set()
            with self.assertRaises(Exception) as ctx:
                await self.service._connect_with_backoff()
            self.assertIn("stopped during connection", str(ctx.exception))


class TestEmailServiceIntegration(unittest.IsolatedAsyncioTestCase):
    """Test email service integration."""

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
        # Create test vaults needed for attachment tests
        conn = self.pool.get_connection()
        conn.execute("INSERT INTO vaults (name) VALUES (?)", ("Vault1",))
        conn.commit()
        self.pool.release_connection(conn)

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
        settings.imap_poll_interval = 10  # Short for tests
        settings.data_dir = Path(self.temp_dir)
        settings.uploads_dir.mkdir(parents=True, exist_ok=True)
        return settings

    def _create_test_pool(self):
        db_path = os.path.join(self.temp_dir, 'test.db')
        from app.models.database import init_db
        init_db(db_path)
        return SQLiteConnectionPool(db_path, max_size=2)

    async def test_service_initialization(self):
        """Test service initializes correctly."""
        self.assertIsNotNone(self.service)
        self.assertFalse(self.service._running)
        self.assertIsNone(self.service._polling_task)
        self.assertIsNone(self.service._last_error)
        self.assertIsNone(self.service._last_poll_time)

    async def test_start_polling_sets_running_flag(self):
        """Test start_polling sets running flag."""
        fake_imap = FakeIMAPClient()

        with patch('app.services.email_service.aioimaplib.IMAP4_SSL', return_value=fake_imap):
            await self.service.start_polling()
            self.assertTrue(self.service._running)
            self.assertIsNotNone(self.service._polling_task)

            # Stop the service
            await self.service.stop_polling()
            # Wait a bit for stop to propagate
            await asyncio.sleep(0.1)

    async def test_start_polling_when_already_running(self):
        """Test start_polling is idempotent when already running."""
        fake_imap = FakeIMAPClient()

        with patch('app.services.email_service.aioimaplib.IMAP4_SSL', return_value=fake_imap):
            await self.service.start_polling()
            first_task = self.service._polling_task

            # Start again (should not create duplicate poller)
            await self.service.start_polling()
            second_task = self.service._polling_task

            self.assertEqual(first_task, second_task)

            # Stop the service
            await self.service.stop_polling()
            await asyncio.sleep(0.1)

    async def test_start_polling_when_disabled(self):
        """Test start_polling does nothing when IMAP disabled."""
        self.settings.imap_enabled = False
        service = EmailIngestionService(
            self.settings,
            self.pool,
            self.background_processor
        )

        await service.start_polling()
        self.assertFalse(service._running)
        self.assertIsNone(service._polling_task)

    async def test_stop_polling_when_not_running(self):
        """Test stop_polling handles not running gracefully."""
        # Should not raise exception
        await self.service.stop_polling()
        self.assertFalse(self.service._running)

    async def test_stop_polling_sets_stop_event(self):
        """Test stop_polling sets stop event."""
        fake_imap = FakeIMAPClient()

        with patch('app.services.email_service.aioimaplib.IMAP4_SSL', return_value=fake_imap):
            await self.service.start_polling()
            self.assertFalse(self.service._stop_event.is_set())

            await self.service.stop_polling()
            self.assertTrue(self.service._stop_event.is_set())

            # Wait a bit for stop to propagate
            await asyncio.sleep(0.1)

    async def test_is_healthy_true_when_running_no_errors(self):
        """Test is_healthy returns True when running without errors."""
        self.service._running = True
        self.service._last_error = None
        self.assertTrue(self.service.is_healthy())

    async def test_is_healthy_false_when_not_running(self):
        """Test is_healthy returns False when not running."""
        self.service._running = False
        self.service._last_error = None
        self.assertFalse(self.service.is_healthy())

    async def test_is_healthy_false_when_has_error(self):
        """Test is_healthy returns False when has error."""
        self.service._running = True
        self.service._last_error = "Connection failed"
        self.assertFalse(self.service.is_healthy())

    async def test_get_last_poll_time(self):
        """Test get_last_poll_time returns correct value."""
        self.assertIsNone(self.service.get_last_poll_time())

        test_time = datetime(2026, 2, 19, 12, 0, 0)
        self.service._last_poll_time = test_time
        self.assertEqual(self.service.get_last_poll_time(), test_time)

    async def test_get_current_backoff_delay(self):
        """Test get_current_backoff_delay returns correct value."""
        self.assertIsNone(self.service.get_current_backoff_delay())

        self.service._current_backoff_delay = 30
        self.assertEqual(self.service.get_current_backoff_delay(), 30)

    async def test_process_email_with_attachment(self):
        """Test processing email with attachment."""
        fake_imap = FakeIMAPClient()

        # Create test email with attachment
        msg = EmailMessage()
        msg['Subject'] = 'Test Document [Vault1]'
        msg['From'] = 'sender@example.com'
        msg.set_content('Email body')
        msg.add_attachment(
            b'PDF content',
            maintype='application',
            subtype='pdf',
            filename='test.pdf'
        )

        fake_imap.emails = {'1': {'content': msg.as_bytes()}}

        with patch('app.services.email_service.aioimaplib.IMAP4_SSL', return_value=fake_imap):
            await self.service._process_email(fake_imap, '1')

            # Check attachment was enqueued
            self.assertEqual(len(self.background_processor.enqueued), 1)
            self.assertEqual(self.background_processor.enqueued[0]['source'], 'email')

    async def test_process_email_with_disallowed_mime_type(self):
        """Test processing email with disallowed MIME type."""
        fake_imap = FakeIMAPClient()

        # Create test email with disallowed attachment
        msg = EmailMessage()
        msg['Subject'] = 'Test Document [Vault1]'
        msg['From'] = 'sender@example.com'
        msg.set_content('Email body')
        msg.add_attachment(
            b'EXE content',
            maintype='application',
            subtype='x-msdownload',
            filename='test.exe'
        )

        fake_imap.emails = {'1': {'content': msg.as_bytes()}}

        with patch('app.services.email_service.aioimaplib.IMAP4_SSL', return_value=fake_imap):
            await self.service._process_email(fake_imap, '1')

            # Check nothing was enqueued
            self.assertEqual(len(self.background_processor.enqueued), 0)

    async def test_process_email_with_oversized_attachment(self):
        """Test processing email with oversized attachment."""
        fake_imap = FakeIMAPClient()

        # Create test email with oversized attachment
        large_content = b'x' * (11 * 1024 * 1024)  # 11MB
        msg = EmailMessage()
        msg['Subject'] = 'Test Document [Vault1]'
        msg['From'] = 'sender@example.com'
        msg.set_content('Email body')
        msg.add_attachment(
            large_content,
            maintype='application',
            subtype='pdf',
            filename='large.pdf'
        )

        fake_imap.emails = {'1': {'content': msg.as_bytes()}}

        with patch('app.services.email_service.aioimaplib.IMAP4_SSL', return_value=fake_imap):
            await self.service._process_email(fake_imap, '1')

            # Check nothing was enqueued
            self.assertEqual(len(self.background_processor.enqueued), 0)

    async def test_oversized_email_marked_seen(self):
        """Regression for issue #280 A9-5: oversized email must be marked
        \\Seen so it is excluded from subsequent UNSEEN searches.

        Before the fix, _process_email returned early after the RFC822.SIZE
        fetch without setting \\Seen. Since the SIZE fetch does not set the
        flag (RFC 3501), the poller re-selected and re-skipped the same
        oversized email on every cycle indefinitely.
        """
        from app.services.email_service import MAX_EMAIL_SIZE

        fake_imap = FakeIMAPClient()

        # Build an email whose raw bytes exceed MAX_EMAIL_SIZE
        large_content = b'x' * (MAX_EMAIL_SIZE + 1024)
        msg = EmailMessage()
        msg['Subject'] = 'Big Doc [Vault1]'
        msg['From'] = 'sender@example.com'
        msg.set_content('body')
        msg.add_attachment(
            large_content,
            maintype='application',
            subtype='pdf',
            filename='huge.pdf',
        )

        fake_imap.emails = {'1': {'content': msg.as_bytes()}}

        with patch('app.services.email_service.aioimaplib.IMAP4_SSL', return_value=fake_imap):
            await self.service._process_email(fake_imap, '1')

            # Nothing should have been enqueued
            self.assertEqual(len(self.background_processor.enqueued), 0)

            # The oversized email MUST have been marked \\Seen
            self.assertIn('1', fake_imap.stored_flags,
                          "Oversized email was not marked \\Seen — "
                          "it will be re-polled indefinitely")
            store_args = fake_imap.stored_flags['1'][0]
            self.assertIn('\\Seen', store_args)

    async def test_stop_polling_awaits_task_completion(self):
        """Regression for issue #280 E2-1: stop_polling must await the
        polling task (or cancel it on timeout) before returning.

        Before the fix, stop_polling was synchronous and only set the stop
        event. The polling task could still be mid-poll when the caller
        proceeded to tear down shared resources (pool, background_processor).
        """
        fake_imap = FakeIMAPClient()

        with patch('app.services.email_service.aioimaplib.IMAP4_SSL', return_value=fake_imap):
            await self.service.start_polling()
            self.assertTrue(self.service._running)
            task = self.service._polling_task
            self.assertIsNotNone(task)

            await self.service.stop_polling()

            # After stop_polling returns, the polling task MUST be done
            self.assertTrue(task.done(),
                            "stop_polling returned before the polling task finished")
            self.assertFalse(self.service._running)

    async def test_oversized_email_store_failure_is_handled(self):
        """Regression for issue #280 follow-up: if marking an oversized
        email as \\Seen fails after retries, _process_email must raise
        OSError so _poll_once can log the failure and continue without
        getting stuck on the same message.
        """
        from app.services.email_service import MAX_EMAIL_SIZE

        fake_imap = FakeIMAPClient()
        call_count = 0

        async def failing_store(uid, *args):
            nonlocal call_count
            call_count += 1
            raise OSError("IMAP store failed")

        fake_imap.store = failing_store

        large_content = b'x' * (MAX_EMAIL_SIZE + 1024)
        msg = EmailMessage()
        msg['Subject'] = 'Big Doc [Vault1]'
        msg['From'] = 'sender@example.com'
        msg.set_content('body')
        msg.add_attachment(
            large_content,
            maintype='application',
            subtype='pdf',
            filename='huge.pdf',
        )

        fake_imap.emails = {'1': {'content': msg.as_bytes()}}

        with patch('app.services.email_service.aioimaplib.IMAP4_SSL', return_value=fake_imap):
            with self.assertRaises(OSError) as ctx:
                await self.service._process_email(fake_imap, '1')

            self.assertIn("Failed to mark oversized email UID 1 as Seen", str(ctx.exception))

            # store() should have been attempted 3 times
            self.assertEqual(call_count, 3)

            # Nothing should have been enqueued
            self.assertEqual(len(self.background_processor.enqueued), 0)

    async def test_stop_polling_timeout_cancels_task(self):
        """Regression for issue #280 E2-1 follow-up: if the polling task
        does not stop within the 5s timeout, stop_polling must cancel it
        and await its completion.
        """
        fake_imap = FakeIMAPClient()

        async def hang_forever():
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                raise

        with patch('app.services.email_service.aioimaplib.IMAP4_SSL', return_value=fake_imap):
            await self.service.start_polling()
            self.assertTrue(self.service._running)
            task = self.service._polling_task
            self.assertIsNotNone(task)

            # Replace the polling task with one that hangs forever
            task.cancel()
            await asyncio.sleep(0)
            self.service._polling_task = asyncio.create_task(hang_forever())
            self.service._polling_task.add_done_callback(self.service._on_polling_task_done)

            await self.service.stop_polling()

            # After stop_polling returns, the polling task MUST be done
            self.assertTrue(self.service._polling_task.done(),
                            "stop_polling did not cancel/complete the polling task")
            self.assertFalse(self.service._running)

    async def test_process_email_multiple_attachments(self):
        """Test processing email with multiple attachments."""
        fake_imap = FakeIMAPClient()

        # Create test email with multiple attachments
        msg = EmailMessage()
        msg['Subject'] = 'Test Document [Vault1]'
        msg['From'] = 'sender@example.com'
        msg.set_content('Email body')
        msg.add_attachment(b'PDF content 1', maintype='application', subtype='pdf', filename='test1.pdf')
        msg.add_attachment(b'PDF content 2', maintype='application', subtype='pdf', filename='test2.pdf')
        msg.add_attachment(b'PDF content 3', maintype='application', subtype='pdf', filename='test3.pdf')

        fake_imap.emails = {'1': {'content': msg.as_bytes()}}

        with patch('app.services.email_service.aioimaplib.IMAP4_SSL', return_value=fake_imap):
            await self.service._process_email(fake_imap, '1')

            # Check all 3 attachments were enqueued
            self.assertEqual(len(self.background_processor.enqueued), 3)

    async def test_resolve_vault_id_found(self):
        """Test resolving vault ID when vault exists."""
        # Insert test vault (will be id=2 since default vault is id=1)
        conn = self.pool.get_connection()
        conn.execute("INSERT INTO vaults (name) VALUES (?)", ("TestVault",))
        conn.commit()
        vault_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        self.pool.release_connection(conn)

        resolved_id = await self.service._resolve_vault_id("TestVault")
        self.assertEqual(resolved_id, vault_id)

    async def test_resolve_vault_id_not_found(self):
        with self.assertRaises(ValueError) as ctx:
            await self.service._resolve_vault_id("NonExistentVault")
        self.assertIn("NonExistentVault", str(ctx.exception))

    async def test_resolve_vault_id_none(self):
        with self.assertRaises(ValueError) as ctx:
            await self.service._resolve_vault_id(None)
        self.assertIn("no vault tag", str(ctx.exception))

    async def test_resolve_vault_id_case_insensitive(self):
        """Test vault resolution is case-insensitive."""
        # Insert test vault (will be id=3 since default is id=1 and previous test added id=2)
        conn = self.pool.get_connection()
        conn.execute("INSERT INTO vaults (name) VALUES (?)", ("MyVault",))
        conn.commit()
        vault_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        self.pool.release_connection(conn)

        resolved_id = await self.service._resolve_vault_id("myvault")
        self.assertEqual(resolved_id, vault_id)

        resolved_id = await self.service._resolve_vault_id("MYVAULT")
        self.assertEqual(resolved_id, vault_id)


class TestSaveAttachmentSentinel(unittest.TestCase):
    """Test _save_attachment sentinel pattern prevents double-close.

    Verifies:
    1. On successful save, fd is closed exactly once
    2. On error (OSError during write), fd is closed exactly once
    3. Sentinel pattern prevents double-close on multiple exceptions
    4. Temp file is unlinked on error
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
        settings.data_dir = Path(self.temp_dir)
        settings.uploads_dir.mkdir(parents=True, exist_ok=True)
        return settings

    def _create_test_pool(self):
        db_path = os.path.join(self.temp_dir, 'test.db')
        from app.models.database import init_db
        init_db(db_path)
        return SQLiteConnectionPool(db_path, max_size=2)

    def _create_attachment_part(self, payload):
        """Helper to create an email attachment part."""
        msg = EmailMessage()
        msg.add_attachment(
            payload,
            filename='test.txt',
            main_type='text',
            sub_type='plain'
        )
        return msg.iter_attachments().__next__()

    @pytest.mark.asyncio
    async def test_save_attachment_success_closes_fd_once(self):
        """On successful save, fd is closed exactly once (no leak, no double-close)."""
        from unittest.mock import AsyncMock, patch

        part = self._create_attachment_part(b'test content')
        vault_id = 1

        close_calls = []
        original_close = os.close

        def track_close(fd):
            close_calls.append(fd)
            original_close(fd)

        with patch('os.close', side_effect=track_close):
            result = await self.service._save_attachment(part, vault_id)

        # Verify success path returned correct result
        self.assertIsInstance(result, str)
        self.assertTrue(result.endswith('.txt'))

        # Verify fd was closed exactly once
        self.assertEqual(len(close_calls), 1, f"Expected 1 close call, got {len(close_calls)}")

    @pytest.mark.asyncio
    async def test_save_attachment_error_closes_fd_once(self):
        """On OSError during write, fd is closed exactly once via sentinel."""
        from unittest.mock import patch

        part = self._create_attachment_part(b'test content')
        vault_id = 1

        close_calls = []
        original_close = os.close

        def track_close(fd):
            close_calls.append(fd)
            original_close(fd)

        # Simulate OSError during write
        with patch('os.close', side_effect=track_close):
            with patch('os.write', side_effect=OSError("Simulated write failure")):
                with self.assertRaises(Exception) as ctx:
                    await self.service._save_attachment(part, vault_id)

                self.assertIn("Failed to save attachment", str(ctx.exception))

        # Verify fd was closed exactly once (sentinel prevents double-close)
        self.assertEqual(len(close_calls), 1, f"Expected 1 close call, got {len(close_calls)}")

    @pytest.mark.asyncio
    async def test_save_attachment_error_unlinks_temp_file(self):
        """On error, temp file is unlinked."""
        from unittest.mock import patch

        part = self._create_attachment_part(b'test content')
        vault_id = 1

        unlink_calls = []
        original_unlink = os.unlink

        def track_unlink(path):
            unlink_calls.append(path)
            original_unlink(path)

        with patch('os.unlink', side_effect=track_unlink):
            with patch('os.write', side_effect=OSError("Simulated write failure")):
                try:
                    await self.service._save_attachment(part, vault_id)
                except Exception:
                    pass

        # Verify unlink was called to clean up temp file
        self.assertEqual(len(unlink_calls), 1, f"Expected 1 unlink call, got {len(unlink_calls)}")

    @pytest.mark.asyncio
    async def test_save_attachment_sentinel_prevents_double_close(self):
        """Sentinel pattern (fd=None) prevents double-close even when finally runs after except."""
        from unittest.mock import patch

        part = self._create_attachment_part(b'test content')
        vault_id = 1

        close_calls = []
        _original_close = os.close  # noqa: F841 - reference kept for restore

        def track_close(fd):
            close_calls.append(fd)

        # Patch close to track calls but not actually close (so finally sees fd!=None)
        # We want to verify the sentinel actually prevents the second call
        with patch('os.close', side_effect=track_close):
            # Simulate error after partial write - this triggers except then finally
            with patch('os.write', side_effect=OSError("Simulated failure")):
                try:
                    await self.service._save_attachment(part, vault_id)
                except Exception:
                    pass

        # Sentinel pattern: except sets fd=None, so finally's if fd is not None check
        # should prevent second close. Without sentinel, we'd see 2 close calls.
        self.assertEqual(len(close_calls), 1,
            f"Sentinel failed: expected 1 close (from except), got {len(close_calls)}. "
            "Double-close occurred if count > 1.")


if __name__ == '__main__':
    unittest.main()
