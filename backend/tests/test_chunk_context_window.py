"""Tests for configurable context window on chunk context endpoint (Issue #396).

Extends the existing /search/chunks/{id}/context tests to cover the new
``context_before``/``context_after`` query params and the structured
``before``/``matched_text``/``after`` response fields, plus backward
compatibility (defaults yield empty lists, context_text still present).
"""

import json
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Stub missing optional dependencies.
for _mod in ("lancedb", "pyarrow"):
    try:
        __import__(_mod)
    except ImportError:
        sys.modules[_mod] = types.ModuleType(_mod)

try:
    from unstructured.partition.auto import partition  # noqa: F401
except ImportError:
    _unstructured = types.ModuleType("unstructured")
    _unstructured.__path__ = []
    _unstructured.partition = types.ModuleType("unstructured.partition")
    _unstructured.partition.__path__ = []
    _unstructured.partition.auto = types.ModuleType("unstructured.partition.auto")
    _unstructured.partition.auto.partition = lambda *a, **k: []
    _unstructured.chunking = types.ModuleType("unstructured.chunking")
    _unstructured.chunking.__path__ = []
    _unstructured.chunking.title = types.ModuleType("unstructured.chunking.title")
    _unstructured.chunking.title.chunk_by_title = lambda *a, **k: []
    _unstructured.documents = types.ModuleType("unstructured.documents")
    _unstructured.documents.__path__ = []
    _unstructured.documents.elements = types.ModuleType(
        "unstructured.documents.elements"
    )
    _unstructured.documents.elements.Element = type("Element", (), {})
    for _name, _sub in (
        ("unstructured", _unstructured),
        ("unstructured.partition", _unstructured.partition),
        ("unstructured.partition.auto", _unstructured.partition.auto),
        ("unstructured.chunking", _unstructured.chunking),
        ("unstructured.chunking.title", _unstructured.chunking.title),
        ("unstructured.documents", _unstructured.documents),
        ("unstructured.documents.elements", _unstructured.documents.elements),
    ):
        sys.modules[_name] = _sub

import jwt  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import settings  # noqa: E402
from app.models.database import (  # noqa: E402
    SQLiteConnectionPool,
    init_db,
    run_migrations,
)


class _RangeAwareVectorStore:
    """Fake vector store supporting get_chunks_by_uid + get_chunks_by_file_range."""

    def __init__(self):
        self.chunk_records = []

    async def get_chunks_by_uid(self, chunk_uids):
        wanted = set(chunk_uids)
        return [c for c in self.chunk_records if c.get("id") in wanted]

    async def get_chunks_by_file_range(
        self, file_id, chunk_index, before, after
    ):
        lo = chunk_index - max(0, before)
        hi = chunk_index + max(0, after)
        out = [
            c
            for c in self.chunk_records
            if c.get("file_id") == file_id
            and lo <= c.get("chunk_index", -1) <= hi
        ]
        out.sort(key=lambda r: r.get("chunk_index", 0))
        return out


class TestChunkContextWindow(unittest.TestCase):
    """GET /search/chunks/{id}/context with context_before/context_after."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        init_db(self.db_path)
        run_migrations(self.db_path)

        self._orig_jwt = settings.jwt_secret_key
        self._orig_users = settings.users_enabled
        settings.jwt_secret_key = "test-secret-key-for-testing-at-least-32-chars-long"
        settings.users_enabled = True

        self.test_pool = SQLiteConnectionPool(self.db_path, max_size=5)
        self._seed_users_and_vaults()

        from app.api.deps import get_db, get_vector_store
        from app.main import app as main_app

        def get_test_db():
            conn = self.test_pool.get_connection()
            try:
                yield conn
            finally:
                self.test_pool.release_connection(conn)

        self.fake_vector_store = _RangeAwareVectorStore()
        main_app.dependency_overrides[get_db] = get_test_db
        main_app.dependency_overrides[get_vector_store] = lambda: (
            self.fake_vector_store
        )
        self.client = TestClient(main_app)
        self.app = main_app

    def tearDown(self):
        settings.jwt_secret_key = self._orig_jwt
        settings.users_enabled = self._orig_users
        self.app.dependency_overrides.clear()
        self.test_pool.close_all()
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _seed_users_and_vaults(self):
        conn = self.test_pool.get_connection()
        try:
            conn.execute(
                "INSERT INTO users (username, hashed_password, full_name, role, is_active, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("member_user", "pw", "Member", "member", 1, datetime.now(timezone.utc).isoformat()),
            )
            self.member_id = conn.execute(
                "SELECT id FROM users WHERE username='member_user'"
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO vaults (name, description, visibility, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                ("Accessible", "v", "private", "2026-01-01", "2026-01-01"),
            )
            self.vault_id = conn.execute("SELECT id FROM vaults WHERE name='Accessible'").fetchone()[0]
            conn.execute(
                "INSERT INTO vault_members (vault_id, user_id, permission, granted_at) "
                "VALUES (?, ?, ?, ?)",
                (self.vault_id, self.member_id, "read", "2026-01-01"),
            )
            conn.commit()
        finally:
            self.test_pool.release_connection(conn)

    def _token(self):
        payload = {
            "sub": str(self.member_id),
            "username": "member_user",
            "role": "member",
            "exp": datetime.now(timezone.utc).timestamp() + 3600,
            "type": "access",
        }
        return jwt.encode(payload, settings.jwt_secret_key, algorithm="HS256")

    def _seed_chunks(self):
        """Seed a file + 5 ordered chunks (indices 0-4); center on index 2."""
        conn = self.test_pool.get_connection()
        try:
            conn.execute(
                "INSERT INTO files (id, file_name, file_path, file_size, status, chunk_count, vault_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (50, "ctx.md", "/uploads/ctx.md", 100, "indexed", 5, self.vault_id),
            )
            conn.commit()
        finally:
            self.test_pool.release_connection(conn)

        self.fake_vector_store.chunk_records = [
            {
                "id": f"50_{i}",
                "text": f"chunk text {i}",
                "file_id": "50",
                "vault_id": str(self.vault_id),
                "chunk_index": i,
                "metadata": json.dumps({"raw_text": f"chunk text {i}"}),
            }
            for i in range(5)
        ]
        return "50_2"  # center chunk id

    def test_before_after_return_ordered_neighbors(self):
        center_id = self._seed_chunks()
        resp = self.client.get(
            f"/api/search/chunks/{center_id}/context?context_before=2&context_after=1",
            headers={"Authorization": f"Bearer {self._token()}"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        # before: chunks 0, 1 (in order); after: chunk 3.
        self.assertEqual(data["before"], ["chunk text 0", "chunk text 1"])
        self.assertEqual(data["after"], ["chunk text 3"])
        self.assertEqual(data["matched_text"], "chunk text 2")

    def test_defaults_backward_compatible(self):
        """With no params (default 0,0), before/after are empty; context_text present."""
        center_id = self._seed_chunks()
        resp = self.client.get(
            f"/api/search/chunks/{center_id}/context",
            headers={"Authorization": f"Bearer {self._token()}"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertEqual(data["before"], [])
        self.assertEqual(data["after"], [])
        self.assertIn("context_text", data)
        self.assertEqual(data["matched_text"], "chunk text 2")

    def test_non_contiguous_indices_returns_only_existing(self):
        """Gap in indices (e.g. a missing index 3): only existing neighbors return."""
        conn = self.test_pool.get_connection()
        try:
            conn.execute(
                "INSERT INTO files (id, file_name, file_path, file_size, status, chunk_count, vault_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (51, "gap.md", "/uploads/gap.md", 100, "indexed", 4, self.vault_id),
            )
            conn.commit()
        finally:
            self.test_pool.release_connection(conn)
        # Indices 0, 1, 2, 4 — index 3 missing (simulates a failed-chunk gap).
        self.fake_vector_store.chunk_records = [
            {
                "id": f"51_{i}",
                "text": f"g text {i}",
                "file_id": "51",
                "vault_id": str(self.vault_id),
                "chunk_index": i,
                "metadata": json.dumps({"raw_text": f"g text {i}"}),
            }
            for i in (0, 1, 2, 4)
        ]
        resp = self.client.get(
            "/api/search/chunks/51_2/context?context_before=0&context_after=2",
            headers={"Authorization": f"Bearer {self._token()}"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        # Only index 4 exists after 2 (index 3 is gone).
        self.assertEqual(data["after"], ["g text 4"])

    def test_inaccessible_vault_returns_404(self):
        """Chunk in a vault the user cannot read → 404 (not 403)."""
        conn = self.test_pool.get_connection()
        try:
            conn.execute(
                "INSERT INTO vaults (name, description, visibility, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                ("Other", "v", "private", "2026-01-01", "2026-01-01"),
            )
            other_vault = conn.execute("SELECT id FROM vaults WHERE name='Other'").fetchone()[0]
            conn.execute(
                "INSERT INTO files (id, file_name, file_path, file_size, status, chunk_count, vault_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (60, "secret.md", "/uploads/secret.md", 100, "indexed", 1, other_vault),
            )
            conn.commit()
        finally:
            self.test_pool.release_connection(conn)
        self.fake_vector_store.chunk_records = [
            {
                "id": "60_0",
                "text": "secret",
                "file_id": "60",
                "vault_id": "999",
                "chunk_index": 0,
                "metadata": "{}",
            }
        ]
        resp = self.client.get(
            "/api/search/chunks/60_0/context?context_before=1",
            headers={"Authorization": f"Bearer {self._token()}"},
        )
        self.assertEqual(resp.status_code, 404)


if __name__ == "__main__":
    unittest.main()
