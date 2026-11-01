"""Route-level redaction mapping pins (issue #717 review round 3).

The swarm review + out-of-band review both flagged that the ROUTE-level
mapping ``is_admin = not require_vault`` (chat_stream, chat.py:2136) was
unpinned: TestSseDonePayloadWiring drives ``stream_chat_response`` directly
with an explicit ``is_admin`` kwarg, so inverting the route mapping leaves
every test green. This file drives the real ``/api/chat/stream`` route
through TestClient with the get_stream_auth DI override (the
test_chat_route_policy_di harness pattern) and asserts the emitted SSE done
frame drops both provider URLs for a member and keeps them for an admin.

Also pins the instant-key fallback: check_models' instant probe must carry
the instant key when set and the CHAT key when only that is configured
(issue #622 runtime parity, OOB auth-identity-secrets-001).

RED at the review head (4d18f65b) for the mapping inversion; GREEN after
the feedback fixes.
"""

import contextlib
import json
from unittest.mock import MagicMock, patch

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

import app.services.circuit_breaker as circuit_breaker_module
from app.api.deps import get_rag_engine
from app.api.routes.chat import ChatStreamRequest, get_stream_auth, router

PROVIDER_URL = "http://provider.internal:8080/v1"
FALLBACK_URL = "http://fallback.internal:9090/v1"


def _make_stream_client(role: str) -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.state.vector_store = MagicMock()
    app.state.vector_store._ready = True

    mock_user = {"id": 1, "username": "u", "role": role}

    async def _stream_auth(request: Request, body: ChatStreamRequest):
        if mock_user.get("role") not in ("superadmin", "admin"):
            if body.vault_id is None:
                raise HTTPException(
                    status_code=403,
                    detail="Searching all vaults requires admin access.",
                )
        return mock_user

    app.dependency_overrides[get_stream_auth] = _stream_auth

    mock_rag = MagicMock()

    def mock_query(*_args, **_kwargs):
        async def _gen():
            yield {"type": "content", "content": "answer"}
            yield {
                "type": "done",
                "sources": [],
                "memories_used": [],
                "llm_metrics": {
                    "provider_url": PROVIDER_URL,
                    "fallback_from": FALLBACK_URL,
                    "status": "ok",
                    "latency_ms": 5.0,
                },
            }

        return _gen()

    mock_rag.query = mock_query
    app.dependency_overrides[get_rag_engine] = lambda: mock_rag

    from app.api.routes import chat as chat_module

    patcher = patch.object(
        chat_module,
        "get_admission_controller",
        lambda: _RoutePermissiveAdmission(),
    )
    patcher.start()
    client = TestClient(app)
    # Keep the patch alive for the client's lifetime.
    client._c12c_admission_patch = patcher  # noqa: SLF001
    return client


class _RoutePermissiveAdmission:
    async def queue_depth(self, cls):
        return 0

    @contextlib.asynccontextmanager
    async def admit(self, admission_class, **kwargs):
        yield


def _done_metrics(resp_text: str) -> dict:
    done = None
    for line in resp_text.splitlines():
        if line.startswith("data:") and '"done"' in line:
            done = line
    assert done is not None, "harness: no done frame in stream"
    return json.loads(done.split("data:", 1)[1]).get("llm_metrics") or {}


class TestStreamRouteRedactionMapping:
    """The route's is_admin=not require_vault mapping is load-bearing:
    inverting it must turn these RED."""

    def test_member_stream_drops_both_provider_urls(self):
        client = _make_stream_client("member")
        resp = client.post(
            "/api/chat/stream",
            json={
                "messages": [{"role": "user", "content": "hello"}],
                "vault_id": 5,
            },
        )
        assert resp.status_code == 200
        metrics = _done_metrics(resp.text)
        assert "provider_url" not in metrics
        assert "fallback_from" not in metrics
        assert metrics.get("status") == "ok"

    def test_admin_stream_keeps_both_provider_urls(self):
        client = _make_stream_client("admin")
        resp = client.post(
            "/api/chat/stream",
            json={
                "messages": [{"role": "user", "content": "hello"}],
                "vault_id": 5,
            },
        )
        assert resp.status_code == 200
        metrics = _done_metrics(resp.text)
        assert metrics.get("provider_url") == PROVIDER_URL
        assert metrics.get("fallback_from") == FALLBACK_URL


class TestInstantKeyFallback:
    """OOB auth-identity-secrets-001: the instant probe mirrors the runtime
    client's instant-key-with-chat-fallback."""

    def setUp(self):
        circuit_breaker_module.model_checker_cb.reset()

    def test_instant_probe_uses_chat_key_when_instant_unset(self):
        circuit_breaker_module.model_checker_cb.reset()
        from app.config import settings
        from app.services.model_checker import ModelChecker

        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200, json={"data": [{"id": "m"}]}
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        checker = ModelChecker(timeout=1.0, fallback_timeout=1.0)
        import asyncio

        with patch(
            "app.services.model_checker.assert_url_safe", MagicMock()
        ), patch(
            "app.services.ssrf_transport.SSRFSafeTransport",
            lambda **kw: httpx.MockTransport(handler),
        ), patch.object(settings, "chat_api_key", "k-chat"), \
             patch.object(settings, "instant_api_key", ""), \
             patch.object(settings, "ollama_embedding_url", ""), \
             patch.object(settings, "ollama_chat_url", "http://fb-chat:8080"), \
             patch.object(settings, "chat_model", "m"), \
             patch.object(settings, "instant_chat_url", "http://fb-instant:8080"), \
             patch.object(settings, "instant_chat_model", "m"), \
             patch.object(
                 checker, "_dialect_order", return_value=["openai_compatible"]
             ):
            asyncio.run(checker.check_models())
        instant_reqs = [
            r for r in requests if "fb-instant" in str(r.url)
        ]
        assert instant_reqs, "instant probe never sent"
        assert all(
            r.headers.get("authorization") == "Bearer k-chat"
            for r in instant_reqs
        )
