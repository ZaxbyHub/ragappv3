"""Acceptance checks for the /api/healthz readiness trace (frozen spec).

These tests encode the issue contract for GET /api/healthz readiness signals:
migration failure, vector-store readiness (_ready), maintenance flag
(reported but NON-blocking), pool saturation, the C02 no-pooled-read guard,
the compose healthcheck target, no-telemetry-deps, and preservation of the
existing healthy/degraded behavior.

Expected status at the PRE-FIX code (this is intentional — the discriminating
checks prove the fix is required):
  FAIL : AC1, AC2, AC3, AC4, AC6
  PASS : AC5, AC7, AC8a, AC8b

Every behavioral assertion goes through the real HTTP route
(`self.client.get("/api/healthz")`); only AC5/AC6/AC7 are source-level.
No mocks are used and no assertion inspects a mock's call state.
"""

import inspect
import os
import re
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Stub missing optional dependencies (same bootstrap pattern as
# tests/test_api_routes.py; kept standalone so this module imports cleanly
# even when run without the conftest stubs).
try:
    import lancedb  # noqa: F401
except ImportError:
    sys.modules["lancedb"] = types.ModuleType("lancedb")

try:
    import pyarrow  # noqa: F401
except ImportError:
    sys.modules["pyarrow"] = types.ModuleType("pyarrow")

try:
    from unstructured.partition.auto import partition  # noqa: F401
except ImportError:
    _unstructured = types.ModuleType("unstructured")
    _unstructured.__path__ = []
    _unstructured.partition = types.ModuleType("unstructured.partition")
    _unstructured.partition.__path__ = []
    _unstructured.partition.auto = types.ModuleType("unstructured.partition.auto")
    _unstructured.partition.auto.partition = lambda *args, **kwargs: []
    _unstructured.chunking = types.ModuleType("unstructured.chunking")
    _unstructured.chunking.__path__ = []
    _unstructured.chunking.title = types.ModuleType("unstructured.chunking.title")
    _unstructured.chunking.title.chunk_by_title = lambda *args, **kwargs: []
    _unstructured.documents = types.ModuleType("unstructured.documents")
    _unstructured.documents.__path__ = []
    _unstructured.documents.elements = types.ModuleType(
        "unstructured.documents.elements"
    )
    _unstructured.documents.elements.Element = type("Element", (), {})
    sys.modules["unstructured"] = _unstructured
    sys.modules["unstructured.partition"] = _unstructured.partition
    sys.modules["unstructured.partition.auto"] = _unstructured.partition.auto
    sys.modules["unstructured.chunking"] = _unstructured.chunking
    sys.modules["unstructured.chunking.title"] = _unstructured.chunking.title
    sys.modules["unstructured.documents"] = _unstructured.documents
    sys.modules["unstructured.documents.elements"] = _unstructured.documents.elements

import yaml
from fastapi.testclient import TestClient

from app.api.routes import health as health_module
from app.main import app
from app.models.database import SQLiteConnectionPool, init_db
from app.services.maintenance import MaintenanceService

# Every app.state attribute any test in this file may touch. `app` is a
# module-global shared by the whole suite, so setUp snapshots each attribute
# (including its ABSENCE) and tearDown restores it exactly.
_STATE_ATTRS = (
    "db_pool",
    "vector_store",
    "embedding_service",
    "maintenance_service",
    "migrations_ok",
)


class _StubVectorStore:
    """Minimal vector-store stand-in: truthy ``.table`` plus an explicit
    ``_ready`` flag (False mirrors the post-embedding-model-mismatch state
    that VectorStore sets on itself; see services/vector_store.py)."""

    def __init__(self, ready: bool = True):
        self.table = object()  # truthy => "connected" for the presence check
        self._ready = ready


class HealthzTestCase(unittest.TestCase):
    """Base: a TestClient (no lifespan, like test_api_routes.py) plus strict
    snapshot/restore isolation for every app.state attribute touched."""

    def setUp(self):
        self.client = TestClient(app)
        self._state_snapshot = {
            name: (hasattr(app.state, name), getattr(app.state, name, None))
            for name in _STATE_ATTRS
        }

    def tearDown(self):
        for name, (present, value) in self._state_snapshot.items():
            if present:
                setattr(app.state, name, value)
            elif hasattr(app.state, name):
                delattr(app.state, name)

    def _install_healthy_state(self, db_pool):
        """Put every healthz component into its healthy configuration.

        maintenance_service is pinned to None so tests are deterministic
        regardless of what earlier files left on the shared app.state; the
        maintenance-specific tests overwrite it with a real service.
        """
        app.state.db_pool = db_pool
        app.state.vector_store = _StubVectorStore(ready=True)
        app.state.embedding_service = object()
        app.state.migrations_ok = True
        app.state.maintenance_service = None


class HealthzDbTestCase(HealthzTestCase):
    """Adds a temp SQLite DB (init_db) and pool lifecycle tracking."""

    def setUp(self):
        super().setUp()
        self.temp_dir = tempfile.mkdtemp(prefix="healthz_readiness_")
        self.db_path = str(Path(self.temp_dir) / "healthz.db")
        init_db(self.db_path)
        self._pools = []
        self._held = []  # (pool, conn) pairs still checked out at teardown

    def tearDown(self):
        super().tearDown()  # restore app.state before closing anything it saw
        for pool, conn in self._held:
            try:
                pool.release_connection(conn)
            except Exception:
                pass
        for pool in self._pools:
            try:
                pool.close_all()
            except Exception:
                pass
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _new_pool(self, max_size: int = 2) -> SQLiteConnectionPool:
        pool = SQLiteConnectionPool(self.db_path, max_size=max_size)
        self._pools.append(pool)
        return pool


class TestHealthzReadinessSignals(HealthzDbTestCase):
    """AC1-AC4 and AC8: readiness behavior through the real HTTP route."""

    def test_ac1_migration_failure_returns_503(self):
        """AC1: a failed startup migration must fail readiness with a
        migration-mentioning issue. The failure state reaches the route via
        app.state.migrations_ok == False (set directly, as the issue allows:
        absent/None/True means not-failed)."""
        self._install_healthy_state(self._new_pool())
        app.state.migrations_ok = False

        response = self.client.get("/api/healthz")

        self.assertEqual(response.status_code, 503, response.text)
        self.assertIn("migration", response.text.lower(), response.text)

    def test_ac2_vector_store_not_ready_returns_503(self):
        """AC2: a vector store with a truthy .table but _ready=False (the
        post-embedding-model-mismatch state) must fail readiness with an
        issue mentioning the vector store."""
        self._install_healthy_state(self._new_pool())
        app.state.vector_store = _StubVectorStore(ready=False)

        response = self.client.get("/api/healthz")

        self.assertEqual(response.status_code, 503, response.text)
        self.assertIn("vector", response.text.lower(), response.text)

    def test_ac3_maintenance_reported_nonblocking(self):
        """AC3: an ENABLED maintenance flag (real MaintenanceService over a
        real SQLiteConnectionPool on an init_db'd temp DB) must be reported
        in the response body but must NOT block the readiness route."""
        maintenance = MaintenanceService(self._new_pool())
        maintenance.set_flag(True, "scheduled maintenance")
        self._install_healthy_state(self._new_pool())
        app.state.maintenance_service = maintenance

        response = self.client.get("/api/healthz")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("maintenance", response.text.lower(), response.text)

    def test_ac4_pool_saturation_reported(self):
        """AC4: a pool that recently forced a caller to WAIT for a connection
        (capacity wait, all waiters timing out) must surface as an issue.

        Saturation is driven through the pool's real public behavior: check
        out the only connection of a max_size=1 pool, then attempt a second
        checkout expecting RuntimeError (~5s — the internal queue get uses
        timeout=5; accepted cost, no internal mocking). The held connection
        stays checked out for the request so both a "recorded wait" and a
        "currently at capacity" implementation satisfy the check. Assertions
        target the HTTP response only — the fix owns how the wait is
        recorded."""
        pool = self._new_pool(max_size=1)
        held = pool.get_connection()
        self._held.append((pool, held))
        with self.assertRaises(RuntimeError):
            pool.get_connection(max_wait_attempts=1)

        self._install_healthy_state(pool)
        response = self.client.get("/api/healthz")

        self.assertEqual(response.status_code, 503, response.text)
        lowered = response.text.lower()
        self.assertTrue(
            any(word in lowered for word in ("pool", "connection", "saturat")),
            f"healthz body must mention pool/connection saturation: {response.text}",
        )

    def test_prr002_degraded_with_maintenance_warning(self):
        """PRR-002 (review round): a blocking issue AND an enabled maintenance
        flag together must yield a 503 whose body carries BOTH the issues list
        and the non-blocking maintenance warning."""
        maintenance = MaintenanceService(self._new_pool())
        maintenance.set_flag(True, "scheduled maintenance")
        self._install_healthy_state(self._new_pool())
        app.state.maintenance_service = maintenance
        app.state.migrations_ok = False

        response = self.client.get("/api/healthz")

        self.assertEqual(response.status_code, 503, response.text)
        body = response.json()
        self.assertIn("migration", str(body).lower())
        self.assertIn("maintenance", str(body).lower())
        self.assertTrue(any("maintenance" in w for w in body["warnings"]), body)

    def test_prr003_flag_read_failure_swallowed(self):
        """PRR-003 (review round): a maintenance service whose flag read raises
        must not break the probe — the route answers normally with no
        maintenance warning (documented fail-open contract)."""

        class _BrokenMaintenance:
            # Shape-compatible with MaintenanceService for the route's
            # capability detection; every read raises.
            async def get_flag_async(self):
                raise RuntimeError("flag store unavailable")

        self._install_healthy_state(self._new_pool())
        app.state.maintenance_service = _BrokenMaintenance()

        response = self.client.get("/api/healthz")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"status": "ok"})
        self.assertNotIn("maintenance", response.text.lower())

    def test_ac8a_healthy_state_ok(self):
        """AC8a (preserving): with every component healthy and the
        maintenance flag disabled, healthz returns 200 with exactly
        {"status": "ok"}."""
        maintenance = MaintenanceService(self._new_pool())  # flag disabled
        self._install_healthy_state(self._new_pool())
        app.state.maintenance_service = maintenance

        response = self.client.get("/api/healthz")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_ac8b_existing_presence_checks_still_503(self):
        """AC8b (preserving): the three attribute-presence failures keep
        returning 503 with their component named (current wording:
        'db_pool not initialized' / 'embedding_service not initialized' /
        'vector_store not connected')."""
        # db_pool attribute absent
        self._install_healthy_state(self._new_pool())
        delattr(app.state, "db_pool")
        response = self.client.get("/api/healthz")
        self.assertEqual(response.status_code, 503, response.text)
        self.assertIn("db_pool", response.text.lower(), response.text)

        # embedding_service attribute absent
        self._install_healthy_state(self._new_pool())
        delattr(app.state, "embedding_service")
        response = self.client.get("/api/healthz")
        self.assertEqual(response.status_code, 503, response.text)
        self.assertIn("embedding_service", response.text.lower(), response.text)

        # vector_store present but .table falsy
        self._install_healthy_state(self._new_pool())
        not_connected = _StubVectorStore(ready=True)
        not_connected.table = None
        app.state.vector_store = not_connected
        response = self.client.get("/api/healthz")
        self.assertEqual(response.status_code, 503, response.text)
        self.assertIn("vector", response.text.lower(), response.text)


class TestHealthzSourceGuards(unittest.TestCase):
    """AC5 and AC7: source-level invariants (no app.state mutation)."""

    def test_ac5_healthz_source_has_no_pooled_read(self):
        """AC5 (preserving, C02 guard): the healthz handler must not check a
        connection out of the pool, call the raw get_flag(, or run raw SQL —
        no synchronous pooled SQLite query on the event loop in the readiness
        path. Note "get_flag(" is NOT a substring of "get_flag_async(", so
        the cached off-loop read stays allowed."""
        source = inspect.getsource(health_module.healthz)
        self.assertNotIn(
            "get_connection", source, "healthz must not check out a pooled connection"
        )
        self.assertNotIn(
            "get_flag(",
            source,
            "healthz must use the cached get_flag_async( read, not raw get_flag(",
        )
        self.assertNotIn(
            ".execute(", source, "healthz must not run raw SQL in the readiness path"
        )

    def test_ac7_no_telemetry_dependencies(self):
        """AC7 (preserving): no opentelemetry/prometheus import in the health
        route module and no such package in backend/requirements.txt."""
        module_source = inspect.getsource(health_module).lower()
        self.assertNotIn("opentelemetry", module_source)
        self.assertNotIn("prometheus", module_source)

        backend_dir = Path(__file__).resolve().parents[1]
        requirements = (backend_dir / "requirements.txt").read_text(encoding="utf-8")
        active_lines = " ".join(
            line
            for line in requirements.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ).lower()
        self.assertNotIn("opentelemetry", active_lines)
        self.assertNotIn("prometheus", active_lines)


class TestComposeHealthcheckTarget(unittest.TestCase):
    """AC6: the repo-root compose file's backend healthcheck (the one probing
    localhost:9090) must target /api/healthz, not the legacy /health."""

    def test_ac6_compose_healthcheck_targets_healthz(self):
        compose_path = Path(__file__).resolve().parents[2] / "docker-compose.yml"
        self.assertTrue(compose_path.exists(), f"missing compose file: {compose_path}")
        with open(compose_path, encoding="utf-8") as fh:
            compose = yaml.safe_load(fh)

        matches = []
        for name, service in (compose.get("services") or {}).items():
            healthcheck = (service or {}).get("healthcheck") or {}
            test_entry = healthcheck.get("test")
            if not test_entry:
                continue
            command = (
                " ".join(test_entry) if isinstance(test_entry, list) else str(test_entry)
            )
            if "localhost:9090" in command:
                matches.append((name, command))
        self.assertEqual(
            len(matches), 1, f"expected exactly one localhost:9090 probe: {matches}"
        )

        _service, command = matches[0]
        self.assertIn(
            "/api/healthz", command, "the backend healthcheck must probe /api/healthz"
        )
        # "/health" must be gone as the probe path. "/healthz" contains the
        # substring "/health", so guard with a lookahead instead of a plain
        # substring check.
        self.assertIsNone(
            re.search(r"/health(?!z)", command),
            f"the backend healthcheck must not probe the legacy /health path: {command}",
        )


if __name__ == "__main__":
    unittest.main()
