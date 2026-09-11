"""Issue #532 coverage-debt tests (PR #532 review findings).

One class per review finding, all asserting REQUIRED behavior through the
real production seams (no re-implementations of the logic under test):

* PRR-004 — the vault-purge transaction loop (``vaults.delete_vault``'s
  per-file ``on_document_changed`` pass) invalidates a dependent Ready
  draft citing a purged document, with the same observable outcomes as the
  single-delete path.
* PRR-005 — the upload-overwrite branch of
  ``DocumentProcessor._insert_or_get_file_record`` (same file_path, new
  content hash) invalidates dependent draft evidence, while a same-hash
  overwrite is a no-op.
* PRR-008 — the REAL ``BackgroundProcessor.cancel_pending_jobs`` marks only
  matching queued items, preserves FIFO order and the queue's
  join()-bookkeeping, and the real worker skips cancelled items while
  processing the rest.
* PRR-009 — a truncated historical evidence pass persists a
  ``draft_reconcile_backlog`` row with the right ``next_offset``, and
  ``reconcile_ready_evidence`` drains it: the remaining rows are processed,
  deletion semantics stamp ``source_deleted_at`` on them, and the backlog
  row is removed once the walk completes.
* PRR-018 — material-change coverage beyond AC14's brief/role: a tier
  change and input authority / as_of_date / locked_spans changes each move
  a Ready draft to needs_review and clear ``ready_revision_id``; unchanged
  values keep Ready.

Harness: subclasses FreshnessTestBase from test_draft_evidence_freshness.py
(temp SQLite via init_db + run_migrations, SimpleConnectionPool, real
files/wiki/kms/draft rows), mirroring test_issue516_freshness_acceptance.py.
No HTTP: PRR-004 drives ``delete_vault`` directly with a stub vector store
(the smallest real caller that reaches the purge loop), the same direct-call
pattern as tests/issue513_checks/test_c19_vault_delete_rollback.py.
"""

import asyncio
import os
import sys
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

try:
    import lancedb  # noqa: F401
except ImportError:
    import types

    sys.modules["lancedb"] = types.ModuleType("lancedb")

from app.config import settings
from app.services import draft_evidence_freshness as freshness
from app.services.draft_store import sha256_text
from tests.draft_room.test_draft_evidence_freshness import (
    OWNER_ID,
    VAULT_ID,
    FreshnessTestBase,
)

PURGED_VAULT_ID = VAULT_ID + 1


# ── PRR-004: vault purge invalidates dependent Ready-draft evidence ─────────


class TestPRR004VaultPurgeInvalidatesDependentEvidence(FreshnessTestBase):
    """The vault DELETE route's purge loop must match single-delete semantics.

    The purged vault's own drafts cascade away with the vault row, so the
    observable survivor is a Ready draft in ANOTHER vault citing a document
    in the purged vault — exactly the cross-vault dependency the per-file
    ``on_document_changed`` loop inside delete_vault's transaction exists to
    invalidate.
    """

    def setUp(self):
        super().setUp()
        # delete_vault builds DraftInputStorage(settings.data_dir / ...) for
        # the draft purge plan; keep it inside the temp dir (same pattern as
        # test_c19_vault_delete_rollback.py).
        self._original_data_dir = settings.data_dir
        settings.data_dir = Path(self._temp_dir)

    def tearDown(self):
        settings.data_dir = self._original_data_dir
        super().tearDown()

    def test_vault_purge_moves_dependent_ready_draft_to_needs_review(self):
        from app.api.routes.vaults import delete_vault

        # Dependent Ready draft in VAULT_ID citing a document in the vault
        # that is about to be purged.
        draft = self.make_draft()
        job = self.make_job(draft.id)
        rev = self.make_revision(draft.id, job)
        self.conn.execute(
            "INSERT OR IGNORE INTO vaults (id, name, description) "
            "VALUES (?, 'V2', '')",
            (PURGED_VAULT_ID,),
        )
        cited_file = self.make_file(
            file_hash="hash-prr004", vault_id=PURGED_VAULT_ID
        )
        filler_file = self.make_file(
            file_hash="hash-prr004-filler", vault_id=PURGED_VAULT_ID
        )
        ev_id = self.add_evidence(
            job,
            label="S1",
            source_kind="document",
            source_content_sha256="hash-prr004",
            file_id=cited_file,
            chunk_uid=f"{cited_file}_0",
        )
        self.mark_ready(draft.id, rev)
        self.assertEqual(self.draft_status(draft.id), "ready")

        class _VectorStoreStub:
            db = None  # post-commit vector reconcile is a no-op here

            async def delete_by_vault(self, vault_id_str):
                return 0

        response = asyncio.run(
            delete_vault(
                vault_id=PURGED_VAULT_ID,
                conn=self.conn,
                vector_store=_VectorStoreStub(),
                user={"id": OWNER_ID, "username": "owner", "role": "member"},
                _csrf_token="test-csrf-token",
            )
        )

        # Guards: the purge itself worked, otherwise the assertions below
        # would be setup failures, not the regression.
        self.assertIn("deleted successfully", response["message"])
        self.assertIsNone(
            self.conn.execute(
                "SELECT id FROM files WHERE id IN (?, ?)",
                (cited_file, filler_file),
            ).fetchone(),
            "vault purge must delete the vault's document rows",
        )
        self.assertIsNone(
            self.conn.execute(
                "SELECT id FROM vaults WHERE id = ?", (PURGED_VAULT_ID,)
            ).fetchone(),
            "vault purge must delete the vault row",
        )

        # REQUIRED BEHAVIOR: the same observable outcomes the single-delete
        # path produces (see test_draft_evidence_freshness deletion tests).
        self.assertEqual(
            self.draft_status(draft.id),
            "needs_review",
            "vault purge must move a dependent Ready draft back to "
            "needs_review when its cited document is purged",
        )
        self.assertEqual(
            self.fact_status(rev),
            "invalidated",
            "vault purge must invalidate the dependent draft's current "
            "fact result",
        )
        row = self.conn.execute(
            "SELECT passage, source_deleted_at FROM draft_evidence WHERE id = ?",
            (ev_id,),
        ).fetchone()
        self.assertIsNotNone(row, "evidence snapshot row must survive")
        self.assertEqual(row["passage"], "snapshotted passage")
        self.assertIsNotNone(
            row["source_deleted_at"],
            "vault purge must stamp source_deleted_at on evidence whose "
            "document was purged",
        )
        blockers = self.open_blockers(draft.id, "source_deleted")
        self.assertEqual(
            len(blockers),
            1,
            "vault purge must open exactly one source_deleted blocker",
        )
        self.assertEqual(blockers[0]["waivable"], 0)


# ── PRR-005: upload-overwrite branch invalidates dependent evidence ────────


class TestPRR005UploadOverwriteInvalidatesDependentEvidence(FreshnessTestBase):
    """Drive the overwrite branch of _insert_or_get_file_record.

    Mirrors AC4's dependent-draft seeding (a Ready draft citing the file)
    but goes through the real ingest seam instead of calling
    on_document_changed directly: the same file_path re-ingested with
    DIFFERENT bytes must invalidate, while the same-content re-ingest must
    not (the hook's hash guard). Direct-call pattern taken from
    test_document_progress_async.py.
    """

    def test_overwrite_with_new_hash_invalidates_dependent_ready_draft(self):
        from app.services.document_processor import DocumentProcessor

        processor = DocumentProcessor(
            chunk_size_chars=2000, chunk_overlap_chars=200, pool=self.pool
        )
        file_path = os.path.join(self._temp_dir, "prr005.txt")
        with open(file_path, "w", encoding="utf-8") as fh:
            fh.write("original bytes")

        file_id = processor._insert_or_get_file_record(
            file_path, "hash-prr005-v1", self.conn, vault_id=VAULT_ID,
            source="upload",
        )

        draft = self.make_draft()
        job = self.make_job(draft.id)
        rev = self.make_revision(draft.id, job)
        ev_id = self.add_evidence(
            job,
            label="S1",
            source_kind="document",
            source_content_sha256="hash-prr005-v1",
            file_id=file_id,
            chunk_uid=f"{file_id}_0",
        )
        self.mark_ready(draft.id, rev)
        self.assertEqual(self.draft_status(draft.id), "ready")

        # Control: re-ingesting the SAME content (same hash) is a no-op —
        # the hook's hash guard must keep the draft Ready.
        same_id = processor._insert_or_get_file_record(
            file_path, "hash-prr005-v1", self.conn, vault_id=VAULT_ID,
            source="upload",
        )
        self.assertEqual(same_id, file_id)
        self.assertEqual(
            self.draft_status(draft.id),
            "ready",
            "a same-content overwrite must not invalidate a Ready draft",
        )
        self.assertEqual(self.fact_status(rev), "passed")

        # REQUIRED BEHAVIOR: an overwrite with DIFFERENT bytes (new hash)
        # runs the update branch's freshness hook and invalidates.
        with open(file_path, "w", encoding="utf-8") as fh:
            fh.write("replacement bytes")
        overwrite_id = processor._insert_or_get_file_record(
            file_path, "hash-prr005-v2", self.conn, vault_id=VAULT_ID,
            source="upload",
        )
        self.assertEqual(
            overwrite_id, file_id, "overwrite must reuse the existing row id"
        )
        stored_hash = self.conn.execute(
            "SELECT file_hash FROM files WHERE id = ?", (file_id,)
        ).fetchone()[0]
        self.assertEqual(stored_hash, "hash-prr005-v2")

        self.assertEqual(
            self.draft_status(draft.id),
            "needs_review",
            "an overwrite with different bytes must move a dependent Ready "
            "draft back to needs_review",
        )
        self.assertEqual(self.fact_status(rev), "invalidated")
        row = self.conn.execute(
            "SELECT passage, source_deleted_at FROM draft_evidence WHERE id = ?",
            (ev_id,),
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertIsNone(
            row["source_deleted_at"],
            "an overwrite is a change, not a deletion — no source_deleted "
            "stamp",
        )
        blockers = self.open_blockers(draft.id, "evidence_changed")
        self.assertEqual(len(blockers), 1)
        self.assertEqual(blockers[0]["waivable"], 0)


# ── PRR-008: real cancel_pending_jobs + worker skip ─────────────────────────


class TestPRR008CancelPendingJobsWorkerSkip(unittest.TestCase):
    """Exercise the REAL BackgroundProcessor cancellation contract.

    Construction follows the established pattern in
    test_background_tasks_bounded_queue.py (a real BackgroundProcessor; its
    DocumentProcessor is never invoked because process_existing_file is
    stubbed at the instance level).
    """

    TARGET_FILE_ID = 4242

    def _make_items(self):
        from app.services.background_tasks import TaskItem

        target = TaskItem(
            file_path="/tmp/target.txt", vault_id=1,
            file_id=self.TARGET_FILE_ID,
        )
        other_a = TaskItem(
            file_path="/tmp/other-a.txt", vault_id=1, file_id=101
        )
        other_b = TaskItem(
            file_path="/tmp/other-b.txt", vault_id=1, file_id=102
        )
        return target, other_a, other_b

    def test_cancel_marks_only_matching_items_and_preserves_queue(self):
        from app.services.background_tasks import BackgroundProcessor

        async def scenario():
            processor = BackgroundProcessor(max_retries=1, retry_delay=0.05)
            target, other_a, other_b = self._make_items()
            for item in (target, other_a, other_b):
                processor.queue.put_nowait(item)
            self.assertEqual(processor.queue.qsize(), 3)

            # Empty match is a caller bug and must be refused (returns 0).
            self.assertEqual(processor.cancel_pending_jobs(), 0)

            cancelled = processor.cancel_pending_jobs(
                file_id=self.TARGET_FILE_ID
            )
            # Size preserved by the drain-and-refill (3 items in, 3 out).
            qsize_after_cancel = processor.queue.qsize()
            # Drain to observe FIFO order without touching private state;
            # task_done keeps the bookkeeping balanced.
            drained = []
            while True:
                try:
                    drained.append(processor.queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            for _ in drained:
                processor.queue.task_done()
            return (
                cancelled, target, other_a, other_b,
                qsize_after_cancel, drained,
            )

        (
            cancelled, target, other_a, other_b, qsize, drained,
        ) = asyncio.run(scenario())

        self.assertEqual(cancelled, 1, "exactly the matching item counts")
        self.assertTrue(target.cancelled, "matching item must be cancelled")
        self.assertFalse(other_a.cancelled, "non-matching items untouched")
        self.assertFalse(other_b.cancelled, "non-matching items untouched")
        self.assertEqual(
            qsize, 3, "cancel must preserve the queue size (marked, not removed)"
        )
        # FIFO order preserved: the drain-and-refill requeues in order and
        # the cancelled item stays ON the queue (marked, not removed).
        self.assertEqual(
            [(it.file_id, it.cancelled) for it in drained],
            [
                (self.TARGET_FILE_ID, True),
                (101, False),
                (102, False),
            ],
        )

    def test_process_task_skips_cancelled_item_directly(self):
        """The worker-skip guard at the top of _process_task (smallest seam)."""
        from app.services.background_tasks import BackgroundProcessor

        async def scenario():
            processor = BackgroundProcessor(max_retries=1, retry_delay=0.05)
            processed = []

            async def fake_process_existing_file(*, file_id, **kwargs):
                processed.append(file_id)
                return SimpleNamespace(
                    chunks=[], vault_id=kwargs.get("vault_id", 1),
                    file_id=file_id, file_path="/tmp/x.txt", file_hash=None,
                )

            processor.processor.process_existing_file = (
                fake_process_existing_file
            )
            processor.processor.should_enqueue_enrichment = (
                lambda *a, **k: False
            )

            target, other_a, _other_b = self._make_items()
            target.cancelled = True
            await processor._process_task(target)  # must be a no-op
            await processor._process_task(other_a)  # must process
            return processed

        processed = asyncio.run(scenario())
        self.assertEqual(
            processed,
            [101],
            "a cancelled item must be dropped without processing while a "
            "non-cancelled one processes",
        )

    def test_real_worker_skips_cancelled_and_join_completes(self):
        """The real worker loop drains the queue; join() must complete.

        join() completing proves cancel_pending_jobs' task_done()/put_nowait()
        pairs kept the unfinished-task bookkeeping balanced; the stubbed
        processor records exactly which items the worker actually processed.
        """
        from app.services.background_tasks import BackgroundProcessor

        async def scenario():
            processor = BackgroundProcessor(max_retries=1, retry_delay=0.05)
            target, other_a, other_b = self._make_items()
            for item in (target, other_a, other_b):
                processor.queue.put_nowait(item)
            processor.cancel_pending_jobs(file_id=self.TARGET_FILE_ID)
            flags_before = (target.cancelled, other_a.cancelled,
                            other_b.cancelled)

            processed = []

            async def fake_process_existing_file(*, file_id, **kwargs):
                processed.append(file_id)
                return SimpleNamespace(
                    chunks=[], vault_id=kwargs.get("vault_id", 1),
                    file_id=file_id, file_path="/tmp/x.txt", file_hash=None,
                )

            processor.processor.process_existing_file = (
                fake_process_existing_file
            )
            processor.processor.should_enqueue_enrichment = (
                lambda *a, **k: False
            )

            worker = asyncio.create_task(processor._worker_loop())
            # Bounded: unbalanced task_done bookkeeping would hang join().
            await asyncio.wait_for(processor.queue.join(), timeout=10.0)
            processor.shutdown_event.set()
            await asyncio.wait_for(worker, timeout=10.0)
            return flags_before, processed, processor.queue.qsize()

        flags_before, processed, qsize_after = asyncio.run(scenario())

        self.assertEqual(flags_before, (True, False, False))
        self.assertEqual(
            processed,
            [101, 102],
            "the worker must process the queued items in FIFO order while "
            "dropping the cancelled one",
        )
        self.assertNotIn(
            self.TARGET_FILE_ID, processed,
            "the cancelled item must never reach the processor",
        )
        self.assertEqual(qsize_after, 0)
        # join() having returned is itself the bookkeeping assertion.


# ── PRR-009: backlog creation and drain ─────────────────────────────────────


class TestPRR009BacklogDrain(FreshnessTestBase):
    """Truncated historical passes persist a backlog row; reconcile drains it.

    Seeding mirrors AC4 (test_issue516_freshness_acceptance.py): one source
    with MAX_EVIDENCE_PER_JOB+2 historical rows ordered ahead of the current
    row. Exact drain contract (draft_evidence_freshness.py): a resumed pass
    that still ends past the cap RE-PERSISTS the backlog with its new offset
    ("a re-truncated one persists its new offset for the next run"); a
    resumed pass whose page is empty completes and DELETES the row. With
    MAX+2 seeded rows the truncated offset is MAX, so drain #1 processes the
    two remaining rows and re-persists MAX+2, and drain #2 finds an empty
    page and deletes the row.
    """

    HISTORICAL = freshness.MAX_EVIDENCE_PER_JOB + 2

    def _seed_overflow_source(self, *, delete_source):
        """Return (draft_id, cur_rev, file_id, ev_id, historical_ids)."""
        draft = self.make_draft()
        old_job = self.make_job(draft.id)
        self.make_revision(draft.id, old_job, revision_no=1, is_current=0)
        cur_job = self.make_job(draft.id)
        cur_rev = self.make_revision(draft.id, cur_job, revision_no=2,
                                     is_current=1)
        file_id = self.make_file(file_hash="hash-prr009-v1")

        passage_hash = sha256_text("historical passage")
        self.conn.executemany(
            "INSERT INTO draft_evidence (job_id, label, source_kind, "
            "file_id, title, passage, passage_sha256, "
            "source_content_sha256) "
            "VALUES (?, ?, 'document', ?, 'T', 'historical passage', ?, ?)",
            [
                (old_job, f"H{i}", file_id, passage_hash, "hash-prr009-v1")
                for i in range(self.HISTORICAL)
            ],
        )
        self.conn.commit()
        historical_ids = [
            row[0]
            for row in self.conn.execute(
                "SELECT id FROM draft_evidence WHERE job_id = ? "
                "ORDER BY id",
                (old_job,),
            ).fetchall()
        ]
        self.assertEqual(len(historical_ids), self.HISTORICAL)

        ev_id = self.add_evidence(
            cur_job,
            label="S1",
            source_kind="document",
            source_content_sha256="hash-prr009-v1",
            file_id=file_id,
            chunk_uid=f"{file_id}_0",
        )
        self.mark_ready(draft.id, cur_rev)

        if delete_source:
            self.conn.execute("DELETE FROM files WHERE id = ?", (file_id,))
            self.conn.commit()
            freshness.on_document_changed(
                self.conn, file_id=file_id, new_content_sha256=None
            )
        else:
            self.conn.execute(
                "UPDATE files SET file_hash = 'hash-prr009-v2' WHERE id = ?",
                (file_id,),
            )
            self.conn.commit()
            freshness.on_document_changed(
                self.conn, file_id=file_id, new_content_sha256="hash-prr009-v2"
            )
        return draft.id, cur_rev, file_id, ev_id, historical_ids

    def _backlog_row(self, file_id):
        return self.conn.execute(
            "SELECT next_offset FROM draft_reconcile_backlog "
            "WHERE source_kind = 'document' AND source_id = ?",
            (file_id,),
        ).fetchone()

    def test_truncation_creates_backlog_row_with_next_offset(self):
        draft_id, cur_rev, file_id, _ev_id, _hist = self._seed_overflow_source(
            delete_source=False
        )

        # The hook already invalidated the current revision (AC4 behavior);
        # the truncation must ALSO have persisted a durable backlog row.
        self.assertEqual(self.draft_status(draft_id), "needs_review")
        self.assertEqual(self.fact_status(cur_rev), "invalidated")
        row = self._backlog_row(file_id)
        self.assertIsNotNone(
            row, "a truncated historical pass must create a backlog row"
        )
        self.assertEqual(
            row["next_offset"],
            freshness.MAX_EVIDENCE_PER_JOB,
            "the first truncation persists the cap as the resume offset",
        )

    def test_reconcile_drains_backlog_after_processing_remaining_rows(self):
        draft_id, cur_rev, file_id, ev_id, _hist = self._seed_overflow_source(
            delete_source=False
        )
        self.assertIsNotNone(self._backlog_row(file_id))

        # Drain #1: the two remaining historical rows are walked; because the
        # resumed walk still ends past the cap, the contract re-persists the
        # backlog with the advanced offset for the next run.
        summary = asyncio.run(freshness.reconcile_ready_evidence(self.pool))
        self.assertEqual(summary.drafts_scanned, 0)
        row = self._backlog_row(file_id)
        self.assertIsNotNone(
            row,
            "a re-truncated drain persists its new offset for the next run",
        )
        self.assertEqual(row["next_offset"], self.HISTORICAL)

        # Drain #2: the resume page is empty — the walk completed, so the
        # backlog row must be DELETED.
        asyncio.run(freshness.reconcile_ready_evidence(self.pool))
        self.assertIsNone(
            self._backlog_row(file_id),
            "a completed drain must remove the backlog row",
        )

        # The live-source drain never stamps deletion semantics, and the
        # original invalidation is stable (no duplicate blocker).
        stamped = self.conn.execute(
            "SELECT COUNT(*) FROM draft_evidence "
            "WHERE source_deleted_at IS NOT NULL"
        ).fetchone()[0]
        self.assertEqual(stamped, 0)
        self.assertEqual(self.fact_status(cur_rev), "invalidated")
        self.assertEqual(self.draft_status(draft_id), "needs_review")
        self.assertEqual(
            len(self.open_blockers(draft_id, "evidence_changed")), 1
        )
        ev_row = self.conn.execute(
            "SELECT source_deleted_at FROM draft_evidence WHERE id = ?",
            (ev_id,),
        ).fetchone()
        self.assertIsNone(ev_row["source_deleted_at"])

        # A third run must not resurrect the row.
        asyncio.run(freshness.reconcile_ready_evidence(self.pool))
        self.assertIsNone(self._backlog_row(file_id))

    def test_drain_of_deleted_source_stamps_source_deleted_and_removes_row(
        self,
    ):
        draft_id, cur_rev, file_id, ev_id, historical_ids = (
            self._seed_overflow_source(delete_source=True)
        )
        self.assertEqual(self.draft_status(draft_id), "needs_review")
        self.assertEqual(self.fact_status(cur_rev), "invalidated")

        # Rows the first (truncated) hook pass covered are already stamped;
        # the two rows beyond the cap are the drain's remaining work.
        def stamped_count(ids):
            placeholders = ",".join("?" for _ in ids)
            return self.conn.execute(
                "SELECT COUNT(*) FROM draft_evidence "
                f"WHERE id IN ({placeholders}) "
                "AND source_deleted_at IS NOT NULL",
                tuple(ids),
            ).fetchone()[0]

        remaining = historical_ids[freshness.MAX_EVIDENCE_PER_JOB:]
        self.assertEqual(len(remaining), 2)
        self.assertEqual(stamped_count(remaining), 0)
        self.assertEqual(stamped_count(historical_ids), self.HISTORICAL - 2)
        self.assertIsNotNone(self._backlog_row(file_id))

        # Drain #1: deletion semantics (the source row is gone) must stamp
        # the remaining historical rows.
        asyncio.run(freshness.reconcile_ready_evidence(self.pool))
        self.assertEqual(
            stamped_count(remaining),
            2,
            "the drain must stamp source_deleted on the remaining evidence "
            "of a deleted source",
        )
        self.assertEqual(stamped_count(historical_ids), self.HISTORICAL)
        ev_row = self.conn.execute(
            "SELECT source_deleted_at FROM draft_evidence WHERE id = ?",
            (ev_id,),
        ).fetchone()
        self.assertIsNotNone(ev_row["source_deleted_at"])

        # Drain #2: walk completed — the backlog row must be removed.
        asyncio.run(freshness.reconcile_ready_evidence(self.pool))
        self.assertIsNone(
            self._backlog_row(file_id),
            "the deleted-source drain must remove its backlog row once "
            "complete",
        )
        self.assertEqual(
            len(self.open_blockers(draft_id, "source_deleted")), 1
        )


# ── PRR-018: material-change field coverage (tier, input metadata) ─────────


class TestPRR018MaterialChangeFields(FreshnessTestBase):
    """AC14 covered brief and role; this covers the remaining material fields.

    Store-level Ready seeding (the direct-columns alternative AC14's
    docstring describes): a current revision marked Ready via mark_ready,
    then DraftStore.update_draft / update_input_metadata — the same store
    methods the PATCH routes delegate to.
    """

    def _ready_draft(self, with_input=False):
        draft = self.make_draft()
        job = self.make_job(draft.id)
        rev = self.make_revision(draft.id, job)
        input_id = None
        if with_input:
            input_id = self.store.reserve_input(
                draft_id=draft.id,
                owner_id=OWNER_ID,
                role="reference",
                authority="secondary",
                as_of_date="2026-01-01",
                original_name="a.txt",
                stored_name=f"{uuid.uuid4().hex}.txt",
                extension=".txt",
                media_type="text/plain",
                size_bytes=100,
                content_sha256=sha256_text(str(uuid.uuid4())),
                max_inputs=10,
                max_total_input_bytes=10_000_000,
            ).id
        self.mark_ready(draft.id, rev)
        self.assertEqual(self.draft_status(draft.id), "ready")
        return draft.id, rev, input_id

    def _lock_version(self, draft_id):
        return self.store.get_draft(draft_id, OWNER_ID).lock_version

    def _row(self, draft_id):
        return self.conn.execute(
            "SELECT status, ready_revision_id FROM drafts WHERE id = ?",
            (draft_id,),
        ).fetchone()

    def _assert_invalidated(self, draft_id, rev):
        row = self._row(draft_id)
        self.assertEqual(
            row["status"],
            "needs_review",
            "a material change must move a Ready draft to needs_review",
        )
        self.assertIsNone(
            row["ready_revision_id"],
            "a material change must clear the Ready pointer",
        )
        # Note: fact_status stays "passed" BY DESIGN — this is approval
        # invalidation (the same ready_invalidated path create_manual_revision
        # uses), not evidence invalidation (apply_evidence_invalidation).
        # The audit event is the behavioral signature of the shared helper.
        self.assertEqual(self.fact_status(rev), "passed")
        events = self.conn.execute(
            "SELECT COUNT(*) FROM draft_events WHERE draft_id = ? "
            "AND event_type = 'ready_invalidated'",
            (draft_id,),
        ).fetchone()[0]
        self.assertEqual(
            events, 1, "a material change must emit ready_invalidated"
        )

    def _assert_still_ready(self, draft_id, rev):
        row = self._row(draft_id)
        self.assertEqual(row["status"], "ready")
        self.assertEqual(row["ready_revision_id"], rev)

    # -- update_draft: tier ------------------------------------------------

    def test_tier_change_invalidates_ready(self):
        draft_id, rev, _ = self._ready_draft()
        self.store.update_draft(
            draft_id=draft_id,
            owner_id=OWNER_ID,
            lock_version=self._lock_version(draft_id),
            tier="high_stakes",
        )
        self._assert_invalidated(draft_id, rev)

    def test_unchanged_tier_and_title_only_keep_ready(self):
        draft_id, rev, _ = self._ready_draft()
        self.store.update_draft(
            draft_id=draft_id,
            owner_id=OWNER_ID,
            lock_version=self._lock_version(draft_id),
            tier="standard",  # unchanged value
        )
        self._assert_still_ready(draft_id, rev)
        self.store.update_draft(
            draft_id=draft_id,
            owner_id=OWNER_ID,
            lock_version=self._lock_version(draft_id),
            title="Retitled But Ready",  # non-material field
        )
        self._assert_still_ready(draft_id, rev)

    # -- update_input_metadata: authority / as_of_date / locked_spans -------

    def test_material_input_metadata_fields_each_invalidate_ready(self):
        material_changes = [
            ("authority", {"authority": "primary"}),
            ("as_of_date", {"as_of_date": "2027-06-30"}),
            (
                "locked_spans",
                {"locked_spans_json": '[{"start": 0, "end": 10}]'},
            ),
        ]
        for field_name, kwargs in material_changes:
            with self.subTest(field=field_name):
                draft_id, rev, input_id = self._ready_draft(with_input=True)
                self.store.update_input_metadata(
                    draft_id=draft_id,
                    owner_id=OWNER_ID,
                    input_id=input_id,
                    **kwargs,
                )
                self._assert_invalidated(draft_id, rev)

    def test_unchanged_input_metadata_values_keep_ready(self):
        draft_id, rev, input_id = self._ready_draft(with_input=True)
        unchanged_updates = [
            {"authority": "secondary"},  # same as seeded
            {"as_of_date": "2026-01-01"},  # same as seeded
            {"locked_spans_json": "[]"},  # same as the column default
        ]
        for kwargs in unchanged_updates:
            with self.subTest(**kwargs):
                self.store.update_input_metadata(
                    draft_id=draft_id,
                    owner_id=OWNER_ID,
                    input_id=input_id,
                    **kwargs,
                )
                self._assert_still_ready(draft_id, rev)


if __name__ == "__main__":
    unittest.main()
