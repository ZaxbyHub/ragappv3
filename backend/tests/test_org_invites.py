"""Tests for organization invite flow (FR-012)."""

import hashlib
import sqlite3
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from backend.tests.schema_constants import TEST_SCHEMA, build_test_schema
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes.auth import router as auth_router
from app.api.routes.organizations import router as organizations_router
from app.services.auth_service import (
    compute_client_fingerprint,
    create_access_token,
    hash_password,
)


@pytest.fixture(autouse=True)
def setup_db(monkeypatch):
    """Set up test database with schema and seed data."""
    temp_dir = tempfile.mkdtemp()
    db_path = str(Path(temp_dir) / "app.db")

    # Clear pool cache BEFORE setting up new database
    from app.models.database import _pool_cache, _pool_cache_lock

    with _pool_cache_lock:
        for path, pool in list(_pool_cache.items()):
            pool.close_all()
        _pool_cache.clear()

    # Initialize schema manually with valid SQL
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    build_test_schema(conn)
    conn.commit()
    conn.close()

    # Patch settings
    monkeypatch.setattr("app.config.settings.data_dir", Path(temp_dir))
    monkeypatch.setattr(
        "app.config.settings.jwt_secret_key",
        "test-secret-key-for-testing-only-min-32-chars!!",
    )
    monkeypatch.setattr("app.config.settings.users_enabled", True)

    # Seed test users
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    pw = hash_password("testpass")
    conn.execute(
        "INSERT INTO users (id, username, hashed_password, full_name, role, is_active) "
        "VALUES (?, ?, ?, ?, ?, 1)",
        (1, "superadmin", pw, "Super Admin", "superadmin"),
    )
    conn.execute(
        "INSERT INTO users (id, username, hashed_password, full_name, role, is_active) "
        "VALUES (?, ?, ?, ?, ?, 1)",
        (2, "admin1", pw, "Admin One", "admin"),
    )
    conn.execute(
        "INSERT INTO users (id, username, hashed_password, full_name, role, is_active) "
        "VALUES (?, ?, ?, ?, ?, 1)",
        (3, "member1@x.com", pw, "Member One", "member"),
    )
    conn.execute(
        "INSERT INTO users (id, username, hashed_password, full_name, role, is_active) "
        "VALUES (?, ?, ?, ?, ?, 1)",
        (4, "member2@x.com", pw, "Member Two", "member"),
    )
    conn.commit()
    conn.close()

    yield db_path

    # Cleanup
    with _pool_cache_lock:
        if db_path in _pool_cache:
            _pool_cache[db_path].close_all()
            del _pool_cache[db_path]

    import shutil

    shutil.rmtree(temp_dir, ignore_errors=True)


def _get_db_conn():
    """Get a direct connection to the test database for setup."""
    from app.config import settings

    return sqlite3.connect(str(settings.sqlite_path))


def _create_org(name: str, owner_user_id: int, description: str = "Desc"):
    """Create an organization and add owner as owner."""
    conn = _get_db_conn()
    conn.execute("PRAGMA foreign_keys = ON")
    cursor = conn.execute(
        "INSERT INTO organizations (name, description, slug, created_by) VALUES (?, ?, ?, ?)",
        (name, description, name.lower().replace(" ", "-"), owner_user_id),
    )
    org_id = cursor.lastrowid
    conn.execute(
        "INSERT INTO org_members (org_id, user_id, role) VALUES (?, ?, 'owner')",
        (org_id, owner_user_id),
    )
    conn.commit()
    conn.close()
    return org_id


def _add_org_member(org_id: int, user_id: int, role: str):
    """Add a member to an organization."""
    conn = _get_db_conn()
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "INSERT OR IGNORE INTO org_members (org_id, user_id, role) VALUES (?, ?, ?)",
        (org_id, user_id, role),
    )
    conn.commit()
    conn.close()


def _make_invite(conn: sqlite3.Connection, org_id: int, email: str, role: str, days_valid: int = 7) -> tuple:
    """Create an invite directly in DB and return (invite_id, raw_token)."""
    import secrets

    raw_token = f"inv_{secrets.token_urlsafe(32)}"
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=days_valid)
    cursor = conn.execute(
        """INSERT INTO org_invites
           (org_id, email, token_hash, role, expires_at, created_at, created_by_user_id)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (org_id, email.lower(), token_hash, role, expires_at.isoformat(), now.isoformat(), 2),
    )
    conn.commit()
    return cursor.lastrowid, raw_token


def superadmin_token():
    return create_access_token(1, "superadmin", "superadmin",
                        client_fingerprint=compute_client_fingerprint(""))


def admin_token():
    return create_access_token(2, "admin1", "admin",
                        client_fingerprint=compute_client_fingerprint(""))


def member_token():
    return create_access_token(3, "member1@x.com", "member",
                        client_fingerprint=compute_client_fingerprint(""))


def member2_token():
    return create_access_token(4, "member2@x.com", "member",
                        client_fingerprint=compute_client_fingerprint(""))


def auth_headers(token_fn):
    return {"Authorization": f"Bearer {token_fn()}"}


@pytest.fixture
def client():
    """Create test client with routers."""
    app = FastAPI()
    app.include_router(auth_router, prefix="/api")
    app.include_router(organizations_router, prefix="/api")
    tc = TestClient(app)
    # Override default User-Agent so fingerprint validation matches token
    tc.headers["user-agent"] = ""
    return tc


# ---------------------------------------------------------------------------
# Tests: Create Invite
# ---------------------------------------------------------------------------


class TestCreateInvite:
    """Tests for POST /api/organizations/{org_id}/invites."""

    def test_admin_creates_invite(self, client):
        """Admin creates an invite successfully (201)."""
        org_id = _create_org("Test Org", 2)  # admin1 owns it

        response = client.post(
            f"/api/organizations/{org_id}/invites",
            json={"email": "newuser@x.com", "role": "member"},
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 201
        data = response.json()
        assert data["email"] == "newuser@x.com"
        assert data["role"] == "member"
        assert data["invite_id"] is not None
        assert data["token"].startswith("inv_")
        assert "expires_at" in data

    def test_admin_creates_admin_invite(self, client):
        """Admin creates an admin invite (201)."""
        org_id = _create_org("Test Org", 2)

        response = client.post(
            f"/api/organizations/{org_id}/invites",
            json={"email": "admininvite@x.com", "role": "admin"},
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 201
        assert response.json()["role"] == "admin"

    def test_owner_creates_invite(self, client):
        """Org owner creates an invite (201)."""
        org_id = _create_org("Owner Org", 1)  # superadmin as owner

        response = client.post(
            f"/api/organizations/{org_id}/invites",
            json={"email": "memberinvite@x.com", "role": "member"},
            headers=auth_headers(superadmin_token),
        )
        assert response.status_code == 201

    def test_member_cannot_create_invite(self, client):
        """Org member cannot create invite (403)."""
        org_id = _create_org("Test Org", 2)
        _add_org_member(org_id, 3, "member")

        response = client.post(
            f"/api/organizations/{org_id}/invites",
            json={"email": "bad@x.com", "role": "member"},
            headers=auth_headers(member_token),
        )
        assert response.status_code == 403

    def test_invite_to_nonexistent_org(self, client):
        """Invite to nonexistent org (404)."""
        response = client.post(
            "/api/organizations/99999/invites",
            json={"email": "bad@x.com", "role": "member"},
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 404

    def test_invalid_identifier_format(self, client):
        """Identifier shorter than 3 chars is rejected (422)."""
        org_id = _create_org("Test Org", 2)

        response = client.post(
            f"/api/organizations/{org_id}/invites",
            json={"email": "ab", "role": "member"},
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 422

    def test_owner_role_not_invitable(self, client):
        """Owner role cannot be assigned via invite (422)."""
        org_id = _create_org("Test Org", 2)

        response = client.post(
            f"/api/organizations/{org_id}/invites",
            json={"email": "bad@x.com", "role": "owner"},
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 422

    def test_custom_expires_in_days(self, client):
        """Custom expires_in_days is respected (201)."""
        org_id = _create_org("Test Org", 2)

        response = client.post(
            f"/api/organizations/{org_id}/invites",
            json={"email": "expire@x.com", "role": "member", "expires_in_days": 14},
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 201
        data = response.json()
        expires_at = datetime.fromisoformat(data["expires_at"])
        now = datetime.now(timezone.utc)
        # Should be ~14 days from now
        assert (expires_at - now).days >= 13


# ---------------------------------------------------------------------------
# Tests: List Invites
# ---------------------------------------------------------------------------


class TestListInvites:
    """Tests for GET /api/organizations/{org_id}/invites."""

    def test_list_invites_returns_metadata(self, client):
        """List invites returns correct metadata (no token)."""
        org_id = _create_org("Test Org", 2)
        conn = _get_db_conn()
        _make_invite(conn, org_id, "user1@x.com", "member")
        _make_invite(conn, org_id, "user2@x.com", "admin")
        conn.close()

        response = client.get(
            f"/api/organizations/{org_id}/invites",
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 200
        data = response.json()["invites"]
        assert len(data) == 2
        for invite in data:
            assert "token" not in invite
            assert "token_hash" not in invite
            assert "email" in invite
            assert "role" in invite
            assert "status" in invite
            assert "expires_at" in invite

    def test_list_invites_shows_pending_status(self, client):
        """Pending invite shows 'pending' status."""
        org_id = _create_org("Test Org", 2)
        conn = _get_db_conn()
        _make_invite(conn, org_id, "user1@x.com", "member", days_valid=30)
        conn.close()

        response = client.get(
            f"/api/organizations/{org_id}/invites",
            headers=auth_headers(admin_token),
        )
        statuses = [i["status"] for i in response.json()["invites"]]
        assert "pending" in statuses

    def test_non_admin_cannot_list(self, client):
        """Non-admin org member cannot list invites (403)."""
        org_id = _create_org("Test Org", 2)
        _add_org_member(org_id, 3, "member")

        response = client.get(
            f"/api/organizations/{org_id}/invites",
            headers=auth_headers(member_token),
        )
        assert response.status_code == 403


# ---------------------------------------------------------------------------
# Tests: Resend Invite
# ---------------------------------------------------------------------------


class TestResendInvite:
    """Tests for POST /api/organizations/{org_id}/invites/{invite_id}/resend."""

    def test_resend_returns_new_token(self, client):
        """Resend returns a new raw token once (201)."""
        org_id = _create_org("Test Org", 2)
        conn = _get_db_conn()
        invite_id, old_raw = _make_invite(conn, org_id, "user1@x.com", "member")
        conn.close()

        response = client.post(
            f"/api/organizations/{org_id}/invites/{invite_id}/resend",
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 201
        data = response.json()
        assert data["token"] != old_raw
        assert data["token"].startswith("inv_")

    def test_resend_invalidates_old_token(self, client):
        """Old token cannot be used after resend."""
        org_id = _create_org("Test Org", 2)
        conn = _get_db_conn()
        invite_id, old_raw = _make_invite(conn, org_id, "user1@x.com", "member")
        conn.close()

        # Resend
        resend_response = client.post(
            f"/api/organizations/{org_id}/invites/{invite_id}/resend",
            headers=auth_headers(admin_token),
        )
        new_token = resend_response.json()["token"]

        # Old token hash should not match the DB anymore
        old_hash = hashlib.sha256(old_raw.encode()).hexdigest()
        conn2 = _get_db_conn()
        cursor = conn2.execute(
            "SELECT token_hash FROM org_invites WHERE id = ?",
            (invite_id,),
        )
        current_hash = cursor.fetchone()[0]
        conn2.close()
        assert current_hash != old_hash

    def test_resend_nonexistent_invite(self, client):
        """Resend nonexistent invite (404)."""
        org_id = _create_org("Test Org", 2)

        response = client.post(
            f"/api/organizations/{org_id}/invites/99999/resend",
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 404

    def test_resend_revoked_invite_fails(self, client):
        """Cannot resend a revoked invite (400)."""
        org_id = _create_org("Test Org", 2)
        conn = _get_db_conn()
        invite_id, _ = _make_invite(conn, org_id, "user1@x.com", "member")
        # Revoke it
        conn.execute(
            "UPDATE org_invites SET revoked_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), invite_id),
        )
        conn.commit()
        conn.close()

        response = client.post(
            f"/api/organizations/{org_id}/invites/{invite_id}/resend",
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 400


# ---------------------------------------------------------------------------
# Tests: Revoke Invite
# ---------------------------------------------------------------------------


class TestRevokeInvite:
    """Tests for POST /api/organizations/{org_id}/invites/{invite_id}/revoke."""

    def test_revoke_invite_succeeds(self, client):
        """Admin revokes an invite (200)."""
        org_id = _create_org("Test Org", 2)
        conn = _get_db_conn()
        invite_id, _ = _make_invite(conn, org_id, "user1@x.com", "member")
        conn.close()

        response = client.post(
            f"/api/organizations/{org_id}/invites/{invite_id}/revoke",
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 200
        assert response.json()["message"] == "Invite revoked"

    def test_revoke_sets_revoked_at(self, client):
        """Revoke sets revoked_at timestamp."""
        org_id = _create_org("Test Org", 2)
        conn = _get_db_conn()
        invite_id, _ = _make_invite(conn, org_id, "user1@x.com", "member")
        conn.close()

        client.post(
            f"/api/organizations/{org_id}/invites/{invite_id}/revoke",
            headers=auth_headers(admin_token),
        )

        conn2 = _get_db_conn()
        cursor = conn2.execute(
            "SELECT revoked_at FROM org_invites WHERE id = ?",
            (invite_id,),
        )
        revoked_at = cursor.fetchone()[0]
        conn2.close()
        assert revoked_at is not None

    def test_revoke_already_revoked_fails(self, client):
        """Revoking already-revoked invite (400)."""
        org_id = _create_org("Test Org", 2)
        conn = _get_db_conn()
        invite_id, _ = _make_invite(conn, org_id, "user1@x.com", "member")
        conn.execute(
            "UPDATE org_invites SET revoked_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), invite_id),
        )
        conn.commit()
        conn.close()

        response = client.post(
            f"/api/organizations/{org_id}/invites/{invite_id}/revoke",
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 400

    def test_revoke_nonexistent_invite(self, client):
        """Revoke nonexistent invite (404)."""
        org_id = _create_org("Test Org", 2)

        response = client.post(
            f"/api/organizations/{org_id}/invites/99999/revoke",
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 404

    def test_member_cannot_revoke(self, client):
        """Org member cannot revoke invite (403)."""
        org_id = _create_org("Test Org", 2)
        _add_org_member(org_id, 3, "member")
        conn = _get_db_conn()
        invite_id, _ = _make_invite(conn, org_id, "user1@x.com", "member")
        conn.close()

        response = client.post(
            f"/api/organizations/{org_id}/invites/{invite_id}/revoke",
            headers=auth_headers(member_token),
        )
        assert response.status_code == 403


# ---------------------------------------------------------------------------
# Tests: Accept Invite
# ---------------------------------------------------------------------------


class TestAcceptInvite:
    """Tests for POST /api/organizations/invites/accept."""

    def test_accept_invite_creates_membership(self, client):
        """Accepting invite creates org_members row (200)."""
        org_id = _create_org("Test Org", 2)
        conn = _get_db_conn()
        invite_id, raw_token = _make_invite(conn, org_id, "member1@x.com", "member")
        conn.close()

        response = client.post(
            "/api/organizations/invites/accept",
            json={"token": raw_token},
            headers=auth_headers(member_token),
        )
        assert response.status_code == 200
        data = response.json()
        assert data["role"] == "member"
        assert data["user_id"] == 3

        # Verify org_members row exists
        conn2 = _get_db_conn()
        cursor = conn2.execute(
            "SELECT role FROM org_members WHERE org_id = ? AND user_id = ?",
            (org_id, 3),
        )
        row = cursor.fetchone()
        conn2.close()
        assert row is not None
        assert row[0] == "member"

    def test_accept_invite_marks_accepted(self, client):
        """Accept sets accepted_at and accepted_by_user_id."""
        org_id = _create_org("Test Org", 2)
        conn = _get_db_conn()
        invite_id, raw_token = _make_invite(conn, org_id, "member1@x.com", "member")
        conn.close()

        client.post(
            "/api/organizations/invites/accept",
            json={"token": raw_token},
            headers=auth_headers(member_token),
        )

        conn2 = _get_db_conn()
        cursor = conn2.execute(
            "SELECT accepted_at, accepted_by_user_id FROM org_invites WHERE id = ?",
            (invite_id,),
        )
        row = cursor.fetchone()
        conn2.close()
        assert row[0] is not None  # accepted_at
        assert row[1] == 3  # accepted_by_user_id

    def test_accept_invite_email_mismatch(self, client):
        """Invite email must match authenticated user email (403)."""
        org_id = _create_org("Test Org", 2)
        conn = _get_db_conn()
        _, raw_token = _make_invite(conn, org_id, "other@x.com", "member")
        conn.close()

        # member_token is member1@x.com, but invite is for other@x.com
        response = client.post(
            "/api/organizations/invites/accept",
            json={"token": raw_token},
            headers=auth_headers(member_token),
        )
        assert response.status_code == 403

    def test_accept_expired_invite(self, client):
        """Expired invite is rejected (400, uniform message)."""
        org_id = _create_org("Test Org", 2)
        conn = _get_db_conn()
        _, raw_token = _make_invite(conn, org_id, "member1@x.com", "member", days_valid=0)
        conn.close()

        response = client.post(
            "/api/organizations/invites/accept",
            json={"token": raw_token},
            headers=auth_headers(member_token),
        )
        assert response.status_code == 400
        assert response.json()["detail"] == "Invalid or expired invite"

    def test_accept_revoked_invite(self, client):
        """Revoked invite is rejected (400, uniform message)."""
        org_id = _create_org("Test Org", 2)
        conn = _get_db_conn()
        invite_id, raw_token = _make_invite(conn, org_id, "member1@x.com", "member")
        conn.execute(
            "UPDATE org_invites SET revoked_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), invite_id),
        )
        conn.commit()
        conn.close()

        response = client.post(
            "/api/organizations/invites/accept",
            json={"token": raw_token},
            headers=auth_headers(member_token),
        )
        assert response.status_code == 400
        assert response.json()["detail"] == "Invalid or expired invite"

    def test_accept_invalid_token(self, client):
        """Invalid token is rejected (400)."""
        response = client.post(
            "/api/organizations/invites/accept",
            json={"token": "inv_notarealtoken"},
            headers=auth_headers(member_token),
        )
        assert response.status_code == 400

    def test_accept_already_accepted_invite(self, client):
        """Already-accepted invite cannot be accepted again (400, uniform message)."""
        org_id = _create_org("Test Org", 2)
        conn = _get_db_conn()
        invite_id, raw_token = _make_invite(conn, org_id, "member1@x.com", "member")
        conn.execute(
            "UPDATE org_invites SET accepted_at = ?, accepted_by_user_id = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), 3, invite_id),
        )
        conn.commit()
        conn.close()

        response = client.post(
            "/api/organizations/invites/accept",
            json={"token": raw_token},
            headers=auth_headers(member_token),
        )
        assert response.status_code == 400
        assert response.json()["detail"] == "Invalid or expired invite"

    def test_accept_creates_correct_role(self, client):
        """Accept respects the invited role (admin)."""
        org_id = _create_org("Test Org", 2)
        conn = _get_db_conn()
        _, raw_token = _make_invite(conn, org_id, "member1@x.com", "admin")
        conn.close()

        response = client.post(
            "/api/organizations/invites/accept",
            json={"token": raw_token},
            headers=auth_headers(member_token),
        )
        assert response.status_code == 200
        assert response.json()["role"] == "admin"

    def test_invite_and_accept_non_email_username(self, client):
        """A plain username (non-email identifier) can be invited and accepted.

        Verifies FR-012: invites work for identifiers beyond just email-shaped
        usernames. user "admin1" (plain username) is invited and accepts.
        """
        org_id = _create_org("Plain User Org", 1)  # superadmin owns it

        # Create an invite for a plain non-email username "alice"
        # First seed alice as a user in the DB (registration not tested here)
        conn = _get_db_conn()
        pw = hash_password("alicepass")
        conn.execute(
            "INSERT INTO users (id, username, hashed_password, full_name, role, is_active) "
            "VALUES (?, ?, ?, ?, ?, 1)",
            (99, "alice", pw, "Alice User", "member"),
        )
        conn.commit()

        # Create invite for "alice" (plain non-email identifier)
        invite_id, raw_token = _make_invite(conn, org_id, "alice", "member")
        conn.close()

        # Verify invite was created (201)
        response = client.post(
            f"/api/organizations/{org_id}/invites",
            json={"email": "alice", "role": "member"},
            headers=auth_headers(superadmin_token),
        )
        assert response.status_code == 201
        assert response.json()["email"] == "alice"

        # Accept with alice's token (need to create access token for alice)
        from app.services.auth_service import (
            compute_client_fingerprint,
            create_access_token,
        )
        alice_token = create_access_token(99, "alice", "member",
                                  client_fingerprint=compute_client_fingerprint(""))

        response = client.post(
            "/api/organizations/invites/accept",
            json={"token": raw_token},
            headers={"Authorization": f"Bearer {alice_token}"},
        )
        assert response.status_code == 200
        assert response.json()["role"] == "member"

    def test_accept_idempotent_member_already_member(self, client):
        """Accept when already a member returns 409."""
        org_id = _create_org("Test Org", 2)
        _add_org_member(org_id, 3, "member")  # Already a member
        conn = _get_db_conn()
        _, raw_token = _make_invite(conn, org_id, "member1@x.com", "member")
        conn.close()

        response = client.post(
            "/api/organizations/invites/accept",
            json={"token": raw_token},
            headers=auth_headers(member_token),
        )
        assert response.status_code == 409

    def test_concurrent_accept_same_invite(self, client):
        """Two concurrent accepts of the same valid invite: exactly one succeeds (200/201).

        Exercises the BEGIN IMMEDIATE + UNIQUE-violation path: the first request
        acquires the exclusive lock and inserts the membership; the second request
        hits the UNIQUE constraint, rolls back, and is rejected (409 or 400).
        """
        org_id = _create_org("Concurrent Org", 2)
        conn = _get_db_conn()
        _, raw_token = _make_invite(conn, org_id, "member1@x.com", "member")
        conn.close()

        results: dict[int, tuple[int, dict]] = {}
        lock = threading.Lock()

        def _accept(idx: int):
            response = client.post(
                "/api/organizations/invites/accept",
                json={"token": raw_token},
                headers=auth_headers(member_token),
            )
            with lock:
                results[idx] = (response.status_code, response.json())

        # Fire two near-simultaneous accepts from different threads.
        with ThreadPoolExecutor(max_workers=2) as executor:
            f1 = executor.submit(_accept, 0)
            f2 = executor.submit(_accept, 1)
            f1.result()
            f2.result()

        statuses = {idx: data[0] for idx, data in results.items()}
        # Exactly one should succeed (200 or 201), the other should be 409 or 400.
        assert len(statuses) == 2
        successes = [s for s in statuses.values() if s in (200, 201)]
        failures = [s for s in statuses.values() if s in (409, 400)]
        assert len(successes) == 1, f"Expected 1 success, got statuses={statuses}"
        assert len(failures) == 1, f"Expected 1 failure, got statuses={statuses}"

        # Verify exactly one membership row was created.
        conn2 = _get_db_conn()
        count = conn2.execute(
            "SELECT COUNT(*) FROM org_members WHERE org_id = ? AND user_id = 3",
            (org_id,),
        ).fetchone()[0]
        conn2.close()
        assert count == 1, "Exactly one membership row must exist after concurrent accepts"


# ---------------------------------------------------------------------------
# Tests: Token Security
# ---------------------------------------------------------------------------


class TestInviteTokenSecurity:
    """Token security properties."""

    def test_raw_token_returned_once(self, client):
        """Raw token is returned only at create time."""
        org_id = _create_org("Test Org", 2)

        create_resp = client.post(
            f"/api/organizations/{org_id}/invites",
            json={"email": "user@x.com", "role": "member"},
            headers=auth_headers(admin_token),
        )
        raw = create_resp.json()["token"]

        # Token should NOT be returned on list
        list_resp = client.get(
            f"/api/organizations/{org_id}/invites",
            headers=auth_headers(admin_token),
        )
        for invite in list_resp.json()["invites"]:
            assert "token" not in invite
            assert "token_hash" not in invite

        # Token should NOT be returned on resend
        invite_id = create_resp.json()["invite_id"]
        resend_resp = client.post(
            f"/api/organizations/{org_id}/invites/{invite_id}/resend",
            headers=auth_headers(admin_token),
        )
        # Should return NEW token, not original
        assert resend_resp.json()["token"] != raw

    def test_token_hash_is_sha256(self, client):
        """Token hash stored as sha256 of raw token."""
        org_id = _create_org("Test Org", 2)
        conn = _get_db_conn()
        invite_id, raw = _make_invite(conn, org_id, "user@x.com", "member")
        conn.close()

        expected_hash = hashlib.sha256(raw.encode()).hexdigest()

        conn2 = _get_db_conn()
        cursor = conn2.execute(
            "SELECT token_hash FROM org_invites WHERE id = ?",
            (invite_id,),
        )
        stored_hash = cursor.fetchone()[0]
        conn2.close()
        assert stored_hash == expected_hash
        assert len(stored_hash) == 64  # sha256 hex length


# ---------------------------------------------------------------------------
# ADVERSARIAL TESTS: FR-012 Invite Flow Security
# ---------------------------------------------------------------------------


class TestAdversarialInviteSecurity:
    """Adversarial / security-focused tests for the org invite flow.

    Covers:
    - Token theft / IDOR
    - Cross-org authorization bypass (IDOR)
    - Role escalation
    - Token tampering
    - Enumeration resistance
    - Race conditions
    """

    # ------------------------------------------------------------------
    # IDOR: cross-org authorization bypass
    # ------------------------------------------------------------------

    def test_idor_cross_org_create_invite(self, client):
        """User of org B cannot create invites in org A (403 IDOR)."""
        org_a = _create_org("Org A", 2)  # admin1 owns org A
        org_b = _create_org("Org B", 1)  # superadmin owns org B
        # member1 is NOT a member of org B
        _add_org_member(org_a, 3, "member")

        response = client.post(
            f"/api/organizations/{org_b}/invites",
            json={"email": "intruder@x.com", "role": "member"},
            headers=auth_headers(member_token),
        )
        assert response.status_code == 403

    def test_idor_cross_org_list_invites(self, client):
        """User of org B cannot list invites in org A (403 IDOR)."""
        org_a = _create_org("Org A", 2)
        org_b = _create_org("Org B", 1)
        _add_org_member(org_a, 3, "member")
        conn = _get_db_conn()
        _make_invite(conn, org_a, "user@x.com", "member")
        conn.close()

        response = client.get(
            f"/api/organizations/{org_b}/invites",
            headers=auth_headers(member_token),
        )
        assert response.status_code == 403

    def test_idor_cross_org_resend_invite(self, client):
        """User of org B cannot resend invite in org A (403 IDOR)."""
        org_a = _create_org("Org A", 2)
        org_b = _create_org("Org B", 1)
        _add_org_member(org_a, 3, "member")
        conn = _get_db_conn()
        invite_id, _ = _make_invite(conn, org_a, "user@x.com", "member")
        conn.close()

        response = client.post(
            f"/api/organizations/{org_b}/invites/{invite_id}/resend",
            headers=auth_headers(member_token),
        )
        assert response.status_code == 403

    def test_idor_cross_org_revoke_invite(self, client):
        """User of org B cannot revoke invite in org A (403 IDOR)."""
        org_a = _create_org("Org A", 2)
        org_b = _create_org("Org B", 1)
        _add_org_member(org_a, 3, "member")
        conn = _get_db_conn()
        invite_id, _ = _make_invite(conn, org_a, "user@x.com", "member")
        conn.close()

        response = client.post(
            f"/api/organizations/{org_b}/invites/{invite_id}/revoke",
            headers=auth_headers(member_token),
        )
        assert response.status_code == 403

    def test_member_of_org_cannot_resend_own_org_invite(self, client):
        """Regular member cannot resend an invite even in their own org (403)."""
        org_id = _create_org("Test Org", 2)
        _add_org_member(org_id, 3, "member")
        conn = _get_db_conn()
        invite_id, _ = _make_invite(conn, org_id, "user@x.com", "member")
        conn.close()

        response = client.post(
            f"/api/organizations/{org_id}/invites/{invite_id}/resend",
            headers=auth_headers(member_token),
        )
        assert response.status_code == 403

    def test_member_of_org_cannot_revoke_own_org_invite(self, client):
        """Regular member cannot revoke an invite even in their own org (403)."""
        org_id = _create_org("Test Org", 2)
        _add_org_member(org_id, 3, "member")
        conn = _get_db_conn()
        invite_id, _ = _make_invite(conn, org_id, "user@x.com", "member")
        conn.close()

        response = client.post(
            f"/api/organizations/{org_id}/invites/{invite_id}/revoke",
            headers=auth_headers(member_token),
        )
        assert response.status_code == 403

    # ------------------------------------------------------------------
    # Role escalation
    # ------------------------------------------------------------------

    def test_invite_superadmin_role_rejected(self, client):
        """Inviting with role=superadmin is rejected (422)."""
        org_id = _create_org("Test Org", 2)

        response = client.post(
            f"/api/organizations/{org_id}/invites",
            json={"email": "bad@x.com", "role": "superadmin"},
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 422
        # The error message lists valid roles — confirm admin/member are the only options
        detail_msg = response.json()["detail"][0]["msg"].lower()
        assert "admin, member" in detail_msg
        assert "superadmin" not in detail_msg

    def test_invite_invalid_role_rejected(self, client):
        """Inviting with role=garbage is rejected (422)."""
        org_id = _create_org("Test Org", 2)

        response = client.post(
            f"/api/organizations/{org_id}/invites",
            json={"email": "bad@x.com", "role": "garbage"},
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 422

    def test_invite_empty_role_rejected(self, client):
        """Inviting with empty role is rejected (422)."""
        org_id = _create_org("Test Org", 2)

        response = client.post(
            f"/api/organizations/{org_id}/invites",
            json={"email": "bad@x.com", "role": ""},
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 422

    # ------------------------------------------------------------------
    # Token theft / stolen invite acceptance
    # ------------------------------------------------------------------

    def test_token_theft_wrong_user(self, client):
        """A user whose username does NOT match invite email cannot accept (403)."""
        org_id = _create_org("Test Org", 2)
        # Create invite for "member1@x.com"
        conn = _get_db_conn()
        _, raw_token = _make_invite(conn, org_id, "member1@x.com", "member")
        conn.close()
        # Authenticate as member2 (user_id 4, username "member2@x.com")
        # username does NOT match invite email "member1@x.com"

        response = client.post(
            "/api/organizations/invites/accept",
            json={"token": raw_token},
            headers=auth_headers(member2_token),
        )
        assert response.status_code == 403
        assert "not addressed to your account" in response.json()["detail"].lower()

    def test_token_theft_non_existent_user(self, client):
        """Invite for a completely different email cannot be accepted (403)."""
        org_id = _create_org("Test Org", 2)
        conn = _get_db_conn()
        _, raw_token = _make_invite(conn, org_id, "notapplicants@other.com", "member")
        conn.close()

        # member1@x.com tries to accept with token meant for notapplicants@other.com
        response = client.post(
            "/api/organizations/invites/accept",
            json={"token": raw_token},
            headers=auth_headers(member_token),
        )
        assert response.status_code == 403

    # ------------------------------------------------------------------
    # Old token invalidation after resend
    # ------------------------------------------------------------------

    def test_old_token_invalid_after_resend(self, client):
        """Old token cannot be used to accept after resend (400)."""
        org_id = _create_org("Test Org", 2)

        # Create ONE invite via API and capture the original token
        create_resp = client.post(
            f"/api/organizations/{org_id}/invites",
            json={"email": "member1@x.com", "role": "member"},
            headers=auth_headers(admin_token),
        )
        assert create_resp.status_code == 201
        old_token = create_resp.json()["token"]
        invite_id = create_resp.json()["invite_id"]

        # Resend: generates a new token, invalidates the old one
        resend_resp = client.post(
            f"/api/organizations/{org_id}/invites/{invite_id}/resend",
            headers=auth_headers(admin_token),
        )
        assert resend_resp.status_code == 201
        assert resend_resp.json()["token"] != old_token

        # Old token must be rejected
        response = client.post(
            "/api/organizations/invites/accept",
            json={"token": old_token},
            headers=auth_headers(member_token),
        )
        assert response.status_code == 400
        assert "invalid" in response.json()["detail"].lower()

    # ------------------------------------------------------------------
    # Token tampering
    # ------------------------------------------------------------------

    def test_token_tampering_fake_token(self, client):
        """Completely fake token is rejected (400)."""
        response = client.post(
            "/api/organizations/invites/accept",
            json={"token": "inv_fakefakefakefakefakefakefakefakefakefakefake"},
            headers=auth_headers(member_token),
        )
        assert response.status_code == 400
        assert "invalid" in response.json()["detail"].lower()

    def test_token_tampering_partial_token(self, client):
        """Partial/truncated token is rejected (400)."""
        response = client.post(
            "/api/organizations/invites/accept",
            json={"token": "inv_abc"},
            headers=auth_headers(member_token),
        )
        assert response.status_code == 400

    def test_token_tampering_empty_token(self, client):
        """Empty token is rejected (422/400)."""
        response = client.post(
            "/api/organizations/invites/accept",
            json={"token": ""},
            headers=auth_headers(member_token),
        )
        # Empty string fails pydantic validation → 422, or hash lookup fails → 400
        assert response.status_code in (400, 422)

    def test_token_tampering_none_token(self, client):
        """Missing token is rejected (422)."""
        response = client.post(
            "/api/organizations/invites/accept",
            json={},
            headers=auth_headers(member_token),
        )
        assert response.status_code == 422

    # ------------------------------------------------------------------
    # Enumeration resistance
    # ------------------------------------------------------------------

    def test_enumeration_no_token_leak(self, client):
        """Expired, revoked, and non-existent tokens return the same error.

        Uniform errors prevent an attacker from enumerating which invites exist.
        """
        # Non-existent token
        resp_none = client.post(
            "/api/organizations/invites/accept",
            json={"token": "inv_doesnotexist123456789012345678901234567890"},
            headers=auth_headers(member_token),
        )
        assert resp_none.status_code == 400

        # Expired token
        org_id = _create_org("Enum Org", 2)
        conn = _get_db_conn()
        _, expired_raw = _make_invite(conn, org_id, "member1@x.com", "member", days_valid=0)
        conn.close()
        resp_exp = client.post(
            "/api/organizations/invites/accept",
            json={"token": expired_raw},
            headers=auth_headers(member_token),
        )
        assert resp_exp.status_code == 400

        # All error responses must be identical to prevent enumeration.
        assert resp_none.json()["detail"] == resp_exp.json()["detail"] == "Invalid or expired invite"

    def test_revoked_vs_nonexistent_same_error(self, client):
        """Revoked and non-existent tokens return the same error message.

        Uniform errors prevent an attacker from enumerating which invites exist.
        """
        # Non-existent
        resp_none = client.post(
            "/api/organizations/invites/accept",
            json={"token": "inv_nevereverexisted1234567890123456789012"},
            headers=auth_headers(member_token),
        )
        # Revoked
        org_id = _create_org("Enum Org 2", 2)
        conn = _get_db_conn()
        invite_id, revoked_raw = _make_invite(conn, org_id, "member1@x.com", "member")
        conn.execute(
            "UPDATE org_invites SET revoked_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), invite_id),
        )
        conn.commit()
        conn.close()
        resp_rev = client.post(
            "/api/organizations/invites/accept",
            json={"token": revoked_raw},
            headers=auth_headers(member_token),
        )
        assert resp_rev.status_code == 400
        # All error responses must be identical to prevent enumeration.
        assert resp_none.json()["detail"] == resp_rev.json()["detail"] == "Invalid or expired invite"

    # ------------------------------------------------------------------
    # Double-accept race / idempotency
    # ------------------------------------------------------------------

    def test_double_accept_uniqueness_constraint(self, client):
        """Two accepts of the same token create at most one membership row.

        The UNIQUE(org_id, user_id) constraint on org_members prevents
        duplicate memberships even if two accepts are somehow processed.
        """
        org_id = _create_org("Race Org", 2)
        conn = _get_db_conn()
        _, raw_token = _make_invite(conn, org_id, "member1@x.com", "member")
        conn.close()

        # First accept succeeds
        resp1 = client.post(
            "/api/organizations/invites/accept",
            json={"token": raw_token},
            headers=auth_headers(member_token),
        )
        assert resp1.status_code == 200

        # Second accept fails with uniform message
        resp2 = client.post(
            "/api/organizations/invites/accept",
            json={"token": raw_token},
            headers=auth_headers(member_token),
        )
        assert resp2.status_code == 400
        assert resp2.json()["detail"] == "Invalid or expired invite"

        # Only one membership row exists
        conn3 = _get_db_conn()
        count = conn3.execute(
            "SELECT COUNT(*) FROM org_members WHERE org_id = ? AND user_id = 3",
            (org_id,),
        ).fetchone()[0]
        conn3.close()
        assert count == 1

    # ------------------------------------------------------------------
    # Cross-org accept boundary
    # ------------------------------------------------------------------

    def test_accept_invite_creates_membership_in_correct_org_only(self, client):
        """Accepting an invite creates membership in the invite's org, not another org.

        Verifies the invite's org_id is used, not the caller's org membership.
        """
        org_a = _create_org("Org A", 2)
        org_b = _create_org("Org B", 1)
        # member1 (user 3) is member of org B only (via _add_org_member below)
        _add_org_member(org_b, 3, "member")

        # Create invite in org A for member1@x.com
        conn = _get_db_conn()
        invite_id, raw_token = _make_invite(conn, org_a, "member1@x.com", "member")
        conn.close()

        # Accept the invite
        response = client.post(
            "/api/organizations/invites/accept",
            json={"token": raw_token},
            headers=auth_headers(member_token),
        )
        assert response.status_code == 200

        # Verify member is in org A (invite's org)
        conn2 = _get_db_conn()
        row_a = conn2.execute(
            "SELECT role FROM org_members WHERE org_id = ? AND user_id = 3",
            (org_a,),
        ).fetchone()
        row_b = conn2.execute(
            "SELECT role FROM org_members WHERE org_id = ? AND user_id = 3",
            (org_b,),
        ).fetchone()
        conn2.close()
        assert row_a is not None, "User should be in org A (invite's org)"
        assert row_a[0] == "member"
        assert row_b is not None, "User should also remain in org B"
        assert row_b[0] == "member"

    # ------------------------------------------------------------------
    # Resend is idempotent-ish for accepted/revoked
    # ------------------------------------------------------------------

    def test_resend_already_accepted_fails(self, client):
        """Cannot resend an already-accepted invite (400)."""
        org_id = _create_org("Test Org", 2)
        conn = _get_db_conn()
        invite_id, _ = _make_invite(conn, org_id, "member1@x.com", "member")
        conn.execute(
            "UPDATE org_invites SET accepted_at = ?, accepted_by_user_id = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), 3, invite_id),
        )
        conn.commit()
        conn.close()

        response = client.post(
            f"/api/organizations/{org_id}/invites/{invite_id}/resend",
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 400
        assert "accepted" in response.json()["detail"].lower()

    # ------------------------------------------------------------------
    # Revoke + accept sequence
    # ------------------------------------------------------------------

    def test_cannot_accept_after_revoke_followed_by_resend(self, client):
        """Even if resend is called on a revoked invite (edge case), accept still fails."""
        org_id = _create_org("Test Org", 2)
        conn = _get_db_conn()
        invite_id, old_raw = _make_invite(conn, org_id, "member1@x.com", "member")
        conn.execute(
            "UPDATE org_invites SET revoked_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), invite_id),
        )
        conn.commit()
        conn.close()

        # Try to resend a revoked invite → 400
        resp_resend = client.post(
            f"/api/organizations/{org_id}/invites/{invite_id}/resend",
            headers=auth_headers(admin_token),
        )
        assert resp_resend.status_code == 400

        # Old token also fails (still revoked)
        resp_accept = client.post(
            "/api/organizations/invites/accept",
            json={"token": old_raw},
            headers=auth_headers(member_token),
        )
        assert resp_accept.status_code == 400

    # ------------------------------------------------------------------
    # Token hash uniqueness (sha256 collision resistance)
    # ------------------------------------------------------------------

    def test_token_hash_uniqueness_constraint(self, client):
        """Cannot insert two invites with identical token hashes (DB constraint).

        This verifies that even if the same raw token were somehow reused,
        the UNIQUE token_hash constraint would prevent it.
        """
        org_id = _create_org("Test Org", 2)

        # Create first invite normally
        resp1 = client.post(
            f"/api/organizations/{org_id}/invites",
            json={"email": "first@x.com", "role": "member"},
            headers=auth_headers(admin_token),
        )
        assert resp1.status_code == 201

        # Try to manually insert a duplicate token hash directly in DB
        conn = _get_db_conn()
        first_hash = hashlib.sha256(resp1.json()["token"].encode()).hexdigest()
        try:
            conn.execute(
                """INSERT INTO org_invites
                   (org_id, email, token_hash, role, expires_at, created_at, created_by_user_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    org_id,
                    "second@x.com",
                    first_hash,  # duplicate hash!
                    "member",
                    (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
                    datetime.now(timezone.utc).isoformat(),
                    2,
                ),
            )
            conn.commit()
            # If we get here, the constraint didn't fire (shouldn't happen)
            pytest.fail("UNIQUE constraint on token_hash was not enforced")
        except sqlite3.IntegrityError as e:
            assert "UNIQUE constraint failed" in str(e)
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # List invites never exposes token
    # ------------------------------------------------------------------

    def test_list_invites_never_returns_token_hash(self, client):
        """GET /invites never returns token or token_hash fields."""
        org_id = _create_org("Test Org", 2)
        conn = _get_db_conn()
        _make_invite(conn, org_id, "user1@x.com", "member")
        _make_invite(conn, org_id, "user2@x.com", "admin")
        _make_invite(conn, org_id, "user3@x.com", "member")
        conn.close()

        response = client.get(
            f"/api/organizations/{org_id}/invites",
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 200
        for invite in response.json()["invites"]:
            assert "token" not in invite
            assert "token_hash" not in invite
            # Also verify no other leaking field
            assert "inv_" not in str(invite)

    # ------------------------------------------------------------------
    # Superadmin without org membership cannot manage invites
    # ------------------------------------------------------------------

    def test_superadmin_without_org_membership_cannot_invite(self, client):
        """Superadmin who is not a member/admin/owner of org A cannot create invites there (403)."""
        org_id = _create_org("Orga", 2)  # admin1 owns it; superadmin (user 1) is NOT a member

        response = client.post(
            f"/api/organizations/{org_id}/invites",
            json={"email": "new@x.com", "role": "member"},
            headers=auth_headers(superadmin_token),
        )
        # superadmin_token() uses user_id=1 who is NOT a member of this org
        assert response.status_code == 403

    def test_superadmin_without_org_membership_cannot_list(self, client):
        """Superadmin who is not a member/admin/owner of org A cannot list invites (403)."""
        org_id = _create_org("Orgb", 2)
        conn = _get_db_conn()
        _make_invite(conn, org_id, "user@x.com", "member")
        conn.close()

        response = client.get(
            f"/api/organizations/{org_id}/invites",
            headers=auth_headers(superadmin_token),
        )
        assert response.status_code == 403

