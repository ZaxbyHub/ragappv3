"""D04 acceptance checks for issue #722: user-mutation invariants.

Frozen arm's-length acceptance checks (issue-tracer Phase 2.5). Each test is
RED at base, GREEN after the fix:

1. test_mutation_writes_security_audit_event — every destructive/mutating
   admin route (delete_user, delete_vault, update_vault visibility,
   update_vault_groups) appends exactly one security_audit_log row.
2. test_refresh_cookie_from_before_deactivation_rejected_after_reactivation —
   deactivation must revoke live refresh sessions, so a cookie minted before
   deactivation is rejected (401) even after the account is reactivated.
3-5. superadmin-target checks — a plain admin may not edit a superadmin's
   profile (update_user without role), reset a superadmin's password, or
   deactivate a superadmin (403 expected).
6. test_update_user_role_change_invokes_last_superadmin_guard — PATCH
   /users/{id} with a role change must refuse to demote the last active
   superadmin (parity with the /role endpoint's atomic guard).
7. test_password_reset_audit_reports_actual_session_revocation — the
   user.password_reset audit metadata must report sessions_revoked truthfully
   (False when the user_sessions table is absent and nothing was revoked).
8. test_delete_user_who_created_org_but_no_longer_owns_it — deleting a user
   who only created (no longer owns) an organization must succeed, not die on
   the organizations.created_by FK.
9. test_sessions_list_omits_session_expired_earlier_today — GET /auth/sessions
   must not return a session whose expires_at is earlier today (the raw
   SQLite string comparison "T..." > " ..." wrongly keeps it).
10. test_login_purges_expired_session_rows — login purges the user's expired
    user_sessions rows while leaving unexpired ones intact.

Harness mirrors the proven recipe in backend/tests/test_users_routes.py
(env vars before app imports, pool override, csrf dependency override,
users.get_pool patch, settings patch, user-agent ""). Tables missing from
TEST_SCHEMA (security_audit_log, user_sessions) are created locally from the
backend/app/models/database.py DDL; no shared helper is modified.
"""

import os

# Set up test environment BEFORE importing app modules
os.environ["JWT_SECRET_KEY"] = (
    "test-jwt-secret-key-for-testing-only-12345678901234567890"
)
os.environ["USERS_ENABLED"] = "true"
os.environ["ADMIN_SECRET_TOKEN"] = (
    "d04-admin-secret-token-0123456789abcdef0123456789abcdef"
)

import contextlib
import json
import shutil
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from backend.tests.user_route_helpers import create_user, get_token, setup_test_db
from fastapi import FastAPI
from fastapi.testclient import TestClient

# security_audit_log is not part of TEST_SCHEMA; DDL mirrors
# backend/app/models/database.py so safe_record_security_event works.
SECURITY_AUDIT_LOG_DDL = """
CREATE TABLE IF NOT EXISTS security_audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    actor_user_id INTEGER,
    actor_username TEXT,
    target_user_id INTEGER,
    target_username TEXT,
    ip_address TEXT,
    user_agent TEXT,
    metadata_json TEXT,
    key_version TEXT NOT NULL,
    hmac_sha256 TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

# user_sessions is not part of TEST_SCHEMA; DDL mirrors the base table in
# backend/app/models/database.py (refresh sessions created by /auth/login).
USER_SESSIONS_DDL = """
CREATE TABLE IF NOT EXISTS user_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    refresh_token_hash TEXT NOT NULL,
    expires_at TIMESTAMP NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_used_at TIMESTAMP,
    ip_address TEXT,
    user_agent TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
"""


class _Env:
    """Per-test environment: db connection, pool-backed app, client."""


@contextlib.contextmanager
def _mutation_env(
    *,
    mount_users: bool = True,
    mount_auth: bool = False,
    mount_vaults: bool = False,
    full_schema: bool = False,
):
    """Build an isolated temp DB + FastAPI app with the requested routers.

    Mirrors test_users_routes.py isolation: _pool_cache.clear(), own tmpdir,
    own pool, get_db override, csrf_protect override, users.get_pool patch,
    settings patch, and a client whose user-agent matches the fingerprint the
    test tokens are minted with ("").
    """
    from app.models.database import _pool_cache

    _pool_cache.clear()

    tmpdir = tempfile.mkdtemp()
    db_path = os.path.join(tmpdir, "d04.db")

    if full_schema:
        # Production schema (init_db) for the vault cascade routes, which
        # touch drafts/files/memories/chat_sessions/document_assets/... tables
        # that TEST_SCHEMA does not carry.
        from app.models.database import init_db

        init_db(db_path)
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.commit()
    else:
        conn = setup_test_db(db_path)
        conn.executescript(SECURITY_AUDIT_LOG_DDL)
        conn.executescript(USER_SESSIONS_DDL)
        conn.commit()

    app = FastAPI()
    patched_get_pools = []

    if mount_users:
        from app.api.routes import users as users_mod
        from app.api.routes.users import router as users_router

        app.include_router(users_router)
        patched_get_pools.append((users_mod, users_mod.get_pool))

    if mount_auth:
        from app.api.routes.auth import router as auth_router

        app.include_router(auth_router)

    if mount_vaults:
        from app.api.routes.vaults import router as vaults_router

        app.include_router(vaults_router)

    from app.models.database import SQLiteConnectionPool

    pool = SQLiteConnectionPool(db_path, max_size=5)
    for mod, _orig in patched_get_pools:
        mod.get_pool = lambda _path, _pool=pool: _pool

    from app.api import deps
    from app.security import csrf_protect

    def override_get_db():
        c = pool.get_connection()
        try:
            yield c
        finally:
            pool.release_connection(c)

    app.dependency_overrides[deps.get_db] = override_get_db
    app.dependency_overrides[csrf_protect] = lambda: "test-csrf-token"

    if mount_vaults:
        mock_vector_store = MagicMock()
        mock_vector_store.delete_by_vault = AsyncMock(return_value=0)
        app.dependency_overrides[deps.get_vector_store] = (
            lambda: mock_vector_store
        )

    from app.config import settings

    saved_users_enabled = settings.users_enabled
    saved_jwt_secret = settings.jwt_secret_key
    settings.users_enabled = True
    settings.jwt_secret_key = os.environ["JWT_SECRET_KEY"]

    client = TestClient(app)
    client.headers["user-agent"] = ""

    env = _Env()
    env.conn = conn
    env.app = app
    env.client = client
    env.pool = pool
    env.db_path = db_path
    try:
        yield env
    finally:
        client.close()
        _pool_cache.clear()
        conn.close()
        for mod, orig in patched_get_pools:
            mod.get_pool = orig
        pool.close_all()
        settings.users_enabled = saved_users_enabled
        settings.jwt_secret_key = saved_jwt_secret
        shutil.rmtree(tmpdir, ignore_errors=True)


def _audit_count(env) -> int:
    return env.conn.execute(
        "SELECT COUNT(*) FROM security_audit_log"
    ).fetchone()[0]


def _bearer(env, user_id: int, username: str, role: str) -> dict:
    return {"Authorization": f"Bearer {get_token(user_id, username, role)}"}


def _insert_vault(env, name: str, org_id=None) -> int:
    cur = env.conn.execute(
        "INSERT INTO vaults (name, org_id) VALUES (?, ?)", (name, org_id)
    )
    env.conn.commit()
    return cur.lastrowid


# ---------------------------------------------------------------------------
# AC1 — destructive mutations must append exactly one security audit event.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "route_case",
    [
        "delete_user",
        "delete_vault",
        "update_vault_visibility",
        "update_vault_groups",
    ],
    ids=["delete_user", "delete_vault", "update_vault_visibility", "update_vault_groups"],
)
def test_mutation_writes_security_audit_event(route_case):
    """Each mutating admin route appends exactly one security_audit_log row."""
    with _mutation_env(mount_vaults=True, full_schema=True) as env:
        superadmin_id = create_user(
            env.conn, "superadmin", "pass123", "superadmin", "Super Admin"
        )
        headers = _bearer(env, superadmin_id, "superadmin", "superadmin")
        body = None

        if route_case == "delete_user":
            member_id = create_user(env.conn, "member", "pass123", "member", "M")
            method, path = "DELETE", f"/users/{member_id}"
        elif route_case == "delete_vault":
            vault_id = _insert_vault(env, "D04 Vault")
            method, path = "DELETE", f"/vaults/{vault_id}"
        elif route_case == "update_vault_visibility":
            # The fixture vault needs an organization: the visibility-'org'-
            # requires-org coherence is enforced on update as of #1003, and
            # this fixture's premise — a SUCCESSFUL visibility mutation as an
            # audit-event source — is legitimately changed by that fix. The
            # org-less rejection is pinned separately by the frozen
            # test_1003_vault_org_adoption.py checks.
            cur = env.conn.execute(
                "INSERT INTO organizations (name) VALUES (?)", ("D04 Visibility Org",)
            )
            vis_org_id = cur.lastrowid
            env.conn.execute(
                "INSERT INTO org_members (org_id, user_id, role)"
                " VALUES (?, ?, 'member')",
                (vis_org_id, superadmin_id),
            )
            env.conn.commit()
            vault_id = _insert_vault(
                env, "D04 Visibility Vault", org_id=vis_org_id
            )
            method, path = "PUT", f"/vaults/{vault_id}"
            body = {"visibility": "org"}
        else:  # update_vault_groups
            cur = env.conn.execute(
                "INSERT INTO organizations (name) VALUES (?)", ("D04 Org",)
            )
            org_id = cur.lastrowid
            cur = env.conn.execute(
                "INSERT INTO groups (org_id, name) VALUES (?, ?)",
                (org_id, "D04 Group"),
            )
            group_id = cur.lastrowid
            vault_id = _insert_vault(env, "D04 Grouped Vault", org_id=org_id)
            method, path = "PUT", f"/vaults/{vault_id}/groups"
            body = {"group_ids": [group_id]}

        before = _audit_count(env)
        response = env.client.request(
            method, path, json=body, headers=headers
        )
        assert response.status_code in (200, 201), (
            f"harness: mutation {route_case} must succeed for the audit check, "
            f"got {response.status_code}: {response.text}"
        )
        after = _audit_count(env)
        assert after - before == 1


# ---------------------------------------------------------------------------
# AC2 — sessions from before a deactivation must not survive reactivation.
# ---------------------------------------------------------------------------


def test_refresh_cookie_from_before_deactivation_rejected_after_reactivation():
    """A refresh cookie minted before deactivation is rejected after the
    account is reactivated (deactivation must have revoked the session)."""
    with _mutation_env(mount_auth=True) as env:
        superadmin_id = create_user(
            env.conn, "superadmin", "pass123", "superadmin", "Super Admin"
        )
        member_id = create_user(env.conn, "member", "pass123", "member", "M")
        admin_headers = _bearer(env, superadmin_id, "superadmin", "superadmin")

        login = env.client.post(
            "/auth/login", json={"username": "member", "password": "pass123"}
        )
        assert login.status_code == 200, (
            f"harness: login must succeed, got {login.status_code}: {login.text}"
        )
        refresh_cookie = login.cookies["refresh_token"]

        off = env.client.patch(
            f"/users/{member_id}/active",
            json={"is_active": False},
            headers=admin_headers,
        )
        assert off.status_code == 200, (
            f"harness: deactivation must succeed, got {off.status_code}"
        )
        on = env.client.patch(
            f"/users/{member_id}/active",
            json={"is_active": True},
            headers=admin_headers,
        )
        assert on.status_code == 200, (
            f"harness: reactivation must succeed, got {on.status_code}"
        )

        response = env.client.post(
            "/auth/refresh", cookies={"refresh_token": refresh_cookie}
        )
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# AC3-AC5 — superadmin-target checks on the three admin mutation routes.
# ---------------------------------------------------------------------------


def test_update_user_without_role_invokes_superadmin_target_check():
    """Admin editing a superadmin's profile (no role change) gets 403 and the
    row is untouched."""
    with _mutation_env() as env:
        superadmin_id = create_user(
            env.conn, "superadmin", "pass123", "superadmin", "Original Name"
        )
        admin_id = create_user(env.conn, "admin", "pass123", "admin", "A")
        headers = _bearer(env, admin_id, "admin", "admin")

        response = env.client.patch(
            f"/users/{superadmin_id}",
            json={"full_name": "Admin-Edited Name"},
            headers=headers,
        )
        assert response.status_code == 403
        row = env.conn.execute(
            "SELECT full_name FROM users WHERE id = ?", (superadmin_id,)
        ).fetchone()
        assert row[0] == "Original Name"


def test_admin_reset_password_invokes_superadmin_target_check():
    """Admin resetting a superadmin's password gets 403."""
    with _mutation_env() as env:
        superadmin_id = create_user(
            env.conn, "superadmin", "pass123", "superadmin", "S"
        )
        admin_id = create_user(env.conn, "admin", "pass123", "admin", "A")
        headers = _bearer(env, admin_id, "admin", "admin")

        response = env.client.patch(
            f"/users/{superadmin_id}/password",
            json={"new_password": "NewSecurePass123!"},
            headers=headers,
        )
        assert response.status_code == 403


def test_update_user_active_invokes_superadmin_target_check():
    """Admin deactivating a superadmin (not the last one) gets 403."""
    with _mutation_env() as env:
        create_user(env.conn, "superadmin", "pass123", "superadmin", "S1")
        superadmin2_id = create_user(
            env.conn, "superadmin2", "pass123", "superadmin", "S2"
        )
        admin_id = create_user(env.conn, "admin", "pass123", "admin", "A")
        headers = _bearer(env, admin_id, "admin", "admin")

        response = env.client.patch(
            f"/users/{superadmin2_id}/active",
            json={"is_active": False},
            headers=headers,
        )
        assert response.status_code == 403


# ---------------------------------------------------------------------------
# AC6 — PATCH /users/{id} role change respects the last-superadmin guard.
# ---------------------------------------------------------------------------


def test_update_user_role_change_invokes_last_superadmin_guard():
    """A superadmin principal with no users row must not be able to demote
    the DB's only active superadmin through PATCH /users/{id}."""
    with _mutation_env() as env:
        from app.api import deps

        env.app.dependency_overrides[deps.get_current_active_user] = lambda: {
            "id": 0,
            "role": "superadmin",
            "username": "bootstrap",
        }

        sole_superadmin_id = create_user(
            env.conn, "solo", "pass123", "superadmin", "Sole Superadmin"
        )

        # The response status is not part of the frozen contract; only the
        # surviving-superadmin invariant below is asserted.
        env.client.patch(f"/users/{sole_superadmin_id}", json={"role": "admin"})

        count = env.conn.execute(
            "SELECT COUNT(*) FROM users WHERE role = 'superadmin' AND is_active = 1"
        ).fetchone()[0]
        assert count == 1


# ---------------------------------------------------------------------------
# AC7 — password-reset audit metadata must report revocation truthfully.
# ---------------------------------------------------------------------------


def test_password_reset_audit_reports_actual_session_revocation():
    """user.password_reset metadata must say sessions_revoked=False when the
    user_sessions table is absent (nothing was actually revoked)."""
    with _mutation_env() as env:
        superadmin_id = create_user(
            env.conn, "superadmin", "pass123", "superadmin", "S"
        )
        member_id = create_user(env.conn, "member", "pass123", "member", "M")
        headers = _bearer(env, superadmin_id, "superadmin", "superadmin")

        # The harness created user_sessions; remove it so the reset path's
        # DELETE FROM user_sessions cannot revoke anything.
        env.conn.execute("DROP TABLE user_sessions")
        env.conn.commit()

        response = env.client.patch(
            f"/users/{member_id}/password",
            json={"new_password": "NewSecurePass123!"},
            headers=headers,
        )
        assert response.status_code == 200, (
            f"harness: password reset must succeed, got {response.status_code}"
        )

        row = env.conn.execute(
            "SELECT metadata_json FROM security_audit_log "
            "WHERE event_type = 'user.password_reset' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row is not None, "harness: password_reset audit row must exist"
        metadata = json.loads(row[0])
        assert metadata["sessions_revoked"] == False  # noqa: E712


# ---------------------------------------------------------------------------
# AC8 — deleting a former org creator must not trip the created_by FK.
# ---------------------------------------------------------------------------


def test_delete_user_who_created_org_but_no_longer_owns_it():
    """Superadmin deleting a user who created an org (ownership since
    transferred to someone else) succeeds with 200 and removes the row."""
    with _mutation_env() as env:
        superadmin_id = create_user(
            env.conn, "superadmin", "pass123", "superadmin", "S"
        )
        creator_id = create_user(env.conn, "creator", "pass123", "member", "C")
        new_owner_id = create_user(env.conn, "owner", "pass123", "member", "W")
        headers = _bearer(env, superadmin_id, "superadmin", "superadmin")

        cur = env.conn.execute(
            "INSERT INTO organizations (name, created_by) VALUES (?, ?)",
            ("D04 FK Org", creator_id),
        )
        org_id = cur.lastrowid
        env.conn.execute(
            "INSERT INTO org_members (org_id, user_id, role) VALUES (?, ?, ?)",
            (org_id, new_owner_id, "owner"),
        )
        env.conn.commit()

        response = env.client.delete(
            f"/users/{creator_id}", headers=headers
        )
        assert response.status_code == 200
        row = env.conn.execute(
            "SELECT id FROM users WHERE id = ?", (creator_id,)
        ).fetchone()
        assert row is None


# ---------------------------------------------------------------------------
# AC9 — sessions list must not return a session expired earlier today.
# ---------------------------------------------------------------------------


def test_sessions_list_omits_session_expired_earlier_today():
    """GET /auth/sessions omits a seeded session whose expires_at is
    00:00:00 UTC today (already past, but "T"-formatted so the raw SQLite
    string comparison wrongly keeps it)."""
    with _mutation_env(mount_auth=True, mount_users=False) as env:
        member_id = create_user(env.conn, "member", "pass123", "member", "M")

        login = env.client.post(
            "/auth/login", json={"username": "member", "password": "pass123"}
        )
        assert login.status_code == 200, (
            f"harness: login must succeed, got {login.status_code}"
        )

        today = env.conn.execute("SELECT date('now')").fetchone()[0]
        cur = env.conn.execute(
            "INSERT INTO user_sessions "
            "(user_id, refresh_token_hash, expires_at, ip_address, user_agent) "
            "VALUES (?, ?, ?, NULL, '')",
            (
                member_id,
                "d04-seeded-hash-earlier-today",
                f"{today}T00:00:00+00:00",
            ),
        )
        seeded_id = cur.lastrowid
        env.conn.commit()

        response = env.client.get(
            "/auth/sessions",
            headers=_bearer(env, member_id, "member", "member"),
        )
        assert response.status_code == 200, (
            f"harness: sessions list must succeed, got {response.status_code}"
        )
        ids = [s["id"] for s in response.json()["sessions"]]
        assert (seeded_id in ids) == False  # noqa: E712


# ---------------------------------------------------------------------------
# AC10 — login purges the user's expired session rows.
# ---------------------------------------------------------------------------


def test_login_purges_expired_session_rows():
    """Logging in deletes the user's already-expired user_sessions rows while
    keeping unexpired ones."""
    with _mutation_env(mount_auth=True, mount_users=False) as env:
        member_id = create_user(env.conn, "member", "pass123", "member", "M")
        now = datetime.now(timezone.utc)

        expired_cur = env.conn.execute(
            "INSERT INTO user_sessions "
            "(user_id, refresh_token_hash, expires_at, ip_address, user_agent) "
            "VALUES (?, ?, ?, NULL, '')",
            (
                member_id,
                "d04-expired-hash",
                (now - timedelta(days=1)).isoformat(),
            ),
        )
        expired_id = expired_cur.lastrowid
        alive_cur = env.conn.execute(
            "INSERT INTO user_sessions "
            "(user_id, refresh_token_hash, expires_at, ip_address, user_agent) "
            "VALUES (?, ?, ?, NULL, '')",
            (
                member_id,
                "d04-alive-hash",
                (now + timedelta(days=7)).isoformat(),
            ),
        )
        alive_id = alive_cur.lastrowid
        env.conn.commit()

        login = env.client.post(
            "/auth/login", json={"username": "member", "password": "pass123"}
        )
        assert login.status_code == 200, (
            f"harness: login must succeed, got {login.status_code}"
        )

        expired_count = env.conn.execute(
            "SELECT COUNT(*) FROM user_sessions WHERE id = ?", (expired_id,)
        ).fetchone()[0]
        assert expired_count == 0
        alive_count = env.conn.execute(
            "SELECT COUNT(*) FROM user_sessions WHERE id = ?", (alive_id,)
        ).fetchone()[0]
        assert alive_count == 1
