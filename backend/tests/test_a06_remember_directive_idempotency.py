"""Issue #688 defect 4 (A06) — "remember ..." directive is not idempotent,
RED at base.

The engine's remember path (rag_engine.py) does a plain
``memory_store.add_memory`` INSERT with no idempotency key and then yields
the confirmation chunk. A consumer cancelled between the INSERT and the
confirmation leaves the memory stored-but-unconfirmed, and re-sending the
same directive for the same turn stores a DUPLICATE row.

This check drives a real MemoryStore on a temp DB through a real RAGEngine
with stub clients (no LLM/embedding call happens on the remember path):
the first turn's consumer is cancelled once the insert has committed (before
the confirmation chunk is consumed), then the same directive is re-sent for
the same vault and turn identity — exactly one row may exist afterwards.
"""

import asyncio
import contextlib
import os
import sqlite3
import sys
import types
from contextlib import asynccontextmanager
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

CSRF_TEST_POLICY = "naive"


def _stub_optional_modules() -> None:
    """Stub missing optional heavy deps so importing app modules is cheap.

    Per-file stubs are load-bearing for CI (requirements-ci.txt omits
    unstructured; lancedb/pyarrow stubs are local fallbacks) — see
    docs/engineering/testing.md section 2.
    """
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


class _PermissiveAdmission:
    """Permissive admission stand-in (518 harness pattern) — the remember
    path consults no admission class, this only guards against unrelated
    controller wiring reaching the engine in test settings."""

    def __init__(self):
        self.calls = []

    async def queue_depth(self, cls):
        return 0

    @asynccontextmanager
    async def admit(self, admission_class, **kwargs):
        self.calls.append((admission_class, kwargs))
        yield


async def _consume(gen):
    chunks = []
    async for chunk in gen:
        chunks.append(chunk)
    return chunks


@pytest.mark.asyncio
async def test_interrupted_remember_turn_resent_stores_one_memory(tmp_path):
    from app.models.database import SQLiteConnectionPool, init_db, run_migrations
    from app.services import rag_engine as rag_engine_module
    from app.services.memory_store import MemoryStore
    from app.services.telemetry import set_current_turn

    db_path = tmp_path / "remember-idempotency.db"
    init_db(str(db_path))
    run_migrations(str(db_path))

    store_pool = SQLiteConnectionPool(str(db_path), max_size=5)
    store = MemoryStore(pool=store_pool, embedding_service=None)
    engine = rag_engine_module.RAGEngine(
        embedding_service=MagicMock(),
        vector_store=MagicMock(),
        memory_store=store,
        llm_client=MagicMock(),
        db_path=str(db_path),
    )

    # Signal the moment the remember INSERT has committed: the wrapper runs
    # in the asyncio.to_thread worker the engine uses for add_memory, so the
    # event is set via call_soon_threadsafe AFTER the store's commit.
    loop = asyncio.get_running_loop()
    insert_committed = asyncio.Event()
    original_add_memory = store.add_memory

    def recording_add_memory(content, *args, **kwargs):
        record = original_add_memory(content, *args, **kwargs)
        loop.call_soon_threadsafe(insert_committed.set)
        return record

    store.add_memory = recording_add_memory

    # Durable turn identity: RAGEngine.query() takes no turn_id parameter;
    # the streaming route binds the durable turn via the current-turn
    # contextvar (chat.py: set_current_turn(durable_turn_id)), so mirror
    # that binding here on BOTH runs. Per the issue's allowance, idempotency
    # keys on content + vault for the re-sent directive.
    turn_id = "a06-remember-turn-1"
    set_current_turn(turn_id)

    query = "remember that the pump code is 7"
    try:
        with patch.object(
            rag_engine_module,
            "get_admission_controller",
            lambda: _PermissiveAdmission(),
        ):
            # Turn 1: the consumer is cancelled once the insert has
            # committed, before the confirmation chunk is consumed. The
            # set-event callback is queued on the loop BEFORE the worker
            # thread's to_thread completion callback (it fires inside
            # add_memory, before the thread finishes), so this cancellation
            # deterministically lands while the generator is still suspended
            # on the add_memory await.
            gen1 = engine.query(
                query, [], stream=True, vault_id=1, can_write_memory=True
            )
            consumer = asyncio.create_task(_consume(gen1))
            await insert_committed.wait()
            consumer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await consumer
            with contextlib.suppress(RuntimeError):
                await gen1.aclose()

            check = sqlite3.connect(str(db_path))
            try:
                stored_unconfirmed = check.execute(
                    "SELECT COUNT(*) FROM memories WHERE content LIKE "
                    "'%pump code is 7%'"
                ).fetchone()[0]
            finally:
                check.close()
            assert stored_unconfirmed == 1, (
                "harness: the interrupted turn must have stored exactly one "
                f"row, found {stored_unconfirmed}"
            )

            # Turn 2 (re-send): same directive, same vault, same turn
            # identity — the resent turn must not duplicate the memory.
            set_current_turn(turn_id)
            chunks = await _consume(
                engine.query(
                    query, [], stream=True, vault_id=1, can_write_memory=True
                )
            )
            assert chunks, "harness: the re-sent turn produced no chunks"
    finally:
        store_pool.close_all()

    check = sqlite3.connect(str(db_path))
    try:
        count = check.execute(
            "SELECT COUNT(*) FROM memories WHERE content LIKE '%pump code is 7%'"
        ).fetchone()[0]
    finally:
        check.close()
    assert count == 1
