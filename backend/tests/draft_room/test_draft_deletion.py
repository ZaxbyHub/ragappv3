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
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

try:
    import lancedb  # noqa: F401
except ImportError:
    sys.modules["lancedb"] = types.ModuleType("lancedb")

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


if __name__ == "__main__":
    unittest.main()
