"""Issue #937 acceptance checks -- memory embedding self-heal.

Arm's-length checks against the NULL-embedding healing story across the
three write paths (memories create route, memories update route, chat
remember-directive) and the heal mechanism the fix schedules:

* AC1 a row left NULL by a failed update-route re-embed must heal after
  provider recovery WITHOUT restart and WITHOUT superadmin action. RED at
  base, GREEN after the fix.
* AC2 a re-sent remember-directive that finds an EXISTING row with a NULL
  embedding must re-embed it. RED at base, GREEN after the fix.
* AC3 the degradation WARNINGs on the three write paths keep naming the
  healing paths that exist at head. GREEN at base and must stay GREEN.

Harness follows backend/tests/test_memory_regression.py (MemoryStore(pool=
...) on a temp SQLite DB), backend/tests/test_a06_remember_directive_
idempotency.py (real RAGEngine remember-directive drive), and
backend/tests/test_a04_memory_update_fields.py (route-level TestClient
drive of the memories create/update routes).
"""

import asyncio
import json
import logging
import os
import sqlite3
import sys
import types
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

CSRF_TEST_POLICY = "naive"


def _stub_optional_modules() -> None:
    """Stub missing optional heavy deps so importing app modules is cheap.

    Per-file stubs are load-bearing for CI (requirements-ci.txt omits
    unstructured; lancedb/pyarrow stubs are local fallbacks) -- see
    docs/engineering/testing.md section 2. Same helper as
    test_a06_remember_directive_idempotency.py.
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

from _db_pool import SimpleConnectionPool  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.models.database import (  # noqa: E402
    SQLiteConnectionPool,
    init_db,
    run_migrations,
)
from app.services.memory_store import MemoryStore  # noqa: E402


class _FlakyEmbedder:
    """Test-double embedding service for MemoryStore.embedding_service.

    ``fail_on_first=True`` fails the first ``embed_passage`` call and
    succeeds afterwards (an outage that ends before the heal pass runs).
    ``always_fail=True`` fails every call (the AC3 degradation paths).
    ``embed_calls`` records every call so tests can count embeds per row.
    """

    def __init__(self, fail_on_first: bool = True, always_fail: bool = False):
        self.fail_on_first = fail_on_first
        self.always_fail = always_fail
        self.embed_calls: list = []
        self._failed_once = False

    async def embed_passage(self, text):
        self.embed_calls.append(text)
        if self.always_fail or (self.fail_on_first and not self._failed_once):
            self._failed_once = True
            raise RuntimeError("simulated embedding provider outage")
        return [0.1, 0.2, 0.3]

    async def effective_embedding_model(self):
        return "test-embed-model"


class _PermissiveAdmission:
    """Permissive admission stand-in (test_a06 harness pattern) -- the
    remember path consults no admission class; this only guards against
    unrelated controller wiring reaching the engine in test settings."""

    def __init__(self):
        self.calls = []

    async def queue_depth(self, cls):
        return 0

    @asynccontextmanager
    async def admit(self, admission_class, **kwargs):
        self.calls.append((admission_class, kwargs))
        yield


class _RouteEnv:
    """Per-test route environment (test_a04_memory_update_fields._RouteEnv
    pattern): temp DB under tmp_path, pools, a MemoryStore, dependency
    overrides on the shared app, and a vault row for vault-scoped
    writes."""

    def __init__(self, base_dir):
        self.base_dir = Path(base_dir)
        self.db_path = str(self.base_dir / "route.db")
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
                "INSERT OR IGNORE INTO vaults (id, name, description)"
                " VALUES (1, 'V1', '')"
            )
            conn.commit()
        finally:
            self.pool.release_connection(conn)

    def close(self):
        for dep in self._deps:
            app.dependency_overrides.pop(dep, None)
        self.store_pool.close_all()
        self.pool.close_all()


def test_null_row_heals_after_provider_recovery_without_restart(tmp_path):
    """AC1 (issue #937): a memory row whose embedding was nulled by a
    content update and whose best-effort re-embed failed must heal after
    the embedding provider recovers -- without restart and without
    superadmin action. RED at base, GREEN after the fix.

    Shape (documented per the issue; amended per the plan critic): the
    update-route store shape is driven directly against MemoryStore
    (content change commits a NULL embedding; the best-effort re-embed
    consumes the flaky provider's single programmed failure). The heal
    is then driven through the REAL scheduler the fix adds: the test
    starts the actual ``periodic_embed_heal_loop`` task (interval=0 so a
    tick fires immediately), waits for the row's embedding to become
    non-NULL, and cancels the task. At base the entry point does not
    exist, the seam assert fails RED ("periodic_embed_heal_loop
    missing"); after the fix the recovered provider embeds the row on
    the tick and the readback is non-NULL.
    """

    db_path = str(tmp_path / "heal.db")
    init_db(db_path)
    run_migrations(db_path)

    flaky = _FlakyEmbedder(fail_on_first=True)
    pool = SQLiteConnectionPool(db_path, max_size=2)
    try:
        store = MemoryStore(pool=pool, embedding_service=flaky)
        record = store.add_memory(
            "projector bulb replaced on 2026-11-01",
            source="user input",
            vault_id=1,
        )

        # Pre-existing embedding (stands in for the successful embed the
        # row had before the content change) -- seeded via SQL so the
        # flaky provider's single programmed failure stays reserved for
        # the update-route re-embed below.
        conn = pool.get_connection()
        try:
            conn.execute(
                "UPDATE memories SET embedding = ?, embedding_model = ?"
                " WHERE id = ?",
                (json.dumps([0.4, 0.5, 0.6]), "test-embed-model", record.id),
            )
            conn.commit()
        finally:
            pool.release_connection(conn)

        # Update-route shape: the content change commits a NULL
        # embedding ...
        store.update_memory_content(
            record.id, "projector bulb replaced on 2026-11-12"
        )
        # ... and the route's best-effort re-embed fails (flaky provider,
        # its first call).
        outcome = asyncio.run(
            store.embed_and_store(
                record.id, "projector bulb replaced on 2026-11-12"
            )
        )
        assert outcome == "failed", "harness: the re-embed must fail here"

        # The REAL scheduler the fix adds must exist and heal the row: this
        # test starts the actual periodic_embed_heal_loop task, lets one
        # tick run after provider recovery, and cancels it -- no superadmin
        # route, no restart, no direct backfill call. At base the entry
        # point does not exist and the seam assert below fails RED.
        assert callable(
            getattr(store, "periodic_embed_heal_loop", None)
        ), "periodic_embed_heal_loop missing: no periodic healer exists"

        async def _drive_one_tick():
            task = asyncio.create_task(
                store.periodic_embed_heal_loop(interval=0)
            )
            try:
                for _ in range(500):
                    await asyncio.sleep(0.01)
                    conn = pool.get_connection()
                    try:
                        row = conn.execute(
                            "SELECT embedding FROM memories WHERE id = ?",
                            (record.id,),
                        ).fetchone()
                    finally:
                        pool.release_connection(conn)
                    if row and row[0] is not None:
                        return True
                return False
            finally:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        assert asyncio.run(_drive_one_tick()) is True, (
            "heal tick ran but the NULL embedding was not re-embedded"
        )
    finally:
        pool.close_all()

    check = sqlite3.connect(db_path)
    try:
        row = check.execute(
            "SELECT embedding FROM memories WHERE id = ?", (record.id,)
        ).fetchone()
    finally:
        check.close()
    assert row[0] is not None


def test_resent_directive_reembeds_null_existing_row(tmp_path):
    """AC2 (issue #937): a re-sent remember-directive whose
    find_memory_by_content lookup finds an EXISTING row (same content,
    chat source, same vault, non-expiring) with a NULL embedding must
    re-embed that row -- embed_and_store called exactly once for it by
    the engine's directive handling. RED at base (the found-existing
    branch never embeds, count is 0), GREEN after the fix (count is 1).

    Driven through a real RAGEngine on a real MemoryStore (the
    test_a06_remember_directive_idempotency harness): the directive query
    "remember that the pump code is 7" resolves to the pre-stored row.
    """
    from app.services import rag_engine as rag_engine_module

    db_path = str(tmp_path / "resent.db")
    init_db(db_path)
    run_migrations(db_path)

    embedder = _FlakyEmbedder(fail_on_first=False)
    pool = SQLiteConnectionPool(db_path, max_size=5)
    try:
        store = MemoryStore(pool=pool, embedding_service=embedder)
        # The row a prior turn stored whose embed failed: exact directive
        # content, chat source, same vault, non-expiring, NULL embedding.
        existing = store.add_memory(
            "the pump code is 7", source="chat", vault_id=1
        )

        original_embed_and_store = store.embed_and_store
        embed_calls: list = []

        async def counting_embed_and_store(memory_id, content):
            embed_calls.append((memory_id, content))
            return await original_embed_and_store(memory_id, content)

        store.embed_and_store = counting_embed_and_store

        engine = rag_engine_module.RAGEngine(
            embedding_service=MagicMock(),
            vector_store=MagicMock(),
            memory_store=store,
            llm_client=MagicMock(),
            db_path=db_path,
        )

        async def _drive():
            chunks = []
            with patch.object(
                rag_engine_module,
                "get_admission_controller",
                lambda: _PermissiveAdmission(),
            ):
                async for chunk in engine.query(
                    "remember that the pump code is 7",
                    [],
                    stream=True,
                    vault_id=1,
                    can_write_memory=True,
                ):
                    chunks.append(chunk)
            return chunks

        chunks = asyncio.run(_drive())
        assert chunks, "harness: the re-sent directive produced no chunks"
    finally:
        pool.close_all()

    calls_for_row = [c for c in embed_calls if c[0] == existing.id]
    assert len(calls_for_row) == 1


def test_degradation_warnings_name_the_real_healing_paths(tmp_path, caplog):
    """AC3 (issue #937, PRESERVING): a failed embed on each of the three
    write paths (create-route shape, update-route shape, remember shape)
    logs a WARNING naming the healing mechanism IN EFFECT -- GREEN at base
    AND after the fix, and never allowed to regress to an overstated
    wording. Mechanism-aware re-target (the amendment issue #937 AC3 itself
    sanctions): when the periodic embed-heal loop exists the three-healer
    wording (startup backfill task, periodic embed-heal task, superadmin
    backfill route) is required; on a tree without the healer the two-healer
    wording is required. C8 independently drives the real healer, so an
    unwired periodic_embed_heal_loop cannot hide behind this check.
    """
    from app.services import rag_engine as rag_engine_module

    # The healing mechanism in effect decides the required wording.
    healer_exists = hasattr(MemoryStore, "periodic_embed_heal_loop")
    if healer_exists:
        heal_paths_substring = (
            "healed by the startup backfill task, the periodic embed-heal "
            "task, or the superadmin backfill route"
        )
        create_warning_substring = heal_paths_substring
    else:
        create_warning_substring = (
            "dense recall degraded to FTS-only for this row until backfill"
        )
        heal_paths_substring = (
            "healed by the startup backfill task or the superadmin "
            "backfill route, not on a timer"
        )

    # -- create + update routes through the app (a04 _RouteEnv pattern)
    env = _RouteEnv(tmp_path / "routes")
    try:
        env.store.embedding_service = _FlakyEmbedder(always_fail=True)
        with caplog.at_level(logging.WARNING):
            created = env.client.post(
                "/api/memories",
                json={"content": "ac3 create content", "vault_id": 1},
            )
            assert created.status_code == 200, created.text
            memory_id = int(created.json()["id"])

            updated = env.client.put(
                f"/api/memories/{memory_id}",
                json={"content": "ac3 changed content"},
            )
            assert updated.status_code == 200, updated.text
    finally:
        env.close()

    route_messages = [
        r.getMessage()
        for r in caplog.records
        if r.name == "app.api.routes.memories" and r.levelno >= logging.WARNING
    ]
    assert any(
        create_warning_substring in m for m in route_messages
    ), "937-AC3 create-path degradation warning missing/changed"
    assert any(
        heal_paths_substring in m for m in route_messages
    ), "937-AC3 update-path degradation warning missing/changed"

    # -- remember-directive through a real RAGEngine (a06 harness pattern)
    remember_db = str(tmp_path / "remember.db")
    init_db(remember_db)
    run_migrations(remember_db)
    rem_pool = SQLiteConnectionPool(remember_db, max_size=5)
    try:
        rem_store = MemoryStore(
            pool=rem_pool,
            embedding_service=_FlakyEmbedder(always_fail=True),
        )
        engine = rag_engine_module.RAGEngine(
            embedding_service=MagicMock(),
            vector_store=MagicMock(),
            memory_store=rem_store,
            llm_client=MagicMock(),
            db_path=remember_db,
        )

        async def _drive_remember():
            chunks = []
            with patch.object(
                rag_engine_module,
                "get_admission_controller",
                lambda: _PermissiveAdmission(),
            ):
                async for chunk in engine.query(
                    "remember that the ac3 probe fires on wednesday",
                    [],
                    stream=True,
                    vault_id=1,
                    can_write_memory=True,
                ):
                    chunks.append(chunk)
            return chunks

        with caplog.at_level(logging.WARNING):
            chunks = asyncio.run(_drive_remember())
        assert chunks, "harness: remember-directive produced no chunks"
    finally:
        rem_pool.close_all()

    remember_messages = [
        r.getMessage()
        for r in caplog.records
        if r.name == "app.services.rag_engine"
        and r.levelno >= logging.WARNING
    ]
    assert any(
        heal_paths_substring in m for m in remember_messages
    ), "937-AC3 remember-path degradation warning missing/changed"
