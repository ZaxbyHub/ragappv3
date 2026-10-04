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


# ---------------------------------------------------------------------------
# PRR-023: the route's rowcount-0 race branches (refused-completed 409 and
# the re-read idempotent 200) — previously zero-covered. The request hook
# flips the row between the route's SELECT and its guarded UPDATE.
# ---------------------------------------------------------------------------


def _flip_row_status(route_env, file_id: int, status: str) -> None:  # noqa: F811
    import sqlite3 as _sqlite3

    conn = _sqlite3.connect(route_env.db_path)
    try:
        conn.execute("UPDATE files SET status = ? WHERE id = ?", (status, file_id))
        conn.commit()
    finally:
        conn.close()


def test_cancel_route_refused_completed_when_worker_wins(route_env):  # noqa: F811 - fixture reuse
    """Rowcount-0 + re-read 'indexed' -> 409 refused-completed, registry
    cleared, row and data untouched (issue #783 review PRR-023)."""
    from unittest.mock import MagicMock as _MG

    from app.api.deps import get_background_processor
    from app.main import app

    file_id = route_env.seed_file("pending")
    route_env.seed_atoms(file_id, 2)

    bp = _MG()
    bp.request_ingest_cancel = _MG(
        side_effect=lambda fid: _flip_row_status(route_env, fid, "indexed")
    )
    saved = app.dependency_overrides.get(get_background_processor)
    app.dependency_overrides[get_background_processor] = lambda: bp
    try:
        resp = route_env.client.post(
            f"/api/documents/{file_id}/cancel",
            headers=route_env.superadmin_headers(),
        )
    finally:
        if saved is not None:
            app.dependency_overrides[get_background_processor] = saved

    assert resp.status_code == 409
    assert route_env.file_status(file_id) == "indexed"
    assert route_env.atom_count(file_id) == 2
    bp.clear_ingest_cancel.assert_called_once_with(file_id)


def test_cancel_route_reread_idempotent_200_when_worker_cancelled(route_env):  # noqa: F811 - fixture reuse
    """Rowcount-0 + re-read 'cancelled' (the worker's own unwind won the
    race) -> idempotent 200 without a duplicate flip (PRR-023)."""
    from unittest.mock import MagicMock as _MG

    from app.api.deps import get_background_processor
    from app.main import app

    file_id = route_env.seed_file("pending")

    bp = _MG()
    bp.request_ingest_cancel = _MG(
        side_effect=lambda fid: _flip_row_status(route_env, fid, "cancelled")
    )
    saved = app.dependency_overrides.get(get_background_processor)
    app.dependency_overrides[get_background_processor] = lambda: bp
    try:
        resp = route_env.client.post(
            f"/api/documents/{file_id}/cancel",
            headers=route_env.superadmin_headers(),
        )
    finally:
        if saved is not None:
            app.dependency_overrides[get_background_processor] = saved

    assert resp.status_code == 200
    assert route_env.file_status(file_id) == "cancelled"


def test_cancel_route_audit_rows_recorded(route_env):  # noqa: F811 - fixture reuse
    """PRR-029: the HMAC audit path records cancel decisions now that the
    fixture installs app.state.secret_manager."""
    import sqlite3 as _sqlite3

    file_id = route_env.seed_file("processing")
    route_env.client.post(
        f"/api/documents/{file_id}/cancel",
        headers=route_env.superadmin_headers(),
    )
    conn = _sqlite3.connect(route_env.db_path)
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM document_actions WHERE file_id = ? "
            "AND action = 'cancel' AND status = 'success'",
            (file_id,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert n == 1


# ---------------------------------------------------------------------------
# PRR-025: gate A (cancel before embedding skips it), failure-raced-cancel,
# JobLease.cancel terminal status value, rollback negatives.
# ---------------------------------------------------------------------------


async def test_gate_a_cancel_skips_embedding(route_env):  # noqa: F811 - fixture reuse
    """PRR-025: a cancel registered before the worker starts trips gate A —
    the stub embedder is never called."""
    from app.services.document_processor import (
        DocumentProcessor,
        IngestCancelledError,
    )

    sql_path = route_env.tmp_path / "gate-a.sql"
    sql_path.write_text(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT);\n",
        encoding="utf-8",
    )
    file_id = route_env.seed_file("pending", name="gate-a.sql")

    vector_store = _FakeVectorStore()
    embedder = MagicMock()
    embedder.MAX_TEXT_LENGTH = 8192
    embedder.embedding_doc_prefix = ""
    embedder.embed_batch = AsyncMock(return_value=([[0.1] * 8], []))
    processor = DocumentProcessor(
        vector_store=vector_store,
        embedding_service=embedder,
        pool=route_env.pool,
    )

    processor.request_cancel(file_id)
    with pytest.raises(IngestCancelledError):
        await processor.process_existing_file(
            file_id=file_id,
            file_path=str(sql_path),
            vault_id=VAULT_ID,
        )
    # Simulate the transport unwind (real transports call this on ICE).
    await processor.rollback_cancelled_ingest(file_id)

    embedder.embed_batch.assert_not_called()
    assert route_env.file_status(file_id) == "cancelled"
    assert route_env.atom_count(file_id) == 0
    assert vector_store.row_count(file_id) == 0


async def test_failure_raced_cancel_lands_cancelled_not_error(route_env):  # noqa: F811 - fixture reuse
    """PRR-025: a generic failure racing a cancel converts to the cancel
    unwind instead of writing status='error'."""
    from app.services.document_processor import (
        DocumentProcessor,
        IngestCancelledError,
    )

    sql_path = route_env.tmp_path / "raced.sql"
    sql_path.write_text(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT);\n",
        encoding="utf-8",
    )
    file_id = route_env.seed_file("pending", name="raced.sql")

    class _RacingEmbedder:
        MAX_TEXT_LENGTH = 8192
        embedding_doc_prefix = ""

        async def embed_batch(self, texts, fail_fast=False):
            # The failure and the cancel arrive together.
            processor.request_cancel(file_id)
            raise RuntimeError("embedder exploded")

    vector_store = _FakeVectorStore()
    processor = DocumentProcessor(
        vector_store=vector_store,
        embedding_service=_RacingEmbedder(),
        pool=route_env.pool,
    )

    with pytest.raises(IngestCancelledError):
        await processor.process_existing_file(
            file_id=file_id,
            file_path=str(sql_path),
            vault_id=VAULT_ID,
        )
    await processor.rollback_cancelled_ingest(file_id)

    assert route_env.file_status(file_id) == "cancelled"
    assert route_env.atom_count(file_id) == 0


async def test_rollback_skips_deletes_when_row_not_cancellable(route_env):  # noqa: F811 - fixture reuse
    """PRR-004: rollback on a row whose flip cannot land (already indexed)
    preserves the survivor's atoms — the destructive deletes are gated on
    the guarded UPDATE landing."""
    from app.services.document_processor import DocumentProcessor

    file_id = route_env.seed_file("indexed")
    route_env.seed_atoms(file_id, 3)
    vector_store = _FakeVectorStore()
    vector_store.records = [{"file_id": file_id, "id": "v"}]
    processor = DocumentProcessor(
        vector_store=vector_store,
        embedding_service=MagicMock(),
        pool=route_env.pool,
    )
    processor.request_cancel(file_id)

    await processor.rollback_cancelled_ingest(file_id)

    assert route_env.file_status(file_id) == "indexed"
    assert route_env.atom_count(file_id) == 3
    assert vector_store.row_count(file_id) == 1
    assert not processor.is_cancel_requested(file_id)


async def test_rollback_tombstones_vectors_on_delete_failure(route_env):  # noqa: F811 - fixture reuse
    """PRR-002: a failing vector delete queues the tombstone and the row
    still lands terminal 'cancelled'."""
    import sqlite3 as _sqlite3

    from app.services.document_processor import DocumentProcessor

    class _FailingVectorStore(_FakeVectorStore):
        async def delete_by_file(self, file_id):
            raise RuntimeError("lancedb io error")

    file_id = route_env.seed_file("cancelled")
    vector_store = _FailingVectorStore()
    processor = DocumentProcessor(
        vector_store=vector_store,
        embedding_service=MagicMock(),
        pool=route_env.pool,
    )
    processor.request_cancel(file_id)

    await processor.rollback_cancelled_ingest(file_id)

    assert route_env.file_status(file_id) == "cancelled"
    conn = _sqlite3.connect(route_env.db_path)
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM vector_delete_pending WHERE file_id = ?",
            (file_id,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert n == 1


async def test_job_settles_exactly_cancelled(route_env):  # noqa: F811 - fixture reuse
    """PRR-025: the lease settle lands jobs.status='cancelled' exactly (the
    frozen settle helper accepts any non-pending/running value)."""
    import sqlite3 as _sqlite3

    from app.api.deps import get_background_processor
    from app.config import settings
    from app.main import app

    sql_path = route_env.tmp_path / "lease-cancel.sql"
    sql_path.write_text(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT);\n",
        encoding="utf-8",
    )
    file_id = route_env.seed_file("pending", name="lease-cancel.sql")

    vector_store = _FakeVectorStore()
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
    try:
        await processor.enqueue(str(sql_path), VAULT_ID, file_id=file_id)
        await asyncio.wait_for(processor.start(), timeout=10)
        assert await asyncio.to_thread(blocked.wait, 30)

        resp = route_env.client.post(
            f"/api/documents/{file_id}/cancel",
            headers=route_env.superadmin_headers(),
        )
        assert resp.status_code == 200
        release.set()

        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if route_env.ingest_job_settled(file_id):
                break
            await asyncio.sleep(0.25)

        conn = _sqlite3.connect(route_env.db_path)
        conn.row_factory = _sqlite3.Row
        try:
            row = conn.execute(
                "SELECT status FROM jobs WHERE queue = 'ingestion' "
                "AND CAST(json_extract(payload_json, '$.file_id') AS INTEGER) = ?",
                (file_id,),
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row["status"] == "cancelled"
    finally:
        release.set()
        settings.ingestion_job_lease_enabled = original_lease
        app.dependency_overrides[get_background_processor] = lambda: MagicMock(
            is_running=True, enqueue=AsyncMock()
        )
        await asyncio.wait_for(processor.stop(timeout=10), timeout=20)

async def test_cancel_during_staged_rebuild_keeps_live_vectors(route_env):  # noqa: F811 - fixture reuse
    """F-001 (external review 5981826111): a cancel on a staged-rebuild
    re-embed must NOT destroy the document's previously indexed live
    vectors/atoms — the rebuild neither owns nor re-creates them."""
    from app.services.document_processor import (
        DocumentProcessor,
        IngestCancelledError,
    )

    sql_path = route_env.tmp_path / "staged.sql"
    sql_path.write_text(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT);\n",
        encoding="utf-8",
    )
    file_id = route_env.seed_file("indexed", name="staged.sql")
    route_env.seed_atoms(file_id, 2)

    class _StagedVectorStore(_FakeVectorStore):
        """Rows recorded per-target: 'live' vs a staged rebuild table."""

        def __init__(self):
            self.live: list = []
            self.staged: list = []

        def _rows_for(self, file_id):
            fid = str(file_id)
            return [r for r in self.live if str(r.get("file_id")) == fid]

        def row_count(self, file_id) -> int:
            return len(self._rows_for(file_id))

        async def init_table(self, embedding_dim, target=None):
            self.embedding_dim = embedding_dim

        async def add_chunks(self, records, generation_prefix=None, target=None):
            (self.staged if target is not None else self.live).extend(records)
            return {"vector_write_ms": 1.0}

        async def delete_old_generation_by_file(
            self, file_id, hash_short, target=None
        ):
            return 0

        async def count_by_file(self, file_id, target=None):
            return len(self._rows_for(file_id))

        async def delete_by_file(self, file_id, target=None):
            # F-001 regression pin: a live-table delete while a staged
            # target is in use IS the data-loss defect.
            if target is None:
                raise AssertionError("live-table delete during staged rebuild")

    store = _StagedVectorStore()
    store.records = [{"file_id": file_id, "id": "live-1"}]
    embedder = MagicMock()
    embedder.MAX_TEXT_LENGTH = 8192
    embedder.embedding_doc_prefix = ""
    embedder.embed_batch = AsyncMock(return_value=([[0.1] * 8], []))
    processor = DocumentProcessor(
        vector_store=store,
        embedding_service=embedder,
        pool=route_env.pool,
    )

    rebuild_handle = object()  # opaque staged-rebuild marker

    async def reembed():
        await processor.process_existing_file(
            file_id=file_id,
            file_path=str(sql_path),
            vault_id=VAULT_ID,
            vector_target=rebuild_handle,
        )

    def cancel_midflight():
        processor.request_cancel(file_id)

    # Cancel lands after the write phase begins: poll for the staged write.
    import threading as _th

    written = _th.Event()
    _base_add = store.add_chunks

    async def add_then_signal(records, generation_prefix=None, target=None):
        result = await _base_add(records, generation_prefix=generation_prefix)
        if target is not None:
            written.set()
        return result

    store.add_chunks = add_then_signal
    task = asyncio.create_task(reembed())
    await asyncio.to_thread(written.wait, 30)
    processor.request_cancel(file_id)
    with pytest.raises(IngestCancelledError):
        await asyncio.wait_for(task, timeout=30)

    # The reindex transport's documented carve-out (the loop that owns this
    # call) restores the prior serving state; perform it here identically.
    import sqlite3 as _sqlite3

    conn = _sqlite3.connect(route_env.db_path)
    try:
        conn.execute(
            "UPDATE files SET status = 'indexed' "
            "WHERE id = ? AND status = 'processing'",
            (file_id,),
        )
        conn.commit()
    finally:
        conn.close()

    # The live generation survives: no live-table delete fired (the pin
    # above), the pre-reindex vector row is intact, and the seeded atoms
    # survive regardless of where the retire pass got interrupted.
    assert store.row_count(file_id) == 1
    assert route_env.atom_count(file_id) >= 1
    assert route_env.file_status(file_id) == "indexed"
    assert not processor.is_cancel_requested(file_id)
