"""Issue #686 supplementary check — AC4 response contracts.

Pins the 409 body shape for a stale expected_updated_at token, and the
in-transaction re-check contract: when the memory row disappears between
the pre-check and the in-transaction re-read (deleted concurrently on the
SAME connection, so the write lock cannot block it), the update returns
404 — not 409 and not a lost-update success.
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
    """Per-test route environment (mirrors test_a04_memory_legacy_tags.py)."""

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

    def execute(self, sql, params=()):
        conn = self.pool.get_connection()
        try:
            conn.execute(sql, params)
            conn.commit()
        finally:
            self.pool.release_connection(conn)

    def fetch_row(self, sql, params=()):
        conn = self.pool.get_connection()
        try:
            return conn.execute(sql, params).fetchone()
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


def _create_memory(env, content):
    resp = env.client.post(
        "/api/memories", json={"content": content, "vault_id": 1}
    )
    assert resp.status_code == 200, resp.text
    return int(resp.json()["id"])


def test_conflict_response_body_is_pinned(env):
    memory_id = _create_memory(env, "a04 recheck body pin")
    stored = env.fetch_row(
        "SELECT updated_at FROM memories WHERE id = ?", (memory_id,)
    )
    stale_token = "2000-01-01 00:00:00"
    assert stored[0] != stale_token

    resp = env.client.put(
        f"/api/memories/{memory_id}",
        json={"content": "should not land", "expected_updated_at": stale_token},
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == "Memory was modified by another session"
    kept = env.fetch_row(
        "SELECT content FROM memories WHERE id = ?", (memory_id,)
    )
    assert kept[0] == "a04 recheck body pin"


def test_row_deleted_before_recheck_returns_404(env):
    """The in-transaction re-read finds the row gone: 404, not 409/500.

    The concurrent delete is injected on the SAME connection (the route's
    own pooled conn, proxied by get_db) at the moment the re-check SELECT
    runs — a second connection's DELETE would block on our RESERVED lock.
    """
    from app.api.deps import get_db

    memory_id = _create_memory(env, "a04 vanished row content")
    stored = env.fetch_row(
        "SELECT updated_at FROM memories WHERE id = ?", (memory_id,)
    )
    token = stored[0]

    class _VanishingConn:
        """Deletes the row right before the in-txn re-check SELECT."""

        def __init__(self, base, memory_id):
            self._base = base
            self._memory_id = memory_id

        def execute(self, sql, params=()):
            if sql.strip().startswith("SELECT updated_at FROM memories"):
                self._base.execute(
                    "DELETE FROM memories WHERE id = ?", (self._memory_id,)
                )
                self._base.commit()
            return self._base.execute(sql, params)

        def __getattr__(self, name):
            return getattr(self._base, name)

    def vanishing_get_db():
        conn = _VanishingConn(env.pool.get_connection(), memory_id)
        try:
            yield conn
        finally:
            env.pool.release_connection(conn._base)

    original = app.dependency_overrides[get_db]
    app.dependency_overrides[get_db] = vanishing_get_db
    try:
        resp = env.client.put(
            f"/api/memories/{memory_id}",
            json={"content": "late write", "expected_updated_at": token},
        )
    finally:
        app.dependency_overrides[get_db] = original

    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == f"Memory with id {memory_id} not found"
