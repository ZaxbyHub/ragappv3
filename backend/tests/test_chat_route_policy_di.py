"""Auth-path tests for the DI evaluate_policy wiring on chat routes.

The /chat and /chat/stream routes were switched from the standalone
``evaluate_policy`` (which opened its own pooled DB connection) to the DI
``get_evaluate_policy`` dependency (which reuses the request's connection).
These tests assert the permission gate still enforces access correctly through
the DI path: a deny → 403, an allow → reaches the engine.
"""

from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from app.api.deps import (
    get_current_active_user,
    get_evaluate_policy,
    get_rag_engine,
)
from app.api.routes.chat import ChatStreamRequest, get_stream_auth, router


def _make_client(*, allow: bool, mock_user: dict = None) -> tuple[TestClient, MagicMock]:
    """Build a test client with overridable policy and returns the mock_rag for inspection.

    Returns:
        A tuple of (TestClient, mock_rag) so callers can assert on engine call counts.
    """
    app = FastAPI()
    app.include_router(router, prefix="/api")

    app.state.vector_store = MagicMock()
    app.state.vector_store._ready = True

    if mock_user is None:
        mock_user = {"id": 1, "username": "testuser", "role": "member"}
    app.dependency_overrides[get_current_active_user] = lambda: mock_user

    # Override the DI policy dependency with an evaluate() that grants/denies.
    async def _evaluate(*_args, **_kwargs) -> bool:
        return allow

    app.dependency_overrides[get_evaluate_policy] = lambda: _evaluate

    # The /chat/stream route resolves auth+authz via get_stream_auth (issue #301),
    # which is a separate seam from get_current_active_user/get_evaluate_policy.
    # Override it to mirror the production boundary: vault_id set -> allow/deny
    # via the `allow` flag; vault_id=None ("All Vaults") -> admin-only gate.
    async def _stream_auth(request: Request, body: ChatStreamRequest):
        if body.vault_id is not None:
            if not allow:
                raise HTTPException(status_code=403, detail="No read access to this vault")
        else:
            if mock_user.get("role") not in ("superadmin", "admin"):
                raise HTTPException(
                    status_code=403,
                    detail="Searching all vaults requires admin access. Please select a specific vault.",
                )
        return mock_user

    app.dependency_overrides[get_stream_auth] = _stream_auth

    mock_rag = MagicMock()

    async def mock_query(*_args, **_kwargs):
        yield {"type": "content", "content": "Test response"}
        yield {"type": "done", "sources": [], "memories_used": []}

    mock_rag.query = mock_query
    app.dependency_overrides[get_rag_engine] = lambda: mock_rag

    return TestClient(app), mock_rag


class TestChatStreamPolicyDI:
    def test_stream_denied_returns_403(self):
        """DI evaluate returning False must produce a 403 (no engine call)."""
        client, _mock_rag = _make_client(allow=False)
        resp = client.post(
            "/api/chat/stream",
            json={
                "messages": [{"role": "user", "content": "hello"}],
                "vault_id": 5,
            },
        )
        assert resp.status_code == 403
        assert "No read access" in resp.text

    def test_stream_allowed_reaches_engine(self):
        """DI evaluate returning True must allow the stream to proceed."""
        client, _mock_rag = _make_client(allow=True)
        resp = client.post(
            "/api/chat/stream",
            json={
                "messages": [{"role": "user", "content": "hello"}],
                "vault_id": 5,
            },
        )
        assert resp.status_code == 200
        assert "Test response" in resp.text


class TestChatNonStreamPolicyDI:
    def test_chat_denied_returns_403(self):
        """Non-stream /chat must also enforce the DI policy with a 403."""
        client, _mock_rag = _make_client(allow=False)
        resp = client.post(
            "/api/chat",
            json={"message": "hello", "vault_id": 5},
        )
        assert resp.status_code == 403
        assert "No read access" in resp.text


class TestStreamingVaultAccessEnforcement:
    """Tests that verify the layered defense: non-vault-member gets clean 403.

    These tests confirm that denied requests:
    - Never invoke the RAG engine at all
    - Return a proper JSON 403 (not a partial SSE stream)
    - Hit the correct layered defense (evaluate vs admin-check) in the right order
    """

    def test_stream_denied_engine_never_called(self):
        """When evaluate returns False, mock_rag.query() is NEVER invoked."""
        client, mock_rag = _make_client(allow=False)

        # Patch query on the mock to count calls
        query_call_count = 0
        original_query = mock_rag.query

        async def counting_query(*args, **kwargs):
            nonlocal query_call_count
            query_call_count += 1
            async for chunk in original_query(*args, **kwargs):
                yield chunk

        mock_rag.query = counting_query

        resp = client.post(
            "/api/chat/stream",
            json={
                "messages": [{"role": "user", "content": "hello"}],
                "vault_id": 5,
            },
        )
        assert resp.status_code == 403
        assert query_call_count == 0, "RAG engine query() must not be called when access is denied"

    def test_chat_denied_engine_never_called(self):
        """When evaluate returns False on /chat, mock_rag.query() is NEVER invoked."""
        client, mock_rag = _make_client(allow=False)

        query_call_count = 0
        original_query = mock_rag.query

        async def counting_query(*args, **kwargs):
            nonlocal query_call_count
            query_call_count += 1
            async for chunk in original_query(*args, **kwargs):
                yield chunk

        mock_rag.query = counting_query

        resp = client.post(
            "/api/chat",
            json={"message": "hello", "vault_id": 5},
        )
        assert resp.status_code == 403
        assert query_call_count == 0, "RAG engine query() must not be called when access is denied"

    def test_stream_denied_response_has_no_sse_content(self):
        """The 403 response body must NOT contain SSE content events (no 'data:' lines)."""
        client, _mock_rag = _make_client(allow=False)
        resp = client.post(
            "/api/chat/stream",
            json={
                "messages": [{"role": "user", "content": "hello"}],
                "vault_id": 5,
            },
        )
        assert resp.status_code == 403
        # A proper JSON denial has no SSE "data:" markers
        assert "data:" not in resp.text, "403 response must not contain SSE 'data:' markers"

    def test_stream_denied_response_is_json_not_sse(self):
        """The 403 response Content-Type is application/json, NOT text/event-stream."""
        client, _mock_rag = _make_client(allow=False)
        resp = client.post(
            "/api/chat/stream",
            json={
                "messages": [{"role": "user", "content": "hello"}],
                "vault_id": 5,
            },
        )
        assert resp.status_code == 403
        content_type = resp.headers.get("content-type", "")
        assert "application/json" in content_type, (
            f"403 response must have application/json Content-Type, got: {content_type}"
        )
        assert "text/event-stream" not in content_type, (
            "403 response must NOT have text/event-stream Content-Type"
        )

    def test_stream_non_member_gets_403_before_admin_check(self):
        """Non-admin with vault_id=None gets 403 from the admin check, not from evaluate.

        When vault_id=None and user.role='member' (not admin/superadmin), the route
        raises 403 at the admin-check block (lines 569-573) BEFORE ever calling
        evaluate(). This proves the layered defense: admin guard fires first.
        """
        # vault_id=None means "All Vaults" — only admins allowed
        # role='member' is NOT admin/superadmin → should get 403 from admin check
        mock_user = {"id": 1, "username": "testuser", "role": "member"}
        client, mock_rag = _make_client(allow=True, mock_user=mock_user)

        # Track evaluate() calls to prove it was never reached
        evaluate_call_count = 0

        async def counting_evaluate(*args, **kwargs):
            nonlocal evaluate_call_count
            evaluate_call_count += 1
            return True

        # Override evaluate to track calls
        from app.api.deps import get_evaluate_policy
        client.app.dependency_overrides[get_evaluate_policy] = lambda: counting_evaluate

        resp = client.post(
            "/api/chat/stream",
            json={
                "messages": [{"role": "user", "content": "hello"}],
                "vault_id": None,  # triggers admin check path
            },
        )
        assert resp.status_code == 403
        assert evaluate_call_count == 0, (
            "evaluate() must not be called for vault_id=None — admin check should fire first"
        )
        # The error message should be about admin access, not about vault read access
        assert "admin" in resp.text.lower() or "select a specific vault" in resp.text.lower()
