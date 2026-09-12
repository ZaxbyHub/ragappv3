"""Issue #494 acceptance check — AC29 / FU-003 (PRESERVING).

Pins the last-known service-status cache of the REAL /api/health route
(``app/api/routes/health.py``, landed via PR #493) so it cannot regress.
The route module's globals (``_deep_cache``, ``_deep_refresh_in_flight``,
``_refresh_tasks``) are saved/reset around the test and the endpoint is driven
through ``TestClient(app)`` with ``TestHealthEndpoint``-style checker mocks
(``app.dependency_overrides``), exactly like ``backend/tests/test_api_routes.py``.

``_deep_cache["ts"]`` is set directly (never slept) for TTL determinism, and a
deterministic ``app.state.vector_store`` mock is installed so the cached
services payload is exact.

Event-loop note (verified against starlette 1.6.0 + the installed anyio):
without a context manager, each ``client.get()`` runs the app on a fresh
per-request portal/loop. anyio's ``BlockingPortal.stop(cancel_remaining=False)``
(the ``start_blocking_portal`` exit path) WAITS for spawned tasks to finish
before the request call returns, so a refresh task spawned during a request
completes deterministically before ``client.get()`` returns: assertions on the
post-request cache/flag state are exact, not disjunctive. Task *creation* is
observed synchronously (set-diff around the real ``_spawn_refresh_task``), so
the single-flight guard test forces ``_deep_refresh_in_flight = True`` directly
to hold the in-flight window open.

Pinned behavior (per the FU-003 acceptance criteria):

(0) Shallow poll with a fresh-but-empty cache: services are all "not checked",
    no ``services_cached`` / ``services_age_seconds`` fields, no deep payload,
    no refresh spawned.
(a) A deep poll runs the deep collection once and synchronously refreshes
    ``_deep_cache`` with the exact services dict (backend/embeddings/chat/
    vector_store) plus the deep payload (llm/models/llm_modes/vector_store).
(b) A subsequent shallow poll serves the cached services bit-for-bit WITHOUT
    invoking the checkers or the deep collection, sets ``services_cached`` and
    a ``services_age_seconds`` consistent with the pinned timestamp, and omits
    the deep payload.
(c) When cache age >= ``_SHALLOW_CACHE_TTL`` (age pinned via ``ts``), a shallow
    poll spawns exactly ONE background refresh task (real
    ``asyncio.create_task`` via ``_spawn_refresh_task``) while still answering
    immediately with the stale last-known services and a truthful stale age.
(d) Single-flight: while ``_deep_refresh_in_flight`` is True, further stale
    shallow polls reach the stale branch (spawn attempts counted) but create
    NO additional task; once the flag clears, a stale poll spawns again.
(e) ``services_age_seconds`` is monotonically non-decreasing across successive
    shallow polls (fixed ``ts``).
(f) The spawned task body (real ``_refresh_deep_cache`` awaited directly on
    the test loop): re-collects deep state when stale, refreshes ts+services,
    and clears the in-flight flag in its finally; fresh cache => early return
    with no collection.

Must be GREEN at the pre-fix base a543361 (behavior already landed in #493).
"""

import os
import sys
import time
import unittest
from unittest.mock import AsyncMock, MagicMock

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Stub missing optional dependencies (CI installs requirements-ci.txt)
try:
    import lancedb  # noqa: F401
except ImportError:
    import types

    sys.modules["lancedb"] = types.ModuleType("lancedb")

try:
    import pyarrow  # noqa: F401
except ImportError:
    import types

    sys.modules["pyarrow"] = types.ModuleType("pyarrow")

try:
    from unstructured.partition.auto import partition  # noqa: F401
except ImportError:
    import types

    _unstructured = types.ModuleType("unstructured")
    _unstructured.__path__ = []
    _unstructured.partition = types.ModuleType("unstructured.partition")
    _unstructured.partition.__path__ = []
    _unstructured.partition.auto = types.ModuleType("unstructured.partition.auto")
    _unstructured.partition.auto.partition = lambda *args, **kwargs: []
    sys.modules["unstructured"] = _unstructured
    sys.modules["unstructured.partition"] = _unstructured.partition
    sys.modules["unstructured.partition.auto"] = _unstructured.partition.auto

from fastapi.testclient import TestClient

from app.api.deps import get_llm_health_checker, get_model_checker
from app.api.routes import health as health_module
from app.main import app

EXPECTED_SERVICES = {
    "backend": True,
    "embeddings": True,
    "chat": False,
    "vector_store": True,
}
LLM_CHECK_ALL_RESULT = {
    "ok": True,
    "embeddings": {"ok": True, "error": None},
    "chat": {"ok": False, "error": "chat endpoint refused"},
    "error": None,
}
MODELS_CHECK_RESULT = {
    "embedding_model": {"available": True, "error": None},
    "chat_model": {"available": False, "error": "Model 'x' not found"},
}


class TestIssue494AC29HealthLastKnownCacheSingleFlight(unittest.IsolatedAsyncioTestCase):
    """One node, sub-assertions (0),(a)-(f); PRESERVING for PR #493 behavior."""

    def setUp(self) -> None:
        # --- save & reset the route module's cache globals ------------------
        self._orig_cache = health_module._deep_cache
        self._orig_in_flight = health_module._deep_refresh_in_flight
        self._orig_tasks = health_module._refresh_tasks
        self._orig_collect = health_module._collect_deep_state
        self._orig_spawn = health_module._spawn_refresh_task
        health_module._deep_cache = {"services": None, "ts": 0.0}
        health_module._deep_refresh_in_flight = False
        health_module._refresh_tasks = set()

        # --- counting spies that delegate to the real implementations -------
        self.collect_calls: list = []
        self.spawn_attempts: list = []
        self.spawned_tasks: set = set()

        real_collect = self._orig_collect

        async def spy_collect(app_state, llm_checker, model_checker):
            self.collect_calls.append(time.monotonic())
            return await real_collect(app_state, llm_checker, model_checker)

        health_module._collect_deep_state = spy_collect

        real_spawn = self._orig_spawn

        def spy_spawn(request, llm_checker, model_checker):
            self.spawn_attempts.append(time.monotonic())
            before = set(health_module._refresh_tasks)
            real_spawn(request, llm_checker, model_checker)
            # Task creation happens synchronously inside real_spawn
            # (flag check -> create_task -> add to _refresh_tasks), so the
            # set-diff observes exactly the tasks this spawn created.
            self.spawned_tasks.update(health_module._refresh_tasks - before)

        health_module._spawn_refresh_task = spy_spawn

        # --- TestHealthEndpoint-style checker mocks --------------------------
        self.llm_checker = MagicMock()
        self.llm_checker.check_all = AsyncMock(return_value=LLM_CHECK_ALL_RESULT)
        self.llm_checker.check_chat_modes = AsyncMock(
            return_value={"thinking": True, "instant": False}
        )
        self.model_checker = MagicMock()
        self.model_checker.check_models = AsyncMock(return_value=MODELS_CHECK_RESULT)

        # Deterministic vector store so the deep payload is exact (rows=7,
        # no dimension mismatch because _get_expected_embedding_dim -> None).
        self._had_vector_store = hasattr(app.state, "vector_store")
        self._orig_vector_store = getattr(app.state, "vector_store", None)
        vector_store = MagicMock()
        vector_store.table.count_rows = AsyncMock(return_value=7)
        vector_store._get_expected_embedding_dim = AsyncMock(return_value=None)
        app.state.vector_store = vector_store

        app.dependency_overrides[get_llm_health_checker] = lambda: self.llm_checker
        app.dependency_overrides[get_model_checker] = lambda: self.model_checker

        self.client = TestClient(app)

    def tearDown(self) -> None:
        app.dependency_overrides.pop(get_llm_health_checker, None)
        app.dependency_overrides.pop(get_model_checker, None)
        if self._had_vector_store:
            app.state.vector_store = self._orig_vector_store
        elif hasattr(app.state, "vector_store"):
            delattr(app.state, "vector_store")
        # Restore the real module functions and original globals last so any
        # stray late-running refresh from a dead per-request loop writes into
        # restored state only via the real code paths.
        health_module._collect_deep_state = self._orig_collect
        health_module._spawn_refresh_task = self._orig_spawn
        health_module._deep_cache = self._orig_cache
        health_module._deep_refresh_in_flight = self._orig_in_flight
        health_module._refresh_tasks = self._orig_tasks

    async def test_last_known_cache_and_single_flight(self) -> None:
        # ---- (0) fresh-but-empty cache: shallow poll reports "not checked" --
        health_module._deep_cache["ts"] = time.monotonic() - 1.0
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "ok")
        self.assertEqual(
            data["services"],
            {"backend": True, "embeddings": None, "chat": None, "vector_store": None},
            "empty cache: shallow poll reports not-checked nulls, never false",
        )
        self.assertNotIn("services_cached", data)
        self.assertNotIn("services_age_seconds", data)
        self.assertNotIn("llm", data)
        self.assertNotIn("models", data)
        self.assertEqual(self.collect_calls, [], "shallow poll must not collect deep state")
        self.assertEqual(self.spawn_attempts, [], "fresh cache must not spawn a refresh")

        # ---- (a) deep poll populates _deep_cache synchronously --------------
        response = self.client.get("/api/health?deep=true")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["services"], EXPECTED_SERVICES)
        self.assertEqual(data["llm"], LLM_CHECK_ALL_RESULT)
        self.assertEqual(data["models"], MODELS_CHECK_RESULT)
        self.assertEqual(data["llm_modes"], {"thinking": True, "instant": False})
        self.assertEqual(data["vector_store"], {"ok": True, "rows": 7})
        self.assertEqual(len(self.collect_calls), 1, "deep poll collects deep state once")
        self.assertEqual(
            health_module._deep_cache["services"], EXPECTED_SERVICES,
            "deep poll refreshes the last-known cache",
        )
        self.assertLess(health_module._cache_age(), 5.0, "cache timestamp is fresh")
        check_all_calls = self.llm_checker.check_all.call_count
        check_models_calls = self.model_checker.check_models.call_count

        # ---- (b) shallow poll serves the cache without invoking checkers ---
        health_module._deep_cache["ts"] = time.monotonic() - 12.0  # pinned age
        collect_before = len(self.collect_calls)
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["services"], EXPECTED_SERVICES, "bit-for-bit last-known")
        self.assertTrue(data["services_cached"])
        self.assertTrue(
            11.0 <= data["services_age_seconds"] <= 14.0,
            f"age consistent with the pinned ts (got {data['services_age_seconds']})",
        )
        self.assertNotIn("llm", data)
        self.assertNotIn("models", data)
        self.assertEqual(len(self.collect_calls), collect_before, "no deep collection")
        self.assertEqual(
            self.llm_checker.check_all.call_count, check_all_calls, "LLM checker untouched"
        )
        self.assertEqual(
            self.model_checker.check_models.call_count, check_models_calls,
            "model checker untouched",
        )
        self.assertEqual(self.spawn_attempts, [], "fresh (12s < TTL) => no refresh spawn")

        # ---- (c) cache age >= TTL: shallow poll spawns exactly one task -----
        stale_ts = time.monotonic() - (health_module._SHALLOW_CACHE_TTL + 10.0)
        health_module._deep_cache["ts"] = stale_ts
        collect_before = len(self.collect_calls)
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(
            data["services"], EXPECTED_SERVICES,
            "stale poll still answers immediately with last-known services",
        )
        self.assertTrue(data["services_cached"])
        self.assertGreaterEqual(
            data["services_age_seconds"], 50.0, "stale age is reported truthfully"
        )
        self.assertEqual(len(self.spawn_attempts), 1, "stale shallow poll spawns (attempts)")
        self.assertEqual(len(self.spawned_tasks), 1, "exactly ONE refresh task created")
        # Deterministic completion: anyio's blocking portal (starlette 1.6.0 /
        # TestClient per-request loops) shuts down with cancel_remaining=False,
        # which WAITS for the spawned task to finish before client.get()
        # returns (anyio.from_thread.BlockingPortal.stop). So by the time the
        # response is back, the real _refresh_deep_cache body has run.
        self.assertEqual(
            len(self.collect_calls),
            collect_before + 1,
            "the spawned task re-collected deep state in the background",
        )
        self.assertGreater(
            health_module._deep_cache["ts"], stale_ts, "background refresh updated ts"
        )
        self.assertEqual(health_module._deep_cache["services"], EXPECTED_SERVICES)
        self.assertFalse(
            health_module._deep_refresh_in_flight,
            "the finished task cleared the in-flight flag",
        )

        # ---- (d) single-flight: in-flight flag suppresses further spawns ----
        # Re-stale the cache ((c)'s background refresh made it fresh), then
        # simulate a refresh still in flight: stale polls must reach the stale
        # branch (spawn attempts counted) but create NO additional task.
        health_module._deep_cache["ts"] = (
            time.monotonic() - (health_module._SHALLOW_CACHE_TTL + 10.0)
        )
        health_module._deep_refresh_in_flight = True  # refresh still running
        for _ in range(3):
            stale_resp = self.client.get("/api/health")
            self.assertEqual(stale_resp.status_code, 200)
        self.assertEqual(
            len(self.spawn_attempts), 4,
            "each stale poll reached the stale branch (1 from (c) + 3 here)",
        )
        self.assertEqual(
            len(self.spawned_tasks), 1,
            "while in flight, additional stale polls create NO additional task",
        )
        health_module._deep_refresh_in_flight = False  # refresh finished
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            len(self.spawned_tasks), 2,
            "flag cleared + still stale => spawn is allowed again",
        )

        # ---- (e) services_age_seconds monotonic across shallow polls --------
        health_module._deep_cache["ts"] = time.monotonic() - 5.0
        health_module._deep_refresh_in_flight = True  # keep this phase spawn-free
        ages = []
        for _ in range(3):
            age_resp = self.client.get("/api/health")
            self.assertEqual(age_resp.status_code, 200)
            age_data = age_resp.json()
            self.assertTrue(age_data["services_cached"])
            ages.append(age_data["services_age_seconds"])
        self.assertEqual(
            ages, sorted(ages), f"ages must be non-decreasing (got {ages})"
        )
        self.assertGreaterEqual(ages[-1], ages[0])

        # ---- (f) the spawned task body: real _refresh_deep_cache ------------
        health_module._deep_refresh_in_flight = True
        stale_ts = time.monotonic() - (health_module._SHALLOW_CACHE_TTL + 1.0)
        health_module._deep_cache["ts"] = stale_ts
        collect_before = len(self.collect_calls)
        await health_module._refresh_deep_cache(
            app.state, self.llm_checker, self.model_checker
        )
        self.assertEqual(
            len(self.collect_calls), collect_before + 1, "stale => re-collects deep state"
        )
        self.assertGreater(
            health_module._deep_cache["ts"], stale_ts, "stale => refreshes the timestamp"
        )
        self.assertEqual(
            health_module._deep_cache["services"], EXPECTED_SERVICES
        )
        self.assertFalse(
            health_module._deep_refresh_in_flight,
            "finally-clause clears the in-flight flag",
        )
        # Fresh cache => early return, no re-collection, flag untouched.
        collect_before = len(self.collect_calls)
        await health_module._refresh_deep_cache(
            app.state, self.llm_checker, self.model_checker
        )
        self.assertEqual(
            len(self.collect_calls), collect_before, "fresh => no deep collection"
        )
        self.assertFalse(health_module._deep_refresh_in_flight)

        print(
            "PRESERVING GREEN: AC29/FU-003 health last-known cache + "
            "single-flight — deep poll populates _deep_cache; shallow poll "
            "serves cached services without touching checkers and reports "
            "services_cached/services_age_seconds; stale (>=50s) poll spawns "
            "exactly one refresh task; in-flight flag suppresses further "
            "spawns; age is monotonic; _refresh_deep_cache refresh-when-stale "
            "and clears the flag"
        )


if __name__ == "__main__":
    unittest.main()
