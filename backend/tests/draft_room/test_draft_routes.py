"""Route tests for Draft Room (issue #435): full lifecycle, pagination,
manual-revision immutability, export, error contract, and the no-`files`-row
guarantee for draft inputs.

Harness copied from ``test_tags_routes.py`` (DD pattern) and adapted with a
pool exposing ``pool.connection()`` (needed by the SSE route, which reads
``request.app.state.db_pool`` directly rather than ``Depends(get_db)``) and
two vaults (one the owner can read, one they cannot) per the Draft Room
authorization model.
"""

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
import zipfile
from contextlib import contextmanager
from io import BytesIO
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
from app.security import CSRFManager, csrf_protect
from app.services.auth_service import compute_client_fingerprint, create_access_token


class _PoolWithConnectionCM:
    """Thread-safe SQLite pool exposing both the ``get_connection``/
    ``release_connection`` idiom (backs the ``get_db`` override) and the
    ``with pool.connection() as conn`` context manager the Draft Room SSE
    route requires from ``request.app.state.db_pool`` (mirrors the production
    ``SQLiteConnectionPool.connection()`` and the pattern in
    ``test_draft_job_processor.py``)."""

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


def _minimal_docx_bytes(*, include_required_member: bool = True) -> bytes:
    """A minimal ZIP with the docx magic bytes, with or without the required
    OOXML member (``word/document.xml``) so upload validation can be exercised
    against both a well-formed and a malformed docx."""
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        if include_required_member:
            zf.writestr("word/document.xml", "<document/>")
        else:
            zf.writestr("not_the_right_member.xml", "<x/>")
    return buf.getvalue()


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
        # get_csrf_manager() 503s without this (normally set by lifespan startup,
        # which TestClient(app) does not run here). Redis is unreachable in this
        # environment, so CSRFManager falls back to its in-memory store — needed
        # by tests that remove the csrf_protect override to exercise the real
        # dependency.
        app.state.csrf_manager = CSRFManager(redis_url="redis://localhost:6379/0", ttl=900)

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
        settings.draft_max_inputs = self._original_draft_max_inputs
        settings.draft_max_total_input_mb = self._original_draft_max_total_input_mb
        settings.max_file_size_mb = self._original_max_file_size_mb
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(csrf_protect, None)
        app.dependency_overrides.pop(get_vector_store, None)
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

    def _files_count(self) -> int:
        conn = self._connection_pool.get_connection()
        try:
            return conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        finally:
            self._connection_pool.release_connection(conn)

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


class TestDraftLifecycle(DraftRoomTestBase):
    def test_full_owner_lifecycle(self):
        # Create
        resp = self._create_draft(title="Lifecycle Draft")
        self.assertEqual(resp.status_code, 201, resp.text)
        draft = resp.json()
        draft_id = draft["id"]
        self.assertEqual(draft["mode"], "rewrite")
        self.assertEqual(draft["vault_access"], "write")
        self.assertNotIn("storage_relpath", resp.text)

        # Upload
        upload = self._upload_input(draft_id)
        self.assertEqual(upload.status_code, 202, upload.text)
        body = upload.json()
        input_id = body["input"]["id"]
        job_id = body["job"]["id"]
        self.assertEqual(body["job"]["job_type"], "parse_input")
        self.assertEqual(body["job"]["status"], "pending")
        self.assertNotIn("storage_relpath", upload.text)
        self.assertNotIn("parsed_text", upload.text)

        # No `files` row, no ingestion job: draft inputs never touch the
        # normal document-processing path (SPEC section 3 rule 3).
        self.assertEqual(self._files_count(), 0)

        # List
        resp = self.client.get("/api/draft-room/drafts", headers=self._owner_headers())
        self.assertEqual(resp.status_code, 200)
        listing = resp.json()
        self.assertEqual(listing["page"], 1)
        self.assertEqual(listing["per_page"], 50)
        self.assertIn(draft_id, [d["id"] for d in listing["items"]])
        self.assertNotIn("storage_relpath", resp.text)
        self.assertNotIn("parsed_text", resp.text)

        # Detail
        resp = self.client.get(f"/api/draft-room/drafts/{draft_id}", headers=self._owner_headers())
        self.assertEqual(resp.status_code, 200)
        detail = resp.json()
        self.assertEqual(detail["summary"]["id"], draft_id)
        self.assertEqual(len(detail["inputs"]), 1)
        self.assertEqual(detail["inputs"][0]["id"], input_id)
        self.assertNotIn("storage_relpath", resp.text)
        lock_version = detail["summary"]["lock_version"]

        # Patch
        resp = self.client.patch(
            f"/api/draft-room/drafts/{draft_id}",
            json={"lock_version": lock_version, "title": "Renamed Draft"},
            headers=self._owner_headers(),
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["title"], "Renamed Draft")
        lock_version = resp.json()["lock_version"]

        # Manual revision create
        resp = self.client.post(
            f"/api/draft-room/drafts/{draft_id}/revisions",
            json={
                "base_revision_id": None,
                "lock_version": lock_version,
                "content_md": "# Draft body\n\nFirst revision.",
            },
            headers=self._owner_headers(),
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        revision = resp.json()
        revision_id = revision["summary"]["id"]
        self.assertEqual(revision["summary"]["revision_no"], 1)
        self.assertTrue(revision["summary"]["is_current"])
        self.assertEqual(revision["content_md"], "# Draft body\n\nFirst revision.")

        # Export (fact_status not_run in this release -> requires ack, -UNVERIFIED.md)
        resp = self.client.post(
            f"/api/draft-room/drafts/{draft_id}/revisions/{revision_id}/export"
            "?acknowledge_not_fact_checked=true",
            headers=self._owner_headers(),
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.text, "# Draft body\n\nFirst revision.")
        self.assertEqual(resp.headers["X-Draft-Fact-Status"], "not_run")
        self.assertIn("UNVERIFIED.md", resp.headers["content-disposition"])

        # Export without acknowledgement is rejected
        resp = self.client.post(
            f"/api/draft-room/drafts/{draft_id}/revisions/{revision_id}/export",
            headers=self._owner_headers(),
        )
        self.assertEqual(resp.status_code, 422, resp.text)
        self.assertEqual(resp.json()["code"], "export_ack_required")

        # Cancel the still-pending parse job so archive's active-job guard clears
        resp = self.client.post(
            f"/api/draft-room/drafts/{draft_id}/jobs/{job_id}/cancel",
            headers=self._owner_headers(),
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["status"], "cancelled")

        # Archive
        draft_now = self.client.get(
            f"/api/draft-room/drafts/{draft_id}", headers=self._owner_headers()
        ).json()
        resp = self.client.post(
            f"/api/draft-room/drafts/{draft_id}/archive",
            json={"lock_version": draft_now["summary"]["lock_version"]},
            headers=self._owner_headers(),
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["status"], "archived")

        # Restore -> needs_review (a current revision exists)
        resp = self.client.post(
            f"/api/draft-room/drafts/{draft_id}/restore",
            json={"lock_version": resp.json()["lock_version"]},
            headers=self._owner_headers(),
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["status"], "needs_review")

        # Delete
        resp = self.client.delete(
            f"/api/draft-room/drafts/{draft_id}", headers=self._owner_headers()
        )
        self.assertEqual(resp.status_code, 204, resp.text)

        resp = self.client.get(
            f"/api/draft-room/drafts/{draft_id}", headers=self._owner_headers()
        )
        self.assertEqual(resp.status_code, 404)

        # Security audit survives the cascade delete.
        conn = self._connection_pool.get_connection()
        try:
            row = conn.execute(
                "SELECT event_type FROM security_audit_log WHERE event_type = 'draft_deleted'"
            ).fetchone()
            self.assertIsNotNone(row)
        finally:
            self._connection_pool.release_connection(conn)


class TestManualRevisions(DraftRoomTestBase):
    def test_omitting_base_revision_id_is_rejected(self):
        """SPEC section 10.2 requires the base_revision_id KEY, null only when there is
        no current revision. Omitting it must not be read as "no base" -- that would
        silently skip the conflict check the field exists to enforce. Guards the
        Field(...) present-required declaration against a later "simplification" to
        = None, which no other test would catch.
        """
        draft_id = self._create_draft().json()["id"]

        resp = self.client.post(
            f"/api/draft-room/drafts/{draft_id}/revisions",
            json={"lock_version": 1, "content_md": "v1"},
            headers=self._owner_headers(),
        )
        self.assertEqual(resp.status_code, 422, resp.text)
        self.assertIn("base_revision_id", resp.text)

        # Explicit null on a draft with no current revision is the accepted form.
        resp = self.client.post(
            f"/api/draft-room/drafts/{draft_id}/revisions",
            json={"base_revision_id": None, "lock_version": 1, "content_md": "v1"},
            headers=self._owner_headers(),
        )
        self.assertEqual(resp.status_code, 201, resp.text)

    def test_manual_revisions_are_immutable_and_lock_versioned(self):
        draft_id = self._create_draft().json()["id"]

        resp = self.client.post(
            f"/api/draft-room/drafts/{draft_id}/revisions",
            json={"base_revision_id": None, "lock_version": 1, "content_md": "v1"},
            headers=self._owner_headers(),
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        rev1 = resp.json()
        rev1_id = rev1["summary"]["id"]
        self.assertEqual(rev1["summary"]["revision_no"], 1)

        draft = self.client.get(
            f"/api/draft-room/drafts/{draft_id}", headers=self._owner_headers()
        ).json()
        lock_version = draft["summary"]["lock_version"]

        # Stale lock_version -> 409
        resp = self.client.post(
            f"/api/draft-room/drafts/{draft_id}/revisions",
            json={"base_revision_id": rev1_id, "lock_version": lock_version + 99, "content_md": "v2-bad-lock"},
            headers=self._owner_headers(),
        )
        self.assertEqual(resp.status_code, 409, resp.text)

        # Wrong base_revision_id -> 409
        resp = self.client.post(
            f"/api/draft-room/drafts/{draft_id}/revisions",
            json={"base_revision_id": 999999, "lock_version": lock_version, "content_md": "v2-bad-base"},
            headers=self._owner_headers(),
        )
        self.assertEqual(resp.status_code, 409, resp.text)

        # Correct base + lock_version -> new revision 2
        resp = self.client.post(
            f"/api/draft-room/drafts/{draft_id}/revisions",
            json={"base_revision_id": rev1_id, "lock_version": lock_version, "content_md": "v2"},
            headers=self._owner_headers(),
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        rev2 = resp.json()
        self.assertEqual(rev2["summary"]["revision_no"], 2)
        self.assertTrue(rev2["summary"]["is_current"])

        # rev1 unchanged
        resp = self.client.get(
            f"/api/draft-room/drafts/{draft_id}/revisions/{rev1_id}", headers=self._owner_headers()
        )
        self.assertEqual(resp.json()["content_md"], "v1")
        self.assertFalse(resp.json()["summary"]["is_current"])

        # Exactly one is_current
        conn = self._connection_pool.get_connection()
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM draft_revisions WHERE draft_id = ? AND is_current = 1",
                (draft_id,),
            ).fetchone()[0]
            self.assertEqual(count, 1)
        finally:
            self._connection_pool.release_connection(conn)

        # Revision list is newest-first, never carries Markdown bodies.
        resp = self.client.get(
            f"/api/draft-room/drafts/{draft_id}/revisions", headers=self._owner_headers()
        )
        self.assertEqual(resp.status_code, 200)
        items = resp.json()["items"]
        self.assertEqual([r["revision_no"] for r in items], [2, 1])
        self.assertNotIn("content_md", resp.text)


class TestUploadValidation(DraftRoomTestBase):
    def test_duplicate_upload_returns_exact_409_body(self):
        draft_id = self._create_draft().json()["id"]
        content = b"identical bytes"
        first = self._upload_input(draft_id, content=content, filename="a.txt")
        self.assertEqual(first.status_code, 202, first.text)
        existing_input_id = first.json()["input"]["id"]

        second = self._upload_input(draft_id, content=content, filename="b.txt")
        self.assertEqual(second.status_code, 409, second.text)
        self.assertEqual(
            second.json(),
            {
                "detail": "input content already exists in this draft",
                "code": "duplicate_input",
                "context": {"existing_input_id": existing_input_id},
            },
        )

    def test_disallowed_extension_returns_415(self):
        draft_id = self._create_draft().json()["id"]
        resp = self._upload_input(
            draft_id, filename="virus.exe", content=b"MZ\x90\x00", content_type="application/octet-stream"
        )
        self.assertEqual(resp.status_code, 415, resp.text)
        self.assertEqual(resp.json()["code"], "unsupported_input")

    def test_bad_signature_returns_415(self):
        draft_id = self._create_draft().json()["id"]
        resp = self._upload_input(
            draft_id, filename="fake.pdf", content=b"not a real pdf", content_type="application/pdf"
        )
        self.assertEqual(resp.status_code, 415, resp.text)
        self.assertEqual(resp.json()["code"], "unsupported_input")

    def test_malformed_ooxml_returns_415(self):
        draft_id = self._create_draft().json()["id"]
        resp = self._upload_input(
            draft_id,
            filename="broken.docx",
            content=_minimal_docx_bytes(include_required_member=False),
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        self.assertEqual(resp.status_code, 415, resp.text)
        self.assertEqual(resp.json()["code"], "unsupported_input")

    def test_wellformed_ooxml_is_accepted(self):
        draft_id = self._create_draft().json()["id"]
        resp = self._upload_input(
            draft_id,
            filename="good.docx",
            content=_minimal_docx_bytes(include_required_member=True),
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        self.assertEqual(resp.status_code, 202, resp.text)

    def test_oversize_upload_returns_413(self):
        settings.max_file_size_mb = 1
        draft_id = self._create_draft().json()["id"]
        big_content = b"x" * (2 * 1024 * 1024)
        resp = self._upload_input(draft_id, content=big_content, filename="big.txt")
        self.assertEqual(resp.status_code, 413, resp.text)
        self.assertEqual(resp.json()["code"], "input_too_large")

    def test_max_inputs_limit_returns_413(self):
        settings.draft_max_inputs = 1
        draft_id = self._create_draft().json()["id"]
        first = self._upload_input(draft_id, content=b"one", filename="one.txt")
        self.assertEqual(first.status_code, 202, first.text)
        second = self._upload_input(draft_id, content=b"two", filename="two.txt")
        self.assertEqual(second.status_code, 413, second.text)

    def test_max_total_bytes_limit_returns_413(self):
        settings.draft_max_total_input_mb = 1
        draft_id = self._create_draft().json()["id"]
        first = self._upload_input(draft_id, content=b"x" * 900_000, filename="one.txt")
        self.assertEqual(first.status_code, 202, first.text)
        second = self._upload_input(draft_id, content=b"y" * 900_000, filename="two.txt")
        self.assertEqual(second.status_code, 413, second.text)


class TestCapabilitiesAndPagination(DraftRoomTestBase):
    def test_capabilities_advertises_unavailable_future_features(self):
        resp = self.client.get("/api/draft-room/capabilities", headers=self._owner_headers())
        self.assertEqual(resp.status_code, 200, resp.text)
        caps = resp.json()
        self.assertTrue(caps["enabled"])
        self.assertEqual(caps["export_formats"], ["md"])
        for key in (
            "compile_available",
            "findings_available",
            "claims_available",
            "evidence_available",
            "ready_available",
            "promote_available",
        ):
            self.assertFalse(caps[key], key)

    def test_list_pagination_shape_and_stable_ordering(self):
        for i in range(3):
            resp = self._create_draft(title=f"Draft {i}")
            self.assertEqual(resp.status_code, 201, resp.text)

        resp = self.client.get(
            "/api/draft-room/drafts?page=1&per_page=2", headers=self._owner_headers()
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["page"], 1)
        self.assertEqual(body["per_page"], 2)
        self.assertEqual(body["total"], 3)
        self.assertEqual(len(body["items"]), 2)

        resp2 = self.client.get(
            "/api/draft-room/drafts?page=2&per_page=2", headers=self._owner_headers()
        )
        self.assertEqual(len(resp2.json()["items"]), 1)
        ids_page1 = {d["id"] for d in body["items"]}
        ids_page2 = {d["id"] for d in resp2.json()["items"]}
        self.assertEqual(len(ids_page1 & ids_page2), 0)


class TestSSE(DraftRoomTestBase):
    def test_events_stream_emits_subscribed_without_content(self):
        """Calls the route function directly rather than through
        ``TestClient.stream()``: the repo's established pattern for SSE
        endpoints (see ``test_wiki_routes_di_migration.py``,
        ``test_wiki_events_stream_runs_permission_check``) because driving a
        real never-ending SSE body through TestClient's sync-over-async
        portal deadlocks — the generator's heartbeat loop never lets the
        underlying ASGI call return, so ``TestClient.stream()`` blocks
        forever waiting for the response to finish rather than treating the
        first chunk as enough."""
        import asyncio

        from fastapi.responses import StreamingResponse

        from app.api.routes.draft_room import draft_room_events_stream

        draft_id = self._create_draft().json()["id"]

        token = create_access_token(
            self.OWNER_ID, "owner", "member", client_fingerprint=compute_client_fingerprint("")
        )
        fake_request = MagicMock()
        fake_request.app.state.db_pool = self._connection_pool
        fake_request.headers = {"authorization": f"Bearer {token}", "user-agent": ""}
        fake_request.cookies = {}

        response = asyncio.run(
            draft_room_events_stream(request=fake_request, draft_id=draft_id)
        )
        self.assertIsInstance(response, StreamingResponse)
        self.assertEqual(response.headers["Cache-Control"], "no-cache")

        async def _first_chunk():
            async for chunk in response.body_iterator:
                return chunk
            return None

        first = asyncio.run(_first_chunk())
        self.assertIsNotNone(first)
        self.assertTrue(first.startswith("data:"))
        payload = json.loads(first[len("data:") :].strip())
        self.assertEqual(payload["type"], "subscribed")
        self.assertEqual(payload["draft_id"], draft_id)
        self.assertNotIn("manuscript", json.dumps(payload))


if __name__ == "__main__":
    unittest.main()
