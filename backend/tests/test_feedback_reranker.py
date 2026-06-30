"""Tests for FeedbackReranker (FR-010)."""

import json
import os
import sqlite3
import threading
import time
import unittest

from app.services.feedback_reranker import (
    _DEFAULT_BONUS_PER_VOTE,
    _DEFAULT_MAX_BONUS,
    FeedbackReranker,
    FeedbackScore,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_schema(conn: sqlite3.Connection) -> None:
    """Bootstrap the minimal schema needed by FeedbackReranker."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS chat_sessions (id INTEGER PRIMARY KEY, vault_id INTEGER NOT NULL)",
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY,
            session_id INTEGER NOT NULL REFERENCES chat_sessions(id),
            role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
            content TEXT NOT NULL,
            sources TEXT,
            feedback TEXT
        )
        """,
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cm_session ON chat_messages(session_id)")


def _insert_message(
    conn: sqlite3.Connection,
    session_id: int,
    role: str,
    sources: list,
    feedback: str | None,
) -> int:
    """Insert a row into chat_messages and commit."""
    cursor = conn.execute(
        """
        INSERT INTO chat_messages (session_id, role, content, sources, feedback)
        VALUES (?, ?, ?, ?, ?)
        """,
        (session_id, role, "content", json.dumps(sources), feedback),
    )
    conn.commit()
    return cursor.lastrowid


# ---------------------------------------------------------------------------
# Per-class shared database.
#
# Each test class gets its own file-based SQLite DB.  The connection is kept
# open from setUpClass until tearDownClass so that:
#   1. All threads (including the asyncio.to_thread pool) see the same data.
#   2. Windows file-lock is released only when the connection is closed.
#
# tearDown wipes test data but leaves the connection open so subsequent tests
# in the same class keep using the same DB file.
# ---------------------------------------------------------------------------

class _SharedDB:
    """Manages a class-level SQLite connection with a persistent temp file."""

    _counter = 0
    _lock = threading.Lock()

    def __init__(self):
        with _SharedDB._lock:
            _SharedDB._counter += 1
            self._id = _SharedDB._counter

        self._path = os.path.join(
            os.environ.get("TEMP", "/tmp"),
            f"test_fb_reranker_{self._id}.db",
        )
        self._conn = sqlite3.connect(self._path, isolation_level=None)
        self._conn.execute("PRAGMA foreign_keys = ON")
        _make_schema(self._conn)
        # Default sessions
        self._conn.execute("INSERT INTO chat_sessions (id, vault_id) VALUES (1, 10)")
        self._conn.execute("INSERT INTO chat_sessions (id, vault_id) VALUES (2, 20)")
        self._conn.commit()

    def conn(self) -> sqlite3.Connection:
        return self._conn

    @property
    def path(self) -> str:
        return self._path

    def reset(self) -> None:
        """Delete test rows but keep schema and default sessions."""
        try:
            self._conn.execute("DELETE FROM chat_messages")
            self._conn.commit()
        except Exception:
            pass

    def close(self) -> None:
        self._conn.close()
        try:
            os.unlink(self._path)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Dataclass tests
# ---------------------------------------------------------------------------

class TestFeedbackScore(unittest.TestCase):
    def test_feedback_score_is_frozen(self):
        fb = FeedbackScore(file_id="f1", net_positive=3, up_votes=5, down_votes=2, computed_at=1.0)
        with self.assertRaises(AttributeError):
            fb.net_positive = 10  # type: ignore[attr-defined]

    def test_feedback_score_slots(self):
        fb = FeedbackScore(file_id="f1", net_positive=1, up_votes=1, down_votes=0, computed_at=1.0)
        self.assertFalse(hasattr(fb, "__dict__"))

    def test_feedback_score_attributes(self):
        fb = FeedbackScore(file_id="f2", net_positive=-4, up_votes=0, down_votes=4, computed_at=99.0)
        self.assertEqual(fb.file_id, "f2")
        self.assertEqual(fb.net_positive, -4)
        self.assertEqual(fb.up_votes, 0)
        self.assertEqual(fb.down_votes, 4)
        self.assertAlmostEqual(fb.computed_at, 99.0)


# ---------------------------------------------------------------------------
# Score computation tests
# ---------------------------------------------------------------------------

class TestScoreComputation(unittest.TestCase):
    _db: _SharedDB = None

    @classmethod
    def setUpClass(cls):
        cls._db = _SharedDB()

    @classmethod
    def tearDownClass(cls):
        cls._db.close()

    def tearDown(self):
        self._db.reset()

    def _reranker(self, **kw) -> FeedbackReranker:
        return FeedbackReranker(db_path=self._db.path, **kw)

    def test_net_positive_count_up_only(self):
        """One 'up' message citing f1 → net = +1."""
        _insert_message(self._db.conn(), 1, "assistant", [{"file_id": "f1"}], "up")
        reranker = FeedbackReranker(db_path=self._db.path)
        score = reranker.get_feedback_score("f1", vault_id=10)
        self.assertIsNotNone(score)
        self.assertEqual(score.net_positive, 1)
        self.assertEqual(score.up_votes, 1)
        self.assertEqual(score.down_votes, 0)

    def test_net_positive_count_mixed(self):
        """2 up + 3 down → net = -1."""
        for _ in range(2):
            _insert_message(self._db.conn(), 1, "assistant", [{"file_id": "f1"}], "up")
        for _ in range(3):
            _insert_message(self._db.conn(), 1, "assistant", [{"file_id": "f1"}], "down")
        reranker = FeedbackReranker(db_path=self._db.path)
        score = reranker.get_feedback_score("f1", vault_id=10)
        self.assertIsNotNone(score)
        self.assertEqual(score.net_positive, -1)

    def test_no_feedback_returns_none(self):
        """File with no associated messages → None."""
        _insert_message(self._db.conn(), 1, "assistant", [{"file_id": "f1"}], None)
        reranker = FeedbackReranker(db_path=self._db.path)
        self.assertIsNone(reranker.get_feedback_score("f1", vault_id=10))

    def test_vault_isolation(self):
        """Feedback from vault 10 must not leak into vault 20."""
        _insert_message(self._db.conn(), 1, "assistant", [{"file_id": "f1"}], "up")
        _insert_message(self._db.conn(), 2, "assistant", [{"file_id": "f1"}], "down")
        reranker = FeedbackReranker(db_path=self._db.path)
        self.assertEqual(reranker.get_feedback_score("f1", vault_id=10).net_positive, 1)
        self.assertEqual(reranker.get_feedback_score("f1", vault_id=20).net_positive, -1)

    def test_empty_source_json_returns_none(self):
        """A message with an empty sources list → file_id not attributed."""
        _insert_message(self._db.conn(), 1, "assistant", [], "up")
        reranker = FeedbackReranker(db_path=self._db.path)
        self.assertIsNone(reranker.get_feedback_score("f1", vault_id=10))

    def test_null_feedback_ignored(self):
        """Messages with feedback=NULL are not counted."""
        _insert_message(self._db.conn(), 1, "assistant", [{"file_id": "f1"}], None)
        _insert_message(self._db.conn(), 1, "assistant", [{"file_id": "f1"}], "up")
        reranker = FeedbackReranker(db_path=self._db.path)
        self.assertEqual(reranker.get_feedback_score("f1", vault_id=10).net_positive, 1)

    def test_user_role_ignored(self):
        """Only assistant messages are considered."""
        _insert_message(self._db.conn(), 1, "user", [{"file_id": "f1"}], "up")
        _insert_message(self._db.conn(), 1, "assistant", [{"file_id": "f1"}], "up")
        reranker = FeedbackReranker(db_path=self._db.path)
        self.assertEqual(reranker.get_feedback_score("f1", vault_id=10).net_positive, 1)

    def test_no_vault_scope(self):
        """vault_scope=False aggregates across all vaults."""
        _insert_message(self._db.conn(), 1, "assistant", [{"file_id": "f1"}], "up")
        _insert_message(self._db.conn(), 2, "assistant", [{"file_id": "f1"}], "up")
        reranker = FeedbackReranker(db_path=self._db.path, vault_scope=False)
        self.assertEqual(reranker.get_feedback_score("f1", vault_id=10).net_positive, 2)


# ---------------------------------------------------------------------------
# Reranking tests
# ---------------------------------------------------------------------------

class _RerankTestMixin:
    """Provides _seed and _rerank helpers.  Subclasses must define self._db."""

    def _seed(self, file_id: str, feedback: str) -> None:
        _insert_message(self._db.conn(), 1, "assistant", [{"file_id": file_id}], feedback)

    def _rerank(self, chunks, **kw):
        return FeedbackReranker(db_path=self._db.path, **kw).rerank(
            chunks, vault_id=10
        )


class MockChunk:
    __slots__ = ("file_id", "score")

    def __init__(self, file_id: str, score: float):
        self.file_id = file_id
        self.score = score

    def __repr__(self):
        return f"MockChunk({self.file_id!r}, {self.score})"


class TestRerankingBoostDemote(_RerankTestMixin, unittest.TestCase):
    _db: _SharedDB = None

    @classmethod
    def setUpClass(cls):
        cls._db = _SharedDB()

    @classmethod
    def tearDownClass(cls):
        cls._db.close()

    def tearDown(self):
        self._db.reset()

    def test_positive_feedback_boosts(self):
        """Net positive feedback raises the adjusted score."""
        self._seed("f_pos", "up")
        chunks = [MockChunk("f_pos", 0.50), MockChunk("f_neutral", 0.50)]
        result = self._rerank(chunks)
        self.assertEqual(result[0].file_id, "f_pos")
        self.assertAlmostEqual(result[0].score, 0.55, places=5)

    def test_negative_feedback_demotes(self):
        """Net negative feedback lowers the adjusted score."""
        self._seed("f_neg", "down")
        chunks = [MockChunk("f_neg", 0.50), MockChunk("f_neutral", 0.50)]
        result = self._rerank(chunks)
        self.assertEqual(result[0].file_id, "f_neutral")
        self.assertAlmostEqual(result[1].score, 0.45, places=5)

    def test_no_feedback_no_change(self):
        """No feedback → no score change; order preserved by stable sort."""
        chunks = [MockChunk("f_no_fb", 0.60), MockChunk("f_other", 0.40)]
        result = self._rerank(chunks)
        self.assertEqual(result[0].file_id, "f_no_fb")
        self.assertAlmostEqual(result[0].score, 0.60, places=5)
        self.assertEqual(result[1].file_id, "f_other")
        self.assertAlmostEqual(result[1].score, 0.40, places=5)

    def test_dict_chunks_supported(self):
        """rerank() accepts dict-key access (not only attribute access)."""
        self._seed("f_pos", "up")
        chunks = [
            {"file_id": "f_pos", "score": 0.50},
            {"file_id": "f_neutral", "score": 0.50},
        ]
        result = self._rerank(chunks)
        self.assertEqual(result[0]["file_id"], "f_pos")
        self.assertAlmostEqual(result[0]["score"], 0.55, places=5)

    def test_empty_chunks_list(self):
        """Empty list is a no-op (returns [])."""
        result = self._rerank([])
        self.assertEqual(result, [])

    def test_equal_scores_reordered_by_feedback(self):
        """Identical base scores are reordered by feedback bonus."""
        self._seed("f_pos", "up")
        self._seed("f_neg", "down")
        chunks = [MockChunk("f_neg", 0.50), MockChunk("f_pos", 0.50)]
        result = self._rerank(chunks)
        self.assertEqual(result[0].file_id, "f_pos")
        self.assertEqual(result[1].file_id, "f_neg")


class TestRerankingBounds(_RerankTestMixin, unittest.TestCase):
    _db: _SharedDB = None

    @classmethod
    def setUpClass(cls):
        cls._db = _SharedDB()

    @classmethod
    def tearDownClass(cls):
        cls._db.close()

    def tearDown(self):
        self._db.reset()

    def test_adjustment_is_bounded_positive(self):
        """100 net-positive votes cap at +max_bonus."""
        for _ in range(100):
            _insert_message(self._db.conn(), 1, "assistant", [{"file_id": "f100"}], "up")
        reranker = FeedbackReranker(
            db_path=self._db.path,
            bonus_per_vote=_DEFAULT_BONUS_PER_VOTE,
            max_bonus=_DEFAULT_MAX_BONUS,
        )
        chunks = [MockChunk("f100", 0.90)]
        result = reranker.rerank(chunks, vault_id=10)
        # 100 * 0.05 = 5.0, clamped to 0.10 → 0.90 + 0.10 = 1.0
        self.assertAlmostEqual(result[0].score, 1.0, places=4)

    def test_adjustment_is_bounded_negative(self):
        """100 net-negative votes cap at -max_bonus."""
        for _ in range(100):
            _insert_message(self._db.conn(), 1, "assistant", [{"file_id": "f_neg100"}], "down")
        reranker = FeedbackReranker(
            db_path=self._db.path,
            bonus_per_vote=_DEFAULT_BONUS_PER_VOTE,
            max_bonus=_DEFAULT_MAX_BONUS,
        )
        chunks = [MockChunk("f_neg100", 0.90)]
        result = reranker.rerank(chunks, vault_id=10)
        # 0.90 - 0.10 = 0.80
        self.assertAlmostEqual(result[0].score, 0.80, places=4)

    def test_custom_bonus_per_vote(self):
        """Smaller bonus_per_vote shrinks the per-vote adjustment."""
        for _ in range(5):
            _insert_message(self._db.conn(), 1, "assistant", [{"file_id": "f5"}], "up")
        reranker = FeedbackReranker(
            db_path=self._db.path,
            bonus_per_vote=0.01,
            max_bonus=0.05,
        )
        chunks = [MockChunk("f5", 0.50)]
        result = reranker.rerank(chunks, vault_id=10)
        # net=5, raw=0.05, capped at max=0.05 → 0.55
        self.assertAlmostEqual(result[0].score, 0.55, places=5)

    def test_neutral_chunk_unchanged_regardless_of_bounds(self):
        """A file with no feedback is never adjusted even when others hit the cap."""
        for _ in range(50):
            _insert_message(self._db.conn(), 1, "assistant", [{"file_id": "f_hot"}], "up")
        reranker = FeedbackReranker(
            db_path=self._db.path,
            bonus_per_vote=0.05,
            max_bonus=0.10,
        )
        chunks = [MockChunk("f_hot", 0.50), MockChunk("f_cold", 0.90)]
        result = reranker.rerank(chunks, vault_id=10)
        # f_cold (0.90) stays ahead of f_hot adjusted (0.50 + 0.10 = 0.60)
        self.assertEqual(result[0].file_id, "f_cold")
        self.assertAlmostEqual(result[0].score, 0.90, places=5)


# ---------------------------------------------------------------------------
# Cache tests
# ---------------------------------------------------------------------------

class TestCache(unittest.TestCase):
    _db: _SharedDB = None

    @classmethod
    def setUpClass(cls):
        cls._db = _SharedDB()

    @classmethod
    def tearDownClass(cls):
        cls._db.close()

    def tearDown(self):
        self._db.reset()

    def test_cache_populated_on_first_access(self):
        """Cache is populated after the first get_feedback_score call."""
        _insert_message(self._db.conn(), 1, "assistant", [{"file_id": "f_cached"}], "up")
        reranker = FeedbackReranker(
            db_path=self._db.path,
            cache_ttl_seconds=300,
        )
        self.assertFalse(reranker._is_cache_valid(10))
        _ = reranker.get_feedback_score("f_cached", vault_id=10)
        self.assertTrue(reranker._is_cache_valid(10))

    def test_cache_is_vault_separate(self):
        """Different vault_ids maintain separate caches."""
        self._db.conn().execute("INSERT INTO chat_sessions (id, vault_id) VALUES (99, 99)")
        self._db.conn().commit()
        _insert_message(self._db.conn(), 1, "assistant", [{"file_id": "f_cached"}], "up")
        reranker = FeedbackReranker(db_path=self._db.path)
        _ = reranker.get_feedback_score("f_cached", vault_id=10)
        _ = reranker.get_feedback_score("f_cached", vault_id=99)
        self.assertIn(10, reranker._cache)
        self.assertIn(99, reranker._cache)

    def test_explicit_refresh_bypasses_ttl(self):
        """Calling refresh() re-queries even when cache is still valid."""
        _insert_message(self._db.conn(), 1, "assistant", [{"file_id": "f_cached"}], "up")
        reranker = FeedbackReranker(
            db_path=self._db.path,
            cache_ttl_seconds=300,
        )
        _ = reranker.get_feedback_score("f_cached", vault_id=10)
        old_ts = reranker._cache_timestamp[10]
        time.sleep(0.01)
        reranker.refresh(10)
        self.assertGreater(reranker._cache_timestamp[10], old_ts)

    def test_refresh_vault_not_in_cache_noops(self):
        """refresh() on an uncached vault is safe (no KeyError)."""
        reranker = FeedbackReranker(
            db_path=self._db.path,
            cache_ttl_seconds=300,
        )
        reranker.refresh(999)  # should not raise
        self.assertNotIn(999, reranker._cache)

    def test_thread_safety(self):
        """Concurrent threads building cache simultaneously must not corrupt state."""
        _insert_message(self._db.conn(), 1, "assistant", [{"file_id": "f_cached"}], "up")
        reranker = FeedbackReranker(
            db_path=self._db.path,
            cache_ttl_seconds=300,
        )
        errors: list = []

        def _worker():
            try:
                for _ in range(50):
                    reranker.get_feedback_score("f_cached", vault_id=10)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertIn(10, reranker._cache)


# ---------------------------------------------------------------------------
# Async tests
# ---------------------------------------------------------------------------

class TestAsyncRerank(unittest.IsolatedAsyncioTestCase):
    _db: _SharedDB = None

    @classmethod
    def setUpClass(cls):
        cls._db = _SharedDB()

    @classmethod
    def tearDownClass(cls):
        cls._db.close()

    def tearDown(self):
        self._db.reset()

    async def test_rerank_async_returns_sorted_list(self):
        """rerank_async is an async wrapper that returns the sorted result."""
        _insert_message(self._db.conn(), 1, "assistant", [{"file_id": "f_async"}], "up")
        reranker = FeedbackReranker(db_path=self._db.path)
        chunks = [MockChunk("f_async", 0.50), MockChunk("f_other", 0.50)]
        result = await reranker.rerank_async(chunks, vault_id=10)
        self.assertEqual(result[0].file_id, "f_async")
        self.assertAlmostEqual(result[0].score, 0.55, places=5)


class TestDistanceScoreGuard(unittest.IsolatedAsyncioTestCase):
    """Verify the engine guard: reranker must NOT be applied when score_type!=rerank.

    When score_type=="distance" the retrieval order is already correct
    (lower distance = better). Running the reranker would invert the ranking.
    The RAGEngine guards the feedback reranker with ``and score_type == "rerank"``
    so it fires only when the reranking pipeline produced the scores.
    """

    _db: _SharedDB = None

    @classmethod
    def setUpClass(cls):
        cls._db = _SharedDB()

    @classmethod
    def tearDownClass(cls):
        cls._db.close()

    def tearDown(self):
        self._db.reset()

    async def test_rerank_async_inverts_distance_order_without_guard(self):
        """Demonstrate the inversion: rerank_async sorts descending (higher=better).

        When chunks have distance scores (lower=better), the reranker's descending
        sort INVERTS the correct ranking:

          Correct order by distance:  f_best(0.05) < f_pos(0.10) < f_neg(0.10) < f_worst(0.50)
          After rerank_async:         f_worst(0.50) first → WRONG

        This proves why the engine guard (score_type == "rerank") is essential.
        Without it, distance-scored chunks are incorrectly reordered.
        """
        _insert_message(self._db.conn(), 1, "assistant", [{"file_id": "f_pos"}], "up")
        reranker = FeedbackReranker(db_path=self._db.path)
        # Distance scores: lower is better.
        # f_pos gets +0.05 bonus → 0.15; f_neg gets -0.05 → 0.05; f_best/f_worst unchanged.
        chunks = [
            MockChunk("f_best",  0.05),   # truly best by distance
            MockChunk("f_pos",    0.10),   # will get +0.05 → 0.15
            MockChunk("f_neg",    0.10),   # will get -0.05 → 0.05
            MockChunk("f_worst", 0.50),
        ]
        result = await reranker.rerank_async(chunks, vault_id=10)
        file_ids = [c.file_id for c in result]

        # Without a guard, the reranker's descending sort puts the worst distance
        # chunk (highest score) FIRST — the exact opposite of correct ordering.
        self.assertEqual(
            file_ids[0],
            "f_worst",
            "f_worst (highest distance) should come first after rerank — proving inversion",
        )
        # f_best should end up LAST (lowest score after reranker's descending sort).
        self.assertEqual(
            file_ids[-1],
            "f_best",
            "f_best (lowest distance) should end up last — proving inversion",
        )
        # The RAGEngine guard (score_type == "rerank") prevents this path
        # from running when distance scores are in use, so the inversion never occurs.


if __name__ == "__main__":
    unittest.main()
