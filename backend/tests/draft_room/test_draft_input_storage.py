"""Tests for app.services.draft_input_storage (issue #435, SPEC section 6.1/6.2).

Exercises the streaming upload validator, the path-resolution guard, the
two-phase tombstone primitives, and startup reconciliation against a real
temp-directory filesystem — no mocks on the filesystem boundary itself.
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

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

try:
    import lancedb  # noqa: F401
except ImportError:
    sys.modules["lancedb"] = types.ModuleType("lancedb")

from app.services.draft_input_storage import (
    DraftInputPathError,
    DraftInputStorage,
    DraftInputTooLargeError,
    DraftInputUnsupportedError,
    StagedUpload,
)


class FakeUpload:
    """Minimal stand-in for a FastAPI ``UploadFile``: async chunked ``.read()``."""

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


def _incoming_entries(root: Path) -> list[Path]:
    incoming = root / ".incoming"
    if not incoming.is_dir():
        return []
    return list(incoming.iterdir())


def _trash_entries(root: Path) -> list[Path]:
    trash = root / ".trash"
    if not trash.is_dir():
        return []
    return list(trash.iterdir())


class DraftInputStorageTestBase(unittest.TestCase):
    def setUp(self):
        self._temp_dir = tempfile.mkdtemp()
        self.root = Path(self._temp_dir) / "draft-room"
        self.storage = DraftInputStorage(self.root)

    def tearDown(self):
        shutil.rmtree(self._temp_dir, ignore_errors=True)

    def stage(self, filename: str, content: bytes, *, allowed=None, max_bytes=10_000_000):
        upload = FakeUpload(filename, content)
        allowed_extensions = allowed if allowed is not None else {Path(filename).suffix.lower()}
        return asyncio.run(
            self.storage.stage_upload(
                upload, allowed_extensions=allowed_extensions, max_file_bytes=max_bytes
            )
        )


class TestStageAndFinalizeHappyPath(DraftInputStorageTestBase):
    def test_staged_file_lands_at_expected_relpath_with_matching_bytes_and_hash(self):
        content = b"Hello, Draft Room.\nSecond line.\n"
        staged = self.stage("note.txt", content)

        self.assertIsInstance(staged, StagedUpload)
        self.assertEqual(staged.size_bytes, len(content))
        self.assertEqual(staged.content_sha256, hashlib.sha256(content).hexdigest())
        self.assertEqual(staged.extension, ".txt")
        self.assertEqual(staged.original_name, "note.txt")
        self.assertTrue(staged.stored_name.endswith(".txt"))
        self.assertNotIn("note", staged.stored_name)  # never the client filename

        relpath = f"7/3/inputs/9/{staged.stored_name}"
        self.storage.finalize(staged, relpath)

        self.assertTrue(self.storage.exists(relpath))
        self.assertEqual(self.storage.read_text(relpath), content.decode("utf-8"))
        final_path = self.storage.resolve(relpath)
        self.assertEqual(final_path.read_bytes(), content)

        # .incoming must be empty once the file has been finalized.
        self.assertEqual(_incoming_entries(self.root), [])


class TestPathSafety(DraftInputStorageTestBase):
    def test_dot_dot_traversal_rejected(self):
        with self.assertRaises(DraftInputPathError):
            self.storage.resolve("../../etc/passwd")
        outside = Path(self._temp_dir).parent / "passwd"
        self.assertFalse(outside.exists())

    def test_absolute_path_rejected(self):
        with self.assertRaises(DraftInputPathError):
            self.storage.resolve("/etc/passwd")

    def test_embedded_dot_dot_component_rejected(self):
        with self.assertRaises(DraftInputPathError):
            self.storage.resolve("1/2/../../../etc/passwd")

    def test_empty_relpath_rejected(self):
        with self.assertRaises(DraftInputPathError):
            self.storage.resolve("")

    def test_symlink_escape_rejected(self):
        outside_dir = tempfile.mkdtemp()
        try:
            secret = Path(outside_dir) / "secret.txt"
            secret.write_text("private", encoding="utf-8")

            link_parent = self.root / "1" / "2" / "inputs" / "3"
            link_parent.mkdir(parents=True)
            link_path = link_parent / "escape.txt"
            os.symlink(secret, link_path)

            with self.assertRaises(DraftInputPathError):
                self.storage.resolve("1/2/inputs/3/escape.txt")
        finally:
            shutil.rmtree(outside_dir, ignore_errors=True)


class TestOversizeUpload(DraftInputStorageTestBase):
    def test_oversize_upload_aborts_mid_stream_and_leaves_no_partial_file(self):
        content = b"x" * 5000
        with self.assertRaises(DraftInputTooLargeError):
            self.stage("big.txt", content, max_bytes=10)

        self.assertEqual(_incoming_entries(self.root), [])


class TestValidationRejections(DraftInputStorageTestBase):
    def test_disallowed_extension_rejected(self):
        with self.assertRaises(DraftInputUnsupportedError):
            self.stage("note.txt", b"hello", allowed={".pdf"})
        self.assertEqual(_incoming_entries(self.root), [])

    def test_mismatched_magic_bytes_rejected(self):
        with self.assertRaises(DraftInputUnsupportedError):
            self.stage("fake.pdf", b"NOTAPDFBODY", allowed={".pdf"})
        self.assertEqual(_incoming_entries(self.root), [])

    def test_malformed_ooxml_rejected(self):
        # Passes the generic ZIP magic-byte check but is not a real docx.
        bogus_zip = b"PK\x03\x04" + b"not a real zip central directory" * 4
        with self.assertRaises(DraftInputUnsupportedError):
            self.stage("bad.docx", bogus_zip, allowed={".docx"})
        self.assertEqual(_incoming_entries(self.root), [])


class TestDiscard(DraftInputStorageTestBase):
    def test_discard_leaves_incoming_clean(self):
        staged = self.stage("note.txt", b"hello")
        self.assertEqual(len(_incoming_entries(self.root)), 1)
        self.storage.discard(staged)
        self.assertEqual(_incoming_entries(self.root), [])


class TestTombstoneFlow(DraftInputStorageTestBase):
    def _finalized(self, content=b"payload"):
        staged = self.stage("note.txt", content)
        relpath = f"1/2/inputs/3/{staged.stored_name}"
        self.storage.finalize(staged, relpath)
        return relpath, content

    def test_tombstone_moves_file_out_of_original_path(self):
        relpath, _content = self._finalized()
        token = self.storage.tombstone(relpath)

        self.assertFalse(self.storage.exists(relpath))
        self.assertEqual(len(_trash_entries(self.root)), 1)

        self.storage.commit_tombstone(token)
        self.assertEqual(_trash_entries(self.root), [])

    def test_restore_tombstone_puts_the_file_back_and_it_stays_readable(self):
        relpath, content = self._finalized()
        token = self.storage.tombstone(relpath)

        self.storage.restore_tombstone(token, relpath)

        self.assertTrue(self.storage.exists(relpath))
        self.assertEqual(self.storage.read_text(relpath), content.decode("utf-8"))
        self.assertEqual(_trash_entries(self.root), [])


class TestReconcile(DraftInputStorageTestBase):
    def _touch_old(self, path: Path, *, age_seconds: float = 100_000):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("stale", encoding="utf-8")
        old = os_time() - age_seconds
        os.utime(path, (old, old))

    def test_reconcile_removes_stale_incoming_and_trash_and_preserves_live_drafts(self):
        stale_incoming = self.root / ".incoming" / "stale.part"
        self._touch_old(stale_incoming)
        stale_trash = self.root / ".trash" / "stale-token"
        self._touch_old(stale_trash)

        # A fresh .incoming entry should survive a short max_age.
        fresh_incoming = self.root / ".incoming" / "fresh.part"
        fresh_incoming.parent.mkdir(parents=True, exist_ok=True)
        fresh_incoming.write_text("fresh", encoding="utf-8")

        # A live draft's directory must be preserved...
        live_dir = self.root / "1" / "5" / "inputs" / "1"
        live_dir.mkdir(parents=True)
        (live_dir / "abc.txt").write_text("keep me", encoding="utf-8")

        # ...while an orphan draft directory is removed immediately regardless of age.
        orphan_dir = self.root / "1" / "6" / "inputs" / "1"
        orphan_dir.mkdir(parents=True)
        (orphan_dir / "def.txt").write_text("remove me", encoding="utf-8")

        counts = self.storage.reconcile({(1, 5)}, max_age_seconds=10)

        self.assertEqual(counts["incoming_removed"], 1)
        self.assertEqual(counts["trash_removed"], 1)
        self.assertEqual(counts["orphan_dirs_removed"], 1)

        self.assertFalse(stale_incoming.exists())
        self.assertFalse(stale_trash.exists())
        self.assertTrue(fresh_incoming.exists())
        self.assertTrue((self.root / "1" / "5").is_dir())
        self.assertFalse((self.root / "1" / "6").exists())


def os_time() -> float:
    import time

    return time.time()


class TestNoFilesRowSideEffect(unittest.TestCase):
    """A draft input must never create a `files` row (SPEC section 6: no ingestion)."""

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
        self.conn.execute("INSERT OR IGNORE INTO vaults (id, name, description) VALUES (1,'V','')")
        self.conn.commit()

        self.root = Path(self._temp_dir) / "draft-room"
        self.storage = DraftInputStorage(self.root)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self._temp_dir, ignore_errors=True)

    def test_upload_and_finalize_does_not_touch_files_table(self):
        from app.services.draft_store import DraftStore

        store = DraftStore(self.conn)
        draft = store.create_draft(
            vault_id=1,
            created_by=1,
            title="My Draft",
            mode="compose",
            tier="standard",
            brief_json="{}",
        )

        before = self.conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]

        content = b"draft input content"
        upload = FakeUpload("manuscript.txt", content)
        staged = asyncio.run(
            self.storage.stage_upload(
                upload, allowed_extensions={".txt"}, max_file_bytes=10_000_000
            )
        )
        record = store.reserve_input(
            draft_id=draft.id,
            owner_id=1,
            role="manuscript",
            authority="unknown",
            as_of_date=None,
            original_name=staged.original_name,
            stored_name=staged.stored_name,
            extension=staged.extension,
            media_type=staged.media_type,
            size_bytes=staged.size_bytes,
            content_sha256=staged.content_sha256,
            max_inputs=10,
            max_total_input_bytes=10_000_000,
        )
        self.storage.finalize(staged, record.storage_relpath)

        after = self.conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        self.assertEqual(before, after)
        self.assertTrue(self.storage.exists(record.storage_relpath))


if __name__ == "__main__":
    unittest.main()
