"""Permanent tests for the Fact stage (issue #436, SPEC 11.8, 12.3-12.5, 17.1).

Drives the REAL Fact stage (``_CompileRun._stage_fact`` / ``_resolve_claims``
inside ``app.services.draft_pipeline``) through ``run_compile`` end to end
against a real temp SQLite database, reusing the harness conventions of
``test_draft_pipeline.py`` (``PipelinePool``, ``FakeModel``, ``FakeRetriever``,
``FakeClock``, ``PipelineTestBase``, canonical stage-payload builders) via a
relative import rather than re-declaring that infrastructure.

Cases already exercised by ``test_draft_pipeline.py`` are NOT repeated here:
byte-identity of the candidate across Standards/Fact/Assemble/revision, the
correction-loop cap storing a ``needs_review`` revision, an evidence label
absent from the snapshot being downgraded to ``unsupported``, a mismatched
direct quote being downgraded to ``unsupported``, a matching quote being
pinned to its passage, and the zero-result claim-retrieval audit. This file
complements those with the remaining SPEC 11.8/12.3-12.5 cases: all six claim
statuses, honest-scoring column hygiene, additional quote-fidelity shapes,
high-stakes widening, unresolved findings that persist without mutating
prose, and the Ready-eligibility signals the Fact stage itself produces.

Two SPEC 12.4 requirements are NOT implemented by the shipped
``draft_store.validate_exact_quote`` and are called out (not silently
asserted away) in ``TestQuoteFidelityGaps`` below: Unicode quote-*mark*-only
normalization is not applied to quote *content* (a curly apostrophe inside a
quoted span does not match a straight one in the passage), and an
ellipsis-marked omission does not validate against the source passage. Both
are reported as findings, not fixed here (module ownership boundary).
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

from .test_draft_pipeline import (  # noqa: E402 - see module docstring
    CANDIDATE,
    EVIDENCE_PASSAGE,
    FakeModel,
    PipelineTestBase,
    draft_section_json,
    fact_json,
    happy_responses,
    sha256_text,
)


def fact_claims_json(claims: list[dict], *, findings: list | None = None) -> str:
    """Build a raw FactReport JSON payload with full control over each claim.

    ``claims`` entries are merged onto sane defaults so callers only specify
    what the test cares about (mirrors ``fact_json`` in test_draft_pipeline
    but supports multiple claims / arbitrary field overrides such as
    ``high_stakes`` and ``single_source_warning``).
    """
    built = []
    for i, overrides in enumerate(claims, start=1):
        claim = {
            "claim_id": f"c{i}",
            "claim_type": "factual",
            "proposition": "The review window is 30 days",
            "status": "supported",
            "evidence_labels": ["S1"],
            "retrieval_audit": None,
            "single_source_warning": False,
            "high_stakes": False,
        }
        claim.update(overrides)
        built.append(claim)
    return json.dumps({"claims": built, "findings": list(findings or [])})


class DraftFactualityTestBase(PipelineTestBase):
    """Alias for readability; no behavioral change from PipelineTestBase."""


# ── 1. All six claim statuses are representable and persisted ────────────────


class TestAllClaimStatuses(DraftFactualityTestBase):
    async def _run_with_status(self, status: str, **fact_kwargs):
        model = FakeModel(happy_responses(fact=[fact_json(status=status, **fact_kwargs)]))
        await self._run(model=model)
        claims = self._claims()
        self.assertEqual(len(claims), 1)
        return claims[0]

    async def test_supported(self):
        claim = await self._run_with_status("supported")
        self.assertEqual(claim["status"], "supported")
        self.assertEqual(self._current_revision()["fact_status"], "passed")

    async def test_contradicted(self):
        claim = await self._run_with_status("contradicted")
        self.assertEqual(claim["status"], "contradicted")
        self.assertEqual(self._current_revision()["fact_status"], "findings")

    async def test_ambiguous(self):
        claim = await self._run_with_status("ambiguous")
        self.assertEqual(claim["status"], "ambiguous")
        self.assertEqual(self._current_revision()["fact_status"], "findings")

    async def test_stale(self):
        claim = await self._run_with_status("stale")
        self.assertEqual(claim["status"], "stale")
        self.assertEqual(self._current_revision()["fact_status"], "findings")

    async def test_unsupported(self):
        # Direct model-reported "unsupported" (not merely a downgrade) still
        # gets a claim-specific retrieval audit and a blocker finding.
        claim = await self._run_with_status("unsupported", labels=())
        self.assertEqual(claim["status"], "unsupported")
        audit = json.loads(claim["retrieval_audit_json"])
        self.assertIn("normalized_query", audit)
        self.assertEqual(self._current_revision()["fact_status"], "findings")

    async def test_opinion(self):
        claim = await self._run_with_status(
            "opinion", claim_type="opinion", proposition="The review window is 30 days"
        )
        self.assertEqual(claim["status"], "opinion")
        self.assertEqual(claim["claim_type"], "opinion")
        # An opinion is not a blocking factual status.
        self.assertEqual(self._current_revision()["fact_status"], "passed")


# ── 2. Fact never mutates prose, even under adversarial claim text ───────────


class TestFactNeverMutatesProse(DraftFactualityTestBase):
    async def test_fact_output_cannot_alter_the_stored_candidate(self):
        pre_fact_sha = None

        model = FakeModel(
            happy_responses(
                fact=[
                    fact_json(
                        status="supported",
                        proposition="The review window is 30 days",
                    )
                ]
            )
        )
        await self._run(model=model)

        standards_sha = self._stage("standards").candidate_sha256
        fact_row = self._stage("fact")
        assemble_sha = self._stage("assemble").candidate_sha256
        revision = self._current_revision()

        self.assertEqual(standards_sha, fact_row.candidate_sha256)
        self.assertEqual(fact_row.candidate_sha256, assemble_sha)
        self.assertEqual(assemble_sha, revision["content_sha256"])
        self.assertEqual(revision["content_md"], CANDIDATE)
        self.assertEqual(fact_row.content_md, CANDIDATE)


# ── 3. Lexical overlap alone never yields a supported verdict (AC-11) ────────


class TestLexicalOverlapIsNotSupport(DraftFactualityTestBase):
    async def test_a_valid_high_overlap_citation_that_the_model_calls_ambiguous_stays_ambiguous(
        self,
    ):
        # The claim proposition is drawn verbatim from the candidate and cites
        # the real evidence label, so lexical overlap against S1 is high --
        # but the model's own verdict is "ambiguous", not "supported". The
        # Fact stage must not upgrade it on overlap alone.
        model = FakeModel(
            happy_responses(
                fact=[
                    fact_json(
                        status="ambiguous",
                        proposition="The review window is 30 days",
                        labels=("S1",),
                    )
                ]
            )
        )
        await self._run(model=model)

        claims = self._claims()
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0]["status"], "ambiguous")
        self.assertNotEqual(claims[0]["status"], "supported")

        # The link table still records the lexical overlap score for the
        # citation -- but it is stored as context, never as a support verdict.
        revision = self._current_revision()
        link = self.conn.execute(
            "SELECT relationship, lexical_overlap_score FROM draft_claim_sources cs "
            "JOIN draft_claims c ON c.id = cs.claim_id WHERE c.revision_id = ?",
            (revision["id"],),
        ).fetchone()
        if link is not None:
            self.assertNotEqual(link["relationship"], "supports")
            self.assertIsNotNone(link["lexical_overlap_score"])


# ── 4. Honest-scoring column hygiene (SPEC 12.3 / hard rule 3) ───────────────


class TestHonestScoringColumnHygiene(DraftFactualityTestBase):
    _FORBIDDEN_SUBSTRINGS = (
        "confidence",
        "correctness",
        "entailment",
        "verification",
    )

    def _assert_no_forbidden_columns(self, table: str):
        rows = self.conn.execute(f"PRAGMA table_info({table})").fetchall()
        names = [row["name"] for row in rows]
        for name in names:
            lowered = name.lower()
            for forbidden in self._FORBIDDEN_SUBSTRINGS:
                self.assertNotIn(
                    forbidden, lowered, f"{table}.{name} uses a forbidden term"
                )
            # "support" alone is forbidden EXCEPT inside the one permitted
            # compound name lexical_overlap_score, or as part of legitimate
            # words like "supports"/"unsupported" data values (not column
            # names) -- no column may be literally named *_support*.
            if "support" in lowered:
                self.assertIn(
                    lowered,
                    {"lexical_overlap_score"},
                    f"{table}.{name} uses a forbidden 'support' column name",
                )

    async def test_no_forbidden_terms_in_persisted_fact_columns(self):
        await self._run()
        for table in ("draft_claims", "draft_claim_sources", "draft_findings"):
            self._assert_no_forbidden_columns(table)

    async def test_lexical_overlap_score_is_stored_separately_from_claim_status(self):
        quoted = "the internal review window at 30 days"
        model = FakeModel(
            happy_responses(
                draft=[draft_section_json(f'The charter fixes "{quoted}". [S1]')],
                fact=[fact_json(proposition=f'The charter fixes "{quoted}"', claim_type="quote")],
            )
        )
        await self._run(model=model)

        revision = self._current_revision()
        row = self.conn.execute(
            "SELECT c.status AS claim_status, cs.lexical_overlap_score "
            "FROM draft_claim_sources cs JOIN draft_claims c ON c.id = cs.claim_id "
            "WHERE c.revision_id = ?",
            (revision["id"],),
        ).fetchone()
        self.assertIsNotNone(row)
        # status lives on draft_claims; lexical_overlap_score lives on the
        # separate link table -- two distinct columns, not one conflated value.
        self.assertEqual(row["claim_status"], "supported")
        self.assertIsInstance(row["lexical_overlap_score"], float)

    async def test_fact_prompt_output_models_carry_no_forbidden_field_names(self):
        from app.services.draft_prompts import FactClaim, FactReport, RetrievalAudit

        for model_cls in (FactClaim, FactReport, RetrievalAudit):
            for field_name in model_cls.model_fields:
                lowered = field_name.lower()
                for forbidden in (
                    "confidence",
                    "correctness",
                    "entailment",
                    "verification",
                    "claim_confidence",
                    "factual_confidence",
                    "support_probability",
                ):
                    self.assertNotIn(
                        forbidden,
                        lowered,
                        f"{model_cls.__name__}.{field_name} uses a forbidden term",
                    )


# ── 5. Quote fidelity ─────────────────────────────────────────────────────────


class TestQuoteFidelity(DraftFactualityTestBase):
    async def test_curly_quote_delimited_span_still_pins_to_the_passage(self):
        quoted = "the internal review window at 30 days"
        model = FakeModel(
            happy_responses(
                draft=[draft_section_json(f"The charter fixes “{quoted}”. [S1]")],
                fact=[
                    fact_json(
                        proposition=f"The charter fixes “{quoted}”",
                        claim_type="quote",
                    )
                ],
            )
        )
        await self._run(model=model)

        claims = self._claims()
        self.assertEqual(claims[0]["status"], "supported")

    async def test_whitespace_normalized_quote_still_pins_to_the_passage(self):
        # The passage has single spaces; the quoted run in the candidate has
        # extra internal spacing, which is the one permitted normalization
        # (the quote-span regex itself excludes newlines from quoted runs).
        quoted_in_candidate = "the internal  review   window at 30 days"
        model = FakeModel(
            happy_responses(
                draft=[draft_section_json(f'The charter fixes "{quoted_in_candidate}". [S1]')],
                fact=[
                    fact_json(
                        proposition=f'The charter fixes "{quoted_in_candidate}"',
                        claim_type="quote",
                    )
                ],
            )
        )
        await self._run(model=model)

        claims = self._claims()
        self.assertEqual(claims[0]["status"], "supported")

    async def test_paraphrase_of_a_quote_is_not_marked_supported(self):
        # Words are changed relative to the passage -- this is a paraphrase,
        # not a verbatim quote, and must not be marked supported even though
        # it is tagged claim_type="quote".
        paraphrase = "the review period for the charter runs thirty days"
        model = FakeModel(
            happy_responses(
                draft=[draft_section_json(f"The charter states {paraphrase}. [S1]")],
                fact=[
                    fact_json(
                        proposition=f"The charter states {paraphrase}",
                        claim_type="quote",
                    )
                ],
            )
        )
        await self._run(model=model)

        claims = self._claims()
        self.assertEqual(len(claims), 1)
        self.assertNotEqual(claims[0]["status"], "supported")
        self.assertEqual(claims[0]["status"], "unsupported")
        rules = {row["rule_id"] for row in self._findings()}
        self.assertIn("fact.quote_mismatch", rules)


class TestQuoteFidelityNormalization(DraftFactualityTestBase):
    """SPEC 12.4's permitted normalizations, and their limits.

    "Normalize line endings and Unicode quote marks only for comparison.
    Otherwise a direct quotation must match the source text exactly, including
    omissions marked with an ellipsis."

    These previously asserted the opposite: an earlier ``validate_exact_quote``
    performed whitespace collapse only, so a curly apostrophe failed against a
    straight one and an ellipsis-marked omission never validated. Both are now
    implemented, and the old assertions are superseded.
    """

    async def test_unicode_quote_marks_fold_for_comparison(self):
        from app.services.draft_store import validate_exact_quote

        passage = "The charter's internal review window is thirty days."
        validate_exact_quote(passage, "charter\u2019s internal review window")

    async def test_curly_double_quotes_and_crlf_fold(self):
        from app.services.draft_store import validate_exact_quote

        passage = 'It said "no exceptions" applied.\r\nThe board agreed.'
        validate_exact_quote(passage, "It said \u201cno exceptions\u201d applied.")

    async def test_ellipsis_marked_omission_validates(self):
        from app.services.draft_store import validate_exact_quote

        passage = (
            "The charter's internal review window is thirty days for all amendments."
        )
        validate_exact_quote(passage, "internal review window is ... thirty days")
        validate_exact_quote(passage, "internal review window\u2026amendments.")

    async def test_a_paraphrase_still_fails(self):
        from app.services.draft_store import DraftValidationError, validate_exact_quote

        passage = "The charter's internal review window is thirty days."
        with self.assertRaises(DraftValidationError):
            validate_exact_quote(passage, "the charter allows a month for review")

    async def test_an_ellipsis_cannot_reorder_or_invent_text(self):
        """The omission rule must not become a wildcard."""
        from app.services.draft_store import DraftValidationError, validate_exact_quote

        passage = (
            "The charter's internal review window is thirty days for all amendments."
        )
        with self.assertRaises(DraftValidationError):
            validate_exact_quote(passage, "amendments.\u2026The charter's internal")
        with self.assertRaises(DraftValidationError):
            validate_exact_quote(passage, "internal review window\u2026was abolished.")
        with self.assertRaises(DraftValidationError):
            validate_exact_quote(passage, "\u2026")


# ── 6. High-stakes classification may only widen, never narrow ───────────────


class TestHighStakesWidensOnly(DraftFactualityTestBase):
    async def test_a_number_bearing_claim_is_treated_high_stakes_even_when_the_model_says_false(
        self,
    ):
        model = FakeModel(
            happy_responses(
                fact=[
                    fact_claims_json(
                        [
                            {
                                "proposition": "The review window is 30 days",
                                "status": "supported",
                                "evidence_labels": ["S1"],
                                "high_stakes": False,  # model narrows; must not stick
                            }
                        ]
                    )
                ]
            )
        )
        await self._run(model=model)

        # A single supporting source on a claim the code independently
        # classifies high-stakes (contains a number) raises the single-source
        # warning even though the model itself said high_stakes=False.
        rules = {row["rule_id"] for row in self._findings()}
        self.assertIn("fact.single_source_high_stakes", rules)


# ── 7. Unresolved findings persist without mutating prose ────────────────────


class TestUnresolvedFindingsPersist(DraftFactualityTestBase):
    async def test_an_unresolved_unsupported_claim_remains_a_finding_and_prose_is_unchanged(
        self,
    ):
        model = FakeModel(
            happy_responses(fact=[fact_json(status="unsupported", labels=())])
        )
        await self._run(model=model)

        claims = self._claims()
        self.assertEqual(claims[0]["status"], "unsupported")
        rules_and_severity = {
            (row["rule_id"], row["severity"], row["waivable"]) for row in self._findings()
        }
        self.assertIn(("fact.claim_unsupported", "blocker", 0), rules_and_severity)

        revision = self._current_revision()
        self.assertEqual(revision["content_md"], CANDIDATE)
        self.assertEqual(revision["content_sha256"], sha256_text(CANDIDATE))
        self.assertEqual(revision["fact_status"], "findings")


# ── 8. Ready-eligibility signals the Fact stage produces (SPEC 12.5) ─────────


class TestReadyEligibilitySignals(DraftFactualityTestBase):
    """The Fact stage is the ONLY compile-side producer of the findings and
    claim rows the Ready route later gates on (module ownership: the Ready
    transaction itself lives in draft_room.py, outside this file's scope).
    These tests assert the signals the Fact stage emits for each SPEC 12.5
    case, not the route's transactional enforcement.
    """

    async def test_a_fully_supported_run_has_no_blocking_findings(self):
        await self._run()  # default happy_responses: single supported claim

        blockers = [row for row in self._findings() if row["severity"] == "blocker"]
        self.assertEqual(blockers, [])
        self.assertEqual(self._current_revision()["fact_status"], "passed")

    async def test_each_unqualified_factual_status_raises_a_non_waivable_blocker(self):
        for status in ("contradicted", "unsupported", "ambiguous", "stale"):
            with self.subTest(status=status):
                model = FakeModel(
                    happy_responses(fact=[fact_json(status=status, labels=("S1",))])
                )
                await self._run(model=model)

                blockers = [
                    row
                    for row in self._findings()
                    if row["severity"] == "blocker" and row["rule_id"] == f"fact.claim_{status}"
                ]
                self.assertEqual(len(blockers), 1, status)
                self.assertEqual(blockers[0]["waivable"], 0, status)

                # Fresh compile job for the next status.
                self.job_id = self._make_compile_job()
                self.conn.execute(
                    "UPDATE drafts SET status = 'running' WHERE id = ?", (self.draft_id,)
                )
                self.conn.commit()

    async def test_single_source_high_stakes_is_a_waivable_warning_regardless_of_tier(self):
        # SPEC 12.5 rule 4 tightens this per tier at the Ready route (standard
        # = warning, high_stakes tier = waivable blocker, sensitive = non-
        # waivable unless the sole source is the primary authority). The Fact
        # stage itself (this module's scope) does not read ``tier`` at all --
        # it always raises a waivable warning-severity finding. This test
        # pins that actual, tier-invariant Fact-stage behavior; tier-specific
        # escalation is reported as a gap, not asserted here as if it existed.
        self.assertEqual(self.store.get_draft(draft_id=self.draft_id, owner_id=91001).tier, "standard")

        model = FakeModel(
            happy_responses(
                fact=[
                    fact_claims_json(
                        [
                            {
                                "proposition": "The review window is 30 days",
                                "status": "supported",
                                "evidence_labels": ["S1"],
                                "high_stakes": True,
                            }
                        ]
                    )
                ]
            )
        )
        await self._run(model=model)

        single_source = [
            row
            for row in self._findings()
            if row["rule_id"] == "fact.single_source_high_stakes"
        ]
        self.assertEqual(len(single_source), 1)
        self.assertEqual(single_source[0]["severity"], "warning")
        self.assertEqual(single_source[0]["waivable"], 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
