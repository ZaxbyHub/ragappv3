"""Route tests for Draft Room promotion (issue #437, ``specs/draft-room/SPEC.md``
sections 3.4, 6.4, 8.2, 9.1, 9.3): copying a draft input or revision into the
draft's vault as a normal document via the shared internal ingestion service
(``app.services.draft_promotion``), without ever mutating the source
``draft_inputs``/``draft_revisions`` row, without writing to or referencing
the private draft input storage path, and with provenance recorded in both
``draft_promotions`` and ``security_audit_log``.

Harness copied from ``test_draft_routes.py``'s ``DraftRoomTestBase`` (per this
package's convention of duplicating rather than importing across test files;
see ``test_draft_compile_routes.py``'s own harness docstring).
"""

import json
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

from app.api.deps import get_background_processor, get_db, get_vector_store
from app.config import settings
from app.main import app
from app.security import CSRFManager, csrf_protect
from app.services.auth_service import compute_client_fingerprint, create_access_token


class _PoolWithConnectionCM:
    """Thread-safe SQLite pool exposing both the ``get_connection``/
    ``release_connection`` idiom (backs the ``get_db`` override) and the
    ``with pool.connection() as conn`` context manager
    ``app.services.document_progress.set_phase`` requires from
    ``request.app.state.db_pool`` (mirrors the production
    ``SQLiteConnectionPool.connection()``)."""

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


class DraftRoomTestBase(unittest.TestCase):
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
        self._original_draft_max_inputs = settings.draft_max_inputs
        self._original_draft_max_total_input_mb = settings.draft_max_total_input_mb
        self._original_max_file_size_mb = settings.max_file_size_mb

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
        app.state.csrf_manager = CSRFManager(redis_url="redis://localhost:6379/0", ttl=900)

        self._mock_vector_store = MagicMock()
        self._mock_vector_store.db = MagicMock()
        self._mock_vector_store.db.table_names = AsyncMock(return_value=["chunks"])
        self._mock_vector_store.db.open_table = AsyncMock(return_value=MagicMock())
        self._mock_vector_store.delete_by_file = AsyncMock(return_value=1)
        app.dependency_overrides[get_vector_store] = lambda: self._mock_vector_store

        # Promotion enqueues real ingestion via `background_processor.enqueue`;
        # `app.state.background_processor` is only set by lifespan startup,
        # which TestClient(app) does not run here, so it must be overridden
        # directly like `get_vector_store` above.
        self._mock_background_processor = MagicMock()
        self._mock_background_processor.enqueue = AsyncMock(return_value=None)
        app.dependency_overrides[get_background_processor] = (
            lambda: self._mock_background_processor
        )

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
        settings.draft_max_inputs = self._original_draft_max_inputs
        settings.draft_max_total_input_mb = self._original_draft_max_total_input_mb
        settings.max_file_size_mb = self._original_max_file_size_mb
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(csrf_protect, None)
        app.dependency_overrides.pop(get_vector_store, None)
        app.dependency_overrides.pop(get_background_processor, None)
        if hasattr(app.state, "db_pool"):
            del app.state.db_pool
        if hasattr(app.state, "csrf_manager"):
            del app.state.csrf_manager
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

    def _create_draft(
        self, *, headers=None, vault_id=READ_VAULT_ID, mode="rewrite", title="My Draft", brief=None
    ):
        headers = headers or self._owner_headers()
        resp = self.client.post(
            "/api/draft-room/drafts",
            json={
                "vault_id": vault_id,
                "title": title,
                "mode": mode,
                "tier": "standard",
                "brief": brief or _default_brief(),
            },
            headers=headers,
        )
        return resp

    def _upload_input(
        self,
        draft_id,
        *,
        headers=None,
        filename="manuscript.txt",
        content=b"Hello world. This is the manuscript body.",
        role="manuscript",
        authority="primary",
        content_type="text/plain",
    ):
        headers = headers or self._owner_headers()
        resp = self.client.post(
            f"/api/draft-room/drafts/{draft_id}/inputs",
            data={"role": role, "authority": authority},
            files={"file": (filename, content, content_type)},
            headers=headers,
        )
        return resp


class DraftPromoteTestBase(DraftRoomTestBase):
    """Extra helpers shared by every promotion test."""

    def _mark_input_ready(self, input_id: int, *, parse_status: str = "ready") -> None:
        conn = self._connection_pool.get_connection()
        try:
            conn.execute(
                "UPDATE draft_inputs SET parse_status = ?, parsed_text = 'parsed body', "
                "parsed_char_count = 11 WHERE id = ?",
                (parse_status, input_id),
            )
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)

    def _create_revision(self, draft_id, *, content_md="# Body\n\nPromotable content.", lock_version=1):
        return self.client.post(
            f"/api/draft-room/drafts/{draft_id}/revisions",
            json={
                "base_revision_id": None,
                "lock_version": lock_version,
                "content_md": content_md,
            },
            headers=self._owner_headers(),
        )

    def _promote(
        self,
        draft_id,
        *,
        source_type="input",
        source_id,
        title="Promoted Document",
        folder_id=None,
        tag_ids=None,
        headers=None,
    ):
        headers = headers or self._owner_headers()
        body: dict = {
            "source_type": source_type,
            "source_id": source_id,
            "title": title,
        }
        if folder_id is not None:
            body["folder_id"] = folder_id
        if tag_ids is not None:
            body["tag_ids"] = tag_ids
        return self.client.post(
            f"/api/draft-room/drafts/{draft_id}/promote", json=body, headers=headers
        )

    def _make_read_only_vault(self, vault_id: int = 5) -> int:
        conn = self._connection_pool.get_connection()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO vaults (id, name, description) VALUES (?, 'Read Only Vault', 'ro')",
                (vault_id,),
            )
            conn.execute(
                "INSERT OR IGNORE INTO vault_members (vault_id, user_id, permission, granted_by) "
                "VALUES (?, 1, 'read', 1)",
                (vault_id,),
            )
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)
        return vault_id

    def _files_rows(self) -> list:
        conn = self._connection_pool.get_connection()
        try:
            return conn.execute("SELECT * FROM files").fetchall()
        finally:
            self._connection_pool.release_connection(conn)

    def _promotion_rows(self, draft_id: int) -> list:
        conn = self._connection_pool.get_connection()
        try:
            return conn.execute(
                "SELECT * FROM draft_promotions WHERE draft_id = ?", (draft_id,)
            ).fetchall()
        finally:
            self._connection_pool.release_connection(conn)

    def _input_row(self, input_id: int):
        conn = self._connection_pool.get_connection()
        try:
            return conn.execute(
                "SELECT * FROM draft_inputs WHERE id = ?", (input_id,)
            ).fetchone()
        finally:
            self._connection_pool.release_connection(conn)

    def _revision_row(self, revision_id: int):
        conn = self._connection_pool.get_connection()
        try:
            return conn.execute(
                "SELECT * FROM draft_revisions WHERE id = ?", (revision_id,)
            ).fetchone()
        finally:
            self._connection_pool.release_connection(conn)

    def _security_audit_rows(self, event_type: str) -> list:
        conn = self._connection_pool.get_connection()
        try:
            return conn.execute(
                "SELECT * FROM security_audit_log WHERE event_type = ?", (event_type,)
            ).fetchall()
        finally:
            self._connection_pool.release_connection(conn)

    def _draft_events(self, draft_id: int, event_type: str) -> list:
        conn = self._connection_pool.get_connection()
        try:
            return conn.execute(
                "SELECT * FROM draft_events WHERE draft_id = ? AND event_type = ?",
                (draft_id, event_type),
            ).fetchall()
        finally:
            self._connection_pool.release_connection(conn)


class TestPromoteInput(DraftPromoteTestBase):
    def test_promote_input_succeeds(self):
        content = b"Original manuscript bytes for promotion."
        draft_id = self._create_draft().json()["id"]
        upload = self._upload_input(draft_id, content=content, filename="manuscript.txt")
        self.assertEqual(upload.status_code, 202, upload.text)
        input_id = upload.json()["input"]["id"]
        self._mark_input_ready(input_id)

        resp = self._promote(draft_id, source_type="input", source_id=input_id, title="My Title")
        self.assertEqual(resp.status_code, 202, resp.text)
        body = resp.json()
        self.assertEqual(body["draft_id"], draft_id)
        self.assertEqual(body["vault_id"], self.READ_VAULT_ID)
        self.assertEqual(body["source_type"], "input")
        self.assertEqual(body["source_id"], input_id)
        self.assertEqual(body["filename"], "My_Title.txt")
        self.assertTrue(body["source_sha256"])
        self.assertIn("file_id", body)
        self.assertIn("promotion_id", body)
        self.assertIn("created_at", body)

        # `files` row created in the destination vault, source-labeled.
        files = self._files_rows()
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0]["id"], body["file_id"])
        self.assertEqual(files[0]["vault_id"], self.READ_VAULT_ID)
        self.assertEqual(files[0]["source"], "draft_room_promote")
        self.assertEqual(files[0]["status"], "pending")

        # `draft_promotions` provenance row created.
        promotions = self._promotion_rows(draft_id)
        self.assertEqual(len(promotions), 1)
        self.assertEqual(promotions[0]["source_type"], "input")
        self.assertEqual(promotions[0]["source_id"], input_id)
        self.assertEqual(promotions[0]["file_id"], body["file_id"])
        self.assertEqual(promotions[0]["vault_id"], self.READ_VAULT_ID)
        self.assertEqual(promotions[0]["promoted_by"], self.OWNER_ID)

        # Promoted bytes are an exact copy of the input's bytes.
        dest_path = Path(files[0]["file_path"])
        self.assertEqual(dest_path.read_bytes(), content)

    def test_promoted_file_lives_under_vault_uploads_not_draft_room_storage(self):
        draft_id = self._create_draft().json()["id"]
        upload = self._upload_input(draft_id)
        input_id = upload.json()["input"]["id"]
        self._mark_input_ready(input_id)

        resp = self._promote(draft_id, source_type="input", source_id=input_id)
        self.assertEqual(resp.status_code, 202, resp.text)

        dest_path = Path(self._files_rows()[0]["file_path"]).resolve()
        vault_uploads_dir = settings.vault_uploads_dir(self.READ_VAULT_ID).resolve()
        draft_room_root = (Path(settings.data_dir) / "draft-room").resolve()
        self.assertTrue(str(dest_path).startswith(str(vault_uploads_dir)))
        self.assertFalse(str(dest_path).startswith(str(draft_room_root)))

    def test_input_row_unchanged_after_promotion(self):
        draft_id = self._create_draft().json()["id"]
        upload = self._upload_input(draft_id)
        input_id = upload.json()["input"]["id"]
        self._mark_input_ready(input_id)
        before = dict(self._input_row(input_id))

        resp = self._promote(draft_id, source_type="input", source_id=input_id)
        self.assertEqual(resp.status_code, 202, resp.text)

        after = dict(self._input_row(input_id))
        # AC-03 / issue #437: promotion must not mutate the source row —
        # not the parse/status fields, not the storage path, not the hash,
        # and no "indexed"-style flag exists on this table at all.
        self.assertEqual(before["parse_status"], after["parse_status"])
        self.assertEqual(before["storage_relpath"], after["storage_relpath"])
        self.assertEqual(before["content_sha256"], after["content_sha256"])
        self.assertEqual(before, after)
        self.assertNotIn("indexed", after.keys())

    def test_ingestion_enqueued_with_expected_arguments(self):
        draft_id = self._create_draft().json()["id"]
        upload = self._upload_input(draft_id)
        input_id = upload.json()["input"]["id"]
        self._mark_input_ready(input_id)

        resp = self._promote(draft_id, source_type="input", source_id=input_id)
        self.assertEqual(resp.status_code, 202, resp.text)
        file_id = resp.json()["file_id"]

        self._mock_background_processor.enqueue.assert_awaited_once()
        _, kwargs = self._mock_background_processor.enqueue.call_args
        self.assertEqual(kwargs["vault_id"], self.READ_VAULT_ID)
        self.assertEqual(kwargs["file_id"], file_id)
        self.assertEqual(kwargs["source"], "draft_room_promote")


class TestPromoteRevision(DraftPromoteTestBase):
    def test_promote_revision_succeeds(self):
        content_md = "# Draft Body\n\nExact revision text to promote."
        draft_id = self._create_draft().json()["id"]
        revision_resp = self._create_revision(draft_id, content_md=content_md)
        self.assertEqual(revision_resp.status_code, 201, revision_resp.text)
        revision_id = revision_resp.json()["summary"]["id"]

        resp = self._promote(
            draft_id, source_type="revision", source_id=revision_id, title="Rev Doc"
        )
        self.assertEqual(resp.status_code, 202, resp.text)
        body = resp.json()
        self.assertEqual(body["source_type"], "revision")
        self.assertEqual(body["source_id"], revision_id)
        self.assertTrue(body["filename"].endswith(".md"))

        dest_path = Path(self._files_rows()[0]["file_path"])
        self.assertEqual(dest_path.read_text(encoding="utf-8"), content_md)

    def test_revision_row_unchanged_after_promotion(self):
        draft_id = self._create_draft().json()["id"]
        revision_id = self._create_revision(draft_id).json()["summary"]["id"]
        before = dict(self._revision_row(revision_id))

        resp = self._promote(draft_id, source_type="revision", source_id=revision_id)
        self.assertEqual(resp.status_code, 202, resp.text)

        after = dict(self._revision_row(revision_id))
        self.assertEqual(before, after)


class TestPromoteAuthorization(DraftPromoteTestBase):
    def test_vault_write_missing_returns_403(self):
        vault_id = self._make_read_only_vault()
        draft_id = self._create_draft(vault_id=vault_id).json()["id"]

        resp = self._promote(draft_id, source_type="input", source_id=999999)
        self.assertEqual(resp.status_code, 403, resp.text)
        self.assertEqual(resp.json()["code"], "vault_write_required")

    def test_non_owner_returns_404(self):
        draft_id = self._create_draft().json()["id"]
        upload = self._upload_input(draft_id)
        input_id = upload.json()["input"]["id"]
        self._mark_input_ready(input_id)

        resp = self._promote(
            draft_id, source_type="input", source_id=input_id, headers=self._other_headers()
        )
        self.assertEqual(resp.status_code, 404, resp.text)


class TestPromoteDisabled(DraftPromoteTestBase):
    def test_draft_room_disabled_returns_503(self):
        draft_id = self._create_draft().json()["id"]
        upload = self._upload_input(draft_id)
        input_id = upload.json()["input"]["id"]
        self._mark_input_ready(input_id)

        settings.draft_room_enabled = False
        try:
            resp = self._promote(draft_id, source_type="input", source_id=input_id)
        finally:
            settings.draft_room_enabled = True
        self.assertEqual(resp.status_code, 503, resp.text)
        self.assertEqual(resp.json()["code"], "draft_room_disabled")


class TestPromoteCSRF(DraftPromoteTestBase):
    def test_missing_csrf_returns_403(self):
        draft_id = self._create_draft().json()["id"]
        upload = self._upload_input(draft_id)
        input_id = upload.json()["input"]["id"]
        self._mark_input_ready(input_id)

        app.dependency_overrides.pop(csrf_protect, None)
        try:
            resp = self._promote(draft_id, source_type="input", source_id=input_id)
        finally:
            app.dependency_overrides[csrf_protect] = lambda: "test-csrf"
        self.assertEqual(resp.status_code, 403, resp.text)


class TestPromoteValidation(DraftPromoteTestBase):
    def test_unknown_source_type_returns_422(self):
        draft_id = self._create_draft().json()["id"]
        resp = self._promote(draft_id, source_type="bogus", source_id=1)
        self.assertEqual(resp.status_code, 422, resp.text)
        self.assertEqual(resp.json()["code"], "validation_failed")

    def test_source_id_from_different_draft_returns_404(self):
        draft_a = self._create_draft(title="Draft A").json()["id"]
        draft_b = self._create_draft(title="Draft B").json()["id"]
        upload = self._upload_input(draft_a)
        input_id = upload.json()["input"]["id"]
        self._mark_input_ready(input_id)

        resp = self._promote(draft_b, source_type="input", source_id=input_id)
        self.assertEqual(resp.status_code, 404, resp.text)

    def test_input_not_ready_returns_409(self):
        draft_id = self._create_draft().json()["id"]
        upload = self._upload_input(draft_id)
        input_id = upload.json()["input"]["id"]
        # Left at its post-upload 'pending' parse_status — never marked ready.

        resp = self._promote(draft_id, source_type="input", source_id=input_id)
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json()["code"], "input_not_ready")

    def test_archived_draft_returns_409(self):
        draft_id = self._create_draft().json()["id"]
        upload = self._upload_input(draft_id)
        input_id = upload.json()["input"]["id"]
        self._mark_input_ready(input_id)
        job_id = upload.json()["job"]["id"]

        # Clear the still-pending parse job so archive's active-job guard passes.
        cancel = self.client.post(
            f"/api/draft-room/drafts/{draft_id}/jobs/{job_id}/cancel",
            headers=self._owner_headers(),
        )
        self.assertEqual(cancel.status_code, 200, cancel.text)
        archive = self.client.post(
            f"/api/draft-room/drafts/{draft_id}/archive",
            json={"lock_version": 1},
            headers=self._owner_headers(),
        )
        self.assertEqual(archive.status_code, 200, archive.text)

        resp = self._promote(draft_id, source_type="input", source_id=input_id)
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json()["code"], "invalid_state")


class TestPromoteDuplicate(DraftPromoteTestBase):
    def test_duplicate_content_in_vault_returns_409(self):
        content = b"Identical bytes across two different drafts."

        draft_1 = self._create_draft(title="First").json()["id"]
        upload_1 = self._upload_input(draft_1, content=content, filename="one.txt")
        input_1 = upload_1.json()["input"]["id"]
        self._mark_input_ready(input_1)
        first = self._promote(draft_1, source_type="input", source_id=input_1)
        self.assertEqual(first.status_code, 202, first.text)
        existing_file_id = first.json()["file_id"]

        draft_2 = self._create_draft(title="Second").json()["id"]
        upload_2 = self._upload_input(draft_2, content=content, filename="two.txt")
        input_2 = upload_2.json()["input"]["id"]
        self._mark_input_ready(input_2)
        second = self._promote(draft_2, source_type="input", source_id=input_2)

        self.assertEqual(second.status_code, 409, second.text)
        body = second.json()
        self.assertEqual(body["code"], "duplicate_document")
        self.assertEqual(body["context"]["existing_file_id"], existing_file_id)

        # No orphan bytes were left behind by the rejected second promotion.
        files = self._files_rows()
        self.assertEqual(len(files), 1)


class TestPromoteAuditAndCapabilities(DraftPromoteTestBase):
    def test_security_audit_log_row_records_new_file_id(self):
        draft_id = self._create_draft().json()["id"]
        upload = self._upload_input(draft_id)
        input_id = upload.json()["input"]["id"]
        self._mark_input_ready(input_id)

        resp = self._promote(draft_id, source_type="input", source_id=input_id)
        self.assertEqual(resp.status_code, 202, resp.text)
        file_id = resp.json()["file_id"]

        rows = self._security_audit_rows("draft_promoted")
        self.assertEqual(len(rows), 1)
        metadata = json.loads(rows[0]["metadata_json"])
        self.assertEqual(metadata["draft_id"], draft_id)
        self.assertEqual(metadata["file_id"], file_id)
        self.assertEqual(metadata["source_type"], "input")
        self.assertIsNone(metadata["revision_fact_status"])
        self.assertIsNone(metadata["was_ready_revision"])

    def test_security_audit_log_records_revision_fact_status(self):
        draft_id = self._create_draft().json()["id"]
        revision_id = self._create_revision(draft_id).json()["summary"]["id"]

        resp = self._promote(draft_id, source_type="revision", source_id=revision_id)
        self.assertEqual(resp.status_code, 202, resp.text)

        rows = self._security_audit_rows("draft_promoted")
        metadata = json.loads(rows[0]["metadata_json"])
        self.assertEqual(metadata["revision_fact_status"], "not_run")
        self.assertFalse(metadata["was_ready_revision"])

    def test_draft_events_row_recorded(self):
        draft_id = self._create_draft().json()["id"]
        upload = self._upload_input(draft_id)
        input_id = upload.json()["input"]["id"]
        self._mark_input_ready(input_id)

        resp = self._promote(draft_id, source_type="input", source_id=input_id)
        self.assertEqual(resp.status_code, 202, resp.text)

        events = self._draft_events(draft_id, "promoted")
        self.assertEqual(len(events), 1)

    def test_capabilities_reports_promote_available(self):
        resp = self.client.get(
            "/api/draft-room/capabilities", headers=self._owner_headers()
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(resp.json()["promote_available"])


class TestPromoteFolderAndTags(DraftPromoteTestBase):
    def _ready_input(self, draft_id, content=b"body"):
        upload = self._upload_input(draft_id, content=content)
        input_id = upload.json()["input"]["id"]
        self._mark_input_ready(input_id)
        return input_id

    def _insert_folder(self, folder_id, vault_id, name="Target"):
        conn = self._connection_pool.get_connection()
        try:
            conn.execute(
                "INSERT INTO folders (id, vault_id, name) VALUES (?, ?, ?)",
                (folder_id, vault_id, name),
            )
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)

    def _insert_tag(self, tag_id, vault_id, name="Tag"):
        conn = self._connection_pool.get_connection()
        try:
            conn.execute(
                "INSERT INTO tags (id, vault_id, name) VALUES (?, ?, ?)",
                (tag_id, vault_id, name),
            )
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)

    def _document_tag_ids(self, file_id):
        conn = self._connection_pool.get_connection()
        try:
            rows = conn.execute(
                "SELECT tag_id FROM document_tags WHERE file_id = ? ORDER BY tag_id",
                (file_id,),
            ).fetchall()
        finally:
            self._connection_pool.release_connection(conn)
        return [r["tag_id"] for r in rows]

    def test_folder_id_moves_promoted_file(self):
        draft_id = self._create_draft().json()["id"]
        input_id = self._ready_input(draft_id)
        self._insert_folder(1, self.READ_VAULT_ID)

        resp = self._promote(
            draft_id, source_type="input", source_id=input_id, folder_id=1
        )
        self.assertEqual(resp.status_code, 202, resp.text)
        self.assertEqual(self._files_rows()[0]["folder_id"], 1)

    def test_tag_ids_assigns_tags(self):
        draft_id = self._create_draft().json()["id"]
        input_id = self._ready_input(draft_id)
        self._insert_tag(1, self.READ_VAULT_ID, "Alpha")
        self._insert_tag(2, self.READ_VAULT_ID, "Beta")

        resp = self._promote(
            draft_id, source_type="input", source_id=input_id, tag_ids=[1, 2]
        )
        self.assertEqual(resp.status_code, 202, resp.text)
        file_id = resp.json()["file_id"]
        self.assertEqual(self._document_tag_ids(file_id), [1, 2])


class TestPromoteOrganizationValidation(DraftPromoteTestBase):
    """Issue #437 follow-up: an invalid organization target must be rejected
    before any promotion side effect — no copied bytes, no `files` row, no
    `draft_promotions` row, no enqueue — so a failed promotion never actually
    happened underneath the error the client sees."""

    def _ready_input(self, draft_id, content=b"body"):
        upload = self._upload_input(draft_id, content=content)
        input_id = upload.json()["input"]["id"]
        self._mark_input_ready(input_id)
        return input_id

    def _assert_no_promotion_side_effects(self, draft_id):
        self.assertEqual(self._files_rows(), [])
        self.assertEqual(self._promotion_rows(draft_id), [])
        self._mock_background_processor.enqueue.assert_not_awaited()
        uploads_dir = settings.vault_uploads_dir(self.READ_VAULT_ID)
        self.assertEqual(list(uploads_dir.glob("*")), [])

    def test_unknown_folder_id_rejected_with_no_side_effects(self):
        draft_id = self._create_draft().json()["id"]
        input_id = self._ready_input(draft_id)

        resp = self._promote(
            draft_id, source_type="input", source_id=input_id, folder_id=999999
        )
        self.assertEqual(resp.status_code, 404, resp.text)
        self.assertEqual(resp.json()["code"], "folder_not_found")
        self.assertEqual(resp.json()["context"]["folder_id"], 999999)
        self._assert_no_promotion_side_effects(draft_id)

    def test_unknown_tag_id_rejected_with_no_side_effects(self):
        draft_id = self._create_draft().json()["id"]
        input_id = self._ready_input(draft_id)

        resp = self._promote(
            draft_id, source_type="input", source_id=input_id, tag_ids=[999999]
        )
        self.assertEqual(resp.status_code, 404, resp.text)
        self.assertEqual(resp.json()["code"], "tag_not_found")
        self.assertEqual(resp.json()["context"]["tag_id"], 999999)
        self._assert_no_promotion_side_effects(draft_id)

    def test_folder_in_different_vault_rejected(self):
        draft_id = self._create_draft().json()["id"]
        input_id = self._ready_input(draft_id)
        other_vault_id = self._make_read_only_vault(vault_id=6)

        conn = self._connection_pool.get_connection()
        try:
            conn.execute(
                "INSERT INTO folders (id, vault_id, name) VALUES (1, ?, 'Elsewhere')",
                (other_vault_id,),
            )
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)

        resp = self._promote(
            draft_id, source_type="input", source_id=input_id, folder_id=1
        )
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json()["code"], "folder_wrong_vault")
        self.assertEqual(resp.json()["context"]["folder_id"], 1)
        self._assert_no_promotion_side_effects(draft_id)

    def test_tag_in_different_vault_rejected(self):
        draft_id = self._create_draft().json()["id"]
        input_id = self._ready_input(draft_id)
        other_vault_id = self._make_read_only_vault(vault_id=7)

        conn = self._connection_pool.get_connection()
        try:
            conn.execute(
                "INSERT INTO tags (id, vault_id, name) VALUES (1, ?, 'Elsewhere')",
                (other_vault_id,),
            )
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)

        resp = self._promote(
            draft_id, source_type="input", source_id=input_id, tag_ids=[1]
        )
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json()["code"], "tag_wrong_vault")
        self.assertEqual(resp.json()["context"]["tag_id"], 1)
        self._assert_no_promotion_side_effects(draft_id)

    def test_tag_ids_over_fifty_rejected_by_schema(self):
        """Confirms `tag_ids` is bounded (SPEC: max 50) at the pydantic layer,
        before the route body ever runs."""
        draft_id = self._create_draft().json()["id"]
        resp = self._promote(
            draft_id,
            source_type="input",
            source_id=1,
            tag_ids=list(range(1, 52)),
        )
        self.assertEqual(resp.status_code, 422, resp.text)
        self._assert_no_promotion_side_effects(draft_id)


if __name__ == "__main__":
    unittest.main()
