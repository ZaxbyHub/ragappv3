"""C13 acceptance check — unified search LIKE wildcard escaping.

AC6: WHEN ``GET /api/search/unified?types=chat&q=%`` with chat sessions titled
"alpha" and "beta" THE SYSTEM SHALL return 0 chat results.

At base the chat arm interpolates the raw query into a LIKE pattern
(``f"%{query_lower}%"`` in the unified search handler) without escaping, so a
literal ``%`` in the query becomes a wildcard pair that matches EVERY titled
session — both "alpha" and "beta" come back. RED at base (master), GREEN after
the fix.

Route-level setup (init_db / run_migrations / pooled get_db override) mirrors
backend/tests/test_issue515_unified_search.py::TestAC45UnifiedSearchEndpoint.
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


class TestUnifiedLikeEscape(unittest.TestCase):
    """AC6: a literal % in the unified-search query is not a wildcard."""

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

        # One vault with two chat sessions whose titles contain no % at all.
        conn = self._connection_pool.get_connection()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO vaults (id, name, description) VALUES (1, 'V1', '')"
            )
            conn.execute(
                "INSERT INTO chat_sessions (vault_id, user_id, title) VALUES (1, NULL, ?)",
                ("alpha",),
            )
            conn.execute(
                "INSERT INTO chat_sessions (vault_id, user_id, title) VALUES (1, NULL, ?)",
                ("beta",),
            )
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)

    def tearDown(self):
        for dep in self._deps:
            app.dependency_overrides.pop(dep, None)
        self._connection_pool.close_all()
        shutil.rmtree(self._temp_dir, ignore_errors=True)

    def test_percent_is_literal_in_chat_title_search(self):
        """AC6: WHEN GET /api/search/unified?types=chat&q=% with chat sessions
        titled "alpha" and "beta" THE SYSTEM SHALL return 0 chat results.
        RED at base (master), GREEN after the fix."""
        response = self.client.get(
            "/api/search/unified",
            params={"q": "%", "types": "chat", "vault_id": 1},
        )
        self.assertEqual(
            response.status_code,
            200,
            f"GET /api/search/unified must return 200, got "
            f"{response.status_code}: {response.text}",
        )
        results = response.json()["results"]
        chat_results = [r for r in results if r.get("type") == "chat"]
        # A literal percent must not act as a LIKE wildcard: neither "alpha"
        # nor "beta" contains a %, so the chat arm must return nothing.
        self.assertEqual(
            len(chat_results),
            0,
            f"literal '%%' matched {len(chat_results)} chat session(s) as a wildcard: "
            f"{chat_results}",
        )


if __name__ == "__main__":
    unittest.main()
