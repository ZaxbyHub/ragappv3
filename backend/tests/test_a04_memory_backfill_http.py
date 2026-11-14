"""Issue #686 supplementary check — AC11 HTTP-surface coverage (PRR-003).

The frozen AC11 check drives the route function directly (its argv is
issue-pinned), so the `/api/memories/backfill-embeddings` HTTP surface —
route registration, superadmin gate, CSRF/auth dependencies — had no
suite-wide coverage. These tests exercise the endpoint through TestClient.
"""

import asyncio
import os
import sys
import threading
import weakref

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from _a04_env import (
    RouteEnv,  # noqa: E402
    app,  # noqa: E402,F401  (app import required before overrides)
)


class _CountingStore:
    """Async stub whose backfill reports a one-row summary."""

    def __init__(self):
        self.calls = 0
        # Issue #937 mock-contract adaptation: the single-flight lock moved
        # onto MemoryStore (the route now delegates to
        # store.get_backfill_lock()), so the stub exposes the same
        # per-event-loop lock.
        self._locks_guard = threading.Lock()
        self._locks: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock]" = (
            weakref.WeakKeyDictionary()
        )

    def get_backfill_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        with self._locks_guard:
            lock = self._locks.get(loop)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[loop] = lock
            return lock

    async def backfill_missing_embeddings(self):
        self.calls += 1
        return {"total": 1, "processed": 1, "skipped": 0, "failed": 0}


@pytest.fixture()
def env():
    test_env = RouteEnv()
    yield test_env
    test_env.close()


def test_backfill_endpoint_runs_for_superadmin(env):
    from app.api.deps import get_memory_store

    stub = _CountingStore()
    original = app.dependency_overrides[get_memory_store]
    app.dependency_overrides[get_memory_store] = lambda: stub
    try:
        resp = env.client.post("/api/memories/backfill-embeddings")
    finally:
        app.dependency_overrides[get_memory_store] = original

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "complete"
    assert body["summary"] == {"total": 1, "processed": 1, "skipped": 0, "failed": 0}
    assert stub.calls == 1


def test_backfill_endpoint_rejects_non_superadmin(env):
    from app.api.deps import get_current_active_user, get_memory_store

    stub = _CountingStore()
    original_store = app.dependency_overrides[get_memory_store]
    original_user = app.dependency_overrides[get_current_active_user]
    app.dependency_overrides[get_memory_store] = lambda: stub
    app.dependency_overrides[get_current_active_user] = lambda: {
        "id": 1, "username": "member", "role": "member",
        "is_active": 1, "must_change_password": 0,
    }
    try:
        resp = env.client.post("/api/memories/backfill-embeddings")
    finally:
        app.dependency_overrides[get_memory_store] = original_store
        app.dependency_overrides[get_current_active_user] = original_user

    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == "Superadmin access required"
    assert stub.calls == 0
