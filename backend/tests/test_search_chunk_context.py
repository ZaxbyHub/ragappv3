"""Legacy-UID fallback on GET /search/chunks/{id}/context (PR #523, PRR-010).

The context endpoint must try the exact chunk id first and, when nothing
matches, retry once with the reupload hash segment stripped
(``_strip_reupload_hash``) so ids captured before a reprocess still resolve
their re-uploaded counterparts instead of 404ing. Exact-match precedence is
preserved and hash-less ids never trigger a second lookup.
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


class _RecordingVectorStore:
    """Fake vector store that records every get_chunks_by_uid uid list and
    answers by exact id match, mirroring the real store's IN-clause lookup."""

    def __init__(self):
        self.chunk_records = []
        self.uid_requests = []

    async def get_chunks_by_uid(self, chunk_uids):
        self.uid_requests.append(list(chunk_uids))
        wanted = set(chunk_uids)
        return [c for c in self.chunk_records if c.get("id") in wanted]


class TestChunkContextLegacyUidFallback(unittest.TestCase):
    """GET /search/chunks/{id}/context retries with the hash-stripped id."""

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

        self.fake_vector_store = _RecordingVectorStore()
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

    def _seed_file(self, file_id):
        conn = self.test_pool.get_connection()
        try:
            conn.execute(
                "INSERT INTO files (id, file_name, file_path, file_size, status, chunk_count, vault_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (file_id, "legacy.md", "/uploads/legacy.md", 100, "indexed", 1, self.vault_id),
            )
            conn.commit()
        finally:
            self.test_pool.release_connection(conn)

    def _get_context(self, chunk_id):
        return self.client.get(
            f"/api/search/chunks/{chunk_id}/context",
            headers={"Authorization": f"Bearer {self._token()}"},
        )

    def test_legacy_uid_resolves_via_stripped_retry(self):
        """A hash-carrying id whose chunk is stored hash-stripped must
        resolve through the normalized retry instead of 404ing."""
        self._seed_file(42)
        # Store the re-uploaded chunk under its legacy-format (hash-stripped)
        # uid; the caller holds the reupload-safe id captured earlier.
        self.fake_vector_store.chunk_records = [
            {
                "id": "42_default_0",
                "text": "reuploaded chunk text",
                "file_id": "42",
                "vault_id": str(self.vault_id),
                "chunk_index": 0,
                "metadata": json.dumps({"raw_text": "reuploaded chunk text"}),
            }
        ]

        resp = self._get_context("42_ab12cd34_default_0")

        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertEqual(data["id"], "42_default_0")
        self.assertEqual(data["matched_text"], "reuploaded chunk text")
        # Exact id was tried first, then the hash-stripped fallback.
        self.assertEqual(
            self.fake_vector_store.uid_requests,
            [["42_ab12cd34_default_0"], ["42_default_0"]],
        )

    def test_exact_match_wins_without_fallback_retry(self):
        """When the exact id resolves, no second normalized lookup happens."""
        self._seed_file(42)
        self.fake_vector_store.chunk_records = [
            {
                "id": "42_ab12cd34_default_0",
                "text": "current chunk text",
                "file_id": "42",
                "vault_id": str(self.vault_id),
                "chunk_index": 0,
                "metadata": json.dumps({"raw_text": "current chunk text"}),
            }
        ]

        resp = self._get_context("42_ab12cd34_default_0")

        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(
            self.fake_vector_store.uid_requests, [["42_ab12cd34_default_0"]]
        )

    def test_hashless_id_gets_single_lookup(self):
        """Ids without a hash segment are unchanged by the strip, so the
        endpoint must not issue a redundant second lookup for them."""
        self._seed_file(42)
        self.fake_vector_store.chunk_records = [
            {
                "id": "42_0",
                "text": "hashless chunk text",
                "file_id": "42",
                "vault_id": str(self.vault_id),
                "chunk_index": 0,
                "metadata": json.dumps({"raw_text": "hashless chunk text"}),
            }
        ]

        resp = self._get_context("42_0")

        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(self.fake_vector_store.uid_requests, [["42_0"]])


if __name__ == "__main__":
    unittest.main()
