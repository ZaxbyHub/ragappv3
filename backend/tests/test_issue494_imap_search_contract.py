"""
Issue #494 acceptance checks — AC20 (EMAIL-003) and AC21 (EMAIL-004): the
IMAP SEARCH call is issued with a positional None and the unseen-count
helper compares aioimaplib result objects to strings.

Root causes (verified at base a543361, aioimaplib 2.0.1):
- AC20: email_service.py ``_poll_once`` and routes/email.py
  ``_get_unseen_count`` both call ``imap_client.search(None, 'UNSEEN')``.
  aioimaplib 2.0.1's signature is
  ``search(self, *criteria, charset: Optional[str] = 'utf-8')`` and its
  protocol serializes ``('CHARSET', charset) + criteria`` when charset is
  not None — so the production call transmits the wire line
  ``<tag> SEARCH CHARSET utf-8  UNSEEN`` (the None criterion is str-joined
  as an EMPTY criterion, producing a double space) instead of the intended
  ``<tag> SEARCH UNSEEN`` (charset=None + criteria UNSEEN only).
- AC21: routes/email.py ``_get_unseen_count`` compares the select() result
  (an ``aioimaplib.Response`` namedtuple) directly to the string 'OK'
  (``result = await imap_client.select(...)`` with no unpacking), so the
  count is always -1 even when select succeeds. The greeting check shares
  AC18's bug (wait_hello_from_server() returns None on success).

Checks:
- AC20 (one node, both call sites + wire bytes): the production call must
  invoke the client with criteria ('UNSEEN',) and charset=None, at BOTH
  ``email_service._poll_once`` and ``routes._get_unseen_count``; the
  recorded args are then serialized through the REAL aioimaplib protocol
  (``IMAP4ClientProtocol.search`` with a capture stand-in for execute) and
  the exact wire line must be ``<tag> SEARCH UNSEEN``.
- AC21: with real-shaped ``Response('OK', ...)`` results for
  greeting/login/select and ``Response('OK', [b'1 2 3'])`` for search,
  ``_get_unseen_count`` must return 3.
"""

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

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


class RecordingIMAPClient:
    """Fake IMAP client mirroring installed aioimaplib 2.0.1 contracts.

    Greeting returns None on success; login/select/logout/search return real
    ``aioimaplib.Response`` namedtuples; search records its invocation.
    """

    def __init__(self, search_lines=(b"",)):
        self.search_lines = list(search_lines)
        self.search_calls = []
        self.login_called = False
        self.selected_mailbox = None
        self.logged_out = False

    async def wait_hello_from_server(self):
        return None  # installed aioimaplib 2.0.1 success contract

    async def login(self, username, password):
        self.login_called = True
        return aioimaplib.Response("OK", [b"LOGIN completed"])

    async def select(self, mailbox):
        self.selected_mailbox = mailbox
        return aioimaplib.Response("OK", [b"1 EXISTS"])

    async def search(self, *args, **kwargs):
        self.search_calls.append((args, kwargs))
        return aioimaplib.Response("OK", self.search_lines)

    async def logout(self):
        self.logged_out = True
        return aioimaplib.Response("OK", [b"BYE"])


class _EmailHarness(unittest.IsolatedAsyncioTestCase):
    """Shared setUp: real service + temp DB."""

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
        self.service = EmailIngestionService(self.settings, self.pool, AsyncMock())

    def _teardown_service(self):
        self.pool.close_all()
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)


class TestSearchCommandContract(_EmailHarness):
    """AC20 — DISCRIMINATING: SEARCH must be criteria-only with charset=None."""

    async def test_ac20_search_invoked_as_criteria_only_charset_none(self):
        """Both production call sites must issue SEARCH ('UNSEEN', charset=None),
        and the real aioimaplib protocol must serialize that as exactly
        ``<tag> SEARCH UNSEEN``."""
        self._make_service()
        try:
            # ── Call site 1: EmailIngestionService._poll_once ─────────────
            service_fake = RecordingIMAPClient()
            with patch.object(
                self.service, "_connect_with_backoff",
                AsyncMock(return_value=service_fake),
            ):
                await self.service._poll_once()
            service_call = (
                service_fake.search_calls[0]
                if service_fake.search_calls else None
            )

            # ── Call site 2: routes/email.py _get_unseen_count ────────────
            from app.api.routes.email import _get_unseen_count

            routes_fake = RecordingIMAPClient()
            with patch.object(aioimaplib, "IMAP4_SSL",
                              return_value=routes_fake), \
                 patch.object(aioimaplib, "IMAP4",
                              return_value=routes_fake):
                await _get_unseen_count(self.service)
            routes_call = (
                routes_fake.search_calls[0]
                if routes_fake.search_calls else None
            )

            print("AC20 CHECK: FAIL")
            for label, call in (
                ("email_service._poll_once", service_call),
                ("routes.email._get_unseen_count", routes_call),
            ):
                self.assertIsNotNone(
                    call,
                    f"{label} never issued a SEARCH command — check the "
                    f"greeting/select handling on the same path",
                )
                args, kwargs = call
                self.assertEqual(
                    args,
                    ("UNSEEN",),
                    f"{label} must issue SEARCH with criteria ('UNSEEN',) "
                    f"only — a positional None becomes an empty criterion "
                    f"on the wire. Recorded args: {args!r}",
                )
                self.assertIs(
                    kwargs.get("charset", "MISSING"),
                    None,
                    f"{label} must pass charset=None (the library default "
                    f"'utf-8' adds 'CHARSET utf-8' to the wire command). "
                    f"Recorded kwargs: {kwargs!r}",
                )

            # ── Wire serialization via the REAL aioimaplib protocol ──────
            await self._assert_wire_line_is_criteria_only(service_call)
        finally:
            self._teardown_service()

    async def _assert_wire_line_is_criteria_only(self, call):
        """Serialize the recorded production args with the real protocol.

        ``IMAP4ClientProtocol.search`` builds the Command exactly as the
        library does and ``execute`` sends ``str(command)``; a capture
        stand-in for execute records the Command without a server. This is
        the exact wire line the production call produces.
        """
        args, kwargs = call
        charset = kwargs.get("charset", "utf-8")  # library default
        loop = asyncio.get_running_loop()
        protocol = aioimaplib.IMAP4ClientProtocol(loop)
        captured = []

        async def capture_execute(command, scrub=None):
            captured.append(command)
            return aioimaplib.Response("OK", [b""])

        protocol.execute = capture_execute
        await protocol.search(*args, charset=charset)

        command = captured[0]
        wire_line = str(command)
        self.assertEqual(
            wire_line,
            f"{command.tag} SEARCH UNSEEN",
            f"the real aioimaplib serialization of the production SEARCH "
            f"call must be '<tag> SEARCH UNSEEN'; got {wire_line!r} "
            f"(a positional None criterion or a non-None charset corrupts "
            f"the command)",
        )


class TestUnseenCountWithRealResponses(_EmailHarness):
    """AC21 — DISCRIMINATING: unseen count with real-shaped responses."""

    async def test_ac21_unseen_count_returns_uid_count(self):
        """Response('OK', [b'1 2 3']) for search must yield an unseen count
        of 3.

        At the pre-fix base _get_unseen_count returns -1 (greeting None is
        compared to 'OK'; the select() Response is compared to 'OK' without
        unpacking).
        """
        self._make_service()
        try:
            from app.api.routes.email import _get_unseen_count

            fake = RecordingIMAPClient(search_lines=[b"1 2 3"])
            with patch.object(aioimaplib, "IMAP4_SSL", return_value=fake), \
                 patch.object(aioimaplib, "IMAP4", return_value=fake):
                count = await _get_unseen_count(self.service)

            print("AC21 CHECK: FAIL")
            self.assertEqual(
                count,
                3,
                f"_get_unseen_count must count the UIDs (3) from a real-shaped "
                f"aioimaplib Response; got {count!r} (Response objects are "
                f"compared to the string 'OK' instead of being unpacked / "
                f"checked via .result)",
            )
            self.assertTrue(fake.logged_out, "client must be logged out")
        finally:
            self._teardown_service()


if __name__ == "__main__":
    unittest.main()
