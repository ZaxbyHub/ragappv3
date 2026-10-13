"""Frozen AC4-AC7 checks for issue #787 (TQ-instruction-batch-4-01/02/03).

Four independent prompt-contract checks; each must be able to fail alone:

* AC4 (``TestN03PromptContracts``) — the Copy and Standards prompts must not
  ask the model for ``before_sha256``/``after_sha256``. A language model
  cannot compute SHA-256, so requesting the pins guarantees garbage hashes
  that ``_apply_edits`` then silently drops (see AC6).
* AC5 (``TestN03PromptContracts``) — the Research prompt must not ask for
  per-evidence ``content_sha256``: the pipeline snapshots the real source
  hashes itself, so the model's guesses can only ever be wrong.
* AC6 (``TestN03CopyEditModelHashes``) — a copy edit whose model-supplied
  hashes are wrong but whose ``before_excerpt`` genuinely matches the
  candidate exactly once must be applied OR surfaced as a finding, never
  silently dropped.
* AC7 (``TestN03FactPromptInputs``) — the Fact prompt promises
  "claim-specific evidence retrieval"; the rendered fact prompt must then
  actually carry the claim-retrieval evidence (or the promise must go).
"""

import dataclasses
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.services.draft_prompts import PROMPTS  # noqa: E402
from app.services.draft_store import sha256_text  # noqa: E402
from draft_room import test_draft_pipeline as tdp  # noqa: E402

# ── AC6 fixtures: one unusable-hash edit against the canonical candidate ─────

#: Appears exactly once in SECTION_MARKDOWN ("The review window is 30 days.
#: [S1]"), which is what the Draft desk's fake response produces.
EDIT_OLD = "30 days"
EDIT_NEW = "thirty days"
EDIT_RATIONALE = "N03 copy edit with unusable model hashes"


def model_hashed_edit_json() -> str:
    """One desk edit with correct excerpts but garbage model-computed hashes.

    Same shape as the harness's ``edit_json`` (start/end computed against the
    candidate, correct ``before_excerpt``) except both hash pins are wrong —
    a real model's best effort at the fields AC4 says the prompts should not
    request.
    """
    start = tdp.CANDIDATE.index(EDIT_OLD)
    return json.dumps(
        {
            "edits": [
                {
                    "section_id": "sec-01",
                    "start": start,
                    "end": start + len(EDIT_OLD),
                    "before_sha256": "f" * 64,
                    "after_sha256": "e" * 64,
                    "before_excerpt": EDIT_OLD,
                    "after_excerpt": EDIT_NEW,
                    "category": "precision",
                    "rationale": EDIT_RATIONALE,
                    "semantic_change": False,
                    "affected_claim_ids": [],
                    "affected_evidence_labels": ["S1"],
                }
            ],
            "findings": [],
        }
    )


# ── AC7 fixtures: the claim-retrieval seam returns a marked passage ──────────

CLAIM_MARKER = "N03-CLAIM-MARKER-7f3a"
#: The normalized proposition of the harness's default ``fact_json`` claim;
#: this is the query ``_claim_retrieval_audit`` issues.
CLAIM_QUERY = "The review window is 30 days"
MARKER_PASSAGE = (
    "N03-CLAIM-MARKER-7f3a the charter fixes the internal review window at "
    "thirty days"
)
MARKER_SOURCE = tdp.FakeSource(
    kind="document",
    title="N03 claim marker source",
    passage=MARKER_PASSAGE,
    score=0.66,
    content_sha256=sha256_text(MARKER_PASSAGE),
)


class ClaimMarkerRetriever(tdp.FakeRetriever):
    """Facet queries keep the harness evidence; the claim-specific query
    (``self._deps.retrieve_sources`` in ``_claim_retrieval_audit``) returns a
    passage carrying the unique marker."""

    async def __call__(self, query, vault_id, *, limit, source_kinds=None):
        result = await super().__call__(
            query, vault_id, limit=limit, source_kinds=source_kinds
        )
        if " ".join(query.split()) == CLAIM_QUERY:
            return dataclasses.replace(result, sources=(MARKER_SOURCE,))
        return result


# ── AC4 / AC5: the prompts must not request hashes a model cannot compute ────


class TestN03PromptContracts(unittest.TestCase):
    longMessage = False

    def test_copy_and_standards_prompts_do_not_request_hashes(self):
        text = PROMPTS["copy"].template + PROMPTS["standards"].template
        requested = "before_sha256" in text or "after_sha256" in text
        self.assertFalse(requested, f"assert {requested} is False")

    def test_research_prompt_does_not_request_evidence_hashes(self):
        text = PROMPTS["research"].template
        requested = "content_sha256" in text
        self.assertFalse(requested, f"assert {requested} is False")


# ── AC6: an unusable-hash edit must be applied or surfaced, not dropped ──────


class TestN03CopyEditModelHashes(tdp.PipelineTestBase):
    longMessage = False

    async def test_copy_edit_with_model_hashes_is_applied_or_surfaced(self):
        model = tdp.FakeModel(
            tdp.happy_responses(copy=[model_hashed_edit_json()])
        )
        await self._run(model=model)

        revision = self._current_revision()
        if revision is not None and EDIT_NEW in revision["content_md"]:
            outcome = "applied"
        else:
            rows = self.conn.execute(
                "SELECT stage, message, original_text, suggestion "
                "FROM draft_findings WHERE job_id = ?",
                (self.job_id,),
            ).fetchall()
            mentioned = any(
                row["stage"] in ("copy", "standards")
                and any(
                    marker
                    in (row["message"] or "")
                    + (row["original_text"] or "")
                    + (row["suggestion"] or "")
                    for marker in (EDIT_OLD, EDIT_NEW, EDIT_RATIONALE)
                )
                for row in rows
            )
            outcome = "finding" if mentioned else "dropped"

        self.assertNotEqual(outcome, "dropped", f"assert {outcome!r} != 'dropped'")


# ── AC7: the fact prompt must carry the evidence it promises ─────────────────


class TestN03FactPromptInputs(tdp.PipelineTestBase):
    longMessage = False

    async def test_fact_prompt_contract_matches_its_inputs(self):
        retriever = ClaimMarkerRetriever()
        model = tdp.FakeModel(tdp.happy_responses())
        await self._run(model=model, retriever=retriever)

        fact_prompts = [
            prompt for prompt in model.prompts if tdp._stage_of(prompt) == "fact"
        ]
        consistent = (
            "claim-specific evidence retrieval" not in PROMPTS["fact"].template
        ) or any(CLAIM_MARKER in prompt for prompt in fact_prompts)
        self.assertTrue(consistent, f"assert {consistent}")
