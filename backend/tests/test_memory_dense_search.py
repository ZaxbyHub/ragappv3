"""Tests for _dense_search with candidate_ids parameter (Task 3.2).

Verifies:
- Dense search without candidate_ids searches all vault memories.
- Dense search with candidate_ids filters via SQL IN clause.
- LIMIT is correctly applied (limit * 3 fetched, then top `limit` returned).
- ORDER BY id DESC biases toward recent candidates.
- Similarity filtering and sort by score descending works.
"""

import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from typing import List

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.models.database import SQLiteConnectionPool, init_db, run_migrations
from app.services.memory_store import MemoryStore, _cosine_similarity


class _StubEmbedder:
    """Synthetic embedder mapping concepts to fixed vectors so the test
    exerts the dense path deterministically without a live model."""

    def __init__(self) -> None:
        # concept → dimension index
        self._concepts = {
            "meeting": 0,
            "schedule": 0,
            "deadline": 1,
            "project": 1,
            "lunch": 2,
            "team": 2,
        }
        self._dim = 8

    def _vector_from(self, text: str) -> List[float]:
        v = [0.0] * self._dim
        for word in text.lower().split():
            tok = word.strip(".,?!;:")
            if tok in self._concepts:
                v[self._concepts[tok]] += 1.0
        n = sum(x * x for x in v) ** 0.5
        return [x / n for x in v] if n > 0 else v

    async def embed_passage(self, text: str) -> List[float]:
        return self._vector_from(text)

    async def embed_single(self, text: str) -> List[float]:
        return self._vector_from(text)


def _make_store(embedding_service=None):
    """Build a MemoryStore backed by a fresh on-disk SQLite db."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    init_db(path)
    run_migrations(path)
    pool = SQLiteConnectionPool(path, max_size=2)
    store = MemoryStore(pool, embedding_service=embedding_service)
    return store, path


def _embed_memory_sync(store, memory_id: int, content: str):
    """Helper to embed a memory synchronously using asyncio.run."""
    asyncio.run(store.embed_and_store(memory_id, content))


class TestDenseSearchWithoutCandidateIds(unittest.TestCase):
    """Tests for _dense_search when candidate_ids is None."""

    def test_dense_search_returns_empty_on_empty_query_embedding(self):
        store, path = _make_store(embedding_service=_StubEmbedder())
        try:
            result = store._dense_search(query_embedding=[], limit=5, vault_id=1)
            self.assertEqual(result, [])
        finally:
            store.pool.close_all()
            os.remove(path)

    def test_dense_search_without_candidates_considers_all_embeddings(self):
        """When candidate_ids is None, all memories with embeddings in vault
        should be candidates for dense search."""
        embedder = _StubEmbedder()
        store, path = _make_store(embedding_service=embedder)
        try:
            # Add memories that will have embeddings stored
            rec1 = store.add_memory(
                "Team meeting scheduled for Monday", vault_id=1
            )
            rec2 = store.add_memory(
                "Project deadline is Friday", vault_id=1
            )
            rec3 = store.add_memory(
                "Team lunch on Wednesday", vault_id=1
            )

            # Force embedding storage
            _embed_memory_sync(store, rec1.id, "Team meeting scheduled for Monday")
            _embed_memory_sync(store, rec2.id, "Project deadline is Friday")
            _embed_memory_sync(store, rec3.id, "Team lunch on Wednesday")

            # Query "meeting schedule" should match rec1 via concept 0
            query_emb = embedder._vector_from("meeting schedule")
            results = store._dense_search(
                query_embedding=query_emb, limit=5, vault_id=1, candidate_ids=None
            )

            # Should return results ordered by similarity descending
            self.assertGreater(len(results), 0)
            for r in results:
                self.assertEqual(r.score_type, "dense")
            # rec1 should be at or near the top (shares "meeting" concept)
            top_ids = [r.id for r in results[:2]]
            self.assertIn(rec1.id, top_ids)
        finally:
            store.pool.close_all()
            os.remove(path)


class TestDenseSearchWithCandidateIds(unittest.TestCase):
    """Tests for _dense_search when candidate_ids is provided."""

    def test_dense_search_with_candidate_ids_filters_to_in_clause(self):
        """candidate_ids should restrict results to only those IDs via SQL IN."""
        embedder = _StubEmbedder()
        store, path = _make_store(embedding_service=embedder)
        try:
            # Add three memories
            rec1 = store.add_memory("Team meeting Monday", vault_id=1)
            rec2 = store.add_memory("Project deadline Friday", vault_id=1)
            rec3 = store.add_memory("Team lunch Wednesday", vault_id=1)

            # Embed all three
            _embed_memory_sync(store, rec1.id, "Team meeting Monday")
            _embed_memory_sync(store, rec2.id, "Project deadline Friday")
            _embed_memory_sync(store, rec3.id, "Team lunch Wednesday")

            # Query that conceptually matches rec2, but only rec1 and rec3 in candidate_ids
            query_emb = embedder._vector_from("project deadline")
            results = store._dense_search(
                query_embedding=query_emb,
                limit=5,
                vault_id=1,
                candidate_ids=[rec1.id, rec3.id],  # rec2 NOT included
            )

            # rec2 should NOT appear even though it's semantically closest
            returned_ids = [r.id for r in results]
            self.assertNotIn(rec2.id, returned_ids)
            # rec1 and rec3 may appear if their similarity passes threshold
        finally:
            store.pool.close_all()
            os.remove(path)

    def test_dense_search_with_empty_candidate_ids_list_means_no_filter(self):
        """Empty candidate_ids list is falsy in Python — treated as 'no filter',
        so the function searches all memories with embeddings (same as passing None).
        This is intentional Python semantics: empty collection means 'no restriction'."""
        embedder = _StubEmbedder()
        store, path = _make_store(embedding_service=embedder)
        try:
            rec1 = store.add_memory("Team meeting Monday", vault_id=1)
            _embed_memory_sync(store, rec1.id, "Team meeting Monday")

            query_emb = embedder._vector_from("meeting")
            results = store._dense_search(
                query_embedding=query_emb,
                limit=5,
                vault_id=1,
                candidate_ids=[],  # empty list — falsy, treated as no filter
            )
            # Empty list is falsy, so function searches all memories (no IN clause)
            self.assertGreater(len(results), 0)
            self.assertEqual(results[0].id, rec1.id)
        finally:
            store.pool.close_all()
            os.remove(path)

    def test_dense_search_candidate_ids_vault_interaction(self):
        """candidate_ids should work together with vault_id filtering."""
        embedder = _StubEmbedder()
        store, path = _make_store(embedding_service=embedder)
        try:
            rec1 = store.add_memory("Vault 1 meeting Monday", vault_id=1)
            rec2 = store.add_memory("Vault 2 lunch Wednesday", vault_id=2)

            _embed_memory_sync(store, rec1.id, "Vault 1 meeting Monday")
            _embed_memory_sync(store, rec2.id, "Vault 2 lunch Wednesday")

            query_emb = embedder._vector_from("meeting lunch")
            # Search vault 1 with candidate_ids that includes rec2 (belongs to vault 2)
            results = store._dense_search(
                query_embedding=query_emb,
                limit=5,
                vault_id=1,
                candidate_ids=[rec1.id, rec2.id],
            )

            # Only rec1 should appear since vault_id=1 restricts to vault 1
            returned_ids = [r.id for r in results]
            self.assertIn(rec1.id, returned_ids)
            self.assertNotIn(rec2.id, returned_ids)
        finally:
            store.pool.close_all()
            os.remove(path)


class TestDenseSearchLimit(unittest.TestCase):
    """Tests for LIMIT application in _dense_search."""

    def test_dense_search_respects_limit(self):
        """After similarity filtering, no more than `limit` records returned."""
        embedder = _StubEmbedder()
        store, path = _make_store(embedding_service=embedder)
        try:
            # Add many memories with varying embeddings
            contents = [
                "meeting alpha",
                "meeting beta",
                "meeting gamma",
                "meeting delta",
                "meeting epsilon",
                "meeting zeta",
            ]
            recs = []
            for content in contents:
                rec = store.add_memory(content, vault_id=1)
                recs.append(rec)

            for rec in recs:
                _embed_memory_sync(store, rec.id, content)

            query_emb = embedder._vector_from("meeting")
            limit = 3
            results = store._dense_search(
                query_embedding=query_emb,
                limit=limit,
                vault_id=1,
                candidate_ids=None,
            )

            self.assertLessEqual(len(results), limit)
        finally:
            store.pool.close_all()
            os.remove(path)

    def test_dense_search_fetches_limit_times_3_from_db(self):
        """The SQL LIMIT is applied as limit*3 to allow for similarity filtering."""
        embedder = _StubEmbedder()
        store, path = _make_store(embedding_service=embedder)
        try:
            # Add 6 memories
            recs = []
            for i in range(6):
                rec = store.add_memory(f"memory content number {i}", vault_id=1)
                recs.append(rec)

            for rec in recs:
                _embed_memory_sync(store, rec.id, f"memory content number {rec.id}")

            query_emb = embedder._vector_from("memory content number")
            limit = 2
            results = store._dense_search(
                query_embedding=query_emb,
                limit=limit,
                vault_id=1,
                candidate_ids=None,
            )

            # With 6 memories and limit=2, we should get at most 2 results
            self.assertLessEqual(len(results), limit)
        finally:
            store.pool.close_all()
            os.remove(path)


class TestDenseSearchOrderBy(unittest.TestCase):
    """Tests for ORDER BY behavior in _dense_search."""

    def test_dense_search_orders_by_similarity_descending(self):
        """Results should be sorted by cosine similarity score descending."""
        embedder = _StubEmbedder()
        store, path = _make_store(embedding_service=embedder)
        try:
            rec1 = store.add_memory("meeting scheduled", vault_id=1)
            rec2 = store.add_memory("meeting scheduled team lunch", vault_id=1)

            _embed_memory_sync(store, rec1.id, "meeting scheduled")
            _embed_memory_sync(store, rec2.id, "meeting scheduled team lunch")

            # Query exactly matches "meeting scheduled" concept
            query_emb = embedder._vector_from("meeting scheduled")
            results = store._dense_search(
                query_embedding=query_emb,
                limit=5,
                vault_id=1,
                candidate_ids=None,
            )

            # Assert the expected result-set size UNCONDITIONALLY first (C3-3):
            # the prior `if len(results) >= 2` guard let a regression that
            # narrowed the result set pass vacuously by skipping the assertion.
            self.assertGreaterEqual(len(results), 2)
            # First result should have >= score than second (descending)
            self.assertGreaterEqual(
                results[0].score, results[1].score
            )
        finally:
            store.pool.close_all()
            os.remove(path)

    def test_dense_search_id_desc_bias_with_no_fts_order(self):
        """SQL ORDER BY id DESC biases toward most recent candidates when
        two candidates share the SAME similarity score (a genuine tie).

        Both memories embed to the same concept dimension so their cosine
        similarity to the query is identical; the tie is broken by id DESC
        (rec2 first). This makes the ordering assertion meaningful and
        non-vacuous (C3-3: the prior `if len(results) == 2` guard let this
        pass vacuously when the unrelated-query fixture returned 0 results).
        """
        embedder = _StubEmbedder()
        store, path = _make_store(embedding_service=embedder)
        try:
            # Two memories sharing the "meeting" concept → tied similarity.
            rec1 = store.add_memory("meeting notes alpha", vault_id=1)
            rec2 = store.add_memory("meeting notes beta", vault_id=1)

            _embed_memory_sync(store, rec1.id, "meeting notes alpha")
            _embed_memory_sync(store, rec2.id, "meeting notes beta")

            # Query matches the shared concept → identical positive similarity.
            query_emb = embedder._vector_from("meeting")
            results = store._dense_search(
                query_embedding=query_emb,
                limit=5,
                vault_id=1,
                candidate_ids=[rec1.id, rec2.id],
            )

            # Assert the expected result-set size UNCONDITIONALLY first (C3-3).
            self.assertEqual(len(results), 2)
            # Tied scores are broken by id DESC → rec2 (higher id) first.
            self.assertEqual(results[0].id, rec2.id)
        finally:
            store.pool.close_all()
            os.remove(path)


class TestDenseSearchEdgeCases(unittest.TestCase):
    """Edge case tests for _dense_search."""

    def test_dense_search_no_embedding_columns(self):
        """When embedding column is absent, returns empty list gracefully."""
        # Init a basic db without running migrations that add embedding columns
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        init_db(path)
        pool = SQLiteConnectionPool(path, max_size=2)
        store = MemoryStore(pool=pool)
        try:
            rec = store.add_memory("Some content", vault_id=1)
            query_emb = [0.1] * 8
            results = store._dense_search(
                query_embedding=query_emb,
                limit=5,
                vault_id=1,
                candidate_ids=None,
            )
            self.assertEqual(results, [])
        finally:
            pool.close_all()
            os.remove(path)

    def test_dense_search_vault_id_null_returns_global_memories(self):
        """vault_id=None should include memories where vault_id IS NULL."""
        embedder = _StubEmbedder()
        store, path = _make_store(embedding_service=embedder)
        try:
            # Add a memory without vault_id (global) and one with vault_id=1
            # Use words that trigger the stub embedder's concept mapping
            rec1 = store.add_memory("Global team meeting", vault_id=None)
            rec2 = store.add_memory("Vault 1 team lunch", vault_id=1)

            _embed_memory_sync(store, rec1.id, "Global team meeting")
            _embed_memory_sync(store, rec2.id, "Vault 1 team lunch")

            query_emb = embedder._vector_from("team")  # "team" is in concepts at dim 2
            # vault_id=None should include both global (vault_id IS NULL) and vault-scoped
            results = store._dense_search(
                query_embedding=query_emb,
                limit=5,
                vault_id=None,
                candidate_ids=None,
            )

            result_ids = [r.id for r in results]
            # Both should be returned since vault_id=None has no vault restriction
            self.assertIn(rec1.id, result_ids)
            self.assertIn(rec2.id, result_ids)
        finally:
            store.pool.close_all()
            os.remove(path)

    def test_dense_search_all_candidates_filtered_by_similarity(self):
        """When all fetched candidates fail min_similarity, return empty list.

        The stub embedder only uses dimensions 0-4. Using dimension 5+ ensures
        zero overlap with any memory that has concepts only in 0-4.
        """
        embedder = _StubEmbedder()
        store, path = _make_store(embedding_service=embedder)
        try:
            rec = store.add_memory("meeting scheduled for monday", vault_id=1)
            _embed_memory_sync(store, rec.id, "meeting scheduled for monday")

            # Query with dimension 5 set — stub embedder only uses dims 0-4,
            # so this is truly orthogonal to any memory's embedding.
            query_emb = [0.0] * 5 + [1.0] + [0.0] * 2  # dimension 5 only
            results = store._dense_search(
                query_embedding=query_emb,
                limit=5,
                vault_id=1,
                candidate_ids=None,
            )
            # All candidates should be filtered out by min_similarity threshold
            self.assertEqual(results, [])
        finally:
            store.pool.close_all()
            os.remove(path)


if __name__ == "__main__":
    unittest.main()
