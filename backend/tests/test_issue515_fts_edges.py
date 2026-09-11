"""Issue #515 acceptance tests — PRR-011 tokenizer edge pins (#531).

Pins ``app.services.fts_query.build_fts_match_query`` edge behavior so the
shared sanitizer cannot silently regress on the shapes real search boxes
produce. The frozen ``test_issue515_ac5_ac6_store_search.py`` covers the
store-level behavior (hyphenated queries return seeded rows); these pins are
additive, at the pure-function level:

- multi-hyphen model names tokenize per segment (``Model-X-Pro`` → the three
  tokens ``model``/``x``/``pro``, each as an FTS5 prefix term);
- a LEADING hyphen is not special (``-Model-X`` → ``model``/``x``) — it must
  not be read as a NOT-operator or dropped-token case;
- CJK text survives tokenization (unicode ``\\w``), including across a
  hyphen (``東京-ノート`` → ``東京``/``ノート`` prefix terms, lowercased);
- empty and punctuation-only input yields ``""`` — the caller's contract for
  "skip the FTS leg entirely".
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.fts_query import build_fts_match_query


class BuildFtsMatchQueryEdges(unittest.TestCase):
    def test_multi_hyphen_model_name_tokenizes_per_segment(self):
        # Hyphens become spaces, lowercase, one "token*" prefix term each.
        self.assertEqual(build_fts_match_query("Model-X-Pro"), "model* x* pro*")

    def test_leading_hyphen_is_not_special(self):
        self.assertEqual(build_fts_match_query("-Model-X"), "model* x*")

    def test_cjk_with_hyphen_preserves_cjk_tokens(self):
        self.assertEqual(build_fts_match_query("東京-ノート"), "東京* ノート*")

    def test_empty_input_returns_empty_query(self):
        self.assertEqual(build_fts_match_query(""), "")

    def test_punctuation_only_input_returns_empty_query(self):
        self.assertEqual(build_fts_match_query("!?-" ), "")
        self.assertEqual(build_fts_match_query("---"), "")
        self.assertEqual(build_fts_match_query(" . , ; : ' \""), "")


if __name__ == "__main__":
    unittest.main()
