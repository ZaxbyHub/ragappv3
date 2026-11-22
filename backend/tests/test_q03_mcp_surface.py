"""Arm's-length acceptance checks for #805 (Workstream E tail, trace Q03).

Binds AC10-AC17 of .agents/issue-traces/804-805-workstream-e-tail/01-issue-summary.md.
Pinned contract (the implementer makes these green WITHOUT editing this file):

- ``import app.services.mcp_server`` (NEW-SURFACE: no collection at base).
- Module attribute ``router`` is an APIRouter with exactly two routes
  (GET /mcp/tools, POST /mcp/tools/call), mounted under the "/api" prefix so
  the HTTP legs call /api/mcp/tools and /api/mcp/tools/call with a
  documents:read sak_ key (no get_current_active_user override).
- GET /api/mcp/tools returns {"tools": [{"name", "description",
  "input_schema"}...}] listing EXACTLY {"retrieval", "wiki_read"}; each
  input_schema is a JSON-schema object with non-empty "properties"; the
  listing still works with settings.agentic_rag_enabled=False.
- POST /api/mcp/tools/call ({"name": str, "arguments": dict}) returns 200
  {"result": {...}}; "synthesis" is rejected 400/404 with SynthesisTool.execute
  never called.
- Every tool invocation goes through the vault-read policy: a
  get_evaluate_policy spy records ("vault", V, "read") for retrieval
  (vault_id argument) and wiki_read (page_id argument).
- AC15 seam: ``build_result_payload(tool_name, tool_result) -> dict`` returns
  the {"result": {...}} payload body; its "output" string embeds each source
  label as f"[{label}]" so parse_citations ([S#]) and parse_wiki_citations
  ([W#]) round-trip the label back to the same string.
- AC16: at least one mounted route whose endpoint is defined in
  app.services.mcp_server carries app.api.deps.get_current_user_or_service_account
  in its dependency tree.
"""

import hashlib
import os
import shutil
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Add backend/ and backend/tests/ to the import path.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Settings() prerequisites when this module is imported outside conftest.
os.environ.setdefault("JWT_SECRET_KEY", "test-sa-wiki-reads-key-32chars-min!")
os.environ.setdefault("ADMIN_SECRET_TOKEN", "test-admin-key")
os.environ.setdefault("REDIS_URL", "")

# Stub missing optional dependencies (same preamble as
# backend/tests/test_service_account_authenticates_to_routes.py).
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
    _unstructured = types.ModuleType("unstructured")
    _unstructured.__path__ = []
    _unstructured.partition = types.ModuleType("unstructured.partition")
    _unstructured.partition.__path__ = []
    _unstructured.partition.auto = types.ModuleType("unstructured.partition.auto")
    _unstructured.partition.auto.partition = lambda *args, **kwargs: []
    _unstructured.chunking = types.ModuleType("unstructured.chunking")
    _unstructured.chunking.__path__ = []
    _unstructured.chunking.title = types.ModuleType("unstructured.chunking.title")
    _unstructured.chunking.title.chunk_by_title = lambda *args, **kwargs: []
    _unstructured.documents = types.ModuleType("unstructured.documents")
    _unstructured.documents.__path__ = []
    _unstructured.documents.elements = types.ModuleType("unstructured.documents.elements")
    _unstructured.documents.elements.Element = type("Element", (), {})
    sys.modules.update({
        "unstructured": _unstructured,
        "unstructured.partition": _unstructured.partition,
        "unstructured.partition.auto": _unstructured.partition.auto,
        "unstructured.chunking": _unstructured.chunking,
        "unstructured.chunking.title": _unstructured.chunking.title,
        "unstructured.documents": _unstructured.documents,
        "unstructured.documents.elements": _unstructured.documents.elements,
    })

from _db_pool import SimpleConnectionPool  # noqa: E402
from fastapi.routing import APIRouter  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.api.deps import (  # noqa: E402
    get_current_user_or_service_account,
    get_db,
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
from app.services.agentic_tools import SynthesisTool, ToolResult  # noqa: E402
from app.services.citation_validator import (  # noqa: E402
    parse_citations,
    parse_wiki_citations,
)

# NEW-SURFACE import: fails at base (ModuleNotFoundError) - that collection
# failure is the expected base evidence for this check file.
import app.services.mcp_server as mcp_server  # noqa: E402


def _dependency_calls(route):
    """Every callable in a route's dependency tree (endpoint + sub-deps).

    FastAPI 0.139 stores sub-dependencies as flat ``Dependant`` objects in
    ``dependant.dependencies`` (no ``.dependant`` wrapper attribute), so
    the walk extends the stack with the dependency entries directly.
    CHECK_WRONG amendment (trace 804-805): the original helper appended
    ``dep.dependant``, which raises AttributeError for any route that has
    dependencies — the check could never execute against a real
    implementation. The tree-walk semantics are unchanged.
    """
    calls = []
    stack = [route.dependant]
    while stack:
        dependant = stack.pop()
        calls.append(dependant.call)
        stack.extend(dependant.dependencies)
    return calls


class TestMCPSurface(unittest.TestCase):
    """AC10-AC17: the MCP read-only adapter surface."""

    def setUp(self):
        self._original_jwt_env = os.environ.get("JWT_SECRET_KEY")
        os.environ["JWT_SECRET_KEY"] = "test-sa-wiki-reads-key-32chars-min!"
        self._temp_dir = tempfile.mkdtemp()
        self._db_path = str(Path(self._temp_dir) / "app.db")

        self._original_jwt_secret = settings.jwt_secret_key
        self._original_users_enabled = settings.users_enabled
        self._original_data_dir = settings.data_dir
        self._original_agentic_rag = settings.agentic_rag_enabled
        settings.users_enabled = True
        settings.data_dir = Path(self._temp_dir)
        settings.jwt_secret_key = os.environ["JWT_SECRET_KEY"]

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

        mock_vector_store = MagicMock()
        mock_vector_store._ready = True
        # NOTE: no override on get_current_active_user - the adapter routes
        # must accept the sak_ service-account path on their own.
        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_vector_store] = lambda: mock_vector_store
        self._overridden_deps = [get_db, get_vector_store]

        self._vault_id = 1
        self._page_id = 1
        self._sa_read_key = "sak_q03_mcp_read_key_000000000000001"
        now = datetime.now(timezone.utc).isoformat()
        conn = self._connection_pool.get_connection()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO vaults (id, name) VALUES (?, ?)",
                (self._vault_id, "Q03Vault"),
            )
            conn.execute(
                "INSERT INTO wiki_pages (id, vault_id, title, slug, page_type, status, markdown) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (self._page_id, self._vault_id, "Q03 Probe Page", "q03-probe-page",
                 "overview", "verified", "Q03 adapter probe page body."),
            )
            conn.execute(
                "INSERT INTO service_accounts (name, key_hash, scopes, created_at) VALUES (?, ?, ?, ?)",
                ("Q03-Read", hashlib.sha256(self._sa_read_key.encode()).hexdigest(),
                 "documents:read", now),
            )
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)

        self.client = TestClient(app, raise_server_exceptions=False)
        self.client.headers["user-agent"] = ""
        self._auth_read = {"Authorization": f"Bearer {self._sa_read_key}"}

    def tearDown(self):
        for dep in self._overridden_deps:
            app.dependency_overrides.pop(dep, None)
        app.dependency_overrides.pop(get_evaluate_policy, None)
        with _pool_cache_lock:
            for _path, pool in list(_pool_cache.items()):
                pool.close_all()
            _pool_cache.clear()
        settings.jwt_secret_key = self._original_jwt_secret
        settings.users_enabled = self._original_users_enabled
        settings.data_dir = self._original_data_dir
        settings.agentic_rag_enabled = self._original_agentic_rag
        self.client.close()
        self._connection_pool.close_all()
        shutil.rmtree(self._temp_dir, ignore_errors=True)
        if self._original_jwt_env is not None:
            os.environ["JWT_SECRET_KEY"] = self._original_jwt_env
        else:
            os.environ.pop("JWT_SECRET_KEY", None)

    def _install_policy_spy(self):
        """Override get_evaluate_policy with an allow-all spy; returns the log."""
        policy_calls = []

        async def spy_evaluate(principal, resource_type, resource_id, action):
            policy_calls.append((resource_type, resource_id, action))
            return True

        app.dependency_overrides[get_evaluate_policy] = lambda: spy_evaluate
        return policy_calls

    def _list_tools(self):
        response = self.client.get("/api/mcp/tools", headers=self._auth_read)
        self.assertEqual(
            response.status_code,
            200,
            f"GET /api/mcp/tools must return 200 for a documents:read SA key, "
            f"got {response.status_code}: {response.text[:300]}",
        )
        body = response.json()
        self.assertIn(
            "tools",
            body,
            f"GET /api/mcp/tools body must carry a 'tools' key; got: {str(body)[:300]}",
        )
        return body["tools"]

    # AC10 --------------------------------------------------------------

    def test_tools_listing_is_exact_allowlist(self):
        """AC10: the adapter enumerates exactly {'retrieval', 'wiki_read'}."""
        router = getattr(mcp_server, "router", None)
        self.assertIsInstance(
            router,
            APIRouter,
            "AC10: app.services.mcp_server must expose an APIRouter named 'router'",
        )
        pairs = set()
        for route in router.routes:
            for method in getattr(route, "methods", None) or ():
                if method == "HEAD":
                    continue
                pairs.add((method, route.path))
        self.assertEqual(
            pairs,
            {("GET", "/mcp/tools"), ("POST", "/mcp/tools/call")},
            f"AC10: the adapter router must carry exactly GET /mcp/tools and "
            f"POST /mcp/tools/call; observed: {sorted(pairs)}",
        )
        names = {tool.get("name") for tool in self._list_tools()}
        self.assertEqual(
            names,
            {"retrieval", "wiki_read"},
            f"AC10: the adapter must list exactly the allowlisted tools; "
            f"observed: {sorted(str(n) for n in names)}",
        )

    # AC11 --------------------------------------------------------------

    def test_synthesis_tool_rejected_and_not_executed(self):
        """AC11: 'synthesis' is rejected as unknown and never executed."""
        with patch.object(SynthesisTool, "execute", new_callable=AsyncMock) as spy:
            response = self.client.post(
                "/api/mcp/tools/call",
                json={"name": "synthesis", "arguments": {"text": "must not run"}},
                headers=self._auth_read,
            )
            self.assertIn(
                response.status_code,
                (400, 404),
                f"AC11: 'synthesis' must be rejected as an unknown tool with "
                f"400/404, got {response.status_code}: {response.text[:300]}",
            )
            self.assertEqual(
                spy.await_count,
                0,
                "AC11: SynthesisTool.execute must never be called through the adapter",
            )

    # AC12 --------------------------------------------------------------

    def test_tool_input_schemas_are_json_objects(self):
        """AC12: each tool carries a JSON-schema object input with properties."""
        tools = self._list_tools()
        self.assertTrue(tools, "AC12: the adapter must list at least one tool")
        for tool in tools:
            schema = tool.get("input_schema")
            self.assertIsInstance(
                schema, dict,
                f"AC12: {tool.get('name')!r} input_schema must be a dict; got {schema!r}",
            )
            self.assertEqual(
                schema.get("type"),
                "object",
                f"AC12: {tool.get('name')!r} input_schema top-level type must be "
                f"'object'; got {schema.get('type')!r}",
            )
            properties = schema.get("properties")
            self.assertIsInstance(
                properties, dict,
                f"AC12: {tool.get('name')!r} input_schema.properties must be a dict; "
                f"got {properties!r}",
            )
            self.assertTrue(
                properties,
                f"AC12: {tool.get('name')!r} input_schema.properties must be non-empty",
            )

    # AC13 --------------------------------------------------------------

    def test_retrieval_call_invokes_vault_read_policy(self):
        """AC13: an SA retrieval call for vault V hits the vault-read policy."""
        policy_calls = self._install_policy_spy()
        try:
            response = self.client.post(
                "/api/mcp/tools/call",
                json={"name": "retrieval",
                      "arguments": {"query": "probe", "vault_id": self._vault_id}},
                headers=self._auth_read,
            )
            self.assertEqual(
                response.status_code,
                200,
                f"AC13: retrieval call must answer 200 for a documents:read SA key, "
                f"got {response.status_code}: {response.text[:300]}",
            )
            self.assertIn(
                "result",
                response.json(),
                "AC13: a successful tool call must return a 'result' envelope",
            )
            self.assertIn(
                ("vault", self._vault_id, "read"),
                policy_calls,
                f"AC13: the adapter must invoke the vault-read policy for the "
                f"retrieval tool; observed: {policy_calls}",
            )
        finally:
            app.dependency_overrides.pop(get_evaluate_policy, None)

    # AC14 --------------------------------------------------------------

    def test_wiki_read_call_invokes_vault_read_policy(self):
        """AC14: an SA wiki_read call for a page in vault V hits the policy."""
        policy_calls = self._install_policy_spy()
        try:
            response = self.client.post(
                "/api/mcp/tools/call",
                json={"name": "wiki_read", "arguments": {"page_id": self._page_id}},
                headers=self._auth_read,
            )
            self.assertEqual(
                response.status_code,
                200,
                f"AC14: wiki_read call must answer 200 for a documents:read SA key, "
                f"got {response.status_code}: {response.text[:300]}",
            )
            self.assertIn(
                ("vault", self._vault_id, "read"),
                policy_calls,
                f"AC14: the adapter must invoke the vault-read policy for the "
                f"wiki_read tool; observed: {policy_calls}",
            )
        finally:
            app.dependency_overrides.pop(get_evaluate_policy, None)

    # AC15 --------------------------------------------------------------

    def test_result_labels_round_trip_through_citation_parser(self):
        """AC15: adapter result labels round-trip through citation parsing."""
        retrieval_payload = mcp_server.build_result_payload(
            "retrieval",
            ToolResult(
                output="Retrieved 1 sources",
                sources=[{"source_label": "S7", "file_id": "f1", "score": 0.9}],
                success=True,
            ),
        )
        self.assertIsInstance(retrieval_payload, dict)
        self.assertIn(
            "output", retrieval_payload,
            "AC15: the result payload must carry an 'output' string",
        )
        sources_cited, _ = parse_citations(retrieval_payload["output"])
        self.assertIn(
            "S7",
            sources_cited,
            f"AC15: a retrieval source label must round-trip through "
            f"parse_citations ([S#]); output was {retrieval_payload['output']!r}",
        )

        wiki_payload = mcp_server.build_result_payload(
            "wiki_read",
            ToolResult(
                output="Read 1 wiki page",
                sources=[{"source_label": "W3", "page_id": 1}],
                success=True,
            ),
        )
        self.assertIsInstance(wiki_payload, dict)
        self.assertIn(
            "output", wiki_payload,
            "AC15: the result payload must carry an 'output' string",
        )
        wiki_cited = parse_wiki_citations(wiki_payload["output"])
        self.assertIn(
            "W3",
            wiki_cited,
            f"AC15: a wiki_read source label must round-trip through "
            f"parse_wiki_citations ([W#]); output was {wiki_payload['output']!r}",
        )

    # AC16 --------------------------------------------------------------

    def test_mcp_routes_use_combined_service_account_dependency(self):
        """AC16: adapter routes carry the combined SA dependency in their tree."""
        mcp_routes = [
            route
            for route in app.routes
            if getattr(getattr(route, "endpoint", None), "__module__", None)
            == "app.services.mcp_server"
        ]
        self.assertTrue(
            mcp_routes,
            "AC16: at least one mounted route must have its endpoint defined "
            "in app.services.mcp_server",
        )
        combined_calls = [c for r in mcp_routes for c in _dependency_calls(r)]
        self.assertIn(
            get_current_user_or_service_account,
            combined_calls,
            "AC16: an adapter route's dependency tree must include "
            "get_current_user_or_service_account",
        )

    # AC17 --------------------------------------------------------------

    def test_tools_listed_when_agentic_rag_disabled(self):
        """AC17: the tool listing ignores settings.agentic_rag_enabled."""
        settings.agentic_rag_enabled = False
        try:
            tools = self._list_tools()
        finally:
            settings.agentic_rag_enabled = self._original_agentic_rag
        names = {tool.get("name") for tool in tools}
        self.assertEqual(
            names,
            {"retrieval", "wiki_read"},
            f"AC17: both allowlisted tools must still be listed with "
            f"agentic_rag_enabled=False; observed: {sorted(str(n) for n in names)}",
        )


if __name__ == "__main__":
    unittest.main()
