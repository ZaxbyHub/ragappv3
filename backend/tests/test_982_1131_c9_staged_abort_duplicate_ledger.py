"""Acceptance check C9 (issue #1131 RP-001): a staged rebuild abort must
compose its discovered duplicate ledger into the returned result (#1131
AC2).

At base, ``_reindex_embed_all``'s per-file ``duplicate_ids`` ledger is a
local inside the try; when the staged dimension rebuild aborts with
discovered failures it raises ``ReindexOperatorGuidance`` and the outer
tail returns ``("failed", {}, exc)`` — the global-empty shape — so the
duplicates discovered on the staged path never reach the terminal
result_json (the lease settlers' compose arms skip empty dicts).

The frozen fix contract (per the staged-narrowed-retry safety proof, which
RULES OUT a narrowed staging retry): carry ``duplicate_file_ids`` out of
the aborted ``_reindex_embed_all`` on the global-failure path; the retry
scope must NOT widen (no narrowing of the staged table).

This check drives the REAL producer over a migrations DB (same harness as
the #1112 e2e leg): an indexed owner plus a same-hash target that trips
the pre-vector duplicate guard, with the live dimension probe reporting a
DIFFERENT dimension than the embed stub so the run enters the staged
rebuild, discovers the duplicate, and aborts. RED at base (the returned
result dict is empty), GREEN after the fix.
"""

import hashlib
import os
import sqlite3
import sys
import types
from types import SimpleNamespace

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


class _StagedRecordingVectorStore:
    """Recording stub for the staged-rebuild lifecycle: live dimension
    intentionally DIFFERS from the embed stub (2) so the run stages."""

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
        return 3  # != embed stub dim (2) -> staged rebuild path

    async def begin_dimension_rebuild(self, *a, **k):
        self._record("begin_dimension_rebuild", *a, **k)
        return SimpleNamespace(table_name="staged_stub")

    async def commit_dimension_rebuild(self, *a, **k):
        self._record("commit_dimension_rebuild", *a, **k)

    async def abort_dimension_rebuild(self, *a, **k):
        self._record("abort_dimension_rebuild", *a, **k)


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


async def test_staged_abort_composes_duplicate_ledger(tmp_path, monkeypatch):
    """AC9 (C9): the aborted staged rebuild must carry its discovered
    duplicate_file_ids out in the returned result dict."""
    db_path = str(tmp_path / "staged.db")
    run_migrations(db_path)
    conn = _connect(db_path)
    pool = SQLiteConnectionPool(db_path, max_size=4)
    try:
        content = b"staged-abort-duplicate-bytes-982c9"
        path = tmp_path / "doc.pdf"
        path.write_bytes(content)
        file_hash = hashlib.sha256(content).hexdigest()
        owner_id = _insert_file(
            conn, file_hash=file_hash, status="indexed", path=str(path)
        )
        target_id = _insert_file(
            conn,
            file_hash=file_hash,
            status="error",
            path=str(path),
            # Plain transient failure (amended C9, CHECK_WRONG): a
            # DUPLICATE_FILE-prefixed error_message is excluded from reindex
            # selection by the #1131 ABSORB fix, which would make this leg
            # discover no duplicate and fail its own sanity assertions. A
            # real staged-reindex duplicate discovery is a transiently-failed
            # file that is re-selected, re-embedded, and hits the duplicate
            # guard live.
            error_message="transient parse failure (probe fixture)",
        )
        assert owner_id != target_id

        bp = BackgroundProcessor(pool=pool, retry_delay=0.05)
        stub = _StagedRecordingVectorStore()
        bp.processor.vector_store = stub

        class _EmbedStub:
            async def embed_batch(self, texts, fail_fast=True, **kwargs):  # noqa: ARG002
                embeddings = [[0.1, 0.2] for _ in texts]  # probe dim 2 != live 3
                return embeddings if fail_fast else (embeddings, [])

        bp.processor.embedding_service = _EmbedStub()
        monkeypatch.setattr(bp.processor, "_is_schema_file", lambda p: True)
        monkeypatch.setattr(bp.processor, "_process_schema_file", _fake_schema_file)

        # All-vaults form: a vault-scoped reindex REFUSES the dimension
        # migration outright (ReindexOperatorGuidance before any staging),
        # so the staged path is only reachable with vault_id=None.
        status, result, error = await bp._reindex_embed_all(1, vault_id=None)  # noqa: SLF001

        # Harness sanity: this really was the staged-abort path (a staged
        # rebuild began, discovered the duplicate, and was aborted).
        assert any(
            name == "begin_dimension_rebuild" for name, _a, _k in stub.calls
        ), (
            f"982C9: harness sanity — the staged rebuild must begin; calls: "
            f"{[name for name, _a, _k in stub.calls]}"
        )
        assert any(
            name == "abort_dimension_rebuild" for name, _a, _k in stub.calls
        ), (
            "982C9: harness sanity — the staged rebuild must abort after the "
            f"duplicate failure; calls: {[name for name, _a, _k in stub.calls]}"
        )
        assert status == "failed", (
            f"982C9: the aborted staged run reports failure; got {status!r}"
        )
        assert result.get("duplicate_file_ids") == [target_id], (
            "982C9: the staged-abort global-failure path discarded the "
            "discovered duplicate ledger (returned result "
            f"{result!r}, error={error!r}) — duplicates found on the staged "
            "path must compose into the terminal result_json without "
            "widening the retry scope (#1131 RP-001)"
        )
    finally:
        pool.close_all()
        conn.close()
