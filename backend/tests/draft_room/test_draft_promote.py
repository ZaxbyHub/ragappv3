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

import asyncio
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from queue import Empty, Queue
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

try:
    import lancedb  # noqa: F401
except ImportError:
    import types

    sys.modules["lancedb"] = types.ModuleType("lancedb")

import anyio
from fastapi.testclient import TestClient

import app.services.draft_promotion as draft_promotion_module
from app.api.deps import get_background_processor, get_db, get_vector_store
from app.config import settings
from app.main import app
from app.security import CSRFManager, csrf_protect
from app.services.auth_service import compute_client_fingerprint, create_access_token
from app.services.draft_input_storage import DraftInputStorage
from app.services.draft_promotion import DraftPromotionDuplicateError, promote_input
from app.services.draft_store import DraftStore


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
        """Covers the SEQUENTIAL case: the second promotion is issued
        strictly after the first has already committed its `files` row.
        See `TestPromoteConcurrentDuplicateContent` below for the genuinely
        concurrent case (`_register_file`'s single `BEGIN IMMEDIATE`
        transaction over both the duplicate-content check and the path
        check closes the race this test does not exercise)."""
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


class TestPromoteConcurrentDuplicateContent(DraftPromoteTestBase):
    """P2 review finding: the duplicate-content check and the row insert
    used to run on separate connections/transactions, so two genuinely
    concurrent promotions of identical bytes (to two different destination
    filenames, so no path conflict masks the race) could both pass the
    duplicate lookup and both commit a `files` row.

    This drives two REAL OS threads, each with its own event loop
    (`asyncio.run`), synchronized with a `threading.Barrier` immediately
    before each calls `promote_input` against the SAME test database and
    vault. The harness cannot express a literal "pause after the duplicate
    lookup, before the insert" barrier any more, because the fix's whole
    point is that no such window exists to pause in any more: the lookup and
    the insert are one `BEGIN IMMEDIATE` transaction on one connection, so
    only one of the two threads can ever be inside it at a time -- the
    second thread's `BEGIN IMMEDIATE` genuinely blocks (`PRAGMA
    busy_timeout`/`timeout=` on the pool's connections) until the first
    commits or rolls back, then it runs its own check *after* that
    transaction's effects are visible. So instead of pausing mid-transaction,
    this test relies on real SQLite locking to serialize two truly
    concurrent callers and asserts the outcome: exactly one success, one
    `409`-shaped rejection, one `files` row, one `draft_promotions` row, and
    one enqueue call -- the property that was NOT guaranteed before the fix
    and is deterministic after it.
    """

    def test_two_concurrent_promotions_of_identical_bytes_collapse_to_one(self):
        content = b"Identical bytes raced by two real threads."

        draft_1 = self._create_draft(title="Racer One").json()["id"]
        upload_1 = self._upload_input(draft_1, content=content, filename="one.txt")
        input_id_1 = upload_1.json()["input"]["id"]
        self._mark_input_ready(input_id_1)

        draft_2 = self._create_draft(title="Racer Two").json()["id"]
        upload_2 = self._upload_input(draft_2, content=content, filename="two.txt")
        input_id_2 = upload_2.json()["input"]["id"]
        self._mark_input_ready(input_id_2)

        storage = DraftInputStorage(Path(settings.data_dir) / "draft-room")
        db_pool = self._connection_pool

        def _resolve(draft_id, input_id):
            conn = db_pool.get_connection()
            try:
                store = DraftStore(conn)
                draft = store.get_draft(draft_id, self.OWNER_ID)
                input_record = store.get_input(
                    draft_id=draft_id, owner_id=self.OWNER_ID, input_id=input_id
                )
                return draft, input_record
            finally:
                db_pool.release_connection(conn)

        draft_a, input_record_a = _resolve(draft_1, input_id_1)
        draft_b, input_record_b = _resolve(draft_2, input_id_2)

        shared_processor = MagicMock()
        shared_processor.enqueue = AsyncMock(return_value=None)

        barrier = threading.Barrier(2)
        results: dict = {}

        def _worker(key, draft, input_record, title):
            barrier.wait(timeout=10)

            async def _run():
                return await promote_input(
                    storage=storage,
                    db_pool=db_pool,
                    background_processor=shared_processor,
                    draft_id=draft.id,
                    vault_id=draft.vault_id,
                    title=title,
                    promoted_by=self.OWNER_ID,
                    input_record=input_record,
                )

            try:
                results[key] = ("ok", asyncio.run(_run()))
            except BaseException as exc:  # noqa: BLE001 -- capturing exactly what each racer got
                results[key] = ("error", exc)

        t1 = threading.Thread(
            target=_worker, args=("a", draft_a, input_record_a, "Racer One Title")
        )
        t2 = threading.Thread(
            target=_worker, args=("b", draft_b, input_record_b, "Racer Two Title")
        )
        t1.start()
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)

        self.assertEqual(set(results.keys()), {"a", "b"})
        outcomes = [results["a"], results["b"]]
        successes = [r for kind, r in outcomes if kind == "ok"]
        errors = [r for kind, r in outcomes if kind == "error"]

        self.assertEqual(len(successes), 1, f"expected exactly one success, got {outcomes}")
        self.assertEqual(len(errors), 1, f"expected exactly one rejection, got {outcomes}")

        winner = successes[0]
        loser = errors[0]
        self.assertIsInstance(loser, DraftPromotionDuplicateError)
        self.assertEqual(loser.existing_file_id, winner.file_id)

        self.assertEqual(len(self._files_rows()), 1)
        self.assertEqual(len(self._promotion_rows(draft_1) + self._promotion_rows(draft_2)), 1)
        shared_processor.enqueue.assert_awaited_once()


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
        """Proves the route actually exists, not just that the capability
        flag is hardcoded True — the flag alone would still pass even if the
        route were deleted."""
        resp = self.client.get(
            "/api/draft-room/capabilities", headers=self._owner_headers()
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(resp.json()["promote_available"])

        from app.api.routes.draft_room import router as draft_room_router

        routes = {
            (method, route.path)
            for route in draft_room_router.routes
            for method in getattr(route, "methods", set())
        }
        self.assertIn(("POST", "/draft-room/drafts/{draft_id}/promote"), routes)


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


class TestPromoteFilenameSafety(DraftPromoteTestBase):
    def test_long_title_is_clamped_not_500(self):
        """A 252-character title used to make the sanitized filename exceed
        typical filesystem NAME_MAX, so `os.open` raised
        `OSError: [Errno 36] File name too long` -> an unhandled 500. The
        sanitized stem is now clamped well under that limit."""
        draft_id = self._create_draft().json()["id"]
        upload = self._upload_input(draft_id)
        input_id = upload.json()["input"]["id"]
        self._mark_input_ready(input_id)

        long_title = "A" * 252
        resp = self._promote(
            draft_id, source_type="input", source_id=input_id, title=long_title
        )
        self.assertEqual(resp.status_code, 202, resp.text)
        self.assertLess(len(resp.json()["filename"]), 255)


class TestPromoteAtomicity(DraftPromoteTestBase):
    """Issue #437 review follow-up: a promotion must never be left half-done
    -- either everything it creates (bytes, `files` row, `draft_promotions`
    row, enqueued job) exists, or none of it does."""

    def _ready_input(self, draft_id, content=b"body"):
        upload = self._upload_input(draft_id, content=content)
        input_id = upload.json()["input"]["id"]
        self._mark_input_ready(input_id)
        return input_id

    def test_enqueue_failure_leaves_no_orphan_files_row(self):
        draft_id = self._create_draft().json()["id"]
        input_id = self._ready_input(draft_id)

        self._mock_background_processor.enqueue = AsyncMock(
            side_effect=RuntimeError("queue unavailable")
        )
        resp = self._promote(draft_id, source_type="input", source_id=input_id)

        self.assertEqual(resp.status_code, 500, resp.text)
        self.assertEqual(resp.json()["code"], "internal_error")
        self.assertEqual(self._files_rows(), [])
        self.assertEqual(self._promotion_rows(draft_id), [])
        uploads_dir = settings.vault_uploads_dir(self.READ_VAULT_ID)
        self.assertEqual(list(uploads_dir.glob("*")), [])

    def test_set_phase_failure_leaves_no_orphan_files_row(self):
        draft_id = self._create_draft().json()["id"]
        input_id = self._ready_input(draft_id)

        with patch.object(
            draft_promotion_module, "set_phase", side_effect=RuntimeError("db unavailable")
        ):
            resp = self._promote(draft_id, source_type="input", source_id=input_id)

        self.assertEqual(resp.status_code, 500, resp.text)
        self.assertEqual(self._files_rows(), [])
        self.assertEqual(self._promotion_rows(draft_id), [])
        self._mock_background_processor.enqueue.assert_not_awaited()

    def test_promotion_row_insert_failure_leaves_no_orphan_document(self):
        """Simulates the exact race from the review finding: the draft is
        deleted between the `files` INSERT and the `draft_promotions`
        INSERT, so the provenance insert fails on its `draft_id` foreign
        key. Nothing this attempt created may survive -- not the `files`
        row, not the bytes, and enqueue must never have been reached."""
        draft_id = self._create_draft().json()["id"]
        input_id = self._ready_input(draft_id)

        real_register_file = draft_promotion_module._register_file

        def _register_then_delete_draft(db_pool, dest_path, file_hash, vault_id):
            created_file_id = real_register_file(db_pool, dest_path, file_hash, vault_id)
            conn = self._connection_pool.get_connection()
            try:
                conn.execute("DELETE FROM drafts WHERE id = ?", (draft_id,))
                conn.commit()
            finally:
                self._connection_pool.release_connection(conn)
            return created_file_id

        with patch.object(
            draft_promotion_module, "_register_file", side_effect=_register_then_delete_draft
        ):
            resp = self._promote(draft_id, source_type="input", source_id=input_id)

        self.assertEqual(resp.status_code, 500, resp.text)
        self.assertEqual(self._files_rows(), [])
        self._mock_background_processor.enqueue.assert_not_awaited()


class TestPromoteRevisionSizeLimit(DraftPromoteTestBase):
    def test_oversized_revision_is_413(self):
        """Probe scenario from the review finding: `max_file_size_mb=1` and a
        2 MiB revision. The input path is already safe (`stage_upload` caps
        it at upload time) -- this is the revision path only."""
        draft_id = self._create_draft().json()["id"]
        content_md = "x" * (2 * 1024 * 1024)  # 2 MiB

        original_max_chars = settings.draft_max_total_parsed_chars
        original_limit = settings.max_file_size_mb
        # Raise the content_md length guard out of the way first -- that
        # guard is a separate, earlier defense this fix also adds, and
        # without raising it here a 2 MiB revision could never be *created*
        # to promote in the first place.
        settings.draft_max_total_parsed_chars = len(content_md) + 10
        try:
            revision_id = self._create_revision(
                draft_id, content_md=content_md
            ).json()["summary"]["id"]

            settings.max_file_size_mb = 1
            resp = self._promote(draft_id, source_type="revision", source_id=revision_id)
        finally:
            settings.draft_max_total_parsed_chars = original_max_chars
            settings.max_file_size_mb = original_limit

        self.assertEqual(resp.status_code, 413, resp.text)
        self.assertEqual(resp.json()["code"], "promotion_too_large")
        self.assertEqual(self._files_rows(), [])
        uploads_dir = settings.vault_uploads_dir(self.READ_VAULT_ID)
        self.assertEqual(list(uploads_dir.glob("*")), [])

    def test_content_md_over_configured_limit_rejected_at_creation(self):
        draft_id = self._create_draft().json()["id"]
        original_max_chars = settings.draft_max_total_parsed_chars
        settings.draft_max_total_parsed_chars = 100
        try:
            resp = self._create_revision(draft_id, content_md="x" * 101)
        finally:
            settings.draft_max_total_parsed_chars = original_max_chars
        self.assertEqual(resp.status_code, 422, resp.text)


class TestPromoteInputSourcePreserved(DraftPromoteTestBase):
    def test_private_input_bytes_survive_promotion_on_disk(self):
        """Promotion COPIES the input's bytes; the private draft-room source
        file must still exist, unmodified, afterward (never moved/deleted)."""
        draft_id = self._create_draft().json()["id"]
        upload = self._upload_input(draft_id, content=b"private manuscript bytes")
        input_id = upload.json()["input"]["id"]
        self._mark_input_ready(input_id)

        storage_relpath = self._input_row(input_id)["storage_relpath"]
        source_path = Path(settings.data_dir) / "draft-room" / storage_relpath
        self.assertTrue(source_path.is_file())

        resp = self._promote(draft_id, source_type="input", source_id=input_id)
        self.assertEqual(resp.status_code, 202, resp.text)

        self.assertTrue(
            source_path.is_file(),
            "the private draft-room source file must not be moved or deleted",
        )
        self.assertEqual(source_path.read_bytes(), b"private manuscript bytes")


class TestPromoteAuditWritesAreBestEffort(DraftPromoteTestBase):
    """HIGH review finding: the promotion itself (bytes, `files` row,
    `draft_promotions` row, enqueue) must not be reported as a failure by
    something that happens strictly *after* it already succeeded — the
    `draft_events`/`security_audit_log` writes are bookkeeping, not part of
    the promotion's own atomicity contract."""

    def _ready_input(self, draft_id, content=b"body"):
        upload = self._upload_input(draft_id, content=content)
        input_id = upload.json()["input"]["id"]
        self._mark_input_ready(input_id)
        return input_id

    def test_draft_deleted_before_draft_events_write_still_returns_202(self):
        """Simulates the draft being deleted between the enqueue call and
        the `draft_events` write: `DraftStore.record_event` starts with
        `get_draft`, which 404s once the draft is gone. That must be
        recorded and logged, never allowed to turn an already-real,
        already-enqueued promotion into a reported failure."""
        draft_id = self._create_draft().json()["id"]
        input_id = self._ready_input(draft_id)

        real_record_event = DraftStore.record_event

        def _delete_draft_then_record_event(store_self, **kwargs):
            conn = self._connection_pool.get_connection()
            try:
                conn.execute("DELETE FROM drafts WHERE id = ?", (draft_id,))
                conn.commit()
            finally:
                self._connection_pool.release_connection(conn)
            return real_record_event(store_self, **kwargs)

        with patch.object(DraftStore, "record_event", _delete_draft_then_record_event):
            resp = self._promote(draft_id, source_type="input", source_id=input_id)

        self.assertEqual(resp.status_code, 202, resp.text)
        body = resp.json()
        file_id = body["file_id"]

        # The promotion genuinely happened and is reported as such.
        files = self._files_rows()
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0]["id"], file_id)
        self._mock_background_processor.enqueue.assert_awaited_once()

        # The provenance row is gone too -- correctly cascade-deleted with
        # the draft (`draft_id` is `ON DELETE CASCADE`) -- but that is a
        # consequence of the draft's own deletion, not something the route
        # needed to succeed at recording for the response to be honest.
        conn = self._connection_pool.get_connection()
        try:
            promotions = conn.execute(
                "SELECT id FROM draft_promotions WHERE file_id = ?", (file_id,)
            ).fetchall()
        finally:
            self._connection_pool.release_connection(conn)
        self.assertEqual(promotions, [])

        # The failure to record it is itself recorded, not silently dropped.
        rows = self._security_audit_rows("draft_promoted")
        self.assertEqual(len(rows), 1)
        metadata = json.loads(rows[0]["metadata_json"])
        self.assertIsNotNone(metadata["draft_event_error"])


class TestPromoteNeverAdoptsAnExistingFileRow(DraftPromoteTestBase):
    """HIGH review finding (architectural): promotion must never touch a
    `files` row it did not itself create — not delete it, and not silently
    UPDATE/adopt it either. The shared path-keyed upsert
    (`DocumentProcessor._insert_or_get_file_record`) is an UPDATE on its
    adoption branch: it overwrites `file_hash`/`file_size`/`file_type`/
    `vault_id`/`source` and forces `status='pending'` on whatever row
    already sits at the destination path. Skipping the DELETE in
    compensation was not enough, because the corruption already happened via
    that UPDATE before compensation ever runs, and it wedges every retry
    into `409 duplicate_document` against the now-hijacked hash forever.
    Promotion now does its own exclusive insert
    (`_register_file`) and refuses outright — `409
    promotion_path_conflict` — if anything already exists at the reserved
    path, rather than adopting or overwriting it."""

    def _ready_input(self, draft_id, content=b"body"):
        upload = self._upload_input(draft_id, content=content)
        input_id = upload.json()["input"]["id"]
        self._mark_input_ready(input_id)
        return input_id

    def _seed_victim_row(self, dest_path, expected_filename):
        conn = self._connection_pool.get_connection()
        try:
            cursor = conn.execute(
                "INSERT INTO files (vault_id, file_path, file_name, file_hash, "
                "file_size, file_type, source, status) "
                "VALUES (?, ?, ?, 'OLD_HASH_AAA', 12345, '.txt', 'upload', 'indexed')",
                (self.READ_VAULT_ID, str(dest_path), expected_filename),
            )
            victim_id = int(cursor.lastrowid)
            conn.execute(
                "INSERT INTO failed_chunks (file_id, chunk_index, chunk_text, "
                "chunk_metadata) VALUES (?, 0, 'the victim document text', '{}')",
                (victim_id,),
            )
            conn.commit()
            before = dict(
                conn.execute("SELECT * FROM files WHERE id = ?", (victim_id,)).fetchone()
            )
        finally:
            self._connection_pool.release_connection(conn)
        return victim_id, before

    def test_preexisting_file_row_is_rejected_not_adopted_or_corrupted(self):
        draft_id = self._create_draft().json()["id"]
        input_id = self._ready_input(draft_id)

        title = "Existing Document"
        expected_filename = "Existing_Document.txt"
        upload_dir = settings.vault_uploads_dir(self.READ_VAULT_ID)
        dest_path = upload_dir / expected_filename

        victim_id, before = self._seed_victim_row(dest_path, expected_filename)
        self.assertFalse(dest_path.exists(), "victim bytes are missing (precondition)")

        resp = self._promote(
            draft_id, source_type="input", source_id=input_id, title=title
        )

        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json()["code"], "promotion_path_conflict")

        # The victim row is untouched byte for byte -- not deleted, and not
        # silently overwritten by an adoption UPDATE either.
        conn = self._connection_pool.get_connection()
        try:
            after = dict(
                conn.execute("SELECT * FROM files WHERE id = ?", (victim_id,)).fetchone()
            )
            chunk_count = conn.execute(
                "SELECT COUNT(*) c FROM failed_chunks WHERE file_id = ?", (victim_id,)
            ).fetchone()["c"]
        finally:
            self._connection_pool.release_connection(conn)
        self.assertEqual(before, after)
        self.assertEqual(chunk_count, 1)

        # No new row was created, no promotion row, nothing enqueued, and
        # the reserved (empty) file this attempt created is cleaned up.
        self.assertEqual(len(self._files_rows()), 1)
        self.assertEqual(self._promotion_rows(draft_id), [])
        self._mock_background_processor.enqueue.assert_not_awaited()

    def test_retry_after_path_conflict_is_not_permanently_wedged(self):
        """A rejected promotion must not leave the destination path — or the
        vault's dedupe index — in a state where every subsequent retry (even
        under a different title, i.e. a different destination path) is
        blocked."""
        draft_id = self._create_draft().json()["id"]
        input_id = self._ready_input(draft_id)

        expected_filename = "Existing_Document.txt"
        dest_path = settings.vault_uploads_dir(self.READ_VAULT_ID) / expected_filename
        self._seed_victim_row(dest_path, expected_filename)

        first = self._promote(
            draft_id, source_type="input", source_id=input_id, title="Existing Document"
        )
        self.assertEqual(first.status_code, 409, first.text)
        self.assertEqual(first.json()["code"], "promotion_path_conflict")

        # Retrying under a different title (a different destination path)
        # succeeds normally -- the earlier rejection left no wedge behind.
        second = self._promote(
            draft_id, source_type="input", source_id=input_id, title="A New Title"
        )
        self.assertEqual(second.status_code, 202, second.text)


class TestPromoteCleanupFailureIsRecorded(DraftPromoteTestBase):
    """MEDIUM review finding: if a compensating delete itself fails after a
    later promotion step already failed, that must not be silent — it is
    recorded as its own audit event and surfaced with a status
    distinguishable from an ordinary clean failure."""

    def _ready_input(self, draft_id, content=b"body"):
        upload = self._upload_input(draft_id, content=content)
        input_id = upload.json()["input"]["id"]
        self._mark_input_ready(input_id)
        return input_id

    def test_compensation_failure_is_recorded_and_distinguishable(self):
        draft_id = self._create_draft().json()["id"]
        input_id = self._ready_input(draft_id)

        self._mock_background_processor.enqueue = AsyncMock(
            side_effect=RuntimeError("queue unavailable")
        )
        with patch.object(draft_promotion_module, "_delete_file_row", return_value=False):
            resp = self._promote(draft_id, source_type="input", source_id=input_id)

        self.assertEqual(resp.status_code, 500, resp.text)
        self.assertEqual(resp.json()["code"], "promotion_failed_incomplete_cleanup")

        rows = self._security_audit_rows("draft_promotion_cleanup_failed")
        self.assertEqual(len(rows), 1)
        metadata = json.loads(rows[0]["metadata_json"])
        self.assertEqual(metadata["draft_id"], draft_id)


class TestPromoteCancellation(DraftPromoteTestBase):
    """MEDIUM review finding: a client disconnect (`asyncio.CancelledError`)
    or interpreter shutdown signal during promotion must unwind as itself —
    not be converted into a `DraftPromotionError` reported as an ordinary
    500 — even though compensation must still run first. Exercised directly
    against the service function: an HTTP-level TestClient request cannot
    reliably simulate genuine mid-request task cancellation."""

    def test_cancelled_error_propagates_unwrapped_after_compensation(self):
        draft_id = self._create_draft().json()["id"]
        upload = self._upload_input(draft_id, content=b"cancel me")
        input_id = upload.json()["input"]["id"]
        self._mark_input_ready(input_id)

        conn = self._connection_pool.get_connection()
        try:
            store = DraftStore(conn)
            draft = store.get_draft(draft_id, self.OWNER_ID)
            input_record = store.get_input(
                draft_id=draft_id, owner_id=self.OWNER_ID, input_id=input_id
            )
        finally:
            self._connection_pool.release_connection(conn)

        storage = DraftInputStorage(Path(settings.data_dir) / "draft-room")
        cancelling_processor = MagicMock()
        cancelling_processor.enqueue = AsyncMock(side_effect=asyncio.CancelledError())

        async def _run():
            return await promote_input(
                storage=storage,
                db_pool=self._connection_pool,
                background_processor=cancelling_processor,
                draft_id=draft_id,
                vault_id=draft.vault_id,
                title="Cancelled Title",
                promoted_by=self.OWNER_ID,
                input_record=input_record,
            )

        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(_run())

        # Compensation still ran despite the cancellation propagating unwrapped.
        self.assertEqual(self._files_rows(), [])
        uploads_dir = settings.vault_uploads_dir(self.READ_VAULT_ID)
        self.assertEqual(list(uploads_dir.glob("*")), [])

    def test_anyio_cancel_scope_still_runs_compensation(self):
        """The real production mechanism: Starlette delivers a dropped
        client connection as anyio task-group cancellation, which is
        level-triggered -- the next `await` inside a cancelled scope raises
        immediately regardless of plain `asyncio.shield`. Only
        `anyio.CancelScope(shield=True)` (used by `_compensate_shielded`)
        actually protects compensation from this."""
        draft_id = self._create_draft().json()["id"]
        upload = self._upload_input(draft_id, content=b"cancel me via anyio")
        input_id = upload.json()["input"]["id"]
        self._mark_input_ready(input_id)

        conn = self._connection_pool.get_connection()
        try:
            store = DraftStore(conn)
            draft = store.get_draft(draft_id, self.OWNER_ID)
            input_record = store.get_input(
                draft_id=draft_id, owner_id=self.OWNER_ID, input_id=input_id
            )
        finally:
            self._connection_pool.release_connection(conn)

        storage = DraftInputStorage(Path(settings.data_dir) / "draft-room")

        class _SlowThenCancelledProcessor:
            async def enqueue(self, **kwargs):
                # Give the killer task a chance to fire the cancel scope
                # while this coroutine is still the one running.
                await anyio.sleep(0.2)

        outcome: dict = {}

        async def _body():
            try:
                await promote_input(
                    storage=storage,
                    db_pool=self._connection_pool,
                    background_processor=_SlowThenCancelledProcessor(),
                    draft_id=draft_id,
                    vault_id=draft.vault_id,
                    title="Cancelled Via Anyio",
                    promoted_by=self.OWNER_ID,
                    input_record=input_record,
                )
            except BaseException as exc:  # noqa: BLE001 -- capturing exactly what propagates
                outcome["exception_type"] = type(exc).__name__

        async def _run():
            async with anyio.create_task_group() as tg:
                async def _killer():
                    await anyio.sleep(0.05)
                    tg.cancel_scope.cancel()

                tg.start_soon(_killer)
                tg.start_soon(_body)

        asyncio.run(_run())

        self.assertIn("exception_type", outcome)
        self.assertEqual(outcome["exception_type"], "CancelledError")
        # Compensation ran to completion despite the cancel scope having
        # already fired -- the whole point of the anyio shield.
        self.assertEqual(self._files_rows(), [])
        uploads_dir = settings.vault_uploads_dir(self.READ_VAULT_ID)
        self.assertEqual(list(uploads_dir.glob("*")), [])


class TestPromoteConcurrentPathWriter(DraftPromoteTestBase):
    """HIGH review finding (architectural): a concurrent writer for the exact
    reserved path — another promotion, a normal upload, or ``FileWatcher``'s
    default-on background scan (`settings.auto_scan_enabled`) — must never
    be silently adopted. Simulated deterministically: a `files` row is
    inserted for the destination path in between path reservation and
    registration, exactly the window a concurrent writer would land in."""

    def test_row_appearing_after_reservation_is_rejected_not_adopted(self):
        draft_id = self._create_draft().json()["id"]
        upload = self._upload_input(draft_id, content=b"racing content")
        input_id = upload.json()["input"]["id"]
        self._mark_input_ready(input_id)

        upload_dir = settings.vault_uploads_dir(self.READ_VAULT_ID)
        title = "Racing Document"
        expected_filename = "Racing_Document.txt"

        real_reserve = draft_promotion_module._reserve_destination_path

        def _reserve_then_insert_concurrent_row(reserve_upload_dir, file_name):
            dest = real_reserve(reserve_upload_dir, file_name)
            # A concurrent writer (e.g. FileWatcher) commits a `files` row
            # for this exact path in the window before `_register_file` runs.
            conn = self._connection_pool.get_connection()
            try:
                conn.execute(
                    "INSERT INTO files (vault_id, file_path, file_name, "
                    "file_hash, file_size, source, status) "
                    "VALUES (?, ?, ?, 'SCAN_HASH', 0, 'scan', 'indexed')",
                    (self.READ_VAULT_ID, str(dest), file_name),
                )
                conn.commit()
            finally:
                self._connection_pool.release_connection(conn)
            return dest

        with patch.object(
            draft_promotion_module,
            "_reserve_destination_path",
            side_effect=_reserve_then_insert_concurrent_row,
        ):
            resp = self._promote(
                draft_id, source_type="input", source_id=input_id, title=title
            )

        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json()["code"], "promotion_path_conflict")

        # The concurrent writer's row survives untouched; no second row was
        # adopted or created alongside it.
        files = self._files_rows()
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0]["source"], "scan")
        self.assertEqual(files[0]["file_hash"], "SCAN_HASH")
        self._mock_background_processor.enqueue.assert_not_awaited()

        dest_path = upload_dir / expected_filename
        self.assertFalse(
            dest_path.exists(), "the reserved (empty) bytes must be cleaned up"
        )


class TestPromoteCorrelatedPoolExhaustion(DraftPromoteTestBase):
    """HIGH review finding: pool exhaustion is exactly the condition most
    likely to cause the original failure (registering the file, inserting
    provenance, updating phase) AND the compensation that tries to clean up
    after it — the two are correlated, not independent. Escaping that
    correlation must never surface as a raw, unenveloped exception."""

    def test_pool_exhausted_from_registration_onward_still_returns_envelope(self):
        draft_id = self._create_draft().json()["id"]
        upload = self._upload_input(draft_id, content=b"pool exhaustion probe")
        input_id = upload.json()["input"]["id"]
        self._mark_input_ready(input_id)

        pool = self._connection_pool
        real_get = pool.get_connection
        armed = {"on": False}

        real_register_file = draft_promotion_module._register_file

        def _register_then_arm(*args, **kwargs):
            result = real_register_file(*args, **kwargs)
            armed["on"] = True
            return result

        def _gated_get():
            if armed["on"]:
                raise RuntimeError(
                    "Could not obtain a connection from the pool after 3 attempts"
                )
            return real_get()

        with (
            patch.object(draft_promotion_module, "_register_file", side_effect=_register_then_arm),
            patch.object(pool, "get_connection", side_effect=_gated_get),
        ):
            try:
                resp = self._promote(draft_id, source_type="input", source_id=input_id)
            finally:
                armed["on"] = False

        # A truly exhausted pool cannot physically run the compensating
        # DELETE either -- that correlation is the whole point of this
        # scenario. The fix's job is not to make the impossible possible;
        # it is to make sure that failure surfaces as the Draft Room error
        # envelope (never a bare escaped exception or plain-text server
        # error) with a status/code distinguishable from an ordinary clean
        # failure, and that it is recorded so an operator can find and
        # reconcile the orphan later.
        self.assertEqual(resp.status_code, 500, resp.text)
        body = resp.json()
        self.assertEqual(body["code"], "promotion_failed_incomplete_cleanup")
        self.assertIn("detail", body)

        files = self._files_rows()
        self.assertEqual(len(files), 1, "the orphan is real -- the pool genuinely could not clean it up")

        cleanup_events = self._security_audit_rows("draft_promotion_cleanup_failed")
        self.assertEqual(
            len(cleanup_events),
            1,
            "the orphan must be recorded so an operator can find it",
        )
        metadata = json.loads(cleanup_events[0]["metadata_json"])
        self.assertEqual(metadata["draft_id"], draft_id)

        self._mock_background_processor.enqueue.assert_not_awaited()


class TestPromotePostEnqueueControlFlowExceptions(DraftPromoteTestBase):
    """MEDIUM review finding: the post-enqueue best-effort guards
    (organization, `draft_events`) must catch `BaseException`, not just
    `Exception` -- a cancellation reaching either of those must not skip the
    other, and must not skip the final `draft_promoted` audit write either."""

    def _ready_input(self, draft_id, content=b"body"):
        upload = self._upload_input(draft_id, content=content)
        input_id = upload.json()["input"]["id"]
        self._mark_input_ready(input_id)
        return input_id

    def test_record_event_basexception_still_returns_202_with_audit(self):
        draft_id = self._create_draft().json()["id"]
        input_id = self._ready_input(draft_id)

        def _boom(*args, **kwargs):
            raise asyncio.CancelledError()

        with patch.object(DraftStore, "record_event", _boom):
            resp = self._promote(draft_id, source_type="input", source_id=input_id)

        self.assertEqual(resp.status_code, 202, resp.text)
        rows = self._security_audit_rows("draft_promoted")
        self.assertEqual(len(rows), 1)
        metadata = json.loads(rows[0]["metadata_json"])
        self.assertIsNotNone(metadata["draft_event_error"])

    def test_organization_basexception_still_returns_202_with_audit(self):
        draft_id = self._create_draft().json()["id"]
        input_id = self._ready_input(draft_id)
        conn = self._connection_pool.get_connection()
        try:
            cursor = conn.execute(
                "INSERT INTO folders (vault_id, name) VALUES (?, ?)",
                (self.READ_VAULT_ID, "Target"),
            )
            folder_id = int(cursor.lastrowid)
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)

        from app.services.folder_store import FolderStore

        def _boom(*args, **kwargs):
            raise asyncio.CancelledError()

        with patch.object(FolderStore, "move_documents", _boom):
            resp = self._promote(
                draft_id,
                source_type="input",
                source_id=input_id,
                folder_id=folder_id,
            )

        self.assertEqual(resp.status_code, 202, resp.text)
        rows = self._security_audit_rows("draft_promoted")
        self.assertEqual(len(rows), 1)
        metadata = json.loads(rows[0]["metadata_json"])
        self.assertIsNotNone(metadata["organization_error"])


if __name__ == "__main__":
    unittest.main()
