"""Sibling regressions for the #726 operational-hygiene census (trace 725-726-d07-d08).

Covers the plan-critic-mandated additions beyond the frozen checks: the
concurrent last-admin matrix (two real connections), the third replace-set
route family (PUT /api/groups/{id}/vaults) with omitted/null/explicit-empty
spelling pins, the groups per_page bounds, the ALLOW_LOCAL_SERVICES
precedence matrix, and the post-commit guard pins (users update re-read,
create_vault Optional response). RED at base for every defect leg; pins that
assert already-correct base behavior are marked.
"""

import os
import sqlite3
import tempfile
import unittest

from fastapi.testclient import TestClient

from app.api.deps import get_current_active_user, get_db
from app.config import settings
from app.main import app
from app.models.database import SQLiteConnectionPool, _pool_cache, init_db
from app.security import csrf_protect


class _SiblingBase(unittest.TestCase):
    """Full-app harness (test_vaults.py pattern) with pool + settings restore."""

    def setUp(self):
        self._temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self._temp_dir, "probe.db")
        init_db(self.db_path)
        _pool_cache.clear()
        self.pool = SQLiteConnectionPool(self.db_path, max_size=4)

        conn = self.pool.get_connection()
        conn.execute(
            "INSERT INTO users (id, username, hashed_password, role, is_active)"
            " VALUES (1, 'superadmin', 'abc123', 'superadmin', 1)"
        )
        conn.execute(
            "INSERT INTO users (id, username, hashed_password, role, is_active)"
            " VALUES (2, 'admin2', 'abc123', 'member', 1)"
        )
        conn.execute(
            "INSERT INTO users (id, username, hashed_password, role, is_active)"
            " VALUES (3, 'admin3', 'abc123', 'member', 1)"
        )
        conn.execute(
            "INSERT INTO organizations (id, name) VALUES (1, 'probe-org')"
        )
        conn.execute(
            "INSERT INTO org_members (org_id, user_id, role) VALUES (1, 1, 'admin')"
        )
        conn.commit()
        self.pool.release_connection(conn)

        def override_get_db():
            c = self.pool.get_connection()
            try:
                yield c
            finally:
                self.pool.release_connection(c)

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[csrf_protect] = lambda: "probe-csrf"

        from app.api.routes import users as users_module

        self._users_module = users_module
        self._orig_get_pool = users_module.get_pool
        users_module.get_pool = lambda path: self.pool

        self._saved = {
            k: getattr(settings, k)
            for k in ("users_enabled", "admin_secret_token", "allow_local_curator",
                      "allow_local_services")
        }
        settings.users_enabled = True
        self.su = {
            "id": 1,
            "username": "superadmin",
            "full_name": "S",
            "role": "superadmin",
            "is_active": True,
            "must_change_password": False,
        }
        app.dependency_overrides[get_current_active_user] = lambda: dict(self.su)
        self.client = TestClient(app)
        self.client.headers["user-agent"] = ""

    def tearDown(self):
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_active_user, None)
        app.dependency_overrides.pop(csrf_protect, None)
        self._users_module.get_pool = self._orig_get_pool
        for k, v in self._saved.items():
            setattr(settings, k, v)
        os.environ.pop("ALLOW_LOCAL_SERVICES", None)
        self.client.close()
        self.pool.close_all()
        _pool_cache.clear()

    def _conn(self):
        return self.pool.get_connection()

    def _release(self, conn):
        self.pool.release_connection(conn)

    def _make_vault_with_admins(self, admin_user_ids):
        conn = self._conn()
        cur = conn.execute(
            "INSERT INTO vaults (name, description) VALUES ('SV', 'd')"
        )
        vid = cur.lastrowid
        for uid in admin_user_ids:
            conn.execute(
                "INSERT INTO vault_members (vault_id, user_id, permission, granted_by)"
                " VALUES (?, ?, 'admin', 1)",
                (vid, uid),
            )
        conn.commit()
        self._release(conn)
        return vid

    def _admin_count(self, vid):
        conn = self._conn()
        n = conn.execute(
            "SELECT COUNT(*) FROM vault_members WHERE vault_id = ? AND permission = 'admin'",
            (vid,),
        ).fetchone()[0]
        self._release(conn)
        return n


class ConcurrentLastAdminMatrix(_SiblingBase):
    """Real two-connection races against the BEGIN IMMEDIATE guard."""

    def test_two_admins_demote_one_succeeds(self):
        vid = self._make_vault_with_admins([2, 3])
        r = self.client.patch(
            "/api/vaults/%d/members/2" % vid, json={"permission": "read"}
        )
        self.assertEqual(r.status_code, 200, r.text[:200])
        self.assertEqual(self._admin_count(vid), 1)

    def test_last_admin_demotion_refused_even_by_route(self):
        vid = self._make_vault_with_admins([2])
        r = self.client.patch(
            "/api/vaults/%d/members/2" % vid, json={"permission": "read"}
        )
        self.assertEqual(r.status_code, 409, r.text[:200])
        self.assertEqual(self._admin_count(vid), 1)

    def test_last_admin_removal_refused(self):
        vid = self._make_vault_with_admins([3])
        r = self.client.delete("/api/vaults/%d/members/3" % vid)
        self.assertEqual(r.status_code, 409, r.text[:200])
        self.assertEqual(self._admin_count(vid), 1)

    def test_concurrent_double_demotion_leaves_one_admin(self):
        """Two simultaneous API demotions of BOTH admins of a two-admin vault.

        BEGIN IMMEDIATE serializes the writers; the loser re-reads the admin
        count inside its own write transaction AFTER the winner committed, so
        it must observe one admin and refuse (409). Never a 500; the vault
        always keeps exactly one admin.
        """
        import threading

        vid = self._make_vault_with_admins([2, 3])
        barrier = threading.Barrier(3)
        results = {}

        def demote(user_id, key):
            barrier.wait(timeout=30)
            r = self.client.patch(
                "/api/vaults/%d/members/%d" % (vid, user_id),
                json={"permission": "read"},
            )
            results[key] = r.status_code

        t1 = threading.Thread(target=demote, args=(2, "a"))
        t2 = threading.Thread(target=demote, args=(3, "b"))
        t1.start()
        t2.start()
        barrier.wait(timeout=30)
        t1.join(timeout=60)
        t2.join(timeout=60)

        self.assertNotIn(500, results.values(), results)
        self.assertEqual(
            sorted(results.values()), [200, 409], results
        )
        self.assertEqual(self._admin_count(vid), 1)

    def test_concurrent_demote_and_remove_leaves_one_admin(self):
        import threading

        vid = self._make_vault_with_admins([2, 3])
        barrier = threading.Barrier(3)
        results = {}

        def demote():
            barrier.wait(timeout=30)
            r = self.client.patch(
                "/api/vaults/%d/members/2" % vid, json={"permission": "read"}
            )
            results["demote"] = r.status_code

        def remove():
            barrier.wait(timeout=30)
            r = self.client.delete("/api/vaults/%d/members/3" % vid)
            results["remove"] = r.status_code

        t1 = threading.Thread(target=demote)
        t2 = threading.Thread(target=remove)
        t1.start()
        t2.start()
        barrier.wait(timeout=30)
        t1.join(timeout=60)
        t2.join(timeout=60)

        self.assertNotIn(500, results.values(), results)
        # One mutation lands; the other must be refused with a 4xx.
        self.assertIn(200, results.values(), results)
        self.assertEqual(self._admin_count(vid), 1, results)

    def test_demote_then_remove_last_admin_second_refused(self):
        vid = self._make_vault_with_admins([2, 3])
        r1 = self.client.patch(
            "/api/vaults/%d/members/2" % vid, json={"permission": "read"}
        )
        self.assertEqual(r1.status_code, 200, r1.text[:200])
        r2 = self.client.delete("/api/vaults/%d/members/3" % vid)
        self.assertEqual(r2.status_code, 409, r2.text[:200])
        self.assertEqual(self._admin_count(vid), 1)


class ThirdReplaceSetFamily(_SiblingBase):
    """PUT /api/groups/{id}/vaults joins the omitted-vs-empty contract."""

    def _make_group(self):
        r = self.client.post("/api/groups", json={"name": "SG", "description": "d"})
        self.assertIn(r.status_code, (200, 201), r.text[:200])
        return r.json()["id"]

    def _make_vault(self):
        conn = self._conn()
        cur = conn.execute("INSERT INTO vaults (name, description) VALUES ('SV3', 'd')")
        vid = cur.lastrowid
        conn.commit()
        self._release(conn)
        return vid

    def _grants_count(self, gid):
        conn = self._conn()
        n = conn.execute(
            "SELECT COUNT(*) FROM vault_group_access WHERE group_id = ?", (gid,)
        ).fetchone()[0]
        self._release(conn)
        return n

    def test_omitted_body_rejected_422(self):
        gid = self._make_group()
        vid = self._make_vault()
        r = self.client.put("/api/groups/%d/vaults" % gid, json={"vault_ids": [vid]})
        self.assertEqual(r.status_code, 200, r.text[:300])
        r = self.client.put("/api/groups/%d/vaults" % gid, json={})
        self.assertEqual(r.status_code, 422, r.text[:300])
        self.assertEqual(self._grants_count(gid), 1)

    def test_null_fields_rejected_422(self):
        gid = self._make_group()
        r = self.client.put(
            "/api/groups/%d/vaults" % gid,
            json={"vault_access": None, "vault_ids": None},
        )
        self.assertEqual(r.status_code, 422, r.text[:300])

    def test_explicit_empty_still_clears(self):
        gid = self._make_group()
        vid = self._make_vault()
        r = self.client.put("/api/groups/%d/vaults" % gid, json={"vault_ids": [vid]})
        self.assertEqual(r.status_code, 200, r.text[:300])
        r = self.client.put("/api/groups/%d/vaults" % gid, json={"vault_ids": []})
        self.assertEqual(r.status_code, 200, r.text[:300])
        self.assertEqual(self._grants_count(gid), 0)


class GroupsPerPageBounds(_SiblingBase):
    def test_negative_and_zero_per_page_rejected(self):
        for bad in (-1, 0):
            r = self.client.get("/api/groups", params={"per_page": bad})
            self.assertEqual(r.status_code, 422, "per_page=%s" % bad)

    def test_upper_contract_preserved(self):
        r = self.client.get("/api/groups", params={"per_page": 1000})
        self.assertEqual(r.status_code, 200, r.text[:200])
        r = self.client.get("/api/groups", params={"per_page": 1001})
        self.assertEqual(r.status_code, 200, r.text[:200])
        body = r.json()
        self.assertLessEqual(body["per_page"], 1000)


class AllowLocalServicesPrecedence(_SiblingBase):
    def _opt_in(self):
        from app.services.ssrf import _local_services_opt_in_enabled

        return _local_services_opt_in_enabled()

    def test_default_deny(self):
        os.environ.pop("ALLOW_LOCAL_SERVICES", None)
        settings.allow_local_services = False
        self.assertFalse(self._opt_in())

    def test_real_env_true_wins_at_call_time(self):
        os.environ["ALLOW_LOCAL_SERVICES"] = "1"
        settings.allow_local_services = False
        self.assertTrue(self._opt_in())

    def test_real_env_false_wins_over_settings(self):
        os.environ["ALLOW_LOCAL_SERVICES"] = "0"
        settings.allow_local_services = True
        self.assertFalse(self._opt_in())

    def test_env_file_field_honoured_when_env_absent(self):
        os.environ.pop("ALLOW_LOCAL_SERVICES", None)
        settings.allow_local_services = True
        self.assertTrue(self._opt_in())


class PostCommitGuardPins(_SiblingBase):
    def test_users_update_racing_delete_answers_404(self):
        """A delete landing right before update_user's post-commit re-read
        must answer 404, not an untyped TypeError 500 (issue #726 census)."""
        real = self.pool.get_connection()
        real.execute(
            "INSERT INTO org_members (org_id, user_id, role) VALUES (1, 2, 'member')"
        )
        real.commit()
        outer = self

        class _ConnProxy:
            def __init__(self, conn):
                self._real = conn

            def __getattr__(self, name):
                return getattr(self._real, name)

            def execute(self, sql, params=()):
                if sql.startswith("SELECT id, username") and "FROM users WHERE id = ?" in sql:
                    self._real.execute(
                        "DELETE FROM users WHERE id = ?", (params[0],)
                    )
                    self._real.commit()
                return self._real.execute(sql, params)

        def override_get_db():
            yield _ConnProxy(real)

        orig_override = app.dependency_overrides[get_db]
        app.dependency_overrides[get_db] = override_get_db
        try:
            r = self.client.patch("/api/users/2", json={"full_name": "Renamed"})
        finally:
            app.dependency_overrides[get_db] = orig_override
            self.pool.release_connection(real)
        self.assertEqual(r.status_code, 404, r.text[:200])

    def test_create_vault_missing_reread_is_fixed_500(self):
        from unittest.mock import patch as mock_patch

        from app.api.routes import vaults as vaults_module

        with mock_patch.object(
            vaults_module, "_fetch_vault_with_counts", return_value=None
        ):
            no_raise = TestClient(app, raise_server_exceptions=False)
            no_raise.headers["user-agent"] = ""
            try:
                r = no_raise.post("/api/vaults", json={"name": "GV", "description": "d"})
            finally:
                no_raise.close()
        self.assertEqual(r.status_code, 500)
        self.assertEqual(r.json().get("detail"), "Failed to create vault")


if __name__ == "__main__":
    unittest.main()
