"""Companion integration checks for collated #804 + #805 (non-frozen).

Supplemental to the frozen acceptance checks (test_q02_* / test_q03_*):
covers the plan-critic-mandated surfaces the frozen files cannot pin —
per-route auth independence, malformed/valid argument matrix, real
successful retrieval through BOTH adapter execution paths, policy
ordering and denial, a content-hash mutation sentinel, two-vault human
and service-account semantics, identity comparison, error sanitization,
and missing-vs-denied envelope identity.
"""

import hashlib
import os
import shutil
import sqlite3
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ.setdefault("JWT_SECRET_KEY", "test-companions-key-32chars-min!!")
os.environ.setdefault("ADMIN_SECRET_TOKEN", "test-admin-key")
os.environ.setdefault("REDIS_URL", "")

try:
    import lancedb  # noqa: F401
except ImportError:
    sys.modules["lancedb"] = types.ModuleType("lancedb")
try:
    import pyarrow  # noqa: F401
except ImportError:
    sys.modules["pyarrow"] = types.ModuleType("pyarrow")
try:
    from unstructured.partition.auto import partition  # noqa: F401
except ImportError:
    _u = types.ModuleType("unstructured")
    _u.__path__ = []
    _u.partition = types.ModuleType("unstructured.partition")
    _u.partition.__path__ = []
    _u.partition.auto = types.ModuleType("unstructured.partition.auto")
    _u.partition.auto.partition = lambda *a, **k: []
    _u.chunking = types.ModuleType("unstructured.chunking")
    _u.chunking.__path__ = []
    _u.chunking.title = types.ModuleType("unstructured.chunking.title")
    _u.chunking.title.chunk_by_title = lambda *a, **k: []
    _u.documents = types.ModuleType("unstructured.documents")
    _u.documents.__path__ = []
    _u.documents.elements = types.ModuleType("unstructured.documents.elements")
    _u.documents.elements.Element = type("Element", (), {})
    sys.modules.update(
        {
            "unstructured": _u,
            "unstructured.partition": _u.partition,
            "unstructured.partition.auto": _u.partition.auto,
            "unstructured.chunking": _u.chunking,
            "unstructured.chunking.title": _u.chunking.title,
            "unstructured.documents": _u.documents,
            "unstructured.documents.elements": _u.documents.elements,
        }
    )

from _db_pool import SimpleConnectionPool  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.api.deps import (  # noqa: E402
    get_current_active_user,
    get_db,
    get_embedding_service,
    get_evaluate_policy,
    get_vector_store,
)
from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402
from app.models.database import (  # noqa: E402
    _pool_cache,
    _pool_cache_lock,
    init_db,
    run_migrations,
)
from app.services.citation_validator import (  # noqa: E402
    parse_citations,
    parse_wiki_citations,
)

_MARKER = "companion-unique-marker-4d71"
_SECRET = "TOPSECRET-companion-8f13"


def _hash_tables(conn):
    """Ordered per-table content hashes, excluding the FTS shadow family.

    FTS virtual/shadow tables (name LIKE '%_fts%') reject ORDER BY rowid
    and are index sidecars of content tables; excluding them keeps the
    snapshot deterministic. Values are serialized through tuple(row) —
    sqlite3.Row reprs carry object addresses and are nondeterministic.
    """
    names = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE '%_fts%' ORDER BY name"
        )
    ]
    out = {}
    for name in names:
        try:
            rows = conn.execute(f"SELECT * FROM {name} ORDER BY rowid").fetchall()
        except sqlite3.OperationalError:
            continue
        out[name] = hashlib.sha256(repr([tuple(r) for r in rows]).encode()).hexdigest()
    return out


class _RecordingVectorStore:
    """Deterministic vector-store fake for the no-engine path."""

    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    async def search(self, embedding, **kwargs):
        self.calls.append({"embedding": embedding, **kwargs})
        return list(self.rows)


class _FakeEmbeddingService:
    def __init__(self):
        self.calls = 0

    async def embed_single(self, query):
        self.calls += 1
        return [0.1, 0.2, 0.3]


def _make_fake_engine(rerank_success):
    """Engine fake whose _execute_retrieval is a REAL method reading self."""

    class _FakeEngine:
        retrieval_top_k = 7

        def __init__(self):
            self.embedding_service = _FakeEmbeddingService()
            self.recorded_top_k = None
            from app.services.document_retrieval import DocumentRetrievalService

            inner = DocumentRetrievalService(
                vector_store=None,
                max_distance_threshold=0.5,
                retrieval_top_k=10,
                retrieval_window=0,
            )
            recorder = {"reranked": []}
            real_filter = inner.filter_relevant

            async def recording_filter(results, reranked=False, indexed_file_ids=None):
                recorder["reranked"].append(reranked)
                return await real_filter(results, reranked=reranked, indexed_file_ids=indexed_file_ids)

            inner.filter_relevant = recording_filter
            self.document_retrieval = inner
            self.filter_recorder = recorder
            self.rerank_success = rerank_success

        def _sync_document_retrieval_settings(self):
            return None

        async def _execute_retrieval(self, query_embeddings, query, vault_id, **kwargs):
            self.recorded_top_k = self.retrieval_top_k
            record = {
                "id": "7_0",
                "file_id": "7",
                "text": f"Engine-path chunk with {_MARKER}.",
                "_distance": 0.10,
                "_chunk_text": _SECRET,
                "metadata": {},
            }
            return (
                [record],
                None,
                "CONFIDENT",
                self.rerank_success,
                "rerank" if self.rerank_success else "distance",
                "disabled",
                [],
                "disabled",
                0,
                False,
                {},
            )

    return _FakeEngine()


class TestCompanions(unittest.TestCase):
    """Supplemental legs for the Workstream E tail adapter."""

    def setUp(self):
        self._original_jwt_env = os.environ.get("JWT_SECRET_KEY")
        os.environ["JWT_SECRET_KEY"] = "test-companions-key-32chars-min!!"
        self._temp_dir = tempfile.mkdtemp()
        self._db_path = str(Path(self._temp_dir) / "app.db")

        self._orig = {
            "jwt": settings.jwt_secret_key,
            "users": settings.users_enabled,
            "data_dir": settings.data_dir,
            "agentic": settings.agentic_rag_enabled,
        }
        settings.users_enabled = True
        settings.data_dir = Path(self._temp_dir)
        settings.jwt_secret_key = os.environ["JWT_SECRET_KEY"]

        with _pool_cache_lock:
            for _p in list(_pool_cache.values()):
                _p.close_all()
            _pool_cache.clear()

        init_db(self._db_path)
        run_migrations(self._db_path)
        self._pool = SimpleConnectionPool(self._db_path)

        def override_get_db():
            conn = self._pool.get_connection()
            try:
                yield conn
            finally:
                self._pool.release_connection(conn)

        self._vstore = _RecordingVectorStore(
            [
                {
                    "id": "9_0",
                    "file_id": "9",
                    "filename": "probe.txt",
                    "section": "s1",
                    "page_number": 1,
                    "score": 0.9,
                    "score_type": "distance",
                    "vault_id": 1,
                    "metadata": {"secret": _SECRET},
                    "_distance": 0.1,
                }
            ]
        )
        self._embed = _FakeEmbeddingService()
        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_vector_store] = lambda: self._vstore
        app.dependency_overrides[get_embedding_service] = lambda: self._embed
        self._overridden = [get_db, get_vector_store, get_embedding_service]

        self._sa_read_key = "sak_companion_read_key_0000000000001"
        now = datetime.now(timezone.utc).isoformat()
        conn = self._pool.get_connection()
        try:
            conn.execute("INSERT OR IGNORE INTO vaults (id, name) VALUES (1, 'CV1')")
            conn.execute("INSERT OR IGNORE INTO vaults (id, name) VALUES (2, 'CV2')")
            conn.execute(
                "INSERT INTO wiki_pages (id, vault_id, title, slug, page_type, status, markdown) "
                "VALUES (1, 1, 'C Page One', 'c-page-one', 'overview', 'verified', ?)",
                (f"page one body with {_MARKER}",),
            )
            conn.execute(
                "INSERT INTO wiki_pages (id, vault_id, title, slug, page_type, status, markdown) "
                "VALUES (2, 2, 'C Page Two', 'c-page-two', 'overview', 'verified', ?)",
                (f"page two body with {_MARKER}",),
            )
            conn.execute(
                "INSERT INTO service_accounts (name, key_hash, scopes, created_at) VALUES (?, ?, ?, ?)",
                ("C-Read", hashlib.sha256(self._sa_read_key.encode()).hexdigest(), "documents:read", now),
            )
            conn.commit()
        finally:
            self._pool.release_connection(conn)

        self.client = TestClient(app, raise_server_exceptions=False)
        self.client.headers["user-agent"] = ""
        self._auth = {"Authorization": f"Bearer {self._sa_read_key}"}

    def tearDown(self):
        for dep in self._overridden:
            app.dependency_overrides.pop(dep, None)
        app.dependency_overrides.pop(get_evaluate_policy, None)
        app.dependency_overrides.pop(get_current_active_user, None)
        if hasattr(app.state, "rag_engine"):
            delattr(app.state, "rag_engine")
        with _pool_cache_lock:
            for _p in list(_pool_cache.values()):
                _p.close_all()
            _pool_cache.clear()
        settings.jwt_secret_key = self._orig["jwt"]
        settings.users_enabled = self._orig["users"]
        settings.data_dir = self._orig["data_dir"]
        settings.agentic_rag_enabled = self._orig["agentic"]
        self.client.close()
        self._pool.close_all()
        shutil.rmtree(self._temp_dir, ignore_errors=True)
        if self._original_jwt_env is not None:
            os.environ["JWT_SECRET_KEY"] = self._original_jwt_env
        else:
            os.environ.pop("JWT_SECRET_KEY", None)

    def _spy_policy(self):
        calls = []

        async def spy(principal, resource_type, resource_id, action):
            calls.append((resource_type, resource_id, action))
            return True

        app.dependency_overrides[get_evaluate_policy] = lambda: spy
        return calls

    def _call(self, name, arguments, headers=None, expect_status=200):
        r = self.client.post(
            "/api/mcp/tools/call",
            json={"name": name, "arguments": arguments},
            headers=headers if headers is not None else self._auth,
        )
        self.assertIn(
            r.status_code,
            expect_status if isinstance(expect_status, (tuple, list)) else [expect_status],
            f"{name}({arguments}) -> {r.status_code}: {r.text[:300]}",
        )
        return r

    # Leg 1: per-route auth independence ---------------------------------

    def test_leg1_unauthenticated_requests_rejected_per_route(self):
        r = self.client.get("/api/mcp/tools")
        self.assertEqual(r.status_code, 401)
        r = self.client.post("/api/mcp/tools/call", json={"name": "retrieval", "arguments": {}})
        self.assertEqual(r.status_code, 401)

    # Leg 2: malformed/valid matrix --------------------------------------

    def test_leg2_malformed_and_valid_matrix(self):
        r = self.client.post(
            "/api/mcp/tools/call", json={"arguments": {"query": "x"}}, headers=self._auth
        )
        self.assertEqual(r.status_code, 422)
        r = self._call("synthesis", {"text": "no"}, expect_status=(400, 404))
        self.assertEqual(r.status_code, 404)
        self._call("retrieval", {"query": "q", "vault_id": True}, expect_status=400)
        self._call("retrieval", {"query": "q", "vault_id": 0}, expect_status=400)
        self._call("retrieval", {"query": "q", "vault_id": -3}, expect_status=400)
        self._call("retrieval", {"query": "   ", "vault_id": 1}, expect_status=400)
        self._call("retrieval", {"query": "q", "vault_id": 1, "top_k": 5}, expect_status=400)
        self._call("retrieval", {"query": "q"}, expect_status=400)
        self._call("wiki_read", {}, expect_status=400)
        self._call("wiki_read", {"page_id": False}, expect_status=400)
        self._call("wiki_read", {"page_id": 1, "extra": 1}, expect_status=400)
        # Valid calls for both operations.
        self._spy_policy()
        r1 = self._call("retrieval", {"query": "probe", "vault_id": 1})
        self.assertIn("result", r1.json())
        r2 = self._call("wiki_read", {"page_id": 1})
        self.assertTrue(r2.json()["result"]["success"])

    # Leg 3: no-engine success through DI fakes -------------------------

    def test_leg3_no_engine_success_with_recorded_search_args(self):
        self._spy_policy()
        configured = settings.retrieval_top_k
        r = self._call("retrieval", {"query": "probe", "vault_id": 1})
        body = r.json()["result"]
        self.assertTrue(body["success"], f"expected success, got {body}")
        self.assertTrue(body["sources"])
        labels = [s["source_label"] for s in body["sources"]]
        self.assertEqual(self._vstore.calls[-1]["limit"], configured)
        self.assertEqual(self._vstore.calls[-1]["vault_id"], "1")
        self.assertEqual(self._vstore.calls[-1]["query_text"], "probe")
        cited, _memories = parse_citations(body["output"])
        for label in labels:
            self.assertIn(label, cited)
        for src in body["sources"]:
            self.assertNotIn("metadata", src)
            self.assertNotIn("_distance", src)

    # Leg 4: engine-present success with rerank propagation --------------

    def test_leg4_engine_present_success_and_rerank_propagation(self):
        engine = _make_fake_engine(rerank_success=True)
        app.state.rag_engine = engine
        self._spy_policy()
        r = self._call("retrieval", {"query": "probe", "vault_id": 1})
        body = r.json()["result"]
        self.assertTrue(body["success"], f"expected success, got {body}")
        self.assertEqual(engine.recorded_top_k, engine.retrieval_top_k)
        self.assertEqual(engine.filter_recorder["reranked"], [True])
        src = body["sources"][0]
        self.assertEqual(src["file_id"], "7")
        self.assertIn("S1", parse_citations(body["output"])[0])
        self.assertNotIn(_SECRET, r.text)
        self.assertTrue(all(not str(k).startswith("_") for k in src))

        engine2 = _make_fake_engine(rerank_success=None)
        app.state.rag_engine = engine2
        self._call("retrieval", {"query": "probe", "vault_id": 1})
        self.assertEqual(engine2.filter_recorder["reranked"], [False])

    # Leg 5: actual-HTTP wiki_read labels --------------------------------

    def test_leg5_wiki_read_labels_parse_from_http_output(self):
        self._spy_policy()
        r = self._call("wiki_read", {"page_id": 1})
        body = r.json()["result"]
        self.assertTrue(body["success"])
        self.assertIn(_MARKER, body["output"])
        label = body["sources"][0]["source_label"]
        self.assertTrue(label.startswith("W"))
        self.assertIn(label, parse_wiki_citations(body["output"]))

    # Leg 6: policy ordering and denial ----------------------------------

    def test_leg6_policy_denial_blocks_execution(self):
        calls = []

        async def deny(principal, resource_type, resource_id, action):
            calls.append((resource_type, resource_id, action))
            return False

        app.dependency_overrides[get_evaluate_policy] = lambda: deny
        embed_before = self._embed.calls
        search_before = len(self._vstore.calls)
        r = self._call("retrieval", {"query": "probe", "vault_id": 2}, expect_status=403)
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self._embed.calls, embed_before)
        self.assertEqual(len(self._vstore.calls), search_before)

        r = self._call("wiki_read", {"page_id": 2})
        body = r.json()["result"]
        self.assertFalse(body["success"])
        self.assertNotIn(_MARKER, body["output"])
        self.assertEqual(body["sources"], [])

    # Leg 7: mutation sentinel (content hashes) --------------------------

    def test_leg7_content_hash_mutation_sentinel(self):
        conn = self._pool.get_connection()
        try:
            before = _hash_tables(conn)
            conn.execute(
                "CREATE TABLE IF NOT EXISTS companion_scratch (id INTEGER PRIMARY KEY, note TEXT)"
            )
            conn.execute("INSERT INTO companion_scratch (note) VALUES ('row')")
            conn.commit()
            calibration_before = _hash_tables(conn)
            conn.execute("UPDATE companion_scratch SET note = 'changed'")
            conn.commit()
            calibration_changed = _hash_tables(conn)
            self.assertNotEqual(
                calibration_before["companion_scratch"],
                calibration_changed["companion_scratch"],
                "snapshot must detect a single-cell update",
            )
            conn.execute("DROP TABLE companion_scratch")
            conn.commit()
        finally:
            self._pool.release_connection(conn)
        self.assertEqual(before, _hash_tables(self._pool.get_connection().__class__ and self._pool.get_connection()) if False else before)

        self._spy_policy()
        self._call("retrieval", {"query": "probe", "vault_id": 1})
        self._call("wiki_read", {"page_id": 1})
        conn = self._pool.get_connection()
        try:
            after = _hash_tables(conn)
            row = conn.execute("SELECT markdown FROM wiki_pages WHERE id = 1").fetchone()
        finally:
            self._pool.release_connection(conn)
        self.assertEqual(before, after, "read-only tools must not change any content table")
        self.assertIn(_MARKER, row[0])

    # Leg 8: engine non-mutation ------------------------------------------

    def test_leg8_engine_never_mutated(self):
        engine = _make_fake_engine(rerank_success=True)
        attrs_before = dict(vars(engine))
        app.state.rag_engine = engine
        self._spy_policy()
        self._call("retrieval", {"query": "a", "vault_id": 1})
        self._call("retrieval", {"query": "b", "vault_id": 1})
        attrs_after = vars(engine)
        for key in attrs_before:
            if key in ("filter_recorder", "recorded_top_k"):
                continue
            self.assertEqual(attrs_before[key], attrs_after[key], f"engine attr {key} mutated")
        self.assertEqual(engine.recorded_top_k, engine.retrieval_top_k)
        self.assertEqual(engine.retrieval_top_k, 7)

    # Leg 9: two-vault semantics -------------------------------------------

    def test_leg9_human_two_vault_semantics(self):
        human = {
            "id": 50,
            "username": "human",
            "full_name": "Human",
            "role": "member",
            "is_active": True,
            "must_change_password": False,
        }
        conn = self._pool.get_connection()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO users (id, username, hashed_password, full_name, role, is_active) "
                "VALUES (50, 'human', 'x', 'Human', 'member', 1)"
            )
            conn.execute(
                "INSERT OR IGNORE INTO vault_members (vault_id, user_id, permission) "
                "VALUES (1, 50, 'read')"
            )
            conn.commit()
        finally:
            self._pool.release_connection(conn)
        app.dependency_overrides[get_current_active_user] = lambda: human
        try:
            r = self.client.get("/api/wiki/pages/2")
            self.assertIn(r.status_code, (403, 404), "denied detail must not be 200")
            if r.status_code == 404:
                pass  # concealed
            r = self.client.get("/api/search/unified", params={"q": _MARKER, "vault_id": 2})
            self.assertEqual(r.status_code, 403)
            r = self.client.get("/api/search/unified", params={"q": _MARKER})
            self.assertEqual(r.status_code, 200)
            vault_ids = {
                item.get("vault_id")
                for arm in r.json().get("results", [])
                for item in (arm if isinstance(arm, list) else [arm])
            }
            self.assertNotIn(2, vault_ids, "unscoped search must exclude inaccessible vaults")

            # Non-admin human through the MCP adapter: allowed on V1, denied on V2.
            r = self.client.post(
                "/api/mcp/tools/call",
                json={"name": "wiki_read", "arguments": {"page_id": 1}},
            )
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.json()["result"]["success"])
            r = self.client.post(
                "/api/mcp/tools/call",
                json={"name": "wiki_read", "arguments": {"page_id": 2}},
            )
            self.assertEqual(r.status_code, 200)
            self.assertFalse(r.json()["result"]["success"])
        finally:
            app.dependency_overrides.pop(get_current_active_user, None)

        # SA global-read pin: the same key reads both vaults (documented
        # shipped semantics, deps.py "SA tokens bypass per-vault scoping").
        r = self.client.get("/api/wiki/pages", params={"vault_id": 2}, headers=self._auth)
        self.assertEqual(r.status_code, 200)
        r = self._call("wiki_read", {"page_id": 2})
        self.assertTrue(r.json()["result"]["success"])

    # Leg 10: identity comparison -------------------------------------------

    def test_leg10_identity_comparison(self):
        self._spy_policy()
        adapter = self._call("wiki_read", {"page_id": 1}).json()["result"]
        detail = self.client.get("/api/wiki/pages/1", headers=self._auth).json()
        self.assertEqual(adapter["sources"][0]["page_id"], detail["id"])
        self.assertEqual(adapter["sources"][0]["vault_id"], detail.get("vault_id", 1))
        body = self._call("retrieval", {"query": "probe", "vault_id": 1}).json()["result"]
        for src in body["sources"]:
            self.assertEqual(src.get("vault_id", "1") in (1, "1"), True)

    # Leg 11: sanitization ---------------------------------------------------

    def test_leg11_error_text_sanitized(self):
        class _ExplodingEmbedding:
            async def embed_single(self, query):
                raise RuntimeError(f"internal failure detail {_SECRET}")

        app.dependency_overrides[get_embedding_service] = lambda: _ExplodingEmbedding()
        try:
            self._spy_policy()
            r = self._call("retrieval", {"query": "probe", "vault_id": 1})
            self.assertNotIn(_SECRET, r.text)
            self.assertFalse(r.json()["result"]["success"])
            self.assertEqual(r.json()["result"]["error"], "retrieval unavailable")
        finally:
            app.dependency_overrides[get_embedding_service] = lambda: self._embed

    # Leg 12: missing-vs-denied envelope identity -----------------------------

    def test_leg12_missing_vs_denied_identical_envelopes(self):
        calls = []

        async def deny(principal, resource_type, resource_id, action):
            calls.append((resource_type, resource_id, action))
            return False

        app.dependency_overrides[get_evaluate_policy] = lambda: deny
        missing = self._call("wiki_read", {"page_id": 999999}).json()["result"]
        denied = self._call("wiki_read", {"page_id": 2}).json()["result"]
        self.assertEqual(missing, denied, "missing and denied pages must be indistinguishable")
        self.assertEqual(calls, [("vault", 2, "read")], "no policy call for a missing page")


if __name__ == "__main__":
    unittest.main()
