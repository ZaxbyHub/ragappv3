"""Issue #532 review-fix regressions (PRR-001/007/013/022/024).

One class per review finding, each pinning the exact fixed behavior:

* PRR-001 — a claim-retrieval ``asyncio.TimeoutError`` raised while
  wall-clock budget remains must settle ``retrieval_unavailable`` (and record
  a failed ``fact`` stage row) instead of escaping as a raw exception that
  ``run_compile`` downgrades to ``internal_error`` with no stage record.
* PRR-007 — startup recovery of a crashed ``running`` compile job must
  preserve the job's original ``started_at`` across recovery AND re-claim, so
  ``_build_context`` derives the resumed wall-clock deadline from the
  original claim; a fresh job still gets its claim time, and the persisted
  model-call budget still resumes (AC8 semantics).
* PRR-013 — ``update_input_metadata`` must reject an archived draft exactly
  like ``update_draft`` does.
* PRR-022 — when the in-transaction cancel recheck in
  ``DraftJobProcessor._commit_success`` settles job+input cancelled, the same
  durable ``job_cancelled`` ``draft_events`` row the API cancel path writes
  must exist for the job/draft.
* PRR-024 — with the RAG engine unwired and a compile job pending,
  ``_claim_next_job`` returns None and warns exactly once per processor
  lifetime (never once per poll).

Harness: reuses ``test_issue516_jobs_acceptance.py``'s bases and doubles
(temp SQLite DB, ``CompilePipelineTestBase`` for pipeline-level checks,
``ParseJobTestBase`` for real ``DraftJobProcessor`` runs). No network, no
lancedb, no unstructured.
"""

import asyncio
import json
import os
import sys
import types
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

try:  # pragma: no cover - CI installs no lancedb; stub it like the other suites
    import lancedb  # noqa: F401
except ImportError:  # pragma: no cover
    sys.modules["lancedb"] = types.ModuleType("lancedb")

from app.services.document_extraction import ExtractedDocument
from app.services.draft_pipeline import (
    CODE_JOB_TIMEOUT,
    CODE_MODEL_CALL_BUDGET_EXCEEDED,
    CODE_RETRIEVAL_UNAVAILABLE,
    CompileFailure,
    PipelineDeps,
    run_compile,
)
from app.services.draft_prompts import PROMPT_BUNDLE_VERSION
from app.services.draft_store import DraftStore, InvalidTransitionError
from tests.draft_room.test_issue516_jobs_acceptance import (
    OWNER_ID,
    CompilePipelineTestBase,
    FakeClock,
    FakeModel,
    FakeRetriever,
    ParseJobTestBase,
    _fact_json,
    happy_responses,
)

# ═══════════════════════════════════════════════════════════════════════════
# PRR-001 (draft_pipeline._claim_retrieval_audit)
# ═══════════════════════════════════════════════════════════════════════════

#: The unsupported claim ``_fact_json()`` reports; the claim-proposition query
#: ``_claim_retrieval_audit`` issues is this exact normalized text.
CLAIM_PROPOSITION = "The review window is 30 days"


class ClaimQueryTimeoutRetriever(FakeRetriever):
    """Facet queries succeed; the claim-proposition query times out.

    Pins the regression to the claim-specific retrieval: the research-stage
    facet query (the manuscript text) still succeeds, so the only
    ``asyncio.TimeoutError`` the run can observe is the one raised inside
    ``_claim_retrieval_audit`` while plenty of wall-clock budget remains.
    """

    def __init__(self) -> None:
        super().__init__()
        self.timed_out_queries: list[str] = []

    async def __call__(self, query, vault_id, *, limit, source_kinds=None):
        normalized = " ".join(query.split())
        if normalized == CLAIM_PROPOSITION:
            self.timed_out_queries.append(normalized)
            raise asyncio.TimeoutError
        return await super().__call__(
            query, vault_id, limit=limit, source_kinds=source_kinds
        )


class TestPRR001ClaimRetrievalTimeoutClassification(CompilePipelineTestBase):
    async def test_retriever_timeout_with_budget_left_settles_retrieval_unavailable(
        self,
    ):
        job_id = self._make_compile_job(
            max_model_calls=40, timeout_seconds=1800
        )
        retriever = ClaimQueryTimeoutRetriever()
        # The fact desk reports one unsupported claim: that is what routes the
        # run into _claim_retrieval_audit (SPEC section 12.3's claim-specific
        # retrieval for unsupported claims).
        model = FakeModel(happy_responses(fact=[_fact_json(status="unsupported")]))
        # Held clock: the run's wall-clock deadline is start + 1800s, so the
        # timeout below fires with budget remaining (the branch that used to
        # re-raise the raw asyncio.TimeoutError).
        clock = FakeClock(datetime(2026, 8, 1, tzinfo=timezone.utc))
        deps = PipelineDeps(
            retrieve_sources=retriever,
            complete=model,
            now=clock,
        )

        with self.assertRaises(CompileFailure) as caught:
            await run_compile(job_id=job_id, pool=self.pool, deps=deps)

        # REQUIRED: the failure came from the claim retrieval specifically.
        self.assertEqual(retriever.timed_out_queries, [CLAIM_PROPOSITION])
        # REQUIRED: stable pre-PR classification, non-retryable.
        self.assertEqual(caught.exception.code, CODE_RETRIEVAL_UNAVAILABLE)
        self.assertFalse(caught.exception.retryable)
        # REQUIRED: the job settled failed with that code (a raw
        # asyncio.TimeoutError would have settled internal_error).
        row = self._job_row(job_id)
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["error_code"], CODE_RETRIEVAL_UNAVAILABLE)
        self.assertEqual(self._draft_status(), "failed")
        # REQUIRED: because it is a CompileFailure, _run_stage recorded the
        # failed stage row a raw exception would have skipped.
        with self.pool.connection() as conn:
            stage_rows = conn.execute(
                "SELECT stage, status, error_code FROM draft_job_stages "
                "WHERE job_id = ?",
                (job_id,),
            ).fetchall()
        failed_fact = [
            r for r in stage_rows if r["stage"] == "fact" and r["status"] == "failed"
        ]
        self.assertTrue(
            failed_fact,
            f"no failed 'fact' stage row was recorded (rows={tuple(map(dict, stage_rows))})",
        )
        self.assertEqual(failed_fact[0]["error_code"], CODE_RETRIEVAL_UNAVAILABLE)


# ═══════════════════════════════════════════════════════════════════════════
# PRR-007 (draft_store.recover_orphaned_compile_jobs / claim_next_compile_job)
# ═══════════════════════════════════════════════════════════════════════════


class _CallRecordingModel:
    """Model double that records every call; must never be invoked here."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def __call__(self, prompt, *, logical_mode, temperature, sensitive):
        self.calls.append(prompt)
        return "{}"


class TestPRR007RecoveredCompileJobWallClockResume(CompilePipelineTestBase):
    def _started_at(self, job_id) -> str:
        with self.pool.connection() as conn:
            return conn.execute(
                "SELECT started_at FROM draft_jobs WHERE id = ?", (job_id,)
            ).fetchone()[0]

    async def test_recovered_job_keeps_started_at_and_times_out_from_it(self):
        # A worker crashed 2h after claiming a compile job that had already
        # consumed 2 model calls.
        job_id = self._make_compile_job(
            status="running",
            max_model_calls=40,
            timeout_seconds=1800,
            model_call_count=2,
        )
        with self.pool.connection() as conn:
            conn.execute(
                "UPDATE draft_jobs SET started_at = datetime('now', '-2 hours') "
                "WHERE id = ?",
                (job_id,),
            )
            conn.commit()
        original_started_at = self._started_at(job_id)

        # Startup recovery returns the job to pending WITHOUT wiping its
        # original claim time.
        self.assertEqual(self.store.recover_orphaned_compile_jobs(), 1)
        self.assertEqual(
            self._started_at(job_id),
            original_started_at,
            "recovery nulled the original claim time",
        )

        # Re-claim keeps the original start (started_at + timeout already
        # passed ~90 minutes ago).
        claimed = self.store.claim_next_compile_job()
        self.assertIsNotNone(claimed)
        self.assertEqual(
            claimed.started_at,
            original_started_at,
            "claim re-stamped started_at on a recovered job",
        )

        # REQUIRED: _build_context derives the deadline from the original
        # start, so the resumed run settles job_timeout immediately and makes
        # zero additional model calls (a fresh full budget would instead run
        # research and call the model).
        model = _CallRecordingModel()
        deps = PipelineDeps(
            retrieve_sources=FakeRetriever(),
            complete=model,
            now=lambda: datetime.now(timezone.utc),
        )
        with self.assertRaises(CompileFailure) as caught:
            await run_compile(job_id=job_id, pool=self.pool, deps=deps)

        self.assertEqual(caught.exception.code, CODE_JOB_TIMEOUT)
        self.assertEqual(model.calls, [])
        row = self._job_row(job_id)
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["error_code"], CODE_JOB_TIMEOUT)
        self.assertEqual(row["model_call_count"], 2)
        self.assertEqual(self._draft_status(), "failed")

    async def test_recovered_job_at_model_call_cap_resumes_budget(self):
        # AC8 semantics through the full recovery path: a job recovered at
        # its persisted model-call cap makes zero model calls and settles
        # budget exhaustion, and its started_at survives recovery + claim.
        job_id = self._make_compile_job(
            status="running",
            max_model_calls=3,
            timeout_seconds=1800,
            model_call_count=3,
        )
        with self.pool.connection() as conn:
            conn.execute(
                "UPDATE draft_jobs SET started_at = CURRENT_TIMESTAMP "
                "WHERE id = ?",
                (job_id,),
            )
            conn.commit()
        original_started_at = self._started_at(job_id)

        self.assertEqual(self.store.recover_orphaned_compile_jobs(), 1)
        claimed = self.store.claim_next_compile_job()
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.started_at, original_started_at)

        with self.assertRaises(CompileFailure) as caught:
            await run_compile(job_id=job_id, pool=self.pool, deps=self._deps())

        self.assertEqual(caught.exception.code, CODE_MODEL_CALL_BUDGET_EXCEEDED)
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(self.model.calls, [])
        row = self._job_row(job_id)
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["error_code"], CODE_MODEL_CALL_BUDGET_EXCEEDED)
        self.assertEqual(row["model_call_count"], 3)

    async def test_fresh_compile_job_still_gets_its_claim_time(self):
        # The COALESCE keeps the fresh-job behavior: a never-claimed job is
        # stamped with its claim time.
        job_id = self._make_compile_job(status="pending", timeout_seconds=1800)
        self.assertIsNone(self._started_at(job_id))

        claimed = self.store.claim_next_compile_job()

        self.assertIsNotNone(claimed)
        self.assertIsNotNone(claimed.started_at)
        self.assertEqual(self._started_at(job_id), claimed.started_at)
        with self.pool.connection() as conn:
            status = conn.execute(
                "SELECT status FROM draft_jobs WHERE id = ?", (job_id,)
            ).fetchone()[0]
        self.assertEqual(status, "running")


# ═══════════════════════════════════════════════════════════════════════════
# PRR-013 (draft_store.update_input_metadata archived guard)
# ═══════════════════════════════════════════════════════════════════════════


class TestPRR013ArchivedDraftInputMetadataGuard(CompilePipelineTestBase):
    async def test_material_input_metadata_change_on_archived_draft_rejected(self):
        # Positive control: the same material change succeeds on a live draft
        # (so the rejection below is the archived guard, nothing else).
        updated = self.store.update_input_metadata(
            draft_id=self.draft_id,
            owner_id=OWNER_ID,
            input_id=self.input_id,
            authority="secondary",
        )
        self.assertEqual(updated.authority, "secondary")
        self.store.update_input_metadata(
            draft_id=self.draft_id,
            owner_id=OWNER_ID,
            input_id=self.input_id,
            authority="primary",
        )

        # Archive exactly as update_draft's own archived tests do: from a
        # compilable status with no active jobs ('running' cannot archive).
        self.conn.execute(
            "UPDATE drafts SET status = 'draft' WHERE id = ?", (self.draft_id,)
        )
        self.conn.commit()
        draft = self.store.get_draft(self.draft_id, OWNER_ID)
        archived = self.store.archive_draft(
            draft_id=self.draft_id,
            owner_id=OWNER_ID,
            lock_version=draft.lock_version,
        )
        self.assertEqual(archived.status, "archived")

        with self.assertRaises(InvalidTransitionError):
            self.store.update_input_metadata(
                draft_id=self.draft_id,
                owner_id=OWNER_ID,
                input_id=self.input_id,
                authority="secondary",
            )


# ═══════════════════════════════════════════════════════════════════════════
# PRR-022 (DraftJobProcessor._commit_success settled-cancelled event row)
# ═══════════════════════════════════════════════════════════════════════════


class TestPRR022CommitBoundaryCancelWritesDurableEvent(ParseJobTestBase):
    async def test_settled_cancel_at_commit_writes_job_cancelled_event(self):
        draft = self.make_draft()
        input_record = await self.add_input(draft.id)
        job = self.enqueue_parse_job(draft.id, 1, input_record.id)

        resolved_path = str(self.storage.resolve(input_record.storage_relpath))
        parsed_but_cancelled = "parsed but cancelled before commit"
        self.extraction.responses[resolved_path] = ExtractedDocument(
            text=parsed_but_cancelled,
            character_count=len(parsed_but_cancelled),
            media_type="text/plain",
            warnings=[],
        )

        # AC17's race: the cancel commits between pre-commit check #2 and the
        # output commit, so only _commit_success's in-transaction recheck can
        # honor it.
        real_limit = self.processor._exceeds_parsed_char_limit

        def limit_eval_and_cancel(draft_id, input_id, extracted):
            with self.pool.connection() as conn:
                DraftStore(conn).request_job_cancel(
                    draft_id=draft_id, owner_id=1, job_id=job.id
                )
            return real_limit(draft_id, input_id, extracted)

        self.processor._exceeds_parsed_char_limit = limit_eval_and_cancel

        await self.processor.start()
        try:
            await self.wait_until(
                lambda: self.get_job(draft.id, 1, job.id).status
                in ("completed", "failed", "cancelled")
            )
        finally:
            await self.processor.stop()

        # REQUIRED (AC17 core): the cancelled output was discarded.
        raw = self.raw_input_row(input_record.id)
        self.assertIsNone(raw["parsed_text"])
        self.assertEqual(raw["parse_status"], "cancelled")
        self.assertEqual(self.get_job(draft.id, 1, job.id).status, "cancelled")

        # REQUIRED (PRR-022): the durable job_cancelled draft_events rows the
        # cancellation leaves behind. Exactly two writers can produce this
        # event type: the API cancel path (request_job_cancel, which fired in
        # the hook above) and the processor's settled-cancelled commit — the
        # row the fix adds. A pre-fix run leaves only the API row behind.
        with self.pool.connection() as conn:
            events = conn.execute(
                "SELECT event_type, actor_user_id, event_json FROM draft_events "
                "WHERE draft_id = ? AND job_id = ? AND event_type = 'job_cancelled'",
                (draft.id, job.id),
            ).fetchall()
        self.assertEqual(
            len(events),
            2,
            f"expected the API cancel row plus the processor's settled-cancel "
            f"row, found {len(events)}: {tuple(dict(e) for e in events)}",
        )
        for event in events:
            payload = json.loads(event["event_json"])
            self.assertEqual(payload.get("prior_status"), "running")
            self.assertEqual(event["actor_user_id"], 1)


# ═══════════════════════════════════════════════════════════════════════════
# PRR-024 (DraftJobProcessor._claim_next_job throttled unwired-engine warning)
# ═══════════════════════════════════════════════════════════════════════════


class TestPRR024UnwiredEngineClaimWarningThrottled(ParseJobTestBase):
    async def test_unwired_engine_warns_once_and_never_claims_compile(self):
        draft = self.make_draft()
        # One queued compile job, exactly as _sync_enqueue_compile leaves it.
        with self.pool.connection() as conn:
            conn.execute(
                "INSERT INTO draft_jobs (draft_id, vault_id, created_by, job_type, "
                "status, max_model_calls, timeout_seconds, model_call_count, "
                "prompt_bundle_version, compile_input_sha256) "
                "VALUES (?, 1, 1, 'compile', 'pending', 40, 1800, 0, ?, NULL)",
                (draft.id, PROMPT_BUNDLE_VERSION),
            )
            conn.commit()

        # ParseJobTestBase's processor keeps the constructor-default engine
        # None: the state between start() and set_rag_engine.
        self.assertIsNone(self.processor._engine)

        with self.assertLogs(
            "app.services.draft_job_processor", level="WARNING"
        ) as logs:
            first = await asyncio.to_thread(self.processor._claim_next_job)
            second = await asyncio.to_thread(self.processor._claim_next_job)

        # REQUIRED: compile claiming is deferred, not failed.
        self.assertIsNone(first)
        self.assertIsNone(second)
        # REQUIRED: the warning fired exactly once, not once per poll.
        fired = [
            record
            for record in logs.records
            if "RAG engine not wired" in record.getMessage()
        ]
        self.assertEqual(
            len(fired),
            1,
            f"unwired-engine warning fired {len(fired)} time(s), expected exactly 1",
        )
        # The queued job was untouched.
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT status FROM draft_jobs WHERE draft_id = ? "
                "AND job_type = 'compile'",
                (draft.id,),
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "pending")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
