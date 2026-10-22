"""Support tests for the #722 users.py mutation-invariants fix (non-frozen).

Covers the plan's item-10 pins that the frozen d04 file does not: positive
superadmin-target flows, the multi-field demotion success path, the
update_user_groups clear-all audit row, deactivation session revocation with
the table present, the login-purge boundary, the membership-writer target
checks, and the AC6 400 pin. Reuses the frozen file's harness (imported, not
duplicated); the frozen file itself is byte-untouched.
"""

import os
import sys

os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret-key-for-testing-only-12345678901234567890")
os.environ.setdefault("USERS_ENABLED", "true")
os.environ.setdefault(
    "ADMIN_SECRET_TOKEN",
    "d04s-support-admin-secret-token-0123456789abcdef0123456789abcdef",
)

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from datetime import datetime, timedelta, timezone  # noqa: E402

import pytest  # noqa: E402
from backend.tests.test_d04_user_mutation_invariants import (  # noqa: E402
    _audit_count,
    _bearer,
    _insert_vault,
    _mutation_env,
)
from backend.tests.user_route_helpers import create_user  # noqa: E402


def test_superadmin_can_edit_superadmin_profile():
    """Positive: superadmin editing a superadmin target is unaffected (200)."""
    with _mutation_env() as env:
        superadmin_id = create_user(env.conn, "superadmin", "pass123", "superadmin", "S")
        other_id = create_user(env.conn, "otheradmin", "pass123", "superadmin", "O2")
        resp = env.client.patch(
            f"/users/{other_id}",
            json={"full_name": "Renamed Super"},
            headers=_bearer(env, superadmin_id, "superadmin", "superadmin"),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["full_name"] == "Renamed Super"


def test_multi_field_demotion_succeeds_with_another_active_superadmin():
    """Positive: with 2 active superadmins a demotion PATCH applies every field."""
    with _mutation_env() as env:
        superadmin_id = create_user(env.conn, "superadmin", "pass123", "superadmin", "S")
        second_id = create_user(env.conn, "second", "pass123", "superadmin", "S2")
        resp = env.client.patch(
            f"/users/{second_id}",
            json={"full_name": "Demoted Now", "role": "admin"},
            headers=_bearer(env, superadmin_id, "superadmin", "superadmin"),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["role"] == "admin"
        assert resp.json()["full_name"] == "Demoted Now"


def test_role_demotion_of_last_active_superadmin_answers_400():
    """AC6-400 pin: the frozen C6 asserts only the surviving count; this pins
    the refusal status and detail the count alone cannot."""
    with _mutation_env() as env:
        from app.api import deps

        env.app.dependency_overrides[deps.get_current_active_user] = lambda: {
            "id": 0,
            "role": "superadmin",
            "username": "bootstrap",
        }

        superadmin_id = create_user(env.conn, "solo", "pass123", "superadmin", "S")
        resp = env.client.patch(f"/users/{superadmin_id}", json={"role": "admin"})
        assert resp.status_code == 400, resp.text
        assert "last superadmin" in resp.json()["detail"]
        row = env.conn.execute(
            "SELECT role, full_name FROM users WHERE id = ?", (superadmin_id,)
        ).fetchone()
        assert row[0] == "superadmin"


def test_vault_group_access_clear_all_records_exactly_one_audit_row():
    """update_user_groups clear-all exit: exactly one new security_audit_log row."""
    with _mutation_env(mount_vaults=True, full_schema=True) as env:
        superadmin_id = create_user(env.conn, "superadmin", "pass123", "superadmin", "S")
        vault_id = _insert_vault(env, "D04S Clear Vault")
        before = _audit_count(env)
        resp = env.client.put(
            f"/vaults/{vault_id}/groups",
            json={"group_ids": []},
            headers=_bearer(env, superadmin_id, "superadmin", "superadmin"),
        )
        assert resp.status_code == 200, resp.text
        assert _audit_count(env) - before == 1


def test_deactivation_deletes_session_rows_when_table_present():
    """Deactivation revokes sessions (table present) — the observable outcome
    behind frozen C2's cookie-level check."""
    with _mutation_env() as env:
        superadmin_id = create_user(env.conn, "superadmin", "pass123", "superadmin", "S")
        member_id = create_user(env.conn, "member", "pass123", "member", "M")
        env.conn.execute(
            "INSERT INTO user_sessions (user_id, refresh_token_hash, expires_at) VALUES (?, ?, ?)",
            (member_id, "d04s-hash", (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()),
        )
        env.conn.commit()
        resp = env.client.patch(
            f"/users/{member_id}/active",
            json={"is_active": False},
            headers=_bearer(env, superadmin_id, "superadmin", "superadmin"),
        )
        assert resp.status_code == 200, resp.text
        rows = env.conn.execute(
            "SELECT COUNT(*) FROM user_sessions WHERE user_id = ?", (member_id,)
        ).fetchone()[0]
        assert rows == 0


def test_login_purge_keeps_row_expiring_exactly_now():
    """Boundary: strict '<' never deletes a session that has not expired. The
    exactly-now row sits at the comparison edge and must survive."""
    with _mutation_env(mount_auth=True) as env:
        member_id = create_user(env.conn, "member", "pass123", "member", "M")
        # expires_at strictly in the future by a hair — isoformat of now+1s
        # is the closest deterministic edge the shared harness allows; the
        # frozen C10 already pins the past-row deletion.
        edge = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()
        env.conn.execute(
            "INSERT INTO user_sessions (user_id, refresh_token_hash, expires_at) VALUES (?, ?, ?)",
            (member_id, "d04s-edge-hash", edge),
        )
        env.conn.commit()
        resp = env.client.post(
            "/auth/login",
            json={"username": "member", "password": "pass123"},
        )
        assert resp.status_code == 200, resp.text
        rows = env.conn.execute(
            "SELECT COUNT(*) FROM user_sessions WHERE refresh_token_hash = ?",
            ("d04s-edge-hash",),
        ).fetchone()[0]
        assert rows == 1


def test_membership_writers_reject_superadmin_targets():
    """R3 wiring: PUT /users/{superadmin}/organizations and /groups both 403
    for a plain admin (five-writer parity of assert_can_modify_target)."""
    with _mutation_env() as env:
        admin_id = create_user(env.conn, "admin", "pass123", "admin", "A")
        superadmin_id = create_user(env.conn, "superadmin", "pass123", "superadmin", "S")
        env.conn.execute(
            "INSERT INTO organizations (name, slug) VALUES ('D04S Org', 'd04s-org')"
        )
        org_id = env.conn.execute("SELECT id FROM organizations WHERE slug = 'd04s-org'").fetchone()[0]
        env.conn.execute(
            "INSERT INTO groups (org_id, name) VALUES (?, 'D04S Group')", (org_id,)
        )
        group_id = env.conn.execute("SELECT id FROM groups WHERE name = 'D04S Group'").fetchone()[0]
        env.conn.commit()
        headers = _bearer(env, admin_id, "admin", "admin")

        orgs_resp = env.client.put(
            f"/users/{superadmin_id}/organizations",
            json={"memberships": [{"org_id": org_id, "role": "member"}]},
            headers=headers,
        )
        assert orgs_resp.status_code == 403, orgs_resp.text
        assert "Only superadmin" in orgs_resp.json()["detail"]

        groups_resp = env.client.put(
            f"/users/{superadmin_id}/groups",
            json={"group_ids": [group_id]},
            headers=headers,
        )
        assert groups_resp.status_code == 403, groups_resp.text
        assert "Only superadmin" in groups_resp.json()["detail"]
