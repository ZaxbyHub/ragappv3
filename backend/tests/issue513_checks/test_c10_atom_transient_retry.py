"""C10 - AC10 (INGEST-011): transient atom failures must be retried
automatically within the run, bounded by the configured attempt cap.

Contract under test (issue #513 AC10):

  Using the REAL ArtifactEnrichmentService with a fake provider client whose
  first call raises a retryable MultimodalProviderError (timeout) and whose
  second call succeeds: the atom worker run must automatically make a SECOND
  attempt without restart and without manual enqueue (the atom reaches
  SUCCEEDED with a derived record). With a persistently-failing client, the
  number of automatic attempts must be capped at settings.multimodal_max_attempts.

Pre-fix expectation: enrich_atoms swallows the per-atom retryable outcome (the
atom stage is marked failed_retryable and no proxy record is returned) and the
atom worker treats the job as completed - only a restart's resume sweep would
ever re-reach the atom, so within the run calls stay at 1 and persistent
failure is never attempt-bounded -> FAIL.
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
_TMPDIR = tempfile.mkdtemp(prefix="issue513_c10_")
sys.path.insert(0, str(ROOT / "backend"))


def _hermetic_env() -> None:
    """Set test env BEFORE any app import (mirrors backend/tests/conftest.py)."""
    os.environ["ADMIN_SECRET_TOKEN"] = "test-secret"
    os.environ["USERS_ENABLED"] = "false"
    os.environ["JWT_SECRET_KEY"] = "test-jwt-secret-key-for-testing-only"
    os.environ["REDIS_URL"] = ""
    os.environ["DATA_DIR"] = _TMPDIR

MAX_ATOM_ATTEMPTS = 2
_WAIT_S = 6.0


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
        "active_generation_hash) VALUES (?, ?, ?, ?, ?, 'indexed', 'genc10')",
        (vault_id, str(tmp / "a.png"), "a.png", "hc10", 1),
    )
    file_id = conn.execute("SELECT id FROM files LIMIT 1").fetchone()[0]

    buf = io.BytesIO()
    Image.new("RGB", (32, 32)).save(buf, format="PNG")
    png = buf.getvalue()
    asset_id = hashlib.sha256(png).hexdigest()
    rel = compute_asset_rel_path(file_id, "genc10", asset_id)
    asset_path = artifact_root(vault_id) / rel
    asset_path.parent.mkdir(parents=True, exist_ok=True)
    asset_path.write_bytes(png)

    atom_id = "c10" * 10 + "deadbeef"
    conn.execute(
        "INSERT INTO document_atoms (atom_id, schema_version, file_id, generation_hash, "
        "ordinal, kind, raw_text, asset_id) VALUES (?, 1, ?, ?, 0, 'image', 'ocr', ?)",
        (atom_id, file_id, "genc10", asset_id),
    )
    conn.commit()
    atom_pk = conn.execute(
        "SELECT id FROM document_atoms WHERE atom_id = ?", (atom_id,)
    ).fetchone()[0]
    conn.close()
    return {"vault_id": vault_id, "file_id": file_id, "atom_pk": atom_pk, "atom_id": atom_id}


def _make_flaky_client(fail_times: int):
    from app.services.multimodal_enrichment import (
        MultimodalProviderClient,
        MultimodalProviderError,
    )

    response = '{"description": "a chart", "retrieval_aids": ["chart"]}'

    class _FlakyClient(MultimodalProviderClient):
        def __init__(self) -> None:
            super().__init__(base_url="http://127.0.0.1:11434", model="m")
            self.calls = 0

        async def chat_multimodal(self, messages, max_tokens: int = 1024) -> str:  # noqa: ANN202
            self.calls += 1
            self._assert_policy()
            if self.calls <= fail_times:
                raise MultimodalProviderError(
                    "provider_timeout", retryable=True, message="simulated timeout"
                )
            return response

    return _FlakyClient()


async def _drive_worker(db_path: str, tmp: Path, fail_times: int) -> dict:
    import app.services.background_tasks as bt
    from app.models.database import get_pool
    from app.services.multimodal_enrichment import ArtifactEnrichmentService

    fx = _build_fixture(db_path, tmp)
    pool = get_pool(db_path, max_size=3)
    client = _make_flaky_client(fail_times)
    svc = ArtifactEnrichmentService(pool=pool, client=client)

    orig_instance = bt._processor_instance
    bt._processor_instance = None
    processor = bt.BackgroundProcessor(
        max_retries=1, retry_delay=0.05, pool=pool, multimodal_service=svc
    )
    processor.shutdown_event.clear()
    processor.atom_enrichment_queue.put_nowait(
        bt.AtomEnrichmentTaskItem(
            file_id=fx["file_id"],
            vault_id=fx["vault_id"],
            generation_hash="genc10",
            file_hash="hc10",
            document_title="doc",
            attempt=0,
        )
    )
    worker = asyncio.create_task(processor._atom_enrichment_worker_loop())
    result = {"calls": client.calls, "fx": fx}
    try:
        deadline = time.monotonic() + _WAIT_S
        while time.monotonic() < deadline:
            await asyncio.sleep(0.1)
            if client.calls >= fail_times + 1 and _stage_succeeded(db_path, fx):
                break
            if client.calls >= MAX_ATOM_ATTEMPTS and _stage(db_path, fx) in (
                "failed_retryable",
                "failed_permanent",
            ):
                break
            if _stage_terminal(db_path, fx):
                # persistent-failure scenario: stop once attempts stop advancing
                if client.calls >= MAX_ATOM_ATTEMPTS:
                    break
        await asyncio.sleep(0.3)
        result["calls"] = client.calls
        return result
    finally:
        processor.shutdown_event.set()
        worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker
        processor._running = False
        bt._processor_instance = orig_instance


def _stage(db_path: str, fx: dict) -> str | None:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT status FROM ingestion_stage_states WHERE file_id = ? "
            "AND atom_id = ? AND stage = 'enrich'",
            (fx["file_id"], fx["atom_pk"]),
        ).fetchone()
        return str(row[0]) if row else None
    finally:
        conn.close()


def _stage_succeeded(db_path: str, fx: dict) -> bool:
    return _stage(db_path, fx) == "succeeded"


def _stage_terminal(db_path: str, fx: dict) -> bool:
    return _stage(db_path, fx) in (
        "failed_permanent",
        "skipped_policy",
        "skipped_not_applicable",
    )


def main() -> int:
    _hermetic_env()
    logging.disable(logging.CRITICAL)
    from app.config import settings

    with patch.object(settings, "data_dir", Path(_TMPDIR)), \
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
            patch.object(settings, "multimodal_max_attempts", MAX_ATOM_ATTEMPTS), \
            patch.dict(os.environ, {"ALLOW_LOCAL_SERVICES": "1"}):

        tmp1 = Path(tempfile.mkdtemp(prefix="c10_run1_"))
        r1 = asyncio.run(_drive_worker(str(tmp1 / "app.db"), tmp1, fail_times=1))
        if r1["calls"] < 2 or not _stage_succeeded(str(tmp1 / "app.db"), r1["fx"]):
            stage = _stage(str(tmp1 / "app.db"), r1["fx"])
            print(
                f"C10 CHECK: FAIL: transient atom failure was not retried within the "
                f"run - provider calls={r1['calls']} (expected >= 2), atom stage="
                f"'{stage}' - retryable outcome is terminal for the run (INGEST-011)"
            )
            return 1

        tmp2 = Path(tempfile.mkdtemp(prefix="c10_run2_"))
        r2 = asyncio.run(_drive_worker(str(tmp2 / "app.db"), tmp2, fail_times=99))
        if r2["calls"] != MAX_ATOM_ATTEMPTS:
            print(
                f"C10 CHECK: FAIL: persistent atom failure attempts not capped at "
                f"multimodal_max_attempts={MAX_ATOM_ATTEMPTS} - observed {r2['calls']} "
                f"automatic attempt(s) (INGEST-011)"
            )
            return 1

    print("C10 CHECK: PASS")
    return 0


def test_c10_atom_transient_retry() -> None:
    assert main() == 0


if __name__ == "__main__":
    raise SystemExit(main())
