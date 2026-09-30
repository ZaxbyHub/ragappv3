"""Issue #686 A04 acceptance checks — memory route transactional atomicity.

ACs covered here:
- AC1: a DELETE that fails mid-route must not leave claim stale-markings behind.
- AC2: a stale-marking failure must abort the DELETE itself (no partial work).
- AC3: an update whose claim invalidation fails must keep the old content.
"""

import os
import shutil
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import patch

import pytest

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

CSRF_TEST_POLICY = "naive"


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

from app.main import app  # noqa: E402
from app.models.database import (  # noqa: E402
    SQLiteConnectionPool,
    init_db,
    run_migrations,
)
from app.services.memory_store import MemoryStore  # noqa: E402


class _RouteEnv:
    """Per-test route environment: temp DB, pools, store, dependency overrides.

    Mirrors test_issue515_memories._MemoryRouteBase.setUp/tearDown.
    """

    def __init__(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = str(Path(self.temp_dir) / "test.db")
        init_db(self.db_path)
        run_migrations(self.db_path)

        self.store_pool = SQLiteConnectionPool(self.db_path, max_size=2)
        self.store = MemoryStore(pool=self.store_pool)
        self.pool = SimpleConnectionPool(self.db_path)
        self.client = TestClient(app)
        self.quiet_client = TestClient(app, raise_server_exceptions=False)

        from app.api.deps import (
            get_current_active_user,
            get_db,
            get_evaluate_policy,
            get_memory_store,
        )
        from app.security import csrf_protect

        self._deps = (
            get_db,
            get_memory_store,
            get_current_active_user,
            get_evaluate_policy,
            csrf_protect,
        )

        def override_get_db():
            conn = self.pool.get_connection()
            try:
                yield conn
            finally:
                self.pool.release_connection(conn)

        async def allow_policy(user, resource_type, resource_id, action):
            return True

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_memory_store] = lambda: self.store
        app.dependency_overrides[get_current_active_user] = lambda: {
            "id": 0, "username": "admin", "role": "superadmin",
            "is_active": 1, "must_change_password": 0,
        }
        app.dependency_overrides[get_evaluate_policy] = lambda: allow_policy
        app.dependency_overrides[csrf_protect] = lambda: "test-csrf-token"

        conn = self.pool.get_connection()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO vaults (id, name, description) VALUES (1, 'V1', '')"
            )
            conn.commit()
        finally:
            self.pool.release_connection(conn)

    def fetch_row(self, sql, params=()):
        conn = self.pool.get_connection()
        try:
            return conn.execute(sql, params).fetchone()
        finally:
            self.pool.release_connection(conn)

    def execute(self, sql, params=()):
        conn = self.pool.get_connection()
        try:
            conn.execute(sql, params)
            conn.commit()
        finally:
            self.pool.release_connection(conn)

    def seed_sole_source_claim(self, memory_id, text="a04 sole-source claim"):
        conn = self.pool.get_connection()
        try:
            cur = conn.execute(
                "INSERT INTO wiki_claims (vault_id, claim_text, source_type, status) "
                "VALUES (1, ?, 'memory', 'active')",
                (text,),
            )
            claim_id = cur.lastrowid
            conn.execute(
                "INSERT INTO wiki_claim_sources (claim_id, source_kind, memory_id) "
                "VALUES (?, 'memory', ?)",
                (claim_id, memory_id),
            )
            conn.commit()
            return claim_id
        finally:
            self.pool.release_connection(conn)

    def create_memory(self, content, **extra):
        payload = {"content": content, "vault_id": 1}
        payload.update(extra)
        created = self.client.post("/api/memories", json=payload)
        assert created.status_code == 200, created.text
        return int(created.json()["id"])

    def close(self):
        for dep in self._deps:
            app.dependency_overrides.pop(dep, None)
        self.store_pool.close_all()
        self.pool.close_all()
        shutil.rmtree(self.temp_dir, ignore_errors=True)


@pytest.fixture()
def env():
    test_env = _RouteEnv()
    yield test_env
    test_env.close()


def test_delete_failure_after_stale_marking_commits_nothing(env):
    """AC1: when the DELETE itself fails, the stale-marking done just before
    it must not persist — the claim must still be active."""
    memory_id = env.create_memory("ac1 claim content")
    claim_id = env.seed_sole_source_claim(memory_id, "ac1 seeded sole-source claim")

    env.execute(
        "CREATE TRIGGER fail_delete BEFORE DELETE ON memories "
        "BEGIN SELECT RAISE(ABORT, 'injected'); END"
    )

    resp = env.quiet_client.delete(f"/api/memories/{memory_id}")
    assert resp.status_code >= 500

    row = env.fetch_row("SELECT status FROM wiki_claims WHERE id = ?", (claim_id,))
    assert row[0] == "active"


def test_stale_marking_failure_aborts_delete(env):
    """AC2: when claim stale-marking raises, the DELETE must be aborted — the
    memory row must survive."""
    memory_id = env.create_memory("ac2 claim content")
    env.seed_sole_source_claim(memory_id, "ac2 seeded sole-source claim")

    with patch(
        "app.services.wiki_store.WikiStore.mark_claims_stale_by_memory",
        side_effect=RuntimeError("injected marking failure"),
    ):
        env.quiet_client.delete(f"/api/memories/{memory_id}")

    row = env.fetch_row(
        "SELECT COUNT(*) FROM memories WHERE id = ?", (memory_id,)
    )
    assert row[0] == 1


def test_update_with_failing_claim_invalidation_keeps_old_content(env):
    """AC3: when claim invalidation raises during an update, the new content
    must not be persisted."""
    memory_id = env.create_memory("old text")

    with patch(
        "app.services.wiki_store.WikiStore.mark_claims_stale_by_memory",
        side_effect=RuntimeError("injected marking failure"),
    ):
        env.client.put(f"/api/memories/{memory_id}", json={"content": "new text"})

    row = env.fetch_row("SELECT content FROM memories WHERE id = ?", (memory_id,))
    assert row[0] == "old text"
