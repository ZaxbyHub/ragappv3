"""Smoke tests for ChatMode enum and instant-mode settings defaults."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.config import Settings, settings
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
    # No model defaults ship (issue #570): endpoints are operator-configured.
    # Construct a fresh Settings (not the test-configured singleton, which
    # conftest's _default_configured_chat_endpoints fixture populates).
    fresh = Settings(
        _env_file=None,
        admin_secret_token="test-admin-token",
        jwt_secret_key="test-jwt-secret-key",
    )
    assert fresh.instant_chat_url == ""
    assert fresh.instant_chat_model == ""
    assert settings.default_chat_mode in ("instant", "thinking")
    assert isinstance(settings.instant_initial_retrieval_top_k, int)
    assert isinstance(settings.instant_reranker_top_n, int)
    assert isinstance(settings.instant_memory_context_top_k, int)
    assert isinstance(settings.instant_max_tokens, int)
    # thinking-mode token budget is configurable and defaults to the prior
    # hardcoded 32768 (issue #395 DD-rag-005).
    assert isinstance(settings.thinking_max_tokens, int)
    assert settings.thinking_max_tokens == 32768


def test_per_family_instant_no_think_control_selection():
    """AC7 (issue #554): the instant client's no-think control is selected by
    the configured model family, not hard-coded for every model."""
    import app.services.llm_client as llm_client_mod

    select = getattr(
        llm_client_mod, "select_no_think_chat_template_kwargs", None
    )
    assert select is not None, (
        "AC7-C7: app.services.llm_client must expose "
        "select_no_think_chat_template_kwargs(model) so the no-think control "
        "is selected per model family"
    )
    # Qwen family (any casing / org prefix) -> the Qwen-style template kwarg.
    assert select("qwen/qwen3.5-122b") == {"enable_thinking": False}, (
        "AC7-C7: Qwen-family model names must select the "
        "chat_template_kwargs={'enable_thinking': False} control"
    )
    assert select("Qwen3-Coder-30B") == {"enable_thinking": False}, (
        "AC7-C7: family matching must be case-insensitive"
    )
    # Unrecognized family (the configured default nemotron) and empty/missing
    # names -> NO control at all (log and fail open, never raise).
    assert select("nvidia/nemotron-3-nano-4b") is None, (
        "AC7-C7: an unrecognized model family must fail open with no control"
    )
    assert select("") is None, "AC7-C7: empty model name must fail open"
    assert select(None) is None, "AC7-C7: missing model name must fail open"
