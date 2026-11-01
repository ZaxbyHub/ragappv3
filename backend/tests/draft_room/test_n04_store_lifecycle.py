"""Frozen store-level acceptance checks for the issue #788 Draft Room audit
(N04).

Store-level companions to ``test_n04_disposition_lifecycle.py`` -- no HTTP,
``DraftStore`` against a real temp SQLite database:

* **AC7** — ``'running'`` sits in ``DRAFT_STATUSES`` but no writer ever sets
  a draft to ``running`` (``claim_next_compile_job`` only moves
  ``draft_jobs``), so the vocabulary and the persisted states disagree. RED
  at base, GREEN after the fix (either the writer moves the draft or the
  vocabulary drops the unreachable status).
* **AC8** — ``archive_draft`` clears the Ready pointer (``ready_revision_id``)
  so a restored draft cannot report a stale Ready revision. RED at base
  (archive/restore never clear it), GREEN after the fix.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.services.draft_store import DRAFT_STATUSES, sha256_text  # noqa: E402
from tests.draft_room.test_draft_store import (  # noqa: E402
    OWNER_ID,
    DraftStoreTestBase,
)


class TestN04StoreLifecycle(DraftStoreTestBase):
    """AC7, AC8: the store-level lifecycle edges of issue #788."""

    longMessage = False

    def test_claimed_compile_status_matches_the_vocabulary(self):
        """AC7: a claimed compile leaves the draft in a status the vocabulary
        actually models -- RED at base (the draft stays 'queued' although
        'running' is in DRAFT_STATUSES and its job was just claimed), GREEN
        after the fix."""
        draft = self._make_draft()
        self.conn.execute(
            "INSERT INTO draft_jobs (draft_id, vault_id, created_by, job_type, "
            "status, max_model_calls, timeout_seconds, prompt_bundle_version) "
            "VALUES (?, ?, ?, 'compile', 'pending', 40, 1800, 'test')",
            (draft.id, draft.vault_id, OWNER_ID),
        )
        self.conn.execute(
            "UPDATE drafts SET status = 'queued' WHERE id = ?", (draft.id,)
        )
        self.conn.commit()

        self.store.claim_next_compile_job(worker_id="n04-worker")

        row = self.conn.execute(
            "SELECT status FROM drafts WHERE id = ?", (draft.id,)
        ).fetchone()
        consistent = ("running" not in DRAFT_STATUSES) or row[0] == "running"
        self.assertTrue(consistent, f"assert {consistent}")

    def test_archive_clears_the_ready_pointer(self):
        """AC8: archive drops ready_revision_id -- RED at base (the pointer
        survives archiving), GREEN after the fix."""
        draft = self._make_draft()
        content_md = "# Body"
        cur = self.conn.execute(
            "INSERT INTO draft_revisions (draft_id, revision_no, source, "
            "content_md, content_sha256, fact_status, is_current, created_by) "
            "VALUES (?, 1, 'pipeline', ?, ?, 'passed', 1, ?)",
            (draft.id, content_md, sha256_text(content_md), OWNER_ID),
        )
        revision_id = int(cur.lastrowid)
        self.conn.execute(
            "UPDATE drafts SET status = 'ready', ready_revision_id = ?, "
            "ready_by = ?, ready_at = CURRENT_TIMESTAMP WHERE id = ?",
            (revision_id, OWNER_ID, draft.id),
        )
        self.conn.commit()

        lv = self.conn.execute(
            "SELECT lock_version FROM drafts WHERE id = ?", (draft.id,)
        ).fetchone()[0]
        self.store.archive_draft(draft_id=draft.id, owner_id=OWNER_ID, lock_version=lv)

        pointer = self.conn.execute(
            "SELECT ready_revision_id FROM drafts WHERE id = ?", (draft.id,)
        ).fetchone()[0]
        self.assertIsNone(pointer, f"assert {pointer} is None")


if __name__ == "__main__":
    unittest.main()
