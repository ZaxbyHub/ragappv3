"""Issue #693 non-frozen edge-case probes (B04 extras).

Companion to the frozen checks C1-C11: these pin the reviewer-mandated edge
cases from the approved fix plan — legacy-duplicate convergence, the
equal-count-different-ids state, rebuild-target guard awareness, partial
generation repair, `_check_duplicate`'s widening, cross-thread reconcile,
the scan gate's pass-through/rejection matrix (including case-variant
extensions), the widened partial unique index's positive/rollback/staleness
behavior, and the path-form-independent enqueue dedupe key.

Deliberately NOT inside the frozen test files: additive names here must
never collide with the frozen pytest selectors.
"""

from __future__ import annotations

import asyncio
import io
import os
import sqlite3
import sys
import tempfile
import threading
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

os.environ.setdefault("ADMIN_SECRET_TOKEN", "test-secret")
os.environ.setdefault("USERS_ENABLED", "false")
os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret-key-for-testing-only")
os.environ.setdefault("REDIS_URL", "")

from tests.test_b04_idempotent_ingest import _FakeEmbeddingService  # noqa: E402

_DIM = 8


def _record(rec_id: str, file_id: int, text: str, dim: int = _DIM) -> dict:
    return {
        "id": rec_id,
        "text": text,
        "file_id": str(file_id),
        "vault_id": "1",
        "chunk_index": 0,
        "metadata": "{}",
        "embedding": [((i + 1) * 0.25) for i in range(dim)],
    }


def _make_db(tag: str) -> str:
    tmp = Path(tempfile.mkdtemp(prefix=f"b04x_{tag}_"))
    db_path = str(tmp / "app.db")
    from app.models.database import init_db

    init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO vaults (name) VALUES (?)", (f"v_{tag}",))
    conn.commit()
    conn.close()
    return db_path


def _vault_id(db_path: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return int(conn.execute("SELECT id FROM vaults LIMIT 1").fetchone()[0])
    finally:
        conn.close()


async def test_same_hash_legacy_duplicates_converge(tmp_path):
    """A store already holding the bug's duplicate rows converges to one."""
    from app.services.vector_store import VectorStore

    store = VectorStore(db_path=tmp_path / "lancedb")
    await store.init_table(_DIM)
    prefix = "7_aaaa1111_"
    # The legacy-duplicate state: three copies of ONE generation id (what the
    # pre-#693 append produced), plus the files row the ids belong to.
    for _ in range(3):
        await store.add_chunks([_record(f"{prefix}default_0", 7, "dup")])
    assert await store.count_by_file("7") == 3
    await store.add_chunks(
        [_record(f"{prefix}default_0", 7, "dup")], generation_prefix=prefix
    )
    assert await store.count_by_file("7") == 1


async def test_equal_count_different_ids_resets(tmp_path):
    """Equal row counts with different id sets are NOT a complete state."""
    from app.services.vector_store import VectorStore

    store = VectorStore(db_path=tmp_path / "lancedb")
    await store.init_table(_DIM)
    prefix = "9_bbbb2222_"
    await store.add_chunks(
        [_record(f"{prefix}scaleA_{i}", 9, f"a{i}") for i in range(3)]
    )
    # Same count (3), different ids (scaleB): the guard must reset + append,
    # not no-op on the count equality.
    await store.add_chunks(
        [_record(f"{prefix}scaleB_{i}", 9, f"b{i}") for i in range(3)],
        generation_prefix=prefix,
    )
    assert await store.count_by_file("9") == 3
    old_rows = await store.get_chunks_by_uid(
        [f"{prefix}scaleA_{i}" for i in range(3)]
    )
    new_rows = await store.get_chunks_by_uid(
        [f"{prefix}scaleB_{i}" for i in range(3)]
    )
    assert old_rows == []
    assert len(new_rows) == 3


async def test_partial_generation_repaired(tmp_path):
    """A crashed mid-add generation (subset of ids) is reset and restored."""
    from app.services.vector_store import VectorStore

    store = VectorStore(db_path=tmp_path / "lancedb")
    await store.init_table(_DIM)
    prefix = "11_cccc3333_"
    incoming = [_record(f"{prefix}default_{i}", 11, f"c{i}") for i in range(3)]
    await store.add_chunks(incoming[:1])  # crash left only chunk 0
    await store.add_chunks(incoming, generation_prefix=prefix)
    assert await store.count_by_file("11") == 3


async def test_guard_counts_rebuild_target_not_live_table(tmp_path):
    """A complete LIVE generation must not silence a rebuild-target add."""
    from app.services.vector_store import VectorStore

    store = VectorStore(db_path=tmp_path / "lancedb")
    await store.init_table(_DIM)
    prefix = "13_dddd4444_"
    records = [_record(f"{prefix}default_{i}", 13, f"d{i}") for i in range(2)]
    await store.add_chunks(records, generation_prefix=prefix)
    assert await store.count_by_file("13") == 2

    handle = await store.begin_dimension_rebuild(_DIM)
    try:
        await store.add_chunks(
            records, target=handle, generation_prefix=prefix
        )
        target_count = await handle.table.count_rows("file_id = '13'")
        assert target_count == 2, "rebuild temp table must receive the rows"
        assert await store.count_by_file("13") == 2
    finally:
        await store.abort_dimension_rebuild(handle)


async def test_check_duplicate_reports_partial_row():
    """`_check_duplicate` (the scan/email worker path) treats partial as live."""
    from app.models.database import get_pool
    from app.services.document_processor import DocumentProcessor
    from app.services.vector_store import VectorStore

    db_path = _make_db("chkdup")
    tmp = Path(db_path).parent
    vid = _vault_id(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO files (vault_id, file_path, file_name, file_hash, file_size, status) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (vid, str(tmp / "p.txt"), "p.txt", "feedfacefeed", 5, "partial"),
    )
    conn.commit()
    conn.close()

    pool = get_pool(db_path, max_size=3)
    processor = DocumentProcessor(
        pool=pool,
        embedding_service=_FakeEmbeddingService(_DIM),
        vector_store=VectorStore(db_path=tmp / "lancedb"),
    )
    with pool.connection() as conn:
        row = processor._check_duplicate("feedfacefeed", conn, vault_id=vid)
    assert row is not None


async def test_cross_thread_reconcile_during_stop_leaves_watcher_running():
    """reconcile from a worker thread during a stop drain restarts after."""
    from app.config import settings
    from app.services.file_watcher import FileWatcher

    with patch.object(settings, "auto_scan_enabled", True):
        fw = FileWatcher(processor=MagicMock(), pool=None)

        async def _slow_scan():
            await asyncio.sleep(0.3)

        fw.scan_once = _slow_scan
        await fw.start()
        t = asyncio.create_task(fw.stop())
        await asyncio.sleep(0)  # stop() begins draining
        # A sync settings-save handler runs on a worker thread: reconcile is
        # invoked off-loop there.
        thread = threading.Thread(target=fw.reconcile, args=(settings,))
        thread.start()
        await t
        thread.join()
        await asyncio.sleep(0.5)
        assert fw.is_running
        assert not fw._stopping
        await fw.stop()


def _zip_bytes(members: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in members.items():
            zf.writestr(name, content)
    return buf.getvalue()


async def test_scan_gate_matrix(tmp_path):
    """One scan: valid files enqueue; invalid/over-rejecting cases do not."""
    from app.config import settings
    from app.models.database import SQLiteConnectionPool, init_db
    from app.services.file_watcher import FileWatcher

    with patch.object(settings, "data_dir", tmp_path):
        db_path = str(settings.sqlite_path)
        init_db(db_path)
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO vaults (name) VALUES ('v')")
        conn.commit()
        conn.close()
        vid = _vault_id(db_path)
        uploads = settings.vault_uploads_dir(vid)

        # Positive controls: plain text, markup-headed html (NOT an image
        # extension — the reject-header screen must not fire), a valid docx.
        (uploads / "good.txt").write_text("plain attachment", encoding="utf-8")
        (uploads / "page.html").write_text(
            "<!DOCTYPE html><html><body>doc</body></html>", encoding="utf-8"
        )
        valid_docx = io.BytesIO()
        with zipfile.ZipFile(valid_docx, "w") as zf:
            zf.writestr("word/document.xml", "<doc/>")
        (uploads / "good.docx").write_bytes(valid_docx.getvalue())

        # Negative controls: junk ZIP with an UPPERCASE .DOCX name (the
        # case-folded extension must still hit the OOXML member check), and a
        # markup-headed payload with an UPPERCASE .JPG name (must hit both
        # image screens once folded).
        (uploads / "BAD.DOCX").write_bytes(_zip_bytes({"junk.txt": "x"}))
        (uploads / "PHOTO.JPG").write_text("<html>not an image", encoding="utf-8")
        (uploads / "bad.docx").write_bytes(_zip_bytes({"junk.txt": "x"}))

        pool = SQLiteConnectionPool(db_path, max_size=2)
        processor = AsyncMock()
        fw = FileWatcher(processor=processor, pool=pool)
        await fw.scan_once()

    enqueued = {
        call.kwargs.get("file_path", call.args[0] if call.args else None)
        for call in processor.enqueue.await_args_list
    }
    enqueued_names = {Path(str(p)).name for p in enqueued if p}
    assert enqueued_names == {"good.txt", "page.html", "good.docx"}


async def test_widened_unique_index_positive_and_staleness(tmp_path):
    """Fresh DB gets the widened index; it rejects a partial+indexed pair;
    a second run is a staleness no-op."""
    from app.models.database import _widen_files_hash_vault_unique_index, init_db

    db_path = str(tmp_path / "a.db")
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    _widen_files_hash_vault_unique_index(conn)
    conn.row_factory = sqlite3.Row
    sql_before = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='idx_files_hash_vault_indexed'"
    ).fetchone()["sql"]
    assert "partial" in sql_before

    conn.execute("INSERT INTO vaults (name) VALUES ('v')")
    vid = int(conn.execute("SELECT id FROM vaults LIMIT 1").fetchone()[0])
    conn.execute(
        "INSERT INTO files (vault_id, file_path, file_name, file_hash, file_size, status) "
        "VALUES (?, 'a', 'a', 'h', 1, 'indexed')",
        (vid,),
    )
    try:
        conn.execute(
            "INSERT INTO files (vault_id, file_path, file_name, file_hash, file_size, status) "
            "VALUES (?, 'b', 'b', 'h', 1, 'partial')",
            (vid,),
        )
        raised = False
    except sqlite3.IntegrityError:
        raised = True
        conn.rollback()
    assert raised, "partial+indexed pair for one hash+vault must be rejected"

    # Staleness: the second run must not rebuild (sql text unchanged).
    _widen_files_hash_vault_unique_index(conn)
    sql_after = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='idx_files_hash_vault_indexed'"
    ).fetchone()["sql"]
    assert sql_after == sql_before
    conn.close()


async def test_widened_unique_index_rollback_restores_narrow(tmp_path):
    """Legacy DB with a partial+indexed pair keeps its NARROW index."""
    from app.models.database import _widen_files_hash_vault_unique_index, init_db

    db_path = str(tmp_path / "b.db")
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    # The pre-#693 narrow index, then rows only IT considers legal.
    conn.execute(
        "CREATE UNIQUE INDEX idx_files_hash_vault_indexed ON files(file_hash, vault_id) "
        "WHERE file_hash IS NOT NULL AND status = 'indexed'"
    )
    conn.execute("INSERT INTO vaults (name) VALUES ('v')")
    vid = int(conn.execute("SELECT id FROM vaults LIMIT 1").fetchone()[0])
    conn.execute(
        "INSERT INTO files (vault_id, file_path, file_name, file_hash, file_size, status) "
        "VALUES (?, 'a', 'a', 'h', 1, 'indexed')",
        (vid,),
    )
    conn.execute(
        "INSERT INTO files (vault_id, file_path, file_name, file_hash, file_size, status) "
        "VALUES (?, 'b', 'b', 'h', 1, 'partial')",
        (vid,),
    )
    conn.commit()

    _widen_files_hash_vault_unique_index(conn)
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='idx_files_hash_vault_indexed'"
    ).fetchone()
    assert row is not None, "rollback must leave the narrow index in place"
    assert "partial" not in row["sql"]
    conn.close()


async def test_enqueue_dedupe_across_path_forms():
    """Email (relative str) + scan (resolved) hold ONE job via path_key."""
    from app.config import settings
    from app.models.database import SQLiteConnectionPool, init_db
    from app.services.background_tasks import BackgroundProcessor
    from app.services.file_watcher import FileWatcher
    from app.services.job_lease import ensure_jobs_schema

    tmp = Path(tempfile.mkdtemp(prefix="b04x_dedup_"))
    old_cwd = os.getcwd()
    old_dir = settings.data_dir
    os.chdir(tmp)
    try:
        settings.data_dir = Path("data")
        db_path = str(settings.sqlite_path)
        init_db(db_path)
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO vaults (name) VALUES ('v')")
        ensure_jobs_schema(conn)
        conn.commit()
        conn.close()
        vid = _vault_id(db_path)
        att = settings.vault_uploads_dir(vid) / "att2.txt"
        att.write_text("attachment", encoding="utf-8")

        pool = SQLiteConnectionPool(db_path, max_size=4)
        with patch.object(settings, "ingestion_job_lease_enabled", True):
            bp = BackgroundProcessor(pool=pool, retry_delay=0.05)
            # Email producer: raw relative string. Double-poll guard too.
            assert await bp.enqueue(str(att), vault_id=vid, source="email")
            assert await bp.enqueue(str(att), vault_id=vid, source="email")
            # Scan producer: resolved absolute path (the watcher resolves).
            fw = FileWatcher(processor=bp, pool=pool)
            await fw.scan_once()
        conn = sqlite3.connect(db_path)
        n = int(
            conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE queue='ingestion'"
            ).fetchone()[0]
        )
        conn.close()
        assert n == 1
    finally:
        settings.data_dir = old_dir
        os.chdir(old_cwd)
