"""Issue #686 supplementary check — AC1 delete-race response contract (PRR-005).

Pins that a memory deleted concurrently between the route's pre-check and
its BEGIN IMMEDIATE returns 404 (not a 200 for a row we did not delete),
mirroring update_memory's in-transaction re-check. The concurrent delete is
injected on the SAME connection (the route's own pooled conn) at the moment
BEGIN IMMEDIATE executes — a second connection's DELETE would block on the
write lock.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from _a04_env import (
    RouteEnv,  # noqa: E402
    app,  # noqa: E402,F401  (app import required before overrides)
)


@pytest.fixture()
def env():
    test_env = RouteEnv()
    yield test_env
    test_env.close()


def test_delete_after_concurrent_delete_returns_404(env):
    from app.api.deps import get_db

    memory_id = env.create_memory("a04 delete-race target")

    class _VanishingConn:
        """Deletes the row right before the route's transaction opens."""

        def __init__(self, base, memory_id):
            self._base = base
            self._memory_id = memory_id

        def execute(self, sql, params=()):
            if sql.strip() == "BEGIN IMMEDIATE":
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
        resp = env.client.delete(f"/api/memories/{memory_id}")
    finally:
        app.dependency_overrides[get_db] = original

    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == f"Memory with id {memory_id} not found"
    assert env.fetch_row(
        "SELECT COUNT(*) FROM memories WHERE id = ?", (memory_id,)
    )[0] == 0


def test_delete_still_succeeds_without_race(env):
    """PRESERVING: the normal delete path keeps its 200 contract."""
    memory_id = env.create_memory("a04 delete normal target")

    resp = env.client.delete(f"/api/memories/{memory_id}")

    assert resp.status_code == 200, resp.text
    assert resp.json()["forgotten"] is True
    assert env.fetch_row(
        "SELECT COUNT(*) FROM memories WHERE id = ?", (memory_id,)
    )[0] == 0
