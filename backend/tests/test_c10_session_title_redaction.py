"""Acceptance checks for issue #909 (C15): the auto-named session title leak.

``_auto_name_session`` in ``backend/app/api/routes/chat.py`` logs the
auto-generated session title at INFO. The title is an LLM summary of the
session's FIRST USER MESSAGE, so user-derived text reaches the default
application log on every newly created UI session where auto-naming
succeeds. Same defect class as #710 (#287 contract), different subsystem.

- AC1 (C7): an auto-named title carrying a distinct marker must never appear
  in an INFO-or-higher record of ``app.api.routes.chat``. RED at base, GREEN
  after the fix.
- AC2 (C8): the session id and the auto-naming fact must still be logged.
  GREEN at base, GREEN after the fix.

The title VALUE persisted to the database is out of scope (logging-only fix)
and is pinned by the existing suite ``tests/test_auto_name_session.py``.

Pool/LLM scaffolding mirrors ``tests/test_auto_name_session.py``; the marker
rides the LLM-returned title, the only user-derived value on this path.
"""

import logging
import os
import sys
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Stub missing optional dependencies (same block as test_auto_name_session.py)
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

from app.api.routes.chat import _auto_name_session

CHAT_LOGGER = "app.api.routes.chat"
TITLE_MARKER = "C10T_MARKER"
SESSION_ID = 42


class _ListHandler(logging.Handler):
    """Collect every record at or above its level; no assertion semantics."""

    def __init__(self, level: int):
        super().__init__(level=level)
        self.records = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@contextmanager
def capture_records(logger_name: str, level: int = logging.INFO):
    """Capture records of ``logger_name`` at ``level``+ without failing when
    the logger stays silent (unlike ``assertLogs``)."""
    lg = logging.getLogger(logger_name)
    handler = _ListHandler(level)
    previous_level = lg.level
    lg.setLevel(level)
    lg.addHandler(handler)
    try:
        yield handler
    finally:
        lg.removeHandler(handler)
        lg.setLevel(previous_level)


class _AsyncConnectionCM:
    """Async CM standing in for pool.connection_async(), yielding the
    recording mock connection (same shape as test_auto_name_session.py)."""

    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, exc_type, exc, tb):
        return None


class _ChatEnv:
    """Patched chat route: recording connection, untitled session row."""

    def __init__(self):
        self.execute_history = []
        self.conn = MagicMock()
        self.conn.rowcount = 1
        self.conn.fetchone.return_value = None  # NULL title -> untitled branch
        self.conn.commit.return_value = None

        def mock_execute(query, params=None):
            cursor = MagicMock()
            if "UPDATE" in query.upper():
                cursor.rowcount = 1  # rows affected -> commit -> INFO log fires
            else:
                cursor.rowcount = self.conn.rowcount
            if "SELECT" in query.upper():
                cursor.fetchone.return_value = self.conn.fetchone.return_value
            else:
                cursor.fetchone.return_value = None
            self.execute_history.append({"query": query, "params": params})
            return cursor

        self.conn.execute = mock_execute
        pool = MagicMock()
        pool.connection_async = MagicMock(return_value=_AsyncConnectionCM(self.conn))

        self._patches = [
            patch("app.api.routes.chat.settings"),
            patch("app.api.routes.chat.get_pool", return_value=pool),
        ]
        mock_settings = self._patches[0].start()
        mock_settings.sqlite_path = "/test/sqlite.db"
        self._patches[1].start()

        self.llm = AsyncMock()

    def stop(self):
        for patcher in reversed(self._patches):
            patcher.stop()


@pytest.fixture
def chat_env():
    env = _ChatEnv()
    yield env
    env.stop()


async def _auto_name_with_marker_title(env: _ChatEnv) -> None:
    """Drive the untitled-session branch with a marker-bearing LLM title."""
    env.llm.chat_completion = AsyncMock(
        return_value=f"{TITLE_MARKER} summary of first message"
    )
    await _auto_name_session(
        session_id=SESSION_ID,
        first_message="first user message for the session",
        llm_client=env.llm,
    )


async def test_auto_named_title_not_logged(chat_env):
    """AC1: auto-named title marker absent from all INFO+ chat records.

    RED at base (``logger.info("Auto-named untitled session %d: %s", ...)``
    emits the title verbatim), GREEN after the fix.
    """
    with capture_records(CHAT_LOGGER, logging.INFO) as captured:
        await _auto_name_with_marker_title(chat_env)

    leaks = [r for r in captured.records if TITLE_MARKER in r.getMessage()]
    assert leaks == [], (
        f"{len(leaks)} INFO+ record(s) of {CHAT_LOGGER} carry the auto-named "
        f"session title (an LLM summary of the user's first message — "
        f"violates the #287/#710/#909 contract): {[r.getMessage() for r in leaks]}"
    )


async def test_auto_name_still_logs_session_id(chat_env):
    """AC2: session id + auto-naming fact still logged at INFO.

    GREEN at base, GREEN after the fix — redaction must not silence the
    operational signal.
    """
    with capture_records(CHAT_LOGGER, logging.INFO) as captured:
        await _auto_name_with_marker_title(chat_env)

    infos = [r.getMessage() for r in captured.records]
    assert any(
        str(SESSION_ID) in message and "auto-named" in message.lower()
        for message in infos
    ), (
        "auto-naming must still log the session id and the auto-name fact at "
        f"INFO; captured: {infos}"
    )
