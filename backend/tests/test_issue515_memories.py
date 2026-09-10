"""Issue #515 acceptance checks — memory store + memory API routes.

Each test pins REQUIRED post-fix behavior for one AC and fails on the current
tree for the stated defect reason (DISCRIMINATING), or passes now and must stay
passing (PRESERVING sibling assertions live inside the same test).

ACs covered here:
- AC18 (API-005): non-string JSON tag elements rejected before persistence.
- AC19 (MEM-003): explicit null clears tags/expiry; omission preserves.
- AC23 (MEM-001): late old-content embedding write must not overwrite.
- AC24 (OBS-004): backfill counts failures truthfully.
- AC25 (MEM-002): importance 0.0 round-trips through store read paths.
- AC27 (D2 contract): no-op memory save does not supersede unchanged claims.
- AC28 (DEEP-D-02): whitespace-only content rejected by create/update API.
"""

import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _stub_optional_modules() -> None:
    """Stub missing optional heavy deps so importing app.main is cheap."""
    for name in ("lancedb", "pyarrow"):
        if name in sys.modules:
            continue
        try:
            __import__(name)
        except ImportError:
            sys.modules[name] = types.ModuleType(name)
    try:
        from unstructured.partition.auto import partition  # noqa: F401

        return
    except Exception:
        pass
    unstructured = types.ModuleType("unstructured")
    unstructured.__path__ = []
    partition_pkg = types.ModuleType("unstructured.partition")
    partition_pkg.__path__ = []
    auto = types.ModuleType("unstructured.partition.auto")
    auto.partition = lambda *args, **kwargs: []
    chunking = types.ModuleType("unstructured.chunking")
    chunking.__path__ = []
    title = types.ModuleType("unstructured.chunking.title")
    title.chunk_by_title = lambda *args, **kwargs: []
    documents = types.ModuleType("unstructured.documents")
    documents.__path__ = []
    elements = types.ModuleType("unstructured.documents.elements")
    elements.Element = type("Element", (), {})
    unstructured.partition = partition_pkg
    partition_pkg.auto = auto
    chunking.title = title
    documents.elements = elements
    for name, mod in [
        ("unstructured", unstructured),
        ("unstructured.partition", partition_pkg),
        ("unstructured.partition.auto", auto),
        ("unstructured.chunking", chunking),
        ("unstructured.chunking.title", title),
        ("unstructured.documents", documents),
        ("unstructured.documents.elements", elements),
    ]:
        sys.modules[name] = mod


_stub_optional_modules()

from _db_pool import SimpleConnectionPool  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402
from app.models.database import (  # noqa: E402
    SQLiteConnectionPool,
    init_db,
    run_migrations,
)
from app.services.memory_store import MemoryStore  # noqa: E402


def _make_store(embedding_service=None):
    """Fresh on-disk SQLite MemoryStore (mirrors test_memory_hybrid_retrieval)."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    init_db(path)
    run_migrations(path)
    pool = SQLiteConnectionPool(path, max_size=2)
    return MemoryStore(pool, embedding_service=embedding_service), path


def _cleanup_store(store, path):
    store.close_all()
    if os.path.exists(path):
        os.remove(path)


class _MemoryRouteBase(unittest.TestCase):
    """Route-level base mirroring test_api_routes.TestMemoriesEndpoints,
    plus an allow-all evaluate policy so vault-scoped memories can be used."""

    def setUp(self):
        self.client = TestClient(app)
        self._temp_dir = tempfile.mkdtemp()
        self._db_path = str(Path(self._temp_dir) / "test.db")
        init_db(self._db_path)
        run_migrations(self._db_path)

        self._store_pool = SQLiteConnectionPool(self._db_path, max_size=2)
        self._test_store = MemoryStore(pool=self._store_pool)
        self._connection_pool = SimpleConnectionPool(self._db_path)

        from app.api.deps import (
            get_current_active_user,
            get_db,
            get_evaluate_policy,
            get_memory_store,
        )
        from app.security import csrf_protect

        def override_get_db():
            conn = self._connection_pool.get_connection()
            try:
                yield conn
            finally:
                self._connection_pool.release_connection(conn)

        async def allow_policy(user, resource_type, resource_id, action):
            return True

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_memory_store] = lambda: self._test_store
        app.dependency_overrides[get_current_active_user] = lambda: {
            "id": 0, "username": "admin", "role": "superadmin",
            "is_active": 1, "must_change_password": 0,
        }
        app.dependency_overrides[get_evaluate_policy] = lambda: allow_policy
        app.dependency_overrides[csrf_protect] = lambda: "test-csrf-token"
        self._deps = [get_db, get_memory_store, get_current_active_user,
                      get_evaluate_policy, csrf_protect]

        conn = self._connection_pool.get_connection()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO vaults (id, name, description) VALUES (1, 'V1', '')"
            )
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)

    def tearDown(self):
        for dep in self._deps:
            app.dependency_overrides.pop(dep, None)
        self._store_pool.close_all()
        self._connection_pool.close_all()
        shutil.rmtree(self._temp_dir, ignore_errors=True)

    def _fetch_row(self, sql, params=()):
        conn = self._connection_pool.get_connection()
        try:
            return conn.execute(sql, params).fetchone()
        finally:
            self._connection_pool.release_connection(conn)


class TestAC18NonStringTagsRejected(_MemoryRouteBase):
    """AC18 (API-005): non-string JSON tag elements are rejected with a
    validation error and nothing is persisted."""

    def test_issue515_ac18_non_string_tags_rejected(self):
        # Don't re-raise server exceptions: the defect manifests as a 500-class
        # response-serialization failure, which we assert against directly.
        client = TestClient(app, raise_server_exceptions=False)
        for bad_tags in ('[1]', '[{"a": 1}]'):
            response = client.post(
                "/api/memories",
                json={"content": f"memory for {bad_tags}", "tags": bad_tags},
            )
            self.assertIn(
                response.status_code,
                (400, 422),
                f"tags={bad_tags!r} must be rejected as a validation error, "
                f"got {response.status_code}: {response.text}",
            )
        # Nothing may be persisted by the rejected requests.
        row = self._fetch_row("SELECT COUNT(*) FROM memories")
        self.assertEqual(row[0], 0, "rejected tag payloads must not persist rows")

        # PRESERVING sibling: valid string tags still round-trip.
        ok = self.client.post(
            "/api/memories",
            json={"content": "valid tagged memory", "tags": '["a", "b"]'},
        )
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertEqual(ok.json()["metadata"]["tags"], ["a", "b"])


class TestAC19ExplicitNullClears(_MemoryRouteBase):
    """AC19 (MEM-003): explicit JSON null clears tags/expiry; field omission
    preserves them."""

    def test_issue515_ac19_explicit_null_clears_tags_and_expiry(self):
        created = self.client.post(
            "/api/memories",
            json={
                "content": "ac19 clearable memory",
                "category": "ac19",
                "tags": '["x"]',
                "vault_id": 1,
                "expires_at": "2099-06-01T00:00:00",
            },
        )
        self.assertEqual(created.status_code, 200, created.text)
        memory_id = created.json()["id"]

        cleared = self.client.put(
            f"/api/memories/{memory_id}",
            json={"tags": None, "expires_at": None},
        )
        self.assertEqual(cleared.status_code, 200, cleared.text)
        data = cleared.json()
        tags_meta = (data.get("metadata") or {}).get("tags")
        self.assertFalse(
            tags_meta,
            f"explicit tags:null must clear tags in the response, got {tags_meta!r}",
        )
        self.assertIsNone(data.get("expires_at"), "explicit expires_at:null must clear expiry in the response")
        row = self._fetch_row(
            "SELECT tags, expires_at FROM memories WHERE id = ?", (int(memory_id),)
        )
        self.assertFalse(row[0], f"tags must be NULL/empty in DB after explicit null, got {row[0]!r}")
        self.assertIsNone(row[1], "expires_at must be NULL in DB after explicit null")

        # Field omission preserves tags/expiry.
        preserved = self.client.post(
            "/api/memories",
            json={
                "content": "ac19 preserved memory",
                "category": "ac19",
                "tags": '["keep"]',
                "vault_id": 1,
                "expires_at": "2099-07-01T00:00:00",
            },
        )
        self.assertEqual(preserved.status_code, 200, preserved.text)
        kept_id = preserved.json()["id"]
        updated = self.client.put(
            f"/api/memories/{kept_id}", json={"content": "only content change"}
        )
        self.assertEqual(updated.status_code, 200, updated.text)
        row = self._fetch_row(
            "SELECT tags, expires_at FROM memories WHERE id = ?", (int(kept_id),)
        )
        self.assertIsNotNone(row[0], "omitted tags must be preserved")
        self.assertIn("keep", row[0])
        self.assertIsNotNone(row[1], "omitted expires_at must be preserved")


class TestAC23LateEmbeddingWrite(unittest.IsolatedAsyncioTestCase):
    """AC23 (MEM-001): a late-arriving embedding computed for OLD content
    must not overwrite the fresh embedding of the CURRENT content."""

    async def test_issue515_ac23_late_embedding_write_does_not_overwrite(self):
        import asyncio
        import json as _json

        content_a = "alpha apples ambient archive"
        content_b = "bravo bananas bridge beacon"
        vec_a = [1.0, 0.0, 0.0]
        vec_b = [0.0, 1.0, 0.0]

        class _GatedEmbedder:
            """Fake provider: passages for content A block on a gate we
            control; all other passages return vec_b."""

            def __init__(self):
                self.gate = asyncio.Event()

            async def embed_passage(self, text):
                if text == content_a:
                    await self.gate.wait()
                    return vec_a
                return vec_b

            async def embed_single(self, text):
                return await self.embed_passage(text)

        embedder = _GatedEmbedder()
        store, path = _make_store(embedding_service=embedder)
        try:
            rec = store.add_memory(content_a, vault_id=1)
            # Start the (slow) embed for the OLD content — it parks on the gate.
            late_task = asyncio.create_task(store.embed_and_store(rec.id, content_a))

            # Update content to B the way the route does: swap content and
            # clear the stale embedding, then recompute best-effort.
            conn = store.pool.get_connection()
            try:
                conn.execute(
                    "UPDATE memories SET content = ?, embedding = NULL, "
                    "embedding_model = NULL WHERE id = ?",
                    (content_b, rec.id),
                )
                conn.commit()
            finally:
                store.pool.release_connection(conn)
            await store.embed_and_store(rec.id, content_b)  # writes vec_b

            # NOW the old-content embedding completes — last writer must NOT win.
            embedder.gate.set()
            await late_task

            row = None
            conn = store.pool.get_connection()
            try:
                row = conn.execute(
                    "SELECT content, embedding, embedding_model FROM memories WHERE id = ?",
                    (rec.id,),
                ).fetchone()
            finally:
                store.pool.release_connection(conn)
            self.assertEqual(row[0], content_b)
            stored_vec = _json.loads(row[1]) if row[1] else None
            self.assertIsNotNone(stored_vec, "current-content embedding must remain stored")
            self.assertNotEqual(
                stored_vec, vec_a,
                f"stale old-content vector overwrote the fresh one: {stored_vec!r}",
            )
            self.assertEqual(stored_vec, vec_b)
            # embedding_model consistency holds.
            self.assertEqual(row[2], settings.embedding_model)

            # Behavioral check: a dense query specific to B still ranks this row.
            dense = store._dense_search(list(vec_b), limit=5, vault_id=1)
            self.assertTrue(dense, "dense search with B's query vector must find the row")
            self.assertEqual(dense[0].id, rec.id)
            self.assertGreater(dense[0].score, 0.9)
        finally:
            _cleanup_store(store, path)


class TestAC24BackfillCountsTruthfully(unittest.IsolatedAsyncioTestCase):
    """AC24 (OBS-004): backfill counts provider failures and None outcomes as
    not-processed; only real successes increment processed."""

    async def test_issue515_ac24_backfill_counts_failures_truthfully(self):
        store, path = _make_store(embedding_service=None)
        try:
            for i in range(3):
                store.add_memory(f"ac24 unembedded memory number {i}", vault_id=1)

            calls = {"n": 0}

            class _FlakyEmbedder:
                async def embed_passage(self, text):
                    calls["n"] += 1
                    if calls["n"] == 1:
                        raise RuntimeError("provider exploded")
                    if calls["n"] == 2:
                        return None  # skip-failure
                    return [0.1, 0.2, 0.3]

                async def embed_single(self, text):
                    return [0.1, 0.2, 0.3]

            store.embedding_service = _FlakyEmbedder()
            summary = await store.backfill_missing_embeddings()

            self.assertEqual(summary["total"], 3)
            self.assertEqual(
                summary["processed"], 1,
                f"only the successful embed may count as processed, got {summary}",
            )
            self.assertGreaterEqual(
                summary["failed"], 2,
                f"provider failure AND None outcome must both count as failed, got {summary}",
            )
            # The failed rows still have NO embedding; exactly one row succeeded.
            conn = store.pool.get_connection()
            try:
                with_emb = conn.execute(
                    "SELECT COUNT(*) FROM memories WHERE embedding IS NOT NULL"
                ).fetchone()[0]
                without_emb = conn.execute(
                    "SELECT COUNT(*) FROM memories WHERE embedding IS NULL"
                ).fetchone()[0]
            finally:
                store.pool.release_connection(conn)
            self.assertEqual(with_emb, 1)
            self.assertEqual(without_emb, 2)
        finally:
            _cleanup_store(store, path)


class TestAC25ZeroImportance(unittest.TestCase):
    """AC25 (MEM-002): importance 0.0 round-trips as 0.0 (not coerced to the
    0.5 default) through the store's read paths."""

    def test_issue515_ac25_zero_importance_round_trips(self):
        from unittest.mock import patch

        store, path = _make_store(embedding_service=None)
        try:
            rec = store.add_memory(
                "ac25 zero importance needle note", vault_id=1, importance=0.0
            )
            self.assertEqual(rec.importance, 0.0)

            row = None
            conn = store.pool.get_connection()
            try:
                row = conn.execute(
                    "SELECT importance FROM memories WHERE id = ?", (rec.id,)
                ).fetchone()
            finally:
                store.pool.release_connection(conn)
            self.assertEqual(float(row[0]), 0.0)

            results = store.search_memories("needle", limit=5, vault_id=1)
            self.assertTrue(results, "expected the FTS hit")
            self.assertEqual(results[0].importance, 0.0)

            # Dense read path returns 0.0 too.
            store._store_embedding(rec.id, [1.0, 0.0, 0.0])
            with patch("app.services.memory_store.settings") as mock_settings:
                mock_settings.memory_relevance_filter_enabled = False
                mock_settings.memory_dense_min_similarity = 0.0
                mock_settings.memory_dense_max_candidates = 1000
                dense = store._dense_search([1.0, 0.0, 0.0], limit=5, vault_id=1)
            self.assertTrue(dense, "expected the dense hit")
            self.assertEqual(dense[0].importance, 0.0)

            # PRESERVING sibling: absent importance defaults to 0.5.
            default_rec = store.add_memory("ac25 default importance note", vault_id=1)
            self.assertEqual(default_rec.importance, 0.5)
        finally:
            _cleanup_store(store, path)


class TestAC27NoopSaveKeepsClaims(_MemoryRouteBase):
    """AC27 (D2 contract): a memory update with IDENTICAL content must not
    supersede sole-source wiki claims; a real content change still does."""

    def test_issue515_ac27_noop_update_keeps_claims_active(self):
        created = self.client.post(
            "/api/memories",
            json={"content": "ac27 original claim content", "vault_id": 1},
        )
        self.assertEqual(created.status_code, 200, created.text)
        memory_id = int(created.json()["id"])

        conn = self._connection_pool.get_connection()
        try:
            cur = conn.execute(
                "INSERT INTO wiki_claims (vault_id, claim_text, source_type, status) "
                "VALUES (1, 'ac27 seeded sole-source claim', 'memory', 'active')"
            )
            claim_id = cur.lastrowid
            conn.execute(
                "INSERT INTO wiki_claim_sources (claim_id, source_kind, memory_id) "
                "VALUES (?, 'memory', ?)",
                (claim_id, memory_id),
            )
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)

        # No-op save: identical content.
        noop = self.client.put(
            f"/api/memories/{memory_id}",
            json={"content": "ac27 original claim content"},
        )
        self.assertEqual(noop.status_code, 200, noop.text)
        status_row = self._fetch_row(
            "SELECT status FROM wiki_claims WHERE id = ?", (claim_id,)
        )
        self.assertEqual(
            status_row[0], "active",
            "identical-content save must not supersede the sole-source claim",
        )

        # PRESERVING sibling: a real content change still supersedes.
        changed = self.client.put(
            f"/api/memories/{memory_id}",
            json={"content": "ac27 edited claim content"},
        )
        self.assertEqual(changed.status_code, 200, changed.text)
        status_row = self._fetch_row(
            "SELECT status FROM wiki_claims WHERE id = ?", (claim_id,)
        )
        self.assertEqual(status_row[0], "superseded")


class TestAC28WhitespaceContentRejected(_MemoryRouteBase):
    """AC28 (DEEP-D-02, backend): whitespace-only content is rejected by both
    the create and update memory APIs and mutates nothing."""

    def test_issue515_ac28_whitespace_content_rejected(self):
        created = self.client.post(
            "/api/memories", json={"content": "ac28 original content", "vault_id": 1}
        )
        self.assertEqual(created.status_code, 200, created.text)
        memory_id = created.json()["id"]

        put = self.client.put(
            f"/api/memories/{memory_id}", json={"content": "   "}
        )
        self.assertIn(
            put.status_code, (400, 422),
            f"whitespace-only PUT content must 4xx, got {put.status_code}: {put.text}",
        )
        row = self._fetch_row(
            "SELECT content FROM memories WHERE id = ?", (int(memory_id),)
        )
        self.assertEqual(row[0], "ac28 original content")

        post = self.client.post("/api/memories", json={"content": "   "})
        self.assertIn(
            post.status_code, (400, 422),
            f"whitespace-only POST content must 4xx, got {post.status_code}: {post.text}",
        )
        row = self._fetch_row("SELECT COUNT(*) FROM memories")
        self.assertEqual(row[0], 1, "whitespace-only POST must not persist a row")


if __name__ == "__main__":
    unittest.main()
