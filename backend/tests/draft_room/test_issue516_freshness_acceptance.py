"""Issue #516 acceptance tests — Draft Room evidence freshness regressions.

One test per acceptance criterion. These are REGRESSION TESTS for the
required (post-fix) behavior, written against current public APIs so the
same tests pass after the fix without editing them:

* AC1 (DRAFT-001, discriminating): the bulk delete-all vault path must
  invalidate dependent draft evidence in the same transaction, producing the
  same observable outcomes as the single-delete path (Ready -> needs_review,
  current revision invalidated, evidence stamped source_deleted, non-waivable
  source_deleted blocker).
* AC2 (DRAFT-002, discriminating): the bounded startup reconciler must reach
  a stale Ready draft sitting behind an unchanged Ready prefix within two
  sweeps at the same budget.
* AC3 (DRAFT-003, preserving): a no-op wiki markdown save must NOT
  invalidate a Ready draft citing unchanged content.
* AC4 (DRAFT-004, discriminating): current-revision evidence must be
  invalidated even when the source has >= MAX_EVIDENCE_PER_JOB historical
  evidence rows ordered ahead of the current row.
* AC5 (DRAFT-025, discriminating): recompiling a KMS document-sourced entry
  whose body CHANGED must invalidate dependent Ready draft evidence, while
  an unchanged-content upsert and a brand-new-entry upsert must not.

Harness: subclasses FreshnessTestBase from test_draft_evidence_freshness.py
(temp SQLite via init_db + run_migrations, SimpleConnectionPool, real
files/wiki_pages/kms_entries rows). No HTTP: AC1 drives the delete-all
route function directly with a minimal Request — the smallest real caller
that reaches the bulk ``DELETE FROM files WHERE id IN (...)`` path in
delete_all_vault_documents's _atomic_delete.
"""

import asyncio
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

try:
    import lancedb  # noqa: F401
except ImportError:
    import types

    sys.modules["lancedb"] = types.ModuleType("lancedb")

from fastapi import Request

from app.services import draft_evidence_freshness as freshness
from app.services.draft_store import sha256_text
from app.services.kms_store import KMSStore
from app.services.wiki_store import WikiStore
from tests.draft_room.test_draft_evidence_freshness import (
    OWNER_ID,
    VAULT_ID,
    FreshnessTestBase,
)


class DraftIssue516Acceptance(FreshnessTestBase):
    """Issue #516 acceptance criteria, one sentinel-emitting test each."""

    # ── AC1 (DRAFT-001) ─────────────────────────────────────────────────

    def test_ac1_bulk_delete_all_invalidates_dependent_evidence(self):
        try:
            from app.api.routes.documents import delete_all_vault_documents

            # Same seeding the single-delete invalidation tests use: a Ready
            # draft whose compile evidence cites one document in the vault.
            draft = self.make_draft()
            job = self.make_job(draft.id)
            rev = self.make_revision(draft.id, job)
            cited_file = self.make_file(file_hash="hash-bulk-a")
            filler_file = self.make_file(file_hash="hash-bulk-b")
            ev_id = self.add_evidence(
                job,
                label="S1",
                source_kind="document",
                source_content_sha256="hash-bulk-a",
                file_id=cited_file,
                chunk_uid=f"{cited_file}_0",
            )
            self.mark_ready(draft.id, rev)
            self.assertEqual(self.draft_status(draft.id), "ready")

            class _VectorStoreStub:
                db = None  # no vector store: chunk purge is skipped

            request = Request(
                scope={
                    "type": "http",
                    "app": SimpleNamespace(state=SimpleNamespace()),
                }
            )
            response = asyncio.run(
                delete_all_vault_documents(
                    vault_id=VAULT_ID,
                    request=request,
                    conn=self.conn,
                    user={"id": OWNER_ID, "username": "owner", "role": "member"},
                    vector_store=_VectorStoreStub(),
                    _csrf_token="test-csrf-token",
                )
            )

            # Guards: the bulk delete itself must have worked, otherwise a
            # failure below would be a setup failure, not the regression.
            self.assertEqual(response.deleted_count, 2)
            remaining = self.conn.execute(
                "SELECT COUNT(*) FROM files WHERE id IN (?, ?)",
                (cited_file, filler_file),
            ).fetchone()[0]
            self.assertEqual(remaining, 0)

            # REQUIRED BEHAVIOR: the same observable outcomes the
            # single-delete path produces today.
            self.assertEqual(
                self.draft_status(draft.id),
                "needs_review",
                "delete-all must move a dependent Ready draft back to "
                "needs_review when its cited document is deleted",
            )
            self.assertEqual(
                self.fact_status(rev),
                "invalidated",
                "delete-all must invalidate the dependent draft's current "
                "fact result",
            )
            row = self.conn.execute(
                "SELECT passage, source_deleted_at FROM draft_evidence "
                "WHERE id = ?",
                (ev_id,),
            ).fetchone()
            self.assertIsNotNone(row, "evidence snapshot row must survive")
            self.assertEqual(row["passage"], "snapshotted passage")
            self.assertIsNotNone(
                row["source_deleted_at"],
                "delete-all must stamp source_deleted_at on evidence whose "
                "document was bulk-deleted",
            )
            blockers = self.open_blockers(draft.id, "source_deleted")
            self.assertEqual(
                len(blockers),
                1,
                "delete-all must open exactly one source_deleted blocker",
            )
            self.assertEqual(blockers[0]["waivable"], 0)
        except Exception:
            print("AC1 CHECK: FAIL")
            raise
        print("AC1 CHECK: PASS")

    # ── AC2 (DRAFT-002) ─────────────────────────────────────────────────

    def test_ac2_reconcile_cursor_reaches_stale_tail_in_two_sweeps(self):
        try:
            # Unchanged-current Ready prefix, larger than one sweep budget.
            stable_body = "stable kms entry body"
            entry_id = self.make_kms_entry(body=stable_body)
            prefix_draft_ids = []
            for idx in range(3):
                draft = self.make_draft()
                job = self.make_job(draft.id)
                rev = self.make_revision(draft.id, job)
                self.add_evidence(
                    job,
                    label=f"K{idx}",
                    source_kind="kms",
                    source_content_sha256=sha256_text(stable_body),
                    kms_entry_id=entry_id,
                )
                self.mark_ready(draft.id, rev)
                prefix_draft_ids.append(draft.id)

            # Stale tail: HIGHER draft id, source deleted out-of-band.
            tail = self.make_draft()
            tail_job = self.make_job(tail.id)
            tail_rev = self.make_revision(tail.id, tail_job)
            tail_file = self.make_file(file_hash="hash-tail")
            self.add_evidence(
                tail_job,
                label="S1",
                source_kind="document",
                source_content_sha256="hash-tail",
                file_id=tail_file,
                chunk_uid=f"{tail_file}_0",
            )
            self.mark_ready(tail.id, tail_rev)
            self.conn.execute("DELETE FROM files WHERE id = ?", (tail_file,))
            self.conn.commit()
            self.assertGreater(
                tail.id,
                max(prefix_draft_ids),
                "tail draft must sort after the unchanged prefix by id",
            )

            budget = 3
            sweep1 = asyncio.run(
                freshness.reconcile_ready_evidence(self.pool, max_drafts=budget)
            )
            self.assertEqual(sweep1.drafts_scanned, budget)
            self.assertTrue(sweep1.truncated)
            # A bounded first sweep cannot have reached the tail yet.
            self.assertEqual(self.draft_status(tail.id), "ready")
            self.assertEqual(self.fact_status(tail_rev), "passed")

            # REQUIRED BEHAVIOR: the second sweep at the SAME budget must
            # progress past the unchanged prefix and reach the stale tail.
            asyncio.run(
                freshness.reconcile_ready_evidence(self.pool, max_drafts=budget)
            )
            self.assertEqual(
                self.fact_status(tail_rev),
                "invalidated",
                "two sweeps at the same budget must reach a stale Ready "
                "draft behind an unchanged Ready prefix",
            )
            self.assertEqual(self.draft_status(tail.id), "needs_review")
            self.assertEqual(
                len(self.open_blockers(tail.id, "source_deleted")), 1
            )
            # The unchanged prefix must remain Ready throughout.
            for draft_id in prefix_draft_ids:
                self.assertEqual(self.draft_status(draft_id), "ready")
        except Exception:
            print("AC2 CHECK: FAIL")
            raise
        print("AC2 CHECK: PASS")

    # ── AC3 (DRAFT-003, preserving) ─────────────────────────────────────

    def test_ac3_noop_wiki_save_does_not_invalidate_ready_draft(self):
        try:
            markdown = "unchanged page body"
            draft = self.make_draft()
            job = self.make_job(draft.id)
            rev = self.make_revision(draft.id, job)
            page_id = self.make_wiki_page(markdown=markdown)
            self.add_evidence(
                job,
                label="W1",
                source_kind="wiki",
                source_content_sha256=sha256_text(markdown),
                wiki_page_id=page_id,
            )
            self.mark_ready(draft.id, rev)
            self.assertEqual(self.draft_status(draft.id), "ready")

            # Save the IDENTICAL markdown: a no-op save must not invalidate.
            WikiStore(self.conn).update_page(
                page_id, VAULT_ID, markdown=markdown
            )

            self.assertEqual(
                self.draft_status(draft.id),
                "ready",
                "a no-op wiki save must not move a Ready draft out of Ready",
            )
            self.assertEqual(
                self.fact_status(rev),
                "passed",
                "a no-op wiki save must not invalidate the current revision",
            )
            self.assertEqual(
                self.open_blockers(draft.id, "evidence_changed"),
                [],
                "a no-op wiki save must not open an evidence_changed blocker",
            )
        except Exception:
            print("AC3 CHECK: FAIL")
            raise
        print("AC3 CHECK: PASS")

    # ── AC4 (DRAFT-004) ─────────────────────────────────────────────────

    def test_ac4_invalidation_reaches_current_row_beyond_evidence_cap(self):
        try:
            draft = self.make_draft()
            old_job = self.make_job(draft.id)
            self.make_revision(draft.id, old_job, revision_no=1, is_current=0)
            cur_job = self.make_job(draft.id)
            cur_rev = self.make_revision(
                draft.id, cur_job, revision_no=2, is_current=1
            )
            file_id = self.make_file(file_hash="hash-cap-a")

            # Bulk-seed MORE than MAX_EVIDENCE_PER_JOB historical rows for
            # this source (superseded revision's job), all with lower ids
            # than the current row: direct INSERT in one transaction for
            # speed; the row satisfies the same single-identity CHECK the
            # store enforces.
            historical = freshness.MAX_EVIDENCE_PER_JOB + 2
            passage_hash = sha256_text("historical passage")
            self.conn.executemany(
                "INSERT INTO draft_evidence (job_id, label, source_kind, "
                "file_id, title, passage, passage_sha256, "
                "source_content_sha256) "
                "VALUES (?, ?, 'document', ?, 'T', 'historical passage', ?, ?)",
                [
                    (old_job, f"H{i}", file_id, passage_hash, "hash-cap-a")
                    for i in range(historical)
                ],
            )
            self.conn.commit()

            ev_id = self.add_evidence(
                cur_job,
                label="S1",
                source_kind="document",
                source_content_sha256="hash-cap-a",
                file_id=file_id,
                chunk_uid=f"{file_id}_0",
            )
            self.mark_ready(draft.id, cur_rev)
            self.assertEqual(self.draft_status(draft.id), "ready")

            # Seeding guards: the source really has more evidence rows than
            # one hook pass may walk, and the current row sorts last.
            counts = self.conn.execute(
                "SELECT COUNT(*), MAX(id) FROM draft_evidence "
                "WHERE source_kind = 'document' AND file_id = ?",
                (file_id,),
            ).fetchone()
            self.assertEqual(counts[0], historical + 1)
            self.assertEqual(counts[1], ev_id)
            self.assertGreater(ev_id, freshness.MAX_EVIDENCE_PER_JOB)

            # Mutate the source (new content hash) — the update hook.
            self.conn.execute(
                "UPDATE files SET file_hash = 'hash-cap-b' WHERE id = ?",
                (file_id,),
            )
            self.conn.commit()
            freshness.on_document_changed(
                self.conn, file_id=file_id, new_content_sha256="hash-cap-b"
            )

            # REQUIRED BEHAVIOR: the current revision's evidence is reached
            # and invalidated despite the >= MAX_EVIDENCE_PER_JOB historical
            # rows ordered ahead of it.
            self.assertEqual(
                self.fact_status(cur_rev),
                "invalidated",
                "a changed source must invalidate current-revision evidence "
                "even when >= MAX_EVIDENCE_PER_JOB historical rows precede "
                "it in e.id ASC order",
            )
            self.assertEqual(self.draft_status(draft.id), "needs_review")
            self.assertEqual(
                len(self.open_blockers(draft.id, "evidence_changed")), 1
            )
        except Exception:
            print("AC4 CHECK: FAIL")
            raise
        print("AC4 CHECK: PASS")

    # ── AC5 (DRAFT-025) ─────────────────────────────────────────────────

    def test_ac5_kms_recompile_changed_body_invalidates(self):
        try:
            kms = KMSStore(self.conn)
            file_a = self.make_file(file_hash="kms-src-a")
            file_b = self.make_file(file_hash="kms-src-b")
            body_v1 = "compiled entry body v1"
            entry = kms.upsert_document_entry(
                VAULT_ID, file_a, title="Doc A", body=body_v1
            )
            self.assertIsNotNone(entry)

            draft = self.make_draft()
            job = self.make_job(draft.id)
            rev = self.make_revision(draft.id, job)
            self.add_evidence(
                job,
                label="K1",
                source_kind="kms",
                source_content_sha256=sha256_text(body_v1),
                kms_entry_id=entry.id,
            )
            self.mark_ready(draft.id, rev)
            self.assertEqual(self.draft_status(draft.id), "ready")

            # Control 1: an unchanged-content recompile must NOT invalidate.
            kms.upsert_document_entry(
                VAULT_ID, file_a, title="Doc A", body=body_v1
            )
            self.assertEqual(self.draft_status(draft.id), "ready")
            self.assertEqual(self.fact_status(rev), "passed")
            self.assertEqual(self.open_blockers(draft.id, "evidence_changed"), [])

            # Control 2: a brand-new (no-prior-entry) upsert must NOT
            # invalidate drafts citing other entries.
            fresh = kms.upsert_document_entry(
                VAULT_ID, file_b, title="Doc B", body="brand new body"
            )
            self.assertNotEqual(fresh.id, entry.id)
            self.assertEqual(self.draft_status(draft.id), "ready")
            self.assertEqual(self.fact_status(rev), "passed")
            self.assertEqual(self.open_blockers(draft.id, "evidence_changed"), [])

            # REQUIRED BEHAVIOR: recompiling the SAME entry with a CHANGED
            # body invalidates the dependent Ready draft's evidence.
            body_v2 = "compiled entry body v2 (changed)"
            updated = kms.upsert_document_entry(
                VAULT_ID, file_a, title="Doc A", body=body_v2
            )
            # Guard: the update branch really ran and changed the body.
            self.assertEqual(updated.id, entry.id)
            self.assertEqual(kms.get_entry(entry.id).body, body_v2)

            self.assertEqual(
                self.fact_status(rev),
                "invalidated",
                "recompiling a KMS document-sourced entry whose body changed "
                "must invalidate dependent current-revision evidence",
            )
            self.assertEqual(self.draft_status(draft.id), "needs_review")
            blockers = self.open_blockers(draft.id, "evidence_changed")
            self.assertEqual(len(blockers), 1)
            self.assertEqual(blockers[0]["waivable"], 0)
        except Exception:
            print("AC5 CHECK: FAIL")
            raise
        print("AC5 CHECK: PASS")


if __name__ == "__main__":
    unittest.main()
