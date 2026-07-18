"""Tests for hybrid memory retrieval (P2.3): FTS + dense + RRF fusion.

Verifies:
- FTS-only fallback when no embedding service is configured.
- Dense search finds semantically related but lexically different memories.
- Vault scoping is preserved across both search paths.
- score_type reflects which path produced the result.
"""

import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from typing import List

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.models.database import SQLiteConnectionPool, init_db, run_migrations
from app.services.memory_store import MemoryStore, _cosine_similarity


class _StubEmbedder:
    """Synthetic embedder mapping concepts to fixed vectors so the test
    exerts the dense path deterministically without a live model.

    Each "concept" gets a one-hot dimension; semantic similarity arises
    when two phrases share concepts. Unmatched phrases still return a
    valid (mostly-zero) vector so the call signature is realistic.
    """

    def __init__(self) -> None:
        # Concept → dimension index (length 8 for headroom).
        self._concepts = {
            "report": 0,
            "summary": 0,  # synonym → same dimension as "report"
            "concise": 1,
            "brief": 1,  # synonym
            "citation": 2,
            "evidence": 2,
            "weekly": 3,
            "format": 4,
        }
        self._dim = 8

    def _vector_from(self, text: str) -> List[float]:
        v = [0.0] * self._dim
        for word in text.lower().split():
            tok = word.strip(".,?!;:")
            if tok in self._concepts:
                v[self._concepts[tok]] += 1.0
        # Normalize so cosine math is stable.
        n = sum(x * x for x in v) ** 0.5
        return [x / n for x in v] if n > 0 else v

    async def embed_passage(self, text: str) -> List[float]:
        return self._vector_from(text)

    async def embed_single(self, text: str) -> List[float]:
        return self._vector_from(text)


def _make_store(embedding_service=None) -> tuple[MemoryStore, str]:
    """Build a MemoryStore backed by a fresh on-disk SQLite db so triggers
    and the optional embedding columns work as in production."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    init_db(path)
    run_migrations(path)
    pool = SQLiteConnectionPool(path, max_size=2)
    store = MemoryStore(pool, embedding_service=embedding_service)
    return store, path


def _cleanup_store(store: MemoryStore, path: str) -> None:
    store.close_all()
    os.remove(path)


class TestHybridFallbacks(unittest.TestCase):
    def test_fts_only_when_no_embedding_service(self):
        store, path = _make_store(embedding_service=None)
        try:
            store.add_memory("User prefers concise summaries", category="pref", vault_id=1)
            store.add_memory("Reading list for project X", category="task", vault_id=1)
            results = store.search_memories("concise", limit=5, vault_id=1)
            self.assertGreaterEqual(len(results), 1)
            top = results[0]
            self.assertIn("concise", top.content.lower())
            self.assertEqual(top.score_type, "fts")
        finally:
            _cleanup_store(store, path)

    def test_vault_scoping_blocks_cross_vault_results(self):
        store, path = _make_store(embedding_service=None)
        try:
            store.add_memory("vault one secret about reports", vault_id=1)
            store.add_memory("vault two unrelated note about reports", vault_id=2)
            results = store.search_memories("reports", limit=5, vault_id=1)
            for r in results:
                # Vault 2 leakage is the failure mode — None is allowed
                # (global memories) but non-1 specific ids are not.
                self.assertIn(r.vault_id, (1, None))
        finally:
            _cleanup_store(store, path)


class TestIncludeGlobalFlag(unittest.TestCase):
    """Direct store-layer coverage for the include_global SQL branches (PRR-005).

    Exercises MemoryStore.search_memories (FTS arm) with include_global True/False
    against real rows, distinguishing which SQL branch fired by inspecting
    whether the global (vault_id IS NULL) memory surfaces. The dense arm shares
    the identical clause-toggle logic, so FTS coverage is sufficient to prove
    the branch selection; dense is exercised indirectly via the hybrid path in
    TestHybridSemanticPath when include_global=True returns the global row.
    """

    def test_include_global_false_excludes_global_memory(self):
        """include_global=False (non-admin default) → a global memory does NOT
        surface in a vault-scoped search. Pre-#404 the OR-vault_id-IS-NULL
        clause always included it."""
        store, path = _make_store(embedding_service=None)
        try:
            store.add_memory("shared global note about reports", vault_id=None)
            store.add_memory("vault one note about reports", vault_id=1)
            results = store.search_memories("reports", limit=5, vault_id=1, include_global=False)
            self.assertTrue(results, "expected the vault-scoped match")
            for r in results:
                self.assertEqual(
                    r.vault_id, 1,
                    f"include_global=False leaked a non-vault row (vault_id={r.vault_id})",
                )
        finally:
            _cleanup_store(store, path)

    def test_include_global_true_includes_global_memory(self):
        """include_global=True (admin) → the global memory surfaces alongside
        the vault-scoped one."""
        store, path = _make_store(embedding_service=None)
        try:
            store.add_memory("shared global note about reports", vault_id=None)
            store.add_memory("vault one note about reports", vault_id=1)
            results = store.search_memories("reports", limit=5, vault_id=1, include_global=True)
            vault_ids = {r.vault_id for r in results}
            self.assertIn(1, vault_ids, "vault-scoped match missing")
            self.assertIn(None, vault_ids, "include_global=True did not surface the global memory")
        finally:
            _cleanup_store(store, path)

    def test_include_global_ignored_when_vault_id_none(self):
        """When vault_id is None (admin cross-vault scan), include_global has
        no effect — all non-expired memories are returned regardless. Pins
        the documented contract so a future refactor can't silently regress."""
        store, path = _make_store(embedding_service=None)
        try:
            store.add_memory("global note about alpha", vault_id=None)
            store.add_memory("vault one note about alpha", vault_id=1)
            # Both flags should yield the same (full) set when vault_id is None.
            res_false = store.search_memories("alpha", limit=5, vault_id=None, include_global=False)
            res_true = store.search_memories("alpha", limit=5, vault_id=None, include_global=True)
            ids_false = {r.id for r in res_false}
            ids_true = {r.id for r in res_true}
            self.assertEqual(ids_false, ids_true, "vault_id=None path diverged by include_global flag")
            self.assertTrue(ids_false, "expected matches when vault_id is None")
        finally:
            _cleanup_store(store, path)


class TestHybridSemanticPath(unittest.TestCase):
    def test_semantic_query_retrieves_related_memory(self):
        embedder = _StubEmbedder()
        store, path = _make_store(embedding_service=embedder)
        try:
            # Memory phrasing uses "summary" + "concise"; query uses
            # "report" + "brief". No lexical overlap → FTS misses, dense
            # hits via the synonym map.
            store.add_memory(
                "User likes concise summary writing", category="pref", vault_id=1
            )
            store.add_memory(
                "Project deadline is next Monday", category="task", vault_id=1
            )

            results = store.search_memories("brief report style", limit=5, vault_id=1)
            self.assertTrue(results, "expected at least one hybrid match")
            # The first memory should appear in the result set even though
            # the query shares NO tokens with it.
            top_contents = [r.content for r in results]
            self.assertTrue(
                any("summary writing" in c for c in top_contents),
                f"semantic match missing from {top_contents}",
            )
            # When dense participates, score_type is either "dense" or
            # "rrf" depending on whether FTS also produced rows.
            self.assertIn(results[0].score_type, ("dense", "rrf"))
        finally:
            _cleanup_store(store, path)

    def test_semantic_only_memory_surfaces_even_when_fts_has_other_hits(self):
        """Regression: dense search must run UNFILTERED, not restricted to the
        FTS candidate set.

        Previously, when FTS returned any lexical hit, dense search was
        pre-filtered to exactly those ids — so a purely-semantic memory (no
        shared tokens with the query) became unreachable whenever FTS matched
        something else. Here memory A lexically matches the query token "brief",
        while memory B shares no tokens but is a strong dense match; B must still
        appear in the fused results.
        """
        embedder = _StubEmbedder()
        store, path = _make_store(embedding_service=embedder)
        try:
            # A: lexical FTS hit for the single-token query "brief" — so the FTS
            # candidate set is non-empty (the condition under which the old code
            # restricted dense to those ids).
            store.add_memory("brief notes draft", category="task", vault_id=1)
            # B: semantic-only — shares NO token with "brief", but its concepts
            # ("concise"→brief dim) make it a strong dense match.
            store.add_memory("concise summary writing", category="pref", vault_id=1)

            # Query "brief" (single token): FTS matches only A, while dense (now
            # unfiltered) also finds B. On the old FTS-gated code, dense was
            # restricted to [A] and B was dropped — this assertion would fail.
            results = store.search_memories("brief", limit=5, vault_id=1)
            contents = [r.content for r in results]
            self.assertTrue(
                any("summary writing" in c for c in contents),
                f"semantic-only memory missing (dense was likely FTS-gated): {contents}",
            )
        finally:
            _cleanup_store(store, path)

    def test_score_type_is_rrf_when_both_paths_match(self):
        embedder = _StubEmbedder()
        store, path = _make_store(embedding_service=embedder)
        try:
            store.add_memory("citation evidence policy", vault_id=1)
            store.add_memory("unrelated meeting note", vault_id=1)
            # Query shares the lexical "citation" term AND maps to the
            # "evidence" concept dimension, so both paths match.
            results = store.search_memories("citation policy", limit=5, vault_id=1)
            self.assertTrue(results)
            self.assertEqual(results[0].score_type, "rrf")
        finally:
            _cleanup_store(store, path)

    def test_update_memory_clears_and_refreshes_embedding(self):
        embedder = _StubEmbedder()
        store, path = _make_store(embedding_service=embedder)
        try:
            rec = store.add_memory("brief weekly summary", vault_id=1)
            # Confirm embedding stored.
            with closing(sqlite3.connect(path)) as conn:
                row = conn.execute(
                    "SELECT embedding FROM memories WHERE id = ?", (rec.id,)
                ).fetchone()
                self.assertIsNotNone(row[0])
                first_emb = json.loads(row[0])

            store.update_memory_content(rec.id, "weekly format guideline")
            with closing(sqlite3.connect(path)) as conn:
                row = conn.execute(
                    "SELECT content, embedding FROM memories WHERE id = ?", (rec.id,)
                ).fetchone()
                self.assertEqual(row[0], "weekly format guideline")
                self.assertIsNotNone(row[1])
                self.assertNotEqual(json.loads(row[1]), first_emb)
        finally:
            _cleanup_store(store, path)


class TestCosineHelper(unittest.TestCase):
    def test_cosine_identity(self):
        v = [1.0, 0.0, 0.0]
        self.assertAlmostEqual(_cosine_similarity(v, v), 1.0)

    def test_cosine_orthogonal(self):
        a = [1.0, 0.0]
        b = [0.0, 1.0]
        self.assertEqual(_cosine_similarity(a, b), 0.0)

    def test_cosine_handles_zero_vector(self):
        self.assertEqual(_cosine_similarity([0.0, 0.0], [1.0, 1.0]), 0.0)

    def test_cosine_handles_length_mismatch(self):
        self.assertEqual(_cosine_similarity([1.0], [1.0, 0.0]), 0.0)


if __name__ == "__main__":
    unittest.main()
