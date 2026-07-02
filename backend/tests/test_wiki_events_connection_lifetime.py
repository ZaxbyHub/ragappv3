"""Connection lifetime regression tests for the wiki SSE event stream.

This test exercises the connection-release behavior of
``get_wiki_events_auth_context`` (the short-lived auth dep injected into
``wiki_events_stream``) by mounting a ``/probe`` endpoint that uses the
same dependency. It does NOT directly call the real ``/api/wiki/events``
SSE endpoint because ``TestClient`` deadlocks on its infinite ``while True``
async generator. The probe route returns immediately after the auth
dependency releases the pool, so we can assert ``tracker.checked_out == 0``
at that point. To verify the real SSE body also keeps no extra connection
pinned, see ``test_wiki_events.py::test_event_generator_yields``
(or add an equivalent test that calls the async generator directly).
"""

from unittest.mock import MagicMock, patch

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.api.routes.wiki import get_wiki_events_auth_context, router
from app.config import settings


class ConnectionTracker:
    def __init__(self):
        self.checked_out = 0

    def get_connection(self):
        self.checked_out += 1
        return MagicMock()

    def release_connection(self, _conn):
        self.checked_out -= 1


def test_wiki_events_releases_auth_db_connection_before_streaming():
    app = FastAPI()
    app.include_router(router, prefix="/api")

    @app.get("/probe")
    async def probe(_user: dict = Depends(get_wiki_events_auth_context)):
        return {"checked_out": tracker.checked_out}

    tracker = ConnectionTracker()

    original_users_enabled = settings.users_enabled
    original_admin_secret = settings.admin_secret_token
    original_wiki_enabled = settings.wiki_enabled
    settings.users_enabled = False
    settings.admin_secret_token = "test-admin-token"
    settings.wiki_enabled = True
    try:
        client = TestClient(app)
        with patch("app.api.routes.wiki.get_pool", return_value=tracker):
            resp = client.get(
                "/probe?vault_id=5",
                headers={"Authorization": "Bearer test-admin-token"},
            )
            assert resp.status_code == 200
            assert resp.json() == {"checked_out": 0}
            assert tracker.checked_out == 0
    finally:
        settings.users_enabled = original_users_enabled
        settings.admin_secret_token = original_admin_secret
        settings.wiki_enabled = original_wiki_enabled
