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
import threading
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

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
    get_current_user_or_service_account,
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


def _dependency_calls(route):
    """Every callable in ONE route's dependency tree (endpoint + sub-deps).

    Same flat-``Dependant`` walk the frozen AC16 check uses (FastAPI 0.139
    stores sub-dependencies directly in ``dependant.dependencies``), kept
    here so the per-route assertion below does not depend on the frozen
    file's helper. Callers must apply it per route: flattening the calls of
    SEVERAL routes into one list proves only the union.
    """
    calls = []
    stack = [route.dependant]
    while stack:
        dependant = stack.pop()
        calls.append(dependant.call)
        stack.extend(dependant.dependencies)
    return calls


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
        # The adapter-owned retrieval gate ran the policy exactly once.
        self.assertEqual(calls, [("vault", 2, "read")])

        r = self._call("wiki_read", {"page_id": 2})
        body = r.json()["result"]
        self.assertFalse(body["success"])
        self.assertNotIn(_MARKER, body["output"])
        self.assertEqual(body["sources"], [])
        # The tool-owned wiki gate ran the policy exactly once for the
        # denial (the spy is not dead code: it proves wiki_read invokes the
        # vault policy on its own path, not only through the adapter gate).
        self.assertEqual(
            calls,
            [("vault", 2, "read"), ("vault", 2, "read")],
            "wiki_read must invoke the vault-read policy exactly once on denial",
        )

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
        # (The dead `... if False else before` comparison that used to sit
        # here was a tautology and leaked two pooled connections; the real
        # before/after sentinel is asserted after the tool calls below.)

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
        # The adapter must pass the engine's OWN configured top_k through to
        # the retrieval sub-service (no hardcoded limit of its own). The fake's
        # configured value is not itself a production contract, so only the
        # pass-through equality is asserted.
        self.assertEqual(engine.recorded_top_k, engine.retrieval_top_k)

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
            # Denied detail: page 2 lives in vault 2, which this human cannot
            # read, so the vault denial must collapse to the concealed 404
            # (never a 200, and never a 403 that would confirm existence).
            r = self.client.get("/api/wiki/pages/2")
            self.assertEqual(r.status_code, 404, "denied detail must be the concealed 404")
            # Positive control: the SAME unscoped search must still return the
            # vault this human CAN read, so the exclusion below is a real
            # filter rather than an empty result set.
            r = self.client.get("/api/search/unified", params={"q": _MARKER, "vault_id": 1})
            self.assertEqual(r.status_code, 200)
            allowed_ids = {
                item.get("vault_id")
                for arm in r.json().get("results", [])
                for item in (arm if isinstance(arm, list) else [arm])
            }
            self.assertIn(1, allowed_ids, "scoped search for an accessible vault must return it")
            r = self.client.get("/api/search/unified", params={"q": _MARKER, "vault_id": 2})
            self.assertEqual(r.status_code, 403)
            r = self.client.get("/api/search/unified", params={"q": _MARKER})
            self.assertEqual(r.status_code, 200)
            vault_ids = {
                item.get("vault_id")
                for arm in r.json().get("results", [])
                for item in (arm if isinstance(arm, list) else [arm])
            }
            self.assertIn(
                1,
                vault_ids,
                "unscoped search must still return the accessible vault (positive "
                f"control: an empty result would pass the exclusion vacuously); got {vault_ids}",
            )
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
        # Assert the real vault id from the detail body (no defaulted read:
        # the seeded page lives in vault 1, so the adapter and the route
        # must both report 1).
        self.assertEqual(adapter["sources"][0]["vault_id"], detail["vault_id"])
        self.assertEqual(detail["vault_id"], 1)
        body = self._call("retrieval", {"query": "probe", "vault_id": 1}).json()["result"]
        for src in body["sources"]:
            self.assertIn(src["vault_id"], (1, "1"), "fallback sources must name vault 1")

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

    # Leg 13: SA principals never read other users' chat titles -------------

    def test_leg13_sa_cannot_read_other_users_chat_titles(self):
        conn = self._pool.get_connection()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO users (id, username, hashed_password, full_name, role, is_active) "
                "VALUES (77, 'alice', 'x', 'Alice', 'member', 1)"
            )
            conn.execute(
                "INSERT INTO chat_sessions (id, vault_id, user_id, title, created_at, updated_at) "
                "VALUES (1, 2, 77, 'zqprobe alice private salary negotiation', ?, ?)",
                (datetime.now(timezone.utc).isoformat(),) * 2,
            )
            conn.commit()
        finally:
            self._pool.release_connection(conn)
        for params in ({"q": "zqprobe", "types": "chat"}, {"q": "zqprobe", "types": "chat", "vault_id": 2}):
            r = self.client.get("/api/search/unified", params=params, headers=self._auth)
            self.assertEqual(r.status_code, 200, r.text[:200])
            chat_rows = [
                item for item in r.json().get("results", []) if item.get("type") == "chat"
            ]
            self.assertEqual(
                chat_rows,
                [],
                "service-account key must never receive other users' chat rows "
                f"(params={params}); observed: {chat_rows}",
            )

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

    # Leg 14: per-route combined dependency ---------------------------------
    #
    # The frozen AC16 check (test_q03_mcp_surface.py) flattens the
    # dependency calls of ALL adapter routes into one list, so it proves
    # only the union. #805 AC7 requires the combined dependency in EACH
    # such route's tree; this non-frozen leg pins the per-route property.

    def test_leg14_each_mcp_route_carries_combined_dependency(self):
        mcp_routes = [
            route
            for route in app.routes
            if getattr(getattr(route, "endpoint", None), "__module__", None)
            == "app.services.mcp_server"
        ]
        self.assertEqual(
            len(mcp_routes),
            2,
            "both adapter routes (GET /api/mcp/tools, POST /api/mcp/tools/call) "
            f"must be mounted; found {len(mcp_routes)}",
        )
        for route in mcp_routes:
            self.assertIn(
                get_current_user_or_service_account,
                _dependency_calls(route),
                f"route {getattr(route, 'path', '?')} lost the combined "
                "service-account dependency (AC7 requires it per route)",
            )

    # Leg 15: write-only key denied on every SA-accepting route -------------

    def test_leg15_write_only_sa_key_denied_on_every_sa_route(self):
        write_only_key = "sak_companion_write_key_000000000002"
        now = datetime.now(timezone.utc).isoformat()
        conn = self._pool.get_connection()
        try:
            conn.execute(
                "INSERT INTO service_accounts (name, key_hash, scopes, created_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    "C-Write",
                    hashlib.sha256(write_only_key.encode()).hexdigest(),
                    "documents:write",
                    now,
                ),
            )
            conn.commit()
        finally:
            self._pool.release_connection(conn)
        headers = {"Authorization": f"Bearer {write_only_key}"}
        probes = [
            ("get", "/api/wiki/pages", {"params": {"vault_id": 1}}),
            ("get", "/api/wiki/pages/1", {}),
            ("get", "/api/search/unified", {"params": {"q": _MARKER}}),
            ("get", "/api/mcp/tools", {}),
            (
                "post",
                "/api/mcp/tools/call",
                {"json": {"name": "retrieval", "arguments": {"query": "q", "vault_id": 1}}},
            ),
        ]
        for method, path, kwargs in probes:
            r = getattr(self.client, method)(path, headers=headers, **kwargs)
            self.assertEqual(
                r.status_code,
                403,
                f"{method.upper()} {path} must reject a documents:write-only key "
                f"with 403, got {r.status_code}: {r.text[:200]}",
            )

    # Leg 16: input-boundary matrix -----------------------------------------

    def test_leg16_input_boundary_matrix(self):
        # Ids at or above 2**63 overflow the SQLite bind; they must be a 400,
        # never an unhandled 500 (the adapter validators and the swapped wiki
        # detail route both enforce the range).
        self._call("retrieval", {"query": "q", "vault_id": 2**63}, expect_status=400)
        self._call("wiki_read", {"page_id": 2**63}, expect_status=400)
        r = self.client.get(
            f"/api/wiki/pages/{2**63}", headers=self._auth
        )
        self.assertEqual(
            r.status_code, 404, f"oversized page id must be a bounded 404, got {r.status_code}"
        )
        # Query length cap.
        self._call("retrieval", {"query": "x" * 1001, "vault_id": 1}, expect_status=400)
        self._call("retrieval", {"query": "x" * 1000, "vault_id": 1})
        # A NUL inside the query must be bounded (never a 500).
        r = self._call(
            "retrieval", {"query": "q\x00probe", "vault_id": 1}, expect_status=(200, 400)
        )
        self.assertIn(
            r.status_code, (200, 400), f"a NUL query must be bounded, got {r.status_code}"
        )
        # Unknown top-level envelope keys are rejected, not silently ignored.
        r = self.client.post(
            "/api/mcp/tools/call",
            json={"name": "wiki_read", "arguments": {"page_id": 1}, "extra": 1},
            headers=self._auth,
        )
        self.assertEqual(
            r.status_code, 422, f"unknown top-level keys must be rejected, got {r.status_code}"
        )
        # The swapped routes' own vault_id parameter carries the same bound:
        # an oversized value is a validation error, never a SQLite overflow.
        r = self.client.get(
            "/api/wiki/pages", params={"vault_id": 2**63}, headers=self._auth
        )
        self.assertEqual(
            r.status_code, 422, f"oversized vault_id must be rejected, got {r.status_code}"
        )
        r = self.client.get(
            "/api/search/unified",
            params={"q": _MARKER, "vault_id": 2**63},
            headers=self._auth,
        )
        self.assertEqual(
            r.status_code, 422, f"oversized vault_id must be rejected, got {r.status_code}"
        )
        # The upper valid boundary is accepted (empty result, not a 500).
        # Vault ids are INTEGER PRIMARY KEY, so every legitimate id is >= 1.
        self._spy_policy()
        r = self.client.get(
            "/api/wiki/pages", params={"vault_id": 2**63 - 1}, headers=self._auth
        )
        self.assertEqual(
            r.status_code, 200, f"the max valid vault_id must be accepted, got {r.status_code}"
        )
        r = self.client.get(
            "/api/search/unified",
            params={"q": _MARKER, "vault_id": 2**63 - 1},
            headers=self._auth,
        )
        self.assertEqual(
            r.status_code,
            200,
            f"the max valid vault_id must be accepted on unified search, got {r.status_code}",
        )

    # Leg 17: wiki subsystem master switch ----------------------------------

    def test_leg17_wiki_disabled_blocks_adapter_wiki_read(self):
        original = settings.wiki_enabled
        settings.wiki_enabled = False
        try:
            r = self._call("wiki_read", {"page_id": 1}, expect_status=503)
            self.assertEqual(
                r.status_code,
                503,
                "wiki_read must honor the wiki subsystem master switch the wiki "
                f"router enforces, got {r.status_code}: {r.text[:200]}",
            )
            # The adapter surface itself (tool listing) stays available: only
            # the wiki_read call is gated, matching require_wiki_enabled.
            r = self.client.get("/api/mcp/tools", headers=self._auth)
            self.assertEqual(r.status_code, 200)
            self.assertEqual(
                sorted(t["name"] for t in r.json()["tools"]),
                ["retrieval", "wiki_read"],
            )
        finally:
            settings.wiki_enabled = original

    # Leg 18: policy-evaluator fault fails closed ---------------------------

    def test_leg18_policy_evaluator_fault_fails_closed(self):
        async def exploding(principal, resource_type, resource_id, action):
            raise RuntimeError("policy backend unavailable")

        app.dependency_overrides[get_evaluate_policy] = lambda: exploding
        embed_before = self._embed.calls
        search_before = len(self._vstore.calls)
        r = self._call("retrieval", {"query": "probe", "vault_id": 1})
        self.assertEqual(
            r.status_code, 200, "a policy-evaluator fault must not escape as a 500"
        )
        body = r.json()["result"]
        self.assertFalse(body["success"], "a policy fault must report failure")
        self.assertNotIn(_MARKER, body["output"], "a policy fault must return no content")
        self.assertEqual(
            self._embed.calls,
            embed_before,
            "no retrieval may execute after a policy-evaluator fault",
        )
        self.assertEqual(len(self._vstore.calls), search_before)

    # Leg 19: the store read runs off the loop and loads no relations -------

    def test_leg19_store_read_off_loop_without_relations(self):
        from app.services.wiki_store import WikiStore

        seen = {}
        real_get_page = WikiStore.get_page

        def recording_get_page(self, page_id, load_relations=True):
            seen["thread"] = threading.get_ident()
            seen["load_relations"] = load_relations
            return real_get_page(self, page_id, load_relations=load_relations)

        relation_calls = []

        def recording_relation(self, *args, **kwargs):
            relation_calls.append(1)
            return []

        loop_thread = {}

        async def spy(principal, resource_type, resource_id, action):
            loop_thread["ident"] = threading.get_ident()
            return True

        app.dependency_overrides[get_evaluate_policy] = lambda: spy
        page_patcher = patch.object(WikiStore, "get_page", recording_get_page)
        page_patcher.start()
        self.addCleanup(page_patcher.stop)
        for name in ("list_claims", "list_entities", "list_lint_findings"):
            relation_patcher = patch.object(WikiStore, name, recording_relation)
            relation_patcher.start()
            self.addCleanup(relation_patcher.stop)

        r = self._call("wiki_read", {"page_id": 1})
        self.assertTrue(r.json()["result"]["success"])
        self.assertIn(_MARKER, r.json()["result"]["output"])
        self.assertEqual(
            relation_calls, [], "the tool reads only the page row; relations must not load"
        )
        self.assertFalse(
            seen["load_relations"], "the tool must request the page without relations"
        )
        self.assertNotEqual(
            seen["thread"],
            loop_thread["ident"],
            "the synchronous store read must run off the event-loop thread",
        )

    # Leg 20: human JWT failure keeps its own 401 contract ------------------

    def test_leg20_human_jwt_failure_keeps_token_specific_401(self):
        for path, kwargs in (
            ("/api/wiki/pages", {"params": {"vault_id": 1}}),
            ("/api/wiki/pages/1", {}),
            ("/api/search/unified", {"params": {"q": _MARKER}}),
        ):
            r = self.client.get(
                path, headers={"Authorization": "Bearer not-a-valid-jwt"}, **kwargs
            )
            self.assertEqual(r.status_code, 401, f"{path}: {r.status_code} {r.text[:200]}")
            self.assertEqual(
                r.headers.get("www-authenticate"),
                "Bearer",
                f"{path}: the human-path 401 must keep its WWW-Authenticate challenge",
            )
            self.assertNotEqual(
                r.json().get("detail"),
                "Not authenticated",
                f"{path}: the human-path 401 must keep its token-specific detail",
            )

    # Leg 21: an allowed retrieval is actually successful -------------------

    def test_leg21_successful_retrieval_reports_success(self):
        """Non-frozen positive control for the frozen AC13 shape.

        The frozen check accepts any 200 carrying a "result" key, so a
        failure envelope (success=false) would satisfy it. This leg pins
        that an ALLOWED retrieval really succeeds and really returns
        content through the no-engine path.
        """
        self._spy_policy()
        r = self._call("retrieval", {"query": "probe", "vault_id": 1})
        self.assertEqual(r.status_code, 200)
        body = r.json()["result"]
        self.assertTrue(body["success"], "an allowed retrieval must report success")
        self.assertTrue(body["output"], "an allowed retrieval must return output")
        self.assertTrue(body["sources"], "an allowed retrieval must return sources")


if __name__ == "__main__":
    unittest.main()
