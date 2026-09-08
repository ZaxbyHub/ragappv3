"""Feedback vote aggregation over DISTINCT (message, document) pairs.

Covers the acceptance behavior of check C4 (issue #511, RERANK-002):

- DEDUP: ONE rated assistant message whose sources JSON cites the same
  file_id multiple times contributes exactly ONE vote for that file. Today
  ``json_each`` fans one message out to N rows and the message counts N
  times.
- PRESERVING: distinct messages each contribute one vote; down votes are
  counted separately; rows with NULL/empty file_id stay excluded.
- Both vault_scope branches (on and off) share the semantics.
"""

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    import lancedb  # noqa: F401
except ImportError:
    import types

    sys.modules["lancedb"] = types.ModuleType("lancedb")

from app.services.feedback_reranker import FeedbackReranker

_SCHEMA = """
CREATE TABLE chat_sessions (
    id INTEGER PRIMARY KEY,
    vault_id INTEGER
);
CREATE TABLE chat_messages (
    id INTEGER PRIMARY KEY,
    session_id INTEGER,
    role TEXT,
    content TEXT,
    feedback TEXT,
    sources TEXT
);
"""


class _TempDB:
    """File-backed SQLite DB created per test (mirrors check C4 isolation)."""

    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self._tmp.name) / "feedback.db")
        conn = sqlite3.connect(self.path)
        try:
            conn.executescript(_SCHEMA)
            conn.commit()
        finally:
            conn.close()

    def insert_session(self, session_id, vault_id):
        conn = sqlite3.connect(self.path)
        try:
            conn.execute(
                "INSERT INTO chat_sessions (id, vault_id) VALUES (?, ?)",
                (session_id, vault_id),
            )
            conn.commit()
        finally:
            conn.close()

    def insert_message(self, session_id, role, feedback, sources):
        conn = sqlite3.connect(self.path)
        try:
            conn.execute(
                "INSERT INTO chat_messages (session_id, role, content, feedback, sources) "
                "VALUES (?, ?, ?, ?, ?)",
                (session_id, role, "content", feedback, json.dumps(sources)),
            )
            conn.commit()
        finally:
            conn.close()

    def close(self):
        self._tmp.cleanup()


class DistinctVotesTestBase(unittest.TestCase):
    vault_scope = True

    def setUp(self):
        self.db = _TempDB()
        self.db.insert_session(1, 7)
        self.db.insert_session(2, 8)

    def tearDown(self):
        self.db.close()

    def _reranker(self):
        return FeedbackReranker(
            db_path=self.db.path, uri=False, vault_scope=self.vault_scope
        )


class TestDistinctVotesVaultScoped(DistinctVotesTestBase):
    """vault_scope=True branch: votes aggregate per DISTINCT (message, file)."""

    vault_scope = True

    def test_one_message_citing_file_twice_counts_one_up_vote(self):
        self.db.insert_message(
            1, "assistant", "up", [{"file_id": "docA"}, {"file_id": "docA"}]
        )
        fr = self._reranker()
        score = fr.get_feedback_score("docA", 7)

        self.assertIsNotNone(score)
        self.assertEqual(score.up_votes, 1)
        self.assertEqual(score.down_votes, 0)
        self.assertEqual(score.net_positive, 1)

    def test_one_message_citing_file_twice_counts_one_down_vote(self):
        self.db.insert_message(
            1, "assistant", "down", [{"file_id": "docA"}, {"file_id": "docA"}]
        )
        fr = self._reranker()
        score = fr.get_feedback_score("docA", 7)

        self.assertIsNotNone(score)
        self.assertEqual(score.down_votes, 1)
        self.assertEqual(score.up_votes, 0)

    def test_distinct_messages_each_count_once(self):
        self.db.insert_message(1, "assistant", "up", [{"file_id": "docA"}])
        self.db.insert_message(1, "assistant", "up", [{"file_id": "docA"}])
        fr = self._reranker()
        score = fr.get_feedback_score("docA", 7)

        self.assertIsNotNone(score)
        self.assertEqual(score.up_votes, 2)
        self.assertEqual(score.down_votes, 0)

    def test_mixed_feedback_across_messages(self):
        self.db.insert_message(1, "assistant", "up", [{"file_id": "docA"}])
        self.db.insert_message(1, "assistant", "down", [{"file_id": "docA"}])
        fr = self._reranker()
        score = fr.get_feedback_score("docA", 7)

        self.assertIsNotNone(score)
        self.assertEqual(score.up_votes, 1)
        self.assertEqual(score.down_votes, 1)
        self.assertEqual(score.net_positive, 0)

    def test_one_message_citing_two_files_votes_each_once(self):
        self.db.insert_message(
            1, "assistant", "up", [{"file_id": "docA"}, {"file_id": "docB"}]
        )
        fr = self._reranker()
        score_a = fr.get_feedback_score("docA", 7)
        score_b = fr.get_feedback_score("docB", 7)

        self.assertIsNotNone(score_a)
        self.assertIsNotNone(score_b)
        self.assertEqual(score_a.up_votes, 1)
        self.assertEqual(score_b.up_votes, 1)

    def test_duplicate_citations_across_distinct_messages(self):
        """Two messages each citing docA twice -> exactly 2 votes, not 4."""
        self.db.insert_message(
            1, "assistant", "up", [{"file_id": "docA"}, {"file_id": "docA"}]
        )
        self.db.insert_message(
            1, "assistant", "up", [{"file_id": "docA"}, {"file_id": "docA"}]
        )
        fr = self._reranker()
        score = fr.get_feedback_score("docA", 7)

        self.assertIsNotNone(score)
        self.assertEqual(score.up_votes, 2)

    def test_vault_isolation_preserved_with_dedup(self):
        self.db.insert_message(
            1, "assistant", "up", [{"file_id": "docA"}, {"file_id": "docA"}]
        )
        self.db.insert_message(2, "assistant", "down", [{"file_id": "docA"}])
        fr = self._reranker()

        vault7 = fr.get_feedback_score("docA", 7)
        vault8 = fr.get_feedback_score("docA", 8)
        self.assertEqual(vault7.up_votes, 1)
        self.assertEqual(vault7.down_votes, 0)
        self.assertEqual(vault8.down_votes, 1)
        self.assertEqual(vault8.up_votes, 0)

    def test_missing_file_id_rows_excluded(self):
        self.db.insert_message(
            1,
            "assistant",
            "up",
            [{"other": "meta"}, {"file_id": None}, {"file_id": "docA"}],
        )
        fr = self._reranker()
        score = fr.get_feedback_score("docA", 7)

        self.assertIsNotNone(score)
        self.assertEqual(score.up_votes, 1)

    def test_all_rows_missing_file_id_contribute_nothing(self):
        self.db.insert_message(1, "assistant", "up", [{"other": "x"}, {"no_id": 1}])
        self.db.insert_message(1, "assistant", "up", [])
        fr = self._reranker()

        self.assertIsNone(fr.get_feedback_score("docA", 7))


class TestDistinctVotesGlobal(DistinctVotesTestBase):
    """vault_scope=False branch: same DISTINCT semantics across all vaults."""

    vault_scope = False

    def test_one_message_citing_file_twice_counts_one_vote_globally(self):
        self.db.insert_message(
            1, "assistant", "up", [{"file_id": "docA"}, {"file_id": "docA"}]
        )
        self.db.insert_message(
            2, "assistant", "up", [{"file_id": "docA"}, {"file_id": "docA"}]
        )
        fr = self._reranker()
        score = fr.get_feedback_score("docA", 7)

        self.assertIsNotNone(score)
        self.assertEqual(score.up_votes, 2, "two distinct messages, one vote each")

    def test_missing_file_id_rows_excluded_globally(self):
        self.db.insert_message(
            1, "assistant", "down", [{"other": "x"}, {"file_id": "docB"}]
        )
        fr = self._reranker()
        score = fr.get_feedback_score("docB", 7)

        self.assertIsNotNone(score)
        self.assertEqual(score.down_votes, 1)


if __name__ == "__main__":
    unittest.main()
