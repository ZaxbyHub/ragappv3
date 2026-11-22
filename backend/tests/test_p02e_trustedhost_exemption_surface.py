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

    def test_trailing_slash_allowed_host_redirects_and_serves(self):
        # Exact-path exemption (PR #1198 feedback F-02): a trailing-slash form
        # is NOT exempt, so it goes through host validation — an ALLOWED host
        # passes and the router 307s to the canonical path. Stage-B review r1:
        # pin the INTERMEDIATE redirect (follow_redirects=False) before the
        # served canonical response, so a direct-200 regression cannot satisfy
        # the same assertion.
        c = _client(_app())
        r = c.get("/health/", headers={"Host": "example.com"}, follow_redirects=False)
        assert r.status_code == 307
        assert r.headers["location"] == "http://example.com/health"
        served = c.get("/health", headers={"Host": "example.com"})
        assert served.status_code == 200

    def test_trailing_slash_invalid_host_rejected_before_redirect(self):
        # FB-001 discriminator (PR #1198 feedback F-02): with the exemption
        # exact-matched, an invalid Host on a trailing-slash health form is
        # rejected (400) BEFORE the router can build a 307 Location from the
        # unvalidated, attacker-chosen header (was an attacker-reflecting
        # Location under the rstrip normalization).
        c = _client(_app())
        r = c.get("/health/", headers={"Host": "evil.example"})
        assert r.status_code == 400
        assert "location" not in {k.lower() for k in r.headers}

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
        # unharmed. PR #1198 feedback F-05 (closeout review r1 correction):
        # the installed Starlette base class validates WEBSOCKET hosts too
        # (trustedhost.py checks scope["type"] in ("http", "websocket")) and
        # denies an invalid host with a WRAPPED denial — http.response.start
        # re-emitted as websocket.http.response.start (responses.py
        # _wrap_websocket_denial_send). This leg pins that boundary: an
        # invalid-Host websocket scope on an EXEMPT path must be denied by
        # the BASE host validation (app never called) — if the subclass's
        # scope-type guard were removed (mutant: every scope takes the
        # exempt branch), the scope would route to the app instead and the
        # app's router would answer with a bare websocket.close, so BOTH
        # assertions below fail.
        app = _app()
        app_called: list[str] = []

        class _Spy:
            async def __call__(self, scope, receive, send):
                app_called.append(scope["type"])
                await app(scope, receive, send)

        wrapped = _TrustedHostWithHealthExempt(_Spy(), allowed_hosts=["example.com"])

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
                "path": "/health",
                "root_path": "",
                "headers": [(b"host", b"evil.example")],
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
        assert outcome == "ok", outcome
        assert app_called == [], (
            "an invalid-Host websocket scope must be denied by the base host "
            "validation, never routed to the app (scope-type guard bypassed?)"
        )
        assert [e for e in events if e.get("type") == "websocket.http.response.start"], (
            "the denial must be the base validator's wrapped 400 "
            "(websocket.http.response.start), not an app-side websocket.close"
        )
