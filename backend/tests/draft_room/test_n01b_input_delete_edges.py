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
* T5 -- upload rollback: ``_delete_input_row_best_effort`` runs in best-effort
  mode, so a compile enqueued between the reserve and the rollback cannot leave
  a stranded row whose bytes are already gone.
* T6-T13 -- active-job ``pending`` arm, concurrent-delete tombstone handling, a
  missing storage root failing closed, restore failure not masking the conflict,
  the in-transaction re-check closing the service→transaction window,
  snapshot membership beyond the first entry, and the reader pinned against
  the production snapshot writer.
"""

import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.services.draft_input_storage import DraftInputPathError
from app.services.draft_store import (
    DraftConflictError,
    DraftNotFoundError,
    DraftStore,
)
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
        snapshot_inputs=None,
    ):
        """Compile job in the production writer shape.

        ``snapshot_input``/``snapshot_inputs`` optionally freeze the ``input_json``
        snapshot the enqueue writes (``_sync_input_snapshot`` element order:
        ``[id, role, authority, sha, sha]``), in the given order.
        """
        entries = snapshot_inputs
        if entries is None and snapshot_input is not None:
            entries = [snapshot_input]
        input_json = "{}"
        if entries:
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
                        for record, role in entries
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

    def test_t5_upload_rollback_still_removes_the_row_under_a_compile(self):
        """Compensation must not be blocked by the guards added for real deletes.

        A compile can be enqueued by the worker's auto-retry between an upload's
        reserve and its rollback. The rollback deletes the row this request just
        created, whose bytes have already been discarded; applying the in-use /
        active-job guards there stranded a row with no file that permanently 409'd
        the owner's re-upload on UNIQUE(draft_id, content_sha256).
        """
        from app.api.routes.draft_room import _delete_input_row_best_effort

        draft = self.make_draft()
        record = self.add_input(draft.id)
        # A compile enqueued after the reserve: the non-compensation delete path
        # must refuse it (see test_t6), but compensation must still clean up.
        self._insert_compile_job(draft_id=draft.id, status="running")

        # The helper hops to a worker thread (asyncio.to_thread) like the
        # route does; the pool connections it gets in production are
        # check_same_thread=False, so mirror that here.
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        try:
            store = DraftStore(conn)
            asyncio.run(
                _delete_input_row_best_effort(
                    store, draft_id=draft.id, owner_id=1, input_id=record.id
                )
            )
        finally:
            conn.close()

        # The row must be gone; a surviving row is the permanent wedge.
        self.assertEqual(
            self.store.list_inputs(draft_id=draft.id, owner_id=1), []
        )

    def test_t6_running_compile_with_snapshot_answers_active_job(self):
        # Production writes the input snapshot at ENQUEUE time, so a running
        # compile already carries it. The delete must answer the active-job
        # conflict, not input_in_use: this pins the snapshot leg's exclusion of
        # non-terminal jobs (dropping it would make the leg match the running
        # job and shadow the active_job refusal).
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

    def test_t7_pending_compile_answers_active_job(self):
        # SPEC 8.2 says "reject if active". An enqueued-but-not-started compile
        # is active; only pinning `running` would let the delete through.
        draft = self.make_draft()
        record = self.add_input(draft.id)
        self._insert_compile_job(draft_id=draft.id, status="pending")

        outcome = self._delete_outcome(draft, record)

        self.assertEqual(outcome, "active_job")
        self.assertTrue(self.storage.exists(record.storage_relpath))
        self.assertEqual(
            len(self.store.list_inputs(draft_id=draft.id, owner_id=1)), 1
        )

    def test_t8_concurrent_delete_commits_tombstone_instead_of_restoring(self):
        """Two DELETEs of one input must not end with bytes and no row.

        Request A tombstones the bytes; request B sees them absent, deletes the
        row and returns 204; request A's row delete then finds nothing. Restoring
        A's tombstone would put the bytes back with no row referencing them, and
        nothing reclaims them.
        """
        draft = self.make_draft()
        record = self.add_input(draft.id)
        relpath = record.storage_relpath

        def concurrent_delete(**kwargs):
            # Stand in for the other request having already removed the row.
            self.conn.execute(
                "DELETE FROM draft_inputs WHERE id = ?", (kwargs["input_id"],)
            )
            self.conn.commit()
            raise DraftNotFoundError("input not found")

        with patch.object(
            self.store, "delete_input_row", side_effect=concurrent_delete
        ):
            with self.assertRaises(DraftNotFoundError):
                self.deletion.delete_input(
                    self.store, draft_id=draft.id, owner_id=1, input_id=record.id
                )

        # No orphan bytes, and no tombstone left behind.
        self.assertFalse(self.storage.exists(relpath))
        trash_dir = self.root / ".trash"
        self.assertEqual(
            list(trash_dir.iterdir()) if trash_dir.is_dir() else [], []
        )

    def test_t9_missing_storage_root_fails_closed(self):
        # mkdir(parents=True) inside tombstone() would silently recreate the
        # tree, so a vanished root used to read as "bytes are gone" and delete
        # the row while the bytes stayed wherever they had been moved.
        draft = self.make_draft()
        record = self.add_input(draft.id)
        shutil.rmtree(self.root)

        with self.assertRaises(DraftInputPathError):
            self.deletion.delete_input(
                self.store, draft_id=draft.id, owner_id=1, input_id=record.id
            )

        self.assertEqual(
            len(self.store.list_inputs(draft_id=draft.id, owner_id=1)), 1
        )

    def test_t14_evidence_leg_is_pinned_independently_of_the_backstop(self):
        # The evidence leg is otherwise masked: the row DELETE trips the
        # draft_evidence CHECK and the IntegrityError backstop returns the
        # same conflict, so deleting the leg leaves every other test green.
        # Assert the store-level predicate directly, with a compile that
        # carries NEITHER the input in its snapshot NOR a legacy input_id.
        draft = self.make_draft()
        record = self.add_input(draft.id)
        other = self.add_input(draft.id, name="b.txt", content=b"other")
        # No snapshot content at all: neither input is named by the compile,
        # so the evidence row below is the ONLY thing that can mark `record`
        # as in use.
        job_id = self._insert_compile_job(draft_id=draft.id, status="completed")
        self.store.insert_evidence(
            job_id=job_id,
            label="n01b-ev-only",
            source_kind="draft_input",
            draft_input_id=record.id,
            title="a.txt",
            passage="The passage the compile cited from this input.",
            source_content_sha256=record.content_sha256,
        )

        self.assertTrue(
            self.store.input_is_in_use(draft_id=draft.id, input_id=record.id),
            "an input cited by a compile's evidence must read as in use even "
            "when it is absent from that compile's snapshot",
        )
        self.assertFalse(
            self.store.input_is_in_use(draft_id=draft.id, input_id=other.id),
            "an input the compile never cited must stay deletable",
        )

    def test_t10_restore_failure_does_not_mask_the_conflict(self):
        # A Windows file lock makes restore_tombstone raise OSError. That must
        # not replace the DraftConflictError the caller needs to see (409), nor
        # escape the route's typed handlers as an untranslated 500.
        draft = self.make_draft()
        record = self.add_input(draft.id)
        self._insert_compile_job(draft_id=draft.id, status="running")

        with patch.object(
            self.storage,
            "restore_tombstone",
            side_effect=OSError(13, "Permission denied"),
        ):
            with self.assertRaises(DraftConflictError) as ctx:
                self.deletion.delete_input(
                    self.store, draft_id=draft.id, owner_id=1, input_id=record.id
                )

        self.assertEqual(ctx.exception.code, "active_job")

    def test_t11_in_transaction_recheck_catches_usage_appearing_mid_delete(self):
        # The service pre-check runs outside the transaction; usage can appear
        # between it and the DELETE. Only the in-transaction recheck closes
        # that window, so it needs a test that fails when it is removed.
        draft = self.make_draft()
        record = self.add_input(draft.id)
        self._insert_compile_job(
            draft_id=draft.id, status="completed", snapshot_input=(record, "reference")
        )
        calls = {"n": 0}
        real = self.store.input_is_in_use

        def appears_between_checks(**kwargs):
            # First call is the service pre-check (must miss); the second is
            # the in-transaction re-check (must hit).
            calls["n"] += 1
            return calls["n"] > 1 and real(**kwargs)

        with patch.object(
            self.store, "input_is_in_use", side_effect=appears_between_checks
        ):
            outcome = self._delete_outcome(draft, record)

        self.assertEqual(outcome, "input_in_use")
        self.assertTrue(self.storage.exists(record.storage_relpath))

    def test_t12_snapshot_membership_is_not_first_entry_only(self):
        # Production snapshots list every input id-ordered, so the target is
        # routinely NOT the first entry. A parser that only inspected the first
        # entry would let a genuinely used input be deleted.
        draft = self.make_draft()
        first = self.add_input(draft.id, name="a.txt", content=b"first")
        target = self.add_input(draft.id, name="b.txt", content=b"second")
        self._insert_compile_job(
            draft_id=draft.id,
            status="completed",
            snapshot_inputs=[(first, "reference"), (target, "manuscript")],
        )

        outcome = self._delete_outcome(draft, target)

        self.assertEqual(outcome, "input_in_use")
        self.assertTrue(self.storage.exists(target.storage_relpath))
        self.assertTrue(self.storage.exists(first.storage_relpath))

    def test_t13_snapshot_built_by_the_production_writer_is_parsed(self):
        # Pins the reader against the WRITER, not a hand-built literal: if
        # _sync_input_snapshot reorders its elements or the enqueue key is
        # renamed, every leg goes inert and this test fails.
        from app.api.routes.draft_room import _sync_input_snapshot

        draft = self.make_draft()
        record = self.add_input(draft.id)
        self.conn.execute(
            "INSERT INTO draft_jobs (draft_id, vault_id, created_by, job_type, "
            "status, input_json, max_model_calls, timeout_seconds) "
            "VALUES (?, 1, 1, 'compile', 'completed', ?, 0, 60)",
            (
                draft.id,
                json.dumps(
                    {"inputs": _sync_input_snapshot(self.store, self.store.get_draft(draft.id, 1))}
                ),
            ),
        )
        self.conn.commit()

        outcome = self._delete_outcome(draft, record)

        self.assertEqual(outcome, "input_in_use")
        self.assertTrue(self.storage.exists(record.storage_relpath))


if __name__ == "__main__":
    unittest.main()
