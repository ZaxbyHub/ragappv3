"""
Issue #494 acceptance check — AC32 (FU-006): health.py serves last-known
service status from module-level cache globals without any cross-worker
guard.

Root cause (verified at base a543361): app/api/routes/health.py keeps the
deep-health result in module globals (``_deep_cache`` /
``_deep_refresh_in_flight`` / ``_refresh_tasks``). With more than one uvicorn
worker, each worker keeps its own copy, so the cache's "single-flight"
refresh and its freshness claims are wrong — the deps.py convention of
documenting that caveat is absent, and nothing warns/fails when the module
cache is run with workers>1.

Planned fix surface (NEW-SURFACE check): the health module must expose a
documented worker guard — a module symbol that encodes the single-worker
constraint — AND the guard must be consumed by the module's own code
(plan-critic Round 1 amendment; a never-referenced constant is a no-op).
"""

import os
import sys
import unittest

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# Stub missing optional dependencies (mirrors the other backend test files)
try:
    import lancedb  # noqa: F401
except ImportError:
    import types
    sys.modules['lancedb'] = types.ModuleType('lancedb')

try:
    import pyarrow  # noqa: F401
except ImportError:
    import types
    sys.modules['pyarrow'] = types.ModuleType('pyarrow')

try:
    from unstructured.partition.auto import partition  # noqa: F401
except ImportError:
    import types
    _unstructured = types.ModuleType('unstructured')
    _unstructured.__path__ = []
    _unstructured.partition = types.ModuleType('unstructured.partition')
    _unstructured.partition.__path__ = []
    _unstructured.partition.auto = types.ModuleType('unstructured.partition.auto')
    _unstructured.partition.auto.partition = lambda *args, **kwargs: []
    sys.modules['unstructured'] = _unstructured
    sys.modules['unstructured.partition'] = _unstructured.partition
    sys.modules['unstructured.partition.auto'] = _unstructured.partition.auto

from app.api.routes import health  # noqa: E402


class TestHealthModuleWorkerGuard(unittest.TestCase):
    """AC32 — DISCRIMINATING (new surface): a worker guard must exist."""

    def test_ac32_health_module_exposes_worker_guard(self):
        """The module-level cache must be accompanied by a WIRED worker guard.

        Amended per plan-critic Round 1 (CHECK_WRONG: the original shape-only
        contract passed on a never-referenced constant). The guard must (a)
        exist as a module symbol encoding the single-worker constraint AND
        (b) be consumed inside the module's own source — a declaration that
        nothing references is a no-op and must fail this check.
        """
        guards = []

        flag = getattr(health, "_SINGLE_WORKER_ASSERTION", None)
        if flag:
            guards.append("_SINGLE_WORKER_ASSERTION")

        for name in dir(health):
            if "worker" not in name.lower():
                continue
            attr = getattr(health, name)
            if callable(attr) or attr:
                guards.append(name)

        print("AC32 CHECK: FAIL")
        self.assertNotEqual(
            guards,
            [],
            "app/api/routes/health.py serves last-known status from "
            "module-level cache globals but exposes no worker guard — add a "
            "documented single-worker assertion/flag or a worker-count "
            "settings check so multi-worker deployments cannot silently "
            "rely on per-worker caches",
        )
        # Amendment 2 (implementation review, NEEDS_REVISION item 1): the
        # reference-count wiring test above still passes when the CALL SITE is
        # commented out (the function body itself references the constant).
        # The decisive discriminator is runtime: the guard must actually be
        # invoked when the /api/health endpoint serves a request. Spy on every
        # callable guard symbol and drive one real request through the route
        # (same harness shape as the AC29 check: TestClient without lifespan
        # startup, checker dependencies overridden).
        from unittest import mock

        from fastapi.testclient import TestClient

        from app.api.deps import get_llm_health_checker, get_model_checker
        from app.main import app

        class _StubChecker:
            async def check_all(self):
                return {"backend": True, "embeddings": True, "chat": True}

            async def check_models(self):
                return {"ok": True, "models": {}}

        class _StubVectorStore:
            def get_stats(self):
                return {"total_chunks": 0}

        callables_to_spy = [
            name
            for name in guards
            if callable(getattr(health, name, None))
        ]
        self.assertNotEqual(
            callables_to_spy,
            [],
            "no callable worker guard to verify on the request path — a "
            "constant alone cannot act when a multi-worker deployment serves "
            "requests",
        )
        had_vs = hasattr(app.state, "vector_store")
        orig_vs = getattr(app.state, "vector_store", None)
        app.state.vector_store = _StubVectorStore()
        app.dependency_overrides[get_llm_health_checker] = lambda: _StubChecker()
        app.dependency_overrides[get_model_checker] = lambda: _StubChecker()
        spies = {}
        try:
            for name in callables_to_spy:
                spies[name] = mock.patch.object(
                    health, name, wraps=getattr(health, name)
                ).start()
            client = TestClient(app)
            resp = client.get("/api/health")
            self.assertEqual(resp.status_code, 200)
        finally:
            mock.patch.stopall()
            app.dependency_overrides.pop(get_llm_health_checker, None)
            app.dependency_overrides.pop(get_model_checker, None)
            if had_vs:
                app.state.vector_store = orig_vs
            else:
                delattr(app.state, "vector_store")
        invoked = [name for name, spy in spies.items() if spy.called]
        self.assertNotEqual(
            invoked,
            [],
            "worker guard is defined but NOT invoked when /api/health serves "
            "a request — a commented-out or dead call site is a no-op guard; "
            "the serve path must call the warning/assertion helper on every "
            "request (or at first serve)",
        )


if __name__ == "__main__":
    unittest.main()
