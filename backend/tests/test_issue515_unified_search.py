"""Issue #515 acceptance check — unified discovery endpoint (PRODUCT-ENH-11).

AC45 (backend half): GET /api/search/unified is a design-frozen contract for
the implementer. THE ENDPOINT DOES NOT EXIST YET — this test pins the contract
shape exactly, so at base it fails with 404 (correct RED):

    GET /api/search/unified
      ? q        (required)
      & vault_id (optional)
      & types    (comma list subset of document,wiki,kms,chat; default all)
      & limit    (optional)

    200 -> {"results": [{"type": "document|wiki|kms|chat", "id": ...,
            "title": ..., "snippet": ..., "vault_id": ..., "url_hint": ...,
            "score": ...}]}

reusing the existing per-entity search logic (documents FTS list-search,
/wiki/search, KMS search, chat session title search).
"""

import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _stub_optional_modules() -> None:
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

from app.api.deps import (  # noqa: E402
    get_current_active_user,
    get_db,
    get_evaluate_policy,
    get_vector_store,
)
from app.main import app  # noqa: E402
from app.models.database import init_db, run_migrations  # noqa: E402

_TERM = "zephyr"
_ALLOWED_TYPES = {"document", "wiki", "kms", "chat"}
_RESULT_KEYS = {"id", "type", "title", "snippet", "vault_id", "url_hint", "score"}


class TestAC45UnifiedSearchEndpoint(unittest.TestCase):
    """AC45: unified cross-entity search contract (route-level)."""

    def setUp(self):
        self.client = TestClient(app)
        self._temp_dir = tempfile.mkdtemp()
        self._db_path = str(Path(self._temp_dir) / "app.db")
        init_db(self._db_path)
        run_migrations(self._db_path)
        self._connection_pool = SimpleConnectionPool(self._db_path)

        def override_get_db():
            conn = self._connection_pool.get_connection()
            try:
                yield conn
            finally:
                self._connection_pool.release_connection(conn)

        async def allow_policy(user, resource_type, resource_id, action):
            return True

        ready_vector_store = MagicMock()
        ready_vector_store._ready = True

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_active_user] = lambda: {
            "id": 0, "username": "admin", "role": "superadmin",
            "is_active": 1, "must_change_password": 0,
        }
        app.dependency_overrides[get_evaluate_policy] = lambda: allow_policy
        app.dependency_overrides[get_vector_store] = lambda: ready_vector_store
        self._deps = [get_db, get_current_active_user, get_evaluate_policy, get_vector_store]

        # One vault holding one matching entity of each type.
        conn = self._connection_pool.get_connection()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO vaults (id, name, description) VALUES (1, 'V1', '')"
            )
            conn.execute(
                "INSERT INTO files (file_name, file_path, file_size, status, "
                "chunk_count, vault_id, parsed_text) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    "unified_plain.txt", "/uploads/unified_plain.txt", 100,
                    "indexed", 1, 1,
                    f"internal notes on the {_TERM} deployment topology",
                ),
            )
            conn.execute(
                "INSERT INTO wiki_pages (vault_id, slug, title, page_type, markdown, summary, status) "
                "VALUES (1, 'zephyr-overview', ?, 'overview', ?, ?, 'draft')",
                (
                    f"{_TERM.capitalize()} Overview",
                    f"# {_TERM.capitalize()}\nEverything about the {_TERM} system.",
                    f"Overview of the {_TERM} system.",
                ),
            )
            conn.execute(
                "INSERT INTO kms_entries (vault_id, slug, title, body, summary, tags_json, "
                "source_type, status) VALUES (1, 'zephyr-runbook', ?, ?, '', '[]', 'manual', 'draft')",
                (
                    f"{_TERM.capitalize()} runbook",
                    f"operational steps for the {_TERM} cluster",
                ),
            )
            conn.execute(
                "INSERT INTO chat_sessions (vault_id, user_id, title) VALUES (1, NULL, ?)",
                (f"{_TERM.capitalize()} kickoff discussion",),
            )
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)

    def tearDown(self):
        for dep in self._deps:
            app.dependency_overrides.pop(dep, None)
        self._connection_pool.close_all()
        shutil.rmtree(self._temp_dir, ignore_errors=True)

    def test_issue515_ac45_unified_search_endpoint(self):
        response = self.client.get(
            "/api/search/unified", params={"q": _TERM, "vault_id": 1}
        )
        self.assertEqual(
            response.status_code,
            200,
            f"GET /api/search/unified must exist and return 200, got "
            f"{response.status_code}: {response.text}",
        )
        payload = response.json()
        self.assertIn("results", payload)
        results = payload["results"]
        self.assertTrue(results, "expected at least one result per matching entity")

        types_seen = {r.get("type") for r in results}
        self.assertEqual(
            types_seen,
            _ALLOWED_TYPES,
            f"all four entity types must be present for a cross-entity term, got {types_seen}",
        )
        for result in results:
            self.assertTrue(
                _RESULT_KEYS.issubset(result.keys()),
                f"result missing contract keys: {sorted(result.keys())}",
            )
            self.assertTrue(result["title"], "every result must carry a title")
            self.assertEqual(result["vault_id"], 1, "results must be vault-scoped")
            self.assertIn(result["type"], _ALLOWED_TYPES)

        # Type filter narrows to the requested entity kind.
        filtered = self.client.get(
            "/api/search/unified",
            params={"q": _TERM, "vault_id": 1, "types": "document"},
        )
        self.assertEqual(filtered.status_code, 200, filtered.text)
        filtered_results = filtered.json()["results"]
        self.assertTrue(filtered_results, "types=document must still return the document")
        self.assertEqual(
            {r["type"] for r in filtered_results}, {"document"},
            f"types=document filter must return only documents, got {filtered_results}",
        )

        # Unknown type value is rejected as a client error.
        invalid = self.client.get(
            "/api/search/unified",
            params={"q": _TERM, "vault_id": 1, "types": "bogus"},
        )
        self.assertIn(
            invalid.status_code, (400, 422),
            f"unknown types value must 4xx, got {invalid.status_code}: {invalid.text}",
        )


if __name__ == "__main__":
    unittest.main()
