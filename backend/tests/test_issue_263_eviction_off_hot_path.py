"""Regression tests for issue #263.

Verifies that evict_expired_memories() is no longer called synchronously
inside search_memories() (the RAG query hot path) and that the periodic
background eviction loop performs cleanup correctly.
"""

import asyncio
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from app.models.database import SQLiteConnectionPool, init_db, run_migrations
from app.services.memory_store import MemoryStore


@pytest.fixture()
def memory_store():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "memories.db"
        init_db(str(db_path))
        run_migrations(str(db_path))
        pool = SQLiteConnectionPool(str(db_path), max_size=2)
        store = MemoryStore(pool=pool)
        try:
            yield store
        finally:
            pool.close_all()


def test_search_memories_does_not_call_eviction(memory_store):
    """search_memories() must NOT call evict_expired_memories() — that
    DELETE + COMMIT was moved off the hot path to a periodic background
    task (issue #263)."""
    with mock.patch.object(
        memory_store, "evict_expired_memories", return_value=0
    ) as patched_evict:
        memory_store.search_memories("anything", limit=5)
        patched_evict.assert_not_called()


def test_search_memories_still_filters_expired_rows_at_read_time(memory_store):
    """Even without synchronous eviction, expired memories must not appear
    in search results because the FTS and dense queries filter on
    expires_at at read time."""
    memory_store.add_memory(
        "expired fact about retention",
        importance=0.9,
        expires_at="2000-01-01T00:00:00",
    )
    memory_store.add_memory("fresh fact about retention", importance=0.8)

    results = memory_store.search_memories("retention", limit=10)
    assert [r.content for r in results] == ["fresh fact about retention"]


def test_periodic_eviction_loop_removes_expired_rows(memory_store):
    """The periodic_eviction_loop coroutine should evict expired memories
    when it wakes up."""
    memory_store.add_memory(
        "ephemeral secret",
        expires_at="2000-01-01T00:00:00",
    )
    memory_store.add_memory("permanent record")

    # Drive one iteration of the loop with a tiny interval.  We cancel
    # after the first eviction cycle completes.
    async def _drive():
        task = asyncio.create_task(
            memory_store.periodic_eviction_loop(interval=0.05)
        )
        # Give the loop time to run at least one eviction cycle.
        await asyncio.sleep(0.15)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(_drive())

    # Expired row should be physically gone now.
    conn = memory_store.pool.get_connection()
    try:
        rows = conn.execute("SELECT content FROM memories ORDER BY id").fetchall()
    finally:
        memory_store.pool.release_connection(conn)
    assert [r[0] for r in rows] == ["permanent record"]


def test_periodic_eviction_loop_survives_errors(memory_store):
    """A single failed eviction must not kill the loop — it should log
    and retry on the next cycle."""
    call_count = {"n": 0}

    original = memory_store.evict_expired_memories

    def flaky_evict():
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("transient db lock")
        return original()

    async def _drive():
        task = asyncio.create_task(
            memory_store.periodic_eviction_loop(interval=0.01)
        )
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    with mock.patch.object(memory_store, "evict_expired_memories", side_effect=flaky_evict):
        asyncio.run(_drive())

    # The loop must have survived the first error and called evict again.
    assert call_count["n"] >= 2
