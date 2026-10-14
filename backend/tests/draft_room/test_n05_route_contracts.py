"""Arm's-length acceptance checks for issue #789 (Workstream N, PR 5 of 6).

Frozen specification for the idempotency/rollback/capabilities/contract-drift
fixes. Every DISCRIMINATING check below is RED at base, GREEN after the fix;
the explicitly marked guardrail tests are GREEN at base and must stay green.

Coverage map (issue #789 acceptance criteria):

* AC1 -- ``TestN05Idempotency``: same key + different *requested* start
  stage on a fresh compile must answer 409, not replay the first job.
* AC2 -- ``TestN05Idempotency``: a genuinely identical retry replay must
  return the original retry job even after that job completed and produced a
  new current revision.
* AC3 -- ``TestN05UploadCompensation``: the upload enqueue domain-error
  branch must still roll the input row back when the compensating unlink
  itself fails, and must keep the original client error.
* AC4 -- ``TestN05Capabilities``: ``compile_available`` must reflect real
  worker liveness (``app.state.draft_job_processor``), not a literal.
* AC5 -- ``TestN05PromotionLogging``: a generic promotion 500 must leave an
  ERROR-level log record carrying the exception.
* AC6 -- ``TestN05ErrorCodes``: the stale-``lock_version`` condition must
  emit one consistent error code across the disposition and compile routes,
  and the two enqueue ``IntegrityError`` races must stay distinguishable.

Harness: subclasses the shared Draft Room route bases
(``CompileRouteTestBase`` for compile-side routes, ``DraftPromoteTestBase``
for the promotion route). The bases own settings isolation, the dependency
overrides (including the token dependency), the pooled SQLite fixture, and
auth headers; nothing here rebuilds that machinery.
"""

import os
import pathlib
import sqlite3
import sys
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

try:
    import lancedb  # noqa: F401
except ImportError:
    import types

    sys.modules["lancedb"] = types.ModuleType("lancedb")

from fastapi.testclient import TestClient

from app.api.deps import get_db
from app.main import app
from app.services.draft_store import DraftConflictError, DraftStore
from tests.draft_room.test_draft_compile_routes import CompileRouteTestBase
from tests.draft_room.test_draft_promote import DraftPromoteTestBase


class TestN05Idempotency(CompileRouteTestBase):
    """AC1/AC2 (DISCRIMINATING) plus the AC9 replay guardrail (GREEN)."""

    def _retry(self, draft_id, job_id, *, idempotency_key=None):
        headers = dict(self._owner_headers())
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        return self.client.post(
            f"/api/draft-room/drafts/{draft_id}/jobs/{job_id}/retry",
            headers=headers,
        )

    def test_same_key_different_requested_start_stage_is_409(self):
        """AC1. The idempotency fingerprint must fold in the client's
        *requested* start stage, so reusing one key for two fresh compiles
        that request different stages is a conflict. RED at base (the second
        request replays the first job and answers 202), GREEN after the fix
        (409)."""
        draft_id = self._draft_with_ready_input()

        first = self._compile(draft_id, idempotency_key="n05-ac1", start_stage="fact")
        self.assertEqual(first.status_code, 202, first.text)

        second = self._compile(
            draft_id, idempotency_key="n05-ac1", start_stage="outline"
        )
        self.assertEqual(second.status_code, 409, second.text)

    def test_retry_replay_after_child_revision_returns_the_original_job(self):
        """AC2. A retry replay is *identical* by construction, so it must
        return the original retry job at any point of that job's lifecycle --
        including after the retried job succeeded and produced a new current
        revision. RED at base (the replay 409s ``idempotency_key_conflict``
        because the fingerprint hashed the then-current revision), GREEN
        after the fix (202 with the original job id)."""
        draft_id = self._draft_with_ready_input()
        parent_job_id = self._compile(draft_id).json()["id"]

        # Drive the parent compile job to a terminal failure so retry is
        # legal (draft_jobs' CHECK constraint has no 'succeeded' value, so a
        # finished job is 'completed').
        conn = self._connection_pool.get_connection()
        try:
            conn.execute(
                "UPDATE draft_jobs SET status = 'failed', "
                "error_code = 'model_unavailable' WHERE id = ?",
                (parent_job_id,),
            )
            conn.execute(
                "UPDATE drafts SET status = 'failed' WHERE id = ?", (draft_id,)
            )
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)

        key = "n05-ac2"
        first_retry = self._retry(draft_id, parent_job_id, idempotency_key=key)
        self.assertEqual(first_retry.status_code, 202, first_retry.text)
        child_job_id = first_retry.json()["id"]

        # The retried job succeeds AND produces a new current revision, as a
        # real run would: settle the child job, insert a current revision,
        # and bump the draft's lock/status the way a real commit does.
        self._seed_current_revision(
            draft_id,
            job_id=child_job_id,
            content_md="# After retry\n\nThe retried compile produced this.",
        )
        conn = self._connection_pool.get_connection()
        try:
            conn.execute(
                "UPDATE draft_jobs SET status = 'completed', "
                "active_stage = 'assemble' WHERE id = ?",
                (child_job_id,),
            )
            conn.execute(
                "UPDATE drafts SET status = 'needs_review', "
                "lock_version = lock_version + 1 WHERE id = ?",
                (draft_id,),
            )
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)

        replay = self._retry(draft_id, parent_job_id, idempotency_key=key)
        self.assertEqual(replay.status_code, 202, replay.text)
        self.assertEqual(replay.json()["id"], child_job_id)

    def test_same_key_same_requested_stage_replay_returns_job_no_second_row(self):
        """AC9 guardrail (GREEN at base, must stay green): a genuinely
        identical compile replay returns the existing job and creates no
        second row."""
        draft_id = self._draft_with_ready_input()
        first = self._compile(draft_id, idempotency_key="n05-ac9", start_stage="fact")
        self.assertEqual(first.status_code, 202, first.text)
        job_id = first.json()["id"]

        second = self._compile(
            draft_id, idempotency_key="n05-ac9", start_stage="fact"
        )
        self.assertEqual(second.status_code, 202, second.text)
        self.assertEqual(second.json()["id"], job_id)

        conn = self._connection_pool.get_connection()
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM draft_jobs WHERE draft_id = ? "
                "AND job_type = 'compile'",
                (draft_id,),
            ).fetchone()[0]
            self.assertEqual(count, 1)
        finally:
            self._connection_pool.release_connection(conn)


class TestN05UploadCompensation(CompileRouteTestBase):
    """AC3 (DISCRIMINATING)."""

    def test_domain_error_branch_rolls_back_the_row_even_if_unlink_fails(self):
        """AC3. When ``enqueue_parse_job`` raises a translated domain error,
        the compensation must run BOTH the staged-bytes unlink and the
        input-row rollback, and a failure inside the unlink must never
        replace the original client error. RED at base (the domain-error
        branch runs an unguarded unlink, so the OSError escapes untranslated
        as a 500 and the row survives), GREEN after the fix (409 and zero
        input rows)."""
        draft_id = self._create_draft(title="N05 Compensation").json()["id"]
        # Established pattern (test_issue516_routes_acceptance.py): a
        # no-reraise client so an untranslated exception surfaces as a 500
        # response instead of blowing up the test call.
        client = TestClient(app, raise_server_exceptions=False)
        client.headers["user-agent"] = ""

        real_unlink = pathlib.Path.unlink

        def _unlink_fails_for_final_storage(path_self, *args, **kwargs):
            text = str(path_self)
            if "draft-room" in text and ".incoming" not in text and ".trash" not in text:
                raise OSError(f"simulated sharing violation on {text}")
            return real_unlink(path_self, *args, **kwargs)

        with patch.object(
            DraftStore,
            "enqueue_parse_job",
            side_effect=DraftConflictError("a parse job is already active"),
        ), patch.object(pathlib.Path, "unlink", _unlink_fails_for_final_storage):
            resp = client.post(
                f"/api/draft-room/drafts/{draft_id}/inputs",
                data={"role": "manuscript", "authority": "primary"},
                files={
                    "file": (
                        "notes.txt",
                        b"Bytes whose parse enqueue will fail.",
                        "text/plain",
                    )
                },
                headers=self._owner_headers(),
            )
        self.assertEqual(resp.status_code, 409, resp.text)

        detail = self.client.get(
            f"/api/draft-room/drafts/{draft_id}", headers=self._owner_headers()
        )
        self.assertEqual(detail.status_code, 200, detail.text)
        self.assertEqual(
            len(detail.json()["inputs"]),
            0,
            "a compensated upload must leave no input row behind, even when "
            "the unlink step of the compensation failed",
        )


class TestN05Capabilities(CompileRouteTestBase):
    """AC4 (DISCRIMINATING) plus a liveness guardrail (GREEN)."""

    def _capabilities(self):
        resp = self.client.get(
            "/api/draft-room/capabilities", headers=self._owner_headers()
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    def _set_processor(self, value):
        """Install ``app.state.draft_job_processor`` and return a callable
        restoring the prior state (the attribute is absent unless a test or
        lifespan set it)."""
        had = hasattr(app.state, "draft_job_processor")
        previous = getattr(app.state, "draft_job_processor", None)
        app.state.draft_job_processor = value

        def _restore():
            if had:
                app.state.draft_job_processor = previous
            else:
                del app.state.draft_job_processor

        return _restore

    def test_compile_unavailable_without_a_job_processor(self):
        """AC4. When startup could not bring up the compile worker
        (``app.state.draft_job_processor`` is None), capabilities must stop
        advertising compile as available. RED at base (the literal True),
        GREEN after the fix."""
        restore = self._set_processor(None)
        try:
            body = self._capabilities()
        finally:
            restore()
        self.assertIs(body["compile_available"], False)

    def test_compile_available_with_a_live_processor_stub(self):
        """Guardrail (GREEN at base, must stay green): with a live processor
        object on app state, compile is available."""
        restore = self._set_processor(object())
        try:
            body = self._capabilities()
        finally:
            restore()
        self.assertIs(body["compile_available"], True)


class TestN05PromotionLogging(DraftPromoteTestBase):
    """AC5 (DISCRIMINATING)."""

    def test_generic_promotion_failure_is_logged(self):
        """AC5. An unexpected promotion failure with a *clean* compensation
        (no cleanup-incomplete sub-case) must still answer 500
        ``internal_error`` and leave at least one ERROR-level record carrying
        the exception -- a 500 with no log line is undebuggable in
        production. RED at base (no ERROR record is ever emitted for this
        path, so assertLogs itself fails), GREEN after the fix."""
        draft_id = self._create_draft().json()["id"]
        upload = self._upload_input(draft_id, content=b"promote then fail")
        input_id = upload.json()["input"]["id"]
        self._mark_input_ready(input_id)

        self._mock_background_processor.enqueue = AsyncMock(
            side_effect=RuntimeError("queue unavailable")
        )
        with self.assertLogs("app.api.routes.draft_room", level="ERROR") as captured:
            resp = self._promote(draft_id, source_type="input", source_id=input_id)

        self.assertEqual(resp.status_code, 500, resp.text)
        self.assertEqual(resp.json()["code"], "internal_error", resp.text)
        self.assertTrue(
            any(
                record.exc_info or "queue unavailable" in record.getMessage()
                for record in captured.records
            ),
            "the ERROR record must carry the exception (exc_info or its text)",
        )


class TestN05ErrorCodes(CompileRouteTestBase):
    """AC6 (DISCRIMINATING) plus the two IntegrityError-race guardrails."""

    CONTENT = "# Heading\n\nThis needs a fix here and stays fine elsewhere."

    def setUp(self):
        super().setUp()
        self.draft_id, self.job_id, self.revision_id = self._seed_ready_eligible_draft(
            content_md=self.CONTENT
        )
        conn, store = self._store()
        try:
            self.finding_id = store.insert_finding(
                draft_id=self.draft_id,
                stage="standards",
                rule_id="n05-rule",
                rule_version="1",
                category="style",
                severity="warning",
                message="needs a fix",
                revision_id=self.revision_id,
                job_id=self.job_id,
                waivable=True,
            )
        finally:
            self._release(conn)

    def test_stale_lock_version_code_is_consistent(self):
        """AC6. The identical stale-``lock_version`` precondition must emit
        one consistent error code on the disposition route and the compile
        route (one condition, one code). Direction-agnostic by design: both
        routes just have to agree. RED at base (``stale_lock_version`` on
        disposition vs ``conflict`` on compile), GREEN after the fix."""
        stale = self._lock_version(self.draft_id) + 5

        disposition = self.client.post(
            f"/api/draft-room/drafts/{self.draft_id}/findings/"
            f"{self.finding_id}/disposition",
            json={
                "action": "dismiss",
                "base_revision_id": self.revision_id,
                "lock_version": stale,
                "note": "code-consistency probe",
            },
            headers=self._owner_headers(),
        )
        compile_resp = self._compile(self.draft_id, lock_version=stale)

        self.assertEqual(disposition.status_code, 409, disposition.text)
        self.assertEqual(compile_resp.status_code, 409, compile_resp.text)
        self.assertEqual(
            disposition.json()["code"],
            compile_resp.json()["code"],
            "the same stale-lock precondition must not emit two different "
            f"codes: disposition={disposition.json()['code']!r} "
            f"compile={compile_resp.json()['code']!r}",
        )

    def test_integrityerror_race_codes_are_distinguishable(self):
        """AC6 companion (DISCRIMINATING). The enqueue INSERT can lose one
        of two unique-index races: the one-active-compile race and the
        idempotency-key race. They must map to different codes. Simulated
        here: a *committed* job row already holds the request's
        ``Idempotency-Key`` for this user, and the enqueue's key lookup is
        made to miss it once (the exact lost-lookup window), so the request's
        own INSERT loses the real unique index. RED at base (both races map
        to ``active_job``), GREEN after the fix
        (``idempotency_key_conflict``)."""
        draft_id = self._draft_with_ready_input()
        key = "n05-race-key"

        conn = self._connection_pool.get_connection()
        try:
            vault_id = conn.execute(
                "SELECT vault_id FROM drafts WHERE id = ?", (draft_id,)
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO draft_jobs (draft_id, vault_id, created_by, job_type, "
                "status, active_stage, max_model_calls, timeout_seconds, "
                "idempotency_key) "
                "VALUES (?, ?, ?, 'compile', 'completed', 'assemble', 40, 1800, ?)",
                (draft_id, vault_id, self.OWNER_ID, key),
            )
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)

        lv = self._lock_version(draft_id)
        original_override = app.dependency_overrides.get(get_db)

        def _racing_get_db():
            racing_conn = sqlite3.connect(
                self._db_path,
                check_same_thread=False,
                timeout=10,
                factory=_IdempotencyRaceConnection,
            )
            racing_conn.row_factory = sqlite3.Row
            racing_conn.execute("PRAGMA foreign_keys = ON;")
            try:
                yield racing_conn
            finally:
                racing_conn.close()

        app.dependency_overrides[get_db] = _racing_get_db
        _IdempotencyRaceConnection.hide_idempotency_lookups = True
        try:
            resp = self.client.post(
                f"/api/draft-room/drafts/{draft_id}/compile",
                json={
                    "base_revision_id": None,
                    "lock_version": lv,
                    "start_stage": "research",
                },
                headers={**self._owner_headers(), "Idempotency-Key": key},
            )
        finally:
            _IdempotencyRaceConnection.hide_idempotency_lookups = False
            if original_override is None:
                app.dependency_overrides.pop(get_db, None)
            else:
                app.dependency_overrides[get_db] = original_override

        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(
            resp.json()["code"],
            "idempotency_key_conflict",
            "losing the (created_by, idempotency_key) unique-index race must "
            "surface as the idempotency conflict, not as active_job",
        )

    def test_active_compile_race_still_active_job(self):
        """Guardrail (GREEN at base, must stay green): a draft that already
        has an active compile job keeps its own one-active-compile code for
        a fresh-key enqueue."""
        draft_id = self._draft_with_ready_input()
        conn = self._connection_pool.get_connection()
        try:
            vault_id = conn.execute(
                "SELECT vault_id FROM drafts WHERE id = ?", (draft_id,)
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO draft_jobs (draft_id, vault_id, created_by, job_type, "
                "status, max_model_calls, timeout_seconds) "
                "VALUES (?, ?, ?, 'compile', 'pending', 40, 1800)",
                (draft_id, vault_id, self.OWNER_ID),
            )
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)

        resp = self._compile(draft_id, idempotency_key="n05-fresh-key")
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json()["code"], "active_job", resp.text)


class _IdempotencyRaceConnection(sqlite3.Connection):
    """Connection factory for the IntegrityError race simulation.

    While ``hide_idempotency_lookups`` is armed, the enqueue's
    ``(created_by, idempotency_key)`` lookup SELECT is answered as a miss
    even though a committed row holds the key -- exactly the window in which
    a concurrent winner commits between a request's lookup and its INSERT.
    The request's own INSERT then fails on the real unique index (no fake
    exception), so any disambiguation strategy (re-query the key on any
    connection, before or after rollback, or read the real index name from
    the IntegrityError) observes the race truthfully.
    """

    hide_idempotency_lookups = False

    def execute(self, sql, params=()):  # type: ignore[override]
        if (
            type(self).hide_idempotency_lookups
            and sql.lstrip().upper().startswith("SELECT")
            and "idempotency_key = ?" in sql
        ):
            return super().execute(
                "SELECT id, draft_id, input_json FROM draft_jobs WHERE 1 = 0", ()
            )
        return super().execute(sql, params)


if __name__ == "__main__":
    unittest.main()
