"""Arm's-length acceptance checks for issue #1135 residuals (trace
1135-926-916-draft-room-residuals): findings-list pagination overflow,
re-archive idempotence, and the crash-strand compile sweep.

Every check here is frozen BEFORE any fix exists and replayed against both
ends, so each docstring phrases behavior true at BOTH ends of the loop:

* AC2 -- ``GET /api/draft-room/drafts/{id}/findings`` with ``page`` past the
  SQLite signed-64-bit bind range must be refused 422 by Query validation,
  never an unhandled 500. RED at base (the oversized offset escapes the
  route as an unhandled bind error), GREEN after the fix bounds ``page``.
* AC3 -- archiving an already-archived draft must be an idempotent refusal:
  no second ``draft_archived`` event, no ``lock_version`` bump. RED at base
  (the self-transition early-return lets the unconditional UPDATE and event
  insert through), GREEN after the fix refuses the second archive.
* AC4 -- a draft left on ``queued``/``running`` whose only compile job row
  is already terminal ``failed`` (the two-transaction settle crash window)
  must be settled by the stranded-draft sweep. RED at base (no sweep
  exists, so nothing settles), GREEN after the fix ships the sweep.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from tests.draft_room.test_draft_compile_routes import (  # noqa: E402
    CompileRouteTestBase,
)
from tests.draft_room.test_draft_store import (  # noqa: E402
    OWNER_ID,
    DraftStoreTestBase,
    InvalidTransitionError,
)


class TestFindingsPageBeyondSqliteBind(CompileRouteTestBase):
    """AC2: ``page`` beyond the SQLite 64-bit bind range must answer 422.

    RED at base (the route declares ``page: int = Query(1, ge=1)`` with no
    upper bound, so ``offset = (page - 1) * per_page`` overflows the SQLite
    signed-64-bit bind and escapes as an unhandled server error), GREEN
    after the fix refuses the page with a 422 validation error."""

    longMessage = False

    def test_findings_page_beyond_the_sqlite_bind_range_is_refused_422(self):
        draft_id, _job_id, _revision_id = self._seed_ready_eligible_draft()
        try:
            resp = self.client.get(
                f"/api/draft-room/drafts/{draft_id}/findings?page={10**18}",
                headers=self._owner_headers(),
            )
        except Exception as exc:  # base: the bind overflow escapes TestClient
            self.fail(
                "sqlite_bind_overflow: findings-list page beyond the SQLite "
                "64-bit bind range must be a 422 validation refusal, never "
                f"an unhandled server exception ({type(exc).__name__}: {exc})"
            )
        self.assertEqual(
            resp.status_code,
            422,
            "sqlite_bind_overflow: page=10**18 must be refused with a 422 "
            f"validation error, got {resp.status_code}: {resp.text}",
        )

    def test_page_bound_boundary_at_max_per_page(self):
        """Plan-critic round 2: pin the bound's exact edges at per_page=100.

        The shipped bound is the maximum page value for which
        (page - 1) * per_page cannot overflow the SQLite 64-bit bind for
        EVERY per_page <= 100: MAX_PAGE = ((2**63 - 1) // 100) + 1.
        RED at base (the route has no upper bound: MAX_PAGE+1 is accepted by
        validation and only dies later at the bind — or the draft 404s after
        the oversized offset; either way not the required 422), GREEN after
        the fix."""
        draft_id, _job_id, _revision_id = self._seed_ready_eligible_draft()
        max_page = ((2**63 - 1) // 100) + 1
        # MAX_PAGE itself is valid: an authenticated request must answer a
        # real status (200 empty page), never a validation refusal.
        resp_ok = self.client.get(
            f"/api/draft-room/drafts/{draft_id}/findings"
            f"?page={max_page}&per_page=100",
            headers=self._owner_headers(),
        )
        self.assertEqual(
            resp_ok.status_code,
            200,
            "page_boundary: the maximum universally safe page at per_page=100 "
            f"must remain a valid (empty) 200 response, got "
            f"{resp_ok.status_code}: {resp_ok.text}",
        )
        # MAX_PAGE + 1 must be refused by Query validation.
        resp_over = self.client.get(
            f"/api/draft-room/drafts/{draft_id}/findings"
            f"?page={max_page + 1}&per_page=100",
            headers=self._owner_headers(),
        )
        self.assertEqual(
            resp_over.status_code,
            422,
            "page_boundary: page=MAX_PAGE+1 at per_page=100 must be refused "
            f"422, got {resp_over.status_code}: {resp_over.text}",
        )

    def test_every_paginated_list_route_bounds_page(self):
        """Plan-critic round 2: ALL SEVEN unbounded `page` Query params in
        draft_room.py carry the `le=` bound (findings + evidence/claims/
        revisions siblings). RED at base (none carry le=), GREEN after the
        fix applies the bound to every route."""
        import ast
        from pathlib import Path

        source = Path(
            __file__).resolve().parents[2] / "app" / "api" / "routes" / "draft_room.py"
        tree = ast.parse(source.read_text(encoding="utf-8-sig"))
        unbounded = []
        bounded = 0
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            # `page: int = Query(...)` puts the Query call in the function's
            # DEFAULTS, not the annotation — pair args with defaults positionally.
            args = node.args.args[-len(node.args.defaults):] if node.args.defaults else []
            for arg, default in zip(args, node.args.defaults):
                if arg.arg != "page":
                    continue
                for call in ast.walk(default):
                    if (
                        isinstance(call, ast.Call)
                        and isinstance(call.func, ast.Name)
                        and call.func.id == "Query"
                    ):
                        kw_names = [kw.arg for kw in call.keywords]
                        if "le" in kw_names:
                            bounded += 1
                        else:
                            unbounded.append(f"{node.name}:page")
        self.assertEqual(
            unbounded,
            [],
            "page_bound_coverage: every `page: int = Query(...)` parameter "
            f"in draft_room.py must carry an le= bound; unbounded: "
            f"{unbounded} (bounded: {bounded})",
        )
        self.assertGreaterEqual(
            bounded,
            7,
            "page_bound_coverage: draft_room.py carried SEVEN unbounded page "
            f"params at base; the fix must bound all of them (bounded: "
            f"{bounded})",
        )


class TestReArchiveIsAnIdempotentRefusal(DraftStoreTestBase):
    """AC3: re-archiving an archived draft refuses, double-event-free.

    RED at base (the second ``archive_draft`` succeeds: the transition
    guard's ``target == current`` early-return bypasses the map and the
    unconditional UPDATE bumps ``lock_version`` and inserts a second
    ``draft_archived`` event), GREEN after the fix raises the transition
    error and leaves the event count and lock version untouched."""

    longMessage = False

    def _archived_event_count(self, draft_id) -> int:
        return int(
            self.conn.execute(
                "SELECT COUNT(*) FROM draft_events "
                "WHERE draft_id = ? AND event_type = 'draft_archived'",
                (draft_id,),
            ).fetchone()[0]
        )

    def test_second_archive_is_refused_with_no_event_and_no_lock_bump(self):
        draft = self._make_draft()
        self.store.archive_draft(
            draft_id=draft.id, owner_id=OWNER_ID, lock_version=draft.lock_version
        )
        archived = self.store.get_draft(draft.id, OWNER_ID)
        self.assertEqual(archived.status, "archived")
        events_after_first = self._archived_event_count(draft.id)
        lock_after_first = archived.lock_version

        with self.assertRaises(InvalidTransitionError):
            self.store.archive_draft(
                draft_id=draft.id,
                owner_id=OWNER_ID,
                lock_version=archived.lock_version,
            )

        after = self.store.get_draft(draft.id, OWNER_ID)
        self.assertEqual(
            self._archived_event_count(draft.id),
            events_after_first,
            "re_archive_double_event: archiving an already-archived draft "
            "must not write a second draft_archived event",
        )
        self.assertEqual(
            after.lock_version,
            lock_after_first,
            "re_archive_lock_bump: archiving an already-archived draft must "
            "not bump lock_version",
        )


class TestReArchiveRouteRefusesOverHttp(CompileRouteTestBase):
    """Plan-critic round 2: the re-archive refusal is pinned at the HTTP
    layer too — the second POST /archive answers 409 with the stable
    transition code and leaves the draft's event count and lock_version
    unchanged. RED at base (the second archive succeeds over HTTP: 200 +
    a second draft_archived event + lock_version bump), GREEN after the
    fix."""

    longMessage = False

    def test_second_archive_over_http_is_a_409_with_no_state_change(self):
        draft_id = self._create_draft().json()["id"]
        first = self.client.post(
            f"/api/draft-room/drafts/{draft_id}/archive",
            json={"lock_version": 1},
            headers=self._owner_headers(),
        )
        self.assertEqual(first.status_code, 200, first.text)
        lock_after_first = first.json()["lock_version"]
        events_after_first = self._store_event_count(draft_id)

        second = self.client.post(
            f"/api/draft-room/drafts/{draft_id}/archive",
            json={"lock_version": lock_after_first},
            headers=self._owner_headers(),
        )
        self.assertEqual(
            second.status_code,
            409,
            f"re_archive_http: archiving an already-archived draft must "
            f"answer 409, got {second.status_code}: {second.text}",
        )
        self.assertEqual(
            second.json().get("code"),
            "invalid_transition",
            f"re_archive_http: the refusal must carry the stable transition "
            f"code; got {second.text}",
        )
        self.assertEqual(
            self._store_event_count(draft_id),
            events_after_first,
            "re_archive_http: no second draft_archived event over the route",
        )
        detail = self.client.get(
            f"/api/draft-room/drafts/{draft_id}",
            headers=self._owner_headers(),
        ).json()
        self.assertEqual(
            (detail.get("summary") or {}).get("lock_version"),
            lock_after_first,
            "re_archive_http: the draft summary's lock_version must not "
            "bump over the route",
        )

    def _store_event_count(self, draft_id) -> int:
        conn = self._connection_pool.get_connection()
        try:
            return int(
                conn.execute(
                    "SELECT COUNT(*) FROM draft_events "
                    "WHERE draft_id = ? AND event_type = 'draft_archived'",
                    (draft_id,),
                ).fetchone()[0]
            )
        finally:
            self._connection_pool.release_connection(conn)


class TestCrashStrandSweep(DraftStoreTestBase):
    """AC4: the crash-window strand is settled by the production startup sweep.

    The settle path writes the job's terminal status in one transaction and
    the draft's failure in a second; a crash between them strands the draft
    on ``queued``/``running`` with no active job. RED at base (no
    stranded-compile-draft sweep exists, so ``_recover_on_startup`` leaves the
    seeded strand silently queued/running), GREEN after the fix ships the
    sweep wired into the real startup recovery entry point. Per the plan
    critic's round-1 shape: the check drives ``_recover_on_startup`` (the
    production entry), asserts the exact repaired status ``failed``, guards
    against settling drafts with an active job or with only non-failed
    terminal jobs, and proves second-sweep idempotence."""

    longMessage = False

    def _seed_job(self, draft, status: str) -> None:
        vault_id = self.conn.execute(
            "SELECT vault_id FROM drafts WHERE id = ?", (draft.id,)
        ).fetchone()[0]
        self.conn.execute(
            "INSERT INTO draft_jobs (draft_id, vault_id, created_by, job_type, "
            "status, active_stage, max_model_calls, timeout_seconds, "
            "prompt_bundle_version) "
            "VALUES (?, ?, ?, 'compile', ?, 'assemble', 40, 1800, 'test')",
            (draft.id, vault_id, OWNER_ID, status),
        )
        self.conn.commit()

    def _seed_stranded(self, draft, status: str) -> None:
        """The exact post-crash state: job settled terminal 'failed' in the
        first transaction, draft never settled by the second."""
        self._seed_job(draft, "failed")
        self.conn.execute(
            "UPDATE drafts SET status = ? WHERE id = ?", (status, draft.id)
        )
        self.conn.commit()

    def _draft_status(self, draft_id) -> str:
        return self.conn.execute(
            "SELECT status FROM drafts WHERE id = ?", (draft_id,)
        ).fetchone()[0]

    def _run_startup_recovery(self) -> None:
        """Drive the PRODUCTION startup recovery entry point over this test's
        database (pattern: test_draft_job_processor.py's
        ``asyncio.to_thread(self.processor._recover_on_startup)`` legs). The
        processor needs the real pool's ``connection()`` context manager, so
        build one over the same temp DB file and close it after."""
        import asyncio
        from pathlib import Path

        from app.models.database import SQLiteConnectionPool
        from app.services.draft_input_storage import DraftInputStorage
        from app.services.draft_job_processor import DraftJobProcessor
        from tests.draft_room.test_draft_job_processor import FakeExtractionService

        pool = SQLiteConnectionPool(self._db_path)
        try:
            processor = DraftJobProcessor(
                pool=pool,
                storage=DraftInputStorage(Path(self._temp_dir) / "draft-room"),
                extraction=FakeExtractionService(),
                poll_interval=0.05,
            )
            asyncio.run(asyncio.to_thread(processor._recover_on_startup))
        finally:
            pool.close_all()

    def test_startup_recovery_settles_crash_stranded_drafts_exactly(self):
        queued = self._make_draft(title="Queued strand")
        running = self._make_draft(title="Running strand")
        self._seed_stranded(queued, "queued")
        self._seed_stranded(running, "running")
        self.assertEqual(self._draft_status(queued.id), "queued")
        self.assertEqual(self._draft_status(running.id), "running")

        self._run_startup_recovery()

        for draft_id, seeded in ((queued.id, "queued"), (running.id, "running")):
            status = self._draft_status(draft_id)
            self.assertEqual(
                status,
                "failed",
                f"crash_strand_sweep: the draft seeded {seeded} whose only "
                f"compile job is terminal 'failed' must settle to exactly "
                f"'failed' via _recover_on_startup, got '{status}' "
                "(#1135 AC4)",
            )

    def test_sweep_guards_active_and_non_failed_terminal_histories(self):
        # Plan-critic round 2: the active-job guard must be DISCRIMINATING —
        # seed an OLDER terminal failed job plus a NEWER pending job (a
        # crash after a retry's re-enqueue). "Any historical failure + no
        # active job" would settle these; the shipped sweep must not, because
        # the newest compile job is live work.
        active_mixed = self._make_draft(title="Failed then pending")
        self._seed_job(active_mixed, "failed")
        self._seed_job(active_mixed, "pending")
        # Older-failed / newer-cancelled: the latest terminal outcome owns
        # the draft (the cancel path) — never settled to failed.
        cancelled_mixed = self._make_draft(title="Failed then cancelled")
        self._seed_job(cancelled_mixed, "failed")
        self._seed_job(cancelled_mixed, "cancelled")
        # Older-failed / newer-completed: the compile SUCCEEDED after the
        # failure — not a failure strand.
        completed_mixed = self._make_draft(title="Failed then completed")
        self._seed_job(completed_mixed, "failed")
        self._seed_job(completed_mixed, "completed")
        # Completed-only and cancelled-only single-job histories (round-1
        # guard shapes, kept).
        completed_only = self._make_draft(title="Completed history")
        self._seed_job(completed_only, "completed")
        cancelled_only = self._make_draft(title="Cancelled history")
        self._seed_job(cancelled_only, "cancelled")
        for draft in (
            active_mixed,
            cancelled_mixed,
            completed_mixed,
            completed_only,
            cancelled_only,
        ):
            self.conn.execute(
                "UPDATE drafts SET status = 'queued' WHERE id = ?", (draft.id,)
            )
        self.conn.commit()

        self._run_startup_recovery()

        for draft_id, expected, label in (
            (active_mixed.id, "queued", "older failed + newer PENDING compile"),
            (cancelled_mixed.id, "queued", "older failed + newer CANCELLED compile"),
            (completed_mixed.id, "queued", "older failed + newer COMPLETED compile"),
            (completed_only.id, "queued", "completed-only compile history"),
            (cancelled_only.id, "queued", "cancelled-only compile history"),
        ):
            self.assertEqual(
                self._draft_status(draft_id),
                expected,
                f"crash_strand_sweep: a draft with {label} history must NOT "
                "be settled to failed by the stranded-draft sweep (#1135 "
                "AC4; selection is latest-compile-job-aware)",
            )

    def test_second_sweep_is_an_idempotent_noop(self):
        stranded = self._make_draft(title="Idempotence strand")
        self._seed_stranded(stranded, "queued")
        self._run_startup_recovery()
        self.assertEqual(self._draft_status(stranded.id), "failed")
        events_after_first = int(
            self.conn.execute(
                "SELECT COUNT(*) FROM draft_events WHERE draft_id = ?",
                (stranded.id,),
            ).fetchone()[0]
        )
        lock_after_first = int(
            self.conn.execute(
                "SELECT lock_version FROM drafts WHERE id = ?", (stranded.id,)
            ).fetchone()[0]
        )

        self._run_startup_recovery()

        self.assertEqual(
            self._draft_status(stranded.id),
            "failed",
            "crash_strand_sweep: a second sweep must leave the settled draft "
            "failed (#1135 AC4)",
        )
        events_after_second = int(
            self.conn.execute(
                "SELECT COUNT(*) FROM draft_events WHERE draft_id = ?",
                (stranded.id,),
            ).fetchone()[0]
        )
        self.assertEqual(
            events_after_second,
            events_after_first,
            "crash_strand_sweep: a second sweep must not write additional "
            "draft_events rows (#1135 AC4)",
        )
        lock_after_second = int(
            self.conn.execute(
                "SELECT lock_version FROM drafts WHERE id = ?", (stranded.id,)
            ).fetchone()[0]
        )
        self.assertEqual(
            lock_after_second,
            lock_after_first,
            "crash_strand_sweep: a second sweep must not bump lock_version "
            "(#1135 AC4)",
        )


if __name__ == "__main__":
    unittest.main()
