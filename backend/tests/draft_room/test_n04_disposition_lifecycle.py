"""Frozen acceptance checks for the issue #788 Draft Room audit (N04).

Issue #788 remediation: the finding-disposition path (apply/dismiss/waive)
drifted from its reference implementation, and several Draft lifecycle edges
never matched their documented contracts. These checks are frozen acceptance
tests for the written contract -- they encode the required behavior exactly
and are RED at base (master d788d838), GREEN after the fix:

* **AC1** — one ``finding_applied`` audit event per apply. Apply already
  writes the event inside its ``BEGIN IMMEDIATE`` transaction; the route's
  post-commit ``record_event`` writes a second one (RED at base: duplicate
  write, GREEN after the fix).
* **AC2** — dismiss on an archived draft is refused with 409 and leaves the
  finding open (RED at base: the store method never re-reads the draft).
* **AC3** — waive bumps ``drafts.lock_version`` exactly once (RED at base:
  waive never re-checks/bumps the lock).
* **AC4** — waive's status write and its audit event are atomic: an aborted
  event insert rolls the waiver back and the route refuses (RED at base: the
  status commits before the separate event write blows up).
* **AC5** — the disposition event payload never carries the user's free-text
  note (RED at base: the note is stored as ``reason``).
* **AC6** — a cancelled draft with a current revision is editable again: the
  manual-revision route accepts it (RED at base: ``_DRAFT_TRANSITIONS`` has
  no failed/cancelled -> needs_review edge).
* **AC9** — the Ready claim gate matches ``set_claim_resolution``'s contract
  (its docstring says resolution is "an audit annotation, not a gate").
* **AC10** — a finding raised against a superseded revision reports
  ``can_apply=false`` (RED at base: ``_to_finding`` ignores whether the
  finding's revision is the draft's current one).

AC7/AC8 (store-level) live in ``test_n04_store_lifecycle.py``.
"""

import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.services.draft_store import DraftStore  # noqa: E402
from tests.draft_room.test_draft_compile_routes import (  # noqa: E402
    CompileRouteTestBase,
)


class TestN04Disposition(CompileRouteTestBase):
    """AC1-AC5, AC10: the finding-disposition surface of issue #788."""

    longMessage = False

    CONTENT = "# Heading\n\nThis needs a fix here and stays fine elsewhere."

    def setUp(self):
        super().setUp()
        self.draft_id, self.job_id, self.revision_id = self._seed_ready_eligible_draft(
            content_md=self.CONTENT
        )
        # A span that exactly covers "a fix", with a suggestion.
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

    def _scalar(self, sql, *params):
        conn, _store = self._store()
        try:
            return conn.execute(sql, params).fetchone()[0]
        finally:
            self._release(conn)

    def test_apply_writes_exactly_one_finding_applied_event(self):
        """AC1: one finding_applied row per apply -- RED at base (duplicate
        write), GREEN after the fix."""
        finding_id = self._insert_finding()
        resp = self._dispose(finding_id, "apply", note="fixing per style desk")
        self.assertEqual(resp.status_code, 201, resp.text)

        count = self._scalar(
            "SELECT COUNT(*) FROM draft_events WHERE draft_id = ? "
            "AND event_type = 'finding_applied'",
            self.draft_id,
        )
        self.assertEqual(count, 1, f"assert {count} == 1")

    def test_dismiss_on_archived_draft_is_409(self):
        """AC2: dismiss on an archived draft is refused -- RED at base (the
        store method never re-reads the draft's status), GREEN after the fix."""
        finding_id = self._insert_finding(severity="info")
        archive = self.client.post(
            f"/api/draft-room/drafts/{self.draft_id}/archive",
            json={"lock_version": self._lock_version(self.draft_id)},
            headers=self._owner_headers(),
        )
        self.assertEqual(archive.status_code, 200, archive.text)

        resp = self._dispose(finding_id, "dismiss", note="not needed")
        self.assertEqual(resp.status_code, 409, f"assert {resp.status_code} == 409")

        status = self._scalar(
            "SELECT status FROM draft_findings WHERE id = ?", finding_id
        )
        self.assertEqual(status, "open", f"assert {status!r} == 'open'")

    def test_waive_bumps_lock_version(self):
        """AC3: waive bumps the draft's lock_version exactly once -- RED at
        base (waive never re-checks/bumps the lock), GREEN after the fix."""
        finding_id = self._insert_finding(severity="blocker")
        before = self._scalar(
            "SELECT lock_version FROM drafts WHERE id = ?", self.draft_id
        )
        resp = self._dispose(finding_id, "waive", note="waiving for the record")
        self.assertEqual(resp.status_code, 200, resp.text)
        after = self._scalar(
            "SELECT lock_version FROM drafts WHERE id = ?", self.draft_id
        )

        delta = after - before
        self.assertEqual(delta, 1, f"assert {delta} == 1")

    def test_waive_status_and_event_are_atomic(self):
        """AC4: an aborted audit-event insert rolls the waiver back -- RED at
        base (the status commits before the separate event write), GREEN after
        the fix."""
        finding_id = self._insert_finding(severity="blocker")
        conn, _store = self._store()
        conn.execute(
            "CREATE TRIGGER n04_block_events BEFORE INSERT ON draft_events "
            "BEGIN SELECT RAISE(ABORT, 'n04'); END"
        )
        conn.commit()
        self._release(conn)
        try:
            try:
                resp = self._dispose(finding_id, "waive", note="waiving anyway")
                status = resp.status_code
            except sqlite3.Error:
                # Base behavior: the waiver itself was already committed when
                # the separate event write blew up, so the disposition was
                # accepted, not refused -- an accepted-then-crashed waive is
                # recorded here as the 200 it effectively was.
                status = 200
            row = self._scalar(
                "SELECT status FROM draft_findings WHERE id = ?", finding_id
            )
            self.assertEqual(row, "open", f"assert {row!r} == 'open'")
            self.assertEqual(status, 409, f"assert {status} == 409")
        finally:
            conn, _store = self._store()
            conn.execute("DROP TRIGGER IF EXISTS n04_block_events")
            conn.commit()
            self._release(conn)

    def test_disposition_event_payload_has_no_note_text(self):
        """AC5: the disposition event never carries the user's note -- RED at
        base (the note is stored as ``reason``), GREEN after the fix."""
        finding_id = self._insert_finding(severity="blocker")
        resp = self._dispose(finding_id, "waive", note="n04-note-7f3a")
        self.assertEqual(resp.status_code, 200, resp.text)

        leaked_count = self._scalar(
            "SELECT COUNT(*) FROM draft_events WHERE draft_id = ? "
            "AND event_json LIKE '%n04-note-7f3a%'",
            self.draft_id,
        )
        leaked = leaked_count > 0
        self.assertIs(leaked, False, f"assert {leaked} is False")

    def test_superseded_revision_finding_is_not_applicable(self):
        """AC10: a finding on a superseded revision reports can_apply=false --
        RED at base (_to_finding ignores revision currency), GREEN after the
        fix."""
        finding_id = self._insert_finding()
        edit = self.client.post(
            f"/api/draft-room/drafts/{self.draft_id}/revisions",
            json={"base_revision_id": self.revision_id,
                  "lock_version": self._lock_version(self.draft_id),
                  "content_md": "# Heading\n\nManually rewritten body entirely."},
            headers=self._owner_headers(),
        )
        self.assertEqual(edit.status_code, 201, edit.text)

        items = self.client.get(
            f"/api/draft-room/drafts/{self.draft_id}/findings",
            headers=self._owner_headers(),
        ).json()["items"]
        row = next(item for item in items if item["id"] == finding_id)
        can_apply = row["can_apply"]
        self.assertIs(can_apply, False, f"assert {can_apply} is False")


class TestN04Lifecycle(CompileRouteTestBase):
    """AC6, AC9: the draft-lifecycle edges of issue #788."""

    longMessage = False

    def test_cancelled_draft_with_revision_is_editable_again(self):
        """AC6: a cancelled draft with a current revision accepts a manual
        edit -- RED at base (no cancelled -> needs_review edge), GREEN after
        the fix."""
        draft_id, _seeded_job_id, revision_id = self._seed_ready_eligible_draft()
        compile_resp = self._compile(draft_id, base_revision_id=revision_id)
        self.assertEqual(compile_resp.status_code, 202, compile_resp.text)
        job_id = compile_resp.json()["id"]

        cancel = self.client.post(
            f"/api/draft-room/drafts/{draft_id}/jobs/{job_id}/cancel",
            headers=self._owner_headers(),
        )
        self.assertEqual(cancel.status_code, 200, cancel.text)

        conn, _store = self._store()
        try:
            status = conn.execute(
                "SELECT status FROM drafts WHERE id = ?", (draft_id,)
            ).fetchone()[0]
        finally:
            self._release(conn)
        self.assertEqual(status, "cancelled", f"assert {status!r} == 'cancelled'")

        edit = self.client.post(
            f"/api/draft-room/drafts/{draft_id}/revisions",
            json={"base_revision_id": revision_id,
                  "lock_version": self._lock_version(draft_id),
                  "content_md": "# Heading\n\nEdited after the cancelled compile."},
            headers=self._owner_headers(),
        )
        self.assertEqual(edit.status_code, 201, f"assert {edit.status_code} == 201")

    def test_claim_resolution_gate_matches_its_contract(self):
        """AC9: the Ready claim gate agrees with set_claim_resolution's
        docstring ("an audit annotation, not a gate") -- RED at base (the
        gate filters on resolution), GREEN after the fix."""
        draft_id, _job_id, revision_id = self._seed_ready_eligible_draft()
        conn, store = self._store()
        try:
            claim_id = store.insert_claim(
                revision_id=revision_id, ordinal=1,
                claim_text="the review window is thirty days",
                span_start=None, span_end=None,
                claim_type="factual", status="contradicted", severity="blocker",
            )
            store.set_claim_resolution(
                claim_id=claim_id, target="accepted", resolved_by=self.OWNER_ID
            )
        finally:
            self._release(conn)

        doc_says_not_a_gate = "not a gate" in DraftStore.set_claim_resolution.__doc__
        ready = self._mark_ready(draft_id, revision_id)
        still_blocked = ready.status_code == 409
        self.assertEqual(
            still_blocked,
            doc_says_not_a_gate,
            f"assert {still_blocked} == {doc_says_not_a_gate}",
        )


if __name__ == "__main__":
    unittest.main()
