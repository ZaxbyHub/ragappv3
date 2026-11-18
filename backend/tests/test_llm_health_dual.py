"""Verify LLMHealthChecker.check_chat_modes() probes both Thinking and Instant.

Uses a stub client that records calls and returns success or failure on
demand. We do NOT spin up real LLM endpoints.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import asyncio
from unittest.mock import AsyncMock

from app.services.llm_health import LLMHealthChecker


def _stub_client(ok: bool):
    """Return an object with an async ``chat_completion`` matching LLMClient's signature."""
    stub = AsyncMock()
    if ok:
        stub.chat_completion = AsyncMock(return_value="pong")
    else:
        stub.chat_completion = AsyncMock(side_effect=RuntimeError("backend down"))
    return stub


def test_check_chat_modes_returns_both_keys():
    thinking = _stub_client(ok=True)
    instant = _stub_client(ok=True)
    checker = LLMHealthChecker(
        thinking_client=thinking,
        instant_client=instant,
    )
    result = asyncio.run(checker.check_chat_modes())
    assert result == {"thinking": True, "instant": True, "editorial": False}


def test_check_chat_modes_failing_instant_reported_false():
    thinking = _stub_client(ok=True)
    instant = _stub_client(ok=False)
    checker = LLMHealthChecker(
        thinking_client=thinking,
        instant_client=instant,
    )
    result = asyncio.run(checker.check_chat_modes())
    assert result == {"thinking": True, "instant": False, "editorial": False}


def test_check_chat_modes_missing_clients_fails_closed():
    checker = LLMHealthChecker()  # no clients
    result = asyncio.run(checker.check_chat_modes())
    assert result == {"thinking": False, "instant": False, "editorial": False}
