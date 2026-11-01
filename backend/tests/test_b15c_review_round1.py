"""Review-round-1 pins for issue #704 (non-frozen, additive).

1. VectorStoreError classification pin: frozen C1 exercises the
   EmbeddingError arm only; this pin holds the VECTOR_STORE_FAILED arm so
   deleting it cannot leave the suite green (implementation-review R2).
2. Attempt-cap terminal write guard: BackgroundProcessor
   ._mark_task_permanently_failed must not demote a concurrently settled row
   (the AC3 class — 'error' over intact vectors — resurfacing end-to-end at
   the retry cap; implementation-review R1).
"""

import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from app.services.vector_store import VectorStoreError


class VectorStoreClassificationPin(unittest.TestCase):
    def test_vector_store_error_classifies_vector_store_failed(self):
        from app.services.document_processor import (
            INGEST_ERROR_VECTOR_STORE_FAILED,
            classify_ingest_error,
        )

        self.assertEqual(
            classify_ingest_error(VectorStoreError("lancedb write rejected")),
            INGEST_ERROR_VECTOR_STORE_FAILED,
        )


class AttemptCapTerminalWriteGuard(unittest.TestCase):
    def test_permanent_failure_does_not_demote_settled_row(self):
        from app.models.database import init_db
        from app.services.background_tasks import BackgroundProcessor, TaskItem

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = str(Path(tmp.name) / "app.db")
        init_db(db)
        conn = sqlite3.connect(db)
        conn.execute("INSERT INTO vaults (name) VALUES ('v')")
        vid = conn.execute("SELECT id FROM vaults LIMIT 1").fetchone()[0]
        conn.execute(
            "INSERT INTO files (vault_id, file_path, file_name, file_hash, "
            "file_size, status) VALUES (?, '/x.sql', 'x.sql', 'h', 1, "
            "'indexed')",
            (vid,),
        )
        file_id = int(conn.execute("SELECT id FROM files LIMIT 1").fetchone()[0])
        # Simulate the AC3 restore having landed (or any concurrent settle):
        # the row is 'indexed' with intact vectors when the attempt cap fires.
        conn.commit()
        conn.close()

        bp = BackgroundProcessor.__new__(BackgroundProcessor)
        bp.max_retries = 3
        bp.processor = MagicMock()
        pool = MagicMock()
        live = sqlite3.connect(db)
        pool.connection.return_value.__enter__.return_value = live
        pool.connection.return_value.__exit__.return_value = False
        bp.processor.pool = pool

        bp._mark_task_permanently_failed(
            TaskItem(file_path="/x.sql", source="upload", vault_id=1, file_id=file_id),
            "boom",
        )
        row = live.execute(
            "SELECT status FROM files WHERE id = ?", (file_id,)
        ).fetchone()
        live.close()
        self.assertEqual(row[0], "indexed")

    def test_permanent_failure_still_marks_processing_row(self):
        from app.models.database import init_db
        from app.services.background_tasks import BackgroundProcessor, TaskItem

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = str(Path(tmp.name) / "app.db")
        init_db(db)
        conn = sqlite3.connect(db)
        conn.execute("INSERT INTO vaults (name) VALUES ('v')")
        vid = conn.execute("SELECT id FROM vaults LIMIT 1").fetchone()[0]
        conn.execute(
            "INSERT INTO files (vault_id, file_path, file_name, file_hash, "
            "file_size, status) VALUES (?, '/y.sql', 'y.sql', 'h', 1, "
            "'processing')",
            (vid,),
        )
        file_id = int(conn.execute("SELECT id FROM files LIMIT 1").fetchone()[0])
        conn.commit()
        conn.close()

        bp = BackgroundProcessor.__new__(BackgroundProcessor)
        bp.max_retries = 3
        bp.processor = MagicMock()
        pool = MagicMock()
        live = sqlite3.connect(db)
        pool.connection.return_value.__enter__.return_value = live
        pool.connection.return_value.__exit__.return_value = False
        bp.processor.pool = pool

        bp._mark_task_permanently_failed(
            TaskItem(file_path="/y.sql", source="upload", vault_id=1, file_id=file_id),
            "boom",
        )
        row = live.execute(
            "SELECT status FROM files WHERE id = ?", (file_id,)
        ).fetchone()
        live.close()
        self.assertEqual(row[0], "error")


if __name__ == "__main__":
    unittest.main()
