"""Companion coverage for the #1179 host-exemption surface.

All legs drive _TrustedHostWithHealthExempt ITSELF (the frozen C11 pin
instantiates Starlette's base class, so it cannot see a blanket bypass).
NOT a frozen check file.
"""
import os

os.environ.setdefault("ADMIN_SECRET_TOKEN", "test-admin-secret")
os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import _TrustedHostWithHealthExempt  # noqa: E402


def _app() -> FastAPI:
    app = FastAPI()

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.get("/api/health")
    async def api_health():
        return {"ok": True}

    @app.get("/api/healthz")
    async def api_healthz():
        return {"ok": True}

    @app.get("/ordinary")
    async def ordinary():
        return {"ok": True}

    return app


def _client(app: FastAPI) -> TestClient:
    wrapped = _TrustedHostWithHealthExempt(app, allowed_hosts=["example.com"])
    return TestClient(wrapped, raise_server_exceptions=False)


class TestExemptionSurface:
    def test_all_three_health_paths_exempt(self):
        c = _client(_app())
        for path in ("/health", "/api/health", "/api/healthz"):
            r = c.get(path, headers={"Host": "localhost"})
            assert r.status_code == 200, path

    def test_unknown_host_on_ordinary_path_rejected(self):
        c = _client(_app())
        r = c.get("/ordinary", headers={"Host": "evil.example.net"})
        assert r.status_code == 400, (
            "the exemption must not blanket-bypass host validation"
        )

    def test_allowed_host_on_ordinary_path_served(self):
        c = _client(_app())
        r = c.get("/ordinary", headers={"Host": "example.com"})
        assert r.status_code == 200

    def test_trailing_slash_normalization(self):
        c = _client(_app())
        r = c.get("/health/", headers={"Host": "localhost"})
        assert r.status_code == 200

    def test_bare_path_with_root_path_scope(self):
        # Supported subpath shape (admin-guide: the proxy strips the mount
        # prefix): the backend sees the bare path while root_path is set.
        app = _app()
        wrapped = _TrustedHostWithHealthExempt(app, allowed_hosts=["example.com"])

        async def _drive():
            received = {}

            async def receive():
                return {"type": "http.request", "body": b"", "more_body": False}

            async def send(message):
                if message["type"] == "http.response.start":
                    received["status"] = message["status"]

            scope = {
                "type": "http",
                "http_version": "1.1",
                "method": "GET",
                "scheme": "http",
                "path": "/health",
                "root_path": "/meridian",
                "headers": [(b"host", b"localhost")],
                "query_string": b"",
                "server": ("localhost", 80),
                "client": ("127.0.0.1", 1),
            }
            await wrapped(scope, receive, send)
            return received.get("status")

        import asyncio

        status = asyncio.run(_drive())
        assert status == 200

    def test_non_http_scope_delegates_to_base(self):
        # A websocket-style scope must pass through the base implementation
        # unharmed (its host check answers 403-close, not crash).
        app = _app()
        wrapped = _TrustedHostWithHealthExempt(app, allowed_hosts=["example.com"])

        async def _drive():
            events = []

            async def receive():
                return {"type": "websocket.connect"}

            async def send(message):
                events.append(message)

            scope = {
                "type": "websocket",
                "http_version": "1.1",
                "scheme": "ws",
                "path": "/ws",
                "root_path": "",
                "headers": [(b"host", b"localhost")],
                "query_string": b"",
                "server": ("localhost", 80),
                "client": ("127.0.0.1", 1),
                "subprotocols": [],
            }
            try:
                await wrapped(scope, receive, send)
            except Exception as exc:  # noqa: BLE001
                return f"error:{type(exc).__name__}", events
            return "ok", events

        import asyncio

        outcome, events = asyncio.run(_drive())
        # What matters for the exemption fix: a non-HTTP scope traverses
        # __call__ without crashing and without the health exemption
        # misfiring (no http.response.start events, no pass-through of an
        # http-style response). The base/app stack owns the exact reject
        # shape (host check close vs router 403-close).
        assert outcome == "ok", outcome
        assert not [e for e in events if e.get("type") == "http.response.start"], (
            "a websocket scope must not be served as an exempt HTTP response"
        )
