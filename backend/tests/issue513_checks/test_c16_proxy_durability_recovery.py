"""C16 - AC16 (INGEST-019): a failed proxy embedding must not be reported
complete, and bounded automatic recovery must write the missing proxy.

Contract under test (issue #513 AC16):

  Phase A - successful derivation followed by a FAILED proxy embedding (the
  embedding service returns a per-text None placeholder): the file/atom must
  NOT report enrichment complete (an incomplete/retryable status is required).

  Phase B - the startup resume path (``_resume_pending_atom_enrichment``) must
  select the SUCCEEDED atom whose durable proxy is missing (derived row with
  proxy_vector_id NULL) and re-enqueue it.

  Phase C - once re-enriched with a working embedding service, the missing
  proxy must actually be written (proxy_vector_id pinned; vector insert
  observed for the recovered atom).

Pre-fix expectation: ``_write_atom_proxies`` skips None embeddings silently,
``_sync_file_enrichment_status`` maps all-SUCCEEDED stages to 'complete'
(background_tasks.py ~977-983), and the startup resume query filters
status IN (pending, failed_retryable, skipped_not_applicable) so a SUCCEEDED
atom with a missing proxy is never re-enqueued -> the atom stays proxy-less
forever while reported complete -> FAIL.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import logging
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

# Repo root: this file lives at <repo>/backend/tests/issue513_checks/<name>.py
ROOT = Path(__file__).resolve().parents[3]
_TMPDIR = tempfile.mkdtemp(prefix="issue513_c16_")
sys.path.insert(0, str(ROOT / "backend"))


def _hermetic_env() -> None:
    """Set test env BEFORE any app import (mirrors backend/tests/conftest.py)."""
    os.environ["ADMIN_SECRET_TOKEN"] = "test-secret"
    os.environ["USERS_ENABLED"] = "false"
    os.environ["JWT_SECRET_KEY"] = "test-jwt-secret-key-for-testing-only"
    os.environ["REDIS_URL"] = ""
    os.environ["DATA_DIR"] = _TMPDIR

_WAIT_S = 6.0


class _ToggleEmbedding:
    """Embedding service double whose proxy embedding can fail on demand."""

    MAX_TEXT_LENGTH = 8192
    embedding_doc_prefix = ""

    def __init__(self) -> None:
        self.failing = True
        self.calls = 0

    async def embed_batch(self, texts, fail_fast=False):  # noqa: ANN001, ANN202
        self.calls += 1
        if self.failing:
            return [None for _ in texts], []
        return [[0.25 * (i + 1) for i in range(4)] for _ in texts], []


class _CapturingVectorStore:
    def __init__(self) -> None:
        self.inserted: list[dict] = []

    async def add_chunks_then_delete_ids(self, records, old_ids):  # noqa: ANN001, ANN202
        self.inserted.extend(records)
        return 0

    async def init_table(self, embedding_dim: int) -> None:  # noqa: ANN001
        return None


def _build_fixture(db_path: str, tmp: Path) -> dict:
    from PIL import Image

    from app.models.database import init_db
    from app.services.artifact_store import artifact_root
    from app.services.multimodal_enrichment import compute_asset_rel_path

    init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO vaults (name) VALUES ('v')")
    vault_id = conn.execute("SELECT id FROM vaults LIMIT 1").fetchone()[0]
    conn.execute("UPDATE vaults SET multimodal_provider_enabled = 1 WHERE id = ?", (vault_id,))
    conn.execute(
        "INSERT INTO files (vault_id, file_path, file_name, file_hash, file_size, status, "
        "active_generation_hash) VALUES (?, ?, ?, ?, ?, 'indexed', 'genc16')",
        (vault_id, str(tmp / "a.png"), "a.png", "hc16", 1),
    )
    file_id = conn.execute("SELECT id FROM files LIMIT 1").fetchone()[0]

    buf = io.BytesIO()
    Image.new("RGB", (32, 32)).save(buf, format="PNG")
    png = buf.getvalue()
    asset_id = hashlib.sha256(png).hexdigest()
    rel = compute_asset_rel_path(file_id, "genc16", asset_id)
    asset_path = artifact_root(vault_id) / rel
    asset_path.parent.mkdir(parents=True, exist_ok=True)
    asset_path.write_bytes(png)

    atom_id = "c16" * 10 + "deadbeef"
    conn.execute(
        "INSERT INTO document_atoms (atom_id, schema_version, file_id, generation_hash, "
        "ordinal, kind, raw_text, asset_id) VALUES (?, 1, ?, ?, 0, 'image', 'ocr', ?)",
        (atom_id, file_id, "genc16", asset_id),
    )
    conn.commit()
    atom_pk = conn.execute(
        "SELECT id FROM document_atoms WHERE atom_id = ?", (atom_id,)
    ).fetchone()[0]
    conn.close()
    return {"vault_id": vault_id, "file_id": file_id, "atom_pk": atom_pk, "atom_id": atom_id}


def _make_client():
    from app.services.multimodal_enrichment import MultimodalProviderClient

    class _OkClient(MultimodalProviderClient):
        def __init__(self) -> None:
            super().__init__(base_url="http://127.0.0.1:11434", model="m")

        async def chat_multimodal(self, messages, max_tokens: int = 1024) -> str:  # noqa: ANN202
            self._assert_policy()
            return '{"description": "a chart", "retrieval_aids": ["chart"]}'

    return _OkClient()


def _enrichment_status(db_path: str, file_id: int) -> str | None:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT enrichment_status FROM files WHERE id = ?", (file_id,)
        ).fetchone()
        return str(row[0]) if row else None
    finally:
        conn.close()


def _proxy_vector_id(db_path: str, fx: dict) -> str | None:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT proxy_vector_id FROM document_atom_enrichments "
            "WHERE file_id = ? AND generation_hash = 'genc16' AND atom_id = ?",
            (fx["file_id"], fx["atom_id"]),
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


async def _drive_atom_worker(processor, settled) -> None:  # noqa: ANN001
    """Run the atom worker until ``settled()`` or the deadline."""
    worker = asyncio.create_task(processor._atom_enrichment_worker_loop())
    try:
        deadline = time.monotonic() + _WAIT_S
        while time.monotonic() < deadline and not settled():
            await asyncio.sleep(0.1)
        await asyncio.sleep(0.3)
    finally:
        processor.shutdown_event.set()
        worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker
        processor._running = False


async def _scenario() -> str:
    """Returns a FAIL reason string, or '' when the contract holds."""
    import app.services.background_tasks as bt
    from app.config import settings
    from app.models.database import get_pool
    from app.services.multimodal_enrichment import ArtifactEnrichmentService

    tmp = Path(tempfile.mkdtemp(prefix="c16_db_"))
    db_path = str(tmp / "app.db")
    pool = get_pool(db_path, max_size=3)
    embedder = _ToggleEmbedding()
    vstore = _CapturingVectorStore()

    orig_instance = bt._processor_instance
    bt._processor_instance = None
    try:
        with patch.object(settings, "data_dir", tmp), \
                patch.object(settings, "multimodal_enrichment_enabled", True), \
                patch.object(settings, "multimodal_allowed_model_origins", ["http://127.0.0.1:11434"]), \
                patch.object(settings, "multimodal_chat_url", "http://127.0.0.1:11434"), \
                patch.object(settings, "multimodal_model", "m"), \
                patch.object(settings, "multimodal_impl_version", "1"), \
                patch.object(settings, "multimodal_prompt_version", "v1"), \
                patch.object(settings, "multimodal_schema_version", "v1"), \
                patch.object(settings, "multimodal_mode", "thinking"), \
                patch.object(settings, "multimodal_max_pixels", 100_000_000), \
                patch.object(settings, "multimodal_max_asset_bytes", 10_000_000), \
                patch.object(settings, "multimodal_max_attempts", 2), \
                patch.dict(os.environ, {"ALLOW_LOCAL_SERVICES": "1"}):

            # Fixture must be built under the patched data_dir so the asset
            # lands where resolve_confined will look for it.
            fx = _build_fixture(db_path, tmp)
            svc = ArtifactEnrichmentService(pool=pool, client=_make_client())

            # Phase A: derivation succeeds, proxy embedding fails.
            processor = bt.BackgroundProcessor(
                max_retries=1,
                retry_delay=0.05,
                pool=pool,
                vector_store=vstore,
                embedding_service=embedder,
                multimodal_service=svc,
            )
            processor.shutdown_event.clear()
            embedder.failing = True
            processor.atom_enrichment_queue.put_nowait(
                bt.AtomEnrichmentTaskItem(
                    file_id=fx["file_id"], vault_id=fx["vault_id"],
                    generation_hash="genc16", file_hash="hc16",
                    document_title="doc", attempt=0,
                )
            )

            def _a_settled() -> bool:
                status = _enrichment_status(db_path, fx["file_id"])
                return (
                    processor.atom_enrichment_queue.empty()
                    and status not in (None, "processing")
                )

            await _drive_atom_worker(processor, _a_settled)
            status = _enrichment_status(db_path, fx["file_id"])
            proxy_missing = _proxy_vector_id(db_path, fx) is None
            if status == "complete" and proxy_missing:
                return (
                    "file enrichment_status='complete' although the succeeded atom "
                    "has no durable proxy (proxy embedding failed and was skipped "
                    "silently) - proxy durability is invisible to status (INGEST-019)"
                )

            # Phase B: startup resume must re-enqueue the SUCCEEDED+missing-proxy atom.
            resume_processor = bt.BackgroundProcessor(
                max_retries=1, retry_delay=0.05, pool=pool,
                vector_store=vstore, embedding_service=embedder,
                multimodal_service=svc,
            )
            resume_processor.shutdown_event.clear()
            await resume_processor._resume_pending_atom_enrichment()
            if resume_processor.atom_enrichment_queue.qsize() < 1:
                return (
                    "startup resume did not re-enqueue the SUCCEEDED atom whose "
                    "derived proxy vector is missing - the resume filter never "
                    "selects SUCCEEDED+null-proxy atoms (INGEST-019)"
                )

            # Phase C: with the embedding service healthy, recovery writes the proxy.
            embedder.failing = False
            inserts_before = len(vstore.inserted)

            def _c_settled() -> bool:
                return _proxy_vector_id(db_path, fx) is not None

            await _drive_atom_worker(resume_processor, _c_settled)
            proxy_id = _proxy_vector_id(db_path, fx)
            if proxy_id is None or len(vstore.inserted) <= inserts_before:
                return (
                    "bounded automatic recovery did not write the missing proxy "
                    f"(proxy_vector_id={proxy_id!r}, new vector inserts="
                    f"{len(vstore.inserted) - inserts_before}) (INGEST-019)"
                )
        return ""
    finally:
        bt._processor_instance = orig_instance


def main() -> int:
    _hermetic_env()
    logging.disable(logging.CRITICAL)
    reason = asyncio.run(_scenario())
    if reason:
        print(f"C16 CHECK: FAIL: {reason}")
        return 1
    print("C16 CHECK: PASS")
    return 0


def test_c16_proxy_durability_recovery() -> None:
    assert main() == 0


if __name__ == "__main__":
    raise SystemExit(main())
