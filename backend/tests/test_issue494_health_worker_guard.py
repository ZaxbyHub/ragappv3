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
        # Amendment (plan-critic Round 1): the guard must be referenced in the
        # module's own source — scanning dir(health) catches imports too, but
        # only a same-module reference proves the cache code path consults it.
        import inspect
        source = inspect.getsource(health)
        wired = []
        for guard_name in guards:
            references = source.count(guard_name)
            # One reference is the definition itself; a wired guard appears
            # at least twice (definition + use) or is imported-but-unused=0.
            if references >= 2 or (callable(getattr(health, guard_name, None)) and references >= 2):
                wired.append(guard_name)
        self.assertNotEqual(
            wired,
            [],
            "worker guard symbols exist in health.py but none is referenced "
            "by the module's own code — a never-consumed constant cannot "
            "protect the module-level deep-health cache; wire the guard into "
            "the refresh/serve path (e.g. log-once warning when "
            "WEB_CONCURRENCY != 1)",
        )


if __name__ == "__main__":
    unittest.main()
