"""
Issue #494 acceptance checks — AC18 (EMAIL-001) and AC19 (EMAIL-002): the
email ingestion service compares aioimaplib results against contracts the
installed library does not have.

Root causes (verified at base a543361, aioimaplib 2.0.1):
- AC18: email_service.py ``_connect_with_backoff`` compares the result of
  ``wait_hello_from_server()`` to the string 'OK', but the installed
  aioimaplib 2.0.1 coroutine is ``async def ... -> None`` and returns None
  on success — so every successful greeting is treated as a failure and the
  service raises "IMAP server greeting failed: None".
- AC19: ``_process_email`` unpacks ``result, data = await fetch(...)`` and
  reads ``data[0][1]`` as the message bytes, but aioimaplib's fetch returns
  ``Response(result, lines)`` where ``lines`` is a flat ``List[bytes]``
  (metadata line, then the literal RFC822 bytes, then a closing line).
  ``data[0][1]`` therefore indexes the SECOND BYTE of the metadata line (an
  int), not the message, and indexing/parsing fails.

Both checks drive the REAL production code against fakes that mirror the
installed library's contracts exactly (verified inside the tests by
inspecting the installed aioimaplib).
"""

import inspect
import os
import sys
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# Stub missing optional dependencies (mirrors the other backend test files)
try:
    import lancedb  # noqa: F401
except ImportError:
    import types
    sys.modules['lancedb'] = types.ModuleType('lancedb')

try:
    import pyarrow  # noqa: F401
except ImportError:
    import types
    sys.modules['pyarrow'] = types.ModuleType('pyarrow')

try:
    from unstructured.partition.auto import partition  # noqa: F401
except ImportError:
    import types
    _unstructured = types.ModuleType('unstructured')
    _unstructured.__path__ = []
    _unstructured.partition = types.ModuleType('unstructured.partition')
    _unstructured.partition.__path__ = []
    _unstructured.partition.auto = types.ModuleType('unstructured.partition.auto')
    _unstructured.partition.auto.partition = lambda *args, **kwargs: []
    sys.modules['unstructured'] = _unstructured
    sys.modules['unstructured.partition'] = _unstructured.partition
    sys.modules['unstructured.partition.auto'] = _unstructured.partition.auto

import aioimaplib  # noqa: E402
from pydantic import SecretStr  # noqa: E402

from app.config import Settings  # noqa: E402
from app.models.database import SQLiteConnectionPool  # noqa: E402
from app.services.email_service import EmailIngestionService  # noqa: E402


class RecordingBackgroundProcessor:
    """Spy standing in for BackgroundProcessor.enqueue."""

    def __init__(self):
        self.enqueued = []

    async def enqueue(self, file_path, source=None, email_subject=None,
                      email_sender=None, vault_id=None):
        self.enqueued.append({
            "file_path": file_path,
            "source": source,
            "email_subject": email_subject,
            "email_sender": email_sender,
            "vault_id": vault_id,
        })


class RealContractIMAPClient:
    """Fake IMAP client that mirrors the installed aioimaplib 2.0.1:

    - ``wait_hello_from_server()`` returns None on success (no greeting
      result object);
    - ``login``/``select``/``logout`` return ``aioimaplib.Response(result,
      lines)`` namedtuples;
    - ``fetch`` returns a real-shaped ``Response`` whose ``lines`` is a flat
      ``List[bytes]``: metadata line(s), the literal payload bytes, closing
      line.
    """

    def __init__(self, raw_email: bytes = b""):
        self.raw_email = raw_email
        self.login_called = False
        self.selected_mailbox = None
        self.logged_out = False
        self.search_calls = []
        self.fetch_calls = []

    async def wait_hello_from_server(self):
        return None  # the installed library's success contract

    async def login(self, username, password):
        self.login_called = True
        return aioimaplib.Response("OK", [b"LOGIN completed"])

    async def select(self, mailbox):
        self.selected_mailbox = mailbox
        return aioimaplib.Response("OK", [b"1 EXISTS"])

    async def search(self, *args, **kwargs):
        self.search_calls.append((args, kwargs))
        return aioimaplib.Response("OK", [b""])

    async def fetch(self, uid, message_parts):
        self.fetch_calls.append((uid, message_parts))
        if message_parts == "(RFC822.SIZE)":
            return aioimaplib.Response(
                "OK", [f"{uid} (UID {uid} RFC822.SIZE {len(self.raw_email)})".encode()]
            )
        if message_parts == "(RFC822)":
            # Real protocol shape for a full-message fetch: untagged FETCH
            # response line with a {N} literal announcement, the N literal
            # message bytes, then the closing parenthesis.
            opening = f"* {uid} FETCH (UID {uid} RFC822 {{{len(self.raw_email)}}}"
            return aioimaplib.Response(
                "OK", [opening.encode(), self.raw_email, b")"]
            )
        return aioimaplib.Response("OK", [])

    async def store(self, uid, *args):
        return aioimaplib.Response("OK", [b"STORE completed"])

    async def logout(self):
        self.logged_out = True
        return aioimaplib.Response("OK", [b"BYE"])


class _EmailHarness(unittest.IsolatedAsyncioTestCase):
    """Shared setUp: real service + temp DB with a resolvable vault."""

    def _make_service(self):
        self.temp_dir = tempfile.mkdtemp()
        self.settings = Settings()
        self.settings.imap_enabled = True
        self.settings.imap_host = "test.example.com"
        self.settings.imap_port = 993
        self.settings.imap_username = "test@example.com"
        self.settings.imap_password = SecretStr("password123")
        self.settings.imap_mailbox = "INBOX"
        self.settings.data_dir = Path(self.temp_dir)
        self.settings.uploads_dir.mkdir(parents=True, exist_ok=True)

        db_path = os.path.join(self.temp_dir, "test.db")
        from app.models.database import init_db
        init_db(db_path)
        self.pool = SQLiteConnectionPool(db_path, max_size=2)
        conn = self.pool.get_connection()
        try:
            conn.execute("INSERT INTO vaults (name) VALUES (?)", ("Vault1",))
            conn.commit()
        finally:
            self.pool.release_connection(conn)

        self.background_processor = RecordingBackgroundProcessor()
        self.service = EmailIngestionService(
            self.settings, self.pool, self.background_processor
        )

    def _teardown_service(self):
        self.pool.close_all()
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)


class TestGreetingContract(_EmailHarness):
    """AC18 — DISCRIMINATING: a None greeting (real contract) must connect."""

    async def test_ac18_connect_completes_when_greeting_returns_none(self):
        """wait_hello_from_server() -> None is SUCCESS in aioimaplib 2.0.1.

        At the pre-fix base _connect_with_backoff compares the greeting to
        'OK' and raises "IMAP server greeting failed: None" before login.
        """
        self._make_service()
        try:
            # Pin the installed library's contract first: the coroutine has
            # no return statement, i.e. it returns None on success.
            hello = aioimaplib.IMAP4.wait_hello_from_server
            self.assertTrue(
                inspect.iscoroutinefunction(hello),
                "installed aioimaplib wait_hello_from_server is no longer a "
                "coroutine — update this check",
            )
            hello_src = inspect.getsource(hello)
            self.assertNotRegex(
                hello_src,
                r"\breturn\b",
                "installed aioimaplib wait_hello_from_server now returns a "
                "value — update the fake (and the production fix) to the new "
                "contract",
            )

            fake = RealContractIMAPClient()
            with patch(
                "app.services.email_service.aioimaplib.IMAP4_SSL",
                return_value=fake,
            ):
                print("AC18 CHECK: FAIL")
                client = await self.service._connect_with_backoff()
                self.assertIs(
                    client,
                    fake,
                    "_connect_with_backoff must establish the connection "
                    "when the greeting returns None (aioimaplib 2.0.1 "
                    "success contract)",
                )
            self.assertTrue(
                fake.login_called,
                "connection must proceed to login after a None greeting",
            )
        finally:
            self._teardown_service()


class TestFetchResponseShape(_EmailHarness):
    """AC19 — DISCRIMINATING: real-shaped fetch lines must be parsed."""

    async def test_ac19_process_email_parses_real_shaped_fetch_lines(self):
        """A Response('OK', [metadata, literal, closing]) fetch must be parsed.

        The literal RFC822 bytes of a message with one attachment must be
        extracted, saved, and enqueued with the exact attachment bytes.
        At the pre-fix base ``data[0][1]`` indexes a byte out of the metadata
        line and _process_email raises before enqueueing anything.
        """
        self._make_service()
        try:
            msg = EmailMessage()
            msg["Subject"] = "Test Document [Vault1]"
            msg["From"] = "sender@example.com"
            msg.set_content("Email body")
            attachment_bytes = b"%PDF-1.4 issue-494 fixture payload \x00\x01\x02"
            msg.add_attachment(
                attachment_bytes,
                maintype="application",
                subtype="pdf",
                filename="issue494-report.pdf",
            )
            raw = msg.as_bytes()

            fake = RealContractIMAPClient(raw_email=raw)
            with patch(
                "app.services.email_service.aioimaplib.IMAP4_SSL",
                return_value=fake,
            ):
                print("AC19 CHECK: FAIL")
                await self.service._process_email(fake, "7")

            self.assertEqual(
                len(self.background_processor.enqueued),
                1,
                "the attachment from a real-shaped fetch response must be "
                "enqueued exactly once (enqueued: "
                f"{self.background_processor.enqueued!r})",
            )
            enqueued = self.background_processor.enqueued[0]
            self.assertEqual(enqueued["source"], "email")
            stored_path = Path(enqueued["file_path"])
            self.assertTrue(
                stored_path.name.startswith("email_"),
                f"stored name must keep the service's email_ prefix, got "
                f"{stored_path.name!r}",
            )
            self.assertTrue(
                stored_path.suffix == ".pdf",
                f"stored name must keep the attachment extension, got "
                f"{stored_path.suffix!r}",
            )
            stored_bytes = stored_path.read_bytes()
            self.assertEqual(
                stored_bytes,
                attachment_bytes,
                "the stored attachment bytes must be the exact RFC822 "
                "literal payload",
            )
        finally:
            self._teardown_service()


if __name__ == "__main__":
    unittest.main()
