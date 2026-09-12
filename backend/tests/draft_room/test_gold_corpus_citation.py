"""Citation-accuracy regression tests (issue #237, EVAL-004).

Uses an in-memory corpus rather than ``load_corpus()`` because the on-disk
fixture's byte-level sha256 contract is CRLF-sensitive on Windows checkouts
(a known environmental artifact; CI on Linux is unaffected).
"""

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from tests.draft_room.gold_corpus import (
    GoldCorpus,
    GoldDocument,
    score_citation_accuracy,
)

TEXT = "Alpha document text about deadlines and project schedules."


def _corpus() -> GoldCorpus:
    doc = GoldDocument(
        id="doc_alpha",
        path="doc_alpha.md",
        title="Alpha",
        role="reference",
        authority="authoritative",
        as_of_date=None,
        vault="default",
        scenario_ids=(),
        sha256="0" * 64,
        byte_length=len(TEXT),
        char_length=len(TEXT),
    )
    return GoldCorpus(
        root=Path("."),
        schema_version="test",
        rubric_version="test",
        corpus_id="test",
        documents=(doc,),
        scenarios=(),
        locked_spans=(),
        exact_quotes=(),
        near_quote_traps=(),
        propositions=(),
        contradictions=(),
        injections=(),
        reviewers={},
        texts={"doc_alpha": TEXT},
    )


class TestEmptyPassageIsInvalidCitation(unittest.TestCase):
    def setUp(self):
        self.corpus = _corpus()

    def test_empty_passage_scores_zero(self):
        result = score_citation_accuracy(self.corpus, [("doc_alpha", "")])
        self.assertEqual(result.valid_count, 0)
        self.assertEqual(result.accuracy, 0.0)

    def test_whitespace_only_passage_scores_zero(self):
        result = score_citation_accuracy(self.corpus, [("doc_alpha", "   \n\t ")])
        self.assertEqual(result.valid_count, 0)
        self.assertEqual(result.accuracy, 0.0)

    def test_real_passage_still_scores_one(self):
        result = score_citation_accuracy(self.corpus, [("doc_alpha", TEXT[:30])])
        self.assertEqual(result.valid_count, 1)
        self.assertEqual(result.accuracy, 1.0)

    def test_empty_citation_list_still_vacuously_valid(self):
        result = score_citation_accuracy(self.corpus, [])
        self.assertEqual(result.accuracy, 1.0)

    def test_mixed_empty_and_real_scores_half(self):
        result = score_citation_accuracy(
            self.corpus, [("doc_alpha", ""), ("doc_alpha", TEXT[:30])]
        )
        self.assertEqual(result.valid_count, 1)
        self.assertEqual(result.accuracy, 0.5)

    def test_unresolved_label_with_empty_passage_scores_zero(self):
        result = score_citation_accuracy(self.corpus, [("no_such_doc", "")])
        self.assertEqual(result.valid_count, 0)
        self.assertEqual(result.accuracy, 0.0)


if __name__ == "__main__":
    unittest.main()
