"""Tests for PR A: phase-aware progress + async upload route + status route.

Covers:
  - Migration adds the new columns idempotently
  - set_phase / clear_progress / set_wiki_pending touch the right columns
  - DocumentProcessor.process_existing_file skips duplicate-check + insert
  - BackgroundProcessor.enqueue(file_id=...) routes the worker to
    process_existing_file rather than process_file
  - GET /documents/{file_id}/status returns the new phase/wiki fields
  - GET /documents/{file_id}/status returns 403 cross-vault
  - POST /documents returns promptly with status="pending" and file_id
  - status enum stays in {pending, processing, indexed, error}
"""

import asyncio
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from queue import Empty, Queue
from unittest.mock import AsyncMock, MagicMock

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Stub missing optional deps (mirrors test_documents_auth.py)
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
from app.models.database import (
    SQLiteConnectionPool,
    init_db,
    migrate_add_files_processing_progress,
    run_migrations,
)
from app.services.background_tasks import BackgroundProcessor
from app.services.document_processor import DocumentProcessor
from app.services.document_progress import (
    PHASE_CHUNKING,
    PHASE_INDEXED,
    PHASE_QUEUED,
    clear_progress,
    set_phase,
    set_wiki_pending,
)


def _columns(db_path: str, table: str) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    finally:
        conn.close()


class TestProgressMigration(unittest.TestCase):
    """The migration must be idempotent and never widen files.status."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "app.db")
        init_db(self.db)

    def tearDown(self):
        if os.path.exists(self.db):
            os.remove(self.db)
        os.rmdir(self.tmp)

    def test_migration_adds_phase_columns_idempotently(self):
        # init_db already includes the columns via SCHEMA, but the
        # migration function must also be safe on existing databases.
        cols_before = _columns(self.db, "files")
        for expected in (
            "phase",
            "phase_message",
            "progress_percent",
            "processed_units",
            "total_units",
            "unit_label",
            "phase_started_at",
            "processing_started_at",
            "wiki_pending",
        ):
            self.assertIn(expected, cols_before, f"missing column {expected}")

        # Running again must not raise.
        migrate_add_files_processing_progress(self.db)
        cols_after = _columns(self.db, "files")
        self.assertEqual(cols_before, cols_after)

    def test_files_status_check_unchanged(self):
        # Inserting a value outside the canonical 4-value enum must fail.
        run_migrations(self.db)
        conn = sqlite3.connect(self.db)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO files (vault_id, file_path, file_name, file_size, status) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (1, "/tmp/x", "x", 1, "queued"),
                )
            conn.rollback()
            # And the legitimate enum values must still work.
            for v in ("pending", "processing", "indexed", "error"):
                conn.execute(
                    "INSERT INTO files (vault_id, file_path, file_name, file_size, status) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (1, f"/tmp/x_{v}", f"x_{v}", 1, v),
                )
            conn.commit()
        finally:
            conn.close()


class TestSetPhase(unittest.TestCase):
    """The phase helpers must touch only the columns explicitly named."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "app.db")
        init_db(self.db)
        self.pool = SQLiteConnectionPool(self.db, max_size=2)
        # Seed a row.
        conn = sqlite3.connect(self.db)
        try:
            cur = conn.execute(
                "INSERT INTO files (vault_id, file_path, file_name, file_size, status) "
                "VALUES (?, ?, ?, ?, 'pending')",
                (1, "/tmp/file", "file", 1),
            )
            self.file_id = cur.lastrowid
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        self.pool.close_all()
        if os.path.exists(self.db):
            os.remove(self.db)
        os.rmdir(self.tmp)

    def _row(self):
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(
                "SELECT * FROM files WHERE id = ?", (self.file_id,)
            ).fetchone()
        finally:
            conn.close()

    def test_set_phase_writes_message_and_units(self):
        set_phase(
            self.pool,
            self.file_id,
            phase=PHASE_CHUNKING,
            message="Prepared 10 chunks",
            total=10,
            processed=10,
            unit="chunks",
            percent=100.0,
        )
        row = self._row()
        self.assertEqual(row["phase"], "chunking")
        self.assertEqual(row["phase_message"], "Prepared 10 chunks")
        self.assertEqual(row["total_units"], 10)
        self.assertEqual(row["processed_units"], 10)
        self.assertEqual(row["unit_label"], "chunks")
        self.assertEqual(row["progress_percent"], 100.0)

    def test_set_phase_does_not_touch_status(self):
        set_phase(
            self.pool,
            self.file_id,
            phase=PHASE_QUEUED,
            message="Queued",
        )
        row = self._row()
        self.assertEqual(row["status"], "pending")  # unchanged

    def test_clear_progress_resets_transient_fields(self):
        set_phase(
            self.pool,
            self.file_id,
            phase=PHASE_CHUNKING,
            message="x",
            total=5,
            processed=5,
            unit="chunks",
            percent=100.0,
        )
        clear_progress(self.pool, self.file_id)
        row = self._row()
        self.assertEqual(row["phase"], PHASE_INDEXED)
        self.assertIsNone(row["phase_message"])
        self.assertIsNone(row["progress_percent"])
        self.assertIsNone(row["processed_units"])
        self.assertIsNone(row["total_units"])
        self.assertIsNone(row["unit_label"])

    def test_set_wiki_pending(self):
        set_wiki_pending(self.pool, self.file_id, True)
        self.assertEqual(self._row()["wiki_pending"], 1)
        set_wiki_pending(self.pool, self.file_id, False)
        self.assertEqual(self._row()["wiki_pending"], 0)


class TestProcessExistingFileSkipsDuplicateCheck(unittest.TestCase):
    """process_existing_file must NOT run dedup or insert a second row."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "app.db")
        init_db(self.db)
        self._original_data_dir = settings.data_dir
        settings.data_dir = Path(self.tmp)
        self.pool = SQLiteConnectionPool(self.db, max_size=2)
        # Seed vault so files FK constraint is satisfied
        conn = self.pool.get_connection()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO vaults (id, name, visibility) VALUES (1, 'Default', 'private')"
            )
            conn.commit()
        finally:
            self.pool.release_connection(conn)
        self.processor = DocumentProcessor(
            chunk_size_chars=2000, chunk_overlap_chars=200, pool=self.pool
        )
        # SQL file processing is the simplest path through process_file
        # that doesn't require external services.
        self.sql_path = os.path.join(self.tmp, "schema.sql")
        with open(self.sql_path, "w", encoding="utf-8") as f:
            f.write(
                "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT);\n"
                "CREATE TABLE posts (id INTEGER PRIMARY KEY, user_id INTEGER);\n"
            )

    def tearDown(self):
        settings.data_dir = self._original_data_dir
        self.pool.close_all()
        for p in (self.sql_path, self.db):
            if os.path.exists(p):
                os.remove(p)
        os.rmdir(self.tmp)

    def test_process_existing_file_uses_provided_row(self):
        from app.utils.file_utils import compute_file_hash

        # Simulate the route side: insert the row first.
        conn = self.pool.get_connection()
        try:
            file_id = self.processor._insert_or_get_file_record(
                self.sql_path,
                compute_file_hash(self.sql_path),
                conn,
                vault_id=1,
                source="upload",
            )
            conn.commit()
        finally:
            self.pool.release_connection(conn)

        # Sanity: only one row exists for this path before processing.
        c = sqlite3.connect(self.db)
        try:
            count_before = c.execute(
                "SELECT COUNT(*) FROM files WHERE file_path = ?", (self.sql_path,)
            ).fetchone()[0]
        finally:
            c.close()
        self.assertEqual(count_before, 1)

        # Run worker-side processing.
        result = asyncio.run(
            self.processor.process_existing_file(
                file_id=file_id, file_path=self.sql_path, vault_id=1
            )
        )
        self.assertEqual(result.file_id, file_id)

        # Still exactly one row.
        c = sqlite3.connect(self.db)
        c.row_factory = sqlite3.Row
        try:
            rows = c.execute(
                "SELECT id, status, phase, wiki_pending FROM files WHERE file_path = ?",
                (self.sql_path,),
            ).fetchall()
        finally:
            c.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], file_id)
        self.assertEqual(rows[0]["status"], "indexed")
        # Phase pinned to indexed by clear_progress at the end.
        self.assertEqual(rows[0]["phase"], "indexed")
        # Reviewer Fix #4: wiki_pending must be cleared on the happy path
        # once the wiki_compile_jobs row exists, so the row carries no
        # stale flag. (The wiki job is created by the processor; the test
        # database has wiki tables via run_migrations through init_db
        # path — if the wiki table is missing, the processor logs a
        # warning and clears the flag in the except branch. Either way
        # the flag must end at 0 on success.)
        self.assertEqual(rows[0]["wiki_pending"], 0)


class TestBackgroundProcessorRoutesByFileId(unittest.TestCase):
    """When file_id is on the task, the worker calls process_existing_file."""

    def test_enqueue_with_file_id_calls_process_existing_file(self):
        bp = BackgroundProcessor()
        bp.processor = MagicMock()
        bp.processor.process_file = AsyncMock()
        bp.processor.process_existing_file = AsyncMock()

        async def runner():
            await bp.enqueue(
                file_path="/tmp/x.txt",
                source="upload",
                vault_id=1,
                file_id=42,
            )
            # Drain one task.
            task = await bp.queue.get()
            await bp._process_task(task)
            bp.queue.task_done()

        asyncio.run(runner())
        bp.processor.process_existing_file.assert_awaited_once_with(
            file_id=42, file_path="/tmp/x.txt", vault_id=1
        )
        bp.processor.process_file.assert_not_awaited()

    def test_enqueue_without_file_id_calls_process_file(self):
        bp = BackgroundProcessor()
        bp.processor = MagicMock()
        bp.processor.process_file = AsyncMock()
        bp.processor.process_existing_file = AsyncMock()

        async def runner():
            await bp.enqueue(
                file_path="/tmp/x.txt",
                source="scan",
                vault_id=1,
            )
            task = await bp.queue.get()
            await bp._process_task(task)
            bp.queue.task_done()

        asyncio.run(runner())
        bp.processor.process_file.assert_awaited_once()
        bp.processor.process_existing_file.assert_not_awaited()


# ---------------------------------------------------------------------------
# HTTP-level tests for the route flip and the extended status endpoint.
# ---------------------------------------------------------------------------


class _SimplePool:
    def __init__(self, db_path):
        self.db_path = db_path
        self._pool = Queue(maxsize=5)
        self._lock = threading.Lock()
        self._closed = False

    def get_connection(self):
        if self._closed:
            raise RuntimeError("Pool closed")
        try:
            return self._pool.get_nowait()
        except Empty:
            return self._create_connection()

    async def get_connection_async(self, max_wait_attempts: int = 3):
        # #592: converted routes check out through the pool's async surface;
        # delegate to the sync checkout on a worker thread to match it.
        return await asyncio.to_thread(self.get_connection)

    def _create_connection(self):
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn

    def release_connection(self, conn):
        if not self._closed:
            try:
                self._pool.put_nowait(conn)
            except Exception:
                conn.close()

    # Context-manager helper for set_phase
    from contextlib import contextmanager

    @contextmanager
    def connection(self):
        conn = self.get_connection()
        try:
            yield conn
        finally:
            self.release_connection(conn)

    def close_all(self):
        self._closed = True
        while True:
            try:
                conn = self._pool.get_nowait()
                conn.close()
            except Empty:
                break


class TestStatusRouteAndAsyncUpload(unittest.TestCase):
    """Route-level tests for PR A async upload + extended status endpoint."""

    def setUp(self):
        from fastapi.testclient import TestClient

        from app.api.deps import (
            get_background_processor,
            get_db,
            get_db_pool,
            get_embedding_service,
            get_vector_store,
        )
        from app.main import app
        from app.services.auth_service import create_access_token, hash_password

        self.app = app
        self.create_token = create_access_token
        self.hash_password = hash_password
        self.client = TestClient(app)

        self.tmp = tempfile.mkdtemp()
        self._original_data_dir = settings.data_dir
        self._original_users_enabled = settings.users_enabled
        self._original_jwt = settings.jwt_secret_key
        settings.data_dir = Path(self.tmp)
        settings.users_enabled = True
        settings.jwt_secret_key = "test-secret-key-for-testing-at-least-32-chars-long"

        self.db = str(Path(self.tmp) / "app.db")

        # Reset pool cache.
        from app.models.database import _pool_cache, _pool_cache_lock

        with _pool_cache_lock:
            for _, p in list(_pool_cache.items()):
                p.close_all()
            _pool_cache.clear()

        run_migrations(self.db)
        self.pool = _SimplePool(self.db)

        self._mock_vec = MagicMock()
        self._mock_emb = MagicMock()
        self._mock_bp = MagicMock()
        self._mock_bp.is_running = True
        self._mock_bp.enqueue = AsyncMock()

        def override_db():
            conn = self.pool.get_connection()
            try:
                yield conn
            finally:
                self.pool.release_connection(conn)

        app.dependency_overrides[get_db] = override_db
        app.dependency_overrides[get_db_pool] = lambda: self.pool
        app.dependency_overrides[get_vector_store] = lambda: self._mock_vec
        app.dependency_overrides[get_embedding_service] = lambda: self._mock_emb
        app.dependency_overrides[get_background_processor] = lambda: self._mock_bp

        # Seed two vaults + two users.
        conn = self.pool.get_connection()
        try:
            conn.execute("DELETE FROM files")
            conn.execute("DELETE FROM vault_members")
            conn.execute("DELETE FROM users WHERE id != 0")
            pw = self.hash_password("pw")
            conn.execute(
                "INSERT INTO users (id, username, hashed_password, full_name, role, is_active) "
                "VALUES (1, 'sa', ?, 'SA', 'superadmin', 1)",
                (pw,),
            )
            conn.execute(
                "INSERT INTO users (id, username, hashed_password, full_name, role, is_active) "
                "VALUES (2, 'm1', ?, 'M1', 'member', 1)",
                (pw,),
            )
            conn.execute(
                "INSERT INTO users (id, username, hashed_password, full_name, role, is_active) "
                "VALUES (3, 'm2', ?, 'M2', 'member', 1)",
                (pw,),
            )
            conn.execute(
                "INSERT OR IGNORE INTO vaults (id, name, description) VALUES (2, 'V2', '')"
            )
            conn.execute(
                "INSERT OR IGNORE INTO vaults (id, name, description) VALUES (3, 'V3', '')"
            )
            conn.execute(
                "INSERT INTO vault_members (vault_id, user_id, permission, granted_by) "
                "VALUES (2, 2, 'write', 1)"
            )
            conn.execute(
                "INSERT INTO vault_members (vault_id, user_id, permission, granted_by) "
                "VALUES (3, 3, 'read', 1)"
            )
            conn.commit()
        finally:
            self.pool.release_connection(conn)

    def tearDown(self):
        from app.models.database import _pool_cache, _pool_cache_lock

        self.app.dependency_overrides.clear()
        with _pool_cache_lock:
            for _, p in list(_pool_cache.items()):
                p.close_all()
            _pool_cache.clear()
        self.pool.close_all()
        settings.data_dir = self._original_data_dir
        settings.users_enabled = self._original_users_enabled
        settings.jwt_secret_key = self._original_jwt
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _token(self, uid, name, role):
        return self.create_token(uid, name, role)

    def test_status_route_returns_new_phase_fields(self):
        # Insert a row in vault 2 with phase fields populated.
        conn = self.pool.get_connection()
        try:
            cur = conn.execute(
                """INSERT INTO files
                   (vault_id, file_path, file_name, file_size, status,
                    phase, phase_message, progress_percent,
                    processed_units, total_units, unit_label,
                    processing_started_at, wiki_pending)
                   VALUES (2, '/uploads/x.txt', 'x.txt', 100,
                           'processing', 'embedding', 'Embedding chunks',
                           50.0, 5, 10, 'chunks', CURRENT_TIMESTAMP, 1)""",
            )
            file_id = cur.lastrowid
            conn.commit()
        finally:
            self.pool.release_connection(conn)

        token = self._token(2, "m1", "member")
        resp = self.client.get(
            f"/api/documents/{file_id}/status",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["status"], "processing")
        self.assertEqual(body["phase"], "embedding")
        self.assertEqual(body["phase_message"], "Embedding chunks")
        self.assertEqual(body["progress_percent"], 50.0)
        self.assertEqual(body["processed_units"], 5)
        self.assertEqual(body["total_units"], 10)
        self.assertEqual(body["unit_label"], "chunks")
        # No wiki_compile_jobs row → wiki_pending=1 surfaces as wiki_status=pending.
        self.assertEqual(body["wiki_status"], "pending")
        # processing_started_at was set, so elapsed should be a non-negative float.
        self.assertIsNotNone(body["elapsed_seconds"])
        self.assertGreaterEqual(body["elapsed_seconds"], 0.0)

    def test_in_flight_duplicate_check_matches_pending_and_processing(self):
        """Reviewer Fix #1: route-side check rejects in-flight duplicates so two
        concurrent uploads of the same hash collapse to a single ingestion."""
        from app.utils.file_utils import compute_file_hash

        # Seed an existing pending row with a known hash.
        path = os.path.join(self.tmp, "dup.txt")
        with open(path, "w") as f:
            f.write("dup-content")
        h = compute_file_hash(path)

        conn = self.pool.get_connection()
        try:
            for st in ("pending", "processing", "indexed"):
                conn.execute(
                    "INSERT INTO files (vault_id, file_path, file_name, file_hash, "
                    "file_size, status) VALUES (2, ?, ?, ?, ?, ?)",
                    (f"/uploads/{st}", st, h, 1, st),
                )
            # An 'error' row must NOT match — re-uploading a failed file is allowed.
            conn.execute(
                "INSERT INTO files (vault_id, file_path, file_name, file_hash, "
                "file_size, status) VALUES (2, '/uploads/err', 'err', ?, 1, 'error')",
                (h,),
            )
            conn.commit()
        finally:
            self.pool.release_connection(conn)

        proc = DocumentProcessor(pool=self.pool)
        conn = self.pool.get_connection()
        try:
            row = proc._check_duplicate_in_flight(h, conn, vault_id=2)
            self.assertIsNotNone(row)
            self.assertIn(row["status"], ("pending", "processing", "indexed"))
        finally:
            self.pool.release_connection(conn)

        # Cross-vault must NOT match.
        conn = self.pool.get_connection()
        try:
            self.assertIsNone(
                proc._check_duplicate_in_flight(h, conn, vault_id=3)
            )
        finally:
            self.pool.release_connection(conn)

        # Hash present only in 'error' state must NOT match.
        h2 = "deadbeef" * 8
        conn = self.pool.get_connection()
        try:
            conn.execute(
                "INSERT INTO files (vault_id, file_path, file_name, file_hash, "
                "file_size, status) VALUES (2, '/uploads/only_err', 'only_err', ?, 1, 'error')",
                (h2,),
            )
            conn.commit()
            self.assertIsNone(
                proc._check_duplicate_in_flight(h2, conn, vault_id=2)
            )
        finally:
            self.pool.release_connection(conn)

    def test_async_upload_409_on_in_flight_hash_collision_does_not_leak_path(self):
        """Critic Fix #3: route returns 409 (not 500) when a different filename
        has the same hash as an in-flight upload, AND the response detail
        does not include the existing row's file_path (info disclosure)."""
        from app.utils.file_utils import compute_file_hash

        # Seed a pending row for vault 2 with a known hash + storage path.
        contents = b"shared-content-bytes"
        existing_path = os.path.join(self.tmp, "uploads_existing_secret.txt")
        with open(existing_path, "wb") as f:
            f.write(contents)
        h = compute_file_hash(existing_path)
        conn = self.pool.get_connection()
        try:
            conn.execute(
                "INSERT INTO files (vault_id, file_path, file_name, file_hash, "
                "file_size, status, phase) VALUES (2, ?, 'existing.txt', ?, ?, "
                "'pending', 'queued')",
                (existing_path, h, len(contents)),
            )
            conn.commit()
        finally:
            self.pool.release_connection(conn)

        # The route streams the new file to vault_uploads_dir; the test
        # client's vault_uploads_dir resolves under settings.data_dir,
        # which we already point at self.tmp. We do NOT need to set up
        # the on-disk file ourselves — the route does.
        token = self._token(2, "m1", "member")
        files_payload = {"file": ("new.txt", contents, "text/plain")}
        resp = self.client.post(
            "/api/documents",
            params={"vault_id": 2},
            headers={"Authorization": f"Bearer {token}"},
            files=files_payload,
        )
        # CONTRACT: 409, not 500.
        self.assertEqual(resp.status_code, 409, resp.text)
        body = resp.json()
        detail = body.get("detail", "")
        # CONTRACT: detail must NOT include the existing file's storage path.
        self.assertNotIn(
            existing_path,
            detail,
            f"409 detail leaked existing file_path: {detail!r}",
        )
        self.assertNotIn(
            "uploads_existing_secret",
            detail,
            f"409 detail leaked existing file basename: {detail!r}",
        )
        # Sanity: hash + status are present so the client can reconcile.
        self.assertIn(h, detail)
        self.assertIn("status=", detail)

    def test_status_route_cross_vault_returns_403(self):
        # File in vault 2; member3 only has access to vault 3.
        conn = self.pool.get_connection()
        try:
            cur = conn.execute(
                "INSERT INTO files (vault_id, file_path, file_name, file_size, status) "
                "VALUES (2, '/uploads/y.txt', 'y.txt', 100, 'indexed')",
            )
            file_id = cur.lastrowid
            conn.commit()
        finally:
            self.pool.release_connection(conn)

        token = self._token(3, "m2", "member")
        resp = self.client.get(
            f"/api/documents/{file_id}/status",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(resp.status_code, 403, resp.text)


class TestHandleFailurePreservesFileId(unittest.TestCase):
    """_handle_failure must carry file_id into the requeued TaskItem.

    Regression guard: the original implementation created a new TaskItem
    without file_id, causing the retry to fall through to process_file
    (legacy path) and re-run duplicate detection against an already-existing
    files row.
    """

    def test_file_id_preserved_in_retry_task(self):
        from app.services.background_tasks import TaskItem

        bp = BackgroundProcessor(retry_delay=0)

        async def runner():
            task = TaskItem(
                file_path="/tmp/x.txt",
                attempt=1,
                source="upload",
                vault_id=1,
                file_id=99,
            )
            await bp._handle_failure(task, "simulated error")
            return await bp.queue.get()

        retry_task = asyncio.run(runner())
        self.assertEqual(retry_task.file_id, 99)
        self.assertEqual(retry_task.attempt, 2)
        self.assertEqual(retry_task.file_path, "/tmp/x.txt")
        self.assertEqual(retry_task.vault_id, 1)

    def test_retry_task_calls_process_existing_file(self):
        """After failure, the requeued task (file_id set) must route to
        process_existing_file — not to process_file which re-runs dedup."""
        from app.services.background_tasks import TaskItem

        bp = BackgroundProcessor(retry_delay=0)
        bp.processor = MagicMock()
        bp.processor.process_file = AsyncMock()
        bp.processor.process_existing_file = AsyncMock()

        async def runner():
            task = TaskItem(
                file_path="/tmp/x.txt",
                attempt=1,
                source="upload",
                vault_id=1,
                file_id=99,
            )
            await bp._handle_failure(task, "simulated error")
            retry_task = await bp.queue.get()
            await bp._process_task(retry_task)
            bp.queue.task_done()

        asyncio.run(runner())
        bp.processor.process_existing_file.assert_awaited_once_with(
            file_id=99, file_path="/tmp/x.txt", vault_id=1
        )
        bp.processor.process_file.assert_not_awaited()

    def test_none_file_id_stays_none_in_retry(self):
        """Legacy scan/email tasks (file_id=None) must also stay None after retry."""
        from app.services.background_tasks import TaskItem

        bp = BackgroundProcessor(retry_delay=0)

        async def runner():
            task = TaskItem(
                file_path="/tmp/scan.txt",
                attempt=1,
                source="scan",
                vault_id=1,
                file_id=None,
            )
            await bp._handle_failure(task, "simulated error")
            return await bp.queue.get()

        retry_task = asyncio.run(runner())
        self.assertIsNone(retry_task.file_id)


class TestAdminRetryPassesFileId(unittest.TestCase):
    """Admin retry route must pass file_id to enqueue so the worker does not
    re-run duplicate detection on the already-existing files row."""

    def test_retry_document_enqueue_includes_file_id(self):
        import inspect

        from app.api.routes.documents import retry_document

        source = inspect.getsource(retry_document)
        self.assertIn(
            "file_id=file_id",
            source,
            "retry_document must pass file_id=file_id to background_processor.enqueue",
        )


if __name__ == "__main__":
    unittest.main()
