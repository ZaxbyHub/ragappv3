"""Acceptance check C11 (issue #1131 ABSORB extension): a later vault
reindex must not deterministically re-fail on a duplicate-settled loser
row.

The reindex SELECT is ``status IN ('indexed','partial','error')``
(background_tasks.py:5144-5170, four variants), so a duplicate-settled
loser (status='error', error_message LIKE 'DUPLICATE_FILE:%') is
re-selected by EVERY later vault reindex, deterministically re-fails at
the pre-vector duplicate guard, settles the job 'failed', and blocks the
identity record + readiness lift (issue #736 gate).

Frozen contract: the settled loser is NOT re-selected — the reindex run
over a vault holding the indexed owner plus the settled loser completes
for the owner without producing a terminal duplicate failure. (The AC also
permits a non-blocking design; that variant requires a CHECK_WRONG amend
of this row, to be scoped with the plan critic.)

This check drives the REAL ``_reindex_embed_all`` over a migrations DB
(same harness as the #1112 e2e leg, live dim == probe dim so the run does
NOT stage). RED at base (the loser is re-selected → duplicate failure →
status 'failed'), GREEN after the fix.
"""

import hashlib
import os
import sqlite3
import sys
import types

# Add parent directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Stub optional dependencies same as other test files
try:
    import lancedb  # noqa: F401
except ImportError:
    sys.modules["lancedb"] = types.ModuleType("lancedb")

try:
    import pyarrow  # noqa: F401
except ImportError:
    sys.modules["pyarrow"] = types.ModuleType("pyarrow")

from app.models.database import SQLiteConnectionPool, run_migrations
from app.services.background_tasks import BackgroundProcessor


def _connect(db_path):
    conn = sqlite3.connect(db_path, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _insert_file(conn, *, file_hash, status, path, error_message=None):
    cur = conn.execute(
        "INSERT INTO files (vault_id, file_path, file_name, file_hash, file_size,"
        " status, phase, source, error_message) VALUES (1, ?, ?, ?, 1, ?,"
        " 'queued', 'upload', ?)",
        (path, path.rsplit("/", 1)[-1], file_hash, status, error_message),
    )
    conn.commit()
    return int(cur.lastrowid)


class _RecordingVectorStore:
    """Recording stub (live dim == embed dim so the run does not stage)."""

    def __init__(self):
        self.calls = []

    def _record(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))

    async def init_table(self, *a, **k):
        self._record("init_table", *a, **k)

    async def add_chunks(self, *a, **k):
        self._record("add_chunks", *a, **k)
        return {}

    async def delete_by_file(self, *a, **k):
        self._record("delete_by_file", *a, **k)

    async def delete_old_generation_by_file(self, *a, **k):
        self._record("delete_old_generation_by_file", *a, **k)
        return 0

    async def count_by_file(self, *a, **k):
        self._record("count_by_file", *a, **k)
        return 1

    async def get_live_embedding_dim(self, *a, **k):
        self._record("get_live_embedding_dim", *a, **k)
        return 2  # == embed stub dim -> in-place path, no staging

    async def record_embedding_metadata(self, *a, **k):
        # CHECK_WRONG amend: with the ABSORB exclusion the run now reaches
        # the completion path's identity-record step, which the original
        # stub never modeled (at base the run never got past the duplicate
        # re-fail).
        self._record("record_embedding_metadata", *a, **k)

    async def mark_ready(self, *a, **k):
        self._record("mark_ready", *a, **k)


async def _fake_schema_file(file_path, file_id, generation_hash, parser_fingerprint):  # noqa: ARG001
    from app.services.chunking import ProcessedChunk
    from app.services.document_processor import ParsedDocument

    chunks = [
        ProcessedChunk(
            text="x",
            metadata={"section": "s", "total_chunks": 1},
            chunk_index=0,
        )
    ]
    return chunks, "x", ParsedDocument(atoms=(), parser_fingerprint=parser_fingerprint)


async def test_reindex_does_not_refail_settled_duplicate(tmp_path, monkeypatch):
    """AC11 (C11): a vault reindex after a duplicate settle completes for
    the owner instead of deterministically re-failing on the settled row."""
    db_path = str(tmp_path / "absorb.db")
    run_migrations(db_path)
    conn = _connect(db_path)
    pool = SQLiteConnectionPool(db_path, max_size=4)
    try:
        content = b"absorb-resel-section-bytes-982c11"
        path = tmp_path / "doc.pdf"
        path.write_bytes(content)
        file_hash = hashlib.sha256(content).hexdigest()
        owner_id = _insert_file(
            conn, file_hash=file_hash, status="indexed", path=str(path)
        )
        loser_id = _insert_file(
            conn,
            file_hash=file_hash,
            status="error",
            path=str(path),
            error_message="DUPLICATE_FILE: settled by the finalization race",
        )
        assert owner_id != loser_id

        bp = BackgroundProcessor(pool=pool, retry_delay=0.05)
        stub = _RecordingVectorStore()
        bp.processor.vector_store = stub

        class _EmbedStub:
            async def embed_batch(self, texts, fail_fast=True, **kwargs):  # noqa: ARG002
                embeddings = [[0.1, 0.2] for _ in texts]
                return embeddings if fail_fast else (embeddings, [])

        bp.processor.embedding_service = _EmbedStub()
        monkeypatch.setattr(bp.processor, "_is_schema_file", lambda p: True)
        monkeypatch.setattr(bp.processor, "_process_schema_file", _fake_schema_file)

        status, result, error = await bp._reindex_embed_all(1, vault_id=1)  # noqa: SLF001

        assert status == "completed", (
            "982C11: the reindex deterministically re-failed on the "
            "duplicate-settled loser row (ABSORB: the settled row is "
            f"re-selected by the reindex SELECT and blocks the readiness "
            f"lift); status={status!r} result={result!r} error={error!r}"
        )
        assert result.get("duplicate_file_ids", []) == [], (
            f"982C11: the settled loser (id={loser_id}) must not be "
            f"re-selected as a fresh duplicate; result={result!r}"
        )
    finally:
        pool.close_all()
        conn.close()
