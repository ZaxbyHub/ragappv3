"""C13 acceptance check — chunk-context store failure must surface as 503.

AC1: WHEN ``GET /api/search/chunks/{id}/context`` is served by a real
``VectorStore`` whose chunk lookup raises ``OSError`` THE SYSTEM SHALL respond
503 with a fixed, generic detail that contains no exception text.

At base the store-level ``OSError`` is swallowed by
``VectorStore.get_chunks_by_uid`` (vector_store.py catches
``OSError/RuntimeError/ValueError`` and returns ``[]``), so the route
misreports the outage as a 404 "Chunk not found" — the test is RED at base
(master), GREEN after the fix.

Setup mirrors backend/tests/test_chunk_context_window.py::TestChunkContextWindow
(TestClient / dependency overrides / member JWT token), except the
``get_vector_store`` override returns a REAL ``VectorStore`` whose ``table``
is a fake whose ``query()`` raises ``OSError``.
"""

import os
import shutil
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Stub missing optional dependencies (defensive; mirrors the sibling suite).
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
from app.services.vector_store import VectorStore  # noqa: E402


class _ExplodingQueryTable:
    """Fake LanceDB table whose ``query()`` raises a store-level OSError.

    ``VectorStore.get_chunks_by_uid`` builds its read through
    ``await self.table.query().where(query).to_list()`` — raising here makes
    the chunk lookup itself fail with the injected store error, whose text
    carries a server-absolute path that must never reach the wire.
    """

    def query(self):
        raise OSError("disk I/O error /data/secret")


class TestStoreFailureIsServiceUnavailable(unittest.TestCase):
    """AC1: a failing VectorStore read is 503, never a masked 404."""

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

        # REAL VectorStore instance; only its LanceDB table is faked so the
        # failure comes from the store read path itself, not a stubbed method.
        self.vector_store = VectorStore(db_path=Path(self.temp_dir) / "lance")
        self.vector_store.table = _ExplodingQueryTable()

        main_app.dependency_overrides[get_db] = get_test_db
        main_app.dependency_overrides[get_vector_store] = lambda: self.vector_store
        self.client = TestClient(main_app)
        self.app = main_app

    def tearDown(self):
        settings.jwt_secret_key = self._orig_jwt
        settings.users_enabled = self._orig_users
        self.app.dependency_overrides.clear()
        self.test_pool.close_all()
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
            self.vault_id = conn.execute(
                "SELECT id FROM vaults WHERE name='Accessible'"
            ).fetchone()[0]
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

    def test_store_failure_is_503_not_404(self):
        """AC1: WHEN GET /api/search/chunks/{id}/context is served by a real
        VectorStore whose chunk lookup raises OSError THE SYSTEM SHALL respond
        503 with a fixed, generic detail that contains no exception text.
        RED at base (master), GREEN after the fix."""
        resp = self.client.get(
            "/api/search/chunks/70_0/context",
            headers={"Authorization": f"Bearer {self._token()}"},
        )
        # Status first: at base the OSError is swallowed and misreported as
        # 404 "Chunk not found"; the contract is a 503 service-unavailable.
        self.assertEqual(resp.status_code, 503, resp.text)

        # The detail must be generic: no server path / exception text...
        self.assertNotIn("/data/secret", resp.text)
        self.assertNotIn("disk I/O", resp.text)
        # ...and not the masked 404 wording (the outage is not a missing chunk).
        detail = resp.json().get("detail", "")
        self.assertTrue(detail, "503 must carry a non-empty generic detail")
        self.assertNotIn("Chunk not found", detail)


if __name__ == "__main__":
    unittest.main()
