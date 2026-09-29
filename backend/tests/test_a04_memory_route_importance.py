"""Issue #686 A04 acceptance check — AC10: importance 0.0 round-trips.

A memory created with importance 0.0 must still report 0.0 from the list
endpoint (0.0 is a legitimate value, distinct from the 0.5 default).
"""

import os
import shutil
import sys
import tempfile
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


def test_zero_importance_round_trips_on_list(env):
    """AC10: a memory stored with importance 0.0 must list back as 0.0, not
    the 0.5 default."""
    created = env.client.post(
        "/api/memories",
        json={"content": "zero importance", "vault_id": 1, "importance": 0.0},
    )
    assert created.status_code == 200, created.text
    memory_id = int(created.json()["id"])

    resp = env.client.get("/api/memories?vault_id=1")
    assert resp.status_code == 200
    matches = [
        m for m in resp.json()["memories"] if m["id"] == str(memory_id)
    ]
    assert matches, "created memory must appear in the list"
    assert matches[0]["importance"] == 0.0
