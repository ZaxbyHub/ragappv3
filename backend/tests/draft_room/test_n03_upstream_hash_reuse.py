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

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.services.draft_pipeline import (  # noqa: E402
    _apply_edits,
    _stage_input_hash,
)
from app.services.draft_prompts import CopyEdit  # noqa: E402
from app.services.draft_store import sha256_text  # noqa: E402
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


class TestN03UpstreamHashReuse(tdp.TestRetryStartStageInheritance):
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

    def test_overlapping_pair_relocates_or_surfaces_never_stale_apply(self):
        # The higher-offset edit applies first; the lower edit's supplied span
        # is invalidated by that application, so it must re-locate (unique
        # excerpt) rather than apply at a stale offset.
        text = "keep alpha beta keep"
        old_hi = "beta"
        new_hi = "BETA!"
        old_lo = "alpha"
        new_lo = "ALPHA!"
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
        self.assertIn(new_hi, result.text)
        self.assertIn(new_lo, result.text)
        self.assertNotIn(old_lo, result.text)
        self.assertNotIn(old_hi, result.text)

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
