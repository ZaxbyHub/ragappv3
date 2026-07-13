"""DI evaluate_policy wiring tests for session/message management chat routes.

Adds to test_chat_route_policy_di.py which covers the /chat and /chat/stream
endpoints. This file covers the remaining endpoints that also migrated to
``evaluate: Callable = Depends(get_evaluate_policy)``:

    GET  /chat/sessions/{session_id}   — evaluate(user, "vault", vault_id, "read")
    POST /chat/sessions                — evaluate(user, "vault", vault_id, "write")
    POST /chat/sessions/{session_id}/fork — evaluate(user, "vault", vault_id, "write")
    POST /chat/sessions/{session_id}/messages — evaluate(user, "vault", vault_id, "write")
    PATCH .../feedback                 — evaluate(user, "vault", vault_id, "write")
    PUT  /chat/sessions/{session_id}   — evaluate(user, "vault", vault_id, "write")
    DELETE /chat/sessions/{session_id}  — evaluate(user, "vault", vault_id, "write")

Each test verifies that when the DI evaluate callable returns False the endpoint
raises HTTP 403 before performing any mutation. When evaluate returns True the
endpoint proceeds (confirmed by the existing full-integration tests in
test_chat_auth.py which use the real evaluate_policy path).
"""

from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import (
    get_current_active_user,
    get_db,
    get_evaluate_policy,
    get_rag_engine,
)
from app.api.routes.chat import router


def _build_app(*, allow: bool, mock_user: dict = None):
    """Build a standalone FastAPI app with DI overrides for chat session routes."""
    app = FastAPI()
    app.include_router(router, prefix="/api")

    # Mock vector_store for require_model_ready
    app.state.vector_store = MagicMock()
    app.state.vector_store._ready = True

    if mock_user is None:
        mock_user = {"id": 1, "username": "testuser", "role": "member"}
    app.dependency_overrides[get_current_active_user] = lambda: mock_user

    # DI evaluate callable override
    async def _evaluate(*_args, **_kwargs) -> bool:
        return allow

    app.dependency_overrides[get_evaluate_policy] = lambda: _evaluate

    # Mock DB — required for endpoints that declare conn: sqlite3.Connection = Depends(get_db)
    mock_conn = MagicMock()
    app.dependency_overrides[get_db] = lambda: mock_conn

    # Mock RAG engine — required for add_message which has rag_engine dependency
    mock_rag = MagicMock()
    mock_rag.llm_client = None

    async def mock_query(*_args, **_kwargs):
        yield {"type": "done", "sources": [], "memories_used": []}

    mock_rag.query = mock_query
    app.dependency_overrides[get_rag_engine] = lambda: mock_rag

    return app


class TestGetSessionPolicyDI:
    """GET /chat/sessions/{session_id} — evaluate(user, 'vault', vault_id, 'read')."""

    def test_denied_returns_403(self):
        """evaluate=False → 403 with 'No read access'."""
        app = _build_app(allow=False)
        client = TestClient(app)

        resp = client.get("/api/chat/sessions/1")
        assert resp.status_code == 403
        assert "No read access" in resp.text

    def test_allowed_skips_403(self):
        """evaluate=True → no 403 from evaluate (proceeds to DB lookup)."""
        app = _build_app(allow=True)
        client = TestClient(app)

        resp = client.get("/api/chat/sessions/1")
        # Should not be 403 "No read access" — may be 404 or any other code
        # but the evaluate path is confirmed reachable
        assert not (
            resp.status_code == 403 and "No read access" in resp.text
        ), "evaluate=True should not produce a permission-denied 403"


class TestCreateSessionPolicyDI:
    """POST /chat/sessions — evaluate(user, 'vault', vault_id, 'write')."""

    def test_denied_returns_403(self):
        """evaluate=False → 403 'No write access'; no session created."""
        app = _build_app(allow=False)
        client = TestClient(app)

        resp = client.post("/api/chat/sessions", json={"vault_id": 3})
        assert resp.status_code == 403
        assert "No write access" in resp.text

    def test_allowed_skips_403(self):
        """evaluate=True → no 403 from evaluate."""
        app = _build_app(allow=True)
        client = TestClient(app)

        resp = client.post("/api/chat/sessions", json={"vault_id": 3})
        assert not (
            resp.status_code == 403 and "write access" in resp.text
        ), "evaluate=True should not produce a permission-denied 403"


class TestForkSessionPolicyDI:
    """POST /chat/sessions/{session_id}/fork — evaluate(user, 'vault', vault_id, 'write')."""

    def test_denied_returns_403(self):
        """evaluate=False → 403 'No write access'."""
        app = _build_app(allow=False)
        client = TestClient(app)

        resp = client.post(
            "/api/chat/sessions/1/fork",
            json={"message_index": 0},
        )
        assert resp.status_code == 403
        assert "No write access" in resp.text


class TestAddMessagePolicyDI:
    """POST /chat/sessions/{session_id}/messages — evaluate(user, 'vault', vault_id, 'write')."""

    def test_denied_returns_403(self):
        """evaluate=False → 403 'No write access'."""
        app = _build_app(allow=False)
        client = TestClient(app)

        resp = client.post(
            "/api/chat/sessions/1/messages",
            json={"role": "user", "content": "hello"},
        )
        assert resp.status_code == 403
        assert "No write access" in resp.text


class TestSetFeedbackPolicyDI:
    """PATCH /chat/sessions/{session_id}/messages/{message_id}/feedback — evaluate(user, 'vault', vault_id, 'write')."""

    def test_denied_returns_403(self):
        """evaluate=False → 403 'No write access'."""
        app = _build_app(allow=False)
        client = TestClient(app)

        resp = client.patch(
            "/api/chat/sessions/1/messages/1/feedback",
            json={"rating": "up"},
        )
        assert resp.status_code == 403
        assert "No write access" in resp.text


class TestUpdateSessionPolicyDI:
    """PUT /chat/sessions/{session_id} — evaluate(user, 'vault', vault_id, 'write')."""

    def test_denied_returns_403(self):
        """evaluate=False → 403 'No write access'."""
        app = _build_app(allow=False)
        client = TestClient(app)

        resp = client.put(
            "/api/chat/sessions/1",
            json={"title": "New Title"},
        )
        assert resp.status_code == 403
        assert "No write access" in resp.text


class TestDeleteSessionPolicyDI:
    """DELETE /chat/sessions/{session_id} — evaluate(user, 'vault', vault_id, 'write')."""

    def test_denied_returns_403(self):
        """evaluate=False → 403 'No write access'."""
        app = _build_app(allow=False)
        client = TestClient(app)

        resp = client.delete("/api/chat/sessions/1")
        assert resp.status_code == 403
        assert "No write access" in resp.text
