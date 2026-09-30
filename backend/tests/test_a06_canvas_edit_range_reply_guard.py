"""Issue #688 acceptance checks (defect 5): edit-range reply guards.

The edit-range route splices the model's replacement text for the selected
line range and appends it as a ``model_edit`` version. Two degenerate model
replies must NOT be spliced into version history:

* an EMPTY reply for a NON-EMPTY selection — deleting the selected lines was
  never instructed; the base route happily splices ``''`` (leaving an empty
  line where content was) and records a bogus version, returning 200;
* a LENGTH-TRUNCATED reply (``finish_reason == "length"``) — the provider cut
  the answer at the token limit, so the replacement is a fragment that must
  not be persisted as though it were the full edit.

Both checks are RED at the pre-fix base (a second version IS recorded) and
turn GREEN once the route rejects these replies without appending.

The harness is the shared CanvasTestBase TestClient pattern from
``test_canvas_artifacts.py`` (temp DB + dependency overrides +
``app.state.llm_client``). The truncated-reply check installs a REAL
``LLMClient`` whose ``_client`` is an ``httpx.AsyncClient`` backed by
``httpx.MockTransport`` (established pattern, see ``test_issue494_llm_client.py``)
so the finish_reason flows through the production parse path into
``last_metrics``.

Note: this module deliberately does not manage request-forgery tokens itself;
the shared conftest toggles the test-only bypass for modules like this one
that do not assert that protection.
"""

import asyncio
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Stub missing optional dependencies (same guards as the other route suites).
try:
    import lancedb  # noqa: F401
except ImportError:
    import types

    sys.modules["lancedb"] = types.ModuleType("lancedb")

try:
    import pyarrow  # noqa: F401
except ImportError:
    import types

    sys.modules["pyarrow"] = types.ModuleType("pyarrow")

from _db_pool import SimpleConnectionPool
from fastapi.testclient import TestClient

from app.api.deps import get_db
from app.config import settings
from app.main import app
from app.models.database import _pool_cache, _pool_cache_lock, init_db, run_migrations
from app.services.auth_service import compute_client_fingerprint, create_access_token
from app.services.llm_client import LLMClient

PW = "unused-test-password-hash"


class CanvasTestBase(unittest.TestCase):
    """Shared fixture chain: temp DB, app client, users/vault/session seeds.

    Copied from ``test_canvas_artifacts.py`` (issue #509 suite).
    """

    def setUp(self):
        self.client = TestClient(app)
        # Match the fingerprint baked into generated tokens.
        self.client.headers["user-agent"] = ""
        self._temp_dir = tempfile.mkdtemp()

        self._originals = {
            "jwt_secret_key": settings.jwt_secret_key,
            "users_enabled": settings.users_enabled,
            "data_dir": settings.data_dir,
            "canvas_enabled": settings.canvas_enabled,
            "canvas_max_artifact_kb": settings.canvas_max_artifact_kb,
        }
        settings.data_dir = Path(self._temp_dir)
        settings.jwt_secret_key = "test-secret-key-for-testing-at-least-32-chars-long"
        settings.users_enabled = True
        settings.canvas_enabled = True
        settings.canvas_max_artifact_kb = 512

        self._db_path = str(Path(self._temp_dir) / "app.db")

        self._reset_shared_pool_cache()
        init_db(self._db_path)
        run_migrations(self._db_path)
        self._connection_pool = SimpleConnectionPool(self._db_path)

        def override_get_db():
            conn = self._connection_pool.get_connection()
            try:
                yield conn
            finally:
                self._connection_pool.release_connection(conn)

        app.dependency_overrides[get_db] = override_get_db

        # Seed users AFTER migrations so the orphan->Default-vault assignment
        # does not grant unexpected read rows on vault 1.
        conn = self._connection_pool.get_connection()
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("DELETE FROM vault_members")
            conn.execute("DELETE FROM canvas_versions")
            conn.execute("DELETE FROM canvas_artifacts")
            conn.execute("DELETE FROM chat_messages")
            conn.execute("DELETE FROM chat_sessions")
            conn.execute("DELETE FROM users WHERE id != 0")
            # 1: superadmin (policy passes everything)
            conn.execute(
                "INSERT INTO users (id, username, hashed_password, full_name, role, is_active) "
                "VALUES (?, ?, ?, ?, ?, 1)",
                (1, "superadmin", PW, "Super Admin", "superadmin"),
            )
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)

    def tearDown(self):
        if hasattr(self, "_connection_pool"):
            self._connection_pool.close_all()
        self._reset_shared_pool_cache()
        app.dependency_overrides.pop(get_db, None)
        if hasattr(app.state, "llm_client"):
            delattr(app.state, "llm_client")
        for key, value in self._originals.items():
            setattr(settings, key, value)
        shutil.rmtree(self._temp_dir, ignore_errors=True)

    @staticmethod
    def _reset_shared_pool_cache():
        with _pool_cache_lock:
            for pool in list(_pool_cache.values()):
                pool.close_all()
            _pool_cache.clear()

    # ── seeds ────────────────────────────────────────────────────────────

    def _db(self):
        return self._connection_pool.get_connection()

    def _release(self, conn):
        self._connection_pool.release_connection(conn)

    def _make_session(self, vault_id=1, user_id=1, title="Canvas session"):
        conn = self._db()
        try:
            session_id = conn.execute(
                "INSERT INTO chat_sessions (vault_id, user_id, title) VALUES (?, ?, ?)",
                (vault_id, user_id, title),
            ).lastrowid
            conn.commit()
            return session_id
        finally:
            self._release(conn)

    # ── auth ─────────────────────────────────────────────────────────────

    def _token(self, user_id, username, role):
        return create_access_token(
            user_id, username, role, client_fingerprint=compute_client_fingerprint("")
        )

    def _super_headers(self):
        return {"Authorization": f"Bearer {self._token(1, 'superadmin', 'superadmin')}"}

    # ── canvas helpers ───────────────────────────────────────────────────

    def _create_artifact(self, session_id, content="print('hello')\n", name="solution",
                         kind="code", language="python"):
        payload = {"kind": kind, "name": name, "content": content}
        if language is not None:
            payload["language"] = language
        return self.client.post(
            f"/api/chat/sessions/{session_id}/artifacts",
            json=payload,
            headers=self._super_headers(),
        )

    def _install_llm(self, replacement):
        mock = MagicMock()
        mock.chat_completion = AsyncMock(return_value=replacement)
        app.state.llm_client = mock
        return mock

    def _edit_line_2(self, uid, instruction="rewrite line 2"):
        return self.client.post(
            f"/api/canvas/artifacts/{uid}/edit-range",
            json={
                "start_line": 2,
                "end_line": 2,
                "instruction": instruction,
                "base_version_no": 1,
            },
            headers=self._super_headers(),
        )

    def _version_count(self, uid):
        listing = self.client.get(
            f"/api/canvas/artifacts/{uid}/versions", headers=self._super_headers()
        )
        assert listing.status_code == 200, listing.text
        return len(listing.json()["versions"])

    @staticmethod
    def _close_llm_client(client: LLMClient) -> None:
        """Best-effort close of the MockTransport-backed AsyncClient.

        The transport holds no real sockets, so closing from a fresh loop is
        safe; nothing here may fail the test.
        """
        if client._client is not None:
            try:
                asyncio.run(client._client.aclose())
            except RuntimeError:
                pass
            client._client = None


class TestA06CanvasEditRangeReplyGuard(CanvasTestBase):
    """Degenerate model replies must never become canvas versions."""

    def test_empty_reply_for_nonempty_selection_records_no_version(self):
        session_id = self._make_session()
        uid = self._create_artifact(
            session_id, content="alpha\nbeta\ngamma"
        ).json()["artifact_uid"]

        self._install_llm("")
        response = self._edit_line_2(uid)

        # The splice must NOT have appended a model_edit version. At base the
        # '' replacement is spliced (leaving an empty line where "beta" was),
        # a second version IS recorded and the route returns 200.
        assert self._version_count(uid) == 1
        assert response.status_code >= 400

    def test_length_truncated_reply_records_no_version(self):
        session_id = self._make_session()
        uid = self._create_artifact(
            session_id, content="alpha\nbeta\ngamma"
        ).json()["artifact_uid"]

        requests_seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests_seen.append(request)
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {"content": "partial"},
                            "finish_reason": "length",
                        }
                    ]
                },
                request=request,
            )

        with patch("app.services.llm_client.assert_url_safe"):
            llm = LLMClient(base_url="http://llm-test-host:11434", model="test-model")
        llm._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addCleanup(self._close_llm_client, llm)
        app.state.llm_client = llm

        self._edit_line_2(uid)

        # The truncated fragment must NOT have been appended as a version. At
        # base 'partial' replaces "beta", a second version IS recorded, and
        # the route returns 200.
        assert self._version_count(uid) == 1
        # In-test controls against a vacuous pass: the provider WAS consulted
        # (exactly once, through the production chat_completion path) and the
        # length-truncation really was reported by the provider.
        assert len(requests_seen) == 1
        assert llm.last_metrics["finish_reason"] == "length"


if __name__ == "__main__":
    unittest.main()
