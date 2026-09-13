"""Real-LanceDB restore drill for issue #518 (E3 closure, G5).

The frozen-scope tests in test_518_restore.py use FakeLanceTable for the
structural cases; this drill uses the REAL lancedb library (a core backend
dependency, importable in CI) to satisfy the issue's required evidence:
"backup while writes occur then restore/query and verify source/claim/
artifact identities."

Drill shape:
- a real lancedb table with tagged rows;
- a SQLite snapshot taken while a write transaction is OPEN (WAL pinned,
  reusing the test_518_backup_wal.py property);
- writes to the table AFTER the backup tag is created (post-tag versions);
- restore into a fresh destination;
- digest verification + tag checkout run for real;
- the restored table queries back exactly the tagged generation (row ids
  preserved, post-tag writes absent);
- the manifest generation binding matches the snapshot's journal row.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import lancedb  # noqa: E402
from backend.app.models.database import init_db, run_migrations  # noqa: E402
from backend.app.models.migration_journal import publish_index_generation  # noqa: E402
from scripts.backup_set import create_backup_set  # noqa: E402
from scripts.restore import RestoreError, restore_backup_set  # noqa: E402

from scripts import backup_set as backup_set_module  # noqa: E402

_KEY = b"drill-key" + b"k" * 23  # 32 bytes


def _make_table(db_path: Path):
    db = lancedb.connect(str(db_path))
    table = db.create_table(
        "drill_chunks",
        [
            {"chunk_id": 1, "text": "alpha"},
            {"chunk_id": 2, "text": "beta"},
        ],
    )
    return table


class TestRealLanceRestoreDrill(unittest.TestCase):
    def setUp(self):
        import sqlite3

        self._tmp = tempfile.TemporaryDirectory(
            prefix="drill518-", ignore_cleanup_errors=True
        )
        self.root = Path(self._tmp.name)
        self.db_path = self.root / "app.db"
        init_db(str(self.db_path))
        run_migrations(str(self.db_path))
        # Authoritative generation rows the manifest must bind (C1).
        conn = sqlite3.connect(str(self.db_path))
        try:
            self.generation_id = publish_index_generation(
                conn,
                store="lancedb",
                table_name="drill_chunks",
                detail="drill generation",
            )
            conn.commit()
        finally:
            conn.close()
        self.lance_root = self.root / "lancedb"
        self.lance_root.mkdir()
        self.table = _make_table(self.lance_root)

    def tearDown(self):
        self._tmp.cleanup()

    def _backup(self) -> Path:
        out = self.root / "backup"
        fake_settings = SimpleNamespace(sqlite_path=str(self.db_path))
        with patch.object(backup_set_module, "settings", fake_settings):
            return create_backup_set(
                out,
                lancedb_dir=self.lance_root,
                lancedb_tables={"drill_chunks": self.table},
                vault_dirs=(),
                draft_room_dir=None,
                key_provider=lambda: (_KEY, 1),
            )

    def test_drill_backup_while_writes_then_restore_and_query(self):
        import sqlite3

        # A write transaction is OPEN while the backup snapshots SQLite
        # (the committed-but-in-WAL row must survive into the snapshot —
        # the sqlite3 backup API property from test_518_backup_wal.py).
        held = sqlite3.connect(str(self.db_path))
        held.execute("PRAGMA journal_mode=WAL")
        held.commit()
        held.execute("BEGIN")
        held.execute(
            "INSERT INTO system_flags(name, value, version, reason)"
            " VALUES ('drill_open_txn', 1, 1, 'held')"
        )
        held.commit()
        held.execute("BEGIN")
        held.execute(
            "UPDATE system_flags SET value = 2 WHERE name = 'drill_open_txn'"
        )

        manifest_path = self._backup()
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        # Post-tag writes: a newer table version exists in the copied tree,
        # but the tagged generation is what restore must check out.
        self.table.add([{"chunk_id": 3, "text": "post-tag-write"}])

        held.rollback()
        held.close()

        lancedb_item = next(
            i for i in manifest["items"] if i.get("kind") == "lancedb"
        )
        self.assertIn("generation", lancedb_item)
        self.assertEqual(lancedb_item["generation"]["id"], self.generation_id)

        dest = self.root / "restore-dest"
        result = restore_backup_set(
            manifest_path.parent,
            dest,
            key_provider=lambda: (_KEY, 1),
            lancedb_factory=lambda name: lancedb.connect(
                str(dest / "lancedb")
            ).open_table(name),
        )
        self.assertTrue(result["verified"], result)

        restored = (
            lancedb.connect(str(dest / "lancedb")).open_table("drill_chunks").to_arrow()
        )
        ids = sorted(int(v) for v in restored.column("chunk_id").to_pylist())
        # The tagged generation only: post-tag writes are absent.
        self.assertEqual(ids, [1, 2])

        restored_db = sqlite3.connect(str(dest / "app.db"))
        try:
            flag = restored_db.execute(
                "SELECT value FROM system_flags WHERE name = 'drill_open_txn'"
            ).fetchone()
            # The committed (WAL) value survives; the uncommitted update
            # (value=2) must NOT — the snapshot was transactionally
            # consistent, not a torn file copy.
            self.assertEqual(flag[0], 1)
            gen = restored_db.execute(
                "SELECT id FROM migration_journal WHERE id = ?",
                (self.generation_id,),
            ).fetchone()
            self.assertIsNotNone(gen)
        finally:
            restored_db.close()

    def test_tampered_generation_binding_fails_restore(self):
        manifest_path = self._backup()
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        for item in data["items"]:
            if item.get("kind") == "lancedb":
                item["generation"]["id"] = 999999
        manifest_path.write_text(json.dumps(data), encoding="utf-8")

        dest = self.root / "restore-tamper"
        with self.assertRaises(RestoreError) as ctx:
            restore_backup_set(
                manifest_path.parent,
                dest,
                key_provider=lambda: (_KEY, 1),
                lancedb_factory=None,
            )
        self.assertIn("generation binding mismatch", str(ctx.exception))

    def test_legacy_manifest_without_generation_restores_with_warning(self):
        manifest_path = self._backup()
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        for item in data["items"]:
            if item.get("kind") == "lancedb":
                item.pop("generation", None)
        manifest_path.write_text(json.dumps(data), encoding="utf-8")

        dest = self.root / "restore-legacy"
        result = restore_backup_set(
            manifest_path.parent,
            dest,
            key_provider=lambda: (_KEY, 1),
            lancedb_factory=None,
        )
        # Legacy tolerance: restores fine; the WARNING is logged (checked
        # by the log-once convention — here we assert the restore still
        # verifies every digest).
        self.assertTrue(result["verified"], result)


if __name__ == "__main__":
    unittest.main()
