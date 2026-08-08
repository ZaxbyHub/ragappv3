"""Tests for the opaque artifact asset endpoint (issue #462).

GET /api/documents/artifacts/{id}/raw serves a confined raster asset by opaque
atom id. These tests assert the safe-contract properties: authz-before-byte-open,
confined-path containment, allowlist MIME, no path/base64 on the wire, opaque 404
(non-disclosing), and safe response headers.
"""

import base64
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import (
    get_current_user_or_service_account,
    get_db,
    get_evaluate_policy,
    get_settings,
)
from app.api.routes import documents
from app.config import settings
from app.models.database import init_db
from app.services.artifact_store import compute_asset_rel_path

# A 1x1 transparent PNG.
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class TestArtifactRawEndpoint(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "test.db")
        init_db(self.db_path)
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.vault_root = Path(self.tmp) / "vault_root" / "artifacts"
        self.orig_asset_bytes = settings.multimodal_max_asset_bytes

        # Seed a user, vault, file, atom + asset.
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO users (username, hashed_password, full_name, role, is_active, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("owner", "pw", "Owner", "admin", 1, "2026-01-01"),
        )
        self.user_id = cur.lastrowid
        cur.execute(
            "INSERT INTO vaults (name, visibility, created_at, updated_at) "
            "VALUES (?, ?, ?, ?)",
            ("Vault", "private", "2026-01-01", "2026-01-01"),
        )
        self.vault_id = cur.lastrowid
        cur.execute(
            "INSERT INTO files (vault_id, file_path, file_name, file_size, status, source) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (self.vault_id, "/uploads/doc.pdf", "doc.pdf", 1234, "indexed", "upload"),
        )
        self.file_id = cur.lastrowid
        self.atom_id = "atom-abc"
        self.asset_id = "asset-xyz"
        self.gen_hash = "g" * 12
        cur.execute(
            "INSERT INTO document_atoms (atom_id, schema_version, file_id, generation_hash,"
            " ordinal, kind, raw_text, page_number, asset_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (self.atom_id, 1, self.file_id, self.gen_hash, 0, "image", "FIG 1",
             1, self.asset_id),
        )
        self.sha256 = __import__("hashlib").sha256(PNG_BYTES).hexdigest()
        rel = compute_asset_rel_path(self.file_id, self.gen_hash, self.asset_id)
        self.rel = rel
        cur.execute(
            "INSERT INTO document_assets (asset_id, file_id, generation_hash, sha256,"
            " rel_path, mime_type, width, height, byte_size) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (self.asset_id, self.file_id, self.gen_hash, self.sha256, rel,
             "image/png", 1, 1, len(PNG_BYTES)),
        )
        self.conn.commit()

        # Write the real asset file beneath the confined vault root.
        abs_path = self.vault_root / rel
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_bytes(PNG_BYTES)

        self.app = FastAPI()
        self.app.include_router(documents.router)

        def _get_db():
            try:
                yield self.conn
            finally:
                pass

        async def _evaluate(user, resource_type, resource_id, action):
            return True

        async def _user():
            return {"id": self.user_id, "role": "admin", "username": "owner"}

        self.app.dependency_overrides[get_db] = _get_db
        self.app.dependency_overrides[get_current_user_or_service_account] = _user
        self.app.dependency_overrides[get_evaluate_policy] = lambda: _evaluate
        self.app.dependency_overrides[get_settings] = lambda: settings
        self.client = TestClient(self.app)

    def tearDown(self):
        settings.multimodal_max_asset_bytes = self.orig_asset_bytes
        self.conn.close()
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _patch_root(self):
        return patch("app.services.artifact_store.artifact_root", return_value=self.vault_root)

    def test_serves_raster_asset(self):
        with self._patch_root():
            r = self.client.get(f"/documents/artifacts/{self.atom_id}/raw")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["content-type"], "image/png")
        self.assertEqual(r.headers["x-content-type-options"], "nosniff")
        self.assertEqual(r.headers["cache-control"], "private, no-store")
        self.assertEqual(r.content, PNG_BYTES)

    def test_authz_denied_returns_forbidden(self):
        async def _deny(user, resource_type, resource_id, action):
            return False

        self.app.dependency_overrides[get_evaluate_policy] = lambda: _deny
        with self._patch_root():
            r = self.client.get(f"/documents/artifacts/{self.atom_id}/raw")
        self.assertEqual(r.status_code, 403)

    def test_unknown_id_is_nondisclosing_404(self):
        with self._patch_root():
            r = self.client.get("/documents/artifacts/does-not-exist/raw")
        self.assertEqual(r.status_code, 404)
        # No storage path or artifact id leaks in the body.
        self.assertNotIn("does-not-exist", r.text)

    def test_traversal_rel_path_rejected(self):
        cur = self.conn.cursor()
        cur.execute(
            "UPDATE document_assets SET rel_path = ? WHERE asset_id = ?",
            ("../../etc/passwd", self.asset_id),
        )
        self.conn.commit()
        with self._patch_root():
            r = self.client.get(f"/documents/artifacts/{self.atom_id}/raw")
        self.assertEqual(r.status_code, 404)

    def test_wrong_parent_file_rejected(self):
        # Asset claim belongs to a different parent file than the atom.
        cur = self.conn.cursor()
        cur.execute(
            "UPDATE document_assets SET file_id = file_id + 999999 WHERE asset_id = ?",
            (self.asset_id,),
        )
        self.conn.commit()
        with self._patch_root():
            r = self.client.get(f"/documents/artifacts/{self.atom_id}/raw")
        self.assertEqual(r.status_code, 404)

    def test_asset_file_missing_is_nondisclosing_404(self):
        (self.vault_root / self.rel).unlink()
        with self._patch_root():
            r = self.client.get(f"/documents/artifacts/{self.atom_id}/raw")
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
