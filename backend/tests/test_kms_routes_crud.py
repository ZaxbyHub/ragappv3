"""Route coverage for the KMS endpoints (DD-C008).

Complements test_kms_routes.py (which covers the 503 master switch, blank-slug
422, and CSRF 403) with the previously-untested surface:

- Entry CRUD happy paths (create / list / get / update / delete)
- Search endpoint
- Compile + recompile job creation (202) and job listing/get
- Authentication (401 unauthenticated) and authorization (403 wrong vault)

Reuses KMSFixTestBase from test_kms_routes (CSRF is bypassed there; CSRF is
exercised separately in test_kms_routes.py).
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from test_kms_routes import KMSFixTestBase

from app.services.kms_store import KMSStore

pytestmark = pytest.mark.usefixtures("ready_vector_store")


class KMSCrudBase(KMSFixTestBase):
    """Adds a no-access vault (id=3) and an indexed file in the write vault."""

    def setUp(self):
        super().setUp()
        conn = self._connection_pool.get_connection()
        try:
            # Vault 3 exists but member1 has no membership -> 403 on access.
            conn.execute(
                "INSERT OR IGNORE INTO vaults (id, name, description) VALUES (3,'No Access','x')"
            )
            # An indexed file in the writable vault (2) for compile tests.
            conn.execute(
                "INSERT OR IGNORE INTO files (id, vault_id, file_path, file_name, "
                "file_size, status) VALUES (500, 2, '/tmp/f.txt', 'f.txt', 10, 'indexed')"
            )
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)

    def _create_entry(self, **overrides):
        payload = {"vault_id": 2, "title": "Hello", "body": "world body"}
        payload.update(overrides)
        return self.client.post(
            "/api/kms/entries", json=payload, headers=self._write_headers()
        )


class TestKMSEntryCrud(KMSCrudBase):
    def test_create_entry_returns_201_with_fields(self):
        resp = self._create_entry(title="Runbook", tags=["ops", "oncall"])
        self.assertEqual(resp.status_code, 201)
        data = resp.json()
        self.assertEqual(data["title"], "Runbook")
        self.assertEqual(data["vault_id"], 2)
        self.assertEqual(data["source_type"], "manual")
        self.assertIn("id", data)
        self.assertEqual(sorted(data["tags"]), ["oncall", "ops"])

    def test_list_entries_includes_created(self):
        self._create_entry(title="Listed Entry")
        resp = self.client.get(
            "/api/kms/entries?vault_id=2", headers=self._write_headers()
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertGreaterEqual(data["total"], 1)
        titles = [e["title"] for e in data["entries"]]
        self.assertIn("Listed Entry", titles)

    def test_get_entry_returns_entry_and_404_for_missing(self):
        created = self._create_entry(title="Fetch Me").json()
        ok = self.client.get(
            f"/api/kms/entries/{created['id']}", headers=self._write_headers()
        )
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.json()["title"], "Fetch Me")

        missing = self.client.get(
            "/api/kms/entries/999999", headers=self._write_headers()
        )
        self.assertEqual(missing.status_code, 404)

    def test_update_entry_changes_fields(self):
        created = self._create_entry(title="Before").json()
        resp = self.client.put(
            f"/api/kms/entries/{created['id']}",
            json={"title": "After", "status": "published"},
            headers=self._write_headers(),
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["title"], "After")
        self.assertEqual(data["status"], "published")

    def test_delete_entry_returns_204_then_404(self):
        created = self._create_entry(title="Delete Me").json()
        resp = self.client.delete(
            f"/api/kms/entries/{created['id']}", headers=self._write_headers()
        )
        self.assertEqual(resp.status_code, 204)
        after = self.client.get(
            f"/api/kms/entries/{created['id']}", headers=self._write_headers()
        )
        self.assertEqual(after.status_code, 404)


class TestKMSSearch(KMSCrudBase):
    def test_search_matches_entry_body(self):
        self._create_entry(title="Searchable", body="pineapple deployment guide")
        resp = self.client.get(
            "/api/kms/search?vault_id=2&q=pineapple", headers=self._write_headers()
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["query"], "pineapple")
        titles = [e["title"] for e in data["entries"]]
        self.assertIn("Searchable", titles)


class TestKMSCompileJobs(KMSCrudBase):
    def test_compile_document_creates_job(self):
        resp = self.client.post(
            "/api/kms/documents/500/compile?vault_id=2",
            headers=self._write_headers(),
        )
        self.assertEqual(resp.status_code, 202)
        data = resp.json()
        self.assertIn("job_id", data)
        self.assertEqual(data["status"], "pending")

        # The job shows up in the vault's job list and is individually fetchable.
        jobs = self.client.get(
            "/api/kms/jobs?vault_id=2", headers=self._write_headers()
        ).json()["jobs"]
        self.assertTrue(any(j["id"] == data["job_id"] for j in jobs))

        one = self.client.get(
            f"/api/kms/jobs/{data['job_id']}?vault_id=2",
            headers=self._write_headers(),
        )
        self.assertEqual(one.status_code, 200)
        self.assertEqual(one.json()["trigger_type"], "ingest")

    def test_compile_missing_file_returns_404(self):
        resp = self.client.post(
            "/api/kms/documents/999999/compile?vault_id=2",
            headers=self._write_headers(),
        )
        self.assertEqual(resp.status_code, 404)

    def test_recompile_vault_creates_job(self):
        resp = self.client.post(
            "/api/kms/recompile?vault_id=2", headers=self._write_headers()
        )
        self.assertEqual(resp.status_code, 202)
        self.assertEqual(resp.json()["status"], "pending")

    def test_get_missing_job_returns_404(self):
        resp = self.client.get(
            "/api/kms/jobs/999999?vault_id=2", headers=self._write_headers()
        )
        self.assertEqual(resp.status_code, 404)


class TestKMSAuthentication(KMSCrudBase):
    def test_endpoints_require_authentication(self):
        # No Authorization header -> 401 across representative endpoints.
        self.assertEqual(self.client.get("/api/kms/entries?vault_id=2").status_code, 401)
        self.assertEqual(
            self.client.post("/api/kms/entries", json={"vault_id": 2, "title": "x"}).status_code,
            401,
        )
        self.assertEqual(
            self.client.get("/api/kms/search?vault_id=2&q=a").status_code, 401
        )
        self.assertEqual(self.client.get("/api/kms/jobs?vault_id=2").status_code, 401)


class TestKMSAuthorization(KMSCrudBase):
    def test_list_create_forbidden_on_inaccessible_vault(self):
        # member1 has no access to vault 3.
        self.assertEqual(
            self.client.get(
                "/api/kms/entries?vault_id=3", headers=self._write_headers()
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                "/api/kms/entries",
                json={"vault_id": 3, "title": "Nope"},
                headers=self._write_headers(),
            ).status_code,
            403,
        )

    def test_get_update_delete_forbidden_for_other_vault_entry(self):
        # Seed an entry directly in vault 3, then member1 must be denied.
        conn = self._connection_pool.get_connection()
        try:
            entry = KMSStore(conn).create_entry(
                vault_id=3, title="Secret", body="b", source_type="manual"
            )
        finally:
            self._connection_pool.release_connection(conn)

        self.assertEqual(
            self.client.get(
                f"/api/kms/entries/{entry.id}", headers=self._write_headers()
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.put(
                f"/api/kms/entries/{entry.id}",
                json={"title": "hack"},
                headers=self._write_headers(),
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.delete(
                f"/api/kms/entries/{entry.id}", headers=self._write_headers()
            ).status_code,
            403,
        )

    def test_jobs_forbidden_on_inaccessible_vault(self):
        self.assertEqual(
            self.client.get(
                "/api/kms/jobs?vault_id=3", headers=self._write_headers()
            ).status_code,
            403,
        )
