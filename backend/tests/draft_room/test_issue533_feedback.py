"""Regression tests from the PR #533 review (swarm-pr-review run 533-run1).

These live OUTSIDE the frozen issue-517 acceptance files (which are
checksum-frozen in the trace checkpoint manifest) so they can grow freely;
they reuse that harness by import and never modify it.
"""

import hashlib
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from tests.draft_room.test_issue517_facts_acceptance import (  # noqa: E402
    DOC_SOURCE,
    FakeModel,
    FakeRetrievalResult,
    FakeRetriever,
    Issue517PipelineBase,
    fact_json,
    happy_responses,
)

_ALL_KINDS = frozenset({"document", "wiki", "kms"})

CONTRADICTION_PASSAGE = (
    "Charter amendment 7 of 2024 shortened the internal review window to ten days."
)


class ContradictingRetriever(FakeRetriever):
    """Answers the vault-facet query normally, but when the Fact desk issues
    the claim-specific retrieval query (the normalized proposition), returns a
    NEWER CONTRADICTING source — pinning that the claim retrieval the
    pipeline performs actually surfaces contradictory/newer evidence and that
    the audit records it (issue #517 DRAFT-012 / PRR-004)."""

    def __init__(self, *, proposition: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.proposition = proposition
        self.contradiction = DOC_SOURCE.__class__(
            kind="document",
            title="Charter amendment 7 (2024)",
            passage=CONTRADICTION_PASSAGE,
            score=0.83,
            content_sha256=hashlib.sha256(
                CONTRADICTION_PASSAGE.encode()
            ).hexdigest(),
            updated_at="2024-06-01T00:00:00Z",
            file_id=4243,
            chunk_uid="chunk-4243-1",
        )

    async def __call__(self, query, vault_id, *, limit, source_kinds=None):
        self.queries.append(query)
        if query.strip().rstrip(".") == self.proposition.strip().rstrip("."):
            return FakeRetrievalResult(
                status="ok",
                sources=(self.contradiction,),
                requested_kinds=_ALL_KINDS,
                successful_kinds=_ALL_KINDS,
                failed_kinds=frozenset(),
                source_only=False,
            )
        return await super().__call__(
            query, vault_id, limit=limit, source_kinds=source_kinds
        )


class TestSupportedClaimSurfacesContradictingEvidence(Issue517PipelineBase):
    """Issue-required scenario: "support plus contradictory/newer source".

    A supported factual claim's claim-specific retrieval must actually
    surface a contradicting/newer source when the vault holds one, and the
    persisted retrieval audit must record what came back — so the reviewer
    sees the tension instead of an audit that only says a query ran.
    """

    async def test_claim_retrieval_audit_records_the_contradicting_source(self):
        proposition = "The review window is 30 days"
        retriever = ContradictingRetriever(proposition=proposition)
        model = FakeModel(
            happy_responses(fact=[fact_json(proposition=proposition)])
        )
        await self._run(model=model, retriever=retriever)

        job = self._job_row()
        self.assertEqual(job["status"], "completed", "fixture: compile must succeed")
        revision = self._current_revision()
        claims = self._claims(revision["id"])
        self.assertEqual(len(claims), 1, "fixture: the claim must be recorded")

        # The claim-specific query IS the normalized proposition, and it was
        # issued (distinct from the facet/research query).
        self.assertIn(proposition, retriever.queries)
        audit = json.loads(claims[0]["retrieval_audit_json"])
        self.assertEqual(audit.get("normalized_query"), proposition)
        # The contradicting/newer source the vault returned is recorded in the
        # audit's returned labels (content hashes), so the tension is
        # inspectable from the claim ledger alone.
        self.assertIn(
            retriever.contradiction.content_sha256,
            audit.get("returned_labels", []),
            "the persisted retrieval audit must record the contradicting/"
            "newer source returned for the claim-specific query",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
