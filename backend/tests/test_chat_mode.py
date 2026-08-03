"""Smoke tests for ChatMode enum and instant-mode settings defaults."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.config import settings
from app.models.chat_mode import ChatMode


def test_chat_mode_enum_values():
    assert ChatMode("instant") == ChatMode.INSTANT
    assert ChatMode("thinking") == ChatMode.THINKING
    assert ChatMode.INSTANT.value == "instant"
    assert ChatMode.THINKING.value == "thinking"
    # str-Enum: members compare equal to their string value
    assert ChatMode.INSTANT == "instant"


def test_chat_mode_enum_rejects_unknown():
    import pytest
    with pytest.raises(ValueError):
        ChatMode("nonsense")


def test_instant_mode_settings_defaults_present():
    assert settings.instant_chat_url == "http://host.docker.internal:1234"
    assert settings.instant_chat_model == "nvidia/nemotron-3-nano-4b"
    assert settings.default_chat_mode in ("instant", "thinking")
    assert isinstance(settings.instant_initial_retrieval_top_k, int)
    assert isinstance(settings.instant_reranker_top_n, int)
    assert isinstance(settings.instant_memory_context_top_k, int)
    assert isinstance(settings.instant_max_tokens, int)
    # thinking-mode token budget is configurable and defaults to the prior
    # hardcoded 32768 (issue #395 DD-rag-005).
    assert isinstance(settings.thinking_max_tokens, int)
    assert settings.thinking_max_tokens == 32768
