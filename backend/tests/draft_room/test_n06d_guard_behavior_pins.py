"""Behavioral negative pins for the #927 transition-discipline guard (TF-001/ST-01).

The static registry (``test_n06b_status_transition_map.py``) verifies that
guard predicates and allowlist constants EXIST; these pins verify they
actually GATE their writes. Each test puts the draft in a prior state outside
the registered constraint, calls the real route or function, and asserts the
refusal plus an unchanged row — so the dedent/raise-to-pass/consult-deletion
mutants that left the static guard green (swarm-pr-review pr1133-r2-20261114,
probe-executed) turn these RED. RED at base is unreachable-by-construction
(the file did not exist); discrimination is proven by the probes this file's
docstring names.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.services.draft_pipeline import _move_draft  # noqa: E402
from tests.draft_room.test_draft_compile_routes import (  # noqa: E402
    CompileRouteTestBase,
)
from tests.draft_room.test_draft_store import (  # noqa: E402
    DraftStoreTestBase,
    OWNER_ID,
)


class TestMarkReadyPriorGuard(CompileRouteTestBase):
    """TF-001 site 1: `_sync_mark_ready` must refuse non-needs_review priors.

    Raising to `pass` (the probe mutant) marked a `queued` draft ready with
    HTTP 200; this pin demands the 409 and an unchanged row.
    """

    longMessage = False
    CONTENT = "# Heading\n\nThis needs a fix here and stays fine elsewhere."

    def test_ready_from_queued_is_refused_and_row_unchanged(self):
        self.draft_id, self.job_id, self.revision_id = self._seed_ready_eligible_draft(
            content_md=self.CONTENT
        )
        conn, _store = self._store()
        try:
            lock_before = self._lock_version(self.draft_id)
            conn.execute(
                "UPDATE drafts SET status = 'queued' WHERE id = ?",
                (self.draft_id,),
            )
            conn.commit()
        finally:
            self._release(conn)

        resp = self._mark_ready(self.draft_id, self.revision_id)
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json().get("code"), "invalid_state")

        conn, _store = self._store()
        try:
            row = conn.execute(
                "SELECT status, lock_version FROM drafts WHERE id = ?",
                (self.draft_id,),
            ).fetchone()
        finally:
            self._release(conn)
        self.assertEqual(row[0], "queued", "the draft must stay queued")
        self.assertEqual(
            int(row[1]),
            int(lock_before),
            "a refused ready must not bump lock_version",
        )


class TestMoveDraftPriorGuard(DraftStoreTestBase):
    """TF-001 site 2: `_move_draft` writes only from queued/running priors.

    Dedenting the UPDATE out of the guarded `if` (the probe mutant) moved a
    `ready` draft to `failed`; this pin demands the no-write.
    """

    longMessage = False

    def test_move_draft_from_ready_prior_writes_nothing(self):
        draft = self._make_draft()
        self.conn.execute(
            "UPDATE drafts SET status = 'ready' WHERE id = ?", (draft.id,)
        )
        self.conn.commit()

        _move_draft(self.conn, draft.id, "failed")

        row = self.conn.execute(
            "SELECT status, lock_version FROM drafts WHERE id = ?", (draft.id,)
        ).fetchone()
        self.assertEqual(
            row[0],
            "ready",
            "a ready draft is outside _move_draft's registered priors; the "
            "write must not land",
        )


class TestCompileEnqueueAllowlistGuard(CompileRouteTestBase):
    """ST-01 site 1: enqueue consults `_COMPILE_ALLOWED_PRIOR_STATUSES`.

    Deleting the call-site check (the probe mutant) let an archived draft be
    enqueued; this pin demands the refusal and an unchanged row.
    """

    longMessage = False

    def test_enqueue_from_archived_is_refused(self):
        """`archived` is outside `_COMPILE_ALLOWED_PRIOR_STATUSES`; the
        allowlist check at draft_room.py:2112 is the invalid_state raiser."""
        self.draft_id, self.job_id, self.revision_id = self._seed_ready_eligible_draft()
        conn, _store = self._store()
        try:
            lock_before = self._lock_version(self.draft_id)
            conn.execute(
                "UPDATE drafts SET status = 'archived' WHERE id = ?",
                (self.draft_id,),
            )
            conn.commit()
        finally:
            self._release(conn)

        lv = self._lock_version(self.draft_id)
        resp = self.client.post(
            f"/api/draft-room/drafts/{self.draft_id}/compile",
            json={"base_revision_id": self.revision_id, "lock_version": lv},
            headers=self._owner_headers(),
        )
        self.assertEqual(resp.status_code, 409, resp.text)
        # The allowlist check itself raises invalid_state here.
        self.assertEqual(resp.json().get("code"), "invalid_state")

        conn, _store = self._store()
        try:
            row = conn.execute(
                "SELECT status, lock_version FROM drafts WHERE id = ?",
                (self.draft_id,),
            ).fetchone()
        finally:
            self._release(conn)
        self.assertEqual(row[0], "archived", "the draft must stay archived")
        self.assertEqual(int(row[1]), int(lock_before), "no lock bump on refusal")


class TestApplyFindingAllowlistGuard(CompileRouteTestBase):
    """ST-01 site 2: apply consults `_APPLY_FINDING_ALLOWED_PRIOR_STATUSES`."""

    longMessage = False
    CONTENT = "# Heading\n\nThis needs a fix here and stays fine elsewhere."

    def _seed_open_finding(self, prior_status):
        """An open finding on a draft moved to `prior_status` (an allowlist
        outsider for apply: not in {draft, needs_review, ready, failed,
        cancelled}). `queued` is used because `archived` is refused earlier in
        the apply path by a dedicated archived check."""
        finding_id = self._insert_finding()
        conn, _store = self._store()
        try:
            lock_before = self._lock_version(self.draft_id)
            conn.execute(
                "UPDATE drafts SET status = ? WHERE id = ?",
                (prior_status, self.draft_id),
            )
            conn.commit()
        finally:
            self._release(conn)
        return finding_id, lock_before

    def _insert_finding(self):
        conn, store = self._store()
        try:
            span_start = self.CONTENT.index("a fix")
            return store.insert_finding(
                draft_id=self.draft_id,
                stage="standards",
                rule_id="rule-x",
                rule_version="1",
                category="style",
                severity="warning",
                message="needs a fix",
                revision_id=self.revision_id,
                job_id=self.job_id,
                waivable=True,
                suggestion="a correction",
                span_start=span_start,
                span_end=span_start + len("a fix"),
            )
        finally:
            self._release(conn)

    def test_apply_from_queued_is_refused(self):
        self.draft_id, self.job_id, self.revision_id = self._seed_ready_eligible_draft(
            content_md=self.CONTENT
        )
        finding_id, lock_before = self._seed_open_finding("queued")

        resp = self.client.post(
            f"/api/draft-room/drafts/{self.draft_id}/findings/{finding_id}/disposition",
            json={
                "action": "apply",
                "base_revision_id": self.revision_id,
                "lock_version": self._lock_version(self.draft_id),
            },
            headers=self._owner_headers(),
        )
        self.assertEqual(resp.status_code, 409, resp.text)
        # The allowlist check (draft_room.py:2501) raises invalid_draft_status.
        self.assertEqual(resp.json().get("code"), "invalid_draft_status")

        conn, _store = self._store()
        try:
            row = conn.execute(
                "SELECT status, lock_version FROM drafts WHERE id = ?",
                (self.draft_id,),
            ).fetchone()
            finding = conn.execute(
                "SELECT status FROM draft_findings WHERE id = ?", (finding_id,)
            ).fetchone()
        finally:
            self._release(conn)
        self.assertEqual(row[0], "queued", "the draft must stay queued")
        self.assertEqual(int(row[1]), int(lock_before), "no lock bump on refusal")
        self.assertEqual(finding[0], "open", "the finding must stay open")


if __name__ == "__main__":
    unittest.main()
