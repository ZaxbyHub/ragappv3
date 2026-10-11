"""Edge coverage for issue #785 beyond the frozen acceptance checks.

The frozen files (``test_n01_input_delete.py`` /
``test_n01_input_delete_routes.py``) are checkpoint-byte-locked, so the
plan-critic Round-1 gap tests live here instead:

* T1 -- the production revision-without-evidence shape: a completed compile
  whose frozen ``input_json`` snapshot lists the input (and which produced a
  revision) but wrote no ``draft_evidence`` rows. Production reaches this via
  late-stage retries (evidence snapshotting runs in the research stage), and
  no ``draft_jobs.input_id``-based guard can ever see it. Must refuse with
  ``input_in_use``.
* T2 -- the IntegrityError backstop: an evidence row on a non-completed job
  defeats every in-use leg, so the row DELETE itself trips the
  ``draft_evidence`` CHECK via ``ON DELETE SET NULL``; the store must
  translate that to ``input_in_use`` instead of leaking a raw
  ``sqlite3.IntegrityError``.
* T3 -- absent bytes + row-delete failure: no tombstone means no restore, and
  nothing may mask the original row-delete error (a ``None`` token passed to
  ``restore_tombstone`` would raise ``DraftInputPathError`` instead).
* T4 -- precedence: an in-use input whose bytes are already absent still
  refuses with ``input_in_use`` (SPEC 6.3's 409 is unconditional) and keeps
  the row; whole-draft delete remains the escape hatch.
* T5 -- upload rollback: ``_delete_input_row_best_effort`` swallows the new
  guard raises (they are ``DraftStoreError`` subclasses) so a failing upload
  never masks its original error with a guard conflict.
"""

import asyncio
import hashlib
import json
import os
import sqlite3
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.services.draft_store import DraftStore
from draft_room.test_draft_deletion import DraftDeletionTestBase


class TestN01bInputDeleteEdges(DraftDeletionTestBase):
    def _insert_compile_job(
        self,
        *,
        draft_id,
        owner_id=1,
        vault_id=1,
        status,
        snapshot_input=None,
    ):
        """Compile job in the production writer shape.

        ``snapshot_input`` optionally freezes the ``input_json`` snapshot the
        route enqueue writes (``_sync_input_snapshot`` element order:
        ``[id, role, authority, content_sha256, parsed_text_sha256]``).
        """
        input_json = "{}"
        if snapshot_input is not None:
            record, role = snapshot_input
            input_json = json.dumps(
                {
                    "inputs": [
                        [
                            record.id,
                            role,
                            "unknown",
                            record.content_sha256,
                            record.parsed_text_sha256 or "",
                        ]
                    ]
                }
            )
        cur = self.conn.execute(
            "INSERT INTO draft_jobs (draft_id, vault_id, created_by, job_type, "
            "status, input_json, max_model_calls, timeout_seconds) "
            "VALUES (?, ?, ?, 'compile', ?, ?, 0, 60)",
            (draft_id, vault_id, owner_id, status, input_json),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def _insert_revision(self, *, draft_id, job_id):
        self.conn.execute(
            "INSERT INTO draft_revisions (draft_id, job_id, revision_no, source, "
            "content_md, content_sha256, fact_status, is_current, created_by) "
            "VALUES (?, ?, 1, 'pipeline', '# Body', ?, 'passed', 1, 1)",
            (draft_id, job_id, hashlib.sha256(b"# Body").hexdigest()),
        )
        self.conn.commit()

    def _delete_outcome(self, draft, record):
        try:
            self.deletion.delete_input(
                self.store, draft_id=draft.id, owner_id=1, input_id=record.id
            )
            return "ok"
        except Exception as exc:  # noqa: BLE001 - outcome IS the assertion
            return getattr(exc, "code", type(exc).__name__)

    def test_t1_revision_without_evidence_is_refused(self):
        draft = self.make_draft()
        record = self.add_input(draft.id)
        job_id = self._insert_compile_job(
            draft_id=draft.id,
            status="completed",
            snapshot_input=(record, "reference"),
        )
        self._insert_revision(draft_id=draft.id, job_id=job_id)

        outcome = self._delete_outcome(draft, record)

        self.assertEqual(outcome, "input_in_use")
        self.assertTrue(self.storage.exists(record.storage_relpath))
        self.assertEqual(
            len(self.store.list_inputs(draft_id=draft.id, owner_id=1)), 1
        )

    def test_t2_integrity_error_backstop_maps_to_input_in_use(self):
        draft = self.make_draft()
        record = self.add_input(draft.id)
        # A non-completed job defeats every in-use leg, so only the row
        # DELETE's CHECK violation (via ON DELETE SET NULL) stands between
        # this evidence row and silent provenance loss.
        job_id = self._insert_compile_job(draft_id=draft.id, status="failed")
        self.store.insert_evidence(
            job_id=job_id,
            label="n01b-ev-1",
            source_kind="draft_input",
            draft_input_id=record.id,
            title="a.txt",
            passage="A passage a partial run snapshotted before failing.",
            source_content_sha256=record.content_sha256,
        )

        outcome = self._delete_outcome(draft, record)

        self.assertEqual(outcome, "input_in_use")
        self.assertTrue(self.storage.exists(record.storage_relpath))

    def test_t3_absent_bytes_and_row_failure_preserves_original_error(self):
        draft = self.make_draft()
        record = self.add_input(draft.id)
        self.storage.resolve(record.storage_relpath).unlink()

        with patch.object(
            self.store, "delete_input_row", side_effect=RuntimeError("boom")
        ):
            with self.assertRaises(RuntimeError) as ctx:
                self.deletion.delete_input(
                    self.store, draft_id=draft.id, owner_id=1, input_id=record.id
                )

        self.assertEqual(str(ctx.exception), "boom")
        self.assertEqual(
            len(self.store.list_inputs(draft_id=draft.id, owner_id=1)), 1
        )

    def test_t4_in_use_wins_over_absent_bytes(self):
        draft = self.make_draft()
        record = self.add_input(draft.id)
        job_id = self._insert_compile_job(draft_id=draft.id, status="completed")
        self.store.insert_evidence(
            job_id=job_id,
            label="n01b-ev-2",
            source_kind="draft_input",
            draft_input_id=record.id,
            title="a.txt",
            passage="The passage the compile cited from this input.",
            source_content_sha256=record.content_sha256,
        )
        self.storage.resolve(record.storage_relpath).unlink()

        outcome = self._delete_outcome(draft, record)

        self.assertEqual(outcome, "input_in_use")
        self.assertEqual(
            len(self.store.list_inputs(draft_id=draft.id, owner_id=1)), 1
        )

    def test_t5_upload_rollback_swallows_guard_conflicts(self):
        from app.api.routes.draft_room import _delete_input_row_best_effort

        draft = self.make_draft()
        record = self.add_input(draft.id)
        # A compile enqueued after the reserve makes the in-transaction guard
        # refuse the row delete; the rollback helper must swallow it.
        self._insert_compile_job(draft_id=draft.id, status="running")

        # The helper hops to a worker thread (asyncio.to_thread) like the
        # route does; the pool connections it gets in production are
        # check_same_thread=False, so mirror that here.
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        try:
            store = DraftStore(conn)

            # Must not raise despite the guard conflict inside delete_input_row.
            asyncio.run(
                _delete_input_row_best_effort(
                    store, draft_id=draft.id, owner_id=1, input_id=record.id
                )
            )
        finally:
            conn.close()

        self.assertEqual(
            len(self.store.list_inputs(draft_id=draft.id, owner_id=1)), 1
        )

    def test_t6_running_compile_with_snapshot_answers_active_job(self):
        # Production writes the input snapshot at ENQUEUE time, so a running
        # compile already carries it. The delete must answer the active-job
        # conflict, not input_in_use: this pins the snapshot leg's
        # status = 'completed' filter (dropping it would make the leg match
        # the running job and shadow the active_job refusal).
        draft = self.make_draft()
        record = self.add_input(draft.id)
        self._insert_compile_job(
            draft_id=draft.id,
            status="running",
            snapshot_input=(record, "reference"),
        )

        outcome = self._delete_outcome(draft, record)

        self.assertEqual(outcome, "active_job")
        self.assertTrue(self.storage.exists(record.storage_relpath))
        self.assertEqual(
            len(self.store.list_inputs(draft_id=draft.id, owner_id=1)), 1
        )


if __name__ == "__main__":
    unittest.main()
