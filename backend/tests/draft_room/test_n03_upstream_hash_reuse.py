"""Phase-4 regression companions for issue #787 (NOT part of the frozen set).

Authored with the fix (per the approved plan): the GX-026 killing tests, the
8-stage corrupted-checkpoint matrix, the non-dict JSON arm, and the
``_apply_edits`` routing matrix. The frozen acceptance checks live in
``test_n03_checkpoint_resume.py`` / ``test_n03_job_json_field.py`` /
``test_n03_prompt_contracts.py``; the structural guardrails live in
``backend/tests/test_n03_guardrails.py``.
"""

import json
import logging
import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.services.draft_pipeline import (  # noqa: E402
    _apply_edits,
    _stage_input_hash,
)
from app.services.draft_prompts import CopyEdit  # noqa: E402
from app.services.draft_store import DraftStore, sha256_text  # noqa: E402
from draft_room import test_draft_pipeline as tdp  # noqa: E402

#: One wrong-typed value for a list field of each stage's artifact model, so
#: the corruption genuinely fails schema validation for every model (the
#: all-defaulted Copy/Standards/Fact reports would swallow a stray key).
CORRUPT = {
    "intake": '{"inputs": 7}',
    "research": '{"evidence": 7}',
    "outline": '{"sections": 7}',
    "draft": '{"sections": 7}',
    "lint": '{"findings": 7}',
    "copy": '{"edits": 7}',
    "standards": '{"edits": 7}',
    "fact": '{"claims": 7}',
}

#: Consulted stages grouped by the retry start_stage that makes their
#: inherited rows reachable (inheritance is strictly before start_stage;
#: fact/assemble are never inherited cross-job).
MATRIX = [
    ("intake", "copy"),
    ("research", "copy"),
    ("outline", "copy"),
    ("draft", "copy"),
    ("lint", "copy"),
    ("copy", "fact"),
    ("standards", "fact"),
]


class _RetryFixtures:
    """Retry helpers lifted from the harness as a mixin.

    Subclassing ``TestRetryStartStageInheritance`` for its fixtures re-ran the
    harness's 11 compile-based tests verbatim under each new class name
    (33 duplicate full-pipeline runs in the Backend CI job). These three
    helpers are all the N03 companions need, and ``PipelineTestBase`` itself
    declares no tests, so the fixtures cost nothing.
    """

    CLOCK = datetime(2026, 8, 1, tzinfo=timezone.utc)

    def _context_for(self, job_id):
        with self.pool.connection() as conn:
            job = DraftStore(conn).get_job(
                draft_id=self.draft_id, owner_id=tdp.OWNER_ID, job_id=job_id
            )
            return tdp._build_context(conn, job, self.CLOCK)

    def _seal(self, job_id, start_stage):
        """Write the fingerprint the retry route would have stored."""
        self.conn.execute(
            "UPDATE draft_jobs SET start_stage = ? WHERE id = ?",
            (start_stage, job_id),
        )
        self.conn.commit()
        self.conn.execute(
            "UPDATE draft_jobs SET compile_input_sha256 = ?, "
            "prompt_bundle_version = ? WHERE id = ?",
            (
                self._context_for(job_id).compile_fingerprint,
                tdp.PROMPT_BUNDLE_VERSION,
                job_id,
            ),
        )
        self.conn.commit()

    def _make_retry(self, start_stage):
        job_id = self._make_compile_job(
            parent_job_id=self.job_id, start_stage=start_stage, attempt_no=2
        )
        self._seal(job_id, start_stage)
        self.conn.execute(
            "UPDATE drafts SET status = 'running' WHERE id = ?", (self.draft_id,)
        )
        self.conn.commit()
        return job_id

    async def _run_job(self, job_id, model=None):
        model = model or tdp.FakeModel(tdp.happy_responses())
        await tdp.run_compile(
            job_id=job_id,
            pool=self.pool,
            deps=tdp.PipelineDeps(
                retrieve_sources=tdp.FakeRetriever(),
                complete=model,
                now=tdp.FakeClock(self.CLOCK),
            ),
        )
        return model

    async def _seed_failed_parent(self):
        """Run this job to a real failure at Copy (genuine stage rows)."""
        self._seal(self.job_id, "research")
        model = tdp.FakeModel(tdp.happy_responses(copy=["not json at all"]))
        failure = await self._run_expect_failure(model=model)
        self.assertEqual(failure.code, tdp.CODE_INVALID_STAGE_OUTPUT)
        return model

    def _completed_for(self, job_id):
        return [
            row[0]
            for row in self.conn.execute(
                "SELECT stage FROM draft_job_stages WHERE job_id = ? AND "
                "status = 'completed' ORDER BY id",
                (job_id,),
            ).fetchall()
        ]


class TestN03UpstreamHashReuse(_RetryFixtures, tdp.PipelineTestBase):
    """GX-026 killers: the upstream-artifact fold must invalidate checkpoints."""

    async def test_input_hash_folds_upstream_artifact_hashes(self):
        ctx = self._context_for(self.job_id)
        base_payload = {"sources": "0" * 64, "research": "a" * 64}
        changed = dict(base_payload, research="b" * 64)
        h1 = _stage_input_hash("outline", ctx, base_payload)
        h2 = _stage_input_hash("outline", ctx, changed)
        self.assertNotEqual(
            h1,
            h2,
            "a stage input hash must change when an upstream artifact hash "
            "changes with the world, bundle, prompt and brief held fixed",
        )

    async def test_changed_upstream_packet_forces_outline_rerun(self):
        await self._seed_failed_parent()
        row = self.conn.execute(
            "SELECT artifact_json FROM draft_job_stages "
            "WHERE job_id = ? AND stage = 'research' AND status = 'completed'",
            (self.job_id,),
        ).fetchone()
        packet = json.loads(row[0])
        packet["gaps"] = [
            {
                "description": "n03 upstream packet change",
                "impact": "none",
                "blocks_drafting": False,
            }
        ]
        new_json = json.dumps(packet, sort_keys=True, separators=(",", ":"))
        # Hash-consistent, still-valid research artifact: research is reused,
        # but the outline stage input hash folds the research packet hash and
        # must stop matching the parent's outline row.
        self.conn.execute(
            "UPDATE draft_job_stages SET artifact_json = ?, artifact_sha256 = ? "
            "WHERE job_id = ? AND stage = 'research'",
            (new_json, sha256_text(new_json), self.job_id),
        )
        self.conn.commit()

        retry_id = self._make_retry("copy")
        await self._run_job(retry_id)

        rows = self.conn.execute(
            "SELECT COUNT(*) FROM draft_job_stages "
            "WHERE job_id = ? AND stage = 'outline'",
            (retry_id,),
        ).fetchone()[0]
        self.assertGreaterEqual(
            rows,
            2,
            "an inherited outline checkpoint must not survive a changed "
            "upstream research packet",
        )

    async def _successful_parent(self):
        # Seal the fresh-compile fingerprint (start_stage NULL) so the retry's
        # inheritance gate can recompute and match it.
        self._seal(self.job_id, None)
        model = await self._run_job(self.job_id)
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM draft_jobs WHERE id = ?", (self.job_id,)
            ).fetchone()[0],
            "completed",
            "parent must complete so every stage row exists",
        )
        return model

    def _restore_pre_run_world(self, *, reopen_job: bool = False) -> None:
        """Drop the revision the successful run created and reopen the draft.

        The compile fingerprint folds the draft's CURRENT revision, so the
        retry would otherwise compute a different world hash than the parent
        row recorded and re-run every stage for the wrong reason (input
        mismatch instead of corruption). Removing the revision restores the
        pre-run world (prior revision absent) for the retry's context.
        The parent job itself stays ``completed`` for cross-job retries —
        ``idx_draft_jobs_one_active_compile`` forbids a second live compile
        row for the draft — and is reopened only for the same-job fact case
        (no new row is inserted there).
        """
        self.conn.execute(
            "DELETE FROM draft_revisions WHERE draft_id = ?", (self.draft_id,)
        )
        self.conn.execute(
            "UPDATE drafts SET status = 'running' WHERE id = ?", (self.draft_id,)
        )
        if reopen_job:
            self.conn.execute(
                "UPDATE draft_jobs SET status = 'running' WHERE id = ?",
                (self.job_id,),
            )
        self.conn.commit()

    def _poison_parent_stage(self, stage: str) -> None:
        bad = CORRUPT[stage]
        self.conn.execute(
            "UPDATE draft_job_stages SET artifact_json = ?, artifact_sha256 = ? "
            "WHERE job_id = ? AND stage = ?",
            (bad, sha256_text(bad), self.job_id, stage),
        )
        self.conn.commit()

    async def _corruption_case(self, stage: str, start_stage: str) -> None:
        """One stage's corruption -> re-run (each case gets a fresh fixture:
        a poisoned row from a prior subTest iteration would otherwise leak
        into the parent's next run and confound the count)."""
        await self._successful_parent()
        self._restore_pre_run_world()
        self._poison_parent_stage(stage)

        retry_id = self._make_retry(start_stage)
        await self._run_job(retry_id)

        rows = self.conn.execute(
            "SELECT COUNT(*) FROM draft_job_stages "
            "WHERE job_id = ? AND stage = ?",
            (retry_id, stage),
        ).fetchone()[0]
        self.assertGreaterEqual(
            rows,
            2,
            "the corrupted inherited row lands on the retry job; only "
            "a genuine re-run writes a second attempt row",
        )
        status = self.conn.execute(
            "SELECT status FROM draft_jobs WHERE id = ?", (retry_id,)
        ).fetchone()[0]
        self.assertEqual(
            status,
            "completed",
            "a corrupted checkpoint must degrade to a re-run, never a "
            "terminal failure",
        )

    def _legacy_copy_report_json(self, extra: str = "") -> str:
        """A pre-#787 copy report artifact: no ``unapplied_edits`` key."""
        return (
            '{"edits": [], "findings": ["legacy desk note"]%s}'
            % (", " + extra if extra else "")
        )

    async def test_legacy_keyless_copy_checkpoint_reuses_without_raising(self):
        # An artifact written before the field existed must still validate
        # (defaults to []) and its desk findings must re-surface (FB-25).
        self._seal(self.job_id, None)
        model = tdp.FakeModel(
            tdp.happy_responses(copy=[self._legacy_copy_report_json()])
        )
        await self._run_job(self.job_id, model=model)

        rows = self.conn.execute(
            "SELECT rule_id, message FROM draft_findings WHERE job_id = ?",
            (self.job_id,),
        ).fetchall()
        messages = [row[1] for row in rows]
        self.assertTrue(
            any("legacy desk note" in m for m in messages),
            "the reused legacy report's desk findings must re-surface",
        )

    async def test_legacy_reuse_does_not_invent_edit_rows(self):
        self._seal(self.job_id, None)
        model = tdp.FakeModel(
            tdp.happy_responses(copy=[self._legacy_copy_report_json()])
        )
        await self._run_job(self.job_id, model=model)
        invented = self.conn.execute(
            "SELECT COUNT(*) FROM draft_findings WHERE job_id = ? AND "
            "rule_id LIKE 'copy.edit_%'",
            (self.job_id,),
        ).fetchone()[0]
        self.assertEqual(invented, 0, "an empty legacy list must surface nothing")

    async def test_corrupted_fact_checkpoint_reruns_on_restart(self):
        """The fact deserialize site fires through the job's OWN rows only."""
        await self._successful_parent()
        self._restore_pre_run_world(reopen_job=True)
        self._poison_parent_stage("fact")
        await self._run_job(self.job_id)

        rows = self.conn.execute(
            "SELECT COUNT(*) FROM draft_job_stages "
            "WHERE job_id = ? AND stage = 'fact'",
            (self.job_id,),
        ).fetchone()[0]
        self.assertGreaterEqual(
            rows, 2, "the corrupted own fact row must force a fact re-run"
        )


def _make_corruption_test(stage: str, start_stage: str):
    async def test(self) -> None:
        await self._corruption_case(stage, start_stage)

    test.__name__ = f"test_corrupted_{stage}_checkpoint_reruns"
    return test


for _stage, _start in MATRIX:
    setattr(
        TestN03UpstreamHashReuse,
        f"test_corrupted_{_stage}_checkpoint_reruns",
        _make_corruption_test(_stage, _start),
    )


class TestN03JobJsonFieldNonDict(tdp.PipelineTestBase):
    def test_non_dict_json_field_is_logged(self):
        self.store.set_job_json_field(
            job_id=self.job_id, field="brief_snapshot_json", value={"a": 1}
        )
        self.conn.execute(
            "UPDATE draft_jobs SET brief_snapshot_json = '[1, 2, 3]' "
            "WHERE id = ?",
            (self.job_id,),
        )
        self.conn.commit()

        records: list[logging.LogRecord] = []

        class Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        handler = Capture(level=logging.WARNING)
        root = logging.getLogger()
        old_level = root.level
        root.setLevel(logging.WARNING)
        root.addHandler(handler)
        try:
            decoded = self.store.get_job_json_field(
                job_id=self.job_id, field="brief_snapshot_json"
            )
        finally:
            root.removeHandler(handler)
            root.setLevel(old_level)

        n = sum(
            1
            for record in records
            if record.levelno >= logging.WARNING
            and str(self.job_id) in record.getMessage()
            and "brief_snapshot_json" in record.getMessage()
        )
        self.assertEqual(decoded, {})
        self.assertGreaterEqual(
            n, 1, "a decodable-but-non-object value must be logged, not silent"
        )


UNLOCATABLE_RATIONALE = "n03 unlocatable edit probe"


def unlocatable_edit_json() -> str:
    """One desk edit whose excerpt matches nothing in the candidate."""
    return json.dumps(
        {
            "edits": [
                {
                    "section_id": "sec-01",
                    "start": -1,
                    "end": -1,
                    "before_sha256": "",
                    "after_sha256": "",
                    "before_excerpt": "absent from the candidate entirely",
                    "after_excerpt": "replacement text",
                    "category": "precision",
                    "rationale": UNLOCATABLE_RATIONALE,
                    "semantic_change": False,
                    "affected_claim_ids": [],
                    "affected_evidence_labels": ["S1"],
                }
            ],
            "findings": [],
        }
    )


class TestN03EditSurfacing(_RetryFixtures, tdp.PipelineTestBase):
    """The unapplied-edit finding channel, end to end (issue #787 review)."""

    def _edit_findings(self, job_id):
        return self.conn.execute(
            "SELECT stage, rule_id, original_text, suggestion FROM "
            "draft_findings WHERE job_id = ? AND rule_id = 'copy.edit_not_located'",
            (job_id,),
        ).fetchall()

    async def test_unlocatable_edit_surfaces_as_a_persisted_finding(self):
        model = tdp.FakeModel(
            tdp.happy_responses(copy=[unlocatable_edit_json()])
        )
        await self._run(model=model)

        rows = self._edit_findings(self.job_id)
        self.assertEqual(len(rows), 1, "exactly one surfacing row expected")
        stage, rule_id, original_text, suggestion = rows[0]
        self.assertEqual(stage, "copy")
        self.assertEqual(original_text, "absent from the candidate entirely")
        self.assertEqual(suggestion, "replacement text")

    async def test_surfaced_finding_survives_checkpoint_reuse_on_retry(self):
        # Parent: the copy desk reports an unlocatable edit, and Fact then
        # fails on invalid output, so the retry reuses the copy checkpoint.
        self._seal(self.job_id, None)
        model = tdp.FakeModel(
            tdp.happy_responses(
                copy=[unlocatable_edit_json()], fact=["not json at all"]
            )
        )
        failure = await self._run_expect_failure(model=model)
        self.assertEqual(failure.code, tdp.CODE_INVALID_STAGE_OUTPUT)

        retry_id = self._make_retry("fact")
        await self._run_job(retry_id)

        rows = self._edit_findings(retry_id)
        self.assertEqual(
            len(rows),
            1,
            "the reused copy checkpoint must re-surface its unapplied edit — "
            "the parent's pending findings died with the failed job",
        )
        self.assertEqual(rows[0][1], "copy.edit_not_located")


def _edit(
    *,
    start=-1,
    end=-1,
    before_sha256="",
    after_sha256="",
    before_excerpt="thirty days",
    after_excerpt="30 days",
) -> CopyEdit:
    return CopyEdit(
        section_id="sec-01",
        start=start,
        end=end,
        before_sha256=before_sha256,
        after_sha256=after_sha256,
        before_excerpt=before_excerpt,
        after_excerpt=after_excerpt,
        category="precision",
        rationale="n03 matrix edit",
        semantic_change=False,
        affected_claim_ids=[],
        affected_evidence_labels=["S1"],
    )


class TestN03ApplyEditsMatrix(unittest.TestCase):
    """Routing matrix for the excerpt-locating `_apply_edits`."""

    TEXT = "The review window is thirty days under the charter."

    def test_correctly_hashed_edit_still_applies(self):
        old = "thirty days"
        new = "30 days"
        start = self.TEXT.index(old)
        edit = _edit(
            start=start,
            end=start + len(old),
            before_sha256=sha256_text(old),
            after_sha256=sha256_text(new),
        )
        result = _apply_edits(self.TEXT, [edit])
        self.assertIn(new, result.text)
        self.assertEqual(len(result.applied_edits), 1)

    def test_excerpt_only_edit_applies_with_server_computed_pins(self):
        result = _apply_edits(self.TEXT, [_edit()])
        self.assertIn("30 days", result.text)
        self.assertEqual(len(result.applied_edits), 1)
        applied = result.applied_edits[0]
        self.assertEqual(applied.start, self.TEXT.index("thirty days"))
        self.assertEqual(
            applied.before_sha256, sha256_text("thirty days")
        )
        self.assertEqual(applied.after_sha256, sha256_text("30 days"))

    def test_broken_offset_edit_applies_via_excerpt(self):
        result = _apply_edits(self.TEXT, [_edit(start=99999, end=99998)])
        self.assertIn("30 days", result.text)
        self.assertEqual(len(result.applied_edits), 1)

    def test_wrong_hash_edit_applies_via_excerpt(self):
        result = _apply_edits(
            self.TEXT, [_edit(before_sha256="f" * 64, after_sha256="e" * 64)]
        )
        self.assertIn("30 days", result.text)
        self.assertEqual(len(result.applied_edits), 1)

    def test_zero_match_excerpt_is_surfaced_not_dropped(self):
        result = _apply_edits(
            self.TEXT, [_edit(before_excerpt="absent from the text")]
        )
        self.assertEqual(self.TEXT, result.text)
        self.assertEqual(len(result.applied_edits), 0)
        self.assertEqual(len(result.unapplied_edits), 1)

    def test_multi_match_excerpt_is_surfaced_not_dropped(self):
        text = "same same wording elsewhere"
        result = _apply_edits(text, [_edit(before_excerpt="same")])
        self.assertEqual(text, result.text)
        self.assertEqual(len(result.unapplied_edits), 1)

    def test_overlapping_pair_surfaces_the_invalidated_span(self):
        # The spans genuinely OVERLAP: the high edit rewrites the tail of the
        # low edit's span, so when the high edit applies first the low edit's
        # pinned bytes no longer verify. Its excerpt ("alpha beta") then
        # matches nothing in the mutated text, so it must be surfaced as
        # invalidated rather than applied at a stale offset (this is the only
        # arm of the apply loop that can produce an overlap-surfacing).
        text = "keep alpha beta keep"
        old_hi = "beta"
        new_hi = "BETA!"
        old_lo = "alpha beta"
        new_lo = "ALPHA+BETA"
        hi = _edit(
            before_excerpt=old_hi,
            after_excerpt=new_hi,
            start=text.index(old_hi),
            end=text.index(old_hi) + len(old_hi),
            before_sha256=sha256_text(old_hi),
            after_sha256=sha256_text(new_hi),
        )
        lo = _edit(
            before_excerpt=old_lo,
            after_excerpt=new_lo,
            start=text.index(old_lo),
            end=text.index(old_lo) + len(old_lo),
            before_sha256=sha256_text(old_lo),
            after_sha256=sha256_text(new_lo),
        )
        result = _apply_edits(text, [lo, hi])
        self.assertEqual(len(result.applied_edits), 1)
        self.assertEqual(result.applied_edits[0].after_excerpt, new_hi)
        self.assertEqual(len(result.unapplied_edits), 1)
        surfaced = result.unapplied_edits[0]
        self.assertEqual(surfaced.reason, "invalidated")
        self.assertEqual(surfaced.edit.before_excerpt, old_lo)
        # The invalidated edit is NOT applied at its stale offset: the only
        # replacement present is the high edit's ("beta" -> "BETA!").
        self.assertNotIn(new_lo, result.text)
        self.assertEqual(result.text, "keep alpha BETA! keep")

    def test_disjoint_pair_both_applies_and_serves_their_pins(self):
        text = "keep alpha and beta"
        lo = _edit(
            before_excerpt="alpha",
            after_excerpt="ALPHA",
            start=text.index("alpha"),
            end=text.index("alpha") + 5,
            before_sha256=sha256_text("alpha"),
        )
        hi = _edit(
            before_excerpt="beta",
            after_excerpt="BETA",
            start=text.index("beta"),
            end=text.index("beta") + 4,
            before_sha256=sha256_text("beta"),
        )
        result = _apply_edits(text, [lo, hi])
        self.assertEqual(len(result.applied_edits), 2)
        self.assertEqual(result.unapplied_edits, ())
        self.assertIn("ALPHA", result.text)
        self.assertIn("BETA", result.text)

    def test_applied_edit_always_carries_server_computed_after_pin(self):
        # Even on the supplied-span fast path the persisted after-pin must
        # describe the replacement actually written (FB-09), so a model that
        # hallucinates after_sha256 cannot put a lie in a durable artifact.
        text = "keep thirty days"
        edit = _edit(
            start=text.index("thirty days"),
            end=text.index("thirty days") + len("thirty days"),
            before_sha256=sha256_text("thirty days"),
            after_sha256="0" * 64,
            after_excerpt="30 days",
        )
        result = _apply_edits(text, [edit])
        self.assertEqual(len(result.applied_edits), 1)
        self.assertEqual(
            result.applied_edits[0].after_sha256, sha256_text("30 days")
        )

    def test_unicode_excerpt_round_trips_and_a_normalized_variant_is_surfaced(self):
        # Real model output copies prose verbatim: a curly apostrophe locates
        # exactly, while a normalized (NFC) variant of a decomposed excerpt is
        # NOT the same codepoint sequence and must surface rather than apply.
        text = "keep café thirty days"  # NFC in the source text
        curly = _edit(before_excerpt="café", after_excerpt="CAFE")
        result = _apply_edits(text, [curly])
        self.assertIn("CAFE", result.text)
        # NFD ("cafe" + U+0301) is a DIFFERENT codepoint sequence than the NFC
        # text above; the locator is exact, so it must surface, not apply.
        decomposed = _edit(
            before_excerpt="café", after_excerpt="COFFEE"
        )  # NFD: a different codepoint sequence
        result2 = _apply_edits(text, [decomposed])
        self.assertEqual(len(result2.unapplied_edits), 1)
        self.assertEqual(result2.unapplied_edits[0].reason, "not_located")
        self.assertEqual(result2.text, text)

    def test_no_op_edit_stays_silent(self):
        old = "thirty days"
        start = self.TEXT.index(old)
        edit = _edit(
            start=start,
            end=start + len(old),
            before_sha256=sha256_text(old),
            after_sha256=sha256_text(old),
            after_excerpt=old,
        )
        result = _apply_edits(self.TEXT, [edit])
        self.assertEqual(self.TEXT, result.text)
        self.assertEqual(len(result.applied_edits), 0)
        self.assertEqual(len(result.unapplied_edits), 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
