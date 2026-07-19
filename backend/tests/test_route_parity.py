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
   the handler (neither returns 307 nor 404) using ``follow_redirects=False``
   so a redirect cannot masquerade as success.

The settings response_model guard (F-PRE-004) lives in
``test_rate_limiting.py`` (the limiter half) and in the schema assertion below
(the response_model half), since the runtime response body is identical with or
without ``response_model`` (handlers return a pre-validated ``SettingsResponse``).
"""

import os
import sys
import unittest

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

from app.main import app


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
        # F-PRE-001: pre-fix, only "/api/organizations/" existed and
        # "/api/organizations" 307-redirected. Post-fix, the non-slash variant
        # is registered and hidden, so the slash variant is the sole entry.
        self.assertIn("/api/organizations/", self.paths)
        self.assertNotIn("/api/organizations", self.paths)

    def test_vault_members_list_single_schema_entry(self):
        # F-PRE-001: same pattern for /vaults/{vault_id}/members.
        self.assertIn("/api/vaults/{vault_id}/members/", self.paths)
        self.assertNotIn("/api/vaults/{vault_id}/members", self.paths)

    def test_vault_group_access_list_single_schema_entry(self):
        # F-PRE-001 (convention-driven parity, same file): group-access router.
        self.assertIn("/api/vaults/{vault_id}/group-access/", self.paths)
        self.assertNotIn("/api/vaults/{vault_id}/group-access", self.paths)

    def test_documents_list_get_single_schema_entry(self):
        # F-PRE-002: pre-fix, BOTH "/api/documents" and "/api/documents/"
        # appeared (neither duplicate was hidden). Post-fix, only the slash
        # variant appears.
        self.assertIn("/api/documents/", self.paths)
        self.assertNotIn("/api/documents", self.paths)


class TestSettingsResponseModelSchema(unittest.TestCase):
    """F-PRE-004 (response_model half): POST/PUT /settings declare a response
    schema. The runtime response body is unchanged (handlers return a
    pre-validated SettingsResponse), so only a schema-level assertion can
    detect a revert of the ``response_model`` decorator argument."""

    @classmethod
    def setUpClass(cls):
        cls.paths = app.openapi()["paths"]

    def test_settings_post_references_settings_response(self):
        op = self.paths["/api/settings"]["post"]
        self.assertIn(
            "SettingsResponse",
            str(op),
            "POST /settings OpenAPI entry must reference SettingsResponse",
        )

    def test_settings_put_references_settings_response(self):
        op = self.paths["/api/settings"]["put"]
        self.assertIn(
            "SettingsResponse",
            str(op),
            "PUT /settings OpenAPI entry must reference SettingsResponse",
        )


class TestRuntimeRouteParity(unittest.TestCase):
    """Both slash and non-slash variants reach the handler (no 307/404).

    Uses ``follow_redirects=False`` so a redirect cannot masquerade as success.
    The handler body is not exercised (no DB setup) — any status other than
    307/404 proves the route matched.
    """

    def setUp(self):
        self.client = TestClient(app, follow_redirects=False)

    def test_non_slash_organizations_reaches_handler(self):
        # F-PRE-001: pre-fix this returned 307 (redirect_slashes=True default).
        r = self.client.get("/api/organizations")
        self.assertNotIn(
            r.status_code,
            (307, 404),
            f"non-slash /api/organizations did not reach handler: {r.status_code}",
        )

    def test_slash_and_non_slash_organizations_match(self):
        a = self.client.get("/api/organizations")
        b = self.client.get("/api/organizations/")
        self.assertEqual(
            a.status_code,
            b.status_code,
            f"parity broken: no-slash={a.status_code} slash={b.status_code}",
        )

    def test_non_slash_vault_members_reaches_handler(self):
        # F-PRE-001: pre-fix the non-slash variant 307-redirected. The path
        # requires a vault_id but routing parity is independent of path-param
        # validity, so any non-307/404 status proves the route matched.
        r = self.client.get("/api/vaults/1/members")
        self.assertNotIn(
            r.status_code,
            (307, 404),
            f"non-slash /api/vaults/1/members did not reach handler: {r.status_code}",
        )

    def test_slash_and_non_slash_vault_members_match(self):
        a = self.client.get("/api/vaults/1/members")
        b = self.client.get("/api/vaults/1/members/")
        self.assertEqual(
            a.status_code,
            b.status_code,
            f"parity broken: no-slash={a.status_code} slash={b.status_code}",
        )

    def test_non_slash_vault_group_access_reaches_handler(self):
        # F-PRE-001 (convention-driven parity, same file as vault_members).
        # Without a runtime check, the schema-only test above passes coincidentally
        # both pre- and post-fix; this runtime assertion is the real guard.
        r = self.client.get("/api/vaults/1/group-access")
        self.assertNotIn(
            r.status_code,
            (307, 404),
            f"non-slash /api/vaults/1/group-access did not reach handler: {r.status_code}",
        )

    def test_slash_and_non_slash_vault_group_access_match(self):
        a = self.client.get("/api/vaults/1/group-access")
        b = self.client.get("/api/vaults/1/group-access/")
        self.assertEqual(
            a.status_code,
            b.status_code,
            f"parity broken: no-slash={a.status_code} slash={b.status_code}",
        )


if __name__ == "__main__":
    unittest.main()
