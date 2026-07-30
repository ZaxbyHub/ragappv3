"""Security/authorization tests for Draft Room (issue #435, SPEC section 9.1/9.2).

Covers: non-owner isolation (404, never 403 — existence must not leak across
owners), cross-vault/cross-draft ID substitution, the post-revocation
allow-list (list/cancel/delete stay available, content/edit/upload/export do
not), CSRF enforcement on every mutation, and the ``draft_room_enabled=False``
gate (create/edit/upload/revision-create 503 while capabilities/list/export/
cancel/delete keep working).

Harness mirrors ``test_draft_routes.py`` (itself copied from
``test_tags_routes.py``, DD pattern).
"""

import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from queue import Empty, Queue
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

try:
    import lancedb  # noqa: F401
except ImportError:
    import types

    sys.modules["lancedb"] = types.ModuleType("lancedb")

from fastapi.testclient import TestClient

from app.api.deps import get_db, get_vector_store
from app.config import settings
from app.main import app
from app.security import csrf_protect
from app.services.auth_service import compute_client_fingerprint, create_access_token


class _PoolWithConnectionCM:
    """See ``test_draft_routes.py`` for rationale (SSE route needs
    ``request.app.state.db_pool.connection()``)."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._pool: Queue = Queue(maxsize=5)
        self._closed = False

    def get_connection(self) -> sqlite3.Connection:
        if self._closed:
            raise RuntimeError("Pool closed")
        try:
            return self._pool.get_nowait()
        except Empty:
            return self._create_connection()

    def _create_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn

    def release_connection(self, conn: sqlite3.Connection) -> None:
        if self._closed:
            conn.close()
            return
        try:
            self._pool.put_nowait(conn)
        except Exception:
            conn.close()

    def close_all(self) -> None:
        self._closed = True
        while True:
            try:
                self._pool.get_nowait().close()
            except Empty:
                break

    @contextmanager
    def connection(self):
        conn = self.get_connection()
        try:
            yield conn
        finally:
            self.release_connection(conn)


def _default_brief(**overrides) -> dict:
    brief = {
        "piece_type": "article",
        "audience": "general readers",
        "purpose": "inform readers about the topic",
        "tone": "clear and direct",
        "target_words": 500,
        "transformation_strength": "moderate",
        "primary_input_id": None,
        "must_include": [],
        "must_avoid": [],
        "preserve_quotes": True,
        "preserve_numbers": True,
        "preserve_uncertainty": True,
        "drafting_priority": "balanced",
        "additional_instructions": "",
    }
    brief.update(overrides)
    return brief


class DraftSecurityTestBase(unittest.TestCase):
    OWNER_ID = 1
    OTHER_ID = 2
    READ_VAULT_ID = 2
    NO_ACCESS_VAULT_ID = 9

    def setUp(self):
        self.client = TestClient(app)
        self.client.headers["user-agent"] = ""
        self._temp_dir = tempfile.mkdtemp()

        self._original_jwt_secret = settings.jwt_secret_key
        self._original_users_enabled = settings.users_enabled
        self._original_data_dir = settings.data_dir
        self._original_draft_room_enabled = settings.draft_room_enabled

        settings.data_dir = Path(self._temp_dir)
        settings.jwt_secret_key = os.urandom(32).hex()
        settings.users_enabled = True
        settings.draft_room_enabled = True

        self._db_path = str(Path(self._temp_dir) / "app.db")

        from app.models.database import _pool_cache, _pool_cache_lock

        with _pool_cache_lock:
            for _path, pool in list(_pool_cache.items()):
                pool.close_all()
            _pool_cache.clear()

        from app.models.database import init_db, run_migrations

        init_db(self._db_path)
        run_migrations(self._db_path)
        self._connection_pool = _PoolWithConnectionCM(self._db_path)

        def override_get_db():
            conn = self._connection_pool.get_connection()
            try:
                yield conn
            finally:
                self._connection_pool.release_connection(conn)

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[csrf_protect] = lambda: "test-csrf"
        app.state.db_pool = self._connection_pool

        self._mock_vector_store = MagicMock()
        self._mock_vector_store.db = MagicMock()
        self._mock_vector_store.db.table_names = AsyncMock(return_value=["chunks"])
        self._mock_vector_store.db.open_table = AsyncMock(return_value=MagicMock())
        self._mock_vector_store.delete_by_file = AsyncMock(return_value=1)
        app.dependency_overrides[get_vector_store] = lambda: self._mock_vector_store

        conn = self._connection_pool.get_connection()
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            pw = "test-password-hash"
            conn.execute(
                "INSERT OR IGNORE INTO users (id, username, hashed_password, full_name, role, is_active) "
                "VALUES (1,'owner',?, 'Owner','member',1)",
                (pw,),
            )
            conn.execute(
                "INSERT OR IGNORE INTO users (id, username, hashed_password, full_name, role, is_active) "
                "VALUES (2,'other',?, 'Other','member',1)",
                (pw,),
            )
            conn.execute(
                "INSERT OR IGNORE INTO vaults (id, name, description) VALUES (2,'Read Vault','r')"
            )
            conn.execute(
                "INSERT OR IGNORE INTO vaults (id, name, description) VALUES (9,'No Access Vault','n')"
            )
            conn.execute(
                "INSERT OR IGNORE INTO vault_members (vault_id, user_id, permission, granted_by) "
                "VALUES (2,1,'write',1)"
            )
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)

    def tearDown(self):
        from app.models.database import _pool_cache, _pool_cache_lock

        with _pool_cache_lock:
            for _path, pool in list(_pool_cache.items()):
                pool.close_all()
            _pool_cache.clear()

        settings.jwt_secret_key = self._original_jwt_secret
        settings.users_enabled = self._original_users_enabled
        settings.data_dir = self._original_data_dir
        settings.draft_room_enabled = self._original_draft_room_enabled
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(csrf_protect, None)
        app.dependency_overrides.pop(get_vector_store, None)
        if hasattr(app.state, "db_pool"):
            del app.state.db_pool
        if hasattr(self, "_connection_pool"):
            self._connection_pool.close_all()
        shutil.rmtree(self._temp_dir, ignore_errors=True)

    def _headers(self, user_id=OWNER_ID, username="owner", role="member"):
        return {
            "Authorization": f"Bearer {create_access_token(user_id, username, role, client_fingerprint=compute_client_fingerprint(''))}"
        }

    def _owner_headers(self):
        return self._headers(self.OWNER_ID, "owner")

    def _other_headers(self):
        return self._headers(self.OTHER_ID, "other")

    def _create_draft(self, *, headers=None, vault_id=READ_VAULT_ID, title="Draft"):
        headers = headers or self._owner_headers()
        resp = self.client.post(
            "/api/draft-room/drafts",
            json={
                "vault_id": vault_id,
                "title": title,
                "mode": "rewrite",
                "tier": "standard",
                "brief": _default_brief(),
            },
            headers=headers,
        )
        return resp

    def _upload_input(self, draft_id, *, headers=None, content=b"manuscript body", filename="a.txt"):
        headers = headers or self._owner_headers()
        return self.client.post(
            f"/api/draft-room/drafts/{draft_id}/inputs",
            data={"role": "manuscript", "authority": "primary"},
            files={"file": (filename, content, "text/plain")},
            headers=headers,
        )

    def _revoke_vault_access(self, user_id: int, vault_id: int) -> None:
        conn = self._connection_pool.get_connection()
        try:
            conn.execute(
                "DELETE FROM vault_members WHERE user_id = ? AND vault_id = ?",
                (user_id, vault_id),
            )
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)


class TestNonOwnerIsolation(DraftSecurityTestBase):
    def test_non_owner_gets_404_on_every_draft_route(self):
        draft_id = self._create_draft().json()["id"]
        upload = self._upload_input(draft_id)
        input_id = upload.json()["input"]["id"]
        job_id = upload.json()["job"]["id"]

        rev = self.client.post(
            f"/api/draft-room/drafts/{draft_id}/revisions",
            json={"base_revision_id": None, "lock_version": 1, "content_md": "v1"},
            headers=self._owner_headers(),
        )
        revision_id = rev.json()["summary"]["id"]

        other = self._other_headers()

        cases = [
            ("GET", f"/api/draft-room/drafts/{draft_id}"),
            (
                "PATCH",
                f"/api/draft-room/drafts/{draft_id}",
                {"lock_version": 1, "title": "x"},
            ),
            ("POST", f"/api/draft-room/drafts/{draft_id}/archive", {"lock_version": 1}),
            ("POST", f"/api/draft-room/drafts/{draft_id}/restore", {"lock_version": 1}),
            ("DELETE", f"/api/draft-room/drafts/{draft_id}"),
            (
                "PATCH",
                f"/api/draft-room/drafts/{draft_id}/inputs/{input_id}",
                {"role": "reference"},
            ),
            ("GET", f"/api/draft-room/drafts/{draft_id}/inputs/{input_id}/content"),
            ("DELETE", f"/api/draft-room/drafts/{draft_id}/inputs/{input_id}"),
            ("GET", f"/api/draft-room/drafts/{draft_id}/jobs"),
            ("GET", f"/api/draft-room/drafts/{draft_id}/jobs/{job_id}"),
            ("POST", f"/api/draft-room/drafts/{draft_id}/jobs/{job_id}/cancel"),
            ("POST", f"/api/draft-room/drafts/{draft_id}/jobs/{job_id}/retry"),
            ("GET", f"/api/draft-room/drafts/{draft_id}/revisions"),
            ("GET", f"/api/draft-room/drafts/{draft_id}/revisions/{revision_id}"),
            (
                "POST",
                f"/api/draft-room/drafts/{draft_id}/revisions",
                {"base_revision_id": revision_id, "lock_version": 1, "content_md": "x"},
            ),
            (
                "POST",
                f"/api/draft-room/drafts/{draft_id}/revisions/{revision_id}/export"
                "?acknowledge_not_fact_checked=true",
            ),
        ]
        for case in cases:
            method, path = case[0], case[1]
            json_body = case[2] if len(case) > 2 else None
            resp = self.client.request(method, path, json=json_body, headers=other)
            self.assertEqual(
                resp.status_code, 404, f"{method} {path} -> {resp.status_code}: {resp.text}"
            )

    def test_non_owner_upload_returns_404(self):
        draft_id = self._create_draft().json()["id"]
        resp = self._upload_input(draft_id, headers=self._other_headers())
        self.assertEqual(resp.status_code, 404)

    def test_cross_draft_input_id_substitution_returns_404(self):
        """An input belonging to draft A, requested through draft B's path
        (even when both drafts share the same owner), must not resolve."""
        draft_a = self._create_draft(title="A").json()["id"]
        draft_b = self._create_draft(title="B").json()["id"]
        upload = self._upload_input(draft_a)
        input_id = upload.json()["input"]["id"]

        resp = self.client.get(
            f"/api/draft-room/drafts/{draft_b}/inputs/{input_id}/content",
            headers=self._owner_headers(),
        )
        self.assertEqual(resp.status_code, 404, resp.text)

    def test_list_never_returns_other_owners_drafts(self):
        self._create_draft(headers=self._owner_headers(), title="Owner draft")
        resp = self.client.get("/api/draft-room/drafts", headers=self._other_headers())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["items"], [])


class TestVaultAccessRevocation(DraftSecurityTestBase):
    def test_revoked_access_blocks_content_but_allows_cancel_and_delete(self):
        draft_id = self._create_draft().json()["id"]
        upload = self._upload_input(draft_id)
        job_id = upload.json()["job"]["id"]

        self._revoke_vault_access(self.OWNER_ID, self.READ_VAULT_ID)

        # Content/edit operations are blocked.
        resp = self.client.get(
            f"/api/draft-room/drafts/{draft_id}", headers=self._owner_headers()
        )
        self.assertEqual(resp.status_code, 403, resp.text)
        self.assertEqual(resp.json()["code"], "vault_access_revoked")

        resp = self.client.patch(
            f"/api/draft-room/drafts/{draft_id}",
            json={"lock_version": 1, "title": "x"},
            headers=self._owner_headers(),
        )
        self.assertEqual(resp.status_code, 403, resp.text)

        # List still returns the row, annotated as revoked (never suppressed).
        resp = self.client.get("/api/draft-room/drafts", headers=self._owner_headers())
        self.assertEqual(resp.status_code, 200)
        row = next(d for d in resp.json()["items"] if d["id"] == draft_id)
        self.assertEqual(row["vault_access"], "revoked")

        # Cancel still works (reduces private processing only).
        resp = self.client.post(
            f"/api/draft-room/drafts/{draft_id}/jobs/{job_id}/cancel",
            headers=self._owner_headers(),
        )
        self.assertEqual(resp.status_code, 200, resp.text)

        # Whole-draft delete still works.
        resp = self.client.delete(
            f"/api/draft-room/drafts/{draft_id}", headers=self._owner_headers()
        )
        self.assertEqual(resp.status_code, 204, resp.text)


class TestCSRFEnforcement(DraftSecurityTestBase):
    def test_every_mutation_requires_csrf(self):
        draft_id = self._create_draft().json()["id"]
        upload = self._upload_input(draft_id)
        input_id = upload.json()["input"]["id"]
        job_id = upload.json()["job"]["id"]

        # Remove the CSRF bypass so the real dependency runs.
        app.dependency_overrides.pop(csrf_protect, None)
        try:
            headers = self._owner_headers()
            mutations = [
                ("POST", "/api/draft-room/drafts", {"vault_id": self.READ_VAULT_ID, "title": "x", "mode": "rewrite", "tier": "standard", "brief": _default_brief()}),
                ("PATCH", f"/api/draft-room/drafts/{draft_id}", {"lock_version": 1, "title": "x"}),
                ("POST", f"/api/draft-room/drafts/{draft_id}/archive", {"lock_version": 1}),
                ("DELETE", f"/api/draft-room/drafts/{draft_id}", None),
                ("PATCH", f"/api/draft-room/drafts/{draft_id}/inputs/{input_id}", {"role": "reference"}),
                ("DELETE", f"/api/draft-room/drafts/{draft_id}/inputs/{input_id}", None),
                ("POST", f"/api/draft-room/drafts/{draft_id}/jobs/{job_id}/cancel", None),
                ("POST", f"/api/draft-room/drafts/{draft_id}/jobs/{job_id}/retry", None),
                (
                    "POST",
                    f"/api/draft-room/drafts/{draft_id}/revisions",
                    {"base_revision_id": None, "lock_version": 1, "content_md": "x"},
                ),
            ]
            for method, path, body in mutations:
                resp = self.client.request(method, path, json=body, headers=headers)
                self.assertEqual(
                    resp.status_code, 403, f"{method} {path} -> {resp.status_code}: {resp.text}"
                )
        finally:
            app.dependency_overrides[csrf_protect] = lambda: "test-csrf"

    def test_upload_requires_csrf(self):
        draft_id = self._create_draft().json()["id"]
        app.dependency_overrides.pop(csrf_protect, None)
        try:
            resp = self._upload_input(draft_id)
            self.assertEqual(resp.status_code, 403, resp.text)
        finally:
            app.dependency_overrides[csrf_protect] = lambda: "test-csrf"


class TestDisabledGate(DraftSecurityTestBase):
    def test_disabled_blocks_mutations_but_keeps_cleanup_available(self):
        # Create one draft while enabled so we have something to inspect/export/delete.
        draft_id = self._create_draft().json()["id"]
        rev = self.client.post(
            f"/api/draft-room/drafts/{draft_id}/revisions",
            json={"base_revision_id": None, "lock_version": 1, "content_md": "v1"},
            headers=self._owner_headers(),
        )
        revision_id = rev.json()["summary"]["id"]

        settings.draft_room_enabled = False

        # Capabilities still works and reports disabled.
        resp = self.client.get("/api/draft-room/capabilities", headers=self._owner_headers())
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()["enabled"])

        # create/edit/upload/revision-create are gated.
        resp = self._create_draft(title="should fail")
        self.assertEqual(resp.status_code, 503, resp.text)
        self.assertEqual(resp.json()["code"], "draft_room_disabled")

        resp = self.client.patch(
            f"/api/draft-room/drafts/{draft_id}",
            json={"lock_version": 2, "title": "x"},
            headers=self._owner_headers(),
        )
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json()["code"], "draft_room_disabled")

        resp = self._upload_input(draft_id)
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json()["code"], "draft_room_disabled")

        resp = self.client.post(
            f"/api/draft-room/drafts/{draft_id}/revisions",
            json={"base_revision_id": revision_id, "lock_version": 2, "content_md": "v2"},
            headers=self._owner_headers(),
        )
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json()["code"], "draft_room_disabled")

        # Owner list/read/export/cancel/delete stay available.
        resp = self.client.get("/api/draft-room/drafts", headers=self._owner_headers())
        self.assertEqual(resp.status_code, 200)

        resp = self.client.get(
            f"/api/draft-room/drafts/{draft_id}", headers=self._owner_headers()
        )
        self.assertEqual(resp.status_code, 200)

        resp = self.client.post(
            f"/api/draft-room/drafts/{draft_id}/revisions/{revision_id}/export"
            "?acknowledge_not_fact_checked=true",
            headers=self._owner_headers(),
        )
        self.assertEqual(resp.status_code, 200, resp.text)

        resp = self.client.delete(
            f"/api/draft-room/drafts/{draft_id}", headers=self._owner_headers()
        )
        self.assertEqual(resp.status_code, 204, resp.text)


if __name__ == "__main__":
    unittest.main()
