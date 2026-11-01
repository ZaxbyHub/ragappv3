"""PR-level hardening tests for the issue #788 disposition/lifecycle fix.

Additive coverage beyond the frozen acceptance checks (plan §8): the frozen
C1-C12 prove the acceptance criteria; these pin the surrounding invariants
the plan critic flagged — exactly-once audit events for dismiss/waive, the
store-level stale-lock re-check, can_* agreement with the route on archived
drafts, the widened apply-from-failed/cancelled recovery, the
crash-recovery -> cancel settle chain, and the compile-only scope of the
can_apply liveness probe.
"""

import json
import os
import sys
import unittest
from contextlib import contextmanager

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.config import settings  # noqa: E402
from app.services.draft_store import (  # noqa: E402
    DraftConflictError,
    sha256_text,
)
from tests.draft_room.test_draft_compile_routes import (  # noqa: E402
    CompileRouteTestBase,
)
from tests.draft_room.test_draft_store import (  # noqa: E402
    OWNER_ID,
    DraftStoreTestBase,
)


@contextmanager
def _lease_disabled():
    """Claim with the shared-lease path off — it is on by default, so the
    fallback branch of ``claim_next_compile_job`` is otherwise unreachable."""
    previous = settings.draft_job_lease_enabled
    settings.draft_job_lease_enabled = False
    try:
        yield
    finally:
        settings.draft_job_lease_enabled = previous


class TestN04BRouteGuards(CompileRouteTestBase):
    """Route-level hardening: events, flags, and the widened apply statuses."""

    longMessage = False

    CONTENT = "# Heading\n\nThis needs a fix here and stays fine elsewhere."

    def setUp(self):
        super().setUp()
        self.draft_id, self.job_id, self.revision_id = self._seed_ready_eligible_draft(
            content_md=self.CONTENT
        )
        self.span_start = self.CONTENT.index("a fix")
        self.span_end = self.span_start + len("a fix")

    def _insert_finding(self, *, severity="warning"):
        conn, store = self._store()
        try:
            return store.insert_finding(
                draft_id=self.draft_id, stage="standards", rule_id="rule-x",
                rule_version="1", category="style", severity=severity,
                message="needs a fix", revision_id=self.revision_id,
                job_id=self.job_id, waivable=True, suggestion="a correction",
                span_start=self.span_start, span_end=self.span_end,
            )
        finally:
            self._release(conn)

    def _dispose(self, finding_id, action, *, note=None):
        lv = self._lock_version(self.draft_id)
        return self.client.post(
            f"/api/draft-room/drafts/{self.draft_id}/findings/{finding_id}/disposition",
            json={"action": action, "base_revision_id": self.revision_id,
                  "lock_version": lv, "note": note},
            headers=self._owner_headers(),
        )

    def _event_count(self, event_type):
        conn, _store = self._store()
        try:
            return conn.execute(
                "SELECT COUNT(*) FROM draft_events WHERE draft_id = ? "
                "AND event_type = ?",
                (self.draft_id, event_type),
            ).fetchone()[0]
        finally:
            self._release(conn)

    def _scalar(self, sql, *params):
        conn, _store = self._store()
        try:
            return conn.execute(sql, params).fetchone()[0]
        finally:
            self._release(conn)

    def _latest_event_row(self, event_type):
        """The newest ``draft_events`` row of a type as (revision_id, payload)."""
        conn, _store = self._store()
        try:
            row = conn.execute(
                "SELECT revision_id, event_json FROM draft_events "
                "WHERE draft_id = ? AND event_type = ? "
                "ORDER BY id DESC LIMIT 1",
                (self.draft_id, event_type),
            ).fetchone()
            if row is None:
                return None, None
            return row[0], json.loads(row[1])
        finally:
            self._release(conn)

    def conn_event_payload(self, event_type):
        _revision_id, payload = self._latest_event_row(event_type)
        return payload

    def conn_event_revision(self, event_type):
        revision_id, _payload = self._latest_event_row(event_type)
        return revision_id

    def test_dismiss_writes_exactly_one_finding_dismissed_event(self):
        """One human dismiss action, one finding_dismissed audit row."""
        finding_id = self._insert_finding(severity="info")
        resp = self._dispose(finding_id, "dismiss", note="acknowledged")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(self._event_count("finding_dismissed"), 1, resp.text)

    def test_waive_writes_exactly_one_finding_waived_event(self):
        """One human waive action, one finding_waived audit row."""
        finding_id = self._insert_finding(severity="blocker")
        resp = self._dispose(finding_id, "waive", note="acceptable risk")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(self._event_count("finding_waived"), 1, resp.text)

    def test_can_flags_agree_with_route_on_archived_draft(self):
        """After archiving, every disposition flag is False on the listing."""
        finding_id = self._insert_finding()
        archive = self.client.post(
            f"/api/draft-room/drafts/{self.draft_id}/archive",
            json={"lock_version": self._lock_version(self.draft_id)},
            headers=self._owner_headers(),
        )
        self.assertEqual(archive.status_code, 200, archive.text)

        items = self.client.get(
            f"/api/draft-room/drafts/{self.draft_id}/findings",
            headers=self._owner_headers(),
        ).json()["items"]
        row = next(item for item in items if item["id"] == finding_id)
        self.assertFalse(row["can_apply"], row)
        self.assertFalse(row["can_dismiss"], row)
        self.assertFalse(row["can_waive"], row)

        # The finding is still open (archiving does not disposition it) —
        # the flags changed because the route now refuses, not the finding.
        self.assertEqual(row["status"], "open", row)

    def test_apply_from_cancelled_and_failed_drafts_succeeds(self):
        """The widened _APPLY_FINDING_ALLOWED_PRIOR_STATUSES is deliberate:
        apply is one of the recovery paths out of failed/cancelled."""
        for terminal in ("cancelled", "failed"):
            with self.subTest(status=terminal):
                draft_id, _job, revision_id = self._seed_ready_eligible_draft(
                    content_md=self.CONTENT
                )
                conn, store = self._store()
                finding_id = store.insert_finding(
                    draft_id=draft_id, stage="standards", rule_id="rule-x",
                    rule_version="1", category="style", severity="warning",
                    message="needs a fix", revision_id=revision_id,
                    job_id=_job, waivable=True, suggestion="a correction",
                    span_start=self.CONTENT.index("a fix"),
                    span_end=self.CONTENT.index("a fix") + len("a fix"),
                )
                self._release(conn)
                self._set_draft_status(draft_id, terminal)
                lv = self._lock_version(draft_id)
                resp = self.client.post(
                    f"/api/draft-room/drafts/{draft_id}/findings/"
                    f"{finding_id}/disposition",
                    json={"action": "apply", "base_revision_id": revision_id,
                          "lock_version": lv, "note": "recovering"},
                    headers=self._owner_headers(),
                )
                self.assertEqual(resp.status_code, 201, f"{terminal}: {resp.text}")
                detail = self.client.get(
                    f"/api/draft-room/drafts/{draft_id}",
                    headers=self._owner_headers(),
                ).json()
                self.assertEqual(
                    detail["summary"]["status"], "needs_review", terminal
                )

    def test_dismiss_event_payload_carries_no_note_text(self):
        """The dismiss call site builds its audit payload separately from
        waive's, so the no-free-text contract needs its own assertion: the row
        exists (count pinned above), and its payload carries only IDs/codes
        plus the has_note flag."""
        finding_id = self._insert_finding(severity="info")
        resp = self._dispose(finding_id, "dismiss", note="n04-dismiss-note-4c1e")
        self.assertEqual(resp.status_code, 200, resp.text)
        payload = self.conn_event_payload("finding_dismissed")
        self.assertIsNotNone(payload)
        self.assertNotIn("n04-dismiss-note-4c1e", payload)
        self.assertEqual(payload.get("finding_id"), finding_id)
        self.assertIn("has_note", payload)

    def test_disposition_event_records_the_findings_revision(self):
        """Audit rows link to the revision the finding was raised against, the
        way the apply and ready_invalidated writers already do."""
        finding_id = self._insert_finding()
        resp = self._dispose(finding_id, "waive", note="recorded against a revision")
        self.assertEqual(resp.status_code, 200, resp.text)
        revision_id = self.conn_event_revision("finding_waived")
        self.assertEqual(revision_id, self.revision_id)

    def test_archived_draft_refusal_carries_the_invalid_state_code(self):
        """AC2 pins the 409 and the untouched finding; this pins the code the
        guard raises, so the archived refusal stays distinguishable from a
        stale lock or any other 409."""
        finding_id = self._insert_finding(severity="info")
        archive = self.client.post(
            f"/api/draft-room/drafts/{self.draft_id}/archive",
            json={"lock_version": self._lock_version(self.draft_id)},
            headers=self._owner_headers(),
        )
        self.assertEqual(archive.status_code, 200, archive.text)
        resp = self._dispose(finding_id, "dismiss", note="not needed")
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json()["code"], "invalid_state")

    def test_non_owner_cannot_dispose_a_finding(self):
        """The owner scoping moved into the store transaction, so the
        cross-user path must stay 404 there and at the route."""
        finding_id = self._insert_finding(severity="info")
        lv = self._lock_version(self.draft_id)
        resp = self.client.post(
            f"/api/draft-room/drafts/{self.draft_id}/findings/"
            f"{finding_id}/disposition",
            json={"action": "dismiss", "base_revision_id": self.revision_id,
                  "lock_version": lv, "note": "not mine"},
            headers=self._other_headers(),
        )
        self.assertEqual(resp.status_code, 404, resp.text)
        status = self._scalar(
            "SELECT status FROM draft_findings WHERE id = ?", finding_id
        )
        self.assertEqual(status, "open")

    def test_can_apply_true_while_parse_job_active(self):
        """can_apply's liveness probe is compile-only: an active input-parse
        job does not suppress Apply (the route accepts the apply)."""
        finding_id = self._insert_finding()
        conn, _store = self._store()
        try:
            vault_id = conn.execute(
                "SELECT vault_id FROM drafts WHERE id = ?", (self.draft_id,)
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO draft_jobs (draft_id, vault_id, created_by, job_type, "
                "status, max_model_calls, timeout_seconds, prompt_bundle_version) "
                "VALUES (?, ?, ?, 'parse_input', 'pending', 40, 1800, 'test')",
                (self.draft_id, vault_id, self.OWNER_ID),
            )
            conn.commit()
        finally:
            self._release(conn)

        items = self.client.get(
            f"/api/draft-room/drafts/{self.draft_id}/findings",
            headers=self._owner_headers(),
        ).json()["items"]
        row = next(item for item in items if item["id"] == finding_id)
        self.assertTrue(row["can_apply"], row)

        # And a compile job DOES suppress it (the probe is not vacuous).
        conn, _store = self._store()
        try:
            conn.execute(
                "UPDATE draft_jobs SET job_type = 'compile' "
                "WHERE draft_id = ? AND status = 'pending'",
                (self.draft_id,),
            )
            conn.commit()
        finally:
            self._release(conn)
        items = self.client.get(
            f"/api/draft-room/drafts/{self.draft_id}/findings",
            headers=self._owner_headers(),
        ).json()["items"]
        row = next(item for item in items if item["id"] == finding_id)
        self.assertFalse(row["can_apply"], row)


class TestN04BStoreGuards(DraftStoreTestBase):
    """Store-level hardening: the in-transaction lock re-check and the
    crash-recovery -> cancel settle chain."""

    longMessage = False

    def _draft_with_open_finding(self):
        draft = self._make_draft()
        content_md = "# Body"
        cur = self.conn.execute(
            "INSERT INTO draft_jobs (draft_id, vault_id, created_by, job_type, "
            "status, max_model_calls, timeout_seconds, prompt_bundle_version) "
            "VALUES (?, ?, ?, 'compile', 'completed', 40, 1800, 'test')",
            (draft.id, draft.vault_id, OWNER_ID),
        )
        job_id = int(cur.lastrowid)
        self.conn.commit()
        cur = self.conn.execute(
            "INSERT INTO draft_revisions (draft_id, job_id, revision_no, source, "
            "content_md, content_sha256, fact_status, is_current, created_by) "
            "VALUES (?, ?, 1, 'pipeline', ?, ?, 'passed', 1, ?)",
            (draft.id, job_id, content_md, sha256_text(content_md), OWNER_ID),
        )
        revision_id = int(cur.lastrowid)
        self.conn.commit()
        finding_id = self.store.insert_finding(
            draft_id=draft.id, stage="standards", rule_id="rule-x",
            rule_version="1", category="style", severity="warning",
            message="needs a fix", revision_id=revision_id, job_id=job_id,
            waivable=True, suggestion="a correction", span_start=2, span_end=6,
        )
        return draft, revision_id, finding_id

    def test_store_disposition_rejects_stale_lock_in_transaction(self):
        """A stale lock_version passed straight to the store method is
        refused by the in-transaction re-check — not by the route's
        pre-call snapshot (which this call bypasses)."""
        draft, _revision_id, finding_id = self._draft_with_open_finding()
        lv = self.conn.execute(
            "SELECT lock_version FROM drafts WHERE id = ?", (draft.id,)
        ).fetchone()[0]

        with self.assertRaises(DraftConflictError) as ctx:
            self.store.dismiss_finding(
                draft_id=draft.id,
                finding_id=finding_id,
                resolved_by=OWNER_ID,
                resolution_note="stale",
                lock_version=lv + 5,
            )
        self.assertEqual(ctx.exception.code, "stale_lock_version")

        row = self.conn.execute(
            "SELECT status FROM draft_findings WHERE id = ?", (finding_id,)
        ).fetchone()
        self.assertEqual(row[0], "open")

    def test_crash_recovery_then_cancel_leaves_draft_compilable(self):
        """claim -> worker crash reset -> cancel settles the draft off
        'running'; it is never stranded on an uncompilable status."""
        draft = self._make_draft()
        cur = self.conn.execute(
            "INSERT INTO draft_jobs (draft_id, vault_id, created_by, job_type, "
            "status, max_model_calls, timeout_seconds, prompt_bundle_version) "
            "VALUES (?, ?, ?, 'compile', 'pending', 40, 1800, 'test')",
            (draft.id, draft.vault_id, OWNER_ID),
        )
        job_id = int(cur.lastrowid)
        self.conn.execute(
            "UPDATE drafts SET status = 'queued' WHERE id = ?", (draft.id,)
        )
        self.conn.commit()

        claimed = self.store.claim_next_compile_job(worker_id="n04b-worker")
        self.assertIsNotNone(claimed)
        status = self.conn.execute(
            "SELECT status FROM drafts WHERE id = ?", (draft.id,)
        ).fetchone()[0]
        self.assertEqual(status, "running")

        # Worker crash: startup recovery resets the job to pending and must
        # also settle the draft back to 'queued' (companion (a)).
        reset = self.store.recover_orphaned_compile_jobs()
        self.assertEqual(reset, 1)
        status = self.conn.execute(
            "SELECT status FROM drafts WHERE id = ?", (draft.id,)
        ).fetchone()[0]
        self.assertEqual(status, "queued")

        # The user cancels the re-pending job: the settle must cover this
        # draft, leaving it in a compilable/archivable terminal state.
        self.store.request_job_cancel(
            draft_id=draft.id, owner_id=OWNER_ID, job_id=job_id
        )
        status = self.conn.execute(
            "SELECT status FROM drafts WHERE id = ?", (draft.id,)
        ).fetchone()[0]
        self.assertEqual(status, "cancelled")

    def test_claimed_compile_without_the_shared_lease_still_marks_running(self):
        """The non-lease claim branch is the default-off fallback
        (``draft_job_lease_enabled`` defaults True), so nothing reaches it
        unless the switch is cleared. It must apply the same draft-status
        write as the lease branch — that is what the PR body claims."""
        draft = self._make_draft()
        cur = self.conn.execute(
            "INSERT INTO draft_jobs (draft_id, vault_id, created_by, job_type, "
            "status, max_model_calls, timeout_seconds, prompt_bundle_version) "
            "VALUES (?, ?, ?, 'compile', 'pending', 40, 1800, 'test')",
            (draft.id, draft.vault_id, OWNER_ID),
        )
        self.conn.execute(
            "UPDATE drafts SET status = 'queued' WHERE id = ?", (draft.id,)
        )
        self.conn.commit()

        with _lease_disabled():
            claimed = self.store.claim_next_compile_job(worker_id="n04b-nolease")

        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.status, "running")
        status = self.conn.execute(
            "SELECT status FROM drafts WHERE id = ?", (draft.id,)
        ).fetchone()[0]
        self.assertEqual(status, "running")

    def test_archive_records_ready_invalidated(self):
        """The pointer clear is pinned by the AC8 check; the paired audit row
        is what makes the transition reviewable, so pin it too."""
        draft = self._ready_draft()
        lv = self.conn.execute(
            "SELECT lock_version FROM drafts WHERE id = ?", (draft.id,)
        ).fetchone()[0]
        self.store.archive_draft(draft_id=draft.id, owner_id=OWNER_ID, lock_version=lv)
        rows = self.conn.execute(
            "SELECT event_json FROM draft_events WHERE draft_id = ? "
            "AND event_type = 'ready_invalidated'",
            (draft.id,),
        ).fetchall()
        self.assertEqual(len(rows), 1, rows)
        self.assertIn("archived", rows[0][0])

    def test_restore_clears_a_pointer_left_by_a_pre_fix_archive(self):
        """Defensive half of the Ready-pointer invariant: a row archived
        before the fix still carries ready_revision_id, and restoring must
        clear it and audit the invalidation."""
        draft = self._ready_draft()
        stale_revision = int(
            self.conn.execute(
                "INSERT INTO draft_revisions (draft_id, revision_no, source, "
                "content_md, content_sha256, fact_status, is_current, created_by) "
                "VALUES (?, 2, 'manual', '# Other', ?, 'not_run', 0, ?)",
                (draft.id, sha256_text("# Other"), OWNER_ID),
            ).lastrowid
        )
        self.conn.execute(
            "UPDATE drafts SET status = 'archived', archived_at = CURRENT_TIMESTAMP, "
            "ready_revision_id = ? WHERE id = ?",
            (stale_revision, draft.id),
        )
        self.conn.commit()

        lv = self.conn.execute(
            "SELECT lock_version FROM drafts WHERE id = ?", (draft.id,)
        ).fetchone()[0]
        self.store.restore_draft(draft_id=draft.id, owner_id=OWNER_ID, lock_version=lv)

        pointer = self.conn.execute(
            "SELECT ready_revision_id FROM drafts WHERE id = ?", (draft.id,)
        ).fetchone()[0]
        self.assertIsNone(pointer)
        events = self.conn.execute(
            "SELECT event_json FROM draft_events WHERE draft_id = ? "
            "AND event_type = 'ready_invalidated'",
            (draft.id,),
        ).fetchall()
        self.assertEqual(len(events), 1, events)
        self.assertIn("restored", events[0][0])

    def _ready_draft(self):
        """A draft whose Ready pointer is set (status 'ready')."""
        draft = self._make_draft()
        cur = self.conn.execute(
            "INSERT INTO draft_revisions (draft_id, revision_no, source, "
            "content_md, content_sha256, fact_status, is_current, created_by) "
            "VALUES (?, 1, 'pipeline', '# Body', ?, 'passed', 1, ?)",
            (draft.id, sha256_text("# Body"), OWNER_ID),
        )
        revision_id = int(cur.lastrowid)
        self.conn.execute(
            "UPDATE drafts SET status = 'ready', ready_revision_id = ?, ready_by = ?, "
            "ready_at = CURRENT_TIMESTAMP WHERE id = ?",
            (revision_id, OWNER_ID, draft.id),
        )
        self.conn.commit()
        return draft


if __name__ == "__main__":
    unittest.main()
