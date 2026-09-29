"""Issue #686 A04 acceptance check — AC4: stale-token concurrent updates.

A save carrying an ``expected_updated_at`` token captured BEFORE a
concurrent edit must be rejected (409) and must not clobber the concurrent
editor's content.
"""

import os
import shutil
import sys
import tempfile
import time
import types
from pathlib import Path

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


def test_stale_token_update_is_rejected(env):
    """AC4: tab A saves with tab B's edit already committed — the stale
    expected_updated_at token must reject tab A's write, leaving tab B's
    content stored."""
    memory_id = env.create_memory("old text")

    original_updated_at = env.fetch_row(
        "SELECT updated_at FROM memories WHERE id = ?", (memory_id,)
    )[0]

    # Tab B saves successfully. CURRENT_TIMESTAMP has 1s resolution, so wait
    # past that boundary to make tab B's updated_at bump observable.
    time.sleep(1.1)
    tab_b = env.client.put(
        f"/api/memories/{memory_id}", json={"content": "tab B text"}
    )
    assert tab_b.status_code == 200, tab_b.text

    # Tab A still holds the pre-tab-B updated_at token.
    resp = env.client.put(
        f"/api/memories/{memory_id}",
        json={"content": "tab A text", "expected_updated_at": original_updated_at},
    )

    row = env.fetch_row("SELECT content FROM memories WHERE id = ?", (memory_id,))
    assert row[0] == "tab B text"
    assert resp.status_code == 409
