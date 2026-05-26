"""Route coverage for the folder hierarchy endpoints.

Covers CRUD, nesting, name-uniqueness, cycle prevention, document moves,
vault-scoped cascade/unfile behavior, the /documents folder_id filter, and
auth (401 unauthenticated, 403 wrong vault, CSRF 403). Reuses KMSFixTestBase
for the users/vault/pool fixture (member1 has write on vault 2).
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from test_kms_routes import KMSFixTestBase

from app.main import app
from app.security import csrf_protect


class FoldersTestBase(KMSFixTestBase):
    def setUp(self):
        super().setUp()
        conn = self._connection_pool.get_connection()
        try:
            # Vault 3: member1 has no membership (403 cases).
            conn.execute(
                "INSERT OR IGNORE INTO vaults (id, name, description) VALUES (3,'No Access','x')"
            )
            # Two indexed files in the writable vault for move/filter tests.
            for fid in (600, 601):
                conn.execute(
                    "INSERT OR IGNORE INTO files (id, vault_id, file_path, file_name, "
                    "file_size, status) VALUES (?, 2, ?, ?, 10, 'indexed')",
                    (fid, f"/tmp/f{fid}.txt", f"f{fid}.txt"),
                )
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)

    def _create(self, name, parent=None, vault_id=2):
        body = {"vault_id": vault_id, "name": name}
        if parent is not None:
            body["parent_folder_id"] = parent
        return self.client.post(
            "/api/folders", json=body, headers=self._write_headers()
        )


class TestFolderCrud(FoldersTestBase):
    def test_create_root_folder(self):
        resp = self._create("Reports")
        self.assertEqual(resp.status_code, 201)
        data = resp.json()
        self.assertEqual(data["name"], "Reports")
        self.assertEqual(data["vault_id"], 2)
        self.assertIsNone(data["parent_folder_id"])
        self.assertEqual(data["document_count"], 0)

    def test_create_subfolder(self):
        parent = self._create("Parent").json()
        child = self._create("Child", parent=parent["id"])
        self.assertEqual(child.status_code, 201)
        self.assertEqual(child.json()["parent_folder_id"], parent["id"])

    def test_duplicate_name_in_same_parent_returns_409(self):
        self._create("Dup")
        again = self._create("Dup")
        self.assertEqual(again.status_code, 409)

    def test_same_name_in_different_parents_allowed(self):
        a = self._create("A").json()
        b = self._create("B").json()
        self.assertEqual(self._create("Shared", parent=a["id"]).status_code, 201)
        self.assertEqual(self._create("Shared", parent=b["id"]).status_code, 201)

    def test_list_folders(self):
        self._create("L1")
        resp = self.client.get("/api/folders?vault_id=2", headers=self._write_headers())
        self.assertEqual(resp.status_code, 200)
        names = [f["name"] for f in resp.json()["folders"]]
        self.assertIn("L1", names)

    def test_rename_folder(self):
        f = self._create("Old").json()
        resp = self.client.put(
            f"/api/folders/{f['id']}",
            json={"name": "New"},
            headers=self._write_headers(),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["name"], "New")

    def test_create_with_missing_parent_returns_404(self):
        resp = self._create("Orphan", parent=999999)
        self.assertEqual(resp.status_code, 404)

    def test_rename_to_existing_sibling_returns_409(self):
        # F-002: PUT /folders/{id} must 409 when the new name collides with an
        # existing sibling at the same level.
        self._create("Alpha")
        beta = self._create("Beta").json()
        resp = self.client.put(
            f"/api/folders/{beta['id']}",
            json={"name": "Alpha"},
            headers=self._write_headers(),
        )
        self.assertEqual(resp.status_code, 409)


class TestFolderReparentAndCycles(FoldersTestBase):
    def test_reparent_to_root(self):
        parent = self._create("P").json()
        child = self._create("C", parent=parent["id"]).json()
        resp = self.client.put(
            f"/api/folders/{child['id']}",
            json={"parent_folder_id": None},
            headers=self._write_headers(),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.json()["parent_folder_id"])

    def test_self_parent_rejected(self):
        f = self._create("Self").json()
        resp = self.client.put(
            f"/api/folders/{f['id']}",
            json={"parent_folder_id": f["id"]},
            headers=self._write_headers(),
        )
        self.assertEqual(resp.status_code, 409)

    def test_descendant_cycle_rejected(self):
        a = self._create("A").json()
        b = self._create("B", parent=a["id"]).json()
        # Moving A under its own child B would create a cycle.
        resp = self.client.put(
            f"/api/folders/{a['id']}",
            json={"parent_folder_id": b["id"]},
            headers=self._write_headers(),
        )
        self.assertEqual(resp.status_code, 409)

    def test_deep_descendant_cycle_rejected(self):
        # F-003: Three-node chain A → B → C; moving A under C (grandchild)
        # must also 409 — the BFS in _descendant_ids must traverse all depths.
        a = self._create("A").json()
        b = self._create("B", parent=a["id"]).json()
        c = self._create("C", parent=b["id"]).json()
        resp = self.client.put(
            f"/api/folders/{a['id']}",
            json={"parent_folder_id": c["id"]},
            headers=self._write_headers(),
        )
        self.assertEqual(resp.status_code, 409)


class TestFolderDeleteCascade(FoldersTestBase):
    def test_delete_cascades_subfolders_and_unfiles_documents(self):
        parent = self._create("Top").json()
        child = self._create("Sub", parent=parent["id"]).json()
        # File 600 lives in the child folder.
        self.client.post(
            "/api/folders/move",
            json={"vault_id": 2, "file_ids": [600], "folder_id": child["id"]},
            headers=self._write_headers(),
        )
        resp = self.client.delete(
            f"/api/folders/{parent['id']}", headers=self._write_headers()
        )
        self.assertEqual(resp.status_code, 204)

        # Both folders are gone, and the file is unfiled (not deleted).
        folders = self.client.get(
            "/api/folders?vault_id=2", headers=self._write_headers()
        ).json()["folders"]
        self.assertEqual(folders, [])

        conn = self._connection_pool.get_connection()
        try:
            row = conn.execute(
                "SELECT folder_id FROM files WHERE id = 600"
            ).fetchone()
        finally:
            self._connection_pool.release_connection(conn)
        self.assertIsNotNone(row)
        self.assertIsNone(row["folder_id"])


class TestFolderMoveAndFilter(FoldersTestBase):
    def test_move_documents_into_folder_and_filter(self):
        folder = self._create("Filed").json()
        move = self.client.post(
            "/api/folders/move",
            json={"vault_id": 2, "file_ids": [600, 601], "folder_id": folder["id"]},
            headers=self._write_headers(),
        )
        self.assertEqual(move.status_code, 200)
        self.assertEqual(move.json()["moved"], 2)

        # The /documents folder_id filter returns only the filed documents.
        listing = self.client.get(
            f"/api/documents?vault_id=2&folder_id={folder['id']}",
            headers=self._write_headers(),
        )
        self.assertEqual(listing.status_code, 200)
        ids = sorted(d["id"] for d in listing.json()["documents"])
        self.assertEqual(ids, [600, 601])
        for doc in listing.json()["documents"]:
            self.assertEqual(doc["folder_id"], folder["id"])

    def test_move_to_root_unfiles(self):
        folder = self._create("Tmp").json()
        self.client.post(
            "/api/folders/move",
            json={"vault_id": 2, "file_ids": [600], "folder_id": folder["id"]},
            headers=self._write_headers(),
        )
        resp = self.client.post(
            "/api/folders/move",
            json={"vault_id": 2, "file_ids": [600], "folder_id": None},
            headers=self._write_headers(),
        )
        self.assertEqual(resp.status_code, 200)
        conn = self._connection_pool.get_connection()
        try:
            row = conn.execute("SELECT folder_id FROM files WHERE id = 600").fetchone()
        finally:
            self._connection_pool.release_connection(conn)
        self.assertIsNone(row["folder_id"])

    def test_move_into_other_vault_folder_returns_404(self):
        # A folder that belongs to vault 3 cannot be a move target for vault 2.
        conn = self._connection_pool.get_connection()
        try:
            cur = conn.execute(
                "INSERT INTO folders (vault_id, name) VALUES (3, 'Foreign')"
            )
            foreign_id = cur.lastrowid
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)
        resp = self.client.post(
            "/api/folders/move",
            json={"vault_id": 2, "file_ids": [600], "folder_id": foreign_id},
            headers=self._write_headers(),
        )
        self.assertEqual(resp.status_code, 404)


class TestFolderAuth(FoldersTestBase):
    def test_unauthenticated_returns_401(self):
        self.assertEqual(self.client.get("/api/folders?vault_id=2").status_code, 401)
        self.assertEqual(
            self.client.post("/api/folders", json={"vault_id": 2, "name": "x"}).status_code,
            401,
        )

    def test_forbidden_on_inaccessible_vault(self):
        self.assertEqual(
            self.client.get(
                "/api/folders?vault_id=3", headers=self._write_headers()
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                "/api/folders",
                json={"vault_id": 3, "name": "Nope"},
                headers=self._write_headers(),
            ).status_code,
            403,
        )

    def test_create_without_csrf_returns_403(self):
        # Remove the CSRF bypass installed by the base fixture and install a
        # CSRF manager so csrf_protect reaches the token check (403, not 503).
        # Restore prior global state afterward to avoid leaking into other files.
        prev_manager = getattr(app.state, "csrf_manager", None)
        app.dependency_overrides.pop(csrf_protect, None)

        class MockCSRFManager:
            def validate_token(self, token):
                return True

        app.state.csrf_manager = MockCSRFManager()
        try:
            resp = self.client.post(
                "/api/folders",
                json={"vault_id": 2, "name": "NoCsrf"},
                headers=self._write_headers(),
                # No X-CSRF-Token header and no CSRF cookie.
            )
            self.assertEqual(resp.status_code, 403)
        finally:
            app.dependency_overrides[csrf_protect] = lambda: "test-csrf"
            if prev_manager is None:
                if hasattr(app.state, "csrf_manager"):
                    delattr(app.state, "csrf_manager")
            else:
                app.state.csrf_manager = prev_manager
