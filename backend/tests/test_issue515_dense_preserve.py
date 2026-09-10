"""Issue #515 acceptance check — dense memory search stays numpy-vectorized and correct.

AC26 (PRESERVING): pins the already-fixed behavior. Must PASS at base and stay
passing after the issue-515 fixes land.

Verified behavior (direct store test):
- Ranking by cosine similarity against the query vector.
- Zero-norm rows are scored 0 (never NaN / ZeroDivisionError) and therefore
  filtered out of the results.
- Dimension-mismatched rows are skipped, not raised on.
- Deterministic tie order: equal scores → newer id first.
- The computation goes through the vectorized helper ``_numpy_cosine_to_rows``
  (structural guardrail).
"""

import inspect
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from app.models.database import SQLiteConnectionPool, init_db, run_migrations
from app.services.memory_store import MemoryStore, _numpy_cosine_to_rows


class TestAC26DenseSearchVectorizedAndCorrect(unittest.TestCase):
    """AC26: dense search correctness + vectorized-implementation guardrail."""

    def _make_store(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        init_db(path)
        run_migrations(path)
        return MemoryStore(pool=SQLiteConnectionPool(path, max_size=2)), path

    def _set_embedding(self, store, memory_id, vec):
        conn = store.pool.get_connection()
        try:
            conn.execute(
                "UPDATE memories SET embedding = ? WHERE id = ?",
                (json.dumps(vec), memory_id),
            )
            conn.commit()
        finally:
            store.pool.release_connection(conn)

    def test_issue515_ac26_dense_search_vectorized_and_correct(self):
        store, path = self._make_store()
        try:
            # Six rows; dim-3 vectors (query space is dim 3).
            mid_1 = store.add_memory("dense row one", vault_id=1).id
            mid_2 = store.add_memory("dense row two", vault_id=1).id
            mid_3 = store.add_memory("dense row three (zero vector)", vault_id=1).id
            mid_4 = store.add_memory("dense row four (mismatched dim)", vault_id=1).id
            mid_5 = store.add_memory("dense row five", vault_id=1).id
            mid_6 = store.add_memory("dense row six (orthogonal)", vault_id=1).id

            self._set_embedding(store, mid_1, [0.5, 0.5, 0.0])          # ~0.7071
            self._set_embedding(store, mid_2, [1.0, 0.0, 0.0])          # 1.0 (tie)
            self._set_embedding(store, mid_3, [0.0, 0.0, 0.0])          # zero-norm
            self._set_embedding(store, mid_4, [1.0, 0.0, 0.0, 0.5])     # 4-dim: skip
            self._set_embedding(store, mid_5, [1.0, 0.0, 0.0])          # 1.0 (tie, newer)
            self._set_embedding(store, mid_6, [0.0, 1.0, 0.0])          # 0.0

            with patch("app.services.memory_store.settings") as mock_settings:
                mock_settings.memory_relevance_filter_enabled = False
                mock_settings.memory_dense_min_similarity = 0.0
                mock_settings.memory_dense_max_candidates = 1000
                results = store._dense_search(
                    query_embedding=[1.0, 0.0, 0.0], limit=10, vault_id=1
                )

            # Ranking + deterministic tie order (equal scores → newer id first).
            self.assertEqual(
                [r.id for r in results],
                [mid_5, mid_2, mid_1],
                f"expected cosine ranking [newer tie, older tie, ~0.707], got "
                f"{[(r.id, r.score) for r in results]}",
            )
            scores = [r.score for r in results]
            self.assertGreaterEqual(scores[0], scores[1])
            self.assertAlmostEqual(scores[0], 1.0, places=6)
            self.assertAlmostEqual(scores[2], 1.0 / (2 ** 0.5), places=6)
            for r in results:
                self.assertEqual(r.score_type, "dense")

            # Zero-norm and mismatched rows never surface.
            returned_ids = {r.id for r in results}
            self.assertNotIn(mid_3, returned_ids, "zero-norm row scored 0 must be filtered")
            self.assertNotIn(mid_4, returned_ids, "dimension-mismatched row must be skipped")
            self.assertNotIn(mid_6, returned_ids, "orthogonal row scored 0 must be filtered")

            # Structural guardrail: the dense scan is numpy-vectorized via the
            # shared helper, and the helper scores zero-norm rows as 0.0.
            self.assertTrue(callable(_numpy_cosine_to_rows))
            self.assertIn(
                "_numpy_cosine_to_rows",
                inspect.getsource(MemoryStore._dense_search),
                "_dense_search must compute similarities through "
                "_numpy_cosine_to_rows (E1-2 vectorization)",
            )
            sims = _numpy_cosine_to_rows(
                np.array([1.0, 0.0, 0.0]),
                np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]),
            )
            self.assertAlmostEqual(float(sims[0]), 0.0, places=9)
            self.assertAlmostEqual(float(sims[1]), 1.0, places=9)
            self.assertAlmostEqual(float(sims[2]), 0.0, places=9)
        finally:
            store.close_all()
            if os.path.exists(path):
                os.remove(path)


if __name__ == "__main__":
    unittest.main()
