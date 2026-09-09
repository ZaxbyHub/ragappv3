"""C17 - AC17 (INGEST-020): an obsolete-generation multimodal response must
publish nothing - no obsolete proxy vector insertion and no stale derived row -
while the ordinary success path stays unchanged.

Contract under test (issue #513 AC17):

  An old-generation (G1) enrichment is paused inside its provider await; a new
  generation (G2) is published meanwhile (the atom stage is re-claimed with the
  G2 fingerprint and the file's active generation moves on); the paused G1
  response then resumes. The stale-completion guard must extend to EVERY
  publication point: the resumed G1 work must insert no proxy vector into the
  vector store and leave no stale derived row. A control run with no
  interleaved generation change must behave exactly as before (proxy inserted,
  derived row + proxy_vector_id pinned) - otherwise the harness is invalid.

Pre-fix expectation (RC-11): ``_enrich_atom`` guards ``complete_atom_stage`` /
``upsert_derived`` by input fingerprint, but the proxy_record is still RETURNED
when the guard rejects, so ``_write_atom_proxies`` writes an orphan obsolete
vector -> FAIL.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

# Repo root: this file lives at <repo>/backend/tests/issue513_checks/<name>.py
ROOT = Path(__file__).resolve().parents[3]
_TMPDIR = tempfile.mkdtemp(prefix="issue513_c17_")
sys.path.insert(0, str(ROOT / "backend"))


def _hermetic_env() -> None:
    """Set test env BEFORE any app import (mirrors backend/tests/conftest.py)."""
    os.environ["ADMIN_SECRET_TOKEN"] = "test-secret"
    os.environ["USERS_ENABLED"] = "false"
    os.environ["JWT_SECRET_KEY"] = "test-jwt-secret-key-for-testing-only"
    os.environ["REDIS_URL"] = ""
    os.environ["DATA_DIR"] = _TMPDIR

G1 = "genc17v1"
G2 = "genc17v2"


class _WorkingEmbedding:
    MAX_TEXT_LENGTH = 8192
    embedding_doc_prefix = ""

    async def embed_batch(self, texts, fail_fast=False):  # noqa: ANN001, ANN202
        return [[0.25 * (i + 1) for i in range(4)] for _ in texts], []


class _CapturingVectorStore:
    def __init__(self) -> None:
        self.inserted: list[dict] = []

    async def add_chunks_then_delete_ids(self, records, old_ids):  # noqa: ANN001, ANN202
        self.inserted.extend(records)
        return 0

    async def init_table(self, embedding_dim: int) -> None:  # noqa: ANN001
        return None


def _png_bytes() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (32, 32)).save(buf, format="PNG")
    return buf.getvalue()


def _build_fixture(db_path: str, tmp: Path) -> tuple[dict, dict]:
    from app.models.database import init_db
    from app.services.artifact_store import artifact_root
    from app.services.multimodal_enrichment import compute_asset_rel_path

    init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO vaults (name) VALUES ('v')")
    vault_id = conn.execute("SELECT id FROM vaults LIMIT 1").fetchone()[0]
    conn.execute("UPDATE vaults SET multimodal_provider_enabled = 1 WHERE id = ?", (vault_id,))

    png = _png_bytes()
    asset_id = hashlib.sha256(png).hexdigest()
    rel = compute_asset_rel_path(0, G1, asset_id)  # shape only; per-file below

    files = []
    for idx, gen in enumerate((G1, G2)):
        conn.execute(
            "INSERT INTO files (vault_id, file_path, file_name, file_hash, file_size, status, "
            "active_generation_hash) VALUES (?, ?, ?, ?, ?, 'indexed', ?)",
            (vault_id, str(tmp / f"a{idx}.png"), f"a{idx}.png", f"hc17{idx}", 1, gen),
        )
        file_id = conn.execute("SELECT id FROM files ORDER BY id DESC LIMIT 1").fetchone()[0]
        atom_id = f"c17{idx}" + "0" * 24
        conn.execute(
            "INSERT INTO document_atoms (atom_id, schema_version, file_id, generation_hash, "
            "ordinal, kind, raw_text, asset_id) VALUES (?, 1, ?, ?, 0, 'image', 'ocr', ?)",
            (atom_id, file_id, gen, asset_id),
        )
        atom_pk = conn.execute(
            "SELECT id FROM document_atoms WHERE atom_id = ?", (atom_id,)
        ).fetchone()[0]
        per_rel = compute_asset_rel_path(file_id, gen, asset_id)
        asset_path = artifact_root(vault_id) / per_rel
        asset_path.parent.mkdir(parents=True, exist_ok=True)
        asset_path.write_bytes(png)
        files.append(
            {
                "file_id": file_id, "atom_pk": atom_pk, "atom_id": atom_id,
                "gen": gen, "asset_id": asset_id,
            }
        )
    conn.commit()
    conn.close()
    del rel
    return {"vault_id": vault_id, "stale": files[0], "control": files[1]}


def _make_interloper_client(pool, stale_fx: dict):
    """Client that succeeds, but first simulates the NEWER generation claiming
    the atom stage (re-claim under a G2 fingerprint) while the G1 call was
    paused at its await."""
    from app.services import enrichment_state as st
    from app.services.multimodal_enrichment import MultimodalProviderClient

    fp2 = st.compute_input_fingerprint(
        generation_hash=G2,
        asset_sha=stale_fx["asset_id"],
        neighbor_hashes=(),
        atom_schema_version=1,
        impl_version="1",
        prompt_version="v1",
        model="m",
        logical_mode="thinking",
        response_schema_version="v1",
        max_pixels=100_000_000,
        max_asset_bytes=10_000_000,
    )

    class _InterloperClient(MultimodalProviderClient):
        def __init__(self) -> None:
            super().__init__(base_url="http://127.0.0.1:11434", model="m")

        async def chat_multimodal(self, messages, max_tokens: int = 1024) -> str:  # noqa: ANN202
            self._assert_policy()
            # --- the await gap: the new generation publishes now ---
            with pool.connection() as conn:
                st.claim_atom_stage(
                    conn,
                    file_id=stale_fx["file_id"],
                    generation_hash=G1,
                    atom_pk=stale_fx["atom_pk"],
                    stage=st.ENRICH_STAGE,
                    input_fingerprint=fp2,
                    implementation_version="1",
                    model_id="m",
                    prompt_id="v1",
                    config_id="v1",
                )
                conn.execute(
                    "UPDATE files SET active_generation_hash = ? WHERE id = ?",
                    (G2, stale_fx["file_id"]),
                )
                conn.commit()
            # --- the paused old-generation response resumes ---
            return '{"description": "stale description", "retrieval_aids": ["stale"]}'

    return _InterloperClient()


def _make_plain_client():
    from app.services.multimodal_enrichment import MultimodalProviderClient

    class _PlainClient(MultimodalProviderClient):
        def __init__(self) -> None:
            super().__init__(base_url="http://127.0.0.1:11434", model="m")

        async def chat_multimodal(self, messages, max_tokens: int = 1024) -> str:  # noqa: ANN202
            self._assert_policy()
            return '{"description": "fresh description", "retrieval_aids": ["fresh"]}'

    return _PlainClient()


def _atom_dict(fx: dict) -> dict:
    return {
        "atom_pk": fx["atom_pk"],
        "atom_id": fx["atom_id"],
        "kind": "image",
        "raw_text": "ocr",
        "asset_id": fx["asset_id"],
        "page_number": 1,
        "caption": None,
    }


def _derived_row(db_path: str, fx: dict) -> dict | None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT input_fingerprint, proxy_vector_id FROM document_atom_enrichments "
            "WHERE file_id = ? AND generation_hash = ? AND atom_id = ?",
            (fx["file_id"], fx["gen"], fx["atom_id"]),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _obsolete_inserts(vstore: _CapturingVectorStore, gen: str) -> list[dict]:
    out = []
    for rec in vstore.inserted:
        try:
            meta = json.loads(rec.get("metadata", "{}"))
        except (TypeError, ValueError):
            meta = {}
        if meta.get("generation_hash") == gen:
            out.append(rec)
    return out


async def _scenario() -> str:
    import app.services.background_tasks as bt
    from app.config import settings
    from app.models.database import get_pool
    from app.services.multimodal_enrichment import ArtifactEnrichmentService

    tmp = Path(tempfile.mkdtemp(prefix="c17_db_"))
    db_path = str(tmp / "app.db")
    pool = get_pool(db_path, max_size=3)
    embedder = _WorkingEmbedding()
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

            fixture = _build_fixture(db_path, tmp)
            stale_fx, control_fx = fixture["stale"], fixture["control"]
            vault_id = fixture["vault_id"]

            processor = bt.BackgroundProcessor(
                max_retries=1, retry_delay=0.05, pool=pool,
                vector_store=vstore, embedding_service=embedder,
            )

            # ---- Control: ordinary success path must remain unchanged ----
            plain_svc = ArtifactEnrichmentService(pool=pool, client=_make_plain_client())
            control_result = await plain_svc._enrich_atom(
                atom=_atom_dict(control_fx), vault_id=vault_id,
                file_id=control_fx["file_id"], generation_hash=control_fx["gen"],
                neighbors=([], []), document_title="doc",
            )
            control_records = [control_result.get("proxy_record")] if control_result.get("proxy_record") else []
            await processor._write_atom_proxies(
                control_records, file_id=control_fx["file_id"], vault_id=vault_id,
                generation_hash=control_fx["gen"],
            )
            control_row = _derived_row(db_path, control_fx)
            if (
                not control_records
                or not _obsolete_inserts(vstore, control_fx["gen"])
                or control_row is None
                or control_row.get("proxy_vector_id") is None
            ):
                return (
                    "harness invalid: control (ordinary success path) did not produce "
                    f"proxy insert + pinned proxy_vector_id (records={len(control_records)}, "
                    f"row={control_row})"
                )

            # ---- Stale-generation run: G1 paused, G2 published, G1 resumes ----
            stale_svc = ArtifactEnrichmentService(
                pool=pool, client=_make_interloper_client(pool, stale_fx)
            )
            stale_result = await stale_svc._enrich_atom(
                atom=_atom_dict(stale_fx), vault_id=vault_id,
                file_id=stale_fx["file_id"], generation_hash=G1,
                neighbors=([], []), document_title="doc",
            )
            stale_records = [stale_result.get("proxy_record")] if stale_result.get("proxy_record") else []
            if stale_records:
                # The worker hands whatever enrich_atoms returns to the vector
                # writer; drive that write path exactly as production does.
                await processor._write_atom_proxies(
                    stale_records, file_id=stale_fx["file_id"], vault_id=vault_id,
                    generation_hash=G1,
                )
            obsolete = _obsolete_inserts(vstore, G1)
            stale_row = _derived_row(db_path, stale_fx)
            if obsolete:
                return (
                    f"obsolete proxy vector for superseded generation {G1} reached "
                    f"insertion ({len(obsolete)} record(s)) - the stale-completion "
                    f"guard does not cover the proxy publication point (INGEST-020)"
                )
            if stale_row is not None:
                return (
                    "stale derived row remains for the superseded generation "
                    "(INGEST-020)"
                )
        return ""
    finally:
        bt._processor_instance = orig_instance


def main() -> int:
    _hermetic_env()
    logging.disable(logging.CRITICAL)
    reason = asyncio.run(_scenario())
    if reason:
        print(f"C17 CHECK: FAIL: {reason}")
        return 1
    print("C17 CHECK: PASS")
    return 0


def test_c17_obsolete_proxy_superseded() -> None:
    assert main() == 0


if __name__ == "__main__":
    raise SystemExit(main())
