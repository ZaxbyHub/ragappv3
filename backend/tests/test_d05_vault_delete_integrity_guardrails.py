"""Issue #723 guardrails (Workstream D PR 5 of 8) — beyond the frozen checks.

Companion to test_d05_vault_delete_integrity.py (the frozen acceptance
checks). This file pins the recurrence-class eradication surfaces the frozen
checks do not reach:

  (a) worker/dispatch vault-existence guards — a job for a deleted vault
      never re-persists wiki/KMS rows (wiki query path, KMS dispatch), and a
      NULL-vault (global) reindex is never gated by the vault check;
  (b) draft_deletion.py BaseException backstops — the four restore-armed
      scopes restore on BaseException, gated on the store transaction for
      the row-delete scopes;
  (c) the full vault-scoped cleanup census — every table in
      _VAULT_SCOPED_CLEANUP_TABLES plus the cascade-covered
      wiki_claim_sources / wiki_page_files has zero rows for a deleted vault;
  (d) tombstone sidecar semantics — sidecar written at tombstone time, exact
      restore via valid_input_relpaths, pair fallback, target-exists entries
      never restored, orphan sidecars removed.
"""

import asyncio
import os
import sqlite3
import sys
import tempfile
import time as time_mod
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Route CRUD only; no CSRF enforcement is exercised (declared for the classifier).
CSRF_TEST_POLICY = "naive"

# Shared optional-dep stubs (same as test_wiki_compile_processor.py).
import types

try:
    import lancedb  # noqa: F401
except ImportError:
    sys.modules["lancedb"] = types.ModuleType("lancedb")

try:
    import pyarrow  # noqa: F401
except ImportError:
    sys.modules["pyarrow"] = types.ModuleType("pyarrow")

try:
    from unstructured.partition.auto import partition  # noqa: F401
except ImportError:
    _u = types.ModuleType("unstructured")
    _u.__path__ = []
    _u.partition = types.ModuleType("unstructured.partition")
    _u.partition.__path__ = []
    _u.partition.auto = types.ModuleType("unstructured.partition.auto")
    _u.partition.auto.__path__ = []
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

from _db_pool import SimpleConnectionPool
from fastapi.testclient import TestClient

from app.api.deps import get_current_active_user, get_db, get_vector_store
from app.config import settings
from app.main import app
from app.models.database import SQLiteConnectionPool, init_db, run_migrations
from app.security import csrf_protect

DEAD_VAULT_ID = 424242


def _count(conn, table, vault_id):
    return conn.execute(
        f"SELECT COUNT(*) FROM {table} WHERE vault_id = ?", (vault_id,)
    ).fetchone()[0]


class _PoolHarness(unittest.TestCase):
    """run_migrations + SQLiteConnectionPool + legacy lease mode pinned."""

    def setUp(self):
        self._td = tempfile.mkdtemp()
        self.db = str(Path(self._td) / "guard.db")
        run_migrations(self.db)
        self.pool = SQLiteConnectionPool(self.db, max_size=3)
        self._original_lease = settings.wiki_kms_job_lease_enabled
        settings.wiki_kms_job_lease_enabled = False

    def tearDown(self):
        settings.wiki_kms_job_lease_enabled = self._original_lease
        self.pool.close_all()
        import shutil

        shutil.rmtree(self._td, ignore_errors=True)


class TestWorkerVaultGuards(_PoolHarness):
    """(a) dispatch- and entry-level guards: no writes for a dead vault."""

    def test_wiki_query_job_for_deleted_vault_writes_nothing(self):
        from app.services.wiki_compile_processor import WikiCompileProcessor
        from app.services.wiki_store import WikiStore

        with self.pool.connection() as conn:
            job = WikiStore(conn).create_job(
                vault_id=DEAD_VAULT_ID,
                trigger_type="query",
                input_json={"assistant_answer": "Alpha relates to Beta."},
            )
        self.assertEqual(job.vault_id, DEAD_VAULT_ID)

        proc = WikiCompileProcessor(self.pool)
        result = proc._dispatch(job)

        self.assertTrue(result.get("skipped"))
        self.assertEqual(result.get("cancelled"), "vault_deleted")
        with self.pool.connection() as conn:
            for table in ("wiki_entities", "wiki_claims", "wiki_relations"):
                self.assertEqual(
                    _count(conn, table, DEAD_VAULT_ID),
                    0,
                    f"{table} rows written for a deleted vault",
                )
            status = conn.execute(
                "SELECT status FROM wiki_compile_jobs WHERE id = ?", (job.id,)
            ).fetchone()[0]
        self.assertEqual(status, "cancelled")

    def test_wiki_compiler_entry_guard_skips_query_compile(self):
        from app.services.wiki_compiler import WikiCompiler
        from app.services.wiki_store import WikiStore

        with self.pool.connection() as conn:
            compiler = WikiCompiler(conn, WikiStore(conn))
            result = compiler.compile_query_job(
                vault_id=DEAD_VAULT_ID,
                input_json={"assistant_answer": "Alpha relates to Beta."},
            )
        self.assertTrue(result.get("skipped"))

    def test_kms_dispatch_for_deleted_vault_writes_nothing(self):
        from app.services.kms_compile_processor import KMSCompileProcessor
        from app.services.kms_store import KMSStore

        with self.pool.connection() as conn:
            job = KMSStore(conn).create_job(
                vault_id=DEAD_VAULT_ID,
                trigger_type="manual",
                input_json={},
            )
        proc = KMSCompileProcessor(self.pool)
        result = proc._dispatch(job)

        self.assertTrue(result.get("skipped"))
        self.assertEqual(result.get("cancelled"), "vault_deleted")
        with self.pool.connection() as conn:
            self.assertEqual(_count(conn, "kms_entries", DEAD_VAULT_ID), 0)
            status = conn.execute(
                "SELECT status FROM kms_compile_jobs WHERE id = ?", (job.id,)
            ).fetchone()[0]
        self.assertEqual(status, "cancelled")


class TestReindexVaultGuard(unittest.IsolatedAsyncioTestCase):
    """(a) the reindex body's vault guard is NULL-aware."""

    async def asyncSetUp(self):
        self._td = tempfile.mkdtemp()
        self.db = str(Path(self._td) / "reindex.db")
        run_migrations(self.db)
        self.conn = sqlite3.connect(self.db, timeout=10, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.pool = SQLiteConnectionPool(self.db, max_size=4)
        from app.services.background_tasks import BackgroundProcessor

        self.processor = BackgroundProcessor(pool=self.pool, retry_delay=0.05)

    async def asyncTearDown(self):
        self.pool.close_all()
        self.conn.close()
        import shutil

        shutil.rmtree(self._td, ignore_errors=True)

    async def test_vault_scoped_reindex_for_deleted_vault_skips(self):
        status, result, exc = await self.processor._reindex_embed_all(1, DEAD_VAULT_ID)
        self.assertEqual(status, "completed")
        self.assertTrue(result.get("skipped"))
        self.assertEqual(result.get("reason"), "vault_deleted")
        self.assertIsNone(exc)

    async def test_null_vault_global_reindex_is_never_gated(self):
        # vault_id=None means a GLOBAL all-vaults reindex: the guard must not
        # fire. Zero files exist, so the shared body returns its pinned
        # zero-files shape — the discriminator is the ABSENCE of the guard's
        # skip keys.
        status, result, exc = await self.processor._reindex_embed_all(1, None)
        self.assertEqual(status, "completed")
        self.assertNotIn("skipped", result)
        self.assertEqual(result.get("processed"), 0)
        self.assertEqual(result.get("failed"), 0)
        self.assertIsNone(exc)


class TestDraftDeletionBaseExceptionBackstops(unittest.TestCase):
    """(b) the four restore-armed scopes in draft_deletion.py."""

    def setUp(self):
        self._td = tempfile.mkdtemp()
        self.db = str(Path(self._td) / "draft.db")
        init_db(self.db)
        run_migrations(self.db)
        self._pool = SimpleConnectionPool(self.db)
        conn = self._pool.get_connection()
        try:
            conn.execute(
                "INSERT INTO users (id, username, hashed_password, role, is_active) "
                "VALUES (1, 'owner', 'h', 'member', 1)"
            )
            conn.commit()
        finally:
            self._pool.release_connection(conn)
        self.storage_root = Path(self._td) / "draft-room"
        from app.services.draft_input_storage import DraftInputStorage

        self.storage = DraftInputStorage(self.storage_root)

    def tearDown(self):
        self._pool.close_all()
        import shutil

        shutil.rmtree(self._td, ignore_errors=True)

    class _Upload:
        def __init__(self, filename, content):
            self.filename = filename
            self._c = content
            self._o = 0

        async def read(self, size=-1):
            if size < 0:
                chunk, self._o = self._c[self._o :], len(self._c)
                return chunk
            chunk = self._c[self._o : self._o + size]
            self._o += len(chunk)
            return chunk

    def _make_draft_with_input(self, owner_id, vault_id, title="G"):
        from app.services.draft_store import DraftStore

        conn = self._pool.get_connection()
        try:
            store = DraftStore(conn)
            draft = store.create_draft(
                vault_id=vault_id,
                created_by=owner_id,
                title=title,
                mode="compose",
                tier="standard",
                brief_json="{}",
            )

            class _Upload:
                def __init__(self, filename, content):
                    self.filename = filename
                    self._c = content
                    self._o = 0

                async def read(self, size=-1):
                    if size < 0:
                        chunk, self._o = self._c[self._o :], len(self._c)
                        return chunk
                    chunk = self._c[self._o : self._o + size]
                    self._o += len(chunk)
                    return chunk

            staged = asyncio.run(
                self.storage.stage_upload(
                    _Upload("a.txt", b"manuscript bytes"),
                    allowed_extensions={".txt"},
                    max_file_bytes=1_000_000,
                )
            )
            record = store.reserve_input(
                draft_id=draft.id,
                owner_id=owner_id,
                role="reference",
                authority="unknown",
                as_of_date=None,
                original_name=staged.original_name,
                stored_name=staged.stored_name,
                extension=staged.extension,
                media_type=staged.media_type,
                size_bytes=staged.size_bytes,
                content_sha256=staged.content_sha256,
                max_inputs=100,
                max_total_input_bytes=10_000_000,
            )
            self.storage.finalize(staged, record.storage_relpath)
            conn.commit()
            return store, draft, record
        finally:
            self._pool.release_connection(conn)

    def _add_input_to_draft(self, draft_id):
        from app.services.draft_store import DraftStore

        conn = self._pool.get_connection()
        try:
            store = DraftStore(conn)
            staged = asyncio.run(
                self.storage.stage_upload(
                    self._Upload("b.txt", b"second input bytes"),
                    allowed_extensions={".txt"},
                    max_file_bytes=1_000_000,
                )
            )
            record = store.reserve_input(
                draft_id=draft_id,
                owner_id=1,
                role="reference",
                authority="unknown",
                as_of_date=None,
                original_name=staged.original_name,
                stored_name=staged.stored_name,
                extension=staged.extension,
                media_type=staged.media_type,
                size_bytes=staged.size_bytes,
                content_sha256=staged.content_sha256,
                max_inputs=100,
                max_total_input_bytes=10_000_000,
            )
            self.storage.finalize(staged, record.storage_relpath)
            conn.commit()
            return record
        finally:
            self._pool.release_connection(conn)

    def test_prepare_purge_base_exception_restores_staged_tokens(self):
        from app.services.draft_deletion import DraftDeletionService
        from app.services.draft_store import DraftStore

        conn = self._pool.get_connection()
        try:
            conn.execute("INSERT INTO vaults (id, name) VALUES (9001, 'G')")
            conn.commit()
        finally:
            self._pool.release_connection(conn)
        _s1, _d1, r1 = self._make_draft_with_input(1, 9001, "One")
        _s2, _d2, r2 = self._make_draft_with_input(1, 9001, "Two")
        self.assertTrue(self.storage.exists(r1.storage_relpath))
        self.assertTrue(self.storage.exists(r2.storage_relpath))

        service = DraftDeletionService(self.storage)
        real_tombstone = self.storage.tombstone

        calls = {"n": 0}

        def _tombstone_then_cancel(relpath):
            calls["n"] += 1
            token = real_tombstone(relpath)
            if calls["n"] >= 2:
                raise KeyboardInterrupt("simulated staging cancel")
            return token

        conn = self._pool.get_connection()
        try:
            with patch.object(
                self.storage, "tombstone", side_effect=_tombstone_then_cancel
            ):
                with self.assertRaises(KeyboardInterrupt):
                    service.prepare_purge_for_vault(DraftStore(conn), 9001)
        finally:
            self._pool.release_connection(conn)

        self.assertTrue(
            self.storage.exists(r1.storage_relpath),
            "the already-tombstoned token must be restored on BaseException",
        )
        self.assertFalse(
            self.storage.exists(r2.storage_relpath),
            "the interrupted staging's own tombstone stays in .trash for the "
            "startup reconcile",
        )

    def test_delete_input_base_exception_before_commit_restores(self):
        from app.services.draft_deletion import DraftDeletionService
        from app.services.draft_store import DraftStore

        conn = self._pool.get_connection()
        try:
            conn.execute("INSERT INTO vaults (id, name) VALUES (9002, 'G2')")
            conn.commit()
        finally:
            self._pool.release_connection(conn)
        _s, _d, record = self._make_draft_with_input(1, 9002)
        service = DraftDeletionService(self.storage)

        class _InterruptingStore(DraftStore):
            def delete_input_row(self, **kwargs):
                self._begin_immediate()
                raise KeyboardInterrupt("simulated pre-commit cancel")

        conn = self._pool.get_connection()
        try:
            with self.assertRaises(KeyboardInterrupt):
                service.delete_input(
                    _InterruptingStore(conn),
                    draft_id=_d.id,
                    owner_id=1,
                    input_id=record.id,
                )
            row = conn.execute(
                "SELECT COUNT(*) FROM draft_inputs WHERE id = ?", (record.id,)
            ).fetchone()[0]
        finally:
            self._pool.release_connection(conn)

        self.assertEqual(row, 1, "the row delete must be rolled back")
        self.assertTrue(
            self.storage.exists(record.storage_relpath),
            "pre-commit BaseException must restore the tombstoned bytes",
        )

    def test_delete_input_base_exception_after_commit_does_not_restore(self):
        from app.services.draft_deletion import DraftDeletionService
        from app.services.draft_store import DraftStore

        conn = self._pool.get_connection()
        try:
            conn.execute("INSERT INTO vaults (id, name) VALUES (9003, 'G3')")
            conn.commit()
        finally:
            self._pool.release_connection(conn)
        _s, _d, record = self._make_draft_with_input(1, 9003)
        service = DraftDeletionService(self.storage)

        conn = self._pool.get_connection()
        try:
            store = DraftStore(conn)
            real_delete = store.delete_input_row

            def _commit_then_interrupt(**kwargs):
                real_delete(**kwargs)
                raise KeyboardInterrupt("delivered after the internal commit")

            with patch.object(
                store, "delete_input_row", side_effect=_commit_then_interrupt
            ):
                with self.assertRaises(KeyboardInterrupt):
                    service.delete_input(
                        store, draft_id=_d.id, owner_id=1, input_id=record.id
                    )
            row = conn.execute(
                "SELECT COUNT(*) FROM draft_inputs WHERE id = ?", (record.id,)
            ).fetchone()[0]
        finally:
            self._pool.release_connection(conn)

        self.assertEqual(row, 0, "the committed row delete stays deleted")
        self.assertFalse(
            self.storage.exists(record.storage_relpath),
            "a BaseException after the store's internal commit must NOT "
            "resurrect deleted bytes",
        )

    def test_delete_draft_base_exception_in_tombstone_loop_restores(self):
        from app.services.draft_deletion import DraftDeletionService
        from app.services.draft_store import DraftStore

        conn = self._pool.get_connection()
        try:
            conn.execute("INSERT INTO vaults (id, name) VALUES (9004, 'G4')")
            conn.commit()
        finally:
            self._pool.release_connection(conn)
        store, draft, record = self._make_draft_with_input(1, 9004)
        record2 = self._add_input_to_draft(draft.id)
        service = DraftDeletionService(self.storage)
        real_tombstone = self.storage.tombstone
        calls = {"n": 0}

        def _tombstone_then_cancel(relpath):
            calls["n"] += 1
            token = real_tombstone(relpath)
            if calls["n"] >= 2:
                raise KeyboardInterrupt("simulated staging cancel")
            return token

        conn = self._pool.get_connection()
        try:
            with patch.object(
                self.storage, "tombstone", side_effect=_tombstone_then_cancel
            ):
                with self.assertRaises(KeyboardInterrupt):
                    service.delete_draft(
                        DraftStore(conn), draft_id=draft.id, owner_id=1
                    )
        finally:
            self._pool.release_connection(conn)

        self.assertTrue(
            self.storage.exists(record.storage_relpath),
            "the tombstone-loop BaseException must restore staged bytes",
        )
        self.assertFalse(
            self.storage.exists(record2.storage_relpath),
            "the interrupted staging's own tombstone stays in .trash for the "
            "startup reconcile",
        )


class TestVaultDeleteFullCleanupCensus(unittest.TestCase):
    """(c) every vault-scoped surface is empty after a vault delete."""

    def setUp(self):
        self._td = tempfile.mkdtemp()
        self._original_data_dir = settings.data_dir
        settings.data_dir = Path(self._td)
        db_path = str(Path(self._td) / "census.db")
        init_db(db_path)
        self._connection_pool = SimpleConnectionPool(db_path)
        conn = self._connection_pool.get_connection()
        conn.execute(
            "INSERT INTO users (id, username, hashed_password, role, is_active) "
            "VALUES (1, 'admin', 'abc123', 'superadmin', 1)"
        )
        conn.commit()
        self._connection_pool.release_connection(conn)

        def override_get_db():
            conn = self._connection_pool.get_connection()
            try:
                yield conn
            finally:
                self._connection_pool.release_connection(conn)

        from unittest.mock import AsyncMock, MagicMock

        self._mock_vector_store = MagicMock()
        self._mock_vector_store.delete_by_vault = AsyncMock(return_value=0)
        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_vector_store] = lambda: self._mock_vector_store
        app.dependency_overrides[get_current_active_user] = lambda: {
            "id": 1,
            "username": "admin",
            "full_name": "Admin",
            "role": "superadmin",
            "is_active": True,
            "must_change_password": False,
        }
        app.dependency_overrides[csrf_protect] = lambda: "test-csrf"
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_vector_store, None)
        app.dependency_overrides.pop(get_current_active_user, None)
        app.dependency_overrides.pop(csrf_protect, None)
        self._connection_pool.close_all()
        settings.data_dir = self._original_data_dir
        import shutil

        shutil.rmtree(self._td, ignore_errors=True)

    def test_census_all_vault_scoped_surfaces_empty_after_delete(self):
        from app.api.routes.vaults import _VAULT_SCOPED_CLEANUP_TABLES

        resp = self.client.post(
            "/api/vaults", json={"name": "Census", "description": ""}
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        vid = resp.json()["id"]

        conn = self._connection_pool.get_connection()
        try:
            claim = conn.execute(
                "INSERT INTO wiki_claims (vault_id, claim_text, source_type) "
                "VALUES (?, 'census claim', 'manual')",
                (vid,),
            ).lastrowid
            page = conn.execute(
                "INSERT INTO wiki_pages (vault_id, title, slug, page_type) "
                "VALUES (?, 'P', ?, 'manual')",
                (vid, f"census-page-{vid}"),
            ).lastrowid
            conn.execute(
                "INSERT INTO wiki_claim_sources (claim_id, source_kind) "
                "VALUES (?, 'manual')",
                (claim,),
            )
            file_id = conn.execute(
                "INSERT INTO files (file_name, file_path, status, vault_id, file_size) "
                "VALUES ('c.txt', '/tmp/c.txt', 'indexed', ?, 1)",
                (vid,),
            ).lastrowid
            conn.execute(
                "INSERT INTO wiki_page_files (vault_id, page_id, file_id) "
                "VALUES (?, ?, ?)",
                (vid, page, file_id),
            )
            conn.execute(
                "INSERT INTO wiki_compile_jobs (vault_id, trigger_type, status) "
                "VALUES (?, 'manual', 'completed')",
                (vid,),
            )
            conn.execute(
                "INSERT INTO kms_compile_jobs (vault_id, trigger_type, status) "
                "VALUES (?, 'manual', 'failed')",
                (vid,),
            )
            conn.execute(
                "INSERT INTO document_reindex_jobs (vault_id, trigger_type, status, "
                "input_json) VALUES (?, 'api', 'completed', '{}')",
                (vid,),
            )
            conn.commit()
        finally:
            self._connection_pool.release_connection(conn)

        resp = self.client.delete(f"/api/vaults/{vid}")
        self.assertEqual(resp.status_code, 200, resp.text)

        conn = self._connection_pool.get_connection()
        try:
            for table in _VAULT_SCOPED_CLEANUP_TABLES:
                self.assertEqual(
                    _count(conn, table, vid),
                    0,
                    f"census: {table} must be empty for the deleted vault",
                )
            n = conn.execute(
                "SELECT COUNT(*) FROM wiki_claim_sources cs "
                "JOIN wiki_claims c ON c.id = cs.claim_id "
                "WHERE c.vault_id = ?",
                (vid,),
            ).fetchone()[0]
            self.assertEqual(
                n, 0, "census: wiki_claim_sources must cascade away with its claim"
            )
            n = conn.execute(
                "SELECT COUNT(*) FROM wiki_page_files WHERE vault_id = ? "
                "OR page_id IN (SELECT id FROM wiki_pages WHERE vault_id = ?)",
                (vid, vid),
            ).fetchone()[0]
            self.assertEqual(
                n, 0, "census: wiki_page_files must cascade away with its parents"
            )
        finally:
            self._connection_pool.release_connection(conn)


class TestStartupReconcileExactRestore(unittest.TestCase):
    """(d) the PRODUCTION startup path derives the exact relpath set with the
    (owner_id, draft_id) tuple order of list_all_owner_draft_pairs.

    Regression: the first implementation unpacked the pair as
    (draft_id, owner_id), querying draft_inputs with OWNER ids — the exact
    restore set came back empty whenever owner != draft, so crash-window
    tombstones of live drafts were never restored and aged out (the C6
    data-loss class through the production wiring). Found by the Phase-4.5
    implementation reviewer (runtime-proven).
    """

    def setUp(self):
        self._td = tempfile.mkdtemp()
        self.db = str(Path(self._td) / "recover.db")
        init_db(self.db)
        run_migrations(self.db)
        # The real pool: _recover_on_startup uses `with pool.connection()`.
        self._pool = SQLiteConnectionPool(self.db, max_size=3)
        conn = self._pool.get_connection()
        try:
            conn.execute(
                "INSERT INTO users (id, username, hashed_password, role, is_active) "
                "VALUES (100, 'owner', 'h', 'member', 1)"
            )
            conn.execute("INSERT INTO vaults (id, name) VALUES (9100, 'R')")
            conn.commit()
        finally:
            self._pool.release_connection(conn)
        from app.services.draft_input_storage import DraftInputStorage

        self.storage = DraftInputStorage(Path(self._td) / "draft-room")

    def tearDown(self):
        self._pool.close_all()
        import shutil

        shutil.rmtree(self._td, ignore_errors=True)

    def test_recover_on_startup_restores_live_input_when_owner_differs_from_draft(self):
        import time as time_mod
        from unittest.mock import MagicMock

        from app.services.draft_job_processor import DraftJobProcessor
        from app.services.draft_store import DraftStore

        conn = self._pool.get_connection()
        try:
            store = DraftStore(conn)
            draft = store.create_draft(
                vault_id=9100,
                created_by=100,
                title="R",
                mode="compose",
                tier="standard",
                brief_json="{}",
            )
            self.assertNotEqual(draft.id, 100, "precondition: owner != draft id")

            class _Upload:
                def __init__(self, filename, content):
                    self.filename = filename
                    self._c = content
                    self._o = 0

                async def read(self, size=-1):
                    if size < 0:
                        chunk, self._o = self._c[self._o :], len(self._c)
                        return chunk
                    chunk = self._c[self._o : self._o + size]
                    self._o += len(chunk)
                    return chunk

            staged = asyncio.run(
                self.storage.stage_upload(
                    _Upload("r.txt", b"crash window bytes"),
                    allowed_extensions={".txt"},
                    max_file_bytes=1_000_000,
                )
            )
            record = store.reserve_input(
                draft_id=draft.id,
                owner_id=100,
                role="reference",
                authority="unknown",
                as_of_date=None,
                original_name=staged.original_name,
                stored_name=staged.stored_name,
                extension=staged.extension,
                media_type=staged.media_type,
                size_bytes=staged.size_bytes,
                content_sha256=staged.content_sha256,
                max_inputs=100,
                max_total_input_bytes=10_000_000,
            )
            self.storage.finalize(staged, record.storage_relpath)
            conn.commit()
            relpath = record.storage_relpath
        finally:
            self._pool.release_connection(conn)

        # Simulate the crash window: the input is tombstoned and the entry
        # ages past the reconcile threshold before the restart.
        token = self.storage.tombstone(relpath)
        trash_entry = Path(self._td) / "draft-room" / ".trash" / token
        old = time_mod.time() - 10 * 24 * 3600
        os.utime(trash_entry, (old, old))

        proc = DraftJobProcessor(self._pool, self.storage, MagicMock())
        proc._recover_on_startup()

        self.assertTrue(
            self.storage.exists(relpath),
            "the production startup path must restore a live draft's "
            "tombstoned input (exact rule via list_all_owner_draft_pairs)",
        )


class TestStartupReconcileExactRuleKillsFallback(unittest.TestCase):
    """(d) the PRODUCTION startup path uses the EXACT relpath rule — a
    tombstone whose draft_inputs row is gone (the delete_input commit-crash
    window) is NOT restored even though its (owner, draft) pair is still
    valid. Pins the exact-vs-fallback distinction end-to-end: dropping the
    valid_input_relpaths wiring (Phase-4.5 round-2 reviewer's set-drop
    mutant) makes the coarser pair fallback live and this test fail.
    """

    def setUp(self):
        self._td = tempfile.mkdtemp()
        self.db = str(Path(self._td) / "exact.db")
        init_db(self.db)
        run_migrations(self.db)
        self._pool = SQLiteConnectionPool(self.db, max_size=3)
        conn = self._pool.get_connection()
        try:
            conn.execute(
                "INSERT INTO users (id, username, hashed_password, role, is_active) "
                "VALUES (100, 'owner', 'h', 'member', 1)"
            )
            conn.execute("INSERT INTO vaults (id, name) VALUES (9101, 'E')")
            conn.commit()
        finally:
            self._pool.release_connection(conn)
        from app.services.draft_input_storage import DraftInputStorage

        self.storage = DraftInputStorage(Path(self._td) / "draft-room")

    def tearDown(self):
        self._pool.close_all()
        import shutil

        shutil.rmtree(self._td, ignore_errors=True)

    class _Upload:
        def __init__(self, filename, content):
            self.filename = filename
            self._c = content
            self._o = 0

        async def read(self, size=-1):
            if size < 0:
                chunk, self._o = self._c[self._o:], len(self._c)
                return chunk
            chunk = self._c[self._o:self._o + size]
            self._o += len(chunk)
            return chunk

    def _seed_draft_with_input(self, store, conn, title):
        draft = store.create_draft(
            vault_id=9101,
            created_by=100,
            title=title,
            mode="compose",
            tier="standard",
            brief_json="{}",
        )
        staged = asyncio.run(
            self.storage.stage_upload(
                self._Upload(f"{title}.txt", f"{title} bytes".encode()),
                allowed_extensions={".txt"},
                max_file_bytes=1_000_000,
            )
        )
        record = store.reserve_input(
            draft_id=draft.id,
            owner_id=100,
            role="reference",
            authority="unknown",
            as_of_date=None,
            original_name=staged.original_name,
            stored_name=staged.stored_name,
            extension=staged.extension,
            media_type=staged.media_type,
            size_bytes=staged.size_bytes,
            content_sha256=staged.content_sha256,
            max_inputs=100,
            max_total_input_bytes=10_000_000,
        )
        self.storage.finalize(staged, record.storage_relpath)
        conn.commit()
        return draft, record

    def test_deleted_input_row_is_not_restored_but_live_one_is(self):
        from unittest.mock import MagicMock

        from app.services.draft_job_processor import DraftJobProcessor
        from app.services.draft_store import DraftStore

        conn = self._pool.get_connection()
        try:
            store = DraftStore(conn)
            draft_a, record_a = self._seed_draft_with_input(store, conn, "live")
            draft_b, record_b = self._seed_draft_with_input(store, conn, "gone")
            self.assertNotEqual(draft_a.id, draft_b.id)

            # Crash window for input B: its row delete COMMITTED (as
            # delete_input would) but the tombstone never reached
            # commit_tombstone. Input A is tombstoned with its row intact.
            self.storage.tombstone(record_a.storage_relpath)
            self.storage.tombstone(record_b.storage_relpath)
            conn.execute(
                "DELETE FROM draft_inputs WHERE id = ?", (record_b.id,)
            )
            conn.commit()
        finally:
            self._pool.release_connection(conn)

        proc = DraftJobProcessor(self._pool, self.storage, MagicMock())
        proc._recover_on_startup()

        self.assertTrue(
            self.storage.exists(record_a.storage_relpath),
            "the live input's tombstone must be restored",
        )
        self.assertFalse(
            self.storage.exists(record_b.storage_relpath),
            "a tombstone whose draft_inputs row is gone must NOT be restored "
            "(exact rule; the pair alone is insufficient)",
        )


class TestTombstoneSidecarSemantics(unittest.TestCase):
    """(d) sidecar + exact-restore semantics of DraftInputStorage."""

    def setUp(self):
        self._td = tempfile.mkdtemp()
        from app.services.draft_input_storage import DraftInputStorage

        self.storage = DraftInputStorage(Path(self._td) / "dr")

    def tearDown(self):
        import shutil

        shutil.rmtree(self._td, ignore_errors=True)

    class _Upload:
        def __init__(self, filename, content):
            self.filename = filename
            self._c = content
            self._o = 0

        async def read(self, size=-1):
            if size < 0:
                chunk, self._o = self._c[self._o :], len(self._c)
                return chunk
            chunk = self._c[self._o : self._o + size]
            self._o += len(chunk)
            return chunk

    def _finalized(self, relpath, content=b"payload"):
        staged = asyncio.run(
            self.storage.stage_upload(
                self._Upload("note.txt", content),
                allowed_extensions={".txt"},
                max_file_bytes=1_000_000,
            )
        )
        self.storage.finalize(staged, relpath)
        return relpath

    def test_tombstone_writes_sidecar_and_refreshes_mtime(self):
        relpath = self._finalized("7/9/inputs/1/a.txt")
        before = time_mod.time()
        token = self.storage.tombstone(relpath)
        sidecar = Path(self._td) / "dr" / ".trash" / f"{token}.json"
        self.assertTrue(sidecar.exists(), "tombstone must write its sidecar")
        self.assertEqual(
            sidecar.read_text(encoding="utf-8"), '{"relpath": "7/9/inputs/1/a.txt"}'
        )
        mtime = (Path(self._td) / "dr" / ".trash" / token).stat().st_mtime
        self.assertGreaterEqual(mtime, before - 1)

    def test_exact_restore_restores_live_relpath_not_deleted_one(self):
        live = self._finalized("7/9/inputs/1/live.txt")
        dead = self._finalized("7/9/inputs/2/dead.txt")
        self.storage.tombstone(live)
        self.storage.tombstone(dead)
        counts = self.storage.reconcile(
            {(7, 9)},
            max_age_seconds=10.0,
            valid_input_relpaths={"7/9/inputs/1/live.txt"},
        )
        self.assertEqual(counts["trash_restored"], 1)
        self.assertTrue(self.storage.exists(live))
        self.assertFalse(self.storage.exists(dead))
        self.assertEqual(counts["trash_removed"], 0)

    def test_pair_fallback_restores_valid_pair_entry(self):
        relpath = self._finalized("7/9/inputs/1/a.txt")
        self.storage.tombstone(relpath)
        counts = self.storage.reconcile({(7, 9)}, max_age_seconds=10.0)
        self.assertEqual(counts["trash_restored"], 1)
        self.assertTrue(self.storage.exists(relpath))

    def test_target_exists_entry_never_restored_and_age_purged(self):
        relpath = self._finalized("7/9/inputs/1/a.txt")
        token = self.storage.tombstone(relpath)
        # Recreate the live copy (a restored duplicate scenario).
        self._finalized("7/9/inputs/1/a.txt")
        trash_entry = Path(self._td) / "dr" / ".trash" / token
        old = time_mod.time() - 10_000
        os.utime(trash_entry, (old, old))
        counts = self.storage.reconcile(
            {(7, 9)},
            max_age_seconds=10.0,
            valid_input_relpaths={relpath},
        )
        self.assertEqual(counts["trash_restored"], 0)
        self.assertEqual(counts["trash_removed"], 1)
        self.assertTrue(self.storage.exists(relpath))

    def test_orphan_sidecar_removed_and_sidecars_never_counted(self):
        self._finalized("7/9/inputs/1/a.txt")
        self.storage.tombstone("7/9/inputs/1/a.txt")
        trash_dir = Path(self._td) / "dr" / ".trash"
        orphan = trash_dir / "deadbeef.json"
        orphan.write_text('{"relpath": "1/2/inputs/1/x.txt"}', encoding="utf-8")
        counts = self.storage.reconcile(set(), max_age_seconds=10.0)
        self.assertFalse(orphan.exists(), "orphan sidecar must be removed")
        self.assertEqual(counts["trash_removed"], 0)


if __name__ == "__main__":
    unittest.main()
