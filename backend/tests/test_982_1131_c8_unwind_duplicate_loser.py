"""Acceptance check C8 (issue #1131 CC-001): the finalization-race loser's
orphaned vector generation must be unwound (#1131 AC1).

At base, ``_settle_duplicate_finalize_race`` settles the loser's files row
to ``status='error'`` (DUPLICATE_FILE code) but NEVER unwinds the vector
generation the loser already wrote — the loser's chunks keep answering
searches for a file the UI reports as failed. The frozen fix contract is a
generation-scoped unwind helper on the document processor
(``_unwind_duplicate_loser_generation``) invoked by the fenced backstop
when it lands the loser's row.

NEW-SURFACE: the helper does not exist at base, so this file fails at
COLLECTION (ImportError) — the expected pre-fix state is ERROR, not RED.
On the fixed tree the test below drives the same executed shape as the
#1112 finalization-backstop leg (real ``process_existing_file`` over a
migrations DB, schema-file parse stub, embed stub, recording vector store)
with the loser writing its generation BEFORE the finalization collision,
and asserts the generation is unwound (a delete-scoped call naming the
loser's file id) and the row settles to the duplicate terminal state.

Phase 4.5 note: this row needs the mandatory revert/mutation probe (delete
the unwind call → this test must go RED).
"""

import os
import sqlite3
import sys
import types
from unittest.mock import AsyncMock

import pytest

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

# The frozen contract: the unwind seam is a named helper on the document
# processor. Module-level import so the pre-fix tree fails at COLLECTION
# (pytest exit 2 / usage error) rather than running a vacuous pass.
from app.services.document_processor import (
    DocumentProcessor,
    _unwind_duplicate_loser_generation,  # noqa: F401
)


class _RecordingVectorStore:
    """Records every vector-store mutation call with its arguments (and
    returns the shapes the pipeline reads); nothing executes."""

    def __init__(self):
        self.calls = []  # (name, args, kwargs)

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

    async def delete_generation_by_file_locked(self, *a, **k):
        self._record("delete_generation_by_file_locked", *a, **k)

    def write_locked(self):
        """Async-context double for the store's public lock context."""
        import contextlib

        @contextlib.asynccontextmanager
        async def _ctx():
            self._record("write_locked_enter")
            try:
                yield self
            finally:
                self._record("write_locked_exit")

        return _ctx()

    async def count_by_file(self, *a, **k):
        self._record("count_by_file", *a, **k)
        return 1

    async def get_live_embedding_dim(self, *a, **k):
        self._record("get_live_embedding_dim", *a, **k)
        return 2


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


async def test_finalization_race_loser_generation_unwound(tmp_path, monkeypatch):
    """AC8 (C8): a loser that already wrote vectors is unwound when the
    fenced backstop settles its row to the duplicate terminal state."""
    db_path = str(tmp_path / "unwind.db")
    run_migrations(db_path)
    conn = sqlite3.connect(db_path, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    pool = SQLiteConnectionPool(db_path, max_size=4)
    try:
        import hashlib

        content = b"finalization-race-loser-bytes-982c8"
        path = tmp_path / "loser.pdf"
        path.write_bytes(content)
        file_hash = hashlib.sha256(content).hexdigest()
        cur = conn.execute(
            "INSERT INTO files (vault_id, file_path, file_name, file_hash,"
            " file_size, status, phase, source) VALUES (1, ?, ?, ?, 1,"
            " 'pending', 'queued', 'upload')",
            (str(path), "loser.pdf", file_hash),
        )
        subject_id = int(cur.lastrowid)
        conn.commit()

        stub = _RecordingVectorStore()
        processor = DocumentProcessor(pool=pool, vector_store=stub)

        async def _fake_finalize(**kwargs):  # noqa: ARG001
            raise sqlite3.IntegrityError(
                "UNIQUE constraint failed: files.file_hash, files.vault_id"
            )

        monkeypatch.setattr(processor, "_finalize_indexed_success", _fake_finalize)
        monkeypatch.setattr(processor, "_process_schema_file", _fake_schema_file)
        monkeypatch.setattr(processor, "_is_schema_file", lambda p: True)

        class _EmbedStub:
            async def embed_batch(self, texts, fail_fast=True, **kwargs):  # noqa: ARG002
                embeddings = [[0.1, 0.2] for _ in texts]
                return embeddings if fail_fast else (embeddings, [])

        processor.embedding_service = _EmbedStub()

        with pytest.raises(sqlite3.IntegrityError):
            await processor.process_existing_file(
                subject_id, str(path), 1, file_hash=file_hash
            )

        # The loser DID write its generation before the collision.
        assert any(name == "add_chunks" for name, _a, _k in stub.calls), (
            "982C8: harness sanity — the loser must write vectors before the "
            f"finalization collision; calls: {stub.calls}"
        )
        # The row settles to the duplicate terminal state (#1112 contract).
        row = conn.execute(
            "SELECT status, error_message FROM files WHERE id = ?", (subject_id,)
        ).fetchone()
        assert row["status"] == "error", (
            f"982C8: the fenced backstop must settle the loser's row; got "
            f"{row['status']!r}"
        )
        assert (row["error_message"] or "").startswith("DUPLICATE_FILE:")

        # The NEW surface (amended C8, CHECK_WRONG): the loser's orphaned
        # generation is unwound through the FENCED locked primitive — a
        # delete_generation_by_file_locked call naming the loser's file id
        # AND carrying the loser's file_hash, under the write_locked()
        # context. The pre-existing delete_old_generation_by_file call
        # cannot satisfy this assertion (different method name), so
        # removing the unwind makes this leg RED.
        subject_str = str(subject_id)
        hash8 = file_hash[:8]
        unwinds = [
            (args, kwargs)
            for name, args, kwargs in stub.calls
            if name == "delete_generation_by_file_locked"
        ]
        assert unwinds, (
            "982C8: the settle path performs no fenced unwind call — the "
            "loser's orphaned generation survives the duplicate settle "
            "(#1131 CC-001); calls: "
            f"{[name for name, _a, _k in stub.calls]}"
        )
        named = [
            (args, kwargs)
            for args, kwargs in unwinds
            if subject_str in str(args) + str(kwargs)
            and (file_hash in str(args) + str(kwargs) or hash8 in str(args) + str(kwargs))
        ]
        assert named, (
            f"982C8: no delete_generation_by_file_locked call carries the "
            f"loser's identity (file_id={subject_id}, hash[:8]={hash8}); "
            f"the unwind is not generation-scoped: {stub.calls}"
        )
        assert any(
            name == "write_locked_enter" for name, _a, _k in stub.calls
        ), (
            "982C8: the unwind must run inside the store's write_locked() "
            "context (Lance-first lock order)"
        )
    finally:
        pool.close_all()
        conn.close()
