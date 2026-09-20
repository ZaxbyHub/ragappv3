"""Issue #645 (AC13): HTTP-level coverage for the seven converted handlers.

The named handlers now check out their pooled connection via
``await pool.get_connection_async()`` (off the event loop, issue #645).
These tests drive each handler through the real FastAPI routing stack
(TestClient over a real app with the real routers and the real
``SQLiteConnectionPool`` over a temp database) so the converted checkout
path runs end to end — request in, dependency graph, async checkout, SQL,
release, response out.

Handlers covered:

* prompts.get_active_prompt_version   (GET  /api/prompts/active)
* prompts.activate_prompt_version     (POST /api/prompts/{version}/activate)
* prompts.get_prompt_version          (GET  /api/prompts/{version})
* organizations.resend_org_invite     (POST /api/organizations/{org_id}/invites/{invite_id}/resend)
* organizations.revoke_org_invite     (POST /api/organizations/{org_id}/invites/{invite_id}/revoke)
* organizations.list_org_invites      (GET  /api/organizations/{org_id}/invites)
* organizations.accept_org_invite     (POST /api/organizations/invites/accept)

Harness reuses the established patterns verbatim:
``tests/test_prompt_ab_variants.py`` (run_migrations temp DB, patched
settings, static admin token for ``require_scope("admin:config")``, csrf
dependency override) and ``tests/test_org_invites.py`` (JWT bearer tokens
minted against seeded users, direct org/invite row seeding).
"""

import hashlib
import shutil
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes.auth import router as auth_router
from app.api.routes.organizations import router as organizations_router
from app.api.routes.prompts import router as prompts_router
from app.models.database import run_migrations
from app.services.auth_service import (
    compute_client_fingerprint,
    create_access_token,
    hash_password,
)


@pytest.fixture()
def seeded_env(monkeypatch):
    """Temp DB (full schema) + patched settings + seeded users.

    ``settings.data_dir`` drives ``settings.sqlite_path`` (a property), so
    the routes' ``get_pool(str(settings.sqlite_path))`` checkouts land on the
    REAL pool over the temp database — the converted checkout path runs for
    real, no pool mock anywhere.
    """
    temp_dir = tempfile.mkdtemp(prefix="issue645_routes_")
    db_path = str(Path(temp_dir) / "app.db")

    from app.models.database import _pool_cache, _pool_cache_lock

    with _pool_cache_lock:
        for _path, pool in list(_pool_cache.items()):
            pool.close_all()
        _pool_cache.clear()

    run_migrations(db_path)

    monkeypatch.setattr("app.config.settings.data_dir", Path(temp_dir))
    monkeypatch.setattr(
        "app.config.settings.jwt_secret_key",
        "test-secret-key-for-testing-only-min-32-chars!!",
    )
    monkeypatch.setattr("app.config.settings.users_enabled", True)
    # require_scope("admin:config") for the prompts router: a static admin
    # token mapped to the scope server-side (test_prompt_ab_variants pattern).
    monkeypatch.setattr("app.config.settings.admin_secret_token", "test-admin-secret")
    monkeypatch.setattr(
        "app.config.settings.admin_token_scopes",
        {"test-admin-secret": ["admin:config"]},
    )

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    pw = hash_password("testpass")
    users = [
        (2, "admin1", "admin"),  # org owner / prompt admin via static token
        (3, "member1@x.com", "member"),
        (4, "member2@x.com", "member"),
    ]
    for uid, username, role in users:
        conn.execute(
            "INSERT INTO users (id, username, hashed_password, full_name, role, is_active) "
            "VALUES (?, ?, ?, ?, ?, 1)",
            (uid, username, pw, username.title(), role),
        )
    conn.commit()
    conn.close()

    env = {"db_path": db_path, "temp_dir": temp_dir}
    yield env

    with _pool_cache_lock:
        if db_path in _pool_cache:
            _pool_cache[db_path].close_all()
            del _pool_cache[db_path]
    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture()
def prompts_client(seeded_env):
    """App exposing the prompts router (admin:config-scoped endpoints)."""
    from app.security import csrf_protect

    app = FastAPI()
    app.include_router(auth_router, prefix="/api")
    app.include_router(prompts_router, prefix="/api")
    app.dependency_overrides[csrf_protect] = lambda: "test-csrf-token"
    tc = TestClient(app)
    tc.headers["user-agent"] = ""
    return tc


@pytest.fixture()
def orgs_client(seeded_env):
    """App exposing the organizations router (JWT-member endpoints)."""
    from app.security import csrf_protect

    app = FastAPI()
    app.include_router(auth_router, prefix="/api")
    app.include_router(organizations_router, prefix="/api")
    app.dependency_overrides[csrf_protect] = lambda: "test-csrf-token"
    tc = TestClient(app)
    tc.headers["user-agent"] = ""
    return tc


_ADMIN_HEADERS = {"Authorization": "Bearer test-admin-secret"}


def _jwt_headers(user_id: int, username: str, role: str) -> dict:
    return {
        "Authorization": (
            "Bearer "
            + create_access_token(
                user_id, username, role, client_fingerprint=compute_client_fingerprint("")
            )
        )
    }


def _owner_headers() -> dict:
    return _jwt_headers(2, "admin1", "admin")


def _member_headers() -> dict:
    return _jwt_headers(3, "member1@x.com", "member")


def _member2_headers() -> dict:
    return _jwt_headers(4, "member2@x.com", "member")


def _seed_prompt_versions(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO prompt_versions (version, content, is_active, created_by) "
            "VALUES (?, ?, ?, ?)",
            ("v1-control", "control content", 1, "setup"),
        )
        conn.execute(
            "INSERT INTO prompt_versions (version, content, is_active, created_by) "
            "VALUES (?, ?, ?, ?)",
            ("v2-challenger", "challenger content", 0, "setup"),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_org_with_owner(db_path: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        cur = conn.execute(
            "INSERT INTO organizations (name, description, slug, created_by) "
            "VALUES (?, ?, ?, ?)",
            ("Acme", "desc", "acme", 2),
        )
        org_id = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO org_members (org_id, user_id, role) VALUES (?, ?, 'owner')",
            (org_id, 2),
        )
        conn.commit()
        return org_id
    finally:
        conn.close()


def _seed_invite(db_path: str, org_id: int, email: str, role: str = "member"):
    """Insert an invite row directly; return (invite_id, raw_token)."""
    import secrets

    raw_token = f"inv_{secrets.token_urlsafe(32)}"
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=7)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        cur = conn.execute(
            """INSERT INTO org_invites
               (org_id, email, token_hash, role, expires_at, created_at, created_by_user_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (org_id, email.lower(), token_hash, role, expires_at.isoformat(),
             now.isoformat(), 2),
        )
        conn.commit()
        return int(cur.lastrowid), raw_token
    finally:
        conn.close()


def _invite_row(db_path: str, invite_id: int) -> sqlite3.Row:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT * FROM org_invites WHERE id = ?", (invite_id,)
        ).fetchone()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Prompts router: get_active_prompt_version / activate_prompt_version /
# get_prompt_version — every request runs the converted get_connection_async
# checkout against the real pool.
# ---------------------------------------------------------------------------


class TestConvertedPromptsRoutes:
    def test_get_active_prompt_version_returns_seeded_active(self, prompts_client, seeded_env):
        _seed_prompt_versions(seeded_env["db_path"])
        resp = prompts_client.get("/api/prompts/active", headers=_ADMIN_HEADERS)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["version"] == "v1-control"
        assert body["content"] == "control content"
        assert body["is_active"] is True
        assert body["created_by"] == "setup"

    def test_activate_prompt_version_switches_active_exactly_once(
        self, prompts_client, seeded_env
    ):
        _seed_prompt_versions(seeded_env["db_path"])

        resp = prompts_client.post(
            "/api/prompts/v2-challenger/activate", headers=_ADMIN_HEADERS
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["version"] == "v2-challenger"
        assert body["is_active"] is True

        # Exactly one active row afterwards (DB state, not just the response).
        conn = sqlite3.connect(seeded_env["db_path"])
        try:
            active = conn.execute(
                "SELECT version FROM prompt_versions WHERE is_active = 1"
            ).fetchall()
        finally:
            conn.close()
        assert active == [("v2-challenger",)]

        # And the /active endpoint (its own converted checkout) agrees.
        active_resp = prompts_client.get("/api/prompts/active", headers=_ADMIN_HEADERS)
        assert active_resp.status_code == 200, active_resp.text
        assert active_resp.json()["version"] == "v2-challenger"

    def test_get_prompt_version_returns_full_content(self, prompts_client, seeded_env):
        _seed_prompt_versions(seeded_env["db_path"])
        resp = prompts_client.get("/api/prompts/v1-control", headers=_ADMIN_HEADERS)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["version"] == "v1-control"
        assert body["content"] == "control content"
        assert body["is_active"] is True

    def test_get_prompt_version_unknown_returns_404(self, prompts_client, seeded_env):
        _seed_prompt_versions(seeded_env["db_path"])
        resp = prompts_client.get("/api/prompts/nope", headers=_ADMIN_HEADERS)
        assert resp.status_code == 404, resp.text
        assert "nope" in resp.json()["detail"]

    def test_activate_prompt_version_unknown_returns_404(self, prompts_client, seeded_env):
        _seed_prompt_versions(seeded_env["db_path"])
        resp = prompts_client.post(
            "/api/prompts/nope/activate", headers=_ADMIN_HEADERS
        )
        assert resp.status_code == 404, resp.text

    def test_prompts_without_admin_token_returns_401(self, prompts_client, seeded_env):
        _seed_prompt_versions(seeded_env["db_path"])
        resp = prompts_client.get("/api/prompts/active")
        assert resp.status_code == 401, resp.text
        assert resp.json()["detail"] == "Authorization header missing"

    def test_prompts_with_wrong_admin_token_returns_403(self, prompts_client, seeded_env):
        _seed_prompt_versions(seeded_env["db_path"])
        resp = prompts_client.get(
            "/api/prompts/active",
            headers={"Authorization": "Bearer not-the-admin-secret"},
        )
        assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# Organizations router: resend_org_invite / revoke_org_invite /
# list_org_invites / accept_org_invite — same converted checkout path.
# ---------------------------------------------------------------------------


class TestConvertedOrgInviteRoutes:
    def test_list_org_invites_returns_seeded_invite(self, orgs_client, seeded_env):
        org_id = _seed_org_with_owner(seeded_env["db_path"])
        invite_id, _token = _seed_invite(
            seeded_env["db_path"], org_id, "member2@x.com"
        )

        resp = orgs_client.get(
            f"/api/organizations/{org_id}/invites", headers=_owner_headers()
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert len(body["invites"]) == 1
        invite = body["invites"][0]
        assert invite["id"] == invite_id
        assert invite["email"] == "member2@x.com"
        assert invite["role"] == "member"
        assert invite["status"] == "pending"
        assert invite["expires_at"]
        assert invite["created_at"]

    def test_resend_org_invite_rotates_token_in_db(self, orgs_client, seeded_env):
        org_id = _seed_org_with_owner(seeded_env["db_path"])
        invite_id, old_token = _seed_invite(
            seeded_env["db_path"], org_id, "member2@x.com"
        )

        resp = orgs_client.post(
            f"/api/organizations/{org_id}/invites/{invite_id}/resend",
            headers=_owner_headers(),
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["invite_id"] == invite_id
        assert body["email"] == "member2@x.com"
        assert body["token"].startswith("inv_")
        assert body["token"] != old_token

        # The old token is genuinely invalidated (DB state): the stored hash
        # now matches the NEW token only.
        row = _invite_row(seeded_env["db_path"], invite_id)
        assert row["token_hash"] == hashlib.sha256(body["token"].encode()).hexdigest()
        assert row["token_hash"] != hashlib.sha256(old_token.encode()).hexdigest()

        # Redemption with the OLD token must fail after the rotation.
        stale = orgs_client.post(
            "/api/organizations/invites/accept",
            json={"token": old_token},
            headers=_member2_headers(),
        )
        assert stale.status_code == 400, stale.text
        assert stale.json()["detail"] == "Invalid or expired invite"

    def test_revoke_org_invite_marks_row_revoked(self, orgs_client, seeded_env):
        org_id = _seed_org_with_owner(seeded_env["db_path"])
        invite_id, _token = _seed_invite(
            seeded_env["db_path"], org_id, "member2@x.com"
        )

        resp = orgs_client.post(
            f"/api/organizations/{org_id}/invites/{invite_id}/revoke",
            headers=_owner_headers(),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body == {"message": "Invite revoked", "invite_id": invite_id}

        # DB state: revoked_at set.
        row = _invite_row(seeded_env["db_path"], invite_id)
        assert row["revoked_at"]

        # The list endpoint (its own converted checkout) surfaces 'revoked'...
        listed = orgs_client.get(
            f"/api/organizations/{org_id}/invites", headers=_owner_headers()
        )
        assert listed.status_code == 200, listed.text
        assert listed.json()["invites"][0]["status"] == "revoked"

        # ...and a revoked invite can no longer be resent.
        resend = orgs_client.post(
            f"/api/organizations/{org_id}/invites/{invite_id}/resend",
            headers=_owner_headers(),
        )
        assert resend.status_code == 400, resend.text

    def test_accept_org_invite_creates_membership(self, orgs_client, seeded_env):
        org_id = _seed_org_with_owner(seeded_env["db_path"])
        invite_id, raw_token = _seed_invite(
            seeded_env["db_path"], org_id, "member2@x.com"
        )

        resp = orgs_client.post(
            "/api/organizations/invites/accept",
            json={"token": raw_token},
            headers=_member2_headers(),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["user_id"] == 4
        assert body["username"] == "member2@x.com"
        assert body["role"] == "member"
        assert body["joined_at"]

        # DB state: membership row + invite marked accepted, atomically.
        row = _invite_row(seeded_env["db_path"], invite_id)
        assert row["accepted_at"]
        assert row["accepted_by_user_id"] == 4
        conn = sqlite3.connect(seeded_env["db_path"])
        try:
            member = conn.execute(
                "SELECT role FROM org_members WHERE org_id = ? AND user_id = ?",
                (org_id, 4),
            ).fetchone()
        finally:
            conn.close()
        assert member == ("member",)

    def test_resend_unknown_invite_returns_404(self, orgs_client, seeded_env):
        org_id = _seed_org_with_owner(seeded_env["db_path"])
        resp = orgs_client.post(
            f"/api/organizations/{org_id}/invites/999999/resend",
            headers=_owner_headers(),
        )
        assert resp.status_code == 404, resp.text

    def test_list_org_invites_as_plain_member_returns_403(self, orgs_client, seeded_env):
        org_id = _seed_org_with_owner(seeded_env["db_path"])
        resp = orgs_client.get(
            f"/api/organizations/{org_id}/invites", headers=_member_headers()
        )
        assert resp.status_code == 403, resp.text

    def test_org_invites_without_auth_returns_401(self, orgs_client, seeded_env):
        org_id = _seed_org_with_owner(seeded_env["db_path"])
        resp = orgs_client.get(f"/api/organizations/{org_id}/invites")
        assert resp.status_code in (401, 403), resp.text
