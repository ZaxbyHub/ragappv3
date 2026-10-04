"""Issue #783 companions (frozen checks live in test_m03_ingest_cancel.py
and are never edited): the worker unwind leaves files.status='cancelled'
with a truthful terminal phase, the route's 404/409-error/idempotent-200/403
branches, the process_file (scan/sync) path's cancel gate, the finalize
guard's post-write cleanup, registry clearing on re-enqueue, and the
destructive re-ingest semantic. Reuses the frozen module's route_env
fixture and stub machinery via import.
"""

CSRF_TEST_POLICY = "naive"

import asyncio
import threading
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from test_m03_ingest_cancel import (  # noqa: F401 - shared harness pieces
    VAULT_ID,
    _BlockingStubEmbeddingService,
    route_env,
)

from app.services.background_tasks import BackgroundProcessor


@pytest.fixture()
def env(request):
    """The frozen module's route_env fixture, resolvable by name so the
    import stays the discovered fixture and no parameter shadows it."""
    return request.getfixturevalue("route_env")


class _FakeVectorStore:
    """In-memory vector-store double whose rows are countable by file_id."""

    def __init__(self):
        self.records = []

    def _rows_for(self, file_id):
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

    def row_count(self, file_id):
        return len(self._rows_for(file_id))


# ---------------------------------------------------------------------------
# Route branches
# ---------------------------------------------------------------------------


def test_cancel_route_404_on_missing_file(env):
    resp = env.client.post(
        "/api/documents/999999/cancel",
        headers=env.superadmin_headers(),
    )
    assert resp.status_code == 404


def test_cancel_route_refused_on_error_status(env):
    file_id = env.seed_file("error")
    resp = env.client.post(
        f"/api/documents/{file_id}/cancel",
        headers=env.superadmin_headers(),
    )
    assert resp.status_code == 409
    assert env.file_status(file_id) == "error"


def test_cancel_route_idempotent_on_already_cancelled(env):
    file_id = env.seed_file("cancelled")
    resp = env.client.post(
        f"/api/documents/{file_id}/cancel",
        headers=env.superadmin_headers(),
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"
    assert env.file_status(file_id) == "cancelled"


def test_cancel_route_403_for_non_admin_member(env):
    from app.services.auth_service import (
        compute_client_fingerprint,
        create_access_token,
    )

    conn = env.pool.get_connection()
    try:
        conn.execute(
            "INSERT INTO users (id, username, hashed_password, full_name, role, "
            "is_active) VALUES (2, 'member', 'pw-hash', 'Member', 'member', 1)"
        )
        conn.commit()
    finally:
        env.pool.release_connection(conn)
    token = create_access_token(
        2, "member", "member",
        client_fingerprint=compute_client_fingerprint(""),
    )

    file_id = env.seed_file("processing")
    resp = env.client.post(
        f"/api/documents/{file_id}/cancel",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403
    assert env.file_status(file_id) == "processing"


# ---------------------------------------------------------------------------
# Worker unwind: terminal status + phase (the frozen C4 asserts orphans only)
# ---------------------------------------------------------------------------


async def test_cancel_unwind_leaves_cancelled_status_and_phase(env):
    from app.api.deps import get_background_processor
    from app.config import settings
    from app.main import app

    sql_path = env.tmp_path / "unwind.sql"
    sql_path.write_text(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT);\n",
        encoding="utf-8",
    )
    file_id = env.seed_file("pending", name="unwind.sql")

    vector_store = _FakeVectorStore()
    blocked = threading.Event()
    release = threading.Event()
    embedder = _BlockingStubEmbeddingService(blocked, release)

    original_lease = settings.ingestion_job_lease_enabled
    settings.ingestion_job_lease_enabled = True
    processor = BackgroundProcessor(
        pool=env.pool,
        retry_delay=0.05,
        vector_store=vector_store,
        embedding_service=embedder,
    )
    app.dependency_overrides[get_background_processor] = lambda: processor
    try:
        await processor.enqueue(str(sql_path), VAULT_ID, file_id=file_id)
        await asyncio.wait_for(processor.start(), timeout=10)
        assert await asyncio.to_thread(blocked.wait, 30)

        resp = env.client.post(
            f"/api/documents/{file_id}/cancel",
            headers=env.superadmin_headers(),
        )
        assert resp.status_code == 200

        release.set()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if env.ingest_job_settled(file_id):
                break
            await asyncio.sleep(0.25)

        assert env.file_status(file_id) == "cancelled"
        import sqlite3 as _sqlite3

        conn = _sqlite3.connect(env.db_path)
        conn.row_factory = _sqlite3.Row
        try:
            row = conn.execute(
                "SELECT phase FROM files WHERE id = ?", (file_id,)
            ).fetchone()
        finally:
            conn.close()
        assert row["phase"] == "cancelled"
        assert env.atom_count(file_id) == 0
        assert vector_store.row_count(file_id) == 0
    finally:
        release.set()
        settings.ingestion_job_lease_enabled = original_lease
        app.dependency_overrides[get_background_processor] = lambda: MagicMock(
            is_running=True, enqueue=AsyncMock()
        )
        await asyncio.wait_for(processor.stop(timeout=10), timeout=20)


# ---------------------------------------------------------------------------
# process_file (scan/sync) path gate: cancel between steps aborts the
# scan-path ingest at the pre-write seam even though this path creates its
# own files row.
# ---------------------------------------------------------------------------


async def test_process_file_path_cancel_gate(env):
    from app.config import settings

    sql_path = env.tmp_path / "scan-path.sql"
    sql_path.write_text(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT);\n",
        encoding="utf-8",
    )

    vector_store = _FakeVectorStore()
    blocked = threading.Event()
    release = threading.Event()
    embedder = _BlockingStubEmbeddingService(blocked, release)

    original_lease = settings.ingestion_job_lease_enabled
    settings.ingestion_job_lease_enabled = False
    processor = BackgroundProcessor(
        pool=env.pool,
        retry_delay=0.05,
        vector_store=vector_store,
        embedding_service=embedder,
    )
    try:
        # No file_id: the worker takes the process_file (scan/sync) path and
        # creates its own files row.
        await processor.enqueue(str(sql_path), VAULT_ID)
        await asyncio.wait_for(processor.start(), timeout=10)
        assert await asyncio.to_thread(blocked.wait, 30)

        # The scan path assigned the row while we were blocked; find it.
        import sqlite3 as _sqlite3

        conn = _sqlite3.connect(env.db_path)
        conn.row_factory = _sqlite3.Row
        try:
            row = conn.execute(
                "SELECT id FROM files WHERE file_name = ? AND status = 'processing'",
                ("scan-path.sql",),
            ).fetchone()
        finally:
            conn.close()
        assert row is not None, "scan-path ingest never reached processing"
        file_id = int(row["id"])

        processor.processor.request_cancel(file_id)
        release.set()

        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if env.file_status(file_id) == "cancelled":
                break
            await asyncio.sleep(0.25)

        assert env.file_status(file_id) == "cancelled"
        assert env.atom_count(file_id) == 0
        assert vector_store.row_count(file_id) == 0
    finally:
        release.set()
        settings.ingestion_job_lease_enabled = original_lease
        await asyncio.wait_for(processor.stop(timeout=10), timeout=20)


# ---------------------------------------------------------------------------
# Finalize guard: a cancel landing AFTER the vector write unwinds the
# already-written generation (no orphans on a cancelled row).
# ---------------------------------------------------------------------------


async def test_finalize_guard_cleans_up_after_vector_write(env):
    from app.services.document_processor import (
        DocumentProcessor,
        IngestCancelledError,
    )

    sql_path = env.tmp_path / "finalize-guard.sql"
    sql_path.write_text(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT);\n",
        encoding="utf-8",
    )
    file_id = env.seed_file("pending", name="finalize-guard.sql")

    class _PostWriteParkedVectorStore(_FakeVectorStore):
        """Records the rows, THEN parks: the cancel lands after the write."""

        def __init__(self):
            super().__init__()
            self.written = threading.Event()
            self.resume = threading.Event()

        async def add_chunks(self, records, generation_prefix=None):
            await super().add_chunks(records, generation_prefix)
            self.written.set()
            await asyncio.to_thread(self.resume.wait, 30)

    vector_store = _PostWriteParkedVectorStore()
    embedder = MagicMock()
    # Chunk-size validation reads these embedder attributes (see the frozen
    # module's _BlockingStubEmbeddingService for the same two).
    embedder.MAX_TEXT_LENGTH = 8192
    embedder.embedding_doc_prefix = ""
    embedder.embed_batch = AsyncMock(
        return_value=([[0.1] * 8], [])
    )
    processor = DocumentProcessor(
        vector_store=vector_store,
        embedding_service=embedder,
        pool=env.pool,
    )

    async def run_ingest():
        await asyncio.to_thread(sql_path.touch)
        await processor.process_existing_file(
            file_id=file_id,
            file_path=str(sql_path),
            vault_id=VAULT_ID,
        )

    task = asyncio.create_task(run_ingest())
    try:
        assert await asyncio.to_thread(vector_store.written.wait, 30)
        assert vector_store.row_count(file_id) > 0  # rows ARE written

        processor.request_cancel(file_id)  # lands after the write, before finalize
        vector_store.resume.set()

        with pytest.raises(IngestCancelledError):
            await asyncio.wait_for(task, timeout=30)

        assert env.file_status(file_id) == "cancelled"
        assert env.atom_count(file_id) == 0
        assert vector_store.row_count(file_id) == 0  # written rows removed
    finally:
        vector_store.resume.set()
        processor.clear_cancel(file_id)
        if not task.done():
            task.cancel()


# ---------------------------------------------------------------------------
# Registry lifecycle
# ---------------------------------------------------------------------------


async def test_registry_cleared_on_reenqueue(env):
    from app.services.background_tasks import BackgroundProcessor

    vector_store = _FakeVectorStore()
    embedder = MagicMock()
    processor = BackgroundProcessor(
        pool=env.pool,
        vector_store=vector_store,
        embedding_service=embedder,
    )
    file_id = env.seed_file("cancelled")

    processor.processor.request_cancel(file_id)
    assert processor.processor.is_cancel_requested(file_id)

    path = env.tmp_path / "reenqueue.sql"
    path.write_text("CREATE TABLE t (id INTEGER);\n", encoding="utf-8")
    await processor.enqueue(str(path), VAULT_ID, file_id=file_id)

    assert not processor.processor.is_cancel_requested(file_id)


async def test_rollback_wipes_prior_generation_leftovers(env):
    """Destructive re-ingest semantic: unwinding a cancelled file removes
    whatever atoms/vectors its prior generation left (issue #783 decision,
    disclosed in the plan)."""
    from app.services.document_processor import DocumentProcessor

    vector_store = _FakeVectorStore()
    vector_store.records = [
        {"file_id": 7, "id": "a"},
        {"file_id": 7, "id": "b"},
        {"file_id": 8, "id": "c"},
    ]
    processor = DocumentProcessor(
        vector_store=vector_store,
        embedding_service=MagicMock(),
        pool=env.pool,
    )
    file_id = env.seed_file("cancelled")
    env.seed_atoms(file_id, 2)

    processor.request_cancel(file_id)
    await processor.rollback_cancelled_ingest(file_id)

    assert env.atom_count(file_id) == 0
    assert vector_store.row_count(file_id) == 0
    assert vector_store.row_count(8) == 1  # other files untouched
    assert env.file_status(file_id) == "cancelled"
    assert not processor.is_cancel_requested(file_id)


# ---------------------------------------------------------------------------
# Reviewer finding 1 (Phase 4.5, Important): a cancel the route ALREADY
# accepted (200 returned, status='cancelled' committed) must survive the
# worker's finalize even when the registry check at finalize entry raced
# False — the success write is conditioned on the row still being
# pre-terminal, and rowcount 0 unwinds the generation.
# ---------------------------------------------------------------------------


async def test_finalize_write_refuses_to_bury_accepted_cancel(env):
    from app.services.document_processor import (
        DocumentProcessor,
        IngestCancelledError,
    )

    file_id = env.seed_file("cancelled")  # the route's accepted flip
    env.seed_atoms(file_id, 2)
    vector_store = _FakeVectorStore()
    vector_store.records = [{"file_id": file_id, "id": "v1"}]
    processor = DocumentProcessor(
        vector_store=vector_store,
        embedding_service=MagicMock(),
        pool=env.pool,
    )
    # Registry EMPTY: the cancel landed after finalize's entry check raced.

    with pytest.raises(IngestCancelledError):
        await processor._finalize_indexed_success(
            file_id=file_id,
            vault_id=VAULT_ID,
            chunks=[],
            document_text="text",
            chunks_failed_count=0,
        )

    assert env.file_status(file_id) == "cancelled"  # the 200 survived
    assert env.atom_count(file_id) == 0  # written generation unwound
    assert vector_store.row_count(file_id) == 0
