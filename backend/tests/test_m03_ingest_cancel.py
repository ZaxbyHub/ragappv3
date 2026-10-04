"""Issue #783 "[Workstream M] PR 3 of 4" — acceptance checks C3/C4/C5
(AC3/AC4/AC5) for the frozen route POST /api/documents/{file_id}/cancel.

Route contract (frozen by the trace): authenticated vault-admin/superadmin,
CSRF-protected like the other mutating document routes; missing row -> 404;
status indexed/partial -> 409 with data untouched; status pending/processing
-> 200 with files.status landing on the new terminal value 'cancelled' and no
orphan document_atoms / vector rows after the worker unwinds. At base the
route does not exist, so C3/C4 fail with `assert 404 == 200` and C5 with
`assert 404 == 409`.

Harness mirrors backend/tests/test_documents_auth.py (TestDocumentAuthBase):
the full app.main app, temp data_dir + SQLiteConnectionPool, _pool_cache
reset, superadmin JWT minted against the "" user-agent fingerprint, and the
conftest CSRF test bypass (declared below). C4 drives the real
BackgroundProcessor + DocumentProcessor through a stub embedder that blocks
on a threading.Event after the first chunk batch (test_559_lease_integration
.py drives the processor the same way; the .sql SchemaParser path keeps the
parse hermetic — unstructured is stubbed in this environment).
"""

CSRF_TEST_POLICY = "naive"

import asyncio
import os
import shutil
import sqlite3
import sys
import threading
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Stub missing optional dependencies (copied from test_documents_auth.py so
# app.main imports cleanly without lancedb/pyarrow/unstructured).
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

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.deps import (
    get_background_processor,
    get_db,
    get_db_pool,
    get_embedding_service,
    get_secret_manager,
    get_vector_store,
)
from app.config import settings
from app.main import app
from app.models.database import SQLiteConnectionPool, init_db, run_migrations
from app.services.auth_service import compute_client_fingerprint, create_access_token

VAULT_ID = 2  # seeded below; superadmin can admin any vault


class _RouteEnv:
    """Per-test namespace: temp DB, pool, TestClient, seeding helpers."""

    def __init__(self, tmp_path: Path, db_path: str, pool: SQLiteConnectionPool):
        self.tmp_path = tmp_path
        self.db_path = db_path
        self.pool = pool
        self.client = TestClient(app)
        # Override default User-Agent so fingerprint validation matches token
        self.client.headers["user-agent"] = ""

    def superadmin_headers(self):
        token = create_access_token(
            1, "superadmin", "superadmin",
            client_fingerprint=compute_client_fingerprint(""),
        )
        return {"Authorization": f"Bearer {token}"}

    def seed_file(self, status: str, name: str = "cancel-me.sql") -> int:
        conn = self.pool.get_connection()
        try:
            cur = conn.execute(
                "INSERT INTO files (vault_id, file_path, file_name, file_size, status) "
                "VALUES (?, ?, ?, 64, ?)",
                (VAULT_ID, str(self.tmp_path / name), name, status),
            )
            conn.commit()
            return int(cur.lastrowid)
        finally:
            self.pool.release_connection(conn)

    def file_status(self, file_id: int):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT status FROM files WHERE id = ?", (file_id,)
            ).fetchone()
            return None if row is None else row["status"]
        finally:
            conn.close()

    def atom_count(self, file_id: int) -> int:
        conn = sqlite3.connect(self.db_path)
        try:
            return int(
                conn.execute(
                    "SELECT COUNT(*) FROM document_atoms WHERE file_id = ?",
                    (file_id,),
                ).fetchone()[0]
            )
        finally:
            conn.close()

    def seed_atoms(self, file_id: int, count: int) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            for ordinal in range(count):
                conn.execute(
                    "INSERT INTO document_atoms "
                    "(atom_id, file_id, generation_hash, ordinal, kind, raw_text) "
                    "VALUES (?, ?, 'seedgen', ?, 'code', ?)",
                    (f"seed-atom-{file_id}-{ordinal}", file_id, ordinal,
                     f"SELECT {ordinal};"),
                )
            conn.commit()
        finally:
            conn.close()

    def ingest_job_settled(self, file_id: int) -> bool:
        """True once the durable ingestion job row left pending/running."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT status FROM jobs WHERE queue = 'ingestion' "
                "AND CAST(json_extract(payload_json, '$.file_id') AS INTEGER) = ?",
                (file_id,),
            ).fetchone()
            return row is not None and row["status"] not in ("pending", "running")
        finally:
            conn.close()


@pytest.fixture()
def route_env(tmp_path):
    """Temp DB + full-app TestClient with dependency overrides seeded
    (the pytest-fixture translation of TestDocumentAuthBase.setUp/tearDown)."""
    original = (
        settings.jwt_secret_key,
        settings.users_enabled,
        settings.data_dir,
    )
    settings.jwt_secret_key = "test-secret-key-for-testing-at-least-32-chars-long"
    settings.users_enabled = True
    settings.data_dir = tmp_path
    db_path = str(tmp_path / "app.db")

    from app.models.database import _pool_cache, _pool_cache_lock

    with _pool_cache_lock:
        for cached_pool in list(_pool_cache.values()):
            cached_pool.close_all()
        _pool_cache.clear()

    init_db(db_path)
    run_migrations(db_path)
    pool = SQLiteConnectionPool(db_path, max_size=6)

    def override_get_db():
        conn = pool.get_connection()
        try:
            yield conn
        finally:
            pool.release_connection(conn)

    mock_vec = MagicMock()
    mock_vec.db = None
    mock_vec.delete_by_file = AsyncMock(return_value=1)
    mock_emb = MagicMock()
    mock_bp = MagicMock()
    mock_bp.is_running = True
    mock_bp.enqueue = AsyncMock()
    mock_sm = MagicMock()
    mock_sm.get_hmac_key.return_value = (b"test-hmac-key-32bytes-padding!!", "v1")

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_vector_store] = lambda: mock_vec
    app.dependency_overrides[get_embedding_service] = lambda: mock_emb
    app.dependency_overrides[get_db_pool] = lambda: pool
    app.dependency_overrides[get_background_processor] = lambda: mock_bp
    app.dependency_overrides[get_secret_manager] = lambda: mock_sm

    conn = pool.get_connection()
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("DELETE FROM files")
        conn.execute("DELETE FROM vault_members")
        conn.execute("DELETE FROM users WHERE id != 0")
        conn.execute(
            "INSERT INTO users (id, username, hashed_password, full_name, role, "
            "is_active) VALUES (1, 'superadmin', 'pw-hash', 'Super Admin', "
            "'superadmin', 1)"
        )
        conn.execute(
            "INSERT OR IGNORE INTO vaults (id, name, description) "
            "VALUES (?, 'Cancel Test Vault', '')",
            (VAULT_ID,),
        )
        conn.commit()
    finally:
        pool.release_connection(conn)

    yield _RouteEnv(tmp_path, db_path, pool)

    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_vector_store, None)
    app.dependency_overrides.pop(get_embedding_service, None)
    app.dependency_overrides.pop(get_db_pool, None)
    app.dependency_overrides.pop(get_background_processor, None)
    app.dependency_overrides.pop(get_secret_manager, None)

    with _pool_cache_lock:
        for cached_pool in list(_pool_cache.values()):
            cached_pool.close_all()
        _pool_cache.clear()
    pool.close_all()
    settings.jwt_secret_key, settings.users_enabled, settings.data_dir = original
    shutil.rmtree(tmp_path, ignore_errors=True)


# ---------------------------------------------------------------------------
# C3 (AC3) — processing row: 200 + files.status 'cancelled'
# ---------------------------------------------------------------------------


def test_cancel_route_marks_file_cancelled(route_env):
    """POST /api/documents/{id}/cancel on a status='processing' file returns
    200 and the files row ends with the new terminal status 'cancelled'."""
    file_id = route_env.seed_file("processing")

    resp = route_env.client.post(
        f"/api/documents/{file_id}/cancel",
        headers=route_env.superadmin_headers(),
    )

    assert resp.status_code == 200
    assert route_env.file_status(file_id) == "cancelled"


# ---------------------------------------------------------------------------
# C5 (AC5) — indexed row: 409 + data kept
# ---------------------------------------------------------------------------


def test_cancel_after_indexed_is_refused_and_data_kept(route_env):
    """Cancel is refused with 409 once ingest finished (indexed), and the
    already-indexed chunk atoms survive untouched."""
    file_id = route_env.seed_file("indexed")
    seeded_atoms = 3
    route_env.seed_atoms(file_id, seeded_atoms)

    resp = route_env.client.post(
        f"/api/documents/{file_id}/cancel",
        headers=route_env.superadmin_headers(),
    )

    assert resp.status_code == 409
    assert route_env.atom_count(file_id) == seeded_atoms


# ---------------------------------------------------------------------------
# C4 (AC4) — mid-ingest cancel unwinds the real worker with no orphans
# ---------------------------------------------------------------------------


class _BlockingFakeVectorStore:
    """In-memory vector-store double (the test_document_processor.py Fake
    pattern) whose rows can be counted by file_id."""

    def __init__(self):
        self.records = []

    def _rows_for(self, file_id) -> list:
        fid = str(file_id)
        return [r for r in self.records if str(r.get("file_id")) == fid]

    async def init_table(self, embedding_dim):
        self.embedding_dim = embedding_dim

    async def add_chunks(self, records, generation_prefix=None):
        self.records.extend(records)

    async def delete_old_generation_by_file(self, file_id, hash_short):
        return 0

    async def delete_by_file(self, file_id):
        keep = [r for r in self.records if r not in self._rows_for(file_id)]
        removed = len(self.records) - len(keep)
        self.records = keep
        return removed

    async def count_by_file(self, file_id):
        return len(self._rows_for(file_id))

    def row_count(self, file_id) -> int:
        return len(self._rows_for(file_id))


class _BlockingStubEmbeddingService:
    """Stub embedder that blocks on a threading.Event after the first chunk
    batch — the issue's prescribed mid-ingest pause point between the
    chunking and embedding steps."""

    MAX_TEXT_LENGTH = 8192
    embedding_doc_prefix = ""

    def __init__(self, blocked: threading.Event, release: threading.Event):
        self.blocked = blocked
        self.release = release
        self.batches = 0

    async def embed_batch(self, texts, fail_fast=False):
        self.batches += 1
        self.blocked.set()
        # Bounded blocking wait off the event loop (asyncio.to_thread), so
        # the worker coroutine parks while the loop stays responsive for the
        # cancel request.
        await asyncio.to_thread(self.release.wait, 30)
        return ([[0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8] for _ in texts], [])


async def test_mid_ingest_cancel_leaves_no_orphans(route_env):
    """A cancel landing while the worker sits between chunking and embedding
    unwinds it without writing chunk atoms or vectors for that file_id."""
    from app.services.background_tasks import BackgroundProcessor

    sql_path = route_env.tmp_path / "mid-ingest.sql"
    sql_path.write_text(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT);\n"
        "CREATE TABLE posts (id INTEGER PRIMARY KEY, user_id INTEGER);\n",
        encoding="utf-8",
    )
    file_id = route_env.seed_file("pending", name="mid-ingest.sql")

    vector_store = _BlockingFakeVectorStore()
    blocked = threading.Event()
    release = threading.Event()
    embedder = _BlockingStubEmbeddingService(blocked, release)

    original_lease = settings.ingestion_job_lease_enabled
    settings.ingestion_job_lease_enabled = True
    processor = BackgroundProcessor(
        pool=route_env.pool,
        retry_delay=0.05,
        vector_store=vector_store,
        embedding_service=embedder,
    )
    app.dependency_overrides[get_background_processor] = lambda: processor

    # Enqueue BEFORE start() so the boot migration sees the pending jobs row
    # and does not create a second one (test_559_lease_integration.py order).
    await processor.enqueue(str(sql_path), VAULT_ID, file_id=file_id)
    await asyncio.wait_for(processor.start(), timeout=10)
    try:
        # Wait until the stub blocks at the embed step (bounded; via a thread
        # so the loop keeps advancing the worker).
        assert await asyncio.to_thread(blocked.wait, 30), (
            "stub embedder never reached the embed step"
        )

        # Real route, real loop: httpx ASGI transport serves the request on
        # this event loop while the worker is parked in the stub.
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            resp = await client.post(
                f"/api/documents/{file_id}/cancel",
                headers={
                    "user-agent": "",
                    **route_env.superadmin_headers(),
                },
            )
        assert resp.status_code == 200

        # Release the stub so the worker can unwind the cancelled ingest.
        release.set()

        # Bounded wait for the worker to finish with this file: the durable
        # job row settles, or (base tree) the completed ingest writes rows.
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if (
                route_env.atom_count(file_id) > 0
                or vector_store.row_count(file_id) > 0
                or route_env.ingest_job_settled(file_id)
            ):
                break
            await asyncio.sleep(0.25)

        assert route_env.atom_count(file_id) == 0
        assert vector_store.row_count(file_id) == 0
    finally:
        release.set()
        settings.ingestion_job_lease_enabled = original_lease
        app.dependency_overrides[get_background_processor] = lambda: MagicMock(
            is_running=True, enqueue=AsyncMock()
        )
        await asyncio.wait_for(processor.stop(timeout=10), timeout=20)
