"""Non-frozen shape pins for the #704 publish-failure marker (AC6 supplement).

The frozen C6 (tests/test_b15_ingest_signals.py) proves the marker EXISTS
after a compensated publish failure. These pins additionally hold the row's
SHAPE (schema-admitted status, content-free error fields, generation keying),
the marker on the re-raising materialization-failure branch, and the
connection-ownership rule of the sync fallback path (release iff own_conn).
"""

import sqlite3
import unittest
from pathlib import Path

from app.models.database import init_db, run_migrations
from app.services import artifact_store
from app.services.document_artifacts import AtomKind, DocumentAsset, DocumentAtom
from app.services.document_processor import DocumentProcessor, ParsedDocument

SCHEMA = Path(__file__).resolve().parents[1] / "app" / "models" / "database.py"


def _asset() -> DocumentAsset:
    return DocumentAsset(
        asset_id="a" * 64,
        file_id=1,
        generation_hash="genA",
        sha256="a" * 64,
        rel_path="vault-artifacts/1/genA/aa.asset",
        mime_type="image/png",
        width=1,
        height=1,
        byte_size=3,
        metadata={},
    )


def _parsed(asset: DocumentAsset, payloads: dict) -> ParsedDocument:
    return ParsedDocument(
        atoms=[
            DocumentAtom(
                atom_id="a-0",
                schema_version=1,
                file_id=1,
                generation_hash="genA",
                ordinal=0,
                kind=AtomKind.IMAGE,
                raw_text="img",
                asset_id=asset.asset_id,
            )
        ],
        assets=(asset,),
        parser_fingerprint="unstructured:test",
        asset_payloads=payloads,
    )


class _FakePool:
    """Minimal sync pool double (get_connection/release_connection)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self.checkouts = 0
        self.releases = 0

    def get_connection(self) -> sqlite3.Connection:
        self.checkouts += 1
        return self._conn

    def release_connection(self, conn: sqlite3.Connection) -> None:
        self.releases += 1


class PublishMarkerShapeTest(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.sqlite_path = str(Path(self.tmp.name) / "db.sqlite")
        init_db(self.sqlite_path)
        run_migrations(self.sqlite_path)
        self.db = sqlite3.connect(self.sqlite_path)
        self.db.row_factory = sqlite3.Row
        self.db.execute(
            "INSERT INTO files (vault_id, file_path, file_name, file_hash, "
            "file_size, status) VALUES (1, '/tmp/x.png', 'x.png', 'h', 1, "
            "'indexed')"
        )
        self.db.commit()
        self.root = Path(self.tmp.name) / "vault-artifacts"
        self.root.mkdir(parents=True, exist_ok=True)
        self.asset = _asset()
        self.payloads = {self.asset.asset_id: b"abc"}

    def tearDown(self) -> None:
        self.db.close()
        self.tmp.cleanup()

    def _processor(self, pool: _FakePool) -> DocumentProcessor:
        proc = object.__new__(DocumentProcessor)
        proc.pool = pool
        return proc


    def _fresh_marker_count(self) -> int:
        """Read the marker through a FRESH connection (issue #704 review:
        reading on the writing connection cannot detect a missing commit —
        the in-transaction view hides the durability the marker promises)."""
        fresh = sqlite3.connect(self.sqlite_path)
        try:
            return fresh.execute(
                "SELECT COUNT(*) FROM ingestion_stage_states WHERE file_id = 1"
                " AND generation_hash = 'genA' AND stage = 'publish'"
                " AND status = 'failed_retryable'"
            ).fetchone()[0]
        finally:
            fresh.close()

    def test_compensated_marker_row_shape(self):
        import unittest.mock as mock

        pool = _FakePool(self.db)
        proc = self._processor(pool)
        with (
            mock.patch.object(
                artifact_store,
                "artifact_root",
                lambda vault_id, settings_obj=None: self.root,
            ),
            mock.patch(
                "app.services.document_processor.artifact_store.publish_generation",
                side_effect=RuntimeError("publish boom"),
            ),
        ):
            DocumentProcessor._publish_artifacts(
                proc, 1, 1, "genA", _parsed(self.asset, self.payloads)
            )
        self.db.close()
        fresh = sqlite3.connect(self.sqlite_path)
        fresh.row_factory = sqlite3.Row
        try:
            row = fresh.execute(
                "SELECT * FROM ingestion_stage_states WHERE file_id = 1 "
                "AND generation_hash = 'genA' AND stage = 'publish'"
            ).fetchone()
        finally:
            fresh.close()
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "failed_retryable")
        self.assertEqual(row["error_code"], "PUBLISH_COMPENSATED")
        # Redaction boundary (#562): no raw exception text reaches the row.
        self.assertNotIn("boom", (row["error_message"] or ""))
        self.assertNotIn("boom", (row["error_code"] or ""))

    def test_materialization_failure_marks_and_raises(self):
        import unittest.mock as mock

        pool = _FakePool(self.db)
        proc = self._processor(pool)
        with (
            mock.patch.object(
                artifact_store,
                "artifact_root",
                lambda vault_id, settings_obj=None: self.root,
            ),
            mock.patch(
                "app.services.document_processor.artifact_store.store_asset_bytes",
                side_effect=OSError("disk full"),
            ),
        ):
            with self.assertRaises(OSError):
                DocumentProcessor._publish_artifacts(
                    proc, 1, 1, "genA", _parsed(self.asset, self.payloads)
                )
        self.db.close()
        self.assertEqual(self._fresh_marker_count(), 1)
        self.db = sqlite3.connect(self.sqlite_path)

    def test_fallback_conn_ownership_no_leak(self):
        import unittest.mock as mock

        pool = _FakePool(self.db)
        proc = self._processor(pool)
        with (
            mock.patch.object(
                artifact_store,
                "artifact_root",
                lambda vault_id, settings_obj=None: self.root,
            ),
            mock.patch(
                "app.services.document_processor.artifact_store.publish_generation",
                side_effect=RuntimeError("publish boom"),
            ),
        ):
            DocumentProcessor._publish_artifacts(
                proc, 1, 1, "genA", _parsed(self.asset, self.payloads)
            )
        # Ownership rule: the fallback released exactly what it checked out.
        self.assertEqual(pool.checkouts, 1)
        self.assertEqual(pool.releases, 1)


if __name__ == "__main__":
    unittest.main()
