"""Route-parity regression tests (issues #389 / #406).

These tests guard the trailing-slash/non-slash route parity convention: list
endpoints must register BOTH the slash and non-slash path variants, with the
duplicate hidden from the OpenAPI schema via ``include_in_schema=False``.

Why these tests exist
---------------------
The existing route test suites (``test_organizations_routes.py``,
``test_vault_members_routes.py``, ``test_documents_*.py``) call only the
non-slash variant via ``TestClient`` with the default ``follow_redirects=True``.
Pre-fix, those calls passed via a 307 redirect hop to the trailing-slash path,
so they were structurally blind to the parity gap — they passed both before and
after the fix and cannot detect a regression. These tests close that blind spot
in two complementary ways:

1. **Schema assertion**: each list route contributes exactly ONE entry to the
   OpenAPI ``paths`` dict (the visible variant). A revert that drops
   ``include_in_schema=False`` from the duplicate (F-PRE-002, documents.py) or
   re-introduces a single-decorator route (F-PRE-001, organizations.py /
   vault_members.py) changes the path set and fails the assertion.

2. **Runtime parity assertion**: both the slash and non-slash variants reach
   the handler — i.e. return a status from the handler-ran set
   (2xx / 400 / 409 / 500), not a routing failure (307 / 404), a pre-handler
   auth rejection (401 / 403), or a pre-handler body-validation rejection
   (422). The runtime tests set up a minimal but real DB + auth override so
   the handler body actually executes; pre-fix the non-slash variant
   307-redirected (asserted via ``follow_redirects=False``). A parity revert
   yields 307, which the assertion rejects; handler correctness itself (e.g.
   a 500 from a real bug) is owned by the existing per-route test suites, so
   500 remains an accepted "handler ran" status by design.

The settings response_model guard (F-PRE-004) lives in ``test_rate_limiting.py``
(the limiter half) and in the schema assertion below (the response_model half),
since the runtime response body is identical with or without ``response_model``
(handlers return a pre-validated ``SettingsResponse``).
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

# Stub missing optional dependencies before importing the app (mirrors the
# bootstrap used by test_vaults.py).
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

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient

from app.api.deps import get_current_active_user
from app.config import settings
from app.main import app
from app.models.database import _pool_cache, _pool_cache_lock, init_db


class _RouteParityHarness(unittest.TestCase):
    """Shared setup: a fresh temp DB, a superadmin auth override, and a
    TestClient with follow_redirects=False (so a 307 surfaces as != 200)."""

    @classmethod
    def setUpClass(cls):
        # Close any pools left over from earlier test modules so the new
        # sqlite_path takes effect cleanly.
        with _pool_cache_lock:
            for pool in list(_pool_cache.values()):
                pool.close_all()
            _pool_cache.clear()

    def setUp(self):
        self._temp_dir = tempfile.mkdtemp()
        # settings.sqlite_path is a computed property = data_dir / "app.db",
        # so point data_dir at the temp dir and name the DB app.db.
        self._db_path = str(Path(self._temp_dir) / "app.db")
        init_db(self._db_path)

        # The organizations route reads via get_pool(str(settings.sqlite_path))
        # directly (not via the get_db dependency), so point settings.data_dir
        # at the temp DB and seed a superadmin + an org so list_organizations
        # returns 200 with a handler-ran body.
        self._original_data_dir = settings.data_dir
        settings.data_dir = Path(self._temp_dir)

        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO users (id, username, hashed_password, role, is_active) "
            "VALUES (?, ?, ?, ?, 1)",
            (1, "superadmin", "x", "superadmin"),
        )
        conn.execute(
            "INSERT INTO organizations (name, description, slug, created_by) "
            "VALUES (?, ?, ?, ?)",
            ("ParityOrg", "desc", "parityorg", 1),
        )
        conn.commit()
        conn.close()

        app.dependency_overrides[get_current_active_user] = lambda: {
            "id": 1,
            "username": "superadmin",
            "full_name": "Super Admin",
            "role": "superadmin",
            "is_active": True,
            "must_change_password": False,
        }
        self.client = TestClient(app, follow_redirects=False)

    def tearDown(self):
        app.dependency_overrides.pop(get_current_active_user, None)
        settings.data_dir = self._original_data_dir
        with _pool_cache_lock:
            for path, pool in list(_pool_cache.items()):
                pool.close_all()
            _pool_cache.clear()
        import shutil
        shutil.rmtree(self._temp_dir, ignore_errors=True)

    @classmethod
    def tearDownClass(cls):
        with _pool_cache_lock:
            for pool in list(_pool_cache.values()):
                pool.close_all()
            _pool_cache.clear()


class TestOpenApiPathParity(unittest.TestCase):
    """Each list route contributes exactly one OpenAPI path entry.

    The convention is: the non-slash variant (``""``) is registered with
    ``include_in_schema=False`` so only the trailing-slash variant appears in
    the generated OpenAPI doc.
    """

    @classmethod
    def setUpClass(cls):
        cls.paths = app.openapi()["paths"]

    def test_organizations_list_single_schema_entry(self):
        self.assertIn("/api/organizations/", self.paths)
        self.assertNotIn("/api/organizations", self.paths)

    def test_vault_members_list_single_schema_entry(self):
        self.assertIn("/api/vaults/{vault_id}/members/", self.paths)
        self.assertNotIn("/api/vaults/{vault_id}/members", self.paths)

    def test_vault_group_access_list_single_schema_entry(self):
        self.assertIn("/api/vaults/{vault_id}/group-access/", self.paths)
        self.assertNotIn("/api/vaults/{vault_id}/group-access", self.paths)

    def test_documents_list_get_single_schema_entry(self):
        self.assertIn("/api/documents/", self.paths)
        self.assertNotIn("/api/documents", self.paths)

    def test_documents_list_post_single_schema_entry(self):
        # F-PRE-002 also covers POST /documents (documents.py:1599). The
        # pre-fix defect had BOTH /api/documents and /api/documents/ visible
        # for POST; assert the non-slash variant is hidden for POST too.
        self.assertIn("/api/documents/", self.paths)
        self.assertNotIn("/api/documents", self.paths)
        # The slash path must declare a POST operation (sanity: confirms the
        # POST list route is still in the schema at all).
        self.assertIn("post", self.paths["/api/documents/"])


class TestSettingsResponseModelSchema(unittest.TestCase):
    """F-PRE-004 (response_model half): POST/PUT /settings declare a response
    schema referencing SettingsResponse. The runtime response body is unchanged
    (handlers return a pre-validated SettingsResponse), so only a schema-level
    assertion can detect a revert of the ``response_model`` decorator argument.
    """

    @classmethod
    def setUpClass(cls):
        cls.paths = app.openapi()["paths"]

    def _response_schema_ref(self, op: dict) -> str:
        """Return the $ref string declared in the 200 response schema, or ''.

        Asserts on this (not a substring of str(op)) so the test fails if
        SettingsResponse appears only in the request body or description but
        not in the actual response schema path.
        """
        success = op.get("responses", {}).get("200") or op.get("responses", {}).get("201")
        if not success:
            return ""
        schema = (success.get("content", {}).get("application/json", {}) or {}).get("schema", {})
        return schema.get("$ref", "") or str(schema.get("items", {}).get("$ref", ""))

    def test_settings_post_response_schema_is_settings_response(self):
        op = self.paths["/api/settings"]["post"]
        ref = self._response_schema_ref(op)
        self.assertEqual(
            ref, "#/components/schemas/SettingsResponse",
            f"POST /settings 200 response must reference SettingsResponse, got: {ref}",
        )

    def test_settings_put_response_schema_is_settings_response(self):
        op = self.paths["/api/settings"]["put"]
        ref = self._response_schema_ref(op)
        self.assertEqual(
            ref, "#/components/schemas/SettingsResponse",
            f"PUT /settings 200 response must reference SettingsResponse, got: {ref}",
        )


class TestRuntimeRouteParity(_RouteParityHarness):
    """Both slash and non-slash variants reach the handler body.

    Uses ``follow_redirects=False`` so a 307 surfaces as a failure. The setUp
    wires a real temp DB + superadmin auth override so the handler body
    actually executes. The assertion rejects the routing/auth/validation
    failure statuses (307/404/401/403/422) and accepts the handler-ran set
    (2xx/400/409/500). A parity regression (one variant missing) yields 307
    on the missing variant, which fails the assertion; handler correctness
    (including a real 500) is owned by the per-route test suites, so 500
    remains accepted here by design.
    """

    def _assert_handler_ran(self, resp, method, path):
        """The response must indicate the handler body executed — not a
        routing failure (307/404), not a pre-handler auth rejection
        (401/403, which would mean auth deps ran but the handler did not),
        and not a pre-handler body-validation rejection (422, which FastAPI
        raises before invoking the handler function). Acceptable statuses
        are therefore 2xx (success), 400/409 (handler-raised domain errors),
        or 500 (handler-raised exception). For route-parity regression
        detection, the key signal is that BOTH slash variants reach the same
        point in the pipeline (no 307 redirect difference)."""
        rejected_codes = (307, 404, 401, 403, 422)
        self.assertNotIn(
            resp.status_code, rejected_codes,
            f"{method} {path} did not reach the handler body "
            f"(routing/auth/validation rejected it): {resp.status_code}",
        )

    # ---- GET parity ----

    def test_get_organizations_non_slash_runs_handler(self):
        # F-PRE-001: pre-fix this returned 307 (redirect_slashes=True default).
        r = self.client.get("/api/organizations")
        self._assert_handler_ran(r, "GET", "/api/organizations")

    def test_get_organizations_slash_and_non_slash_both_run_handler(self):
        a = self.client.get("/api/organizations")
        b = self.client.get("/api/organizations/")
        self._assert_handler_ran(a, "GET", "/api/organizations")
        self._assert_handler_ran(b, "GET", "/api/organizations/")
        self.assertEqual(
            a.status_code, b.status_code,
            f"parity broken: no-slash={a.status_code} slash={b.status_code}",
        )

    def test_get_vault_members_non_slash_runs_handler(self):
        # Seed a vault + member so list_vault_members can return 200.
        self._seed_vault_and_member(vault_id=1)
        r = self.client.get("/api/vaults/1/members")
        self._assert_handler_ran(r, "GET", "/api/vaults/1/members")

    def test_get_vault_members_slash_and_non_slash_both_run_handler(self):
        self._seed_vault_and_member(vault_id=1)
        a = self.client.get("/api/vaults/1/members")
        b = self.client.get("/api/vaults/1/members/")
        self._assert_handler_ran(a, "GET", "/api/vaults/1/members")
        self._assert_handler_ran(b, "GET", "/api/vaults/1/members/")
        self.assertEqual(a.status_code, b.status_code)

    def test_get_vault_group_access_non_slash_runs_handler(self):
        # F-PRE-001 (convention-driven parity, same file as vault_members).
        self._seed_vault_and_member(vault_id=1)
        r = self.client.get("/api/vaults/1/group-access")
        self._assert_handler_ran(r, "GET", "/api/vaults/1/group-access")

    def test_get_vault_group_access_slash_and_non_slash_both_run_handler(self):
        self._seed_vault_and_member(vault_id=1)
        a = self.client.get("/api/vaults/1/group-access")
        b = self.client.get("/api/vaults/1/group-access/")
        self._assert_handler_ran(a, "GET", "/api/vaults/1/group-access")
        self._assert_handler_ran(b, "GET", "/api/vaults/1/group-access/")
        self.assertEqual(a.status_code, b.status_code)

    # ---- POST parity (F-PRE-001 also covers POST list routes) ----

    def test_post_organizations_non_slash_runs_handler(self):
        # create_organization requires admin + CSRF. Override csrf_protect so
        # the handler body runs without a real CSRF token.
        self._override_csrf()
        r = self.client.post(
            "/api/organizations",
            json={"name": "ParityPostOrg", "description": "d"},
        )
        self._assert_handler_ran(r, "POST", "/api/organizations")

    def test_post_organizations_slash_and_non_slash_both_run_handler(self):
        self._override_csrf()
        body = {"name": "ParityPostOrg2", "description": "d"}
        a = self.client.post("/api/organizations", json=body)
        b = self.client.post("/api/organizations/", json={"name": "ParityPostOrg3", "description": "d"})
        self._assert_handler_ran(a, "POST", "/api/organizations")
        self._assert_handler_ran(b, "POST", "/api/organizations/")
        self.assertEqual(a.status_code, b.status_code)

    def test_post_vault_members_non_slash_runs_handler(self):
        # add_vault_member requires vault admin permission + CSRF. Seed a vault
        # owned by the superadmin so the permission check passes.
        self._seed_vault_and_member(vault_id=1, owner_id=1)
        self._override_csrf()
        # Need a second user to add as a member.
        self._seed_user(user_id=2, username="member2", role="member")
        r = self.client.post(
            "/api/vaults/1/members",
            json={"member_user_id": 2, "permission": "read"},
        )
        self._assert_handler_ran(r, "POST", "/api/vaults/1/members")

    def test_post_vault_members_slash_and_non_slash_both_run_handler(self):
        self._seed_vault_and_member(vault_id=1, owner_id=1)
        self._seed_user(user_id=2, username="m_slash", role="member")
        self._seed_user(user_id=3, username="m_noslash", role="member")
        self._override_csrf()
        a = self.client.post("/api/vaults/1/members", json={"member_user_id": 2, "permission": "read"})
        b = self.client.post("/api/vaults/1/members/", json={"member_user_id": 3, "permission": "read"})
        self._assert_handler_ran(a, "POST", "/api/vaults/1/members")
        self._assert_handler_ran(b, "POST", "/api/vaults/1/members/")
        self.assertEqual(a.status_code, b.status_code)

    def test_post_vault_group_access_non_slash_runs_handler(self):
        # grant_vault_group_access requires vault admin + CSRF. Seed vault +
        # group so the handler runs.
        self._seed_vault_and_member(vault_id=1, owner_id=1)
        self._seed_group(group_id=1)
        self._override_csrf()
        r = self.client.post(
            "/api/vaults/1/group-access",
            json={"group_id": 1, "permission": "read"},
        )
        self._assert_handler_ran(r, "POST", "/api/vaults/1/group-access")

    def test_post_vault_group_access_slash_and_non_slash_both_run_handler(self):
        self._seed_vault_and_member(vault_id=1, owner_id=1)
        self._seed_group(group_id=1)
        self._seed_group(group_id=2)
        self._override_csrf()
        a = self.client.post("/api/vaults/1/group-access", json={"group_id": 1, "permission": "read"})
        b = self.client.post("/api/vaults/1/group-access/", json={"group_id": 2, "permission": "read"})
        self._assert_handler_ran(a, "POST", "/api/vaults/1/group-access")
        self._assert_handler_ran(b, "POST", "/api/vaults/1/group-access/")
        self.assertEqual(a.status_code, b.status_code)

    # ---- seed helpers ----

    def _seed_user(self, user_id, username, role="member"):
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT OR IGNORE INTO users (id, username, hashed_password, role, is_active) "
            "VALUES (?, ?, ?, ?, 1)",
            (user_id, username, "x", role),
        )
        conn.commit()
        conn.close()

    def _seed_vault_and_member(self, vault_id, owner_id=1):
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT OR IGNORE INTO vaults (id, name, description, org_id, visibility, owner_id) "
            "VALUES (?, ?, ?, NULL, 'private', ?)",
            (vault_id, f"Vault{vault_id}", "d", owner_id),
        )
        conn.execute(
            "INSERT OR IGNORE INTO vault_members (vault_id, user_id, permission) "
            "VALUES (?, ?, 'admin')",
            (vault_id, owner_id),
        )
        conn.commit()
        conn.close()

    def _seed_group(self, group_id):
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        # groups.org_id is NOT NULL with FK to organizations; the org seeded
        # in setUp has id=1.
        conn.execute(
            "INSERT OR IGNORE INTO groups (id, org_id, name, description) "
            "VALUES (?, 1, ?, ?)",
            (group_id, f"Group{group_id}", "d"),
        )
        conn.commit()
        conn.close()

    def _override_csrf(self):
        from app.security import csrf_protect

        app.dependency_overrides[csrf_protect] = lambda: "test-csrf-token"
        self.addCleanup(app.dependency_overrides.pop, csrf_protect, None)


if __name__ == "__main__":
    unittest.main()
