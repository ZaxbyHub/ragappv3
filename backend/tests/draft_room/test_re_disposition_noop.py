"""Arm's-length acceptance checks for the #901 disposition-integrity half of
the collated #901 + #920 + #927 trace (AC1-AC5).

Every test here asserts the DESIRED behavior: the file is RED at base
b369c02f for the right reason and GREEN after the fix. The probes that
demonstrate the current defects live in
``.agents/issue-traces/901-920-927-draft-room-integrity/repro/probe_phase2.py``;
this file is the post-fix mirror of those probes.

Covered acceptance criteria:

* AC1 — a second dismiss/waive of an already-dispositioned finding is
  refused (409), leaves exactly one audit event, and leaves
  ``drafts.lock_version`` unchanged after the first disposition.
* AC2 — the disposition response derives its currency input from
  ``store.get_current_revision`` (the draft's CURRENT revision), not the
  finding's own ``revision_id``.
* AC3 — GET ``/drafts/{id}/findings`` batches its eligibility reads instead
  of paying one ``_run_store`` thread hop per read.
* AC4 — a mid-transaction ``sqlite3.OperationalError`` (lock contention)
  must not surface as the client-conflict class mapped to HTTP 409.
* AC5 — dismiss and waive share ONE finish path owning status write + lock
  bump + event + commit.
"""

import ast
import inspect
import os
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.api.routes import draft_room  # noqa: E402
from app.services.draft_store import DraftStore  # noqa: E402
from tests.draft_room.test_draft_compile_routes import (  # noqa: E402
    CompileRouteTestBase,
)

CONTENT = "# Heading\n\nThis needs a fix here and stays fine elsewhere."


def _finding_status_write_sites(node):
    """Line numbers of every ``<conn>.execute("UPDATE draft_findings SET
    status ...")`` call site inside ``node`` (a module or one function)."""
    sites = []
    for sub in ast.walk(node):
        if (
            isinstance(sub, ast.Call)
            and isinstance(sub.func, ast.Attribute)
            and sub.func.attr == "execute"
            and sub.args
            and isinstance(sub.args[0], ast.Constant)
            and isinstance(sub.args[0].value, str)
            and sub.args[0].value.startswith("UPDATE draft_findings SET status")
        ):
            sites.append(sub.lineno)
    return sites


class TestReDispositionNoOp(CompileRouteTestBase):
    """Desired-behavior checks — RED at base, GREEN after the fix."""

    longMessage = False

    def setUp(self):
        super().setUp()
        self.draft_id, self.job_id, self.revision_id = (
            self._seed_ready_eligible_draft(content_md=CONTENT)
        )
        self.span_start = CONTENT.index("a fix")
        self.span_end = self.span_start + len("a fix")

    # -- harness (probe_phase2.py patterns) --------------------------------

    def _insert_finding(self, *, severity="warning"):
        conn, store = self._store()
        try:
            return store.insert_finding(
                draft_id=self.draft_id,
                stage="standards",
                rule_id="rule-x",
                rule_version="1",
                category="style",
                severity=severity,
                message="needs a fix",
                revision_id=self.revision_id,
                job_id=self.job_id,
                waivable=True,
                suggestion="a correction",
                span_start=self.span_start,
                span_end=self.span_end,
            )
        finally:
            self._release(conn)

    def _events(self, event_type, finding_id):
        conn, _store = self._store()
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM draft_events WHERE event_type = ? "
                "AND json_extract(event_json, '$.finding_id') = ?",
                (event_type, finding_id),
            ).fetchone()
            return int(row[0])
        finally:
            self._release(conn)

    def _lock_version_db(self):
        conn, _store = self._store()
        try:
            row = conn.execute(
                "SELECT lock_version FROM drafts WHERE id = ?", (self.draft_id,)
            ).fetchone()
            return int(row[0])
        finally:
            self._release(conn)

    def _dispose(self, finding_id, action, *, note=None, base_revision_id=None):
        lv = self._lock_version(self.draft_id)
        return self.client.post(
            f"/api/draft-room/drafts/{self.draft_id}/findings/"
            f"{finding_id}/disposition",
            json={
                "action": action,
                "base_revision_id": (
                    base_revision_id
                    if base_revision_id is not None
                    else self.revision_id
                ),
                "lock_version": lv,
                "note": note,
            },
            headers=self._owner_headers(),
        )

    # -- AC1: re-disposition is refused, not a churned no-op ----------------

    def test_double_dismiss_is_refused_with_one_event_and_no_lock_churn(self):
        finding_id = self._insert_finding()
        lock_before = self._lock_version_db()
        first = self._dispose(finding_id, "dismiss")
        assert first.status_code == 200, first.text
        lock_after_first = self._lock_version_db()
        assert lock_after_first == lock_before + 1, (
            "the first (real) dismiss keeps the +1 lock bump: "
            f"{lock_before} -> {lock_after_first}"
        )

        second = self._dispose(finding_id, "dismiss")
        events = self._events("finding_dismissed", finding_id)
        assert events == 1, (
            "one dismiss action = one audit row: a second dismiss of an "
            "already-dispositioned finding must be refused, never recorded; "
            f"found {events} finding_dismissed events"
        )
        assert second.status_code == 409, second.text
        assert self._lock_version_db() == lock_after_first, (
            "the refused second dismiss must leave drafts.lock_version "
            "unchanged after the first disposition"
        )

    def test_double_waive_is_refused_with_one_event_and_no_lock_churn(self):
        finding_id = self._insert_finding()
        lock_before = self._lock_version_db()
        first = self._dispose(finding_id, "waive", note="legit waiver note")
        assert first.status_code == 200, first.text
        lock_after_first = self._lock_version_db()
        assert lock_after_first == lock_before + 1, (
            "the first (real) waive keeps the +1 lock bump: "
            f"{lock_before} -> {lock_after_first}"
        )

        second = self._dispose(finding_id, "waive", note="second waiver note")
        events = self._events("finding_waived", finding_id)
        assert events == 1, (
            "one waive action = one audit row: a second waive of an "
            "already-dispositioned finding must be refused, never recorded; "
            f"found {events} finding_waived events"
        )
        assert second.status_code == 409, second.text
        assert self._lock_version_db() == lock_after_first, (
            "the refused second waive must leave drafts.lock_version "
            "unchanged after the first disposition"
        )

    # -- AC2: dispose response currency input -------------------------------

    def test_disposition_response_uses_the_drafts_current_revision(self):
        finding_id = self._insert_finding()
        # A second current revision: the finding stays anchored to revision 1
        # while the draft's CURRENT revision moves on, so "the finding's own
        # revision_id" and "the draft's current revision" now differ.
        newer_revision_id = self._seed_current_revision(
            self.draft_id,
            job_id=self.job_id,
            content_md="# Heading\n\nA later compile superseded the first.",
        )
        assert newer_revision_id != self.revision_id

        consulted = []
        real_get_current_revision = DraftStore.get_current_revision

        def spy(store_self, *args, **kwargs):
            result = real_get_current_revision(store_self, *args, **kwargs)
            consulted.append(None if result is None else int(result.id))
            return result

        lv = self._lock_version(self.draft_id)
        with patch.object(DraftStore, "get_current_revision", spy):
            resp = self.client.post(
                f"/api/draft-room/drafts/{self.draft_id}/findings/"
                f"{finding_id}/disposition",
                json={
                    "action": "dismiss",
                    "base_revision_id": newer_revision_id,
                    "lock_version": lv,
                    "note": "dismissing",
                },
                headers=self._owner_headers(),
            )
        assert resp.status_code == 200, resp.text
        assert newer_revision_id in consulted, (
            "the disposition response must derive its eligibility input from "
            "store.get_current_revision — the draft's CURRENT revision "
            f"({newer_revision_id}) — not from the finding's own revision_id "
            f"({self.revision_id}); get_current_revision was consulted with "
            f"{consulted} during the dispose request"
        )

    # -- AC3: findings-list read batching ------------------------------------

    def test_findings_list_batches_its_reads(self):
        self._insert_finding()
        hops = []
        real_run_store = draft_room._run_store

        async def counting_run_store(func, *args, **kwargs):
            hops.append(getattr(func, "__qualname__", repr(func)))
            return await real_run_store(func, *args, **kwargs)

        with patch.object(draft_room, "_run_store", counting_run_store):
            resp = self.client.get(
                f"/api/draft-room/drafts/{self.draft_id}/findings",
                headers=self._owner_headers(),
            )
        assert resp.status_code == 200, resp.text
        assert len(hops) <= 2, (
            "GET /drafts/{id}/findings must batch its eligibility reads "
            "(draft row, current revision, compile-liveness) instead of "
            "paying one _run_store thread hop per read and re-reading the "
            "drafts row inside each helper; one route call cost "
            f"{len(hops)} hops: {hops}"
        )

    # -- AC4: lock contention is not a client conflict ----------------------

    def test_lock_contention_is_not_reported_as_a_client_conflict(self):
        finding_id = self._insert_finding()

        def locked(_self, **_kwargs):
            raise sqlite3.OperationalError("database is locked")

        with patch.object(DraftStore, "_insert_event", locked):
            try:
                resp = self._dispose(finding_id, "dismiss")
            except sqlite3.OperationalError:
                # Raw propagation of the storage fault past the route is an
                # acceptable server-side surface — it is not the mapped
                # client-conflict class.
                return
        body = resp.json()
        assert resp.status_code != 409 or body.get("code") != "conflict", (
            "sqlite3.OperationalError raised inside the disposition "
            "transaction must surface as a retryable server-side failure, "
            "not as the client-conflict class (DraftConflictError "
            "code='conflict') the route maps to HTTP 409 — lock contention "
            "is not a reload conflict; surfaced "
            f"{resp.status_code} {body.get('code')!r}"
        )

    # -- AC5: one shared disposition finish path -----------------------------

    def test_one_shared_finish_path(self):
        store_path = Path(inspect.getsourcefile(DraftStore))
        module_tree = ast.parse(store_path.read_text(encoding="utf-8"))

        # The module owns exactly ONE inline draft_findings status write —
        # the shared finish path. Two copies (waive's hand-rolled UPDATE next
        # to _complete_finding_disposition's) is the drift the AC forbids.
        inline_sites = _finding_status_write_sites(module_tree)
        assert len(inline_sites) == 1, (
            "draft_store.py must carry exactly one inline "
            "'UPDATE draft_findings SET status' writer — the single shared "
            "disposition finish path owning status write + lock bump + "
            "event + commit; found "
            f"{len(inline_sites)} at lines {inline_sites}"
        )

        waive_fn = next(
            (
                node
                for node in ast.walk(module_tree)
                if isinstance(node, ast.FunctionDef)
                and node.name == "waive_finding"
            ),
            None,
        )
        assert waive_fn is not None, "draft_store.py has no waive_finding method"
        assert not _finding_status_write_sites(waive_fn), (
            "waive_finding must not hand-roll its own status write; it must "
            "route through _complete_finding_disposition"
        )
        routes_through_helper = any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_complete_finding_disposition"
            for node in ast.walk(waive_fn)
        )
        assert routes_through_helper, (
            "waive_finding must call _complete_finding_disposition so "
            "dismiss and waive share one finish path"
        )

        finding_id = self._insert_finding()
        observed = []
        real_complete = DraftStore._complete_finding_disposition

        def counting(store_self, **kwargs):
            observed.append(kwargs.get("target"))
            return real_complete(store_self, **kwargs)

        with patch.object(DraftStore, "_complete_finding_disposition", counting):
            resp = self._dispose(finding_id, "waive", note="one finish path")
        assert resp.status_code == 200, resp.text
        assert observed == ["waived"], (
            "the waive request must go through the shared "
            f"_complete_finding_disposition finish path at runtime; "
            f"observed {observed}"
        )


if __name__ == "__main__":
    unittest.main()
