"""Tests for app.services.draft_deletion (issue #435, SPEC section 6.1/6.3).

Exercises the two-phase tombstone deletion flow against a real SQLite database
and a real temp-directory filesystem: input deletion, whole-draft deletion,
the in-use/active-parse-job conflicts, the rollback-restore durability guard,
and the vault/user cascades.
"""

import asyncio
import hashlib
import os
import shutil
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from queue import Empty, Queue
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

try:
    import lancedb  # noqa: F401
except ImportError:
    sys.modules["lancedb"] = types.ModuleType("lancedb")

from fastapi.testclient import TestClient

from app.api.deps import get_db, get_vector_store
from app.config import settings
from app.main import app
from app.security import csrf_protect
from app.services.auth_service import compute_client_fingerprint, create_access_token
from app.services.draft_deletion import DraftDeletionService
from app.services.draft_input_storage import DraftInputStorage
from app.services.draft_store import DraftConflictError, DraftNotFoundError, DraftStore


class FakeUpload:
    def __init__(self, filename: str, content: bytes) -> None:
        self.filename = filename
        self._content = content
        self._offset = 0

    async def read(self, size: int = -1) -> bytes:
        if size < 0:
            chunk = self._content[self._offset :]
            self._offset = len(self._content)
            return chunk
        chunk = self._content[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk


class DraftDeletionTestBase(unittest.TestCase):
    def setUp(self):
        self._temp_dir = tempfile.mkdtemp()
        self._db_path = str(Path(self._temp_dir) / "app.db")

        from app.models.database import init_db, run_migrations

        init_db(self._db_path)
        run_migrations(self._db_path)

        self.conn = sqlite3.connect(self._db_path)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute(
            "INSERT OR IGNORE INTO users (id, username, hashed_password, full_name, role, is_active) "
            "VALUES (1,'owner','hash','Owner','member',1)"
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO users (id, username, hashed_password, full_name, role, is_active) "
            "VALUES (2,'owner2','hash','Owner Two','member',1)"
        )
        self.conn.execute("INSERT OR IGNORE INTO vaults (id, name, description) VALUES (1,'V','')")
        self.conn.execute("INSERT OR IGNORE INTO vaults (id, name, description) VALUES (2,'V2','')")
        self.conn.commit()

        self.store = DraftStore(self.conn)
        self.root = Path(self._temp_dir) / "draft-room"
        self.storage = DraftInputStorage(self.root)
        self.deletion = DraftDeletionService(self.storage)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self._temp_dir, ignore_errors=True)

    def make_draft(self, *, owner_id=1, vault_id=1, title="Draft"):
        return self.store.create_draft(
            vault_id=vault_id,
            created_by=owner_id,
            title=title,
            mode="compose",
            tier="standard",
            brief_json="{}",
        )

    def add_input(self, draft_id, *, owner_id=1, name="a.txt", content=b"hello world"):
        upload = FakeUpload(name, content)
        staged = asyncio.run(
            self.storage.stage_upload(
                upload, allowed_extensions={".txt"}, max_file_bytes=10_000_000
            )
        )
        record = self.store.reserve_input(
            draft_id=draft_id,
            owner_id=owner_id,
            role="reference",
            authority="unknown",
            as_of_date=None,
            original_name=staged.original_name,
            stored_name=staged.stored_name,
            extension=staged.extension,
            media_type=staged.media_type,
            size_bytes=staged.size_bytes,
            content_sha256=staged.content_sha256,
            max_inputs=100,
            max_total_input_bytes=10_000_000,
        )
        self.storage.finalize(staged, record.storage_relpath)
        return record

    def insert_job(self, *, draft_id, owner_id, vault_id, job_type, status, input_id=None):
        cur = self.conn.execute(
            "INSERT INTO draft_jobs (draft_id, vault_id, created_by, job_type, input_id, "
            "status, max_model_calls, timeout_seconds) VALUES (?,?,?,?,?,?,0,60)",
            (draft_id, vault_id, owner_id, job_type, input_id, status),
        )
        self.conn.commit()
        return int(cur.lastrowid)


class TestDeleteInput(DraftDeletionTestBase):
    def test_happy_path_removes_file_and_row(self):
        draft = self.make_draft()
        record = self.add_input(draft.id)
        relpath = record.storage_relpath

        self.deletion.delete_input(
            self.store, draft_id=draft.id, owner_id=1, input_id=record.id
        )

        self.assertFalse(self.storage.exists(relpath))
        self.assertEqual(self.store.list_inputs(draft_id=draft.id, owner_id=1), [])
        self.assertEqual(list((self.root / ".trash").iterdir()) if (self.root / ".trash").is_dir() else [], [])

    def test_input_in_use_by_completed_compile_is_refused_and_file_kept(self):
        draft = self.make_draft()
        record = self.add_input(draft.id)
        self.insert_job(
            draft_id=draft.id,
            owner_id=1,
            vault_id=1,
            job_type="compile",
            status="completed",
            input_id=record.id,
        )

        with self.assertRaises(DraftConflictError) as ctx:
            self.deletion.delete_input(
                self.store, draft_id=draft.id, owner_id=1, input_id=record.id
            )
        self.assertEqual(ctx.exception.code, "input_in_use")
        self.assertTrue(self.storage.exists(record.storage_relpath))

    def test_active_parse_job_is_refused_and_file_kept(self):
        draft = self.make_draft()
        record = self.add_input(draft.id)
        self.insert_job(
            draft_id=draft.id,
            owner_id=1,
            vault_id=1,
            job_type="parse_input",
            status="pending",
            input_id=record.id,
        )

        with self.assertRaises(DraftConflictError) as ctx:
            self.deletion.delete_input(
                self.store, draft_id=draft.id, owner_id=1, input_id=record.id
            )
        self.assertEqual(ctx.exception.code, "input_in_use")
        self.assertTrue(self.storage.exists(record.storage_relpath))

    def test_db_failure_restores_the_tombstoned_file(self):
        draft = self.make_draft()
        record = self.add_input(draft.id)
        relpath = record.storage_relpath
        content = self.storage.resolve(relpath).read_bytes()

        with patch.object(
            self.store, "delete_input_row", side_effect=RuntimeError("boom")
        ):
            with self.assertRaises(RuntimeError):
                self.deletion.delete_input(
                    self.store, draft_id=draft.id, owner_id=1, input_id=record.id
                )

        self.assertTrue(self.storage.exists(relpath))
        self.assertEqual(self.storage.resolve(relpath).read_bytes(), content)
        trash_dir = self.root / ".trash"
        self.assertEqual(list(trash_dir.iterdir()) if trash_dir.is_dir() else [], [])
        # Row must still be present too — bytes and rows agree.
        self.assertEqual(len(self.store.list_inputs(draft_id=draft.id, owner_id=1)), 1)


class TestDeleteDraft(DraftDeletionTestBase):
    def test_whole_draft_delete_removes_every_input_and_all_rows(self):
        draft = self.make_draft()
        rec1 = self.add_input(draft.id, name="one.txt", content=b"one")
        rec2 = self.add_input(draft.id, name="two.txt", content=b"two")

        self.deletion.delete_draft(self.store, draft_id=draft.id, owner_id=1)

        self.assertFalse(self.storage.exists(rec1.storage_relpath))
        self.assertFalse(self.storage.exists(rec2.storage_relpath))
        with self.assertRaises(DraftNotFoundError):
            self.store.get_draft(draft.id, 1)

        project_dir = self.root / "1" / str(draft.id)
        self.assertFalse(project_dir.exists())

    def test_running_job_blocks_deletion_with_conflict(self):
        draft = self.make_draft()
        self.insert_job(
            draft_id=draft.id,
            owner_id=1,
            vault_id=1,
            job_type="compile",
            status="running",
        )

        with self.assertRaises(DraftConflictError) as ctx:
            self.deletion.delete_draft(self.store, draft_id=draft.id, owner_id=1)
        self.assertEqual(ctx.exception.code, "active_job")
        # Draft must still exist.
        self.store.get_draft(draft.id, 1)

    def test_pending_jobs_are_cancelled_then_deletion_proceeds(self):
        draft = self.make_draft()
        self.insert_job(
            draft_id=draft.id,
            owner_id=1,
            vault_id=1,
            job_type="compile",
            status="pending",
        )

        self.deletion.delete_draft(self.store, draft_id=draft.id, owner_id=1)

        with self.assertRaises(DraftNotFoundError):
            self.store.get_draft(draft.id, 1)


class TestCascades(DraftDeletionTestBase):
    def test_vault_delete_purges_bytes_for_every_affected_draft(self):
        d1 = self.make_draft(vault_id=1, title="A")
        d2 = self.make_draft(vault_id=1, title="B")
        r1 = self.add_input(d1.id)
        r2 = self.add_input(d2.id)

        purged = self.deletion.delete_drafts_for_vault(self.store, 1)

        self.assertEqual(purged, 2)
        self.assertFalse(self.storage.exists(r1.storage_relpath))
        self.assertFalse(self.storage.exists(r2.storage_relpath))
        with self.assertRaises(DraftNotFoundError):
            self.store.get_draft(d1.id, 1)
        with self.assertRaises(DraftNotFoundError):
            self.store.get_draft(d2.id, 1)

    def test_user_delete_purges_bytes_for_every_owned_draft(self):
        d1 = self.make_draft(owner_id=1, vault_id=1, title="A")
        d2 = self.make_draft(owner_id=1, vault_id=2, title="B")
        r1 = self.add_input(d1.id, owner_id=1)
        r2 = self.add_input(d2.id, owner_id=1)

        purged = self.deletion.delete_drafts_for_user(self.store, 1)

        self.assertEqual(purged, 2)
        self.assertFalse(self.storage.exists(r1.storage_relpath))
        self.assertFalse(self.storage.exists(r2.storage_relpath))

    def test_one_failing_draft_does_not_abort_the_cascade(self):
        d1 = self.make_draft(vault_id=1, title="A")
        d2 = self.make_draft(vault_id=1, title="B")
        self.add_input(d1.id)
        self.add_input(d2.id)

        real_delete_draft_row = self.store.delete_draft_row

        def flaky_delete(*, draft_id, owner_id):
            if draft_id == d1.id:
                raise RuntimeError("simulated failure")
            return real_delete_draft_row(draft_id=draft_id, owner_id=owner_id)

        with patch.object(self.store, "delete_draft_row", side_effect=flaky_delete):
            purged = self.deletion.delete_drafts_for_vault(self.store, 1)

        self.assertEqual(purged, 1)
        # d1 survives because its own deletion failed and was rolled back.
        self.store.get_draft(d1.id, 1)
        with self.assertRaises(DraftNotFoundError):
            self.store.get_draft(d2.id, 1)


class _RouteTestPool:
    """Thread-safe SQLite pool matching the ``get_connection``/
    ``release_connection`` idiom the route tests need for the ``get_db``
    override (copied inline — mirrors ``tests/_db_pool.py`` and
    ``tests/draft_room/test_draft_routes.py`` — this module lives one
    directory deeper than ``tests/_db_pool.py`` so a bare-name import would
    need an extra ``sys.path`` entry; a local copy avoids that)."""

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


class DraftRouteWiringTestBase(unittest.TestCase):
    """Route-level tests proving DELETE /api/vaults/{id} and DELETE
    /api/users/{id} purge Draft Room private bytes (issue #435), not just
    that the service methods work in isolation. Harness mirrors
    ``test_tags_routes.py``. Vault ids are >= 501 to avoid colliding with the
    ``Default`` vault (id=1) seeded by
    ``migrate_assign_orphan_users_to_default_vault``.
    """

    def setUp(self):
        self.client = TestClient(app)
        self.client.headers["user-agent"] = ""
        self._temp_dir = tempfile.mkdtemp()

        self._original_jwt_secret = settings.jwt_secret_key
        self._original_users_enabled = settings.users_enabled
        self._original_data_dir = settings.data_dir

        settings.data_dir = Path(self._temp_dir)
        settings.jwt_secret_key = os.urandom(32).hex()
        settings.users_enabled = True

        self._db_path = str(Path(self._temp_dir) / "app.db")

        from app.models.database import _pool_cache, _pool_cache_lock

        with _pool_cache_lock:
            for _path, pool in list(_pool_cache.items()):
                pool.close_all()
            _pool_cache.clear()

        from app.models.database import init_db, run_migrations

        init_db(self._db_path)
        run_migrations(self._db_path)
        self._connection_pool = _RouteTestPool(self._db_path)

        def override_get_db():
            conn = self._connection_pool.get_connection()
            try:
                yield conn
            finally:
                self._connection_pool.release_connection(conn)

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[csrf_protect] = lambda: "test-csrf"

        self._mock_vector_store = MagicMock()
        self._mock_vector_store.delete_by_vault = AsyncMock(return_value=0)
        app.dependency_overrides[get_vector_store] = lambda: self._mock_vector_store

        conn = self._connection_pool.get_connection()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO users (id, username, hashed_password, full_name, role, is_active) "
                "VALUES (100,'root-admin','h','Root','superadmin',1)"
            )
            conn.execute(
                "INSERT OR IGNORE INTO users (id, username, hashed_password, full_name, role, is_active) "
                "VALUES (101,'root-admin-2','h','Root2','superadmin',1)"
            )
            conn.execute(
                "INSERT OR IGNORE INTO users (id, username, hashed_password, full_name, role, is_active) "
                "VALUES (200,'owner-a','h','Owner A','member',1)"
            )
            conn.execute(
                "INSERT OR IGNORE INTO users (id, username, hashed_password, full_name, role, is_active) "
                "VALUES (201,'owner-b','h','Owner B','member',1)"
            )
            conn.execute(
                "INSERT OR IGNORE INTO vaults (id, name, description) VALUES (501,'VaultA','')"
            )
            conn.execute(
                "INSERT OR IGNORE INTO vaults (id, name, description) VALUES (502,'VaultB','')"
            )
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)

        self.root = Path(self._temp_dir) / "draft-room"
        self.storage = DraftInputStorage(self.root)

    def tearDown(self):
        from app.models.database import _pool_cache, _pool_cache_lock

        with _pool_cache_lock:
            for _path, pool in list(_pool_cache.items()):
                pool.close_all()
            _pool_cache.clear()

        settings.jwt_secret_key = self._original_jwt_secret
        settings.users_enabled = self._original_users_enabled
        settings.data_dir = self._original_data_dir
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(csrf_protect, None)
        app.dependency_overrides.pop(get_vector_store, None)
        if hasattr(self, "_connection_pool"):
            self._connection_pool.close_all()
        shutil.rmtree(self._temp_dir, ignore_errors=True)

    def _headers(self, user_id, username, role):
        token = create_access_token(
            user_id, username, role, client_fingerprint=compute_client_fingerprint("")
        )
        return {"Authorization": f"Bearer {token}"}

    def _make_draft_with_input(self, *, owner_id, vault_id, title="Draft"):
        conn = self._connection_pool.get_connection()
        try:
            store = DraftStore(conn)
            draft = store.create_draft(
                vault_id=vault_id,
                created_by=owner_id,
                title=title,
                mode="compose",
                tier="standard",
                brief_json="{}",
            )
            upload = FakeUpload("a.txt", b"private manuscript bytes")
            staged = asyncio.run(
                self.storage.stage_upload(
                    upload, allowed_extensions={".txt"}, max_file_bytes=10_000_000
                )
            )
            record = store.reserve_input(
                draft_id=draft.id,
                owner_id=owner_id,
                role="reference",
                authority="unknown",
                as_of_date=None,
                original_name=staged.original_name,
                stored_name=staged.stored_name,
                extension=staged.extension,
                media_type=staged.media_type,
                size_bytes=staged.size_bytes,
                content_sha256=staged.content_sha256,
                max_inputs=100,
                max_total_input_bytes=10_000_000,
            )
            self.storage.finalize(staged, record.storage_relpath)
            return draft, record
        finally:
            self._connection_pool.release_connection(conn)

    def _draft_row_count(self, draft_id):
        conn = self._connection_pool.get_connection()
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM drafts WHERE id = ?", (draft_id,)
            ).fetchone()
            return row[0]
        finally:
            self._connection_pool.release_connection(conn)


class TestVaultDeleteWiresDraftRoomPurge(DraftRouteWiringTestBase):
    def test_deleting_vault_purges_files_and_rows_and_spares_other_vault(self):
        draft_in, input_in = self._make_draft_with_input(owner_id=200, vault_id=501)
        draft_other, input_other = self._make_draft_with_input(owner_id=200, vault_id=502)

        resp = self.client.delete(
            "/api/vaults/501", headers=self._headers(100, "root-admin", "superadmin")
        )
        self.assertEqual(resp.status_code, 200, resp.text)

        # Purged vault's draft: file gone, row gone, owner/draft dir gone.
        self.assertFalse(self.storage.exists(input_in.storage_relpath))
        self.assertEqual(self._draft_row_count(draft_in.id), 0)
        self.assertFalse((self.root / "200" / str(draft_in.id)).exists())

        # Isolation guard: a draft in a DIFFERENT vault is untouched.
        self.assertTrue(self.storage.exists(input_other.storage_relpath))
        self.assertEqual(self._draft_row_count(draft_other.id), 1)

    def test_purge_failure_does_not_block_vault_deletion(self):
        draft_in, input_in = self._make_draft_with_input(owner_id=200, vault_id=501)

        with patch.object(
            DraftDeletionService,
            "delete_drafts_for_vault",
            side_effect=RuntimeError("simulated purge failure"),
        ):
            with self.assertLogs("app.api.routes.vaults", level="WARNING") as logs:
                resp = self.client.delete(
                    "/api/vaults/501",
                    headers=self._headers(100, "root-admin", "superadmin"),
                )

        # Vault deletion must proceed despite the purge failure.
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(self._draft_row_count(draft_in.id), 0)
        self.assertTrue(
            any("draft_room_vault_purge_failed" in line for line in logs.output)
        )
        # File is orphaned in this simulated-failure scenario (expected: the
        # purge itself failed), but the parent deletion was not blocked.
        self.assertTrue(self.storage.exists(input_in.storage_relpath))


class TestUserDeleteWiresDraftRoomPurge(DraftRouteWiringTestBase):
    def test_deleting_user_purges_files_and_rows_and_spares_other_user(self):
        draft_a, input_a = self._make_draft_with_input(owner_id=200, vault_id=501)
        draft_b, input_b = self._make_draft_with_input(owner_id=201, vault_id=501)

        resp = self.client.delete(
            "/api/users/200", headers=self._headers(100, "root-admin", "superadmin")
        )
        self.assertEqual(resp.status_code, 200, resp.text)

        # Deleted user's draft: file gone, row gone.
        self.assertFalse(self.storage.exists(input_a.storage_relpath))
        self.assertEqual(self._draft_row_count(draft_a.id), 0)
        self.assertFalse((self.root / "200" / str(draft_a.id)).exists())

        # Isolation guard: a draft owned by a DIFFERENT user is untouched.
        self.assertTrue(self.storage.exists(input_b.storage_relpath))
        self.assertEqual(self._draft_row_count(draft_b.id), 1)

    def test_purge_failure_does_not_block_user_deletion(self):
        draft_a, input_a = self._make_draft_with_input(owner_id=200, vault_id=501)

        with patch.object(
            DraftDeletionService,
            "delete_drafts_for_user",
            side_effect=RuntimeError("simulated purge failure"),
        ):
            with self.assertLogs("app.api.routes.users", level="WARNING") as logs:
                resp = self.client.delete(
                    "/api/users/200",
                    headers=self._headers(100, "root-admin", "superadmin"),
                )

        # User deletion must proceed despite the purge failure.
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(self._draft_row_count(draft_a.id), 0)
        self.assertTrue(
            any("draft_room_user_purge_failed" in line for line in logs.output)
        )
        self.assertTrue(self.storage.exists(input_a.storage_relpath))


if __name__ == "__main__":
    unittest.main()
