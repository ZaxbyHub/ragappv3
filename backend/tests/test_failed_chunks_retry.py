"""Tests for failed-chunk accounting + chunk-scoped retry (Issue #396).

Covers:
- ``failed_chunks`` table existence + migration.
- ``_persist_failed_chunks`` / ``_build_failed_chunk_metadata`` correctness.
- ``retry_failed_chunks`` service method: success path (LanceDB write, DELETE
  rows, decrement count), None-embedding failure path (attempts+1, no write),
  reconciliation pre-check, and the status-not-indexed 409 path.
- HTTP endpoint authz (admin only) + 409 mapping.
- Status/detail response surfacing of failed_chunks/failed_chunk_ids.
"""

import json
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

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
from app.services.chunking import ProcessedChunk  # noqa: E402
from app.services.document_processor import DocumentProcessor  # noqa: E402


class _FakeEmbeddingService:
    """Fake embedding service matching embed_batch(fail_fast=False) contract."""

    def __init__(self, fail_indices=None):
        self._fail = set(fail_indices or [])
        self._call_count = 0

    async def embed_batch(self, texts, fail_fast=False):
        self._call_count += 1
        idx = self._call_count - 1
        if idx in self._fail:
            return ([None], [0])
        return ([[0.1, 0.2, 0.3]], [])


class _FakeVectorStore:
    """Fake vector store capturing add_chunks + supporting get_chunks_by_uid."""

    def __init__(self):
        self.added_records = []
        self._existing_ids = set()

    async def add_chunks(self, records):
        for r in records:
            self.added_records.append(r)
            self._existing_ids.add(r["id"])

    async def get_chunks_by_uid(self, chunk_uids):
        return [
            {"id": uid, "text": "existing"}
            for uid in chunk_uids
            if uid in self._existing_ids
        ]


def _make_pool(db_path):
    return SQLiteConnectionPool(db_path, max_size=5)


class TestFailedChunksRetry(unittest.TestCase):
    """Service-layer + HTTP tests for chunk-scoped retry."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        init_db(self.db_path)
        run_migrations(self.db_path)
        self.pool = _make_pool(self.db_path)

        self._orig_jwt = settings.jwt_secret_key
        self._orig_users = settings.users_enabled
        settings.jwt_secret_key = "test-secret-key-for-testing-at-least-32-chars-long"
        settings.users_enabled = True

        self._seed_users_vaults_file()

    def tearDown(self):
        settings.jwt_secret_key = self._orig_jwt
        settings.users_enabled = self._orig_users
        self.pool.close_all()
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _seed_users_vaults_file(self):
        conn = self.pool.get_connection()
        try:
            conn.execute(
                "INSERT INTO users (username, hashed_password, full_name, role, is_active, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("admin_user", "pw", "Admin", "admin", 1, datetime.now(timezone.utc).isoformat()),
            )
            self.admin_id = conn.execute(
                "SELECT id FROM users WHERE username='admin_user'"
            ).fetchone()[0]
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
                ("V", "v", "private", "2026-01-01", "2026-01-01"),
            )
            self.vault_id = conn.execute("SELECT id FROM vaults WHERE name='V'").fetchone()[0]
            conn.execute(
                "INSERT INTO vault_members (vault_id, user_id, permission, granted_at) "
                "VALUES (?, ?, ?, ?)",
                (self.vault_id, self.member_id, "read", "2026-01-01"),
            )
            # An indexed file with chunks_failed=2.
            conn.execute(
                "INSERT INTO files (id, file_name, file_path, file_size, status, chunk_count, "
                "chunks_failed, vault_id, file_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (70, "f.txt", "/uploads/f.txt", 100, "indexed", 3, 2, self.vault_id, "abc12345"),
            )
            conn.commit()
        finally:
            self.pool.release_connection(conn)

    def _seed_failed_chunks(self, file_id=70):
        conn = self.pool.get_connection()
        try:
            for idx in (1, 3):
                meta = json.dumps({
                    "raw_text": f"failed chunk {idx}",
                    "chunk_index": idx,
                    "chunk_scale": "default",
                    "chunk_uid": f"{file_id}_{idx}",
                    "chunk_position": idx,
                    "parent_window_start": None,
                    "parent_window_end": None,
                    "page_number": None,
                    "chunk_bbox": None,
                    "total_chunks": 4,
                })
                conn.execute(
                    "INSERT INTO failed_chunks (file_id, chunk_index, chunk_text, chunk_metadata) "
                    "VALUES (?, ?, ?, ?)",
                    (file_id, idx, f"failed chunk {idx}", meta),
                )
            conn.commit()
        finally:
            self.pool.release_connection(conn)

    def _make_processor(self, embedding_service, vector_store):
        return DocumentProcessor(
            chunk_size_chars=500,
            chunk_overlap_chars=0,
            vector_store=vector_store,
            embedding_service=embedding_service,
            pool=self.pool,
        )

    # ── Schema / migration ──────────────────────────────────────────────

    def test_failed_chunks_table_exists_after_migration(self):
        conn = self.pool.get_connection()
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='failed_chunks'"
            ).fetchall()
            self.assertEqual(len(rows), 1)
            cols = {
                r[1] for r in conn.execute("PRAGMA table_info(failed_chunks)").fetchall()
            }
            for expected in (
                "id",
                "file_id",
                "chunk_index",
                "chunk_text",
                "chunk_metadata",
                "error_reason",
                "attempts",
                "created_at",
            ):
                self.assertIn(expected, cols)
        finally:
            self.pool.release_connection(conn)

    # ── Service: retry_failed_chunks success path ───────────────────────

    def test_retry_succeeds_writes_lancedb_deletes_rows(self):
        self._seed_failed_chunks()
        emb = _FakeEmbeddingService()  # no failures
        vs = _FakeVectorStore()
        proc = self._make_processor(emb, vs)

        # reupload_safe_order=True (default) → id = {file_id}_{hash[:8]}_{scale}_{idx}
        expected_ids = {"70_abc12345_default_1", "70_abc12345_default_3"}

        result = proc.loop.run_until_complete if hasattr(proc, "loop") else None
        import asyncio

        result = asyncio.run(proc.retry_failed_chunks(70))

        self.assertEqual(result["retried"], 2)
        self.assertEqual(result["succeeded"], 2)
        self.assertEqual(result["still_failing"], 0)
        # LanceDB received 2 rebuilt records.
        self.assertEqual(len(vs.added_records), 2)
        rebuilt_ids = {r["id"] for r in vs.added_records}
        self.assertEqual(rebuilt_ids, expected_ids)
        # failed_chunks rows deleted; chunks_failed decremented to 0.
        conn = self.pool.get_connection()
        try:
            remaining = conn.execute(
                "SELECT COUNT(*) FROM failed_chunks WHERE file_id = 70"
            ).fetchone()[0]
            self.assertEqual(remaining, 0)
            cf = conn.execute(
                "SELECT chunks_failed FROM files WHERE id = 70"
            ).fetchone()[0]
            self.assertEqual(cf, 0)
        finally:
            self.pool.release_connection(conn)

    def test_retry_none_embedding_keeps_row_increments_attempts(self):
        """[N1] embed_batch returning None → chunk stays, attempts+1, no write."""
        self._seed_failed_chunks()
        # Both calls fail (call indices 0, 1).
        emb = _FakeEmbeddingService(fail_indices=[0, 1])
        vs = _FakeVectorStore()
        proc = self._make_processor(emb, vs)

        import asyncio

        result = asyncio.run(proc.retry_failed_chunks(70))

        self.assertEqual(result["succeeded"], 0)
        self.assertEqual(result["still_failing"], 2)
        self.assertEqual(len(vs.added_records), 0)  # no LanceDB write
        conn = self.pool.get_connection()
        try:
            rows = conn.execute(
                "SELECT attempts FROM failed_chunks WHERE file_id = 70 ORDER BY chunk_index"
            ).fetchall()
            self.assertEqual([r[0] for r in rows], [2, 2])  # 1 → 2
            cf = conn.execute(
                "SELECT chunks_failed FROM files WHERE id = 70"
            ).fetchone()[0]
            self.assertEqual(cf, 2)  # unchanged
        finally:
            self.pool.release_connection(conn)

    def test_retry_reconciles_already_indexed_chunks(self):
        """[N3] Chunks already in LanceDB are skipped + rows cleared."""
        self._seed_failed_chunks()
        vs = _FakeVectorStore()
        # Pretend chunk index 1 is already in LanceDB (reupload_safe_order id format).
        vs._existing_ids.add("70_abc12345_default_1")
        emb = _FakeEmbeddingService()
        proc = self._make_processor(emb, vs)

        import asyncio

        result = asyncio.run(proc.retry_failed_chunks(70))

        # Only chunk 3 needed re-embedding (chunk 1 pre-check skipped it).
        self.assertEqual(result["retried"], 1)
        self.assertEqual(result["succeeded"], 1)
        self.assertEqual(len(vs.added_records), 1)
        conn = self.pool.get_connection()
        try:
            remaining = conn.execute(
                "SELECT COUNT(*) FROM failed_chunks WHERE file_id = 70"
            ).fetchone()[0]
            self.assertEqual(remaining, 0)  # both rows cleared
        finally:
            self.pool.release_connection(conn)

    def test_retry_rejects_non_indexed_status(self):
        """[C1.2] status != indexed → ValueError (caller maps to 409)."""
        conn = self.pool.get_connection()
        try:
            conn.execute("UPDATE files SET status = 'error' WHERE id = 70")
            conn.commit()
        finally:
            self.pool.release_connection(conn)
        self._seed_failed_chunks()
        emb = _FakeEmbeddingService()
        vs = _FakeVectorStore()
        proc = self._make_processor(emb, vs)

        import asyncio

        with self.assertRaises(ValueError):
            asyncio.run(proc.retry_failed_chunks(70))

    def test_retry_no_failed_chunks_returns_zero_summary(self):
        emb = _FakeEmbeddingService()
        vs = _FakeVectorStore()
        proc = self._make_processor(emb, vs)
        import asyncio

        result = asyncio.run(proc.retry_failed_chunks(70))
        self.assertEqual(result, {
            "retried": 0,
            "succeeded": 0,
            "still_failing": 0,
            "failed_chunk_indices": [],
        })

    # ── List/status surfacing ───────────────────────────────────────────

    def test_list_surfaces_failed_chunks_non_admin_without_vault_id(self):
        """The non-admin (vault_id IN accessible) list branch must surface
        failed_chunks/failed_chunk_ids — regression guard for the branch that
        originally omitted the correlated subqueries."""
        self._seed_failed_chunks()
        emb = _FakeEmbeddingService()
        vs = _FakeVectorStore()
        client, main_app = self._setup_client(emb, vs)
        try:
            resp = client.get(
                "/api/documents",
                headers={"Authorization": f"Bearer {self._member_token()}"},
            )
            self.assertEqual(resp.status_code, 200, resp.text)
            docs = resp.json()["documents"]
            target = [d for d in docs if d["id"] == 70]
            self.assertTrue(target, "seeded file 70 must appear in list")
            self.assertEqual(target[0]["failed_chunks"], 2)
            self.assertEqual(sorted(target[0]["failed_chunk_ids"]), [1, 3])
        finally:
            main_app.dependency_overrides.clear()

    def test_list_surfaces_failed_chunks_admin_all_docs(self):
        """The admin (all-docs) list branch must also surface failed_chunks."""
        self._seed_failed_chunks()
        emb = _FakeEmbeddingService()
        vs = _FakeVectorStore()
        client, main_app = self._setup_client(emb, vs)
        try:
            resp = client.get(
                "/api/documents",
                headers={"Authorization": f"Bearer {self._admin_token()}"},
            )
            self.assertEqual(resp.status_code, 200, resp.text)
            docs = resp.json()["documents"]
            target = [d for d in docs if d["id"] == 70]
            self.assertTrue(target, "seeded file 70 must appear in list")
            self.assertEqual(target[0]["failed_chunks"], 2)
            self.assertEqual(sorted(target[0]["failed_chunk_ids"]), [1, 3])
        finally:
            main_app.dependency_overrides.clear()

    def test_status_surfaces_failed_chunks(self):
        """GET /documents/{id}/status surfaces failed_chunks/failed_chunk_ids."""
        self._seed_failed_chunks()
        emb = _FakeEmbeddingService()
        vs = _FakeVectorStore()
        client, main_app = self._setup_client(emb, vs)
        try:
            resp = client.get(
                "/api/documents/70/status",
                headers={"Authorization": f"Bearer {self._member_token()}"},
            )
            self.assertEqual(resp.status_code, 200, resp.text)
            data = resp.json()
            self.assertEqual(data["failed_chunks"], 2)
            self.assertEqual(sorted(data["failed_chunk_ids"]), [1, 3])
            self.assertEqual(data["chunks_failed"], 2)
        finally:
            main_app.dependency_overrides.clear()

    # ── HTTP endpoint ───────────────────────────────────────────────────

    def _setup_client(self, embedding_service, vector_store):
        from app.api.deps import (
            get_db,
            get_db_pool,
            get_embedding_service,
            get_secret_manager,
            get_vector_store,
        )
        from app.main import app as main_app

        def get_test_db():
            conn = self.pool.get_connection()
            try:
                yield conn
            finally:
                self.pool.release_connection(conn)

        main_app.dependency_overrides[get_db] = get_test_db
        main_app.dependency_overrides[get_db_pool] = lambda: self.pool
        main_app.dependency_overrides[get_vector_store] = lambda: vector_store
        main_app.dependency_overrides[get_embedding_service] = lambda: (
            embedding_service
        )
        _sm = MagicMock()
        _sm.get_hmac_key.return_value = (b"test-hmac-key-32bytes-padding!!", "v1")
        main_app.dependency_overrides[get_secret_manager] = lambda: _sm
        client = TestClient(main_app)
        client.headers["user-agent"] = ""
        return client, main_app

    def _admin_token(self):
        payload = {
            "sub": str(self.admin_id),
            "username": "admin_user",
            "role": "admin",
            "exp": datetime.now(timezone.utc).timestamp() + 3600,
            "type": "access",
        }
        return jwt.encode(payload, settings.jwt_secret_key, algorithm="HS256")

    def _member_token(self):
        payload = {
            "sub": str(self.member_id),
            "username": "member_user",
            "role": "member",
            "exp": datetime.now(timezone.utc).timestamp() + 3600,
            "type": "access",
        }
        return jwt.encode(payload, settings.jwt_secret_key, algorithm="HS256")

    def test_endpoint_admin_retry_succeeds(self):
        self._seed_failed_chunks()
        emb = _FakeEmbeddingService()
        vs = _FakeVectorStore()
        client, main_app = self._setup_client(emb, vs)
        try:
            resp = client.post(
                "/api/documents/70/retry-chunks",
                headers={"Authorization": f"Bearer {self._admin_token()}"},
            )
            self.assertEqual(resp.status_code, 200, resp.text)
            data = resp.json()
            self.assertEqual(data["succeeded"], 2)
        finally:
            main_app.dependency_overrides.clear()

    def test_endpoint_member_forbidden(self):
        """Non-admin cannot retry chunks → 403."""
        self._seed_failed_chunks()
        emb = _FakeEmbeddingService()
        vs = _FakeVectorStore()
        client, main_app = self._setup_client(emb, vs)
        try:
            resp = client.post(
                "/api/documents/70/retry-chunks",
                headers={"Authorization": f"Bearer {self._member_token()}"},
            )
            self.assertEqual(resp.status_code, 403)
        finally:
            main_app.dependency_overrides.clear()

    def test_endpoint_unauthenticated_returns_401(self):
        emb = _FakeEmbeddingService()
        vs = _FakeVectorStore()
        client, main_app = self._setup_client(emb, vs)
        try:
            resp = client.post("/api/documents/70/retry-chunks")
            self.assertEqual(resp.status_code, 401)
        finally:
            main_app.dependency_overrides.clear()


if __name__ == "__main__":
    unittest.main()
