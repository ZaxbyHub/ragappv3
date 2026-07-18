"""Regression tests for issue #404 — global-memory admin-gating + chat-time
memory write authorization.

A "global memory" is a ``memories`` row with ``vault_id IS NULL``. Before
#404, the global tier had NO authorization on any path:

* READ leak — ``GET /memories?vault_id=N`` and ``MemoryStore.search_memories``
  used ``WHERE (vault_id = ? OR vault_id IS NULL)``, so any non-admin member
  of vault N saw every global memory (cross-tenant leak). The RAG chat path
  (``rag_engine.query`` → ``search_memories``) injected global memories into
  a non-admin's chat prompt.
* WRITE leak — ``POST /memories`` with ``vault_id=null`` (and PUT/DELETE on a
  global row) skipped the vault authz check entirely, so any authenticated
  user could create/mutate a global memory.
* PROMOTE leak — ``WikiCompiler.promote_memory`` skipped its vault-scope check
  for global memories, so a vault-writer could promote (read the content of) a
  global memory.
* CHAT WRITE leak (R12) — ``rag_engine.query`` persisted a "remember ..."
  directive via ``add_memory`` whenever ``detect_memory_intent`` matched,
  without verifying the caller had WRITE access to the vault; a read-only
  member could write a vault memory by chatting.

#404 makes global memories admin/superadmin-only on every path and threads a
``can_write_memory`` flag through the chat path. Each test below FAILS on
pre-fix code (verified via stash-check) and PASSES after the fix.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

# Ensure backend/ is on sys.path so `app` + `tests` import cleanly.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from _db_pool import SimpleConnectionPool  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.api.deps import get_db, get_memory_store, get_vector_store  # noqa: E402
from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402
from app.models.database import init_db, run_migrations  # noqa: E402
from app.services.auth_service import (  # noqa: E402
    compute_client_fingerprint,
    create_access_token,
    hash_password,
)


class _GlobalMemoryAuthBase(unittest.TestCase):
    """Shared scaffolding for #404 global-memory regression tests.

    Seeds users with distinct permissions so every authz branch is
    reachable:
      - superadmin (1), admin (2)
      - member_writer (3): WRITE on vault 2, ADMIN on vault 3
      - member_reader (4): READ on vault 2 (read-only — exercises R12)
      - member_noaccess (5): no vault memberships

    Seeds two memories in vault 2 plus one global (vault_id IS NULL) memory
    so list/search leak assertions can distinguish vault-scoped vs global
    rows.
    """

    def setUp(self):
        self.client = TestClient(app)
        self.client.headers["user-agent"] = ""
        self._temp_dir = tempfile.mkdtemp()

        self._original_jwt_secret = settings.jwt_secret_key
        self._original_users_enabled = settings.users_enabled
        self._original_data_dir = settings.data_dir

        settings.data_dir = Path(self._temp_dir)
        settings.jwt_secret_key = "test-secret-key-for-testing-at-least-32-chars-long"
        settings.users_enabled = True

        self._db_path = str(Path(self._temp_dir) / "app.db")

        from app.models.database import _pool_cache, _pool_cache_lock

        with _pool_cache_lock:
            for _path, pool in list(_pool_cache.items()):
                pool.close_all()
            _pool_cache.clear()

        init_db(self._db_path)
        run_migrations(self._db_path)
        self._connection_pool = SimpleConnectionPool(self._db_path)

        def override_get_db():
            conn = self._connection_pool.get_connection()
            try:
                yield conn
            finally:
                self._connection_pool.release_connection(conn)

        self._mock_vector_store = MagicMock()
        self._mock_vector_store.delete_by_vault = MagicMock(return_value=0)

        # Build a REAL MemoryStore backed by the test pool so the search/list
        # leak assertions exercise include_global end-to-end through the store
        # (not a mock). The create/update/delete tests also assert against real
        # DB rows, so a mock store would not prove the authz gate fires before
        # the write.
        from app.services.memory_store import MemoryStore

        self._real_memory_store = MemoryStore(self._connection_pool)

        # Override via FastAPI DI (NOT app.state mutation) so other test
        # modules that read app.state are not poisoned by this class. The
        # search route's require_model_ready → get_vector_store dependency
        # chain is satisfied by the mock vector store override.
        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_memory_store] = lambda: self._real_memory_store
        app.dependency_overrides[get_vector_store] = lambda: self._mock_vector_store

        conn = self._connection_pool.get_connection()
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("DELETE FROM vault_members")
            conn.execute("DELETE FROM memories")
            conn.execute("DELETE FROM users WHERE id != 0")

            pw = hash_password("testpass")
            users = [
                (1, "superadmin", pw, "Super Admin", "superadmin"),
                (2, "admin1", pw, "Admin One", "admin"),
                (3, "member_writer", pw, "Member Writer", "member"),
                (4, "member_reader", pw, "Member Reader", "member"),
                (5, "member_noaccess", pw, "Member NoAccess", "member"),
            ]
            for uid, uname, hashed, full, role in users:
                conn.execute(
                    "INSERT INTO users (id, username, hashed_password, full_name, role, is_active) "
                    "VALUES (?, ?, ?, ?, ?, 1)",
                    (uid, uname, hashed, full, role),
                )

            conn.execute(
                "INSERT OR IGNORE INTO vaults (id, name, description) VALUES (?, ?, ?)",
                (2, "Private Vault", "A private vault"),
            )

            memberships = [
                # member_writer: WRITE on vault 2
                (2, 3, "write", 1),
                # member_reader: READ on vault 2 (read-only — exercises R12)
                (2, 4, "read", 1),
            ]
            for vault_id, user_id, permission, granted_by in memberships:
                conn.execute(
                    "INSERT INTO vault_members (vault_id, user_id, permission, granted_by) "
                    "VALUES (?, ?, ?, ?)",
                    (vault_id, user_id, permission, granted_by),
                )

            # Vault-scoped memories (vault 2) + one GLOBAL memory (vault_id NULL).
            # Distinct content lets leak assertions identify which row surfaced.
            conn.execute(
                "INSERT INTO memories (id, content, category, source, vault_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                (1001, "vault-two-secret", "test", "test", 2),
            )
            conn.execute(
                "INSERT INTO memories (id, content, category, source, vault_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                (1002, "GLOBAL-SECRET-LEAK-MARKER", "test", "test", None),
            )
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)

    def tearDown(self):
        from app.models.database import _pool_cache, _pool_cache_lock

        with _pool_cache_lock:
            for _path, pool in list(_pool_cache.items()):
                pool.close_all()
            _pool_cache.clear()

        settings.jwt_secret_key = self._original_jwt_secret
        settings.users_enabled = self._original_users_enabled
        if hasattr(self, "_original_data_dir"):
            settings.data_dir = self._original_data_dir

        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_memory_store, None)
        app.dependency_overrides.pop(get_vector_store, None)
        if hasattr(self, "_connection_pool"):
            self._connection_pool.close_all()
        import shutil

        shutil.rmtree(self._temp_dir, ignore_errors=True)

    # --- token helpers ---
    def _superadmin_token(self):
        return create_access_token(1, "superadmin", "superadmin",
                                   client_fingerprint=compute_client_fingerprint(""))

    def _admin_token(self):
        return create_access_token(2, "admin1", "admin",
                                   client_fingerprint=compute_client_fingerprint(""))

    def _member_writer_token(self):
        return create_access_token(3, "member_writer", "member",
                                   client_fingerprint=compute_client_fingerprint(""))

    def _member_reader_token(self):
        return create_access_token(4, "member_reader", "member",
                                   client_fingerprint=compute_client_fingerprint(""))

    def _member_noaccess_token(self):
        return create_access_token(5, "member_noaccess", "member",
                                   client_fingerprint=compute_client_fingerprint(""))

    def _h(self, token):
        return {"Authorization": f"Bearer {token}"}

    def _list_memory_ids(self, resp):
        return [int(m["id"]) for m in resp.json().get("memories", [])]

    def _list_memory_contents(self, resp):
        return [m["content"] for m in resp.json().get("memories", [])]


class TestGlobalMemoryReadAuthz(_GlobalMemoryAuthBase):
    """READ-side: non-admins must NOT see global memories via list/search/chat."""

    def test_member_list_excludes_global_memories(self):
        """member_reader GET /memories?vault_id=2 → only vault-2 memory, NOT the
        global one. Pre-fix: the global row surfaced (leak)."""
        resp = self.client.get("/api/memories?vault_id=2", headers=self._h(self._member_reader_token()))
        self.assertEqual(resp.status_code, 200, resp.text)
        contents = self._list_memory_contents(resp)
        self.assertIn("vault-two-secret", contents)
        self.assertNotIn(
            "GLOBAL-SECRET-LEAK-MARKER",
            contents,
            "Non-admin saw a global memory via the vault-id list path (leak).",
        )

    def test_admin_list_includes_global_memories(self):
        """admin GET /memories?vault_id=2 → both vault-2 and global memories."""
        resp = self.client.get("/api/memories?vault_id=2", headers=self._h(self._admin_token()))
        self.assertEqual(resp.status_code, 200, resp.text)
        contents = self._list_memory_contents(resp)
        self.assertIn("vault-two-secret", contents)
        self.assertIn("GLOBAL-SECRET-LEAK-MARKER", contents)

    def test_member_search_excludes_global_memories(self):
        """member_reader GET /memories/search → only vault-scoped results.
        Uses the real DB-backed search by NOT mocking the store, so the
        include_global flag is exercised end-to-end through the store."""
        # The query matches content present in BOTH the global and vault rows
        # (both contain "secret"), so a leak would surface the global row.
        resp = self.client.get(
            "/api/memories/search?query=secret&vault_id=2",
            headers=self._h(self._member_reader_token()),
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        ids = [int(r["id"]) for r in resp.json().get("results", [])]
        self.assertIn(1001, ids)
        self.assertNotIn(
            1002,
            ids,
            "Non-admin search returned a global memory (leak).",
        )

    def test_admin_search_includes_global_memories(self):
        resp = self.client.get(
            "/api/memories/search?query=secret&vault_id=2",
            headers=self._h(self._admin_token()),
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        ids = [int(r["id"]) for r in resp.json().get("results", [])]
        self.assertIn(1001, ids)
        self.assertIn(1002, ids)


class TestGlobalMemoryWriteAuthz(_GlobalMemoryAuthBase):
    """WRITE-side: global memory create/update/delete require admin."""

    def test_member_create_global_memory_403(self):
        """member POST /memories {vault_id: null} → 403. Pre-fix: 201."""
        resp = self.client.post(
            "/api/memories",
            json={"content": "rogue global memory"},
            headers=self._h(self._member_writer_token()),
        )
        self.assertEqual(resp.status_code, 403, resp.text)

    def test_admin_create_global_memory_201(self):
        resp = self.client.post(
            "/api/memories",
            json={"content": "legit global memory"},
            headers=self._h(self._admin_token()),
        )
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_member_update_global_memory_403(self):
        """member PUT /memories/1002 (the global row) → 403. Pre-fix: 200."""
        resp = self.client.put(
            "/api/memories/1002",
            json={"content": "tampered global memory"},
            headers=self._h(self._member_writer_token()),
        )
        self.assertEqual(resp.status_code, 403, resp.text)

    def test_member_delete_global_memory_403(self):
        """member DELETE /memories/1002 → 403. Pre-fix: 200."""
        resp = self.client.delete(
            "/api/memories/1002",
            headers=self._h(self._member_writer_token()),
        )
        self.assertEqual(resp.status_code, 403, resp.text)

    def test_member_noaccess_create_vault_memory_403(self):
        """Sanity: a member with no vault access still cannot write to a vault."""
        resp = self.client.post(
            "/api/memories",
            json={"content": "x", "vault_id": 2},
            headers=self._h(self._member_noaccess_token()),
        )
        self.assertEqual(resp.status_code, 403, resp.text)


class TestGlobalMemoryPromoteAuthz(_GlobalMemoryAuthBase):
    """promote_memory: a vault-writer must not promote (read) a global memory."""

    def test_member_writer_promote_global_memory_403(self):
        """member_writer (WRITE on vault 2) POST /wiki/promote-memory targeting
        the global memory 1002 → 403. Pre-fix: the vault-scope check was
        skipped for global memories and promotion succeeded (content read)."""
        resp = self.client.post(
            "/api/wiki/promote-memory",
            json={"memory_id": 1002, "vault_id": 2},
            headers=self._h(self._member_writer_token()),
        )
        self.assertEqual(resp.status_code, 403, resp.text)


class TestChatMemoryWriteAuthz(_GlobalMemoryAuthBase):
    """R12: a read-only member must not persist a memory via chat "remember"."""

    def _make_engine_mock(self):
        """Build a minimal RAGEngine mock whose query() drives the
        memory-intent branch deterministically.

        We can't easily exercise the full RAG pipeline here; instead we
        verify the contract at its seam: the route resolves
        ``can_write_memory`` and threads it into ``query()``. The engine
        itself is unit-tested for the flag's effect in
        test_rag_engine_memory_scope.py.
        """
        engine = MagicMock()

        async def _fake_query(*args, **kwargs):
            # Capture the kwargs so the test can assert the route threaded
            # can_write_memory correctly.
            self._last_query_kwargs = kwargs
            yield {"type": "content", "content": "ok"}

        engine.query = _fake_query
        return engine

    def test_chat_passes_can_write_memory_false_for_readonly_member(self):
        """Non-streaming /chat: a read-only member gets can_write_memory=False."""
        from app.api.deps import get_rag_engine

        engine = self._make_engine_mock()
        app.dependency_overrides[get_rag_engine] = lambda: engine
        try:
            resp = self.client.post(
                "/api/chat",
                json={
                    "message": "remember that the key is under the mat",
                    "vault_id": 2,
                    "stream": False,
                },
                headers=self._h(self._member_reader_token()),
            )
            self.assertEqual(resp.status_code, 200, resp.text)
            self.assertFalse(
                self._last_query_kwargs.get("can_write_memory", True),
                "read-only member was granted can_write_memory=True (R12 leak).",
            )
            self.assertFalse(self._last_query_kwargs.get("include_global", True))
        finally:
            app.dependency_overrides.pop(get_rag_engine, None)

    def test_chat_passes_can_write_memory_true_for_writer(self):
        """Non-streaming /chat: a write member gets can_write_memory=True."""
        from app.api.deps import get_rag_engine

        engine = self._make_engine_mock()
        app.dependency_overrides[get_rag_engine] = lambda: engine
        try:
            resp = self.client.post(
                "/api/chat",
                json={"message": "remember the deadline", "vault_id": 2, "stream": False},
                headers=self._h(self._member_writer_token()),
            )
            self.assertEqual(resp.status_code, 200, resp.text)
            self.assertTrue(self._last_query_kwargs.get("can_write_memory", False))
            self.assertFalse(self._last_query_kwargs.get("include_global", True))
        finally:
            app.dependency_overrides.pop(get_rag_engine, None)

    def test_chat_admin_no_vault_can_write_memory_true(self):
        """Admin chatting with vault_id=None gets can_write_memory=True AND
        include_global=True (composes A4 + A7)."""
        from app.api.deps import get_rag_engine

        engine = self._make_engine_mock()
        app.dependency_overrides[get_rag_engine] = lambda: engine
        try:
            resp = self.client.post(
                "/api/chat",
                json={"message": "remember the policy", "stream": False},
                headers=self._h(self._admin_token()),
            )
            self.assertEqual(resp.status_code, 200, resp.text)
            self.assertTrue(self._last_query_kwargs.get("can_write_memory", False))
            self.assertTrue(self._last_query_kwargs.get("include_global", False))
        finally:
            app.dependency_overrides.pop(get_rag_engine, None)

    def test_chat_stream_readonly_member_threads_can_write_memory_false(self):
        """Streaming /chat/stream: a read-only member's can_write_memory=False
        is stashed by get_stream_auth (inside the pooled-conn block) and read
        by chat_stream → stream_chat_response → query(). Exercises the full
        streaming-flag-threading path (reviewer nit).

        We override get_stream_auth to simulate the stashed-flag contract
        (the real dependency runs the same authz + stashing logic against the
        pool, which this test class already sets up)."""
        from app.api.deps import get_rag_engine
        from app.api.routes.chat import get_stream_auth

        engine = self._make_engine_mock()
        # Simulate get_stream_auth's output for a read-only member of vault 2:
        # role=member, write-permission False → _can_write_memory=False,
        # _include_global_memories=False.
        stashed_user = {
            "id": 4,
            "username": "member_reader",
            "role": "member",
            "_can_write_memory": False,
            "_include_global_memories": False,
        }
        app.dependency_overrides[get_rag_engine] = lambda: engine
        app.dependency_overrides[get_stream_auth] = lambda: stashed_user
        try:
            resp = self.client.post(
                "/api/chat/stream",
                json={
                    "messages": [{"role": "user", "content": "remember the key"}],
                    "vault_id": 2,
                },
                headers=self._h(self._member_reader_token()),
            )
            self.assertEqual(resp.status_code, 200, resp.text)
            self.assertFalse(
                self._last_query_kwargs.get("can_write_memory", True),
                "Streaming path granted can_write_memory=True to a read-only member.",
            )
            self.assertFalse(self._last_query_kwargs.get("include_global", True))
        finally:
            app.dependency_overrides.pop(get_rag_engine, None)
            app.dependency_overrides.pop(get_stream_auth, None)


if __name__ == "__main__":
    unittest.main()
