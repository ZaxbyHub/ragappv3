"""Arm's-length checks for issue #785: single-input Draft Room delete safety.

Each test reproduces one leg of the broken delete flow against the real
service/storage/SQLite stack (no mocks), using the harness proven by
``test_draft_deletion.py``:

* AC1 -- an input whose bytes were cited into a completed compile (a
  ``draft_evidence`` row with ``source_kind='draft_input'``; the compile job
  itself carries ``input_id`` NULL, the shape every production compile writer
  writes) must be refused with ``input_in_use``, never crash into a raw
  ``sqlite3.IntegrityError`` when the evidence FK tries to ``SET NULL`` a
  CHECK-guarded column.
* AC3 -- a pending/running compile on the draft must block single-input
  delete with ``active_job`` (bytes and row both kept).
* AC4 -- deleting an input whose bytes are already absent must still remove
  the row (idempotent-by-row), not raise ``FileNotFoundError``.
* AC5 -- a crash window between tombstone and row delete (tombstone purged by
  ``reconcile``) must not permanently wedge the input; the retried delete
  still succeeds.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from draft_room.test_draft_deletion import DraftDeletionTestBase


class TestN01InputDelete(DraftDeletionTestBase):
    def insert_compile_job(self, *, draft_id, owner_id=1, vault_id=1, status):
        """Insert a compile job exactly the way production writers do: with
        ``input_id`` left NULL (both the route enqueue and the worker retry
        INSERT omit the column; only parse jobs ever set it)."""
        cur = self.conn.execute(
            "INSERT INTO draft_jobs (draft_id, vault_id, created_by, job_type, "
            "status, max_model_calls, timeout_seconds) "
            "VALUES (?, ?, ?, 'compile', ?, 0, 60)",
            (draft_id, vault_id, owner_id, status),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def test_used_input_is_refused_with_input_in_use(self):
        draft = self.make_draft()
        record = self.add_input(draft.id)
        job_id = self.insert_compile_job(draft_id=draft.id, status="completed")
        # Provenance: the compile's evidence ledger cites this input. The
        # production writer (insert_evidence) computes passage_sha256 from the
        # passage text itself; source_content_sha256 carries the input's
        # content hash, exactly as the compile pipeline records it.
        self.store.insert_evidence(
            job_id=job_id,
            label="n01-ev-1",
            source_kind="draft_input",
            draft_input_id=record.id,
            title="a.txt",
            passage="The manuscript passage cited into the compiled draft.",
            source_content_sha256=record.content_sha256,
        )

        captured = "no-error"
        try:
            self.deletion.delete_input(
                self.store, draft_id=draft.id, owner_id=1, input_id=record.id
            )
        except Exception as exc:  # noqa: BLE001 - outcome IS the assertion
            captured = getattr(exc, "code", type(exc).__name__)

        self.assertEqual(captured, "input_in_use")
        self.assertTrue(self.storage.exists(record.storage_relpath))

    def test_delete_during_active_compile_is_refused(self):
        draft = self.make_draft()
        record = self.add_input(draft.id)
        self.insert_compile_job(draft_id=draft.id, status="running")

        captured = "no-error"
        try:
            self.deletion.delete_input(
                self.store, draft_id=draft.id, owner_id=1, input_id=record.id
            )
        except Exception as exc:  # noqa: BLE001 - outcome IS the assertion
            captured = getattr(exc, "code", type(exc).__name__)

        self.assertEqual(captured, "active_job")
        self.assertEqual(
            len(self.store.list_inputs(draft_id=draft.id, owner_id=1)),
            1,
        )
        self.assertTrue(self.storage.exists(record.storage_relpath))

    def test_delete_with_missing_bytes_removes_the_row(self):
        draft = self.make_draft()
        record = self.add_input(draft.id)
        # Bytes already gone (external cleanup / earlier failed attempt):
        self.storage.resolve(record.storage_relpath).unlink()

        outcome = "ok"
        try:
            self.deletion.delete_input(
                self.store, draft_id=draft.id, owner_id=1, input_id=record.id
            )
        except Exception as exc:  # noqa: BLE001 - outcome IS the assertion
            outcome = getattr(exc, "code", type(exc).__name__)

        self.assertEqual(outcome, "ok")
        self.assertEqual(self.store.list_inputs(draft_id=draft.id, owner_id=1), [])

    def test_input_stays_deletable_after_tombstone_reconcile(self):
        draft = self.make_draft()
        record = self.add_input(draft.id)
        # Crash window: the file was tombstoned but the row delete never ran,
        # then a restart reconciled .trash away (age-based purge).
        self.storage.tombstone(record.storage_relpath)
        self.storage.reconcile(set(), max_age_seconds=0)

        outcome = "ok"
        try:
            self.deletion.delete_input(
                self.store, draft_id=draft.id, owner_id=1, input_id=record.id
            )
        except Exception as exc:  # noqa: BLE001 - outcome IS the assertion
            outcome = getattr(exc, "code", type(exc).__name__)

        self.assertEqual(outcome, "ok")
        self.assertEqual(self.store.list_inputs(draft_id=draft.id, owner_id=1), [])


if __name__ == "__main__":
    unittest.main()
