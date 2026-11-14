"""Disposition/store contract companions for the #901/#920/#927 bundle.

Closes the swarm-pr-review pr1133-20261114 feedback items that strengthen
coverage without touching the frozen check files:

* TF-06 — AC3's no-redundant-read clause, pinned at the STORE level: a
  ``sqlite3.set_trace_callback`` over ``findings_page_after_authorization``
  asserts the method never reads ``drafts`` and touches only the three child
  tables; its ``DraftValidationError`` guards get direct unit tests (they are
  unreachable over HTTP because the route pre-validates).
* TF-08 — the re-disposition refusal is pinned to its CODE
  (``finding_not_open``), and the cross-status negatives (waive after dismiss,
  dismiss after waive) are covered.
* TF-07 — SPEC 10.3's scoping clause is pinned structurally (all three
  co-owner module names + the guard pointer + the "Centralize ..." sentence
  ordered AFTER the scoping), not just by keyword presence.
"""

from __future__ import annotations

import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.services.draft_store import DraftValidationError  # noqa: E402
from tests.draft_room.test_draft_compile_routes import (  # noqa: E402
    CompileRouteTestBase,
)
from tests.draft_room.test_draft_store import (  # noqa: E402
    DraftStoreTestBase,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SPEC = _REPO_ROOT / "specs" / "draft-room" / "SPEC.md"

CO_OWNER_MODULES = (
    "api/routes/draft_room.py",
    "services/draft_job_processor.py",
    "services/draft_pipeline.py",
)


class TestFindingsPageStoreContract(DraftStoreTestBase):
    """TF-06: the batched reader's table discipline, pinned at the store."""

    longMessage = False

    def setUp(self):
        super().setUp()
        self.draft_id = self._make_draft().id

    def test_never_reads_the_drafts_row(self):
        statements: list[str] = []
        self.conn.set_trace_callback(
            lambda sql: statements.append(" ".join(sql.split()))
        )
        try:
            self.store.findings_page_after_authorization(
                draft_id=self.draft_id, limit=50, offset=0
            )
        finally:
            self.conn.set_trace_callback(None)
        drafts_reads = [
            s
            for s in statements
            if re.search(r"\b(FROM|JOIN)\s+drafts\b", s, re.I)
        ]
        self.assertEqual(
            drafts_reads,
            [],
            f"the batched reader must never read drafts (FROM or JOIN); saw {drafts_reads}",
        )
        touched = {
            m.group(2).lower()
            for s in statements
            for m in re.finditer(r"\b(FROM|JOIN)\s+([a-z_]+)", s, re.I)
        }
        self.assertTrue(
            touched <= {"draft_revisions", "draft_jobs", "draft_findings"},
            f"only the three child tables may be read; saw {sorted(touched)}",
        )
        self.assertTrue(
            {"draft_revisions", "draft_jobs", "draft_findings"} <= touched,
            f"the eligibility inputs and the page must all be read; saw {sorted(touched)}",
        )

    def test_unknown_status_is_rejected(self):
        with self.assertRaises(DraftValidationError):
            self.store.findings_page_after_authorization(
                draft_id=self.draft_id, status="bogus", limit=50, offset=0
            )

    def test_unknown_severity_is_rejected(self):
        with self.assertRaises(DraftValidationError):
            self.store.findings_page_after_authorization(
                draft_id=self.draft_id, severity="bogus", limit=50, offset=0
            )


class TestReDispositionCodeAndCrossStatus(CompileRouteTestBase):
    """TF-08: the refusal carries its code, for every status pair."""

    longMessage = False
    CONTENT = "# Heading\n\nThis needs a fix here and stays fine elsewhere."

    def setUp(self):
        super().setUp()
        self.draft_id, self.job_id, self.revision_id = self._seed_ready_eligible_draft(
            content_md=self.CONTENT
        )
        self.span_start = self.CONTENT.index("a fix")
        self.span_end = self.span_start + len("a fix")

    def _insert_finding(self):
        conn, store = self._store()
        try:
            return store.insert_finding(
                draft_id=self.draft_id, stage="standards", rule_id="rule-x",
                rule_version="1", category="style", severity="warning",
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

    def _assert_refused_finding_not_open(self, resp):
        self.assertEqual(resp.status_code, 409, resp.text)
        body = resp.json()
        self.assertEqual(
            body.get("code"),
            "finding_not_open",
            f"the refusal must carry its documented code; got {body}",
        )

    def test_second_dismiss_carries_the_code(self):
        fid = self._insert_finding()
        self.assertEqual(self._dispose(fid, "dismiss").status_code, 200)
        self._assert_refused_finding_not_open(self._dispose(fid, "dismiss"))

    def test_second_waive_carries_the_code(self):
        fid = self._insert_finding()
        self.assertEqual(
            self._dispose(fid, "waive", note="legit waiver note").status_code, 200
        )
        self._assert_refused_finding_not_open(
            self._dispose(fid, "waive", note="second waiver note")
        )

    def test_waive_after_dismiss_refused(self):
        fid = self._insert_finding()
        self.assertEqual(self._dispose(fid, "dismiss").status_code, 200)
        self._assert_refused_finding_not_open(
            self._dispose(fid, "waive", note="cross-status attempt")
        )

    def test_dismiss_after_waive_refused(self):
        fid = self._insert_finding()
        self.assertEqual(
            self._dispose(fid, "waive", note="legit waiver note").status_code, 200
        )
        self._assert_refused_finding_not_open(self._dispose(fid, "dismiss"))


class TestSpec103ScopingPinned(unittest.TestCase):
    """TF-07: the 10.3 scoping clause pinned structurally, not by keyword."""

    longMessage = False

    def test_scoping_clause_is_structural(self):
        text = _SPEC.read_text(encoding="utf-8-sig")
        section = text[text.index("### 10.3 State transitions"):]
        section = section[: section.index("```text")] if "```text" in section else section
        lowered = " ".join(section.split()).lower()
        for module in CO_OWNER_MODULES:
            self.assertIn(
                module,
                lowered,
                f"SPEC 10.3 must name the declared co-owner {module}",
            )
        self.assertIn(
            "test_n06b_status_transition_map.py",
            lowered,
            "SPEC 10.3 must point at the enforcing guard",
        )
        self.assertIn("allowlist", lowered)
        self.assertIn("co-owner", lowered)
        centralize = lowered.index("centralize the following transitions")
        scoping = lowered.index("declared co-owners")
        self.assertLess(
            scoping,
            centralize,
            "the 'Centralize ... reject all others' sentence must come AFTER "
            "the co-owner scoping clause so it reads as scoped",
        )


if __name__ == "__main__":
    unittest.main()
