"""Editorial no-think control (F2 / issue #712).

create_editorial_client() must send ``chat_template_kwargs={"enable_thinking":
False}`` by default (a server whose chat template already disables
# reasoning will just ignore it, but one that does not has
a reasoning-on template default with xhigh effort and will burn a small
budget on reasoning and return empty content), and must send no control when
``DRAFT_EDITORIAL_ENABLE_THINKING=true`` opts out.
"""

import pytest

from app.config import Settings
from app.services import llm_client as lc


@pytest.fixture(autouse=True)
def _allow_test_endpoints(monkeypatch):
    monkeypatch.setenv("ALLOW_LOCAL_SERVICES", "1")


def _client_with(env: dict[str, str]):
    settings = Settings(**{"_env_file": None, **env})
    orig = lc.settings
    lc.settings = settings
    try:
        return lc.create_editorial_client()
    finally:
        lc.settings = orig


def test_editorial_client_sends_no_think_by_default(monkeypatch):
    monkeypatch.setenv("DRAFT_EDITORIAL_CHAT_URL", "http://127.0.0.1:8200")
    monkeypatch.setenv("DRAFT_EDITORIAL_CHAT_MODEL", "custom-editorial-27b")
    client = _client_with({})
    assert client.chat_template_kwargs == {"enable_thinking": False}


def test_editorial_client_omits_control_when_opted_in(monkeypatch):
    monkeypatch.setenv("DRAFT_EDITORIAL_CHAT_URL", "http://127.0.0.1:8200")
    monkeypatch.setenv("DRAFT_EDITORIAL_CHAT_MODEL", "custom-editorial-27b")
    monkeypatch.setenv("DRAFT_EDITORIAL_ENABLE_THINKING", "true")
    client = _client_with(
        {"DRAFT_EDITORIAL_ENABLE_THINKING": "true"}
    )
    assert client.chat_template_kwargs == {}


def test_name_based_helper_does_not_recognise_alias():
    """Documents the helper gap: the alias 'custom-editorial-27b' is a Qwen3.8
    merge, but select_no_think_chat_template_kwargs keys on the id substring
    "qwen" and returns None. That is why create_editorial_client sends the
    control directly instead of via the helper."""
    assert lc.select_no_think_chat_template_kwargs("custom-editorial-27b") is None
    assert lc.select_no_think_chat_template_kwargs("Qwen3.8-27B-anything") == {
        "enable_thinking": False
    }


def test_editorial_client_unconfigured_raises(monkeypatch):
    monkeypatch.delenv("DRAFT_EDITORIAL_CHAT_URL", raising=False)
    monkeypatch.delenv("OLLAMA_CHAT_URL", raising=False)
    with pytest.raises(lc.ModelNotConfiguredError):
        _client_with({"DRAFT_EDITORIAL_CHAT_URL": "", "OLLAMA_CHAT_URL": ""})
