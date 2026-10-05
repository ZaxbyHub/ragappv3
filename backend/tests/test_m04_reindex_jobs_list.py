"""Issue #784 "[Workstream M] PR 4 of 4" — backend coverage for the new
read-only list route GET /api/documents/reindex/jobs (the Activity tray's
reindex family source).

Route contract (from the approved plan, 05-fix-plan.md §4):
- admin-gated (require_admin_role); non-admin callers get 403;
- deliberately WITHOUT the sibling per-job route's csrf dependency and
  WITHOUT a rate-limit decorator: the tray polls this route every ~8s and
  the SPA's CSRF interceptor only arms on mutating methods, so either would
  make the family uncallable or silently blank — the tests below pin both
  properties (a no-token GET succeeds; a rapid burst does not 429);
- dual-store read mirroring get_reindex_job_status: the unified ``jobs``
  table (queue='reindex') when settings.reindex_job_lease_enabled, else the
  legacy document_reindex_jobs table, newest first, LIMIT 20;
- the legacy terminal 'interrupted' status maps to 'failed' in BOTH
  branches (the sibling maps it only on the unified path).

Harness mirrors backend/tests/test_m03_ingest_cancel.py (the #783 sibling):
full app.main app, temp data_dir + SQLiteConnectionPool, _pool_cache reset,
superadmin JWT minted against the "" user-agent fingerprint.
"""

CSRF_TEST_POLICY = "manages"  # the no-token GET behaviour is under test

import json
import os
import shutil
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Stub missing optional dependencies (copied from test_m03_ingest_cancel.py
# so app.main imports cleanly without lancedb/pyarrow/unstructured).
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

    _u = types.ModuleType("unstructured")
    _u.__path__ = []
    _u.partition = types.ModuleType("unstructured.partition")
    _u.partition.__path__ = []
    _u.partition.auto = types.ModuleType("unstructured.partition.auto")
    _u.partition.auto.partition = lambda *a, **k: []
    _u.chunking = types.ModuleType("unstructured.chunking")
    _u.chunking.__path__ = []
    _u.chunking.title = types.ModuleType("unstructured.chunking.title")
    _u.chunking.title.chunk_by_title = lambda *a, **k: []
    _u.documents = types.ModuleType("unstructured.documents")
    _u.documents.__path__ = []
    _u.documents.elements = types.ModuleType("unstructured.documents.elements")
    _u.documents.elements.Element = type("Element", (), {})
    sys.modules["unstructured"] = _u
    sys.modules["unstructured.partition"] = _u.partition
    sys.modules["unstructured.partition.auto"] = _u.partition.auto
    sys.modules["unstructured.chunking"] = _u.chunking
    sys.modules["unstructured.chunking.title"] = _u.chunking.title
    sys.modules["unstructured.documents"] = _u.documents
    sys.modules["unstructured.documents.elements"] = _u.documents.elements

import pytest
from fastapi.testclient import TestClient

from app.api.deps import (
    get_background_processor,
    get_db,
    get_db_pool,
    get_embedding_service,
    get_secret_manager,
    get_vector_store,
)
from app.config import settings
from app.main import app
from app.models.database import SQLiteConnectionPool, init_db, run_migrations
from app.services.auth_service import compute_client_fingerprint, create_access_token

VAULT_ID = 2


class _RouteEnv:
    """Per-test namespace: temp DB, pool, TestClient, seeding helpers."""

    def __init__(self, tmp_path: Path, db_path: str, pool: SQLiteConnectionPool):
        self.tmp_path = tmp_path
        self.db_path = db_path
        self.pool = pool
        self.client = TestClient(app)
        self.client.headers["user-agent"] = ""

    def _token_headers(self, user_id: int, role: str):
        token = create_access_token(
            user_id,
            role,
            role,
            client_fingerprint=compute_client_fingerprint(""),
        )
        return {"Authorization": f"Bearer {token}"}

    def superadmin_headers(self):
        return self._token_headers(1, "superadmin")

    def plain_user_headers(self):
        return self._token_headers(7, "viewer")

    def seed_legacy_job(
        self,
        status: str,
        created_at: str,
        vault_id: int = VAULT_ID,
        error: str | None = None,
    ) -> int:
        conn = self.pool.get_connection()
        try:
            cur = conn.execute(
                "INSERT INTO document_reindex_jobs "
                "(vault_id, trigger_type, trigger_id, status, error, created_at) "
                "VALUES (?, 'api', '1', ?, ?, ?)",
                (vault_id, status, error, created_at),
            )
            conn.commit()
            return int(cur.lastrowid)
        finally:
            self.pool.release_connection(conn)

    def seed_unified_job(
        self,
        status: str,
        created_at: str,
        vault_id: int = VAULT_ID,
        attempts: int = 0,
    ) -> int:
        from app.services.job_lease import ensure_jobs_schema

        conn = self.pool.get_connection()
        try:
            ensure_jobs_schema(conn)
            cur = conn.execute(
                "INSERT INTO jobs (queue, payload_json, status, attempts, created_at) "
                "VALUES ('reindex', ?, ?, ?, ?)",
                (
                    json.dumps(
                        {
                            "vault_id": vault_id,
                            "trigger_type": "api",
                            "trigger_id": "1",
                            "input_json": json.dumps({"vault_id": vault_id}),
                        }
                    ),
                    status,
                    attempts,
                    created_at,
                ),
            )
            conn.commit()
            return int(cur.lastrowid)
        finally:
            self.pool.release_connection(conn)


@pytest.fixture()
def route_env(tmp_path, monkeypatch):
    """Temp DB + full-app TestClient with dependency overrides seeded."""
    original = (
        settings.jwt_secret_key,
        settings.users_enabled,
        settings.data_dir,
    )
    settings.jwt_secret_key = "test-secret-key-for-testing-at-least-32-chars-long"
    settings.users_enabled = True
    settings.data_dir = tmp_path
    monkeypatch.setattr(settings, "reindex_job_lease_enabled", False, raising=False)
    db_path = str(tmp_path / "app.db")

    from app.models.database import _pool_cache, _pool_cache_lock

    with _pool_cache_lock:
        for cached_pool in list(_pool_cache.values()):
            cached_pool.close_all()
        _pool_cache.clear()

    init_db(db_path)
    run_migrations(db_path)
    pool = SQLiteConnectionPool(db_path, max_size=6)

    def override_get_db():
        conn = pool.get_connection()
        try:
            yield conn
        finally:
            pool.release_connection(conn)

    mock_vec = MagicMock()
    mock_vec.db = None
    mock_vec.delete_by_file = AsyncMock(return_value=1)
    mock_emb = MagicMock()
    mock_bp = MagicMock()
    mock_bp.is_running = True
    mock_bp.enqueue = AsyncMock()
    mock_sm = MagicMock()
    mock_sm.get_hmac_key.return_value = (b"test-hmac-key-32bytes-padding!!", "v1")

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_vector_store] = lambda: mock_vec
    app.dependency_overrides[get_embedding_service] = lambda: mock_emb
    app.dependency_overrides[get_db_pool] = lambda: pool
    app.dependency_overrides[get_background_processor] = lambda: mock_bp
    app.dependency_overrides[get_secret_manager] = lambda: mock_sm

    conn = pool.get_connection()
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("DELETE FROM files")
        conn.execute("DELETE FROM vault_members")
        conn.execute("DELETE FROM users WHERE id != 0")
        conn.execute(
            "INSERT INTO users (id, username, hashed_password, full_name, role, "
            "is_active) VALUES (1, 'superadmin', 'pw-hash', 'Super Admin', "
            "'superadmin', 1)"
        )
        conn.execute(
            "INSERT INTO users (id, username, hashed_password, full_name, role, "
            "is_active) VALUES (7, 'plainuser', 'pw-hash', 'Plain User', "
            "'viewer', 1)"
        )
        conn.execute(
            "INSERT OR IGNORE INTO vaults (id, name, description) "
            "VALUES (?, 'Reindex List Test Vault', '')",
            (VAULT_ID,),
        )
        conn.commit()
    finally:
        pool.release_connection(conn)

    yield _RouteEnv(tmp_path, db_path, pool)

    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_vector_store, None)
    app.dependency_overrides.pop(get_embedding_service, None)
    app.dependency_overrides.pop(get_db_pool, None)
    app.dependency_overrides.pop(get_background_processor, None)
    app.dependency_overrides.pop(get_secret_manager, None)

    with _pool_cache_lock:
        for cached_pool in list(_pool_cache.values()):
            cached_pool.close_all()
        _pool_cache.clear()
    pool.close_all()
    settings.jwt_secret_key, settings.users_enabled, settings.data_dir = original
    shutil.rmtree(tmp_path, ignore_errors=True)


# ---------------------------------------------------------------------------
# Legacy store (lease switch off)
# ---------------------------------------------------------------------------


def test_lists_legacy_jobs_newest_first_and_maps_interrupted(route_env):
    """Legacy rows come back newest-first and 'interrupted' maps to 'failed'."""
    completed_id = route_env.seed_legacy_job("completed", "2026-01-01 00:00:01")
    interrupted_id = route_env.seed_legacy_job(
        "interrupted", "2026-01-02 00:00:02", error="worker died"
    )
    newest_id = route_env.seed_legacy_job("running", "2026-01-03 00:00:03")

    resp = route_env.client.get(
        "/api/documents/reindex/jobs", headers=route_env.superadmin_headers()
    )

    assert resp.status_code == 200
    jobs = resp.json()["jobs"]
    assert [job["id"] for job in jobs] == [newest_id, interrupted_id, completed_id]
    by_id = {job["id"]: job for job in jobs}
    assert by_id[interrupted_id]["status"] == "failed"
    assert by_id[interrupted_id]["error"] == "worker died"
    assert by_id[newest_id]["status"] == "running"
    assert by_id[newest_id]["vault_id"] == VAULT_ID


def test_legacy_list_caps_at_twenty(route_env):
    """LIMIT 20: the 25th-oldest row drops off."""
    for i in range(25):
        route_env.seed_legacy_job(
            "pending", f"2026-01-01 00:00:{i:02d}"
        )

    resp = route_env.client.get(
        "/api/documents/reindex/jobs", headers=route_env.superadmin_headers()
    )

    assert resp.status_code == 200
    assert len(resp.json()["jobs"]) == 20


# ---------------------------------------------------------------------------
# Unified store (lease switch on)
# ---------------------------------------------------------------------------


def test_lists_unified_jobs_with_payload_fields(route_env, monkeypatch):
    """Lease-on rows map vault_id/trigger from payload_json and attempts to
    retry_count. ('interrupted' cannot be seeded here: the unified table's
    CHECK forbids it — legacy rows are migrated in as failed already, and
    the route's unified-branch CASE mapping is defensive-only, mirroring
    the sibling per-job route; the legacy-branch mapping is asserted
    above.)"""
    monkeypatch.setattr(settings, "reindex_job_lease_enabled", True, raising=False)
    completed_id = route_env.seed_unified_job("completed", "2026-02-01 00:00:01", attempts=1)
    running_id = route_env.seed_unified_job(
        "running", "2026-02-02 00:00:02", attempts=2
    )

    resp = route_env.client.get(
        "/api/documents/reindex/jobs", headers=route_env.superadmin_headers()
    )

    assert resp.status_code == 200
    jobs = resp.json()["jobs"]
    assert [job["id"] for job in jobs] == [running_id, completed_id]
    by_id = {job["id"]: job for job in jobs}
    assert by_id[running_id]["vault_id"] == VAULT_ID
    assert by_id[running_id]["trigger_type"] == "api"
    assert by_id[running_id]["retry_count"] == 2


# ---------------------------------------------------------------------------
# Auth / CSRF-free / poll fitness
# ---------------------------------------------------------------------------


def test_non_admin_gets_403(route_env):
    """require_admin_role: a plain authenticated user is refused."""
    route_env.seed_legacy_job("running", "2026-01-01 00:00:01")

    resp = route_env.client.get(
        "/api/documents/reindex/jobs", headers=route_env.plain_user_headers()
    )

    assert resp.status_code == 403


def test_get_without_token_header_succeeds_for_admin(route_env):
    """No csrf dependency on this GET: an admin request carrying no token
    header at all still lists (the SPA's interceptor only arms on mutating
    methods, so a guarded GET would be uncallable from the frontend)."""
    route_env.seed_legacy_job("running", "2026-01-01 00:00:01")

    resp = route_env.client.get(
        "/api/documents/reindex/jobs", headers=route_env.superadmin_headers()
    )

    assert resp.status_code == 200
    assert len(resp.json()["jobs"]) == 1


def test_rapid_polling_burst_does_not_rate_limit(route_env):
    """Poll fitness: the tray hits this route every ~8s per open admin tab;
    a burst well past that rate must not 429 (the sibling per-job route's
    admin_rate_limit is deliberately NOT mirrored here)."""
    route_env.seed_legacy_job("running", "2026-01-01 00:00:01")

    statuses = [
        route_env.client.get(
            "/api/documents/reindex/jobs", headers=route_env.superadmin_headers()
        ).status_code
        for _ in range(25)
    ]

    assert statuses == [200] * 25
