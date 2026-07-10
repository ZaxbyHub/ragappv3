"""Regression tests for issue #296 PR-C: backend stores (#279 + #274 + #278-be).

Each test exercises specific behavior added/fixed and would fail on the
pre-fix code:

- store_utils: shared vault_file_ids + cosine_similarity (with strict length
  guard) replace duplicated/divergent copies (F3-3, F3-5).
- memory_store: add_memory's retry unit no longer re-runs the INSERT on a
  post-commit SELECT-back failure (A5-1); dense search is numpy-vectorized
  but returns the same ordering (E1-2); the quote-guard suppresses
  attribution-verb-+"that" quoting context without suppressing legitimate
  "according to my calendar, remember to…" directives (A5-2).
- documents: a non-OOXML ZIP uploaded as .docx is rejected (B3-1).
- file_watcher: _find_new_files re-raises on DB-query failure (RES-4).
- retrieval services: KMSRetrievalService/WikiRetrievalService use the shared
  DualPoolMixin (F3-4).
"""

import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestStoreUtilsSharedHelpers(unittest.TestCase):
    """F3-3 / F3-5: shared helpers exist and behave correctly."""

    def test_cosine_strict_length_guard(self):
        from app.services.store_utils import cosine_similarity

        # Length mismatch → 0.0 (the divergence fix: the old chunking/
        # context_distiller copies silently truncated via zip()).
        self.assertEqual(cosine_similarity([1.0, 2.0, 3.0], [1.0, 2.0]), 0.0)
        # Identical unit vectors → 1.0.
        self.assertAlmostEqual(cosine_similarity([1.0, 0.0], [1.0, 0.0]), 1.0)
        # Zero vector → 0.0.
        self.assertEqual(cosine_similarity([0.0, 0.0], [1.0, 2.0]), 0.0)

    def test_vault_file_ids_empty_short_circuit(self):
        from app.services.store_utils import vault_file_ids

        db = MagicMock()
        self.assertEqual(vault_file_ids(db, 1, []), [])
        db.execute.assert_not_called()


class TestMemoryStoreAddMemoryRetryUnit(unittest.TestCase):
    """A5-1: the post-commit SELECT-back is outside the retried unit."""

    def test_select_back_failure_does_not_duplicate_insert(self):
        # Build a store against a temp DB with the memories table.
        import sqlite3

        from app.models.database import init_db, run_migrations
        from app.services.memory_store import MemoryStore

        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        try:
            conn = sqlite3.connect(tmp.name)
            init_db(tmp.name)
            run_migrations(tmp.name)
            conn.close()

            import sqlite3 as _sqlite

            class _FixedPool:
                def __init__(self, path):
                    self._path = path

                def get_connection(self):
                    return _sqlite.connect(self._path)

                def release_connection(self, c):
                    c.close()

            # Wrap the real sqlite3.Connection so the post-commit SELECT-back
            # (add_memory's second `conn.execute(...)` call) raises, while
            # the INSERT (inside the retried `_insert_and_commit`) succeeds
            # normally on the same connection.
            class _SelectFailsAfterInsertConn:
                def __init__(self, real_conn):
                    self._real = real_conn
                    self.execute_calls = []

                def execute(self, sql, params=()):
                    self.execute_calls.append(sql)
                    if sql.strip().upper().startswith("SELECT"):
                        raise _sqlite.OperationalError(
                            "simulated SELECT-back failure"
                        )
                    return self._real.execute(sql, params)

                def commit(self):
                    self._real.commit()

                def close(self):
                    self._real.close()

            wrapped = _SelectFailsAfterInsertConn(_sqlite.connect(tmp.name))

            class _FixedPoolWithWrappedConn:
                def get_connection(self):
                    return wrapped

                def release_connection(self, c):
                    pass  # wrapped connection is closed explicitly below

            store = MemoryStore(pool=_FixedPoolWithWrappedConn())

            with self.assertRaises(_sqlite.OperationalError):
                store.add_memory(content="hello world")
            wrapped.close()

            # The SELECT-back's failure must NOT have caused the INSERT to
            # be re-run: exactly one row should exist, and the INSERT sql
            # must have executed exactly once (not retried/duplicated).
            insert_calls = [s for s in wrapped.execute_calls if s.strip().upper().startswith("INSERT")]
            self.assertEqual(
                len(insert_calls), 1,
                "INSERT ran more than once — the SELECT-back failure must not "
                "re-trigger the retried INSERT unit (A5-1)",
            )

            verify_conn = _sqlite.connect(tmp.name)
            try:
                count = verify_conn.execute(
                    "SELECT COUNT(*) FROM memories WHERE content = ?", ("hello world",)
                ).fetchone()[0]
            finally:
                verify_conn.close()
            self.assertEqual(
                count, 1,
                "expected exactly one persisted row; a duplicate-insert "
                "regression would leave 2+",
            )
        finally:
            os.unlink(tmp.name)


class TestMemoryQuoteGuardAttribution(unittest.TestCase):
    """A5-2: attribution-verb-+"that" quoting context is suppressed; bare
    "according to my calendar, remember to…" is NOT (legitimate directive)."""

    def _store(self):
        from app.services.memory_store import MemoryStore

        store = MemoryStore.__new__(MemoryStore)
        store.MEMORY_PATTERNS = MemoryStore.MEMORY_PATTERNS
        store._QUOTE_GUARD_RE = MemoryStore._QUOTE_GUARD_RE
        return store

    def test_legitimate_calendar_directive_is_captured(self):
        # True positive: the user wants to remember — must NOT be suppressed.
        store = self._store()
        result = store.detect_memory_intent(
            "According to my calendar, remember to call mom"
        )
        self.assertIsNotNone(result)
        self.assertIn("call mom", result.lower())

    def test_quoting_context_with_that_clause_is_suppressed(self):
        # False positive: the directive is embedded in described report content.
        store = self._store()
        result = store.detect_memory_intent(
            "The report notes that, remember to back up these files before Q3"
        )
        self.assertIsNone(result)


class TestOOXMLValidation(unittest.TestCase):
    """B3-1: _validate_ooxml_member rejects a non-OOXML ZIP masquerading as .docx."""

    def test_validate_rejects_plain_zip_without_docx_member(self):
        import io
        import zipfile

        from app.api.routes.documents import _validate_ooxml_member

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("not-ooxml.txt", "hello")
        tmp = tempfile.NamedTemporaryFile(suffix=".docx", delete=False)
        tmp.write(buf.getvalue())
        tmp.close()
        try:
            self.assertFalse(_validate_ooxml_member(tmp.name, "word/document.xml"))
        finally:
            os.unlink(tmp.name)

    def test_validate_accepts_real_docx_structure(self):
        import io
        import zipfile

        from app.api.routes.documents import _validate_ooxml_member

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("word/document.xml", "<doc/>")
        tmp = tempfile.NamedTemporaryFile(suffix=".docx", delete=False)
        tmp.write(buf.getvalue())
        tmp.close()
        try:
            self.assertTrue(_validate_ooxml_member(tmp.name, "word/document.xml"))
        finally:
            os.unlink(tmp.name)


class TestFileWatcherRaisesOnDbError(unittest.TestCase):
    """RES-4: _find_new_files re-raises on DB-query failure (no silent empty set)."""

    def test_db_error_propagates(self):
        from pathlib import Path

        from app.services.file_watcher import FileWatcher

        fw = FileWatcher.__new__(FileWatcher)
        fw.pool = MagicMock()
        # Simulate a DB-query failure.
        fw.pool.get_connection.side_effect = RuntimeError("pool exhausted")

        with self.assertRaises(RuntimeError):
            fw._find_new_files(Path("/tmp"))


class TestDualPoolMixinShared(unittest.TestCase):
    """F3-4: KMSRetrievalService and WikiRetrievalService use the shared mixin."""

    def test_both_services_use_dual_pool_mixin(self):
        from app.services.kms_retrieval import KMSRetrievalService
        from app.services.store_utils import DualPoolMixin
        from app.services.wiki_retrieval import WikiRetrievalService

        self.assertTrue(issubclass(KMSRetrievalService, DualPoolMixin))
        self.assertTrue(issubclass(WikiRetrievalService, DualPoolMixin))

    def test_mixin_acquire_release_both_interfaces(self):
        from app.services.store_utils import DualPoolMixin

        # Production-style pool.
        prod_pool = MagicMock()
        prod_pool.get_connection.return_value = "prod-conn"
        m = DualPoolMixin.__new__(DualPoolMixin)
        m._pool = prod_pool
        self.assertEqual(m._acquire(), "prod-conn")
        m._release("prod-conn")
        prod_pool.release_connection.assert_called_once_with("prod-conn")

        # Test-style pool (get/put) — use a real object so hasattr() reflects
        # the actual interface (MagicMock auto-creates get_connection).
        class _QueuePool:
            def __init__(self):
                self.put = MagicMock()
                self._conn = "test-conn"

            def get(self):
                return self._conn

        test_pool = _QueuePool()
        m2 = DualPoolMixin.__new__(DualPoolMixin)
        m2._pool = test_pool
        self.assertEqual(m2._acquire(), "test-conn")
        m2._release("test-conn")
        test_pool.put.assert_called_once_with("test-conn")


class TestDenseSearchDropsRaggedVectors(unittest.TestCase):
    """E1-2 robustness: a mixed-dimension DB row is dropped, not raised on.

    Guards against the regression where np.asarray(..., dtype=float64) would
    raise ValueError on an inhomogeneous (ragged) candidate list.
    """

    def test_dense_search_skips_mismatched_dimension_row(self):
        import sqlite3
        import tempfile

        import numpy as np

        from app.models.database import init_db, run_migrations
        from app.services.memory_store import MemoryStore

        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        try:
            init_db(tmp.name)
            run_migrations(tmp.name)

            class _FixedPool:
                def __init__(self, path):
                    self._path = path

                def get_connection(self):
                    c = sqlite3.connect(self._path)
                    c.row_factory = sqlite3.Row
                    return c

                def release_connection(self, c):
                    c.close()

            store = MemoryStore(pool=_FixedPool(tmp.name))
            # Insert two memories with GOOD 3-dim embeddings, plus one with a
            # MISMATCHED 4-dim embedding directly into the DB.
            rec1 = store.add_memory("good one", vault_id=1)
            rec2 = store.add_memory("good two", vault_id=1)
            store._store_embedding(rec1.id, [1.0, 0.0, 0.0])
            store._store_embedding(rec2.id, [0.9, 0.1, 0.0])
            conn = store.pool.get_connection()
            try:
                # Insert a stale 4-dim row that should be dropped, not crash.
                conn.execute(
                    "UPDATE memories SET embedding=? WHERE id=?",
                    (str([0.1, 0.2, 0.3, 0.4]), rec2.id),
                )
                conn.commit()
            finally:
                store.pool.release_connection(conn)

            from unittest.mock import patch

            with patch("app.services.memory_store.settings") as mock_settings:
                mock_settings.memory_relevance_filter_enabled = False
                mock_settings.memory_dense_min_similarity = 0.0
                mock_settings.memory_dense_max_candidates = 1000
                # Must NOT raise; the 4-dim row is dropped, rec1 (3-dim) survives.
                results = store._dense_search(
                    query_embedding=[1.0, 0.0, 0.0],
                    limit=10,
                    vault_id=1,
                )
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].id, rec1.id)
        finally:
            os.unlink(tmp.name)


class TestBatchMemoryWikiStatusBody(unittest.TestCase):
    """UI-PERF-4: the batch endpoint reads memory_ids from the JSON body."""

    def test_endpoint_signature_reads_body(self):
        import ast

        source = open(
            os.path.join(os.path.dirname(__file__), "..", "app", "api", "routes", "wiki.py"),
            encoding="utf-8",
        ).read()
        # The endpoint must use Body (not Query) for memory_ids so the array
        # survives axios serialization.
        idx = source.find("async def batch_memory_wiki_status")
        self.assertGreater(idx, -1)
        window = source[idx : idx + 400]
        self.assertIn("Body(", window)
        self.assertIn("memory_ids", window)
        # Must NOT use Query for memory_ids.
        self.assertNotIn("memory_ids: List[int] = Query", window)


class TestBackfillConcurrencySetting(unittest.IsolatedAsyncioTestCase):
    """backfill_missing_embeddings must bound its concurrency Semaphore with
    settings.embedding_concurrent_batches (NOT the non-existent
    embed_concurrent_batches — a typo that made the knob permanently inert,
    always falling back to the hardcoded default of 4)."""

    async def test_semaphore_uses_configured_embedding_concurrent_batches(self):
        from app.services.memory_store import MemoryStore

        store = MemoryStore.__new__(MemoryStore)
        store.embedding_service = MagicMock()
        store.embed_and_store = AsyncMock(return_value=None)
        store._has_embedding_columns = MagicMock(return_value=True)

        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = [(1, "a"), (2, "b")]
        mock_conn = MagicMock()
        mock_conn.execute.return_value = mock_cursor
        store.pool = MagicMock()
        store.pool.get_connection.return_value = mock_conn

        mock_settings = MagicMock()
        mock_settings.embedding_model = "test-model"
        mock_settings.embedding_concurrent_batches = 2

        with patch("app.services.memory_store.settings", mock_settings), \
             patch("app.services.memory_store.asyncio.Semaphore") as mock_sem:
            mock_sem.return_value = MagicMock()
            mock_sem.return_value.__aenter__ = AsyncMock(return_value=None)
            mock_sem.return_value.__aexit__ = AsyncMock(return_value=None)

            await store.backfill_missing_embeddings()

        mock_sem.assert_called_once_with(2)


if __name__ == "__main__":
    unittest.main()
