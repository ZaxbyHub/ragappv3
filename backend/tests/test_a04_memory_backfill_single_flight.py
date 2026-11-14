"""Issue #686 A04 acceptance check — AC11: embedding backfill is single-flight.

Two concurrent backfill requests must not run the (expensive) backfill loop
at the same time — the second caller must wait for (or join) the in-flight
run instead of overlapping it.
"""

import asyncio
import os
import sys
import threading
import types
import weakref

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

from app.api.routes.memories import backfill_memory_embeddings  # noqa: E402

_SUPERADMIN = {
    "id": 0,
    "username": "admin",
    "role": "superadmin",
    "is_active": 1,
    "must_change_password": 0,
}


class _SlowBackfillStore:
    """Stub store whose backfill is slow enough to expose overlap."""

    def __init__(self):
        self.in_flight = 0
        self.max_in_flight = 0
        # Wiring pin (r2 review TF-1/TI-1): count get_backfill_lock calls
        # so the route->store delegation is discriminatingly asserted —
        # reverting the route to its own module-level lock (the pre-#937
        # shape) previously kept every test green because a module lock
        # and this stub's loop-keyed lock serialize identically.
        self.get_backfill_lock_calls = 0
        # Issue #937 mock-contract adaptation: the single-flight lock moved
        # from the memories route module onto MemoryStore — the stub now
        # exposes the same per-event-loop lock the route delegates to
        # (loop-keyed weakref map, the pattern the store itself uses).
        self._locks_guard = threading.Lock()
        self._locks: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock]" = (
            weakref.WeakKeyDictionary()
        )

    def get_backfill_lock(self) -> asyncio.Lock:
        self.get_backfill_lock_calls += 1
        loop = asyncio.get_running_loop()
        with self._locks_guard:
            lock = self._locks.get(loop)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[loop] = lock
            return lock

    async def backfill_missing_embeddings(self, batch_size: int = 50) -> dict:
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(0.2)
        finally:
            self.in_flight -= 1
        return {"total": 0, "processed": 0, "skipped": 0, "failed": 0}


def test_concurrent_backfill_requests_do_not_overlap():
    """AC11: two concurrent backfill calls must never overlap — the stub's
    observed max in-flight count must stay at 1."""

    async def main():
        store = _SlowBackfillStore()
        await asyncio.gather(
            backfill_memory_embeddings(
                memory_store=store, user=dict(_SUPERADMIN), _csrf_token="t"
            ),
            backfill_memory_embeddings(
                memory_store=store, user=dict(_SUPERADMIN), _csrf_token="t"
            ),
        )
        return store

    store = asyncio.run(main())
    assert store.max_in_flight == 1
    # Wiring pin (r2 review TF-1/TI-1): the route must acquire the lock
    # THROUGH the store (once per route call) — a route that keeps its own
    # module-level lock never calls the store's getter.
    assert store.get_backfill_lock_calls == 2, store.get_backfill_lock_calls
