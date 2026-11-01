"""Review-driven hardening for issue #909 (C15): the auto-name FAILURE path.

Plan-critic Round 1, finding F3 (live-probed): when the LLM summarization
call raises ``LLMError("HTTP error 400: <provider body echo>")``, the
failure WARNING at chat.py formatted the whole exception — and an LLM
provider error body can echo the request payload (the prompt contains the
user's first message), so the provider echo reached the default log through
the except branch of the very helper #909 remediates. The fix logs the
exception class only at WARNING (full text at DEBUG).

This file is additive review-driven coverage, outside the frozen
acceptance table (the frozen checks C7/C8 pin the two success-path INFO
sites; this pins the failure-path WARNING).
"""

import logging
import os
import sys
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Optional-dependency stubs (same block as test_auto_name_session.py /
# test_c10_session_title_redaction.py).
try:
    import lancedb  # noqa: F401
except ImportError:
    import types

    sys.modules["lancedb"] = types.ModuleType("lancedb")

try:
    import pyarrow  # noqa: F401
except ImportError:
    import types

    sys.modules["pyarrow"] = types.ModuleType("pyarrow")

try:
    from unstructured.partition.auto import partition  # noqa: F401
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
BODY_MARKER = "C15_AUTONAME_ECHO"
SESSION_ID = 42


class _ListHandler(logging.Handler):
    def __init__(self, level: int):
        super().__init__(level=level)
        self.records = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@contextmanager
def capture_records(logger_name: str, level: int = logging.INFO):
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
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, exc_type, exc, tb):
        return None


class _FailingLLMEnv:
    """Patched chat route whose LLM call raises a body-echoing LLMError."""

    def __init__(self):
        self.executed = []
        self.conn = MagicMock()

        def mock_execute(query, params=None):
            cursor = MagicMock()
            cursor.rowcount = 1
            self.executed.append({"query": query, "params": params})
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

    def stop(self):
        for patcher in reversed(self._patches):
            patcher.stop()


@pytest.fixture
def failing_env():
    env = _FailingLLMEnv()
    yield env
    env.stop()


async def _drive_failure(env: _FailingLLMEnv) -> None:
    # The REAL production exception type (swarm-pr-review PRR-017c): a local
    # subclass made the class-name assertion pass by construction.
    from app.services.llm_client import LLMError

    async def raise_body_echo(*args, **kwargs):
        raise LLMError(f"HTTP error 400: {BODY_MARKER} provider echo")

    llm = AsyncMock()
    llm.chat_completion = raise_body_echo
    await _auto_name_session(
        session_id=SESSION_ID,
        first_message=f"first message {BODY_MARKER} in the user text",
        llm_client=llm,
    )


async def test_autoname_failure_body_echo_not_logged(failing_env):
    """The provider-body echo must not reach any INFO+ chat record.

    Pre-fix behavior (plan-critic probe): the WARNING formatted the whole
    LLMError, emitting "HTTP error 400: <body marker>". Post-fix the WARNING
    names only the exception class.
    """
    with capture_records(CHAT_LOGGER, logging.INFO) as captured:
        await _drive_failure(failing_env)

    leaks = [r.getMessage() for r in captured.records if BODY_MARKER in r.getMessage()]
    assert leaks == [], (
        f"auto-name failure path leaked the provider body echo at INFO+: {leaks} "
        "(#909/#287 contract: user-derived text stays out of default-level logs)"
    )


async def test_autoname_failure_warning_keeps_operator_signal(failing_env):
    """Redaction must not silence the failure: session id, the failure fact,
    the exception class, and the fallback must all still be observable."""
    with capture_records(CHAT_LOGGER, logging.INFO) as captured:
        await _drive_failure(failing_env)

    messages = [r.getMessage() for r in captured.records if r.levelno >= logging.WARNING]
    assert any(
        str(SESSION_ID) in m and "failed" in m.lower() and "fallback" in m.lower()
        for m in messages
    ), f"failure WARNING must keep session id + failure fact + fallback: {messages}"
    assert any("LLMError" in m for m in messages), (
        f"failure WARNING must name the exception class: {messages}"
    )

    fallback_writes = [
        e for e in failing_env.executed if "New conversation" in str(e["params"])
    ]
    assert fallback_writes, (
        "the fallback title write must still happen on LLM failure; executed: "
        f"{failing_env.executed}"
    )
