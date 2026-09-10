"""Issue #515 acceptance tests — WikiCurator defects (WIKI-012/013/015).

Regression tests authored from the defect report; implementations of the
fixes do NOT exist yet. Every ``test_issue515_acN_*`` function encodes the
REQUIRED (post-fix) behavior:

  AC12 (WIKI-012) Quotes that change a NUMBER are rejected for automatic
                  activation (fuzzy partial_ratio >= 92 used to accept
                  '30 minutes' vs '300 minutes').
  AC13 (WIKI-013) The curator dedupe key is full equality: two candidates
                  with null triples + the same quote but DIFFERENT
                  claim_text must both survive.
  AC15 (WIKI-015) open_questions from the curator prompt schema are parsed,
                  exposed on the result, and retained in the summary.

Fixtures mirror backend/tests/test_wiki_curator.py: the model is faked at
the CuratorClient.propose boundary (_StubClient) so no network is touched.
"""

import asyncio
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Shared optional-dep stubs (same as test_wiki_curator.py).
try:
    import lancedb  # noqa: F401
except ImportError:
    import types
    sys.modules["lancedb"] = types.ModuleType("lancedb")

try:
    import pyarrow  # noqa: F401
except ImportError:
    import types
    sys.modules["pyarrow"] = types.ModuleType("pyarrow")

try:
    from unstructured.partition.auto import partition  # noqa: F401
except ImportError:
    import types
    _u = types.ModuleType("unstructured")
    _u.__path__ = []
    _u.partition = types.ModuleType("unstructured.partition")
    _u.partition.__path__ = []
    _u.partition.auto = types.ModuleType("unstructured.partition.auto")
    _u.partition.auto.partition = lambda *a, **k: []
    _u.chunking = types.ModuleType("unstructured.chunking")
    _u.chunking.__path__ = []
    _u.chunking.title = types.ModuleType("unstructured.chunking.title")
    _u.chunking.title.chunk_by_title = lambda *a, **k: []
    _u.documents = types.ModuleType("unstructured.documents")
    _u.documents.__path__ = []
    _u.documents.elements = types.ModuleType("unstructured.documents.elements")
    _u.documents.elements.Element = type("Element", (), {})
    sys.modules["unstructured"] = _u
    sys.modules["unstructured.partition"] = _u.partition
    sys.modules["unstructured.partition.auto"] = _u.partition.auto
    sys.modules["unstructured.chunking"] = _u.chunking
    sys.modules["unstructured.chunking.title"] = _u.chunking.title
    sys.modules["unstructured.documents"] = _u.documents
    sys.modules["unstructured.documents.elements"] = _u.documents.elements

from app.config import settings
from app.services.wiki_curator import CuratorChunk, WikiCurator, verify_quote


class _StubClient:
    """Fake CuratorClient.propose returning a canned JSON string.

    The verification logic happens after the JSON is parsed, so stubbing at
    this boundary keeps tests deterministic (mirrors test_wiki_curator.py).
    """

    def __init__(self, payload: str):
        self.payload = payload

    async def propose(self, *_args, **_kwargs):
        return self.payload


class _Issue515CuratorTestBase(unittest.TestCase):
    """Snapshot + override curator settings per test (mirrors TestWikiCurator)."""

    _FIELDS = (
        "wiki_llm_curator_enabled",
        "wiki_llm_curator_url",
        "wiki_llm_curator_model",
        "wiki_llm_curator_mode",
        "wiki_llm_curator_require_quote_match",
        "wiki_llm_curator_require_chunk_id",
        "wiki_llm_curator_max_input_chars",
    )

    def setUp(self):
        self._snap = {f: getattr(settings, f) for f in self._FIELDS}
        settings.wiki_llm_curator_enabled = True
        settings.wiki_llm_curator_url = "https://api.example.com"
        settings.wiki_llm_curator_model = "qwen-1b"
        settings.wiki_llm_curator_mode = "active_if_verified"
        settings.wiki_llm_curator_require_quote_match = True
        settings.wiki_llm_curator_require_chunk_id = True
        settings.wiki_llm_curator_max_input_chars = 6000

    def tearDown(self):
        for f, v in self._snap.items():
            setattr(settings, f, v)

    def _curator(self, payload: str) -> WikiCurator:
        return WikiCurator(client=_StubClient(payload))

    def _chunks(self, source_text: str) -> list[CuratorChunk]:
        return [
            CuratorChunk(
                chunk_id="42_0",
                source_text=source_text,
                file_id=42,
                source_label="file:42",
            )
        ]


# ---------------------------------------------------------------------------
# AC12 (WIKI-012) — changed numbers must not verify / auto-activate
# ---------------------------------------------------------------------------


class TestIssue515Ac12ChangedNumberQuotes(_Issue515CuratorTestBase):

    def test_issue515_ac12_changed_number_quote_not_verified(self):
        source = "The session lasts 30 minutes."
        changed = "The session lasts 300 minutes."

        # DISCRIMINATING: a quote that changes a number ('30' -> '300') is a
        # materially different fact. Today rapidfuzz partial_ratio scores it
        # ~94.7 >= 92, so verify_quote wrongly accepts it.
        self.assertFalse(
            verify_quote(changed, source),
            "AC12: verify_quote must reject a quote that changes a number "
            "('30 minutes' vs '300 minutes'); a fuzzy partial_ratio >= 92 must "
            "not qualify a numeric mutation as verifiable",
        )

        # PRESERVING: unchanged quote verifies.
        self.assertTrue(
            verify_quote(source, source),
            "AC12 preserving: an unchanged quote must still verify",
        )
        # PRESERVING: whitespace/case-only differences verify.
        self.assertTrue(
            verify_quote("the   SESSION lasts 30 minutes.", source),
            "AC12 preserving: whitespace/case-only differences must verify",
        )
        # PRESERVING: punctuation-only differences still verify via the
        # normalized path.
        self.assertTrue(
            verify_quote("The session, lasts 30 minutes.", source),
            "AC12 preserving: punctuation-only differences must still verify",
        )

        # Pipeline: in active_if_verified mode a candidate whose quote changes
        # a number must NOT land as an active claim (it belongs in
        # needs_review or as a rejection — never auto-activated).
        payload = json.dumps({
            "claims": [
                {
                    "claim_text": "The session lasts 30 minutes",
                    "claim_type": "fact",
                    "source_quote": "The session lasts 30 minutes.",
                    "chunk_id": "42_0",
                    "confidence": 0.9,
                },
                {
                    "claim_text": "The session lasts 300 minutes",
                    "claim_type": "fact",
                    "source_quote": "The session lasts 300 minutes.",
                    "chunk_id": "42_0",
                    "confidence": 0.9,
                },
            ]
        })
        cur = self._curator(payload)
        out = asyncio.run(
            cur.curate(
                vault_id=1,
                file_id=42,
                chunks=self._chunks(source + " Other details follow."),
            )
        )
        self.assertEqual(
            [],
            [a for a in out.accepted if "300 minutes" in a.claim_text
             and a.status == "active"],
            "AC12: a changed-number candidate must never auto-activate in "
            "active_if_verified mode",
        )
        control = [
            a for a in out.accepted
            if a.claim_text == "The session lasts 30 minutes"
        ]
        self.assertEqual(
            1, len(control),
            "AC12 control: the exact-quote candidate must still be accepted",
        )
        self.assertEqual(
            "active", control[0].status,
            "AC12 control: exact-quote candidates stay auto-activable in "
            "active_if_verified mode",
        )


# ---------------------------------------------------------------------------
# AC13 (WIKI-013) — dedupe key full equality (claim_text participates)
# ---------------------------------------------------------------------------


class TestIssue515Ac13DedupeKeyFullEquality(_Issue515CuratorTestBase):

    def test_issue515_ac13_same_quote_different_claim_text_both_survive(self):
        # Two candidates with null/empty triples and the SAME source quote but
        # DIFFERENT claim_text, plus an exact repeat of the first candidate.
        # The dedupe key is subject|predicate|obj|normalized_quote with NO
        # claim_text component, so today the second distinct candidate is
        # dropped as a "duplicate" of the first.
        payload = json.dumps({
            "claims": [
                {
                    "claim_text": "Claim alpha regarding the session timeout",
                    "claim_type": "fact",
                    "source_quote": "The session timeout",
                    "chunk_id": "42_0",
                },
                {
                    "claim_text": "Claim beta regarding the session timeout",
                    "claim_type": "fact",
                    "source_quote": "The session timeout",
                    "chunk_id": "42_0",
                },
                {
                    # Exact repeat of candidate 1 — must still be deduped.
                    "claim_text": "Claim alpha regarding the session timeout",
                    "claim_type": "fact",
                    "source_quote": "The session timeout",
                    "chunk_id": "42_0",
                },
            ]
        })
        cur = self._curator(payload)
        out = asyncio.run(
            cur.curate(
                vault_id=1,
                file_id=42,
                chunks=self._chunks(
                    "The session timeout defaults to 30 minutes in staging."
                ),
            )
        )
        texts = [a.claim_text for a in out.accepted]
        self.assertEqual(
            2, len(out.accepted),
            f"AC13: two candidates with the same quote but different claim_text "
            f"must BOTH survive (got {texts!r})",
        )
        self.assertEqual(
            1, texts.count("Claim alpha regarding the session timeout"),
            "AC13: the exact repeated candidate is still deduped to one",
        )
        self.assertEqual(
            1, texts.count("Claim beta regarding the session timeout"),
            "AC13: the distinct-claim_text candidate must not be dropped",
        )


# ---------------------------------------------------------------------------
# AC15 (WIKI-015) — open_questions retained
# ---------------------------------------------------------------------------


class TestIssue515Ac15OpenQuestions(_Issue515CuratorTestBase):

    def test_issue515_ac15_open_questions_exposed_and_retained(self):
        payload = json.dumps({
            "claims": [],
            "open_questions": [
                {"question": "What timeout?", "reason": "unclear", "source_quote": "timeout"}
            ],
        })
        cur = self._curator(payload)
        out = asyncio.run(
            cur.curate(
                vault_id=1,
                file_id=42,
                chunks=self._chunks("The timeout is not specified in the source."),
            )
        )
        self.assertTrue(
            hasattr(out, "open_questions"),
            "AC15: CuratorResult must expose the open_questions the prompt "
            "schema requests (today the field does not exist)",
        )
        self.assertEqual(1, len(out.open_questions))
        self.assertEqual("What timeout?", out.open_questions[0]["question"])

        # Retained surface: the summary (what flows into the compile result /
        # job outcome) must carry them. This is the minimal honest retention
        # assertion the prompt's own schema implies.
        summary = out.to_summary()
        self.assertIn(
            "open_questions", summary,
            "AC15: to_summary() must retain the open questions",
        )
        self.assertIn("What timeout?", json.dumps(summary))

        # Empty array -> no questions, no error.
        out_empty = asyncio.run(
            self._curator(json.dumps({"claims": [], "open_questions": []})).curate(
                vault_id=1,
                file_id=42,
                chunks=self._chunks("The timeout is not specified in the source."),
            )
        )
        self.assertEqual([], list(out_empty.open_questions))
        self.assertEqual([], out_empty.errors)

    def test_issue515_ac15_no_open_questions_no_error(self):
        """PRESERVING: a response without open_questions completes cleanly
        with zero accepted claims and zero errors (passes at base)."""
        payload = json.dumps({"claims": []})
        cur = self._curator(payload)
        out = asyncio.run(
            cur.curate(
                vault_id=1,
                file_id=42,
                chunks=self._chunks("Alice founded the company."),
            )
        )
        self.assertEqual([], out.accepted)
        self.assertEqual([], out.errors)


if __name__ == "__main__":
    unittest.main()
