"""Provider preflight (item 10 / F3): reachability fail-fast + editorial warm-up."""

import asyncio

import pytest

from app.api.routes import draft_room as dr
from app.services import llm_health


@pytest.fixture(autouse=True)
def _no_local_services(monkeypatch):
    """Other suites may leave ALLOW_LOCAL_SERVICES=1 in the environment;
    with it set the routes skip provider preflight entirely."""
    monkeypatch.delenv("ALLOW_LOCAL_SERVICES", raising=False)


class _StubResp:
    def __init__(self, status_code):
        self.status_code = status_code


@pytest.mark.asyncio
async def test_provider_reachable_false_on_closed_port():
    assert await dr._provider_reachable("http://127.0.0.1:1") is False


@pytest.mark.asyncio
async def test_provider_reachable_true_on_2xx(monkeypatch):
    class _Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            return _StubResp(200)

    monkeypatch.setattr(dr.httpx, "AsyncClient", _Client)
    assert await dr._provider_reachable("http://127.0.0.1:8200") is True


@pytest.mark.asyncio
async def test_preflight_fails_fast_with_code(monkeypatch):
    monkeypatch.setattr(dr, "_assert_provider_policy", lambda *, sensitive: None)
    monkeypatch.setattr(
        dr, "_provider_base_url_for_mode", lambda m: "http://127.0.0.1:1" if m == "editorial" else ""
    )

    async def _unreachable(url):
        return False

    monkeypatch.setattr(dr, "_provider_reachable", _unreachable)
    with pytest.raises(dr.DraftRoomHTTPError) as exc:
        await dr._preflight_providers(sensitive=False)
    assert exc.value.status_code == 503
    assert getattr(exc.value, "code", None) == "provider_unreachable"


@pytest.mark.asyncio
async def test_preflight_passes_when_all_providers_reachable(monkeypatch):
    monkeypatch.setattr(dr, "_assert_provider_policy", lambda *, sensitive: None)

    async def _reach(url):
        return True

    monkeypatch.setattr(dr, "_provider_reachable", _reach)
    await dr._preflight_providers(sensitive=False)


@pytest.mark.asyncio
async def test_check_editorial_not_configured():
    import app.config as config

    orig = config.settings.editorial_chat_url
    config.settings.editorial_chat_url = ""
    try:
        result = await llm_health.LLMHealthChecker(timeout=2.0).check_editorial()
    finally:
        config.settings.editorial_chat_url = orig
    assert result["ok"] is False
    assert result["status"] == "not_configured"
