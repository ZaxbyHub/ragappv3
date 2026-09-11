"""Issue #517 acceptance checks: draft-input evidence and manuscript reach.

Discriminating regression tests (expected RED at HEAD d9e3460) for the
draft-review finding DRAFT-016:

* **AC1** — a compile whose ONLY input is an uploaded manuscript against a
  genuinely empty vault must still snapshot that manuscript as
  ``draft_input`` evidence with a ``[D1]`` label: ``draft_evidence`` rows with
  ``source_kind='draft_input'``, a model-emitted ``[D1]`` citation that
  survives pre-Fact sanitation into the assembled revision, ``source_only``
  still True (input evidence is not vault evidence), and the claim ledger
  resolving the citation. Today ``_label_evidence`` mints no D labels, the
  ``EvidenceSnapshot`` carries no ``draft_input_id``, and pre-Fact sanitation
  strips every ``[D1]``.
* **AC2** — outline/draft stages must see the manuscript beyond the first
  ``_MAX_FACETS_PER_INPUT`` (5) sentences: with a 12-sentence manuscript whose
  9th sentence carries a distinctive marker and an outline that assigns input
  label D1 to section 2, the section-2 draft prompt must contain that
  sentence's text. Today the draft desk sees only the (empty) evidence
  registry, so nothing beyond the first five sentences ever reaches section
  drafting.

Also carries the **P1 preserving check**: a NON-empty vault retrieval keeps
``source_only`` False (the genuine empty-vault acknowledgement must not
trigger when real evidence was found).
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

try:  # pragma: no cover - CI installs no lancedb; backend/conftest.py stubs it
    import lancedb  # noqa: F401
except ImportError:  # pragma: no cover
    import types

    sys.modules["lancedb"] = types.ModuleType("lancedb")

from app.services.draft_research import run_research
from tests.draft_room.test_issue517_facts_acceptance import (
    DOC_SOURCE,
    MANUSCRIPT_TEXT,
    FakeModel,
    FakeRetriever,
    Issue517PipelineBase,
    draft_section_json,
    fact_json,
    happy_responses,
    outline_json,
    research_json,
)

MARKER = "ZZXSENT-NINE"

TWELVE_SENTENCE_MANUSCRIPT = " ".join(
    (
        f"Record number {n:02d} documents one charter rule for the committee file."
        if n != 9
        else f"Record number nine {MARKER} documents one charter rule for the "
        "committee file."
    )
    for n in range(1, 13)
)

DRAFT_WITH_CITATION = "The internal review window is thirty days. [D1]"
DRAFT_PROPOSITION = "The internal review window is thirty days"


# ── AC1 (DRAFT-016 part 1): draft_input evidence + surviving [D1] ────────────


class TestDraftInputEvidenceSnapshot(Issue517PipelineBase):
    """Empty vault + manuscript-only input must still yield D-labeled
    draft_input evidence and a surviving ``[D1]`` citation."""

    async def test_manuscript_becomes_d1_evidence_and_citation_survives(self):
        model = FakeModel(
            happy_responses(
                outline=[outline_json(labels=("D1",))],
                draft=[draft_section_json(
                    markdown=DRAFT_WITH_CITATION, labels=("D1",)
                )],
                fact=[fact_json(proposition=DRAFT_PROPOSITION, labels=("D1",))],
            )
        )
        # Pre-fix the compile itself dies at assemble: with no D registry the
        # [D1] citation is stripped as unknown, and the sanitizer's resulting
        # warning finding cannot even be persisted. Tolerate that failure so
        # the assertion below pins the missing-evidence defect itself rather
        # than crashing on the side effect.
        from app.services.draft_pipeline import CompileFailure

        try:
            await self._run(model=model, retriever=FakeRetriever(empty=True))
        except CompileFailure:
            pass

        print("AC1 CHECK: FAIL", flush=True)

        # (1) The manuscript input is snapshotted as draft_input evidence D1.
        evidence_rows = self.conn.execute(
            "SELECT label, source_kind, draft_input_id, passage FROM draft_evidence "
            "WHERE job_id = ? ORDER BY id",
            (self.job_id,),
        ).fetchall()
        d_rows = [
            row
            for row in evidence_rows
            if row["source_kind"] == "draft_input" and row["label"] == "D1"
        ]
        self.assertTrue(
            d_rows,
            "research must persist draft_evidence rows with "
            "source_kind='draft_input' and label 'D1' for the manuscript input; "
            f"persisted evidence rows: {[tuple(r) for r in evidence_rows]}",
        )
        self.assertEqual(d_rows[0]["draft_input_id"], self.input_id)

        # (2) A model-emitted [D1] citation survives to the assembled revision.
        revision = self._current_revision()
        self.assertIsNotNone(
            revision,
            "the compile must assemble a revision once the [D1] label is "
            "backed by the draft-input evidence registry",
        )
        self.assertIn(
            "[D1]",
            revision["content_md"],
            "a [D1] citation backed by the draft-input evidence registry must "
            "survive pre-Fact sanitation into the persisted revision (it must "
            "not be stripped like an unknown label); content was: "
            f"{revision['content_md']!r}",
        )

        # (3) source_only stays True: input evidence is not vault evidence, so
        # the genuine empty-vault acknowledgement must still be required.
        qa_summary = json.loads(revision["qa_summary_json"] or "{}")
        self.assertTrue(qa_summary.get("source_only"))

        # (4) The claim ledger resolves the citation to the D1 evidence row.
        claims = self._claims(revision["id"])
        matching = [
            row for row in claims if row["claim_text"] == DRAFT_PROPOSITION
        ]
        self.assertTrue(matching, "the fact-checked claim must be in the ledger")
        links = self.conn.execute(
            "SELECT cs.evidence_id, e.label FROM draft_claim_sources cs "
            "JOIN draft_claims c ON c.id = cs.claim_id "
            "JOIN draft_evidence e ON e.id = cs.evidence_id "
            "WHERE c.revision_id = ?",
            (revision["id"],),
        ).fetchall()
        self.assertIn(
            "D1",
            [row["label"] for row in links],
            "the claim ledger must resolve the [D1] citation to the snapshotted "
            "draft-input evidence row",
        )

        # (5) With the citation backed, the compile completes normally.
        self.assertEqual(self._job_row()["status"], "completed")


# ── AC2 (DRAFT-016 part 2): manuscript reach beyond the facet budget ─────────


class TestManuscriptReachBeyondFacets(Issue517PipelineBase):
    """Section drafting must see manuscript content past the first five
    sentences (the per-input facet budget)."""

    MANUSCRIPT = TWELVE_SENTENCE_MANUSCRIPT

    async def test_section_two_draft_prompt_carries_sentence_nine(self):
        model = FakeModel(
            happy_responses(
                outline=[outline_json(section_count=2, labels=("D1",))],
                draft=[draft_section_json(
                    markdown="The review window is thirty days.", labels=("D1",)
                )],
                fact=[fact_json(proposition=DRAFT_PROPOSITION, labels=())],
            )
        )
        await self._run(model=model, retriever=FakeRetriever(empty=True))

        job = self._job_row()
        self.assertEqual(job["status"], "completed", "fixture: compile must succeed")
        self.assertIsNotNone(self._current_revision())

        draft_prompts = model.prompts_for("draft")
        self.assertEqual(
            len(draft_prompts),
            2,
            "fixture: both outline sections must reach the draft desk",
        )

        print("AC2 CHECK: FAIL", flush=True)
        self.assertIn(
            MARKER,
            draft_prompts[1],
            "the section-2 draft prompt (whose outline entry cites the "
            "manuscript input label D1) must carry the manuscript content "
            "beyond the first five facet sentences — sentence nine's "
            f"{MARKER!r} marker never reached section drafting",
        )


# ── P1 (preserving): non-empty retrieval keeps source_only False ─────────────


class TestSourceOnlyStaysFalseWithVaultEvidence(unittest.IsolatedAsyncioTestCase):
    """P1 preserving check — must stay GREEN on the current tree and after
    the DRAFT-016 fix (input evidence must not flip the empty-vault flag)."""

    async def test_non_empty_retrieval_makes_source_only_false(self):
        retriever = FakeRetriever()  # answers the manuscript facet with DOC_SOURCE
        model = FakeModel({"research": [research_json()]})

        outcome = await run_research(
            brief={},
            inputs=[
                {
                    "input_id": 1,
                    "role": "manuscript",
                    "text": MANUSCRIPT_TEXT,
                }
            ],
            vault_id=1,
            retrieve=retriever,
            complete=model,
            limit=5,
            retry_limit=1,
        )

        self.assertTrue(outcome.evidence, "fixture: evidence must be retrieved")
        self.assertEqual(outcome.evidence[0].label, "S1")
        self.assertEqual(outcome.evidence[0].source_kind, "document")
        self.assertFalse(
            outcome.source_only,
            "source_only must stay False when vault evidence WAS retrieved",
        )
        self.assertFalse(outcome.packet.source_only)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
