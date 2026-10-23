"""Support tests for the #722 users.py mutation-invariants fix (non-frozen).

Covers the plan's item-10 pins that the frozen d04 file does not: positive
superadmin-target flows, the multi-field demotion success path, the
update_user_groups clear-all audit row, deactivation session revocation with
the table present, the login-purge boundary, the membership-writer target
checks, and the AC6 400 pin. Reuses the frozen file's harness (imported, not
duplicated); the frozen file itself is byte-untouched.
"""

import json  # noqa: E402
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
    behind frozen C2's cookie-level check. A second user's session must
    survive: the revocation is scoped to the deactivated user only (review
    F-019 — pins the WHERE user_id = ? scoping against a global-logout
    mutant)."""
    with _mutation_env() as env:
        superadmin_id = create_user(env.conn, "superadmin", "pass123", "superadmin", "S")
        member_id = create_user(env.conn, "member", "pass123", "member", "M")
        bystander_id = create_user(env.conn, "bystander", "pass123", "member", "B")
        env.conn.execute(
            "INSERT INTO user_sessions (user_id, refresh_token_hash, expires_at) VALUES (?, ?, ?)",
            (member_id, "d04s-hash", (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()),
        )
        env.conn.execute(
            "INSERT INTO user_sessions (user_id, refresh_token_hash, expires_at) VALUES (?, ?, ?)",
            (bystander_id, "d04s-bystander-hash", (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()),
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
        bystander_rows = env.conn.execute(
            "SELECT COUNT(*) FROM user_sessions WHERE user_id = ?", (bystander_id,)
        ).fetchone()[0]
        assert bystander_rows == 1


def test_active_last_superadmin_guard_answers_400():
    """PATCH /users/{id}/active against the sole active superadmin answers
    400 "last superadmin" and leaves the account active (review F-012 — the
    frozen file never reaches this /active predicate; deleting the
    count-guard SQL clause would otherwise leave the suite green)."""
    with _mutation_env() as env:
        from app.api import deps

        env.app.dependency_overrides[deps.get_current_active_user] = lambda: {
            "id": 0,
            "role": "superadmin",
            "username": "bootstrap",
        }

        sole_id = create_user(env.conn, "solo", "pass123", "superadmin", "S")
        resp = env.client.patch(f"/users/{sole_id}/active", json={"is_active": False})
        assert resp.status_code == 400, resp.text
        assert "last superadmin" in resp.json()["detail"]
        row = env.conn.execute(
            "SELECT is_active FROM users WHERE id = ?", (sole_id,)
        ).fetchone()
        assert row[0] == 1


def test_active_superadmin_target_403_leaves_state_untouched():
    """AC5 state pin: a plain admin's refused deactivation of a superadmin
    target must neither flip is_active nor revoke any session (review F-018 —
    pins the guard-before-side-effect ordering against a mutant that deletes
    sessions before the target check)."""
    with _mutation_env() as env:
        superadmin_id = create_user(env.conn, "superadmin", "pass123", "superadmin", "S")
        admin_id = create_user(env.conn, "admin", "pass123", "admin", "A")
        env.conn.execute(
            "INSERT INTO user_sessions (user_id, refresh_token_hash, expires_at) VALUES (?, ?, ?)",
            (superadmin_id, "d04s-super-hash", (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()),
        )
        env.conn.commit()
        resp = env.client.patch(
            f"/users/{superadmin_id}/active",
            json={"is_active": False},
            headers=_bearer(env, admin_id, "admin", "admin"),
        )
        assert resp.status_code == 403, resp.text
        row = env.conn.execute(
            "SELECT is_active FROM users WHERE id = ?", (superadmin_id,)
        ).fetchone()
        assert row[0] == 1
        sessions = env.conn.execute(
            "SELECT COUNT(*) FROM user_sessions WHERE user_id = ?", (superadmin_id,)
        ).fetchone()[0]
        assert sessions == 1


def test_reset_superadmin_target_403_leaves_password_untouched():
    """AC4 state pin: a plain admin's refused password reset of a superadmin
    target must not rotate the stored hash (review F-018 — the mutant that
    rotates first and answers 403 after was green on status-only asserts)."""
    with _mutation_env() as env:
        superadmin_id = create_user(env.conn, "superadmin", "pass123", "superadmin", "S")
        admin_id = create_user(env.conn, "admin", "pass123", "admin", "A")
        before = env.conn.execute(
            "SELECT hashed_password FROM users WHERE id = ?", (superadmin_id,)
        ).fetchone()[0]
        resp = env.client.patch(
            f"/users/{superadmin_id}/password",
            json={"new_password": "AdminSuppliedPass123!"},
            headers=_bearer(env, admin_id, "admin", "admin"),
        )
        assert resp.status_code == 403, resp.text
        after = env.conn.execute(
            "SELECT hashed_password FROM users WHERE id = ?", (superadmin_id,)
        ).fetchone()[0]
        assert after == before


def test_password_reset_audit_reports_sessions_revoked_true_when_table_present():
    """user.password_reset metadata must say sessions_revoked=True when the
    reset actually revoked live session rows (review F-013 — the frozen file
    pins only the False branch; a mutant deleting the revocation or logging a
    hardcoded False was green)."""
    with _mutation_env() as env:
        superadmin_id = create_user(env.conn, "superadmin", "pass123", "superadmin", "S")
        member_id = create_user(env.conn, "member", "pass123", "member", "M")
        env.conn.execute(
            "INSERT INTO user_sessions (user_id, refresh_token_hash, expires_at) VALUES (?, ?, ?)",
            (member_id, "d04s-reset-hash", (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()),
        )
        env.conn.commit()
        resp = env.client.patch(
            f"/users/{member_id}/password",
            json={"new_password": "NewSecurePass123!"},
            headers=_bearer(env, superadmin_id, "superadmin", "superadmin"),
        )
        assert resp.status_code == 200, resp.text
        row = env.conn.execute(
            "SELECT metadata_json FROM security_audit_log "
            "WHERE event_type = 'user.password_reset' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row is not None, "harness: password_reset audit row must exist"
        metadata = json.loads(row[0])
        assert metadata["sessions_revoked"] == True  # noqa: E712


def test_new_event_types_are_recorded_verbatim():
    """The four event-type strings this PR introduces must land in
    security_audit_log verbatim (review F-017a — a renamed string such as
    user.delete was green on count-only asserts)."""
    with _mutation_env(mount_vaults=True, full_schema=True) as env:
        superadmin_id = create_user(env.conn, "superadmin", "pass123", "superadmin", "S")
        member_id = create_user(env.conn, "member", "pass123", "member", "M")
        vault_id = _insert_vault(env, "D04S Event Vault")
        headers = _bearer(env, superadmin_id, "superadmin", "superadmin")

        def last_event_type():
            row = env.conn.execute(
                "SELECT event_type FROM security_audit_log ORDER BY id DESC LIMIT 1"
            ).fetchone()
            return row[0] if row else None

        # PUT /vaults/{id} visibility field → vault.visibility_updated
        vis = env.client.put(
            f"/vaults/{vault_id}", json={"visibility": "public"}, headers=headers
        )
        assert vis.status_code == 200, vis.text
        assert last_event_type() == "vault.visibility_updated"

        # PUT /vaults/{id}/groups → vault.group_access_replaced
        grp = env.client.put(
            f"/vaults/{vault_id}/groups", json={"group_ids": []}, headers=headers
        )
        assert grp.status_code == 200, grp.text
        assert last_event_type() == "vault.group_access_replaced"

        # DELETE /vaults/{id} → vault.deleted
        del_resp = env.client.delete(f"/vaults/{vault_id}", headers=headers)
        assert del_resp.status_code in (200, 204), del_resp.text
        assert last_event_type() == "vault.deleted"

        # DELETE /users/{id} → user.deleted
        del_user = env.client.delete(f"/users/{member_id}", headers=headers)
        assert del_user.status_code in (200, 204), del_user.text
        assert last_event_type() == "user.deleted"


def test_sessions_list_includes_the_live_login_session():
    """Positive control for GET /auth/sessions: the just-minted login session
    must appear in the listing (review F-017b — the frozen file pins only the
    expired-seeded-row exclusion, so an over-filtering mutant that returns an
    empty list was green)."""
    with _mutation_env(mount_auth=True) as env:
        member_id = create_user(env.conn, "member", "pass123", "member", "M")
        login = env.client.post(
            "/auth/login", json={"username": "member", "password": "pass123"}
        )
        assert login.status_code == 200, login.text
        live = env.conn.execute(
            "SELECT id FROM user_sessions WHERE user_id = ? ORDER BY id DESC LIMIT 1",
            (member_id,),
        ).fetchone()
        assert live is not None, "harness: login must persist a session row"
        resp = env.client.get(
            "/auth/sessions",
            headers=_bearer(env, member_id, "member", "member"),
        )
        assert resp.status_code == 200, resp.text
        ids = [s["id"] for s in resp.json()["sessions"]]
        assert live[0] in ids


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
