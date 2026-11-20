"""Arm's-length acceptance checks for the /api/activity/jobs trace (audit
1115-1157-1158 activity API + tray budget) — RED at base, GREEN after the fix.

The contract under construction (backend/app/api/routes/activity.py, a module
that does NOT exist at base):
- ``GET /api/activity/jobs`` with ``change_token: Optional[str] = None`` and
  ``limit: int = Query(50, ge=1, le=500)``;
- bearer-auth like the sibling routes; vault scoping via the
  ``get_user_accessible_vault_ids`` semantics (superadmin/admin see ALL
  vaults; a plain member sees only vaults granted through vault_members);
- response ``{"jobs": [...], "change_token": "<64-hex>", "changed": <bool>}``
  with each job ``{family, id, vault_id, status, title, created_at,
  completed_at}``; the ``reindex`` family is admin/superadmin-only (issue
  #1039's posture) and jobs are bounded per family by ``limit``;
- delta semantics: a request whose ``change_token`` equals the token the
  server would currently issue answers ``{"jobs": [], "change_token": <same>,
  "changed": false}``; any readable-set change yields a different token,
  ``changed: true`` and a non-empty jobs array.

Companion contract change pinned here: ``GET /api/wiki/jobs`` and
``GET /api/kms/jobs`` return at most the default page size (50) when the
``limit`` query param is OMITTED (today wiki's omitted limit is unbounded and
kms has no limit param at all).

Harness mirrors backend/tests/test_m04_reindex_jobs_list.py: full app.main
app, temp data_dir + SQLiteConnectionPool, _pool_cache reset, superadmin and
member JWTs minted against the "" user-agent fingerprint. Job seeds go
through the unified ``jobs`` table (queue + payload_json {"vault_id": ...})
after ensure_jobs_schema — the same shape the m04 seed helpers use — plus
draft_jobs (draft-room), and files rows in 'processing' (ingest).
"""

CSRF_TEST_POLICY = "naive"  # GET-only suite; no csrf dependency is exercised

import json
import os
import shutil
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Stub missing optional dependencies (copied from test_m04_reindex_jobs_list.py
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

SUPERADMIN_ID = 1
MEMBER_ID = 7
VAULT_A = 11
VAULT_B = 12
VAULT_FORBIDDEN = 13
READABLE_VAULTS = {VAULT_A, VAULT_B}
ALL_VAULTS = {VAULT_A, VAULT_B, VAULT_FORBIDDEN}


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
        return self._token_headers(SUPERADMIN_ID, "superadmin")

    def member_headers(self):
        return self._token_headers(MEMBER_ID, "member")

    def _with_conn(self, fn):
        conn = self.pool.get_connection()
        try:
            return fn(conn)
        finally:
            self.pool.release_connection(conn)

    def seed_queue_job(
        self,
        queue: str,
        vault_id: int | None,
        status: str = "running",
        created_at: str = "2026-11-01 00:00:00",
    ) -> int:
        """One row in the unified jobs table (wiki / kms / reindex queues).

        ``vault_id=None`` seeds the global/legacy shape (payload without a
        concrete vault) — the reindex family's vault-less rows.
        """
        from app.services.job_lease import ensure_jobs_schema

        def _seed(conn):
            ensure_jobs_schema(conn)
            payload = {"vault_id": vault_id, "trigger_type": "manual"}
            cur = conn.execute(
                "INSERT INTO jobs (queue, payload_json, status, attempts, created_at) "
                "VALUES (?, ?, ?, 0, ?)",
                (queue, json.dumps(payload), status, created_at),
            )
            conn.commit()
            return int(cur.lastrowid)

        return self._with_conn(_seed)

    def seed_draft_job(self, vault_id: int, status: str = "running") -> int:
        """One draft_jobs row (draft-room family): a draft + its compile job."""

        def _seed(conn):
            cur = conn.execute(
                "INSERT INTO drafts (vault_id, created_by, title, mode) "
                "VALUES (?, ?, ?, 'compose')",
                (vault_id, MEMBER_ID, f"Draft for vault {vault_id}"),
            )
            draft_id = int(cur.lastrowid)
            cur2 = conn.execute(
                "INSERT INTO draft_jobs (draft_id, vault_id, created_by, job_type, "
                "status, max_model_calls, timeout_seconds) "
                "VALUES (?, ?, ?, 'compile', ?, 5, 60)",
                (draft_id, vault_id, MEMBER_ID, status),
            )
            conn.commit()
            return int(cur2.lastrowid)

        return self._with_conn(_seed)

    def seed_ingest_file(self, vault_id: int) -> int:
        """One files row mid-ingest (ingest family: status 'processing')."""

        def _seed(conn):
            cur = conn.execute(
                "INSERT INTO files (vault_id, file_path, file_name, file_size, status) "
                "VALUES (?, ?, ?, 123, 'processing')",
                (vault_id, f"handbook-{vault_id}.pdf", f"handbook-{vault_id}.pdf"),
            )
            conn.commit()
            return int(cur.lastrowid)

        return self._with_conn(_seed)

    def seed_all_families(self, vault_id: int):
        """One job of every family in `vault_id` (readable-set fixture)."""
        self.seed_queue_job("wiki", vault_id)
        self.seed_queue_job("kms", vault_id)
        self.seed_queue_job("reindex", vault_id)
        self.seed_draft_job(vault_id)
        self.seed_ingest_file(vault_id)


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
    # The unified jobs table is the authoritative store for every queue the
    # activity endpoint aggregates (wiki/kms lease mode is on by default; the
    # reindex lease switch is pinned on so the queue='reindex' seeds are the
    # rows every reader sees).
    monkeypatch.setattr(settings, "reindex_job_lease_enabled", True, raising=False)
    monkeypatch.setattr(settings, "kms_enabled", True, raising=False)
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
            "is_active) VALUES (7, 'plainmember', 'pw-hash', 'Plain Member', "
            "'member', 1)"
        )
        for vault_id in sorted(ALL_VAULTS):
            conn.execute(
                "INSERT OR IGNORE INTO vaults (id, name, description) "
                "VALUES (?, ?, '')",
                (vault_id, f"Activity Test Vault {vault_id}"),
            )
        # Member read grants: VAULT_A + VAULT_B only (VAULT_FORBIDDEN stays
        # out of the readable set).
        conn.execute(
            "INSERT INTO vault_members (vault_id, user_id, permission) "
            "VALUES (?, ?, 'read')",
            (VAULT_A, MEMBER_ID),
        )
        conn.execute(
            "INSERT INTO vault_members (vault_id, user_id, permission) "
            "VALUES (?, ?, 'read')",
            (VAULT_B, MEMBER_ID),
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
# Test 1 — vault scoping + family visibility (member vs superadmin)
# ---------------------------------------------------------------------------


def test_activity_endpoint_is_scoped_to_readable_vaults(route_env):
    """A member sees only the readable vaults' jobs of the four member
    families (no reindex); a superadmin sees all vaults AND the reindex
    family including its vault-less (global) rows."""
    for vault_id in sorted(ALL_VAULTS):
        route_env.seed_all_families(vault_id)
    # One global reindex job (payload without a concrete vault) — the
    # admin-only family's vault-less shape.
    route_env.seed_queue_job("reindex", None, status="running")

    resp = route_env.client.get(
        "/api/activity/jobs", headers=route_env.member_headers()
    )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert {"jobs", "change_token", "changed"} <= set(data.keys())
    jobs = data["jobs"]
    assert jobs, "member call must list the seeded readable jobs"

    # Every job carries the contract shape.
    for job in jobs:
        assert {"family", "id", "vault_id", "status", "title", "created_at",
                "completed_at"} <= set(job.keys())

    # Vault scoping: only the member-readable vaults, never the forbidden one.
    assert {job["vault_id"] for job in jobs} <= READABLE_VAULTS
    assert all(job["vault_id"] != VAULT_FORBIDDEN for job in jobs)

    # The four member families are all present...
    families = {job["family"] for job in jobs}
    assert {"wiki", "kms", "draft-room", "ingest"} <= families
    # ...and the admin-gated reindex family never leaks to a member (#1039).
    assert "reindex" not in families

    # Superadmin: all vaults, reindex present, incl. a NULL-vault_id row.
    resp_admin = route_env.client.get(
        "/api/activity/jobs", headers=route_env.superadmin_headers()
    )
    assert resp_admin.status_code == 200, resp_admin.text
    admin_jobs = resp_admin.json()["jobs"]
    assert admin_jobs
    assert VAULT_FORBIDDEN in {job["vault_id"] for job in admin_jobs}
    reindex_rows = [job for job in admin_jobs if job["family"] == "reindex"]
    assert reindex_rows, "superadmin must see the reindex family"
    assert any(
        job["vault_id"] is None for job in reindex_rows
    ), "the global (vault-less) reindex row must be included for admins"


# ---------------------------------------------------------------------------
# Test 2 — delta semantics via change_token
# ---------------------------------------------------------------------------


def test_activity_endpoint_returns_empty_delta(route_env):
    """Same readable set -> same token, empty jobs, changed=false; a new job
    -> a different token, changed=true, non-empty jobs."""
    route_env.seed_queue_job("wiki", VAULT_A, status="running")
    route_env.seed_queue_job("kms", VAULT_A, status="running")
    route_env.seed_ingest_file(VAULT_A)
    route_env.seed_draft_job(VAULT_A)

    headers = route_env.superadmin_headers()

    first = route_env.client.get("/api/activity/jobs", headers=headers)
    assert first.status_code == 200, first.text
    first_data = first.json()
    assert first_data["jobs"], "initial call must be non-empty"
    token1 = first_data["change_token"]
    assert isinstance(token1, str)
    assert len(token1) == 64
    assert all(c in "0123456789abcdef" for c in token1)

    # Unchanged set: same token -> empty delta.
    second = route_env.client.get(
        "/api/activity/jobs", params={"change_token": token1}, headers=headers
    )
    assert second.status_code == 200, second.text
    second_data = second.json()
    assert second_data["jobs"] == []
    assert second_data["changed"] is False
    assert second_data["change_token"] == token1

    # A new readable job changes the set the token covers.
    route_env.seed_queue_job(
        "wiki", VAULT_A, status="pending", created_at="2026-11-02 00:00:00"
    )
    third = route_env.client.get(
        "/api/activity/jobs", params={"change_token": token1}, headers=headers
    )
    assert third.status_code == 200, third.text
    third_data = third.json()
    assert third_data["changed"] is True
    assert third_data["jobs"], "changed delta must be non-empty"
    assert third_data["change_token"] != token1


# ---------------------------------------------------------------------------
# Test 3 — default-bounded wiki/kms job lists
# ---------------------------------------------------------------------------


def test_job_lists_are_bounded_by_default(route_env):
    """Omitted limit -> at most the default page size (50) on both job-list
    routes; an explicit limit is still honored."""
    from app.services.kms_store import KMSStore
    from app.services.wiki_store import WikiStore

    def _seed_bulk():
        conn = route_env.pool.get_connection()
        try:
            wiki_store = WikiStore(conn)
            kms_store = KMSStore(conn)
            for _ in range(60):
                wiki_store.create_job(vault_id=VAULT_A, trigger_type="manual")
                kms_store.create_job(
                    vault_id=VAULT_A, trigger_type="manual"
                )
            conn.commit()
        finally:
            route_env.pool.release_connection(conn)

    _seed_bulk()
    headers = route_env.member_headers()

    # Omitted limit: at most the default page size on each route.
    wiki_default = route_env.client.get(
        "/api/wiki/jobs", params={"vault_id": VAULT_A}, headers=headers
    )
    assert wiki_default.status_code == 200, wiki_default.text
    assert len(wiki_default.json()["jobs"]) <= 50

    kms_default = route_env.client.get(
        "/api/kms/jobs", params={"vault_id": VAULT_A}, headers=headers
    )
    assert kms_default.status_code == 200, kms_default.text
    assert len(kms_default.json()["jobs"]) <= 50

    # Explicit bound still honored: a limit above the default returns it.
    wiki_bounded = route_env.client.get(
        "/api/wiki/jobs", params={"vault_id": VAULT_A, "limit": 60}, headers=headers
    )
    assert wiki_bounded.status_code == 200, wiki_bounded.text
    assert len(wiki_bounded.json()["jobs"]) == 60

    kms_bounded = route_env.client.get(
        "/api/kms/jobs", params={"vault_id": VAULT_A, "limit": 60}, headers=headers
    )
    assert kms_bounded.status_code == 200, kms_bounded.text
    assert len(kms_bounded.json()["jobs"]) == 60
