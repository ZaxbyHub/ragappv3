"""Evidence freshness and invalidation tests (issue #436 section 7, SPEC 12.6).

Exercises ``app.services.draft_evidence_freshness`` against a real temp SQLite
database with real ``files``/``wiki_pages``/``wiki_claims``/``kms_entries`` rows,
so every identity family is resolved the way production resolves it. No HTTP.

Harness mirrors test_draft_store.py: init_db + run_migrations on a temp file,
connections from ``_db_pool.SimpleConnectionPool``.
"""

import asyncio
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

try:
    import lancedb  # noqa: F401
except ImportError:
    import types

    sys.modules["lancedb"] = types.ModuleType("lancedb")

from _db_pool import SimpleConnectionPool

from app.services import draft_evidence_freshness as freshness
from app.services.draft_store import DraftStore, sha256_text
from app.services.kms_store import KMSStore
from app.services.wiki_store import WikiStore

OWNER_ID = 91001
VAULT_ID = 91001


class FreshnessTestBase(unittest.TestCase):
    def setUp(self):
        self._temp_dir = tempfile.mkdtemp()
        self._db_path = str(Path(self._temp_dir) / "app.db")

        from app.models.database import init_db, run_migrations

        init_db(self._db_path)
        run_migrations(self._db_path)

        self.pool = SimpleConnectionPool(self._db_path)
        self.conn = self.pool.get_connection()
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute(
            "INSERT OR IGNORE INTO users (id, username, hashed_password, full_name, "
            "role, is_active) VALUES (?, 'owner', 'h', 'Owner', 'member', 1)",
            (OWNER_ID,),
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO vaults (id, name, description) VALUES (?, 'V1', '')",
            (VAULT_ID,),
        )
        self.conn.commit()
        self.store = DraftStore(self.conn)

    def tearDown(self):
        self.pool.close_all()
        shutil.rmtree(self._temp_dir, ignore_errors=True)

    # ── fixtures ─────────────────────────────────────────────────────────

    def make_draft(self, *, status="needs_review"):
        draft = self.store.create_draft(
            vault_id=VAULT_ID,
            created_by=OWNER_ID,
            title="D",
            mode="compose",
            tier="standard",
            brief_json="{}",
        )
        self.conn.execute(
            "UPDATE drafts SET status = ? WHERE id = ?", (status, draft.id)
        )
        self.conn.commit()
        return draft

    def make_job(self, draft_id):
        cur = self.conn.execute(
            "INSERT INTO draft_jobs (draft_id, vault_id, created_by, job_type, "
            "status, max_model_calls, timeout_seconds) "
            "VALUES (?,?,?, 'compile', 'completed', 0, 60)",
            (draft_id, VAULT_ID, OWNER_ID),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def make_revision(self, draft_id, job_id, *, revision_no=1, is_current=1,
                      fact_status="passed"):
        content = f"Body {revision_no}"
        cur = self.conn.execute(
            "INSERT INTO draft_revisions (draft_id, job_id, revision_no, source, "
            "content_md, content_sha256, fact_status, is_current, created_by) "
            "VALUES (?, ?, ?, 'pipeline', ?, ?, ?, ?, ?)",
            (
                draft_id,
                job_id,
                revision_no,
                content,
                sha256_text(content),
                fact_status,
                is_current,
                OWNER_ID,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def mark_ready(self, draft_id, revision_id):
        self.conn.execute(
            "UPDATE drafts SET status = 'ready', ready_revision_id = ?, ready_by = ?, "
            "ready_at = CURRENT_TIMESTAMP WHERE id = ?",
            (revision_id, OWNER_ID, draft_id),
        )
        self.conn.commit()

    # -- live sources --

    def make_file(self, *, file_hash="hash-a", vault_id=VAULT_ID):
        cur = self.conn.execute(
            "INSERT INTO files (vault_id, file_path, file_name, file_hash, file_size, "
            "status) VALUES (?, '/tmp/a.txt', 'a.txt', ?, 10, 'indexed')",
            (vault_id, file_hash),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def make_wiki_page(self, *, markdown="page body", slug="p1"):
        cur = self.conn.execute(
            "INSERT INTO wiki_pages (vault_id, slug, title, page_type, markdown) "
            "VALUES (?, ?, 'P', 'manual', ?)",
            (VAULT_ID, slug, markdown),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def make_wiki_claim(self, page_id, *, claim_text="the sky is blue"):
        cur = self.conn.execute(
            "INSERT INTO wiki_claims (vault_id, page_id, claim_text, source_type) "
            "VALUES (?, ?, ?, 'manual')",
            (VAULT_ID, page_id, claim_text),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def make_kms_entry(self, *, body="entry body", slug="e1"):
        cur = self.conn.execute(
            "INSERT INTO kms_entries (vault_id, slug, title, body) "
            "VALUES (?, ?, 'E', ?)",
            (VAULT_ID, slug, body),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    # -- evidence --

    def add_evidence(self, job_id, *, label, source_kind, source_content_sha256,
                     **identity):
        return self.store.insert_evidence(
            job_id=job_id,
            label=label,
            source_kind=source_kind,
            title="T",
            passage="snapshotted passage",
            source_content_sha256=source_content_sha256,
            **identity,
        )

    # -- assertions --

    def open_blockers(self, draft_id, rule_id):
        return self.conn.execute(
            "SELECT id, waivable, severity, status FROM draft_findings "
            "WHERE draft_id = ? AND rule_id = ? AND status = 'open'",
            (draft_id, rule_id),
        ).fetchall()

    def fact_status(self, revision_id):
        return self.conn.execute(
            "SELECT fact_status FROM draft_revisions WHERE id = ?", (revision_id,)
        ).fetchone()[0]

    def draft_status(self, draft_id):
        return self.conn.execute(
            "SELECT status FROM drafts WHERE id = ?", (draft_id,)
        ).fetchone()[0]


# ── document identity ───────────────────────────────────────────────────────


class TestDocumentEvidence(FreshnessTestBase):
    def test_unchanged_document_evidence_is_current(self):
        draft = self.make_draft()
        job = self.make_job(draft.id)
        rev = self.make_revision(draft.id, job)
        file_id = self.make_file(file_hash="hash-a")
        self.add_evidence(
            job,
            label="S1",
            source_kind="document",
            source_content_sha256="hash-a",
            file_id=file_id,
            chunk_uid=f"{file_id}_0",
        )

        result = freshness.enforce_evidence_freshness(
            self.conn, draft_id=draft.id, revision_id=rev, job_id=job
        )

        self.assertTrue(result.is_current)
        self.assertEqual(result.checked, 1)
        self.assertEqual(self.fact_status(rev), "passed")
        self.assertEqual(self.open_blockers(draft.id, "evidence_changed"), [])

    def test_content_hash_change_invalidates_with_non_waivable_blocker(self):
        draft = self.make_draft()
        job = self.make_job(draft.id)
        rev = self.make_revision(draft.id, job)
        file_id = self.make_file(file_hash="hash-a")
        self.add_evidence(
            job,
            label="S1",
            source_kind="document",
            source_content_sha256="hash-a",
            file_id=file_id,
            chunk_uid=f"{file_id}_0",
        )
        # The document is re-ingested with different bytes.
        self.conn.execute(
            "UPDATE files SET file_hash = 'hash-b' WHERE id = ?", (file_id,)
        )
        self.conn.commit()

        result = freshness.enforce_evidence_freshness(
            self.conn, draft_id=draft.id, revision_id=rev, job_id=job
        )

        self.assertFalse(result.is_current)
        self.assertEqual(result.reasons, ("evidence_changed",))
        self.assertEqual(self.fact_status(rev), "invalidated")
        blockers = self.open_blockers(draft.id, "evidence_changed")
        self.assertEqual(len(blockers), 1)
        self.assertEqual(blockers[0]["severity"], "blocker")
        self.assertEqual(blockers[0]["waivable"], 0)

    def test_document_moved_out_of_vault_scope_reads_as_deleted(self):
        draft = self.make_draft()
        job = self.make_job(draft.id)
        rev = self.make_revision(draft.id, job)
        self.conn.execute(
            "INSERT OR IGNORE INTO vaults (id, name, description) VALUES (99, 'V9', '')"
        )
        file_id = self.make_file(file_hash="hash-a")
        self.add_evidence(
            job,
            label="S1",
            source_kind="document",
            source_content_sha256="hash-a",
            file_id=file_id,
            chunk_uid=f"{file_id}_0",
        )
        self.conn.execute("UPDATE files SET vault_id = 99 WHERE id = ?", (file_id,))
        self.conn.commit()

        result = freshness.enforce_evidence_freshness(
            self.conn, draft_id=draft.id, revision_id=rev, job_id=job
        )

        self.assertEqual(result.reasons, ("source_deleted",))
        self.assertEqual(len(self.open_blockers(draft.id, "source_deleted")), 1)

    def test_source_deletion_adds_non_waivable_source_deleted_blocker(self):
        draft = self.make_draft()
        job = self.make_job(draft.id)
        rev = self.make_revision(draft.id, job)
        file_id = self.make_file(file_hash="hash-a")
        ev_id = self.add_evidence(
            job,
            label="S1",
            source_kind="document",
            source_content_sha256="hash-a",
            file_id=file_id,
            chunk_uid=f"{file_id}_0",
        )
        self.conn.execute("DELETE FROM files WHERE id = ?", (file_id,))
        self.conn.commit()

        result = freshness.enforce_evidence_freshness(
            self.conn, draft_id=draft.id, revision_id=rev, job_id=job
        )

        self.assertEqual(result.reasons, ("source_deleted",))
        blockers = self.open_blockers(draft.id, "source_deleted")
        self.assertEqual(len(blockers), 1)
        self.assertEqual(blockers[0]["waivable"], 0)
        # The snapshot row survives and is stamped, not removed.
        row = self.conn.execute(
            "SELECT passage, source_deleted_at FROM draft_evidence WHERE id = ?",
            (ev_id,),
        ).fetchone()
        self.assertEqual(row["passage"], "snapshotted passage")
        self.assertIsNotNone(row["source_deleted_at"])


# ── Ready state ─────────────────────────────────────────────────────────────


class TestReadyInvalidation(FreshnessTestBase):
    def test_ready_draft_moves_back_to_needs_review(self):
        draft = self.make_draft()
        job = self.make_job(draft.id)
        rev = self.make_revision(draft.id, job)
        self.mark_ready(draft.id, rev)
        file_id = self.make_file(file_hash="hash-a")
        self.add_evidence(
            job,
            label="S1",
            source_kind="document",
            source_content_sha256="hash-a",
            file_id=file_id,
            chunk_uid=f"{file_id}_0",
        )
        self.conn.execute("DELETE FROM files WHERE id = ?", (file_id,))
        self.conn.commit()
        self.assertEqual(self.draft_status(draft.id), "ready")

        result = freshness.enforce_evidence_freshness(
            self.conn, draft_id=draft.id, revision_id=rev, job_id=job
        )

        self.assertFalse(result.is_current)
        self.assertEqual(self.draft_status(draft.id), "needs_review")
        row = self.conn.execute(
            "SELECT ready_revision_id, ready_at FROM drafts WHERE id = ?", (draft.id,)
        ).fetchone()
        self.assertIsNone(row["ready_revision_id"])
        self.assertIsNone(row["ready_at"])

    def test_invalidation_is_committed_even_if_caller_rolls_back(self):
        """The Ready path raises 409 after this call; the record must survive."""
        draft = self.make_draft()
        job = self.make_job(draft.id)
        rev = self.make_revision(draft.id, job)
        file_id = self.make_file(file_hash="hash-a")
        self.add_evidence(
            job,
            label="S1",
            source_kind="document",
            source_content_sha256="hash-a",
            file_id=file_id,
            chunk_uid=f"{file_id}_0",
        )
        self.conn.execute("DELETE FROM files WHERE id = ?", (file_id,))
        self.conn.commit()

        self.conn.execute("BEGIN IMMEDIATE")
        freshness.enforce_evidence_freshness(
            self.conn, draft_id=draft.id, revision_id=rev, job_id=job,
            actor_user_id=OWNER_ID,
        )
        self.conn.rollback()

        other = self.pool.get_connection()
        try:
            self.assertEqual(
                other.execute(
                    "SELECT fact_status FROM draft_revisions WHERE id = ?", (rev,)
                ).fetchone()[0],
                "invalidated",
            )
        finally:
            self.pool.release_connection(other)

    def test_repeated_enforcement_does_not_duplicate_blockers(self):
        draft = self.make_draft()
        job = self.make_job(draft.id)
        rev = self.make_revision(draft.id, job)
        file_id = self.make_file(file_hash="hash-a")
        self.add_evidence(
            job,
            label="S1",
            source_kind="document",
            source_content_sha256="hash-a",
            file_id=file_id,
            chunk_uid=f"{file_id}_0",
        )
        self.conn.execute("DELETE FROM files WHERE id = ?", (file_id,))
        self.conn.commit()

        for _ in range(3):
            freshness.enforce_evidence_freshness(
                self.conn, draft_id=draft.id, revision_id=rev, job_id=job
            )

        self.assertEqual(len(self.open_blockers(draft.id, "source_deleted")), 1)


# ── Wiki identity, including page-level rows (wiki_claim_id IS NULL) ────────


class TestWikiEvidence(FreshnessTestBase):
    def test_page_level_evidence_resolves_against_page_markdown(self):
        draft = self.make_draft()
        job = self.make_job(draft.id)
        rev = self.make_revision(draft.id, job)
        page_id = self.make_wiki_page(markdown="original page body")
        self.add_evidence(
            job,
            label="W1",
            source_kind="wiki",
            source_content_sha256=sha256_text("original page body"),
            wiki_page_id=page_id,
        )

        current = freshness.check_evidence_freshness(self.conn, job_id=job)
        self.assertTrue(current.is_current, current.issues)
        self.assertEqual(current.checked, 1)

        self.conn.execute(
            "UPDATE wiki_pages SET markdown = 'rewritten body' WHERE id = ?", (page_id,)
        )
        self.conn.commit()

        result = freshness.enforce_evidence_freshness(
            self.conn, draft_id=draft.id, revision_id=rev, job_id=job
        )
        self.assertEqual(result.reasons, ("evidence_changed",))
        self.assertEqual(result.issues[0].identity, f"wiki_page_id={page_id}")
        self.assertEqual(self.fact_status(rev), "invalidated")

    def test_page_deletion_invalidates_page_level_evidence(self):
        draft = self.make_draft()
        job = self.make_job(draft.id)
        rev = self.make_revision(draft.id, job)
        page_id = self.make_wiki_page(markdown="body")
        self.add_evidence(
            job,
            label="W1",
            source_kind="wiki",
            source_content_sha256=sha256_text("body"),
            wiki_page_id=page_id,
        )
        self.conn.execute("DELETE FROM wiki_pages WHERE id = ?", (page_id,))
        self.conn.commit()

        result = freshness.enforce_evidence_freshness(
            self.conn, draft_id=draft.id, revision_id=rev, job_id=job
        )
        self.assertEqual(result.reasons, ("source_deleted",))

    def test_claim_level_evidence_resolves_claim_and_page(self):
        draft = self.make_draft()
        job = self.make_job(draft.id)
        rev = self.make_revision(draft.id, job)
        page_id = self.make_wiki_page(markdown="body")
        claim_id = self.make_wiki_claim(page_id, claim_text="the sky is blue")
        self.add_evidence(
            job,
            label="W1",
            source_kind="wiki",
            source_content_sha256=sha256_text("the sky is blue"),
            wiki_page_id=page_id,
            wiki_claim_id=claim_id,
        )

        self.assertTrue(freshness.check_evidence_freshness(self.conn, job_id=job).is_current)

        self.conn.execute(
            "UPDATE wiki_claims SET claim_text = 'the sky is green' WHERE id = ?",
            (claim_id,),
        )
        self.conn.commit()
        result = freshness.enforce_evidence_freshness(
            self.conn, draft_id=draft.id, revision_id=rev, job_id=job
        )
        self.assertEqual(result.reasons, ("evidence_changed",))
        self.assertEqual(result.issues[0].identity, f"wiki_claim_id={claim_id}")

    def test_page_update_hook_invalidates_page_and_claim_level_rows(self):
        draft = self.make_draft()
        job = self.make_job(draft.id)
        rev = self.make_revision(draft.id, job)
        page_id = self.make_wiki_page(markdown="body")
        claim_id = self.make_wiki_claim(page_id, claim_text="claim text")
        self.add_evidence(
            job,
            label="W1",
            source_kind="wiki",
            source_content_sha256=sha256_text("body"),
            wiki_page_id=page_id,
        )
        self.add_evidence(
            job,
            label="W2",
            source_kind="wiki",
            source_content_sha256=sha256_text("claim text"),
            wiki_page_id=page_id,
            wiki_claim_id=claim_id,
        )

        WikiStore(self.conn).update_page(page_id, VAULT_ID, markdown="brand new body")

        self.assertEqual(self.fact_status(rev), "invalidated")
        self.assertEqual(len(self.open_blockers(draft.id, "evidence_changed")), 1)

    def test_claim_update_and_delete_hooks_invalidate(self):
        wiki = WikiStore(self.conn)
        for idx, delete in enumerate((False, True)):
            draft = self.make_draft()
            job = self.make_job(draft.id)
            rev = self.make_revision(draft.id, job)
            page_id = self.make_wiki_page(markdown="body", slug=f"pg{idx}")
            claim_id = self.make_wiki_claim(page_id, claim_text=f"claim {idx}")
            self.add_evidence(
                job,
                label="W1",
                source_kind="wiki",
                source_content_sha256=sha256_text(f"claim {idx}"),
                wiki_page_id=page_id,
                wiki_claim_id=claim_id,
            )

            if delete:
                wiki.delete_claim(claim_id, VAULT_ID)
                rule = "source_deleted"
            else:
                wiki.update_claim(claim_id, VAULT_ID, claim_text="edited claim")
                rule = "evidence_changed"

            self.assertEqual(self.fact_status(rev), "invalidated")
            self.assertEqual(len(self.open_blockers(draft.id, rule)), 1)

    def test_page_delete_hook_marks_evidence_deleted_without_removing_it(self):
        draft = self.make_draft()
        job = self.make_job(draft.id)
        self.make_revision(draft.id, job)
        page_id = self.make_wiki_page(markdown="body")
        ev_id = self.add_evidence(
            job,
            label="W1",
            source_kind="wiki",
            source_content_sha256=sha256_text("body"),
            wiki_page_id=page_id,
        )

        WikiStore(self.conn).delete_page(page_id, VAULT_ID)

        row = self.conn.execute(
            "SELECT passage, source_deleted_at FROM draft_evidence WHERE id = ?",
            (ev_id,),
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["passage"], "snapshotted passage")
        self.assertIsNotNone(row["source_deleted_at"])


# ── KMS identity ────────────────────────────────────────────────────────────


class TestKMSEvidence(FreshnessTestBase):
    def test_entry_update_hook_invalidates(self):
        draft = self.make_draft()
        job = self.make_job(draft.id)
        rev = self.make_revision(draft.id, job)
        entry_id = self.make_kms_entry(body="entry body")
        self.add_evidence(
            job,
            label="K1",
            source_kind="kms",
            source_content_sha256=sha256_text("entry body"),
            kms_entry_id=entry_id,
        )

        KMSStore(self.conn).update_entry(entry_id, VAULT_ID, body="rewritten entry")

        self.assertEqual(self.fact_status(rev), "invalidated")
        blockers = self.open_blockers(draft.id, "evidence_changed")
        self.assertEqual(len(blockers), 1)
        self.assertEqual(blockers[0]["waivable"], 0)

    def test_entry_delete_hook_invalidates_and_marks(self):
        draft = self.make_draft()
        job = self.make_job(draft.id)
        rev = self.make_revision(draft.id, job)
        entry_id = self.make_kms_entry(body="entry body")
        ev_id = self.add_evidence(
            job,
            label="K1",
            source_kind="kms",
            source_content_sha256=sha256_text("entry body"),
            kms_entry_id=entry_id,
        )

        KMSStore(self.conn).delete_entry(entry_id, VAULT_ID)

        self.assertEqual(self.fact_status(rev), "invalidated")
        self.assertEqual(len(self.open_blockers(draft.id, "source_deleted")), 1)
        self.assertIsNotNone(
            self.conn.execute(
                "SELECT source_deleted_at FROM draft_evidence WHERE id = ?", (ev_id,)
            ).fetchone()[0]
        )

    def test_no_op_body_save_does_not_invalidate(self):
        draft = self.make_draft()
        job = self.make_job(draft.id)
        rev = self.make_revision(draft.id, job)
        entry_id = self.make_kms_entry(body="entry body")
        self.add_evidence(
            job,
            label="K1",
            source_kind="kms",
            source_content_sha256=sha256_text("entry body"),
            kms_entry_id=entry_id,
        )

        KMSStore(self.conn).update_entry(entry_id, VAULT_ID, body="entry body")

        self.assertEqual(self.fact_status(rev), "passed")
        self.assertEqual(self.open_blockers(draft.id, "evidence_changed"), [])


# ── Historical evidence retention ───────────────────────────────────────────


class TestHistoricalEvidence(FreshnessTestBase):
    def test_superseded_revision_evidence_is_marked_but_not_reused_or_deleted(self):
        draft = self.make_draft()
        old_job = self.make_job(draft.id)
        old_rev = self.make_revision(draft.id, old_job, revision_no=1, is_current=0)
        new_job = self.make_job(draft.id)
        new_rev = self.make_revision(draft.id, new_job, revision_no=2, is_current=1)

        entry_id = self.make_kms_entry(body="entry body")
        old_ev = self.add_evidence(
            old_job,
            label="K1",
            source_kind="kms",
            source_content_sha256=sha256_text("entry body"),
            kms_entry_id=entry_id,
        )
        new_ev = self.add_evidence(
            new_job,
            label="K1",
            source_kind="kms",
            source_content_sha256=sha256_text("entry body"),
            kms_entry_id=entry_id,
        )

        KMSStore(self.conn).delete_entry(entry_id, VAULT_ID)

        # Both snapshots survive and both are marked; only the current revision
        # is invalidated.
        rows = self.conn.execute(
            "SELECT id, passage, source_deleted_at FROM draft_evidence "
            "WHERE id IN (?, ?) ORDER BY id",
            (old_ev, new_ev),
        ).fetchall()
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(row["passage"], "snapshotted passage")
            self.assertIsNotNone(row["source_deleted_at"])
        self.assertEqual(self.fact_status(new_rev), "invalidated")
        self.assertEqual(self.fact_status(old_rev), "passed")

        # A new run never sees the marked rows: evidence is keyed by job_id and
        # each run is a new job.
        self.assertEqual(
            [e.id for e in self.store.list_evidence_identities(job_id=new_job)],
            [new_ev],
        )
        self.assertEqual(
            [e.id for e in self.store.list_evidence_identities(job_id=self.make_job(draft.id))],
            [],
        )


# ── Hooks are non-fatal ─────────────────────────────────────────────────────


class TestHooksNeverRaise(FreshnessTestBase):
    def test_hooks_survive_missing_draft_tables(self):
        bare_path = str(Path(self._temp_dir) / "bare.db")
        bare = sqlite3.connect(bare_path)
        try:
            freshness.on_document_changed(bare, file_id=1, new_content_sha256="x")
            freshness.on_wiki_page_changed(bare, page_id=1, new_markdown="x")
            freshness.on_wiki_claim_changed(bare, claim_id=1, new_claim_text=None)
            freshness.on_kms_entry_changed(bare, entry_id=1, new_body=None)
        finally:
            bare.close()

    def test_hooks_survive_a_closed_connection(self):
        dead = sqlite3.connect(self._db_path)
        dead.close()
        freshness.on_document_changed(dead, file_id=1, new_content_sha256=None)
        freshness.on_kms_entry_changed(dead, entry_id=1, new_body="x")

    def test_hooks_survive_a_locked_database(self):
        blocker = sqlite3.connect(self._db_path, timeout=0.1)
        blocker.execute("BEGIN EXCLUSIVE")
        try:
            reader = sqlite3.connect(self._db_path, timeout=0.1)
            try:
                freshness.on_document_changed(
                    reader, file_id=1, new_content_sha256=None
                )
                freshness.on_wiki_page_changed(reader, page_id=1, new_markdown=None)
            finally:
                reader.close()
        finally:
            blocker.rollback()
            blocker.close()

    def test_host_delete_still_succeeds_when_invalidation_cannot_run(self):
        """The KMS delete must commit even if the hook blows up."""
        entry_id = self.make_kms_entry(body="entry body")
        original = freshness._invalidate_for_source

        def boom(*args, **kwargs):
            raise RuntimeError("hook exploded")

        freshness._invalidate_for_source = boom
        try:
            self.assertTrue(KMSStore(self.conn).delete_entry(entry_id, VAULT_ID))
        finally:
            freshness._invalidate_for_source = original
        self.assertIsNone(
            self.conn.execute(
                "SELECT id FROM kms_entries WHERE id = ?", (entry_id,)
            ).fetchone()
        )

    def test_hook_with_null_identity_is_a_noop(self):
        freshness.on_document_changed(self.conn, file_id=None)
        freshness.on_wiki_claim_changed(self.conn, claim_id=None)


# ── Startup reconciler ──────────────────────────────────────────────────────


class TestReadyReconciler(FreshnessTestBase):
    def _ready_draft_with_deleted_source(self):
        draft = self.make_draft()
        job = self.make_job(draft.id)
        rev = self.make_revision(draft.id, job)
        file_id = self.make_file(file_hash="hash-a")
        self.add_evidence(
            job,
            label="S1",
            source_kind="document",
            source_content_sha256="hash-a",
            file_id=file_id,
            chunk_uid=f"{file_id}_0",
        )
        self.mark_ready(draft.id, rev)
        # Out-of-band deletion: no hook ever ran.
        self.conn.execute("DELETE FROM files WHERE id = ?", (file_id,))
        self.conn.commit()
        return draft.id, rev

    def test_reconciler_catches_out_of_band_deletion(self):
        draft_id, rev = self._ready_draft_with_deleted_source()

        summary = asyncio.run(freshness.reconcile_ready_evidence(self.pool))

        self.assertEqual(summary.drafts_scanned, 1)
        self.assertEqual(summary.drafts_invalidated, 1)
        self.assertEqual(summary.evidence_checked, 1)
        self.assertFalse(summary.truncated)
        self.assertEqual(self.draft_status(draft_id), "needs_review")
        self.assertEqual(self.fact_status(rev), "invalidated")
        self.assertEqual(len(self.open_blockers(draft_id, "source_deleted")), 1)

    def test_second_run_is_idempotent(self):
        draft_id, _rev = self._ready_draft_with_deleted_source()

        first = asyncio.run(freshness.reconcile_ready_evidence(self.pool))
        second = asyncio.run(freshness.reconcile_ready_evidence(self.pool))

        self.assertEqual(first.drafts_invalidated, 1)
        # The draft left Ready, so the second pass has nothing to scan and
        # creates no second blocker.
        self.assertEqual(second.drafts_scanned, 0)
        self.assertEqual(second.drafts_invalidated, 0)
        self.assertEqual(len(self.open_blockers(draft_id, "source_deleted")), 1)

    def test_reconciler_is_bounded_by_max_drafts_and_pages(self):
        for _ in range(5):
            self._ready_draft_with_deleted_source()

        summary = asyncio.run(
            freshness.reconcile_ready_evidence(self.pool, page_size=2, max_drafts=3)
        )

        self.assertEqual(summary.drafts_scanned, 3)
        self.assertTrue(summary.truncated)
        still_ready = self.conn.execute(
            "SELECT COUNT(*) FROM drafts WHERE status = 'ready'"
        ).fetchone()[0]
        self.assertEqual(still_ready, 2)

        # The next pass resumes and finishes the sweep.
        rest = asyncio.run(
            freshness.reconcile_ready_evidence(self.pool, page_size=2, max_drafts=3)
        )
        self.assertEqual(rest.drafts_scanned, 2)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM drafts WHERE status = 'ready'"
            ).fetchone()[0],
            0,
        )

    def test_reconciler_leaves_current_ready_drafts_alone(self):
        draft = self.make_draft()
        job = self.make_job(draft.id)
        rev = self.make_revision(draft.id, job)
        entry_id = self.make_kms_entry(body="entry body")
        self.add_evidence(
            job,
            label="K1",
            source_kind="kms",
            source_content_sha256=sha256_text("entry body"),
            kms_entry_id=entry_id,
        )
        self.mark_ready(draft.id, rev)

        summary = asyncio.run(freshness.reconcile_ready_evidence(self.pool))

        self.assertEqual(summary.drafts_scanned, 1)
        self.assertEqual(summary.drafts_invalidated, 0)
        self.assertEqual(self.draft_status(draft.id), "ready")

    def test_reconciler_skips_manual_revisions_without_a_job(self):
        draft = self.make_draft()
        cur = self.conn.execute(
            "INSERT INTO draft_revisions (draft_id, job_id, revision_no, source, "
            "content_md, content_sha256, fact_status, is_current, created_by) "
            "VALUES (?, NULL, 1, 'manual', 'x', ?, 'not_run', 1, ?)",
            (draft.id, sha256_text("x"), OWNER_ID),
        )
        self.mark_ready(draft.id, int(cur.lastrowid))

        summary = asyncio.run(freshness.reconcile_ready_evidence(self.pool))

        self.assertEqual(summary.drafts_scanned, 1)
        self.assertEqual(summary.drafts_invalidated, 0)
        self.assertEqual(self.draft_status(draft.id), "ready")

    def test_reconciler_never_raises_on_a_broken_pool(self):
        class BrokenPool:
            def get_connection(self):
                raise RuntimeError("pool is gone")

            def release_connection(self, conn):
                pass

        summary = asyncio.run(freshness.reconcile_ready_evidence(BrokenPool()))
        self.assertEqual(summary.drafts_scanned, 0)


# ── Bounding of the per-job evidence pass ───────────────────────────────────


class TestEvidencePassBounds(FreshnessTestBase):
    def test_exceeding_the_evidence_ceiling_fails_closed(self):
        draft = self.make_draft()
        job = self.make_job(draft.id)
        rev = self.make_revision(draft.id, job)
        entry_id = self.make_kms_entry(body="entry body")
        for i in range(3):
            self.add_evidence(
                job,
                label=f"K{i}",
                source_kind="kms",
                source_content_sha256=sha256_text("entry body"),
                kms_entry_id=entry_id,
            )

        result = freshness.check_evidence_freshness(
            self.conn, job_id=job, page_size=1, max_evidence=2
        )

        self.assertEqual(result.checked, 2)
        self.assertTrue(result.truncated)
        self.assertFalse(result.is_current)
        self.assertEqual(result.reasons, ("evidence_changed",))

        enforced = freshness.enforce_evidence_freshness(
            self.conn,
            draft_id=draft.id,
            revision_id=rev,
            job_id=job,
            page_size=1,
            max_evidence=2,
        )
        self.assertFalse(enforced.is_current)
        self.assertEqual(self.fact_status(rev), "invalidated")

    def test_pagination_covers_every_row(self):
        draft = self.make_draft()
        job = self.make_job(draft.id)
        entry_id = self.make_kms_entry(body="entry body")
        for i in range(7):
            self.add_evidence(
                job,
                label=f"K{i}",
                source_kind="kms",
                source_content_sha256=sha256_text("entry body"),
                kms_entry_id=entry_id,
            )

        result = freshness.check_evidence_freshness(self.conn, job_id=job, page_size=2)

        self.assertEqual(result.checked, 7)
        self.assertFalse(result.truncated)
        self.assertTrue(result.is_current)


if __name__ == "__main__":
    unittest.main()
