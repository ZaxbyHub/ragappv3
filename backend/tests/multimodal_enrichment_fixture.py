"""Shared real-SQLite fixture for multimodal enrichment tests."""

import hashlib
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image as PILImage

from app.config import settings
from app.models.database import init_db
from app.services import enrichment_state as st
from app.services import multimodal_enrichment as me
from app.services.artifact_store import artifact_root
from app.services.multimodal_enrichment import MultimodalProviderClient


class FakeEnrichClient(MultimodalProviderClient):
    """Keep provider policy checks while replacing only the HTTP round trip."""

    def __init__(self, *, response: str, base_url: str, model: str = "m"):
        super().__init__(base_url=base_url, model=model)
        self._response = response
        self.calls = 0

    async def chat_multimodal(self, messages, max_tokens: int = 1024) -> str:
        self.calls += 1
        self._assert_policy()
        return self._response


class MultimodalEnrichmentFixture(unittest.TestCase):
    """Real SQLite/database and confined PNG asset used by end-to-end tests."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name) / "data"
        self.db_path = str(Path(self._tmp.name) / "test.db")
        init_db(self.db_path)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")

        self.conn.execute("INSERT INTO vaults (name) VALUES ('v')")
        self.vault_id = self.conn.execute("SELECT id FROM vaults LIMIT 1").fetchone()["id"]
        self.conn.execute(
            "UPDATE vaults SET multimodal_provider_enabled = 1 WHERE id = ?",
            (self.vault_id,),
        )
        self.conn.execute(
            "INSERT INTO files (vault_id, file_path, file_name, file_hash, file_size, "
            "active_generation_hash) VALUES (?, ?, ?, ?, ?, ?)",
            (self.vault_id, "/img/a.png", "a.png", "h", 1, "gen123"),
        )
        self.file_id = self.conn.execute("SELECT id FROM files LIMIT 1").fetchone()["id"]
        self.generation_hash = "gen123"

        buf = io.BytesIO()
        PILImage.new("RGB", (32, 32)).save(buf, format="PNG")
        self.png_bytes = buf.getvalue()
        self.asset_id = hashlib.sha256(self.png_bytes).hexdigest()
        rel = me.compute_asset_rel_path(self.file_id, self.generation_hash, self.asset_id)

        self._data_dir_patch = patch.object(settings, "data_dir", self.data_dir)
        self._data_dir_patch.start()
        self.addCleanup(self._data_dir_patch.stop)
        asset_path = artifact_root(self.vault_id) / rel
        asset_path.parent.mkdir(parents=True, exist_ok=True)
        asset_path.write_bytes(self.png_bytes)
        self.asset_path = asset_path

        self.atom_id = "aabbccdd11223344aabbccdd11223344"
        self.conn.execute(
            "INSERT INTO document_atoms (atom_id, schema_version, file_id, generation_hash, "
            "ordinal, kind, raw_text, asset_id) VALUES (?, 1, ?, ?, 0, 'image', 'ocr text', ?)",
            (self.atom_id, self.file_id, self.generation_hash, self.asset_id),
        )
        self.conn.commit()
        self.atom_pk = self.conn.execute(
            "SELECT id FROM document_atoms WHERE atom_id = ?", (self.atom_id,)
        ).fetchone()["id"]

        self._st = st
        self._me = me
        self._patches = [
            patch.object(settings, "multimodal_enrichment_enabled", True),
            patch.object(settings, "multimodal_allowed_model_origins", ["http://127.0.0.1:11434"]),
            patch.object(settings, "multimodal_chat_url", "http://127.0.0.1:11434"),
            patch.object(settings, "multimodal_model", "m"),
            patch.object(settings, "multimodal_impl_version", "1"),
            patch.object(settings, "multimodal_prompt_version", "v1"),
            patch.object(settings, "multimodal_schema_version", "v1"),
            patch.object(settings, "multimodal_mode", "thinking"),
            patch.object(settings, "multimodal_max_pixels", 100_000_000),
            patch.object(settings, "multimodal_max_asset_bytes", 10_000_000),
        ]
        for setting_patch in self._patches:
            setting_patch.start()
            self.addCleanup(setting_patch.stop)

    def tearDown(self) -> None:
        try:
            self.conn.close()
        finally:
            self._tmp.cleanup()

    def _pool(self):
        conn = self.conn

        class _Context:
            def __enter__(self):
                return conn

            def __exit__(self, *args):
                return False

        class _Pool:
            def connection(self):
                return _Context()

        return _Pool()

    def _atom_dict(self) -> dict:
        return {
            "atom_pk": self.atom_pk,
            "atom_id": self.atom_id,
            "kind": "image",
            "raw_text": "ocr text",
            "asset_id": self.asset_id,
            "page_number": 1,
            "caption": None,
        }
