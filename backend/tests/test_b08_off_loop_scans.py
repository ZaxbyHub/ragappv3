"""Issue #697 acceptance checks (Workstream B PR 8) — off-event-loop scans.

AC1 (test_record_file_centroid_runs_off_event_loop): during a real
reprocess ingest (the C9 harness shape — real VectorStore, init_db DB,
``DocumentProcessor.process_existing_file`` with ``_process_document_file``
AsyncMock-ed), the near-duplicate ``record_file_centroid`` call must run
OFF the event loop. A recorder wrapping the real function appends True
when ``asyncio.get_running_loop()`` succeeds in the calling thread; the
on-loop call count must be 0.

AC2 (test_second_ingest_does_not_rescan_unmatched_legacy_text): on a
run_migrations DB, the text-fingerprint fallback scan must only
fingerprint the ingested file's own text. When file y is recorded after
file x, the count of ``_text_fingerprint`` calls during y's ingest must
be 1 (y's own text) — not y plus every unmatched legacy file in the vault.

AC5 (test_list_documents_resolves_enrichment_off_loop): route-function
direct ``list_documents`` over 3 seeded rows must resolve per-file
enrichment OFF the event loop (``is_enrichment_enabled_for_file`` patched
to an on-loop recorder); the on-loop call count must be 0.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

_B08_ENV = {
    "ADMIN_SECRET_TOKEN": "test-secret",
    "USERS_ENABLED": "false",
    "JWT_SECRET_KEY": "test-jwt-secret-key-for-testing-only",
    "REDIS_URL": "",
}


@pytest.fixture(autouse=True, scope="module")
def _b08_hermetic_env():
    """Set hermetic env BEFORE any app import; restore afterwards.

    Mirrors backend/tests/conftest.py and the C9 harness's ``_hermetic_env``
    pattern: every app import in this module is function-local, so this
    module-scoped fixture has run (and the env keys are set) before the
    first ``app.*`` module is imported. All previous values are restored on
    teardown so later modules see the conftest-provided environment.
    """
    saved = {key: os.environ.get(key) for key in _B08_ENV}
    os.environ.update(_B08_ENV)
    yield
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


class _FakeEmbeddingService:
    """Deterministic, no-network embedding service (C9 harness shape)."""

    MAX_TEXT_LENGTH = 8192
    embedding_doc_prefix = ""

    def __init__(self, dim: int) -> None:
        self._dim = dim

    async def embed_batch(self, texts, fail_fast=False):  # noqa: ANN001, ANN202
        return [[((i + 1) * 0.25) for i in range(self._dim)] for _ in texts], []


async def test_record_file_centroid_runs_off_event_loop() -> None:
    """AC1 (issue #697): the ingest-time centroid scan must be off-loop."""
    from app.config import settings
    from app.models.database import get_pool, init_db
    from app.services import near_duplicates
    from app.services.chunking import ProcessedChunk
    from app.services.document_artifacts import ParsedDocument
    from app.services.document_processor import DocumentProcessor
    from app.services.vector_store import VectorStore

    tmp = Path(tempfile.mkdtemp(prefix="b08_ac1_"))
    db_path = str(tmp / "app.db")
    init_db(db_path)
    # issue #704 (T1-25-S2-10): finalize writes parsed_text in the status
    # transaction; the column is migration-added.
    from app.models.database import run_migrations as _run_migrations

    _run_migrations(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO vaults (name) VALUES ('v')")
    vault_id = conn.execute("SELECT id FROM vaults LIMIT 1").fetchone()[0]
    conn.execute(
        "INSERT INTO files (vault_id, file_path, file_name, file_hash, file_size, status) "
        "VALUES (?, ?, ?, ?, ?, 'indexed')",
        (vault_id, str(tmp / "doc.txt"), "doc.txt", "hashb08ac1", 12),
    )
    conn.commit()
    file_id = conn.execute("SELECT id FROM files LIMIT 1").fetchone()[0]
    conn.close()
    upload_path = tmp / "doc.txt"
    upload_path.write_text("seed content", encoding="utf-8")

    # SAME dim as the seeded vector table so the #691 fail-closed dimension
    # guard does not refuse the ingest.
    dim = 4
    real = near_duplicates.record_file_centroid
    seen: list[bool] = []

    def recorder(*args, **kwargs):  # noqa: ANN002, ANN003
        try:
            asyncio.get_running_loop()
            on_loop = True
        except RuntimeError:
            on_loop = False
        seen.append(on_loop)
        return real(*args, **kwargs)

    chunk = ProcessedChunk(
        text="reindexed content with the embedding model",
        metadata={"chunk_scale": "default", "raw_text": "reindexed content"},
        chunk_index=0,
    )
    pool = get_pool(db_path, max_size=3)
    store = VectorStore(db_path=tmp / "lancedb")
    try:
        with (
            patch.object(settings, "data_dir", tmp),
            patch.object(settings, "wiki_enabled", False),
            patch.object(settings, "wiki_compile_on_ingest", False),
            patch.object(settings, "kms_enabled", False),
            patch.object(settings, "multi_scale_indexing_enabled", False),
            patch.object(settings, "contextual_chunking_enabled", False),
            patch.object(settings, "optimize_mode", "manual"),
        ):
            await store.init_table(dim)
            await store.add_chunks(
                [
                    {
                        "id": f"{file_id}_0",
                        "text": "seed content",
                        "file_id": str(file_id),
                        "vault_id": "1",
                        "chunk_index": 0,
                        "metadata": "{}",
                        "embedding": [((i + 1) * 0.25) for i in range(dim)],
                    }
                ]
            )
            processor = DocumentProcessor(
                pool=pool,
                embedding_service=_FakeEmbeddingService(dim=dim),
                vector_store=store,
            )
            with (
                patch.object(
                    processor,
                    "_process_document_file",
                    new=AsyncMock(
                        return_value=(
                            [chunk],
                            "reindexed content",
                            ParsedDocument(atoms=()),
                        )
                    ),
                ),
                patch(
                    "app.services.document_processor.compute_file_hash",
                    return_value="hashb08ac1",
                ),
                patch("app.services.document_processor.compute_parent_windows"),
                patch.object(near_duplicates, "record_file_centroid", recorder),
            ):
                await processor.process_existing_file(
                    file_id=file_id,
                    file_path=str(upload_path),
                    vault_id=vault_id,
                )
    finally:
        try:
            pool.close_all()
        except Exception:  # noqa: BLE001 - best-effort cleanup only
            pass
        shutil.rmtree(tmp, ignore_errors=True)
    on_loop_count = sum(1 for flag in seen if flag)
    assert on_loop_count == 0


def test_second_ingest_does_not_rescan_unmatched_legacy_text() -> None:
    """AC2 (issue #697): only y's own text is fingerprinted on y's ingest."""
    from app.models.database import run_migrations
    from app.services import near_duplicates

    tmp = Path(tempfile.mkdtemp(prefix="b08_ac2_"))
    db_path = str(tmp / "app.db")
    run_migrations(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("INSERT INTO vaults (name) VALUES ('v')")
        vid = conn.execute("SELECT id FROM vaults LIMIT 1").fetchone()[0]
        for i in range(5):
            conn.execute(
                "INSERT INTO files "
                "(vault_id, file_path, file_name, file_hash, file_size, status, parsed_text) "
                "VALUES (?, ?, ?, ?, ?, 'indexed', ?)",
                (
                    vid,
                    f"/b08ac2/legacy{i}.txt",
                    f"legacy{i}.txt",
                    f"hash-l{i}",
                    10,
                    f"completely unrelated legacy corpus number {i} with padding words",
                ),
            )
        conn.execute(
            "INSERT INTO files "
            "(vault_id, file_path, file_name, file_hash, file_size, status, parsed_text) "
            "VALUES (?, ?, ?, ?, ?, 'indexed', ?)",
            (
                vid,
                "/b08ac2/x.txt",
                "x.txt",
                "hash-x",
                10,
                "the quick brown fox jumps over the lazy dog",
            ),
        )
        conn.execute(
            "INSERT INTO files "
            "(vault_id, file_path, file_name, file_hash, file_size, status, parsed_text) "
            "VALUES (?, ?, ?, ?, ?, 'indexed', ?)",
            (
                vid,
                "/b08ac2/y.txt",
                "y.txt",
                "hash-y",
                10,
                "the quick brown fox jumps over the lazy dog again",
            ),
        )
        conn.commit()
        ids = {r[1]: r[0] for r in conn.execute("SELECT id, file_name FROM files")}
        x = ids["x.txt"]
        y = ids["y.txt"]
        emb = [[1.0] * 8]

        near_duplicates.record_file_centroid(
            conn,
            vid,
            x,
            emb,
            document_text="the quick brown fox jumps over the lazy dog",
        )

        real_fp = near_duplicates._text_fingerprint
        holder = {"n": 0}

        def counting_fp(text):  # noqa: ANN001
            holder["n"] += 1
            return real_fp(text)

        with patch.object(near_duplicates, "_text_fingerprint", counting_fp):
            near_duplicates.record_file_centroid(
                conn,
                vid,
                y,
                emb,
                document_text="the quick brown fox jumps over the lazy dog again",
            )
        calls = holder["n"]
    finally:
        conn.close()
        shutil.rmtree(tmp, ignore_errors=True)
    assert calls == 1


async def test_list_documents_resolves_enrichment_off_loop() -> None:
    """AC5 (issue #697): list_documents enrichment resolution is off-loop."""
    from app.api.routes.documents import list_documents
    from app.models.database import get_pool, run_migrations
    from app.services import document_processor

    tmp = Path(tempfile.mkdtemp(prefix="b08_ac5_"))
    db_path = str(tmp / "app.db")
    run_migrations(db_path)
    seed = sqlite3.connect(db_path)
    seed.execute("INSERT INTO vaults (name) VALUES ('v')")
    vid = seed.execute("SELECT id FROM vaults LIMIT 1").fetchone()[0]
    for i in range(3):
        seed.execute(
            "INSERT INTO files (vault_id, file_path, file_name, file_hash, file_size, status) "
            "VALUES (?, ?, ?, ?, ?, 'indexed')",
            (vid, f"/b08ac5/f{i}.txt", f"f{i}.txt", f"hash-f{i}", 10),
        )
    seed.commit()
    seed.close()

    # CRITICAL: the conn must come from the pool — pool connections are
    # check_same_thread=False and list_documents dispatches conn.execute
    # via asyncio.to_thread; a raw sqlite3.connect conn raises
    # ProgrammingError there.
    pool = get_pool(db_path, max_size=2)
    conn = pool.get_connection()
    conn.row_factory = sqlite3.Row
    on_loop_flags: list[bool] = []

    def recorder(file_id, vault_id):  # noqa: ANN001
        try:
            asyncio.get_running_loop()
            on_loop_flags.append(True)
        except RuntimeError:
            on_loop_flags.append(False)
        return False

    async def evaluate(user, rtype, rid, action):  # noqa: ANN001
        return True

    try:
        with patch.object(
            document_processor, "is_enrichment_enabled_for_file", recorder
        ):
            await list_documents(
                vault_id=vid,
                page=1,
                per_page=50,
                search=None,
                status=None,
                tag_id=None,
                folder_id=None,
                sort_by="created_at",
                sort_order="desc",
                conn=conn,
                user={"id": 1, "role": "superadmin"},
                evaluate=evaluate,
            )
    finally:
        try:
            pool.release_connection(conn)
        except Exception:  # noqa: BLE001 - best-effort cleanup only
            pass
        try:
            pool.close_all()
        except Exception:  # noqa: BLE001 - best-effort cleanup only
            pass
        shutil.rmtree(tmp, ignore_errors=True)
    on_loop = sum(1 for flag in on_loop_flags if flag)
    assert on_loop == 0
