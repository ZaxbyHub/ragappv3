"""Arm's-length acceptance checks for #804 (Workstream E tail, trace Q02 part 1).

Binds AC1-AC5 of .agents/issue-traces/804-805-workstream-e-tail/01-issue-summary.md:
service-account (sak_) principals must reach the read-only wiki surface and
unified search through the existing combined dependency
(app.api.deps.get_current_user_or_service_account), with the vault-read
policy invoked on the SA wiki path and the existing documents:read scope
check rejecting write-only keys with 403.

Frozen check contract (the implementer makes these green WITHOUT editing
this file):

- AC1: a documents:read sak_ key gets 200 on GET /api/wiki/pages?vault_id=V.
- AC2: the same key gets 200 on GET /api/wiki/pages/{page_id} with that page
  id present in the body.
- AC3: the same key gets 200 on GET /api/search/unified?q=<term>.
- AC4: listing wiki pages for vault V with a get_evaluate_policy override
  spy records EXACTLY one ("vault", V, "read") evaluation.
- AC5: a documents:write-only sak_ key gets 403 (not 401) on the wiki list.

The SA legs deliberately install NO dependency override on
get_current_active_user (the combined dependency short-circuits on such an
override, which would make these checks vacuous) and none on
get_evaluate_policy except the AC4 spy leg.
"""

import hashlib
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

# Add backend/ and backend/tests/ to the import path (arm's-length checks are
# collected from the repo-root pytest invocation: cd backend && pytest tests/...).
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
    import types

    sys.modules["lancedb"] = types.ModuleType("lancedb")

try:
    import pyarrow  # noqa: F401
except ImportError:
    import types

    sys.modules["pyarrow"] = types.ModuleType("pyarrow")

try:
    from unstructured.partition.auto import partition  # noqa: F401
except ImportError:
    import types

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
    sys.modules["unstructured"] = _unstructured
    sys.modules["unstructured.partition"] = _unstructured.partition
    sys.modules["unstructured.partition.auto"] = _unstructured.partition.auto
    sys.modules["unstructured.chunking"] = _unstructured.chunking
    sys.modules["unstructured.chunking.title"] = _unstructured.chunking.title
    sys.modules["unstructured.documents"] = _unstructured.documents
    sys.modules["unstructured.documents.elements"] = _unstructured.documents.elements

from _db_pool import SimpleConnectionPool  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.api.deps import (  # noqa: E402
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


class TestSAWikiReads(unittest.TestCase):
    """AC1-AC5: service accounts on wiki reads and unified search."""

    def setUp(self):
        # Settings prerequisites (conftest sets USERS_ENABLED=false globally;
        # the SA path in get_current_user_or_service_account is only reachable
        # with users_enabled=True).
        self._original_jwt_env = os.environ.get("JWT_SECRET_KEY")
        os.environ["JWT_SECRET_KEY"] = "test-sa-wiki-reads-key-32chars-min!"
        self._temp_dir = tempfile.mkdtemp()
        self._db_path = str(Path(self._temp_dir) / "app.db")

        self._original_jwt_secret = settings.jwt_secret_key
        self._original_users_enabled = settings.users_enabled
        self._original_data_dir = settings.data_dir
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
        mock_embedding = MagicMock()
        mock_embedding.embed_single = AsyncMock(return_value=[0.1, 0.2, 0.3])
        # Only the deps the wiki/unified-search read paths consume. NOTE: no
        # override on get_current_active_user or get_evaluate_policy here -
        # the combined dependency returns a get_current_active_user override
        # verbatim before the SA branch, which would make AC1-AC5 vacuous.
        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_vector_store] = lambda: mock_vector_store
        app.dependency_overrides[get_embedding_service] = lambda: mock_embedding
        self._overridden_deps = [get_db, get_vector_store, get_embedding_service]

        # Fixtures: one vault, one wiki page, one read-scope and one
        # write-only-scope service account (sha256 key hashes, as production).
        self._vault_id = 1
        self._page_id = 1
        self._page_markdown = "AC1-AC5 probe page body with unique marker 6f4c."
        self._sa_read_key = "sak_q02_wiki_read_key_000000000000001"
        self._sa_write_key = "sak_q02_wiki_write_key_00000000000001"
        now = datetime.now(timezone.utc).isoformat()
        conn = self._connection_pool.get_connection()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO vaults (id, name) VALUES (?, ?)",
                (self._vault_id, "Q02Vault"),
            )
            conn.execute(
                "INSERT INTO wiki_pages (id, vault_id, title, slug, page_type, status, markdown) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    self._page_id,
                    self._vault_id,
                    "Q02 Probe Page",
                    "q02-probe-page",
                    "overview",
                    "verified",
                    self._page_markdown,
                ),
            )
            conn.execute(
                "INSERT INTO service_accounts (name, key_hash, scopes, created_at) VALUES (?, ?, ?, ?)",
                (
                    "Q02-Read",
                    hashlib.sha256(self._sa_read_key.encode()).hexdigest(),
                    "documents:read",
                    now,
                ),
            )
            conn.execute(
                "INSERT INTO service_accounts (name, key_hash, scopes, created_at) VALUES (?, ?, ?, ?)",
                (
                    "Q02-Write",
                    hashlib.sha256(self._sa_write_key.encode()).hexdigest(),
                    "documents:write",
                    now,
                ),
            )
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)

        self.client = TestClient(app, raise_server_exceptions=False)
        self.client.headers["user-agent"] = ""
        self._auth_read = {"Authorization": f"Bearer {self._sa_read_key}"}
        self._auth_write = {"Authorization": f"Bearer {self._sa_write_key}"}

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
        self.client.close()
        self._connection_pool.close_all()
        shutil.rmtree(self._temp_dir, ignore_errors=True)
        if self._original_jwt_env is not None:
            os.environ["JWT_SECRET_KEY"] = self._original_jwt_env
        else:
            os.environ.pop("JWT_SECRET_KEY", None)

    # AC1 ---------------------------------------------------------------

    def test_sa_read_key_wiki_list_returns_200(self):
        """AC1: documents:read sak_ key gets 200 on GET /api/wiki/pages."""
        response = self.client.get(
            "/api/wiki/pages",
            params={"vault_id": self._vault_id},
            headers=self._auth_read,
        )
        self.assertEqual(
            response.status_code,
            200,
            f"AC1: expected 200 for documents:read SA on GET /api/wiki/pages, "
            f"got {response.status_code}: {response.text[:300]}",
        )

    # AC2 ---------------------------------------------------------------

    def test_sa_read_key_wiki_detail_returns_200_with_page_id(self):
        """AC2: the same key gets 200 with the page id on wiki detail."""
        response = self.client.get(
            f"/api/wiki/pages/{self._page_id}",
            headers=self._auth_read,
        )
        self.assertEqual(
            response.status_code,
            200,
            f"AC2: expected 200 for documents:read SA on GET /api/wiki/pages/"
            f"{self._page_id}, got {response.status_code}: {response.text[:300]}",
        )
        body = response.json()
        self.assertEqual(
            body.get("id"),
            self._page_id,
            f"AC2: expected page id {self._page_id} in body, got: {str(body)[:300]}",
        )

    # AC3 ---------------------------------------------------------------

    def test_sa_read_key_unified_search_returns_200(self):
        """AC3: the same key gets 200 on GET /api/search/unified."""
        response = self.client.get(
            "/api/search/unified",
            params={"q": "probe", "vault_id": self._vault_id},
            headers=self._auth_read,
        )
        self.assertEqual(
            response.status_code,
            200,
            f"AC3: expected 200 for documents:read SA on GET /api/search/unified, "
            f"got {response.status_code}: {response.text[:300]}",
        )

    # AC4 ---------------------------------------------------------------

    def test_sa_wiki_list_invokes_vault_read_policy_once(self):
        """AC4: the SA wiki-list path invokes the vault-read policy exactly once.

        A get_evaluate_policy dependency-override spy (async, returns True)
        records ("vault", V, "read") exactly once for the list request.
        """
        policy_calls = []

        async def spy_evaluate(principal, resource_type, resource_id, action):
            policy_calls.append((resource_type, resource_id, action))
            return True

        app.dependency_overrides[get_evaluate_policy] = lambda: spy_evaluate
        try:
            response = self.client.get(
                "/api/wiki/pages",
                params={"vault_id": self._vault_id},
                headers=self._auth_read,
            )
            self.assertEqual(
                response.status_code,
                200,
                f"AC4: expected 200 for documents:read SA on GET /api/wiki/pages, "
                f"got {response.status_code}: {response.text[:300]}",
            )
            self.assertEqual(
                policy_calls,
                [("vault", self._vault_id, "read")],
                "AC4: the vault-read policy must be invoked exactly once with "
                f"('vault', {self._vault_id}, 'read'); observed: {policy_calls}",
            )
        finally:
            app.dependency_overrides.pop(get_evaluate_policy, None)

    # AC5 ---------------------------------------------------------------

    def test_sa_write_only_key_wiki_list_returns_403(self):
        """AC5: a documents:write-only sak_ key gets 403 (scope denial) on the wiki list."""
        response = self.client.get(
            "/api/wiki/pages",
            params={"vault_id": self._vault_id},
            headers=self._auth_write,
        )
        self.assertEqual(
            response.status_code,
            403,
            f"AC5: expected 403 for documents:write-only SA on GET /api/wiki/pages "
            f"(the existing scope check must run), got {response.status_code}: "
            f"{response.text[:300]}",
        )


if __name__ == "__main__":
    unittest.main()
