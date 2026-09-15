"""Org-invite identifier resolution acceptance check (issue #560, check C4).

Encoded rule: creating an org invite whose identifier does not resolve to
an existing user's username is rejected with 400 at creation (instead of
minting a permanently unredeemable token — acceptance matches the stored
identifier against usernames, and the users table has no email column).

PRESERVED behaviors (issue #300 design): inviting an existing PLAIN
username mints 201; inviting an existing EMAIL-SHAPED username mints 201;
inviting an existing viewer stays 400 (below-member rule).

Error-message contract for the unresolvable case (documented for the
implementer): the 400 detail must echo the offending identifier — the
literal identifier string ("dept@example.test") appears in the detail.

Base-expected outcome: RED (DISCRIMINATING) — the unresolvable identifier
returns 201 at base; the PRESERVING sub-tests pass at base and after.
"""

import sqlite3
import tempfile
from pathlib import Path

import pytest
from backend.tests.schema_constants import build_test_schema
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes.organizations import router as organizations_router
from app.services.auth_service import (
    compute_client_fingerprint,
    create_access_token,
    hash_password,
)


@pytest.fixture(autouse=True)
def setup_db(monkeypatch):
    """Temp DB with the shared test schema plus seeded users (harness
    mirrored from test_org_invites.py)."""
    temp_dir = tempfile.mkdtemp()
    db_path = str(Path(temp_dir) / "app.db")

    from app.models.database import _pool_cache, _pool_cache_lock

    with _pool_cache_lock:
        for path, pool in list(_pool_cache.items()):
            pool.close_all()
        _pool_cache.clear()

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    build_test_schema(conn)
    conn.commit()
    conn.close()

    monkeypatch.setattr("app.config.settings.data_dir", Path(temp_dir))
    monkeypatch.setattr(
        "app.config.settings.jwt_secret_key",
        "test-secret-key-for-testing-only-min-32-chars!!",
    )
    monkeypatch.setattr("app.config.settings.users_enabled", True)

    # Seed users: plain usernames for 1-3, plus one email-shaped username
    # and one viewer for the preserving edges.
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    pw = hash_password("testpass")
    for user_id, username, role in (
        (1, "superadmin", "superadmin"),
        (2, "admin1", "admin"),
        (3, "member1", "member"),
        (4, "member2@x.com", "member"),
        (5, "viewer1", "viewer"),
    ):
        conn.execute(
            "INSERT INTO users (id, username, hashed_password, full_name, "
            "role, is_active) VALUES (?, ?, ?, ?, ?, 1)",
            (user_id, username, pw, username.title(), role),
        )
    conn.commit()
    conn.close()

    yield db_path

    with _pool_cache_lock:
        if db_path in _pool_cache:
            _pool_cache[db_path].close_all()
            del _pool_cache[db_path]

    import shutil

    shutil.rmtree(temp_dir, ignore_errors=True)


def _get_db_conn():
    """Direct connection to the test DB for seeding (mirror of
    test_org_invites.py)."""
    from app.config import settings

    return sqlite3.connect(str(settings.sqlite_path))


def _create_org(name: str, owner_user_id: int) -> int:
    """Create an organization owned by owner_user_id; return its id."""
    conn = _get_db_conn()
    conn.execute("PRAGMA foreign_keys = ON")
    cursor = conn.execute(
        "INSERT INTO organizations (name, description, slug, created_by) "
        "VALUES (?, ?, ?, ?)",
        (name, "Desc", name.lower().replace(" ", "-"), owner_user_id),
    )
    org_id = cursor.lastrowid
    conn.execute(
        "INSERT INTO org_members (org_id, user_id, role) "
        "VALUES (?, ?, 'owner')",
        (org_id, owner_user_id),
    )
    conn.commit()
    conn.close()
    return org_id


def _admin_token() -> str:
    return create_access_token(
        2, "admin1", "admin", client_fingerprint=compute_client_fingerprint("")
    )


def _auth_headers() -> dict:
    return {"Authorization": f"Bearer {_admin_token()}"}


@pytest.fixture
def client():
    """Standalone app with the organizations router mounted at /api."""
    app = FastAPI()
    app.include_router(organizations_router, prefix="/api")
    tc = TestClient(app)
    # Override default User-Agent so fingerprint validation matches token
    tc.headers["user-agent"] = ""
    return tc


class TestInviteIdentifierResolution:
    """POST /api/organizations/{org_id}/invites identifier resolution."""

    def test_unresolvable_email_identifier_rejected_400(self, client):
        """RED at base (201 today): an email that is not any existing
        user's username must be rejected at creation, and the error must
        name the unresolvable identifier."""
        org_id = _create_org("Resolution Org", 2)  # admin1 owns it
        response = client.post(
            f"/api/organizations/{org_id}/invites",
            json={"email": "dept@example.test", "role": "member"},
            headers=_auth_headers(),
        )
        assert response.status_code == 400, (
            "invite to an identifier matching no existing username must be "
            f"rejected at creation, got {response.status_code}: "
            f"{response.text}"
        )
        # Message contract: the detail must echo the unresolvable identifier
        # (documented contract — see module docstring).
        assert "dept@example.test" in response.json()["detail"], (
            "the 400 detail must name the unresolvable identifier, got: "
            f"{response.json()['detail']!r}"
        )

    def test_existing_plain_username_invite_still_201(self, client):
        """PRESERVING: inviting an existing plain username keeps minting an
        invite (201)."""
        org_id = _create_org("Plain Org", 2)
        response = client.post(
            f"/api/organizations/{org_id}/invites",
            json={"email": "member1", "role": "member"},
            headers=_auth_headers(),
        )
        assert response.status_code == 201
        assert response.json()["email"] == "member1"

    def test_email_shaped_username_invite_still_201(self, client):
        """PRESERVING: a user whose username IS email-shaped stays
        invitable by that exact string (201)."""
        org_id = _create_org("EmailShaped Org", 2)
        response = client.post(
            f"/api/organizations/{org_id}/invites",
            json={"email": "member2@x.com", "role": "member"},
            headers=_auth_headers(),
        )
        assert response.status_code == 201
        assert response.json()["email"] == "member2@x.com"

    def test_existing_viewer_invite_still_400(self, client):
        """PRESERVING: inviting an existing viewer stays 400 (below-member
        rule at organizations.py:643-649)."""
        org_id = _create_org("Viewer Org", 2)
        response = client.post(
            f"/api/organizations/{org_id}/invites",
            json={"email": "viewer1", "role": "member"},
            headers=_auth_headers(),
        )
        assert response.status_code == 400
        assert "global role below member" in response.json()["detail"].lower()
